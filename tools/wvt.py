#!/usr/bin/env python3
"""wvt — woow release train version tool (stdlib only).

The single source of truth is ``woow-versions.yaml``. HA and PaaS both align to
it. This tool reads the manifest, extracts the version pins of each leg and
compares them with the manifest.

Commands
  validate   schema + internal consistency + exception expiry
  check      compare the pins found in one checked-out repo (offline)
  drift      fetch every leg's current pin from the real sources and render a
             markdown report (product x leg x observation point)
  issue      create / update / close the single tracking issue on Gitea

Phase 0 is report-only: ``check`` and ``drift`` exit 0 whatever they find,
unless ``--fail-on`` (or ``--strict``) asks otherwise.

Only Python 3 standard library is used, so the tool runs on any runner image.
The manifest is YAML restricted to a small subset (block mappings, block
sequences, single-line flow collections, quoted/plain scalars, comments, block
scalars). ``load_yaml`` below parses that subset; it is also used to read
Chart.yaml / config.yaml files of the legs. Anchors, aliases, tags and multiple
documents are not supported.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

SCHEMA_VERSION = 1
LEGS = ("ha", "paas")  # Podman / k3s are out of scope (owner decision 2026-10-16)
POLICY_MODES = ("lockstep", "range", "independent")
EXTRACTORS = ("dockerfile_arg", "yaml", "json", "seed_image", "seed_field", "catalog")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REPO_RE = re.compile(r"^(github|gitea):[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
HARD_RULES = ("license_ceiling", "min_version", "digest", "shared_files", "exception_expired")
EXPIRY_WARN_DAYS = 14
TRACKING_MARKER = "<!-- wvt:drift-tracking -->"
TAIPEI = _dt.timezone(_dt.timedelta(hours=8))  # day boundaries for expiry / days-behind

# Status vocabulary. DRIFT_STATUSES make the drift report "dirty".
STATUS_LABELS = {
    "ok": "✅ 一致",
    "info": "ℹ️ 參考",
    "excepted": "🟡 例外中",
    "behind": "🟠 落後",
    "ahead": "🔵 領先",
    "mismatch": "🔴 不符",
    "violation": "⛔ 違反政策",
    "unknown": "❔ 無法判讀",
    "unreachable": "⚠️ 無法取得",
}
DRIFT_STATUSES = {"behind", "ahead", "mismatch", "violation", "unknown", "unreachable"}


class WvtError(Exception):
    pass


# --------------------------------------------------------------------------
# Minimal YAML subset loader
# --------------------------------------------------------------------------

_BLOCK_INDICATOR = re.compile(r"^[|>][+-]?[0-9]?$")
_INT_RE = re.compile(r"^[-+]?(0|[1-9][0-9]*)$")
_FLOAT_RE = re.compile(r"^[-+]?([0-9]+\.[0-9]*|\.[0-9]+)([eE][-+]?[0-9]+)?$")


def _strip_comment(line: str) -> str:
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if quote == '"' and ch == "\\":
                continue
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            # quotes only open a scalar at token start
            if i == 0 or line[i - 1] in " \t:[{,-":
                quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


def _split_key(content: str):
    """Return (key, rest) if content is 'key: value' / 'key:'; else None."""
    if not content or content[0] in "[{":
        return None
    if content[0] in ("'", '"'):
        q = content[0]
        j = 1
        while j < len(content):
            if content[j] == "\\" and q == '"':
                j += 2
                continue
            if content[j] == q:
                if q == "'" and j + 1 < len(content) and content[j + 1] == "'":
                    j += 2
                    continue
                break
            j += 1
        after = content[j + 1:]
        if after.startswith(":") and (len(after) == 1 or after[1] in " \t"):
            return _scalar(content[: j + 1]), after[1:].strip()
        return None
    m = re.search(r":(\s|$)", content)
    if not m:
        return None
    key = content[: m.start()].strip()
    if not key or key.startswith("- "):
        return None
    return key, content[m.end():].strip()


def _unquote_double(s: str) -> str:
    out = []
    i = 0
    esc = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/", "0": "\0", " ": " "}
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            nx = s[i + 1]
            if nx in esc:
                out.append(esc[nx])
                i += 2
                continue
            if nx == "u" and i + 5 < len(s):
                out.append(chr(int(s[i + 2: i + 6], 16)))
                i += 6
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _scalar(text: str):
    t = text.strip()
    if t == "":
        return None
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        return _unquote_double(t[1:-1])
    if len(t) >= 2 and t[0] == "'" and t[-1] == "'":
        return t[1:-1].replace("''", "'")
    if t in ("null", "Null", "NULL", "~"):
        return None
    if t in ("true", "True", "TRUE"):
        return True
    if t in ("false", "False", "FALSE"):
        return False
    if _INT_RE.match(t):
        return int(t)
    if _FLOAT_RE.match(t):
        return float(t)
    return t


class _Flow:
    def __init__(self, s: str):
        self.s = s
        self.i = 0

    def ws(self):
        while self.i < len(self.s) and self.s[self.i] in " \t\n":
            self.i += 1

    def parse(self):
        self.ws()
        ch = self.s[self.i]
        if ch == "[":
            self.i += 1
            out = []
            while True:
                self.ws()
                if self.s[self.i] == "]":
                    self.i += 1
                    return out
                out.append(self.parse())
                self.ws()
                if self.s[self.i] == ",":
                    self.i += 1
        if ch == "{":
            self.i += 1
            out = {}
            while True:
                self.ws()
                if self.s[self.i] == "}":
                    self.i += 1
                    return out
                key = self.atom(key=True)
                self.ws()
                if self.s[self.i] != ":":
                    raise WvtError("flow mapping: expected ':' in %r" % self.s)
                self.i += 1
                out[key] = self.parse()
                self.ws()
                if self.s[self.i] == ",":
                    self.i += 1
        return self.atom()

    def atom(self, key=False):
        self.ws()
        ch = self.s[self.i]
        if ch in ("'", '"'):
            j = self.i + 1
            while j < len(self.s):
                if ch == '"' and self.s[j] == "\\":
                    j += 2
                    continue
                if self.s[j] == ch:
                    if ch == "'" and j + 1 < len(self.s) and self.s[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            tok = self.s[self.i: j + 1]
            self.i = j + 1
            return _scalar(tok)
        j = self.i
        stops = ",]}" + (":" if key else "")
        while j < len(self.s) and self.s[j] not in stops:
            if not key and self.s[j] == ":" and j + 1 < len(self.s) and self.s[j + 1] in " ,]}":
                break
            j += 1
        tok = self.s[self.i: j]
        self.i = j
        return _scalar(tok)


def _parse_flow(text: str):
    return _Flow(text).parse()


def _tokenize(text: str):
    """Turn YAML text into logical tokens (indent, content, block_scalar|None)."""
    raw = text.replace("\t", "    ").splitlines()
    toks = []
    i = 0
    while i < len(raw):
        line = raw[i]
        stripped = line.strip()
        if stripped in ("---", "...") and not line.startswith(" "):
            i += 1
            continue
        content = _strip_comment(line)
        if not content.strip():
            i += 1
            continue
        indent = len(content) - len(content.lstrip(" "))
        content = content.strip()
        # explode "- " sequence markers into their own tokens
        dash_indent = None
        while content == "-" or content.startswith("- "):
            dash_indent = indent
            toks.append([indent, "-", None])
            rest = content[1:]
            pad = len(rest) - len(rest.lstrip(" "))
            indent = indent + 1 + pad
            content = rest.strip()
            if not content:
                break
        if not content:
            i += 1
            continue
        # block scalar?
        kv = _split_key(content)
        val = kv[1] if kv else content
        # multi-line quoted scalar: fold continuation lines with single spaces
        if val and val[0] in "'\"" and not _quote_closed(val):
            j = i
            joined = val
            while j + 1 < len(raw) and not _quote_closed(joined):
                j += 1
                joined = _fold(joined, raw[j].strip())
            content = content[: len(content) - len(val)] + joined
            val = joined
            i = j
        elif val and val[0] not in "[{|>&*!" and not _BLOCK_INDICATOR.match(val):
            # multi-line plain scalar: continuation lines are more indented than the key / dash
            # (a bare scalar line, e.g. under an empty "key:", continues at its own indent)
            parent = indent if kv else dash_indent if dash_indent is not None else indent - 1
            j = i
            joined = val
            while j + 1 < len(raw):
                nxt = raw[j + 1].replace("\t", "    ")
                nstrip = _strip_comment(nxt).strip()
                nind = len(nxt) - len(nxt.lstrip(" "))
                if not nxt.strip():
                    k = j + 1
                    while k < len(raw) and not raw[k].strip():
                        k += 1
                    if k < len(raw) and (len(raw[k]) - len(raw[k].lstrip(" "))) > parent and not kv_like(raw[k]):
                        joined += "\n" * (k - j - 1)
                        j = k - 1
                        continue
                    break
                if nind <= parent or nstrip.startswith("- ") or nstrip == "-" or kv_like(nxt) or nstrip.startswith("#"):
                    break
                sep = "" if joined.endswith("\n") else " "
                joined += sep + nstrip
                j += 1
            if j != i:
                content = content[: len(content) - len(val)] + joined
                val = joined
                i = j
        if _BLOCK_INDICATOR.match(val or ""):
            parent = indent if kv or dash_indent is None else dash_indent
            block = []
            j = i + 1
            while j < len(raw):
                nxt = raw[j].replace("\t", "    ")
                if nxt.strip() == "" or (len(nxt) - len(nxt.lstrip(" "))) > parent:
                    block.append(nxt)
                    j += 1
                else:
                    break
            while block and not block[-1].strip():
                block.pop()
            ind = min((len(b) - len(b.lstrip(" ")) for b in block if b.strip()), default=0)
            lines = [b[ind:] if b.strip() else "" for b in block]
            if val.startswith("|"):
                body = "\n".join(lines)
            else:
                paras, cur = [], []
                for ln in lines:
                    if ln == "":
                        paras.append(" ".join(cur))
                        cur = []
                    else:
                        cur.append(ln)
                paras.append(" ".join(cur))
                body = "\n".join(paras)
            if "-" not in val[1:]:
                body += "\n"
            toks.append([indent, content, body])
            i = j
            continue
        # multi-line flow collection
        if val and val[0] in "[{":
            depth = _flow_depth(val)
            j = i
            while depth > 0 and j + 1 < len(raw):
                j += 1
                more = _strip_comment(raw[j]).strip()
                content += " " + more
                depth += _flow_depth(more)
            i = j
        toks.append([indent, content, None])
        i += 1
    return toks


def kv_like(line: str) -> bool:
    return _split_key(_strip_comment(line).strip()) is not None


def _fold(acc: str, nxt: str) -> str:
    """YAML flow folding: a blank line is a newline, otherwise lines join with one space."""
    if nxt == "":
        return acc + "\n"
    return acc + ("" if acc.endswith("\n") else " ") + nxt


def _quote_closed(v: str) -> bool:
    q = v[0]
    j = 1
    while j < len(v):
        if q == '"' and v[j] == "\\":
            j += 2
            continue
        if v[j] == q:
            if q == "'" and j + 1 < len(v) and v[j + 1] == "'":
                j += 2
                continue
            return True
        j += 1
    return False


def _flow_depth(s: str) -> int:
    d = 0
    quote = None
    for ch in s:
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch in "[{":
            d += 1
        elif ch in "]}":
            d -= 1
    return d


class _Parser:
    def __init__(self, toks):
        self.t = toks
        self.i = 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else None

    def node(self, indent):
        tok = self.peek()
        if tok is None or tok[0] < indent:
            return None
        ind, content, _ = tok
        if content == "-":
            return self.seq(ind)
        if _split_key(content) is not None:
            return self.mapping(ind)
        self.i += 1
        return self.value_text(content, tok)

    def value_text(self, text, tok):
        if tok[2] is not None:
            return tok[2]
        if text and text[0] in "[{":
            return _parse_flow(text)
        return _scalar(text)

    def mapping(self, ind):
        out = {}
        while True:
            tok = self.peek()
            if tok is None or tok[0] < ind:
                return out
            if tok[0] > ind:
                raise WvtError("yaml: unexpected indentation at %r" % tok[1])
            if tok[1] == "-":
                return out
            kv = _split_key(tok[1])
            if kv is None:
                raise WvtError("yaml: expected 'key: value', got %r" % tok[1])
            key, rest = kv
            if key in out:
                raise WvtError("yaml: duplicate key %r" % key)
            self.i += 1
            if tok[2] is not None:
                out[key] = tok[2]
            elif rest == "":
                nxt = self.peek()
                if nxt is None:
                    out[key] = None
                elif nxt[0] > ind:
                    out[key] = self.node(nxt[0])
                elif nxt[0] == ind and nxt[1] == "-":
                    out[key] = self.seq(ind)
                else:
                    out[key] = None
            else:
                out[key] = self.value_text(rest, tok)

    def seq(self, ind):
        out = []
        while True:
            tok = self.peek()
            if tok is None or tok[0] != ind or tok[1] != "-":
                return out
            self.i += 1
            nxt = self.peek()
            if nxt is None or nxt[0] <= ind:
                out.append(None)
            else:
                out.append(self.node(nxt[0]))


def load_yaml(text: str):
    toks = _tokenize(text)
    if not toks:
        return None
    p = _Parser(toks)
    val = p.node(toks[0][0])
    if p.i != len(toks):
        raise WvtError("yaml: could not parse near %r" % toks[p.i][1])
    return val


# --------------------------------------------------------------------------
# Versions, digests, image references
# --------------------------------------------------------------------------

_VER_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?$")


def parse_version(v):
    if v is None:
        return None
    m = _VER_RE.match(str(v).strip())
    if not m:
        return None
    return tuple(int(x) if x is not None else 0 for x in m.groups())


def cmp_versions(a, b):
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return None
    return (pa > pb) - (pa < pb)


def satisfies(version, constraint: str) -> bool:
    """constraint: '<5.9.0', '<=x', '>=x', '>x', '==x' (comma = AND)."""
    for part in constraint.split(","):
        part = part.strip()
        m = re.match(r"^(<=|>=|==|<|>)\s*(.+)$", part)
        if not m:
            raise WvtError("bad constraint %r" % constraint)
        op, ref = m.groups()
        c = cmp_versions(version, ref)
        if c is None:
            raise WvtError("cannot compare %r with %r" % (version, ref))
        ok = {"<": c < 0, "<=": c <= 0, ">": c > 0, ">=": c >= 0, "==": c == 0}[op]
        if not ok:
            return False
    return True


def norm_digest(value):
    if value is None:
        return None
    v = str(value).strip()
    if re.match(r"^[0-9a-f]{64}$", v):
        return "sha256:" + v
    return v if DIGEST_RE.match(v) else None


def parse_image_ref(ref: str) -> dict:
    """'reg/repo:tag@sha256:x' -> {repository, tag, digest}. Also accepts 'tag@sha256:x'."""
    ref = str(ref).strip()
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
        digest = norm_digest(digest)
    repo, tag = ref, None
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = ref.rsplit(":", 1)
    if "/" not in ref and ":" not in ref and ref:
        # bare tag such as '20261013.2012'
        repo, tag = None, ref
    return {"repository": repo or None, "tag": tag or None, "digest": digest}


def short(d, n=12):
    if not d:
        return "—"
    d = str(d)
    return d[7: 7 + n] if d.startswith("sha256:") else d[:n]


def dig(obj, path: str):
    cur = obj
    for seg in path.split("."):
        if isinstance(cur, dict):
            if seg not in cur:
                return None
            cur = cur[seg]
        elif isinstance(cur, list) and seg.isdigit() and int(seg) < len(cur):
            cur = cur[int(seg)]
        else:
            return None
    return cur


# --------------------------------------------------------------------------
# Extractors: (file text, pin spec) -> raw value
# --------------------------------------------------------------------------


def extract_dockerfile_arg(text: str, name: str):
    args = {}
    found = None
    for line in text.splitlines():
        m = re.match(r"^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)(?:=(.*))?\s*$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if val is None:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        val = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda mm: args.get(mm.group(1), mm.group(0)), val)
        args.setdefault(key, val)
        if key == name and found is None:
            found = val
    return found


def extract_yaml(text: str, path: str):
    return dig(load_yaml(text), path)


def extract_json(text: str, path: str):
    return dig(json.loads(text), path)


def _seed_record(text: str, record: str):
    root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    for rec in root.iter("record"):
        if rec.get("id") == record:
            return rec
    return None


def extract_seed_field(text: str, record: str, field: str):
    rec = _seed_record(text, record)
    if rec is None:
        return None
    for f in rec.findall("field"):
        if f.get("name") == field:
            return (f.text or "").strip()
    return None


def extract_seed_image(text: str, record: str, image_path: str = "image"):
    """Image of an Odoo seed record: helm_default_values JSON -> image.{repository,tag}."""
    raw = extract_seed_field(text, record, "helm_default_values")
    if not raw:
        return None
    vals = json.loads(raw)
    img = dig(vals, image_path)
    if not isinstance(img, dict):
        return None
    repo = img.get("repository")
    tag = img.get("tag") or ""
    ref = parse_image_ref(tag) if tag else {"tag": None, "digest": None}
    digest = ref.get("digest") or norm_digest(img.get("digest"))
    return {"repository": repo, "tag": ref.get("tag"), "digest": digest,
            "raw": "%s:%s" % (repo, tag) if repo else tag}


def extract_catalog(text: str, comp_id: str):
    data = json.loads(text)
    comp = (data.get("components") or {}).get(comp_id)
    if not comp:
        return None
    return {"version": comp.get("version"), "source_sha": comp.get("source_sha"),
            "repository": comp.get("repository"), "source_path": comp.get("source_path")}


def run_extractor(pin: dict, text: str):
    kind = pin["kind"]
    if kind == "dockerfile_arg":
        return extract_dockerfile_arg(text, pin["name"])
    if kind == "yaml":
        return extract_yaml(text, pin["path"])
    if kind == "json":
        return extract_json(text, pin["path"])
    if kind == "seed_field":
        return extract_seed_field(text, pin["record"], pin["field"])
    if kind == "seed_image":
        return extract_seed_image(text, pin["record"], pin.get("image_path", "image"))
    if kind == "catalog":
        return extract_catalog(text, pin["id"])
    raise WvtError("unknown extractor %r" % kind)


def observe(pin: dict, raw) -> dict:
    """Normalise an extracted raw value into {raw, version, digest, tag}."""
    obs = {"raw": raw, "version": None, "digest": None, "tag": None}
    if raw is None:
        return obs
    if isinstance(raw, dict):  # seed_image / catalog
        obs["raw"] = raw.get("raw") or raw.get("version")
        obs["digest"] = raw.get("digest")
        obs["tag"] = raw.get("tag")
        if raw.get("version"):
            obs["version"] = str(raw["version"])
        return obs
    value = str(raw)
    parse = pin.get("parse", "version")
    if parse == "image_ref":
        ref = parse_image_ref(value)
        obs["digest"], obs["tag"] = ref["digest"], ref["tag"]
        if ref["tag"] and parse_version(ref["tag"]):
            obs["version"] = ref["tag"].lstrip("v") if pin.get("strip_v", True) else ref["tag"]
    elif parse == "digest":
        obs["digest"] = norm_digest(value)
    else:
        v = value
        if pin.get("strip_v") and v[:1] in ("v", "V"):
            v = v[1:]
        obs["version"] = v
    return obs


# --------------------------------------------------------------------------
# Manifest model + validation
# --------------------------------------------------------------------------


def load_manifest(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith(".json"):
        return json.loads(text)
    return load_yaml(text)


def iter_pins(product: dict, leg: str):
    legd = (product.get("legs") or {}).get(leg) or {}
    default_repo = legd.get("repo")
    for pin in legd.get("pins") or []:
        p = dict(pin)
        p.setdefault("repo", default_repo)
        p.setdefault("ref", legd.get("ref", "main"))
        yield p


def target_value(product: dict, compare: str):
    tgt = product.get("target") or {}
    if compare == "version":
        return tgt.get("version")
    return dig(tgt, compare)


def known_entries(product: dict):
    tgt = dict(product.get("target") or {})
    tgt["_is_target"] = True
    yield tgt
    for kv in product.get("known_versions") or []:
        yield kv


def resolve_digest(product: dict, digest: str):
    """digest -> (version, form) using target + known_versions, else (None, None)."""
    if not digest:
        return None, None
    for e in known_entries(product):
        if e.get("index_digest") == digest:
            return e.get("version"), "index_digest"
        for arch, d in (e.get("platforms") or {}).items():
            if d == digest:
                return e.get("version"), "platforms." + arch
        for name, d in (e.get("artifacts") or {}).items():
            if d == digest:
                return e.get("version"), "artifacts." + name
    return None, None


def _days_between(a: str, b: _dt.date) -> int:
    return (b - _dt.date.fromisoformat(a)).days


def validate_manifest(m: dict, today: _dt.date | None = None):
    """Return (errors, warnings)."""
    today = today or _dt.datetime.now(TAIPEI).date()
    errors, warnings = [], []
    E, W = errors.append, warnings.append
    if not isinstance(m, dict):
        return ["manifest is not a mapping"], []
    if m.get("schema") != SCHEMA_VERSION:
        E("schema must be %d (got %r)" % (SCHEMA_VERSION, m.get("schema")))
    if not isinstance(m.get("phase"), int) or m.get("phase") not in (0, 1, 2, 3):
        E("phase must be one of 0..3")
    srcs = m.get("sources") or {}
    for name, s in srcs.items():
        if not REPO_RE.match(str(s.get("repo", ""))):
            E("sources.%s.repo invalid: %r" % (name, s.get("repo")))
        if not s.get("file"):
            E("sources.%s.file missing" % name)
    prods = m.get("products")
    if not isinstance(prods, dict) or not prods:
        E("products must be a non-empty mapping")
        return errors, warnings
    for pname, p in prods.items():
        where = "products.%s" % pname
        if not isinstance(p, dict):
            E("%s must be a mapping" % where)
            continue
        up = p.get("upstream") or {}
        if not up.get("image"):
            E("%s.upstream.image missing" % where)
        pol = p.get("policy") or {}
        mode = pol.get("mode")
        if mode not in POLICY_MODES:
            E("%s.policy.mode must be one of %s" % (where, POLICY_MODES))
        for r in pol.get("hard_rules") or []:
            if r not in HARD_RULES:
                E("%s.policy.hard_rules: unknown rule %r" % (where, r))
        tgt = p.get("target") or {}
        tv = tgt.get("version")
        if not isinstance(tv, str) or parse_version(tv) is None:
            E("%s.target.version must be a quoted version string" % where)
        if tgt.get("since") is not None and not (isinstance(tgt["since"], str) and DATE_RE.match(tgt["since"])):
            E("%s.target.since must be a quoted YYYY-MM-DD string" % where)
        for k in ("index_digest",):
            if tgt.get(k) is not None and not DIGEST_RE.match(str(tgt[k])):
                E("%s.target.%s is not sha256:<64 hex>" % (where, k))
        for grp in ("platforms", "artifacts"):
            for arch, d in (tgt.get(grp) or {}).items():
                if not DIGEST_RE.match(str(d)):
                    E("%s.target.%s.%s is not sha256:<64 hex>" % (where, grp, arch))
        if "license_ceiling" in pol:
            try:
                if tv and not satisfies(tv, pol["license_ceiling"]):
                    E("%s.target.version %s breaks license_ceiling %s" % (where, tv, pol["license_ceiling"]))
            except WvtError as exc:
                E("%s.policy.license_ceiling: %s" % (where, exc))
        if mode == "range":
            mv = pol.get("min_version")
            if not isinstance(mv, str) or parse_version(mv) is None:
                E("%s.policy.min_version required (quoted) for range mode" % where)
            elif tv and cmp_versions(mv, tv) > 0:
                E("%s.policy.min_version %s is above target %s" % (where, mv, tv))
            if not isinstance(pol.get("max_days_behind"), int):
                E("%s.policy.max_days_behind (int) required for range mode" % where)
            if not tgt.get("since"):
                E("%s.target.since required for range mode (max_days_behind clock)" % where)
        seen = {}
        for kv in known_entries(p):
            label = "target" if kv.get("_is_target") else "known_versions[%s]" % kv.get("version")
            if not kv.get("_is_target"):
                if not isinstance(kv.get("version"), str) or parse_version(kv.get("version")) is None:
                    E("%s.%s.version must be a quoted version" % (where, label))
                if tv and kv.get("version") == tv:
                    E("%s.%s duplicates the target version" % (where, label))
            ds = [kv.get("index_digest")] + list((kv.get("platforms") or {}).values()) + list((kv.get("artifacts") or {}).values())
            for d in ds:
                if d is None:
                    continue
                if not DIGEST_RE.match(str(d)):
                    E("%s.%s digest %r invalid" % (where, label, d))
                if d in seen and seen[d] != label:
                    E("%s digest %s appears in both %s and %s" % (where, short(d), seen[d], label))
                seen[d] = label
        legs = p.get("legs") or {}
        for leg in legs:
            if leg not in LEGS:
                E("%s.legs.%s is out of scope (only %s; Podman/k3s are not modelled)" % (where, leg, "/".join(LEGS)))
        for leg in LEGS:
            if leg not in legs:
                W("%s has no %s leg" % (where, leg))
                continue
            ids = set()
            for pin in iter_pins(p, leg):
                pw = "%s.legs.%s.pins[%s]" % (where, leg, pin.get("id"))
                if not pin.get("id"):
                    E("%s: id missing" % pw)
                elif pin["id"] in ids:
                    E("%s: duplicate id" % pw)
                ids.add(pin.get("id"))
                if not REPO_RE.match(str(pin.get("repo") or "")):
                    E("%s: repo must be github:OWNER/REPO or gitea:OWNER/REPO" % pw)
                if not pin.get("file"):
                    E("%s: file missing" % pw)
                kind = pin.get("kind")
                if kind not in EXTRACTORS:
                    E("%s: kind must be one of %s" % (pw, EXTRACTORS))
                need = {"dockerfile_arg": ["name"], "yaml": ["path"], "json": ["path"], "seed_image": ["record"],
                        "seed_field": ["record", "field"], "catalog": ["id"]}.get(kind, [])
                for k in need:
                    if not pin.get(k):
                        E("%s: %s required for kind %s" % (pw, k, kind))
                cmp_ = pin.get("compare", "version")
                if cmp_ not in ("version", "index_digest", "info") and not re.match(r"^(platforms|artifacts)\.[A-Za-z0-9_.-]+$", cmp_):
                    E("%s: compare %r invalid" % (pw, cmp_))
                elif cmp_ not in ("version", "info") and target_value(p, cmp_) is None:
                    E("%s: compare %s but target has no such digest" % (pw, cmp_))
                ref = pin.get("ref")
                if isinstance(ref, str) and ref.startswith("$") and ref[1:] not in srcs:
                    E("%s: ref %s refers to an unknown source" % (pw, ref))
            pkgs = legs[leg].get("package_version") or []
            for pkg in ([pkgs] if isinstance(pkgs, dict) else pkgs):
                kw = "%s.legs.%s.package_version[%s]" % (where, leg, pkg.get("label"))
                if pkg.get("kind") not in EXTRACTORS or not pkg.get("file"):
                    E("%s: kind/file required" % kw)
                if not REPO_RE.match(str(pkg.get("repo") or legs[leg].get("repo") or "")):
                    E("%s: repo missing" % kw)
            pub = legs[leg].get("published")
            if pub is not None:
                if pub.get("kind") != "catalog" or not pub.get("id"):
                    E("%s.legs.%s.published must be {kind: catalog, id: ...}" % (where, leg))
                if not REPO_RE.match(str(pub.get("repo") or "")):
                    E("%s.legs.%s.published.repo invalid" % (where, leg))
        for sf in p.get("shared_files") or []:
            src = sf.get("source") or {}
            if not REPO_RE.match(str(src.get("repo") or "")):
                E("%s.shared_files.source.repo invalid" % where)
            if not SHA1_RE.match(str(src.get("commit") or "")):
                E("%s.shared_files.source.commit must be a full 40-hex sha" % where)
            if not src.get("files"):
                E("%s.shared_files.source.files must list the shared files" % where)
            for c in sf.get("consumers") or []:
                if c.get("leg") not in LEGS or not REPO_RE.match(str(c.get("repo") or "")) or not c.get("lock"):
                    E("%s.shared_files.consumers entry needs leg/repo/lock" % where)
        for ex in p.get("exceptions") or []:
            ew = "%s.exceptions[%s]" % (where, ex.get("what"))
            if ex.get("leg") not in legs:
                E("%s: leg %r is not a leg of this product" % (ew, ex.get("leg")))
            for k in ("what", "reason", "approved_by"):
                if not ex.get(k):
                    E("%s: %s required" % (ew, k))
            exp = ex.get("expires")
            if not (isinstance(exp, str) and DATE_RE.match(exp)):
                E("%s: expires must be a quoted YYYY-MM-DD string" % ew)
                continue
            left = -_days_between(exp, today)
            if left < 0:
                E("%s: expired on %s — renew with a reason or remove the exception" % (ew, exp))
            elif left <= EXPIRY_WARN_DAYS:
                W("%s: expires in %d days (%s)" % (ew, left, exp))
            for s in ex.get("waives") or []:
                if s not in ("behind", "ahead", "mismatch"):
                    E("%s: waives may only list behind/ahead/mismatch" % ew)
    obs = m.get("observations") or []
    for o in obs:
        if not (isinstance(o.get("as_of"), str) and DATE_RE.match(o["as_of"])):
            E("observations: as_of must be a quoted YYYY-MM-DD string")
        elif _days_between(o["as_of"], today) > 14:
            W("observations snapshot for %s is %d days old" % (o.get("product"), _days_between(o["as_of"], today)))
    return errors, warnings


# --------------------------------------------------------------------------
# Policy evaluation
# --------------------------------------------------------------------------


def finding(status, rule, message, **kw):
    d = {"status": status, "rule": rule, "message": message}
    d.update(kw)
    return d


def active_exceptions(product: dict, leg: str, today: _dt.date, pin_id: str | None = None):
    out = []
    for ex in product.get("exceptions") or []:
        if ex.get("leg") != leg:
            continue
        if ex.get("pin") and pin_id and ex["pin"] != pin_id:
            continue
        exp = ex.get("expires")
        expired = bool(exp and DATE_RE.match(str(exp)) and _days_between(exp, today) > 0)
        out.append(dict(ex, expired=expired))
    return out


def evaluate(product: dict, leg: str, pin: dict, obs: dict, today: _dt.date | None = None) -> dict:
    """Compare one observation with the manifest. Returns {status, version, findings}."""
    today = today or _dt.datetime.now(TAIPEI).date()
    pol = product.get("policy") or {}
    mode = pol.get("mode")
    tgt = product.get("target") or {}
    tv = tgt.get("version")
    compare = pin.get("compare", "version")
    findings = []
    if compare == "info":
        return {"status": "info", "version": obs.get("version"),
                "findings": [finding("info", "info", "參考值（不跨邊比較）")]}
    if obs.get("raw") is None and obs.get("version") is None and obs.get("digest") is None:
        return {"status": "unknown", "version": None,
                "findings": [finding("unknown", "extract", "抽不到釘點（檔案或欄位不存在）")]}
    version = obs.get("version")
    digest_ok = None
    if compare != "version":
        want = target_value(product, compare)
        got = obs.get("digest")
        if got is None:
            return {"status": "unknown", "version": version,
                    "findings": [finding("unknown", "extract", "釘點沒有 digest，無法比對 %s" % compare)]}
        digest_ok = (got == want)
        if not digest_ok:
            rv, form = resolve_digest(product, got)
            if rv is not None:
                if version and version != rv:
                    findings.append(finding("mismatch", "digest",
                                            "tag 寫 %s，但 digest 屬於 %s" % (version, rv)))
                version = rv
                if form != compare:
                    findings.append(finding("info", "digest_form",
                                            "digest 形式是 %s（清冊比 %s）" % (form, compare)))
            else:
                findings.append(finding("mismatch", "digest",
                                        "digest %s 不是清冊目標或任何已知版本" % short(got)))
        else:
            if version and cmp_versions(version, tv) != 0:
                findings.append(finding("mismatch", "digest",
                                        "tag 寫 %s，但 digest 是清冊目標 %s" % (version, tv)))
            version = tv
    # license ceiling (upstream version only — never the package version)
    if pol.get("license_ceiling") and version and parse_version(version):
        if not satisfies(version, pol["license_ceiling"]):
            findings.append(finding("violation", "license_ceiling",
                                    "%s 超過授權上限 %s" % (version, pol["license_ceiling"])))
    c = cmp_versions(version, tv) if version else None
    if c is None and digest_ok is not True:
        if not any(f["status"] == "mismatch" for f in findings):
            findings.append(finding("unknown", "version", "無法解析版本 %r" % (version,)))
    elif mode == "lockstep":
        if c != 0 or digest_ok is False:
            st = "behind" if (c is not None and c < 0) else "ahead" if (c is not None and c > 0) else "mismatch"
            findings.append(finding(st, "digest" if compare != "version" else "lockstep",
                                    "lockstep：%s ≠ 清冊 %s" % (version, tv)))
    elif mode == "range":
        mv = pol.get("min_version")
        if mv and version and cmp_versions(version, mv) is not None and cmp_versions(version, mv) < 0:
            findings.append(finding("violation", "min_version", "%s 低於安全下限 %s" % (version, mv)))
        if c is not None and c < 0:
            msg = "%s 落後清冊 %s" % (version, tv)
            since = tgt.get("since")
            if since:
                days = _days_between(since, today)
                limit = pol.get("max_days_behind")
                msg += "（目標自 %s 起已 %d 天，上限 %s 天）" % (since, days, limit)
                if isinstance(limit, int) and days > limit:
                    findings.append(finding("violation", "max_days_behind", "落後超過 %d 天" % limit))
            findings.append(finding("behind", "range", msg))
        elif c is not None and c > 0:
            findings.append(finding("ahead", "range", "%s 比清冊 %s 新，應提 PR 把清冊目標往上調" % (version, tv)))
    # exceptions
    for ex in active_exceptions(product, leg, today, pin.get("id")):
        if ex["expired"]:
            findings.append(finding("violation", "exception_expired",
                                    "例外「%s」已於 %s 到期" % (ex.get("what"), ex.get("expires"))))
            continue
        waived = set(ex.get("waives") or [])
        for f in findings:
            if f["status"] in waived:
                f["status"] = "excepted"
                f["message"] += "（例外：%s，至 %s）" % (ex.get("what"), ex.get("expires"))
    status = worst([f["status"] for f in findings]) if findings else "ok"
    if status == "info":
        status = "ok"
    return {"status": status, "version": version, "findings": findings}


_SEVERITY = ["ok", "info", "excepted", "ahead", "behind", "unknown", "unreachable", "mismatch", "violation"]


def worst(statuses):
    return max(statuses, key=_SEVERITY.index) if statuses else "ok"


def is_hard(product: dict, f: dict) -> bool:
    return f.get("rule") in ((product.get("policy") or {}).get("hard_rules") or [])


# --------------------------------------------------------------------------
# check: offline, against a checked-out repo
# --------------------------------------------------------------------------


def repo_from_git(repo_dir: str):
    try:
        url = subprocess.run(["git", "-C", repo_dir, "remote", "get-url", "origin"], capture_output=True,
                             text=True, check=True).stdout.strip()
    except Exception:
        return None
    m = re.search(r"(github\.com|git-prod\.woowtech\.io|gitea[^/:]*)[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        return None
    host = "github" if "github.com" in m.group(1) else "gitea"
    return "%s:%s/%s" % (host, m.group(2), m.group(3))


def check_lock(product: dict, sf: dict, consumer: dict, lock_text: str):
    """Compare a consumer's lock file with the manifest's shared_files entry."""
    src = sf["source"]
    out = []
    try:
        lock = json.loads(lock_text)
    except ValueError as exc:
        return [finding("unknown", "shared_files", "lock 不是 JSON：%s" % exc)]
    ls = lock.get("source") or {}
    if ls.get("repository") and src["repo"].split(":", 1)[1] != ls.get("repository"):
        out.append(finding("mismatch", "shared_files", "lock 來源 %s ≠ 清冊 %s" % (ls.get("repository"), src["repo"])))
    if ls.get("path") and src.get("path") and ls["path"].rstrip("/") != src["path"].rstrip("/"):
        out.append(finding("mismatch", "shared_files", "lock 路徑 %s ≠ 清冊 %s" % (ls["path"], src["path"])))
    if ls.get("commit") != src["commit"]:
        out.append(finding("mismatch", "shared_files",
                           "lock commit %s ≠ 清冊 %s" % (short(ls.get("commit"), 10), short(src["commit"], 10))))
    if src.get("require_on_main") and ls.get("merged_to_main") is not True:
        out.append(finding("mismatch", "shared_files", "lock 未標記 merged_to_main"))
    want = set(src.get("files") or [])
    have = set((lock.get("files") or {}).keys())
    if want != have:
        out.append(finding("mismatch", "shared_files", "檔案清單不同：清冊 %s／lock %s" % (sorted(want), sorted(have))))
    if not out:
        out.append(finding("ok", "shared_files", "lock commit %s、%d 個檔案與清冊一致" % (short(src["commit"], 10), len(want))))
    return out


def cmd_check(args) -> int:
    m = load_manifest(args.manifest)
    errors, _ = validate_manifest(m)
    if errors:
        for e in errors:
            print("ERROR manifest: %s" % e)
        return 2
    repo = args.repo or repo_from_git(args.repo_dir)
    today = _today(args)
    results = []
    for pname, p in m["products"].items():
        if args.product and pname != args.product:
            continue
        for pin in iter_pins(p, args.leg):
            if repo and pin["repo"] != repo:
                continue
            path = os.path.join(args.repo_dir, pin["file"])
            if not os.path.exists(path):
                if repo:
                    results.append((pname, p, pin, {"status": "unknown", "version": None,
                                                     "findings": [finding("unknown", "extract", "找不到 %s" % pin["file"])]}))
                continue
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            try:
                obs = observe(pin, run_extractor(pin, text))
            except Exception as exc:  # parse error in the leg file
                results.append((pname, p, pin, {"status": "unknown", "version": None,
                                                 "findings": [finding("unknown", "extract", "解析失敗：%s" % exc)]}))
                continue
            results.append((pname, p, pin, evaluate(p, args.leg, pin, obs, today)))
        for sf in p.get("shared_files") or []:
            for c in sf.get("consumers") or []:
                if c["leg"] != args.leg or (repo and c["repo"] != repo):
                    continue
                path = os.path.join(args.repo_dir, c["lock"])
                if not os.path.exists(path):
                    continue
                with open(path, encoding="utf-8") as fh:
                    fs = check_lock(p, sf, c, fh.read())
                results.append((pname, p, {"id": "shared_files", "file": c["lock"]},
                                {"status": worst([f["status"] for f in fs]), "version": None, "findings": fs}))
    if not results:
        print("wvt check: %s 在清冊的 %s 邊沒有任何釘點（略過）" % (repo or args.repo_dir, args.leg))
        return 0
    gha = bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("GITEA_ACTIONS"))
    fail = False
    for pname, p, pin, res in results:
        label = STATUS_LABELS[res["status"]]
        print("%-8s %-5s %-14s %-40s %s  %s" % (pname, args.leg, pin["id"], pin["file"], res.get("version") or "—", label))
        for f in res["findings"]:
            hard = is_hard(p, f)
            lvl = "error" if hard and f["status"] == "violation" else "warning"
            if f["status"] in ("ok", "info"):
                lvl = None
            line = "  - [%s%s] %s" % (f["rule"], " · 硬規則(第1期起)" if hard and lvl else "", f["message"])
            print(line)
            if gha and lvl:
                print("::%s file=%s::wvt %s/%s %s" % (lvl, pin["file"], pname, args.leg, f["message"]))
            if args.fail_on == "drift" and f["status"] in DRIFT_STATUSES:
                fail = True
            if args.fail_on == "violation" and f["status"] in ("violation", "mismatch") and hard:
                fail = True
    phase = m.get("phase", 0)
    if args.fail_on == "none":
        print("（第 %s 期：只報告，不擋流程）" % phase)
    return 1 if fail else 0


# --------------------------------------------------------------------------
# drift: online, fetch real sources
# --------------------------------------------------------------------------


class Fetcher:
    """HTTP access to GitHub (public raw/API) and Gitea (token). Records reachability."""

    def __init__(self, gitea_api=None, gitea_token=None, github_token=None, timeout=20):
        self.gitea_api = (gitea_api or "https://git-prod.woowtech.io/api/v1").rstrip("/")
        self.gitea_token = gitea_token
        self.github_token = github_token
        self.timeout = timeout
        self.probes = {}  # endpoint -> {"ok": n, "errors": [...]}
        self.cache = {}

    def _probe(self, ep, err=None):
        pr = self.probes.setdefault(ep, {"ok": 0, "errors": []})
        if err is None:
            pr["ok"] += 1
        elif len(pr["errors"]) < 3:
            pr["errors"].append(err)

    def _get(self, url, headers=None, endpoint=None):
        if url in self.cache:
            return self.cache[url]
        req = urllib.request.Request(url, headers=dict(headers or {}, **{"User-Agent": "woow-release-train-wvt"}))
        ep = endpoint or urllib.parse.urlsplit(url).netloc
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                self._probe(ep)  # reachable; the file is just not there
                self.cache[url] = None
                return None
            self._probe(ep, "HTTP %d" % exc.code)
            raise WvtError("%s: HTTP %d" % (ep, exc.code))
        except Exception as exc:  # DNS, timeout, TLS
            self._probe(ep, "%s: %s" % (type(exc).__name__, exc))
            raise WvtError("%s unreachable: %s" % (ep, exc))
        self._probe(ep)
        self.cache[url] = body
        return body

    def read(self, repo: str, path: str, ref: str):
        host, full = repo.split(":", 1)
        if host == "github":
            url = "https://raw.githubusercontent.com/%s/%s/%s" % (full, ref, urllib.parse.quote(path))
            try:
                body = self._get(url, endpoint="raw.githubusercontent.com")
                if body is not None:
                    return body
            except WvtError:
                pass
            api = "https://api.github.com/repos/%s/contents/%s?ref=%s" % (full, urllib.parse.quote(path), urllib.parse.quote(ref))
            hdr = {"Accept": "application/vnd.github.raw+json"}
            if self.github_token:
                hdr["Authorization"] = "Bearer " + self.github_token
            return self._get(api, hdr, endpoint="api.github.com")
        url = "%s/repos/%s/raw/%s?ref=%s" % (self.gitea_api, full, urllib.parse.quote(path), urllib.parse.quote(ref))
        return self._get(url, self._gitea_hdr(), endpoint="gitea")

    def _gitea_hdr(self):
        return {"Authorization": "token " + self.gitea_token} if self.gitea_token else {}

    def gitea_json(self, path: str):
        body = self._get(self.gitea_api + path, self._gitea_hdr(), endpoint="gitea")
        return json.loads(body) if body is not None else None

    def blob_shas(self, repo: str, path: str, ref: str):
        host, full = repo.split(":", 1)
        if host != "gitea":
            raise WvtError("blob_shas only implemented for gitea")
        data = self.gitea_json("/repos/%s/contents/%s?ref=%s" % (full, urllib.parse.quote(path.rstrip("/")), urllib.parse.quote(ref)))
        if not isinstance(data, list):
            return None
        return {x["name"]: x["sha"] for x in data if x.get("type") == "file"}

    def commits_between(self, repo: str, base: str, head: str):
        host, full = repo.split(":", 1)
        data = self.gitea_json("/repos/%s/compare/%s...%s" % (full, base, head))
        return None if data is None else data.get("total_commits")


def extract_addon_ref(values_text: str):
    """Production platform addon tag from paas-app-bundle-cd values-prod.yaml.

    The pinned image is structured (image.tag/digest) but the platform release it
    was built from is only recorded in the comment above it ("Built from addon_ref
    v18.0.1.0.242"). Take the first such mention in the '── image ──' block.
    """
    lines = values_text.splitlines()
    start = next((i for i, ln in enumerate(lines) if "── image ──" in ln), 0)
    end = next((i for i, ln in enumerate(lines) if i > start and re.match(r"^image:", ln)), len(lines))
    block = "\n".join(lines[start:end])
    m = re.search(r"addon_ref[ =:]+(v\d+(?:\.\d+)+)", block) or re.search(r"\b(v18\.0\.\d+\.\d+\.\d+)\b", block)
    tag = m.group(1) if m else None
    img = {}
    for ln in lines[end + 1: end + 12]:
        if ln and not ln.startswith(" "):
            break
        mm = re.match(r'^\s+(tag|digest):\s*["\']?([^"\'\s]+)', ln)
        if mm:
            img.setdefault(mm.group(1), mm.group(2))
    return {"tag": tag, "image_tag": img.get("tag"), "image_digest": img.get("digest")}


def resolve_ref(ref: str, ctx: dict):
    if isinstance(ref, str) and ref.startswith("$"):
        return ctx.get(ref[1:])
    return ref


def collect_drift(m: dict, f: Fetcher, today: _dt.date | None = None) -> dict:
    today = today or _dt.datetime.now(TAIPEI).date()
    ctx, notes = {}, []
    for name, src in (m.get("sources") or {}).items():
        try:
            text = f.read(src["repo"], src["file"], src.get("ref", "main"))
            if text is None:
                raise WvtError("file not found")
            info = extract_addon_ref(text)
            ctx[name] = info["tag"]
            ctx[name + "_info"] = info
            if not info["tag"]:
                notes.append("無法從 %s 判讀正式站平台版" % src["file"])
        except WvtError as exc:
            notes.append("讀不到 %s:%s（%s）" % (src["repo"], src["file"], exc))
            ctx[name] = None
    catalogs = {}
    rows = []
    for pname, p in m["products"].items():
        for leg in LEGS:
            legd = (p.get("legs") or {}).get(leg) or {}
            for pin in iter_pins(p, leg):
                refs = [pin["ref"]] + list(pin.get("also_refs") or [])
                for ref in refs:
                    rref = resolve_ref(ref, ctx)
                    point = _point_label(pin, ref, rref)
                    row = {"product": pname, "leg": leg, "pin": pin["id"], "file": pin["file"], "repo": pin["repo"],
                           "ref": rref, "point": point, "compare": pin.get("compare", "version")}
                    if rref is None:
                        row.update(status="unreachable", version=None, observed=None,
                                   findings=[finding("unreachable", "fetch", "無法解析 %s" % ref)])
                        rows.append(row)
                        continue
                    rows.append(_observe_remote(m, p, leg, pin, rref, f, today, row))
            pub = legd.get("published")
            if pub:
                rows.extend(_published_rows(p, pname, leg, legd, pub, f, today, catalogs))
            pkgs = legd.get("package_version") or []
            for pkg in ([pkgs] if isinstance(pkgs, dict) else pkgs):
                for ref in [pkg.get("ref", legd.get("ref", "main"))] + list(pkg.get("also_refs") or []):
                    rref = resolve_ref(ref, ctx)
                    rows.append(_package_row(pname, leg, legd, pkg, rref, ref, f))
        for sf in p.get("shared_files") or []:
            rows.extend(_shared_rows(p, pname, sf, f))
        for n in p.get("notes") or []:
            notes.append("**%s**：%s" % (pname, n))
    exceptions = []
    for pname, p in m["products"].items():
        for ex in p.get("exceptions") or []:
            left = -_days_between(ex["expires"], today)
            exceptions.append(dict(ex, product=pname, days_left=left))
    drift = any(r["status"] in DRIFT_STATUSES for r in rows) or any(e["days_left"] < 0 for e in exceptions)
    now = _dt.datetime.now(_dt.timezone.utc)
    return {"generated_at": "%s（台北 %s）" % (now.strftime("%Y-%m-%d %H:%M UTC"), now.astimezone(TAIPEI).strftime("%m-%d %H:%M")),
            "today": today.isoformat(), "phase": m.get("phase"), "context": ctx, "rows": rows,
            "exceptions": exceptions, "observations": m.get("observations") or [], "notes": notes,
            "probes": f.probes, "drift": drift}


def _point_label(pin, ref, rref):
    base = pin.get("label") or pin["id"]
    if isinstance(ref, str) and ref.startswith("$"):
        return "%s（正式站 %s）" % (base, rref or "?")
    return "%s（%s）" % (base, rref)


def _observe_remote(m, p, leg, pin, ref, f, today, row):
    try:
        text = f.read(pin["repo"], pin["file"], ref)
    except WvtError as exc:
        row.update(status="unreachable", version=None, observed=None,
                   findings=[finding("unreachable", "fetch", str(exc))])
        return row
    if text is None:
        row.update(status="unknown", version=None, observed=None,
                   findings=[finding("unknown", "fetch", "%s@%s 不存在" % (pin["file"], ref))])
        return row
    try:
        obs = observe(pin, run_extractor(pin, text))
    except Exception as exc:
        row.update(status="unknown", version=None, observed=None,
                   findings=[finding("unknown", "extract", "解析失敗：%s" % exc)])
        return row
    res = evaluate(p, leg, pin, obs, today)
    row.update(status=res["status"], version=res["version"], observed=obs, findings=res["findings"])
    return row


def _published_rows(p, pname, leg, legd, pub, f, today, catalogs):
    """HA published state: Store catalog version + upstream pin at the catalog's source_sha."""
    rows = []
    key = (pub["repo"], pub["file"])
    try:
        if key not in catalogs:
            catalogs[key] = f.read(pub["repo"], pub["file"], pub.get("ref", "main"))
        comp = extract_catalog(catalogs[key], pub["id"]) if catalogs[key] else None
    except (WvtError, ValueError) as exc:
        return [{"product": pname, "leg": leg, "pin": "published", "point": "Store 已發佈", "file": pub["file"],
                 "repo": pub["repo"], "ref": "main", "status": "unreachable", "version": None, "observed": None,
                 "compare": "version", "findings": [finding("unreachable", "fetch", str(exc))]}]
    if not comp:
        return [{"product": pname, "leg": leg, "pin": "published", "point": "Store 已發佈", "file": pub["file"],
                 "repo": pub["repo"], "ref": "main", "status": "unknown", "version": None, "observed": None,
                 "compare": "version", "findings": [finding("unknown", "catalog", "catalog 沒有 %s" % pub["id"])]}]
    sha = comp["source_sha"]
    for pin in iter_pins(p, leg):
        if pin.get("compare", "version") == "info":
            continue
        row = {"product": pname, "leg": leg, "pin": pin["id"], "file": pin["file"], "repo": pin["repo"],
               "ref": sha, "point": "%s（Store 已發佈 %s @%s）" % (pin.get("label") or pin["id"], comp["version"], sha[:7]),
               "compare": pin.get("compare", "version")}
        rows.append(_observe_remote(None, p, leg, pin, sha, f, today, row))
    return rows


def _package_row(pname, leg, legd, pkg, rref, ref, f):
    repo = pkg.get("repo") or legd.get("repo")
    row = {"product": pname, "leg": leg, "pin": "package_version", "file": pkg["file"], "repo": repo, "ref": rref,
           "point": "%s（%s）" % (pkg.get("label", "包裝版號"), rref if not str(ref).startswith("$") else "正式站 %s" % rref),
           "compare": "info"}
    try:
        text = f.read(repo, pkg["file"], rref) if rref else None
        val = run_extractor(pkg, text) if text else None
    except (WvtError, ValueError) as exc:
        row.update(status="unreachable", version=None, observed=None, findings=[finding("unreachable", "fetch", str(exc))])
        return row
    row.update(status="info", version=str(val) if val is not None else None, observed={"raw": val},
               findings=[finding("info", "package_version", pkg.get("note", "包裝版號，不跨邊比較"))])
    return row


def _shared_rows(p, pname, sf, f):
    rows = []
    src = sf["source"]
    try:
        main_blobs = f.blob_shas(src["repo"], src["path"], src.get("branch", "main"))
    except WvtError as exc:
        main_blobs = None
        err = str(exc)
    else:
        err = None
    behind = None
    try:
        behind = f.commits_between(src["repo"], src["commit"], src.get("branch", "main"))
    except WvtError:
        pass
    for c in sf.get("consumers") or []:
        row = {"product": pname, "leg": c["leg"], "pin": "shared_files", "file": c["lock"], "repo": c["repo"],
               "ref": c.get("ref", "main"), "point": "共用檔案 lock", "compare": "shared_files"}
        try:
            text = f.read(c["repo"], c["lock"], c.get("ref", "main"))
        except WvtError as exc:
            row.update(status="unreachable", version=None, observed=None, findings=[finding("unreachable", "fetch", str(exc))])
            rows.append(row)
            continue
        if text is None:
            row.update(status="unknown", version=None, observed=None, findings=[finding("unknown", "fetch", "lock 不存在")])
            rows.append(row)
            continue
        fs = check_lock(p, sf, c, text)
        lock = json.loads(text)
        if main_blobs is None:
            fs.append(finding("unreachable", "shared_files", "讀不到 %s main 的檔案清單（%s）" % (src["repo"], err)))
        else:
            for name in src.get("files") or []:
                lb = ((lock.get("files") or {}).get(name) or {}).get("git_blob")
                mb = main_blobs.get(name)
                if lb != mb:
                    fs.append(finding("mismatch", "shared_files",
                                      "%s：lock blob %s ≠ %s main %s" % (name, short(lb, 10), src["repo"], short(mb, 10))))
            extra = sorted(set(main_blobs) - set(src.get("files") or []))
            if extra:
                fs.append(finding("info", "shared_files", "%s main 另有 PaaS 專用檔（不共用）：%s" % (src["path"], ", ".join(extra))))
        if behind:
            fs.append(finding("info", "shared_files", "清冊 commit 之後 main 又有 %s 個 commit（共用檔內容見上）" % behind))
        row.update(status=worst([x["status"] for x in fs]), version=short(src["commit"], 10),
                   observed={"raw": (lock.get("source") or {}).get("commit")}, findings=fs)
        if row["status"] == "info":
            row["status"] = "ok"
        rows.append(row)
    return rows


LEG_NAMES = {"ha": "HA", "paas": "PaaS"}


def _md(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def render_markdown(m: dict, rep: dict) -> str:
    out = []
    A = out.append
    A(TRACKING_MARKER)
    A("# 版本漂移報告（woow release train）")
    A("")
    A("- 產生時間：%s；第 %s 期：**只報告，不擋任何流程**" % (rep["generated_at"], rep["phase"]))
    prod = rep["context"].get("prod_platform_info") or {}
    if rep["context"].get("prod_platform"):
        A("- 正式站平台版：`%s`（`paas-app-bundle-cd:paas-odoo-platform/values-prod.yaml`，image `%s@%s`）"
          % (rep["context"]["prod_platform"], prod.get("image_tag"), short(prod.get("image_digest"))))
    A("- 只比上游身分（版本＋digest）；add-on 版號、chart 版號、JCR 時間戳 tag 只列參考，不跨邊比較。")
    A("")
    A("## 總覽")
    A("")
    A("| 產品 | 政策 | 清冊目標 | HA | PaaS | 結論 |")
    A("|---|---|---|---|---|---|")
    for pname, p in m["products"].items():
        pol = p.get("policy") or {}
        tgt = p.get("target") or {}
        mode = pol.get("mode")
        extra = []
        if pol.get("license_ceiling"):
            extra.append("授權上限 %s" % pol["license_ceiling"])
        if mode == "range":
            extra.append("下限 %s、最多落後 %s 天" % (pol.get("min_version"), pol.get("max_days_behind")))
        cells = []
        for leg in LEGS:
            rs = [r for r in rep["rows"] if r["product"] == pname and r["leg"] == leg and r["compare"] != "info"]
            st = worst([r["status"] for r in rs]) if rs else "unknown"
            vers = sorted({r["version"] for r in rs if r.get("version") and r["pin"] != "shared_files"},
                          key=lambda v: parse_version(v) or (0,))
            cells.append("%s %s" % (STATUS_LABELS[st], " / ".join(vers) or ""))
        allst = worst([r["status"] for r in rep["rows"] if r["product"] == pname and r["compare"] != "info"] or ["unknown"])
        exc = [e for e in rep["exceptions"] if e["product"] == pname]
        concl = STATUS_LABELS[allst] + ("（有例外 %d）" % len(exc) if exc else "")
        A("| %s | %s%s | %s | %s | %s | %s |" % (
            pname, mode, "（%s）" % "；".join(extra) if extra else "", tgt.get("version"), _md(cells[0]), _md(cells[1]), concl))
    A("")
    A("## 明細")
    A("")
    A("| 產品 | 邊 | 觀測點 | 檔案 | 觀測值 | 比對 | 狀態 | 說明 |")
    A("|---|---|---|---|---|---|---|---|")
    for r in rep["rows"]:
        obs = r.get("observed") or {}
        val = r.get("version") or "—"
        if obs.get("digest"):
            val = "%s `%s`" % (val, short(obs["digest"]))
        if obs.get("tag") and r["compare"] != "version":
            val += "（tag %s）" % obs["tag"]
        msgs = "；".join(x["message"] for x in r["findings"] if x["status"] != "ok" or r["compare"] == "shared_files") or ""
        A("| %s | %s | %s | `%s` | %s | %s | %s | %s |" % (
            r["product"], LEG_NAMES[r["leg"]], _md(r["point"]), _md("%s:%s" % (r["repo"].split(":", 1)[1].split("/")[-1], r["file"])),
            _md(val), r["compare"], STATUS_LABELS[r["status"]], _md(msgs)))
    A("")
    if rep["exceptions"]:
        A("## 例外（必須有理由與到期日，到期即轉為失敗）")
        A("")
        A("| 產品 | 邊 | 內容 | 理由 | 核准 | 到期 | 剩餘天數 |")
        A("|---|---|---|---|---|---|---|")
        for e in rep["exceptions"]:
            A("| %s | %s | %s | %s | %s | %s | %s |" % (e["product"], LEG_NAMES.get(e["leg"], e["leg"]), _md(e["what"]),
                                                     _md(e["reason"]), _md(e.get("approved_by", "")), e["expires"],
                                                     e["days_left"] if e["days_left"] >= 0 else "**已過期**"))
        A("")
    if rep["observations"]:
        A("## 已部署（手動快照，非即時）")
        A("")
        A("範本對齊不等於線上服務對齊：租戶要 Redeploy 才換版。第 1 期起改由 operator 回報。")
        A("")
        A("| 產品 | 快照日 | 來源 | 內容 |")
        A("|---|---|---|---|")
        for o in rep["observations"]:
            A("| %s | %s | %s | %s |" % (o.get("product"), o.get("as_of"), _md(o.get("source", "")), _md(o.get("summary", ""))))
        A("")
    if rep["notes"]:
        A("## 備註")
        A("")
        for n in rep["notes"]:
            A("- %s" % n)
        A("")
    A("## 來源連線")
    A("")
    A("| 端點 | 結果 |")
    A("|---|---|")
    for ep, res in sorted(rep["probes"].items()):
        txt = "成功 %d 次" % res["ok"]
        if res["errors"]:
            txt += "；失敗：%s" % "；".join(res["errors"])
        A("| %s | %s |" % (ep, _md(txt)))
    A("")
    A("狀態圖例：" + "、".join("%s" % v for v in STATUS_LABELS.values()))
    A("")
    return "\n".join(out)


def cmd_drift(args) -> int:
    m = load_manifest(args.manifest)
    errors, warnings = validate_manifest(m, _today(args))
    for w in warnings:
        print("WARN manifest: %s" % w, file=sys.stderr)
    if errors:
        for e in errors:
            print("ERROR manifest: %s" % e, file=sys.stderr)
        return 2
    token = os.environ.get("GITEA_READ_TOKEN") or os.environ.get("GITEA_TOKEN")
    f = Fetcher(os.environ.get("GITEA_API_URL"), token, os.environ.get("GITHUB_TOKEN"))
    rep = collect_drift(m, f, _today(args))
    md = render_markdown(m, rep)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(md)
    else:
        print(md)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=1, default=str)
    print("drift=%s rows=%d probes=%s" % (str(rep["drift"]).lower(), len(rep["rows"]), json.dumps(rep["probes"], ensure_ascii=False)),
          file=sys.stderr)
    if args.fail_on == "drift" and rep["drift"]:
        return 1
    return 0


# --------------------------------------------------------------------------
# issue: one tracking issue on Gitea
# --------------------------------------------------------------------------


def gitea_call(api, token, method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(api.rstrip("/") + path, data=data, method=method,
                                 headers={"Authorization": "token " + token, "Content-Type": "application/json",
                                          "User-Agent": "woow-release-train-wvt"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else None


def sync_issue(call, repo: str, label: str, title: str, body: str, drift: bool, run_url: str = ""):
    """call(method, path, body) -> json. Returns (action, issue_number)."""
    labels = call("GET", "/repos/%s/labels?limit=50" % repo) or []
    lab = next((x for x in labels if x["name"] == label), None)
    if lab is None:
        lab = call("POST", "/repos/%s/labels" % repo,
                   {"name": label, "color": "#e99695", "description": "woow release train：版本漂移追蹤（自動）"})
    issues = call("GET", "/repos/%s/issues?state=open&type=issues&labels=%s&limit=50" % (repo, urllib.parse.quote(label))) or []
    mine = [i for i in issues if TRACKING_MARKER in (i.get("body") or "")] or issues
    current = mine[0] if mine else None
    if drift:
        if current:
            call("PATCH", "/repos/%s/issues/%d" % (repo, current["number"]), {"title": title, "body": body})
            return "updated", current["number"]
        new = call("POST", "/repos/%s/issues" % repo, {"title": title, "body": body, "labels": [lab["id"]]})
        return "created", new["number"]
    if current:
        msg = "最新一次 drift 已無落後項目，自動關閉。" + (" 執行紀錄：%s" % run_url if run_url else "")
        call("PATCH", "/repos/%s/issues/%d" % (repo, current["number"]), {"body": body})
        call("POST", "/repos/%s/issues/%d/comments" % (repo, current["number"]), {"body": msg})
        call("PATCH", "/repos/%s/issues/%d" % (repo, current["number"]), {"state": "closed"})
        return "closed", current["number"]
    return "noop", None


def cmd_issue(args) -> int:
    token = os.environ.get("GITEA_TOKEN")
    api = os.environ.get("GITEA_API_URL", "https://git-prod.woowtech.io/api/v1")
    if not token:
        print("GITEA_TOKEN is not set", file=sys.stderr)
        return 2
    with open(args.report, encoding="utf-8") as fh:
        body = fh.read()
    with open(args.json, encoding="utf-8") as fh:
        rep = json.load(fh)
    if args.run_url:
        body += "\n---\n本 issue 由 `drift.yml` 自動維護（只報告）；最近一次執行：%s\n" % args.run_url
    action, num = sync_issue(lambda mth, p, b=None: gitea_call(api, token, mth, p, b), args.repo, args.label,
                             args.title, body, bool(rep.get("drift")), args.run_url)
    print("tracking issue: %s #%s" % (action, num))
    return 0


# --------------------------------------------------------------------------


def cmd_validate(args) -> int:
    m = load_manifest(args.manifest)
    errors, warnings = validate_manifest(m, _today(args))
    for w in warnings:
        print("WARN  %s" % w)
    for e in errors:
        print("ERROR %s" % e)
    if not errors:
        n = sum(len(list(iter_pins(p, leg))) for p in m["products"].values() for leg in LEGS)
        print("OK    schema %s, phase %s, %d products, %d pins" % (m["schema"], m["phase"], len(m["products"]), n))
    return 1 if errors else 0


def _today(args):
    """Day boundaries follow Asia/Taipei (runners are UTC)."""
    t = getattr(args, "today", None)
    return _dt.date.fromisoformat(t) if t else _dt.datetime.now(TAIPEI).date()


def main(argv=None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    default_manifest = os.path.join(os.path.dirname(here), "woow-versions.yaml")
    ap = argparse.ArgumentParser(prog="wvt", description=__doc__.split("\n")[0])
    ap.add_argument("--manifest", default=os.environ.get("WVT_MANIFEST", default_manifest))
    ap.add_argument("--today", help="override today's date (YYYY-MM-DD) for tests")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate", help="schema + consistency + exception expiry")
    c = sub.add_parser("check", help="compare pins in a checked-out repo with the manifest")
    c.add_argument("--leg", choices=LEGS, required=True)
    c.add_argument("--repo-dir", default=".")
    c.add_argument("--repo", help="github:OWNER/REPO or gitea:OWNER/REPO (default: from git remote)")
    c.add_argument("--product")
    c.add_argument("--fail-on", choices=("none", "violation", "drift"), default="none",
                   help="none = phase 0 report-only; violation = hard rules (phase 1); drift = everything")
    c.add_argument("--strict", action="store_const", const="drift", dest="fail_on", help="same as --fail-on drift")
    d = sub.add_parser("drift", help="fetch real sources and render the drift table")
    d.add_argument("--out")
    d.add_argument("--json")
    d.add_argument("--fail-on", choices=("none", "drift"), default="none")
    i = sub.add_parser("issue", help="create/update/close the tracking issue")
    i.add_argument("--report", required=True)
    i.add_argument("--json", required=True)
    i.add_argument("--repo", default="woow-paas/woow-release-train")
    i.add_argument("--label", default="version-drift")
    i.add_argument("--title", default="版本漂移追蹤：HA ↔ PaaS（woow release train）")
    i.add_argument("--run-url", default="")
    args = ap.parse_args(argv)
    try:
        return {"validate": cmd_validate, "check": cmd_check, "drift": cmd_drift, "issue": cmd_issue}[args.cmd](args)
    except WvtError as exc:
        print("wvt: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
