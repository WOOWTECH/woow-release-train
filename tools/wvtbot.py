#!/usr/bin/env python3
"""wvtbot — woow release train bots, phase 2 (stdlib only).

  propose   a leg's default branch is ahead of the manifest, or the source of a shared
            file changed first → look the new version up in its registry → a manifest PR
            on Gitea (the version owner merges it)
  fanout    the manifest is ahead of a leg → a PR in each of that leg's repos: HA on
            GitHub through the GitHub App, PaaS on Gitea once the upstream digest is
            mirrored into JCR (mirror-image, a human comments /approve)

Humans merge every PR; the bots never merge and never comment /approve. What each bot may
do is read from the manifest (`bots:`), so switching one on is itself a reviewed PR:
  off      nothing
  dry-run  report the plan (stdout, $GITHUB_STEP_SUMMARY, --plan), touch nothing
  open-pr  push the bot branch, open / update its PR, close the PR when it is no longer needed

A bot branch is one per product and repo (wvt/propose-<product>, wvt/fanout-<product>):
a rerun updates it instead of opening another PR, and an unchanged plan pushes nothing.
"""

from __future__ import annotations

import argparse
import base64
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wvt  # noqa: E402
from wvt import WvtError  # noqa: E402

BOTS, BOT_MODES = wvt.BOTS, wvt.BOT_MODES
UA = "woow-release-train-wvt"
BOT_NAME = "woow release train bot"
BOT_EMAIL = "woow-release-train-bot@users.noreply.git-prod.woowtech.io"
MARKER_RE = re.compile(r"<!-- wvt:bot (\w+) (.*?) -->")
JCR_TAG_RE = re.compile(r"^\d{8}\.\d{4}$")
MIRROR_REPO = "woow-paas/paas-odoo-ci"
MIRROR_WORKFLOW = "mirror-image.yml"
MIRROR_TITLE = "[prod-approval:mirror-image.yml]"
GITEA_TITLE_PREFIX = "⛔ 勿代 merge｜待覆核 — "


def bot_modes(m: dict) -> dict:
    conf = m.get("bots") or {}
    return {b: conf.get(b, "off") for b in BOTS}


# --------------------------------------------------------------------------
# HTTP (no automatic redirects: a registry blob redirect must not carry the token)
# --------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def http(url, headers=None, data=None, method=None, timeout=30, _hops=0):
    """(status, {lower-case header: value}, body bytes). Never raises for an HTTP status."""
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}, **{"User-Agent": UA}))
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
        if e.code in (301, 302, 303, 307, 308) and hdrs.get("location") and _hops < 5:
            loc = urllib.parse.urljoin(url, hdrs["location"])
            safe = {k: v for k, v in (headers or {}).items() if k.lower() != "authorization"}
            return http(loc, safe, None if e.code == 303 else data, None if e.code == 303 else method, timeout, _hops + 1)
        return e.code, hdrs, e.read()
    except Exception as exc:  # DNS, TLS, timeout
        raise WvtError("%s unreachable: %s" % (urllib.parse.urlsplit(url).netloc, exc))


# --------------------------------------------------------------------------
# OCI registry: tag -> index digest + per-architecture digests (anonymous pull)
# --------------------------------------------------------------------------

REGISTRY_ACCEPT = ",".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))
REGISTRY_HOSTS = {"docker.io": "https://registry-1.docker.io"}


def split_image(image: str):
    """'docker.io/n8nio/n8n' -> ('https://registry-1.docker.io', 'n8nio/n8n')."""
    host, _, repo = image.partition("/")
    if not repo or ("." not in host and ":" not in host and host != "localhost"):
        host, repo = "docker.io", image
    if host == "docker.io" and "/" not in repo:
        repo = "library/" + repo
    return REGISTRY_HOSTS.get(host, "https://" + host), repo


class Registry:
    def __init__(self, get=None):
        self.get = get or (lambda url, headers: http(url, headers))
        self.tokens = {}

    def _fetch(self, base, repo, path, accept):
        url = "%s/v2/%s/%s" % (base, repo, path)
        hdr = {"Accept": accept}
        if (base, repo) in self.tokens:
            hdr["Authorization"] = "Bearer " + self.tokens[(base, repo)]
        st, h, body = self.get(url, hdr)
        if st == 401 and (base, repo) not in self.tokens:
            ch = dict(re.findall(r'(\w+)="([^"]*)"', h.get("www-authenticate", "")))
            if not ch.get("realm"):
                raise WvtError("registry %s: 401 without a token realm" % base)
            q = urllib.parse.urlencode({k: ch[k] for k in ("service", "scope") if k in ch})
            st2, _, tb = self.get(ch["realm"] + ("?" + q if q else ""), {})
            if st2 != 200:
                raise WvtError("registry token for %s/%s: HTTP %d" % (base, repo, st2))
            tj = json.loads(tb)
            self.tokens[(base, repo)] = tj.get("token") or tj.get("access_token")
            return self._fetch(base, repo, path, accept)
        if st != 200:
            raise WvtError("registry %s/%s %s: HTTP %d" % (base, repo, path, st))
        return h, body

    def resolve(self, image: str, tag: str) -> dict:
        """{index_digest, platforms: {amd64, arm64}} of image:tag."""
        base, repo = split_image(image)
        h, body = self._fetch(base, repo, "manifests/" + tag, REGISTRY_ACCEPT)
        digest = h.get("docker-content-digest") or "sha256:" + hashlib.sha256(body).hexdigest()
        doc = json.loads(body)
        out = {"index_digest": None, "platforms": {}}
        if doc.get("manifests"):
            out["index_digest"] = digest
            for mm in doc["manifests"]:
                p = mm.get("platform") or {}
                if p.get("os") == "linux" and p.get("architecture") in ("amd64", "arm64"):
                    out["platforms"].setdefault(p["architecture"], mm["digest"])
        else:  # a single-platform manifest: its architecture is in the config blob
            cfg = (doc.get("config") or {}).get("digest")
            arch = json.loads(self._fetch(base, repo, "blobs/" + cfg, "*/*")[1]).get("architecture") if cfg else None
            if arch:
                out["platforms"][arch] = digest
        return out


def fetch_sha256_file(url: str, get=None) -> str:
    """The sha256 published next to an upstream artifact (`<hex>  <name>` or bare hex)."""
    st, _, body = (get or (lambda u, h: http(u, h)))(url, {})
    if st != 200:
        raise WvtError("%s: HTTP %d" % (url, st))
    m = re.search(r"\b([0-9a-f]{64})\b", body.decode("utf-8", "replace"))
    if not m:
        raise WvtError("%s: no sha256 in the file" % url)
    return "sha256:" + m.group(1)


# --------------------------------------------------------------------------
# GitHub App: RS256 JWT (pure Python) -> installation token for one repository
# --------------------------------------------------------------------------


def _b64url(b: bytes) -> bytes:
    return base64.urlsafe_b64encode(b).rstrip(b"=")


def _der(buf: bytes, i: int):
    tag, ln = buf[i], buf[i + 1]
    i += 2
    if ln & 0x80:
        n = ln & 0x7F
        ln = int.from_bytes(buf[i:i + n], "big")
        i += n
    return tag, buf[i:i + ln], i + ln


def _der_items(content: bytes):
    items, i = [], 0
    while i < len(content):
        tag, val, i = _der(content, i)
        items.append((tag, val))
    return items


def rsa_private_key(pem: str):
    """(n, e, d) of a PKCS#1 ("RSA PRIVATE KEY", what GitHub hands out) or PKCS#8 key."""
    body = "".join(ln.strip() for ln in pem.strip().splitlines() if ln.strip() and not ln.startswith("-----"))
    try:
        _, seq, _ = _der(base64.b64decode(body), 0)
        items = _der_items(seq)
        if len(items) == 3 and items[1][0] == 0x30:  # PKCS#8 PrivateKeyInfo wraps the PKCS#1 key
            items = _der_items(_der(items[2][1], 0)[1])
        ints = [int.from_bytes(v, "big") for t, v in items if t == 0x02]
        _, n, e, d = ints[:4]
    except Exception:
        raise WvtError("the GitHub App private key is not an RSA PEM key")
    return n, e, d


_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def rs256(pem: str, msg: bytes) -> bytes:
    """RSASSA-PKCS1-v1_5 with SHA-256."""
    n, _, d = rsa_private_key(pem)
    k = (n.bit_length() + 7) // 8
    t = _SHA256_DIGEST_INFO + hashlib.sha256(msg).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


def app_jwt(app_id, pem: str, now=None) -> str:
    now = int(time.time() if now is None else now)
    seg = lambda o: _b64url(json.dumps(o, separators=(",", ":")).encode())  # noqa: E731
    signing = seg({"alg": "RS256", "typ": "JWT"}) + b"." + seg({"iat": now - 60, "exp": now + 540, "iss": str(app_id)})
    return (signing + b"." + _b64url(rs256(pem, signing))).decode()


# --------------------------------------------------------------------------
# Forges: the API calls the bots need on Gitea and GitHub
# --------------------------------------------------------------------------


class Forge:
    kind = ""

    def __init__(self, api: str, token: str, call=None):
        self.api = api.rstrip("/")
        self.token = token
        self._call = call

    def headers(self):
        raise NotImplementedError

    def call(self, method, path, body=None, ok=(200, 201, 204)):
        if self._call:
            return self._call(method, path, body)
        data = json.dumps(body).encode() if body is not None else None
        st, _, raw = http(self.api + path, dict(self.headers(), **{"Content-Type": "application/json"}), data, method)
        if st not in ok:
            raise WvtError("%s %s %s: HTTP %d %s" % (self.kind, method, path, st, raw[:200].decode("utf-8", "replace")))
        return json.loads(raw) if raw else None

    def find_prs(self, repo, branch, state="open"):
        raise NotImplementedError

    def create_pr(self, repo, branch, base, title, body):
        return self.call("POST", "/repos/%s/pulls" % repo, {"title": title, "head": branch, "base": base, "body": body})

    def update_pr(self, repo, number, title, body):
        return self.call("PATCH", "/repos/%s/pulls/%d" % (repo, number), {"title": title, "body": body})

    def close_pr(self, repo, number, comment):
        self.call("POST", "/repos/%s/issues/%d/comments" % (repo, number), {"body": comment})
        return self.call("PATCH", "/repos/%s/pulls/%d" % (repo, number), {"state": "closed"})


class Gitea(Forge):
    kind = "gitea"

    def headers(self):
        return {"Authorization": "token " + self.token}

    def find_prs(self, repo, branch, state="open"):
        out, page = [], 1
        while page <= 10:
            prs = self.call("GET", "/repos/%s/pulls?state=%s&limit=50&page=%d" % (repo, state, page)) or []
            out += [p for p in prs if (p.get("head") or {}).get("ref") == branch]
            if len(prs) < 50:
                break
            page += 1
        return out


class GitHub(Forge):
    kind = "github"

    def headers(self):
        return {"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def find_prs(self, repo, branch, state="open"):
        owner = repo.split("/")[0]
        return self.call("GET", "/repos/%s/pulls?state=%s&head=%s&per_page=50" % (repo, state, urllib.parse.quote(owner + ":" + branch))) or []


def github_installation_token(app_id, pem, repo, call=None) -> str:
    """A token for one repository, valid one hour (contents, pull requests, workflows: write)."""
    jwt = app_jwt(app_id, pem)
    gh = GitHub("https://api.github.com", jwt, call)
    inst = gh.call("GET", "/repos/%s/installation" % repo)
    tok = gh.call("POST", "/app/installations/%d/access_tokens" % inst["id"],
                  {"repositories": [repo.split("/")[1]],
                   "permissions": {"contents": "write", "pull_requests": "write", "workflows": "write"}})
    return tok["token"]


def git(*args, cwd=None, env=None, check=True):
    r = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise WvtError("git %s: %s" % (args[0], (r.stderr or r.stdout).strip()[-300:]))
    return r.stdout.strip()


def push_branch(url: str, auth: str, base: str, branch: str, files: dict, message: str) -> tuple:
    """Commit `files` ({path: text, or None to delete}) on top of `base` and force-push `branch`.

    Returns (status, commit): "unchanged" (files equal base), "same" (the branch already holds
    exactly this change on the current base: nothing pushed), or "pushed". The credential
    travels as an http.extraHeader of each command, never in the URL or a config file.
    """
    with tempfile.TemporaryDirectory(prefix="wvtbot-") as d:
        x = ["-c", "http.extraHeader=Authorization: " + auth]
        git("init", "-q", cwd=d)
        git(*x, "fetch", "-q", "--depth", "2", url, "refs/heads/%s:refs/remotes/o/base" % base, cwd=d)
        git("checkout", "-q", "-b", "wvt", "refs/remotes/o/base", cwd=d)
        for path, text in files.items():
            full = os.path.join(d, path)
            if text is None:
                if os.path.exists(full):
                    os.remove(full)
                continue
            os.makedirs(os.path.dirname(full) or d, exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
        git("add", "-A", cwd=d)
        if not git("status", "--porcelain", cwd=d):
            return "unchanged", None
        env = dict(os.environ, GIT_AUTHOR_NAME=BOT_NAME, GIT_AUTHOR_EMAIL=BOT_EMAIL,
                   GIT_COMMITTER_NAME=BOT_NAME, GIT_COMMITTER_EMAIL=BOT_EMAIL)
        git("commit", "-q", "-m", message, cwd=d, env=env)
        head_tree, base_sha = git("rev-parse", "HEAD^{tree}", cwd=d), git("rev-parse", "refs/remotes/o/base", cwd=d)
        if git(*x, "ls-remote", url, "refs/heads/" + branch, cwd=d):
            git(*x, "fetch", "-q", "--depth", "2", url, "refs/heads/%s:refs/remotes/o/bot" % branch, cwd=d)
            if (git("rev-parse", "refs/remotes/o/bot^{tree}", cwd=d) == head_tree
                    and git("rev-parse", "refs/remotes/o/bot^", cwd=d, check=False) == base_sha):
                return "same", git("rev-parse", "refs/remotes/o/bot", cwd=d)
        git(*x, "push", "-q", "--force", url, "HEAD:refs/heads/" + branch, cwd=d)
        return "pushed", git("rev-parse", "HEAD", cwd=d)


def basic(user: str, token: str) -> str:
    return "Basic " + base64.b64encode(("%s:%s" % (user, token)).encode()).decode()


# --------------------------------------------------------------------------
# Writers: the inverse of wvt's extractors, editing one value in place
# --------------------------------------------------------------------------


def _quoted(old: str, new: str) -> str:
    old = old.strip()
    q = old[0] if len(old) >= 2 and old[0] == old[-1] and old[0] in "'\"" else ""
    return q + new + q


def write_dockerfile_arg(text: str, name: str, value: str) -> str:
    """Rewrite the first `ARG NAME=value` (the one wvt's extractor reads)."""
    out = text.splitlines(keepends=True)
    for i, line in enumerate(out):
        body = line.rstrip("\r\n")
        m = re.match(r"^(\s*ARG\s+%s=)(\S.*?)\s*$" % re.escape(name), body)
        if m:
            out[i] = m.group(1) + _quoted(m.group(2), value) + line[len(body):]
            return "".join(out)
    raise WvtError("ARG %s=… not found" % name)


_YAML_KV = re.compile(r"""^(\s*(?:"[^"]*"|'[^']*'|[^\s#'"][^#]*?)\s*:[ \t]+)(\S.*?)\s*$""")


def write_yaml_path(text: str, path: str, value: str) -> str:
    """Rewrite the scalar at a dotted path of block mappings, keeping its quotes and comment."""
    want = path.split(".")
    stack = []
    out = text.splitlines(keepends=True)
    for i, line in enumerate(out):
        raw = line.rstrip("\r\n")
        body = wvt._strip_comment(raw)
        if not body.strip() or body.lstrip().startswith("-"):
            continue
        indent = len(body) - len(body.lstrip(" "))
        kv = wvt._split_key(body.strip())
        if not kv:
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, str(kv[0])))
        if [k for _, k in stack] != want:
            continue
        m = _YAML_KV.match(body)
        if not m or kv[1] in ("", "|", ">") or kv[1][0] in "[{":
            raise WvtError("%s is not a plain scalar" % path)
        out[i] = m.group(1) + _quoted(m.group(2), value) + raw[len(body):] + line[len(raw):]
        return "".join(out)
    raise WvtError("yaml path %s not found" % path)


def _record_span(text: str, record: str):
    m = re.search(r'<record\b[^>]*\bid="%s"[^>]*>.*?</record>' % re.escape(record), text, re.S)
    if not m:
        raise WvtError("seed record %s not found" % record)
    return m.start(), m.end()


def write_seed_image_tag(text: str, record: str, old_tag: str, new_tag: str) -> str:
    """Replace the image `tag` (tag@digest) inside one seed record's helm_default_values."""
    a, b = _record_span(text, record)
    seg = text[a:b]
    needle = '"tag": "%s"' % old_tag
    if seg.count(needle) != 1:
        needle = '"tag":"%s"' % old_tag
        if seg.count(needle) != 1:
            raise WvtError("seed %s: image tag %s not found exactly once" % (record, old_tag))
    return text[:a] + seg.replace(needle, needle.replace(old_tag, new_tag)) + text[b:]


def write_seed_field(text: str, record: str, field: str, value: str) -> str:
    a, b = _record_span(text, record)
    seg = text[a:b]
    pat = re.compile(r'(<field name="%s"[^>]*>)([^<]*)(</field>)' % re.escape(field))
    if len(pat.findall(seg)) != 1:
        raise WvtError("seed %s: field %s not found exactly once" % (record, field))
    return text[:a] + pat.sub(lambda mm: mm.group(1) + value + mm.group(3), seg) + text[b:]


def write_json_path(text: str, path: str, value: str) -> str:
    """Set one value and write the file back in its own style (indent, escaping, final newline).

    Refuses when no json.dumps style reproduces the original byte for byte: rewriting such a
    file would reformat all of it.
    """
    data = json.loads(text)
    style = None
    for indent in (2, 1, 4, None):
        for ascii_ in (True, False):
            dumped = json.dumps(data, indent=indent, ensure_ascii=ascii_)
            if dumped in (text, text.rstrip("\n")):
                style = (indent, ascii_, text[len(dumped):])
                break
        if style:
            break
    if not style:
        raise WvtError("cannot rewrite this JSON file without reformatting it")
    cur, keys = data, path.split(".")
    for k in keys[:-1]:
        cur = cur[int(k)] if isinstance(cur, list) else cur[k]
    last = keys[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    elif last in cur:
        cur[last] = value
    else:
        raise WvtError("json path %s not found" % path)
    return json.dumps(data, indent=style[0], ensure_ascii=style[1]) + style[2]


def write_dockerfile_from(text: str, stage, value: str) -> str:
    """Rewrite the image of one FROM line (same stage choice as wvt's extractor); ${ARG} lines are refused."""
    out = text.splitlines(keepends=True)
    froms = [(i, m) for i, m in ((i, re.match(r"^(\s*FROM\s+(?:--platform=\S+\s+)?)(\S+)((?:\s+AS\s+(\S+))?\s*)$", ln.rstrip("\r\n"), re.I))
                                 for i, ln in enumerate(out)) if m]
    if not froms:
        raise WvtError("no FROM line")
    if stage is None:
        i, m = froms[-1]
    elif str(stage).isdigit():
        i, m = froms[int(stage)]
    else:
        i, m = next((x for x in froms if x[1].group(4) == stage), (None, None))
        if m is None:
            raise WvtError("FROM … AS %s not found" % stage)
    if "$" in m.group(2):
        raise WvtError("FROM %s comes from an ARG: pin the ARG instead" % m.group(2))
    line = out[i]
    body = line.rstrip("\r\n")
    out[i] = m.group(1) + value + m.group(3) + line[len(body):]
    return "".join(out)


def write_regex(text: str, pattern: str, value: str, group=1) -> str:
    """Replace the capture group at every match (wvt's extractor requires them to agree)."""
    rx = re.compile(pattern, re.M)
    if not rx.search(text):
        raise WvtError("regex %s does not match" % pattern)
    return rx.sub(lambda mm: mm.group(0)[:mm.start(int(group)) - mm.start(0)] + value + mm.group(0)[mm.end(int(group)) - mm.start(0):], text)


def write_pin(text: str, pin: dict, value: str) -> str:
    kind = pin["kind"]
    if kind == "dockerfile_from":
        return write_dockerfile_from(text, pin.get("stage"), value)
    if kind == "regex":
        return write_regex(text, pin["pattern"], value, pin.get("group", 1))
    if kind == "dockerfile_arg":
        return write_dockerfile_arg(text, pin["name"], value)
    if kind == "yaml":
        return write_yaml_path(text, pin["path"], value)
    if kind == "json":
        return write_json_path(text, pin["path"], value)
    if kind == "seed_field":
        return write_seed_field(text, pin["record"], pin["field"], value)
    raise WvtError("no writer for kind %s" % kind)


def bump_patch(v: str) -> str:
    m = re.match(r"^(v?)(\d+)\.(\d+)\.(\d+)(.*)$", str(v))
    if not m:
        raise WvtError("cannot bump %r" % v)
    return "%s%s.%s.%d" % (m.group(1), m.group(2), m.group(3), int(m.group(4)) + 1)


def _in_jcr(repository) -> bool:
    return str(repository or "").startswith("jcr-prod.woowtech.io/")


def new_image_ref(old: str, version: str, digest: str | None, jcr_tag: str | None = None) -> str:
    """Same repository; a version tag follows the manifest (v-style kept), a JCR timestamp tag
    becomes the new mirror tag, and the digest is the manifest's."""
    ref = wvt.parse_image_ref(old)
    tag = ref["tag"]
    if tag and JCR_TAG_RE.match(tag) and _in_jcr(ref["repository"]):
        if not jcr_tag:
            raise WvtError("%s: needs the JCR mirror tag" % old)
        tag = jcr_tag
    elif tag and wvt.parse_version(tag) is not None:  # incl. a build id such as 20261015.0617
        tag = ("v" if tag[:1] in "vV" else "") + version
    out = ref["repository"] or ""
    if tag:
        out += (":" if out else "") + tag
    if digest:
        out += "@" + digest
    return out


def changelog_with_entry(text: str, heading: str, entry: str) -> str:
    """Prepend `## <heading>` + entry under the title; follow the file's `## v1.2.3` style."""
    first = re.search(r"^## (v?)\S", text, re.M)
    if first and first.group(1) and not heading.startswith("v"):
        heading = "v" + heading
    block = "## %s\n\n%s\n\n" % (heading, entry)
    if re.search(r"^## %s\s*$" % re.escape(heading), text, re.M):
        return text  # already has this version
    if first:
        return text[:first.start()] + block + text[first.start():]
    return text.rstrip("\n") + "\n\n" + block


# --------------------------------------------------------------------------
# Manifest editor: change target / shared files in woow-versions.yaml, keep the rest byte for byte
# --------------------------------------------------------------------------


def _q(v) -> str:
    return '"%s"' % str(v).replace("\\", "\\\\").replace('"', '\\"')


def _block_end(lines, i, indent):
    """First line after i whose indent is <= indent (blank lines belong to the block)."""
    j = i + 1
    while j < len(lines):
        s = lines[j]
        if s.strip() and (len(s) - len(s.lstrip(" "))) <= indent:
            break
        j += 1
    while j > i + 1 and not lines[j - 1].strip():
        j -= 1
    return j


def _find_key(lines, a, b, indent, key):
    pat = re.compile(r"^%s%s:(\s|$)" % (" " * indent, re.escape(key)))
    return next((i for i in range(a, b) if pat.match(lines[i])), None)


def _product_span(lines, product):
    start = next((i for i, ln in enumerate(lines) if re.match(r"^  %s:\s*(#.*)?$" % re.escape(product), ln)), None)
    if start is None:
        raise WvtError("product %s not found in the manifest" % product)
    return start, _block_end(lines, start, 2)


def render_target(t: dict, indent: int = 6) -> list:
    pad = " " * indent
    out = ["%sversion: %s" % (pad, _q(t["version"]))]
    if t.get("since"):
        out.append("%ssince: %s" % (pad, _q(t["since"])))
    if t.get("index_digest"):
        out.append("%sindex_digest: %s" % (pad, _q(t["index_digest"])))
    for grp in ("platforms", "artifacts", "identity"):
        if t.get(grp):
            out.append("%s%s:" % (pad, grp))
            out += ["%s  %s: %s" % (pad, k, _q(v)) for k, v in t[grp].items()]
    if t.get("verified"):
        out.append("%sverified: %s" % (pad, _q(t["verified"])))
    return out


def set_target(text: str, product: str, new: dict) -> str:
    """New `target`; the old one (its comments too) becomes the first known_versions entry."""
    lines = text.split("\n")
    a, b = _product_span(lines, product)
    t = _find_key(lines, a, b, 4, "target")
    if t is None:
        raise WvtError("%s has no target" % product)
    te = _block_end(lines, t, 4)
    old = [ln for ln in lines[t + 1:te] if not re.match(r"^\s{6}since:", ln)]
    first = next(i for i, ln in enumerate(old) if ln.strip() and not ln.lstrip().startswith("#"))
    item = ["  " + ln if ln.strip() else ln for ln in old]
    item[first] = "      - " + old[first].strip()
    new_lines = lines[:t + 1] + render_target(new) + lines[te:]
    a, b = _product_span(new_lines, product)
    k = _find_key(new_lines, a, b, 4, "known_versions")
    if k is not None and new_lines[k].split(":", 1)[1].split("#")[0].strip() == "[]":
        new_lines[k] = "    known_versions:"
        new_lines[k + 1:k + 1] = item
    elif k is not None:
        ke = _block_end(new_lines, k, 4)
        at = next((i for i in range(k + 1, ke) if new_lines[i].startswith("      - ")), ke)
        new_lines[at:at] = item
    else:
        te2 = _block_end(new_lines, _find_key(new_lines, a, b, 4, "target"), 4)
        new_lines[te2:te2] = ["    known_versions:"] + item
    # the new target version must not also stay a known version
    m = wvt.load_yaml("\n".join(new_lines))
    dup = [kv for kv in m["products"][product].get("known_versions") or [] if kv.get("version") == new["version"]]
    if dup:
        new_lines = _drop_known_version(new_lines, product, new["version"])
    return "\n".join(new_lines)


def _drop_known_version(lines, product, version):
    a, b = _product_span(lines, product)
    k = _find_key(lines, a, b, 4, "known_versions")
    ke = _block_end(lines, k, 4)
    starts = [i for i in range(k + 1, ke) if lines[i].startswith("      - ")] + [ke]
    for s, e in zip(starts, starts[1:]):
        if re.search(r'version:\s*"%s"' % re.escape(version), lines[s]):
            return lines[:s] + lines[e:]
    return lines


def _shared_item(lines, product, index):
    """(first, end) line span of products.<product>.shared_files[index]."""
    a, b = _product_span(lines, product)
    s = _find_key(lines, a, b, 4, "shared_files")
    se = _block_end(lines, s, 4)
    starts = [i for i in range(s + 1, se) if lines[i].startswith("      - ")] + [se]
    return starts[index], starts[index + 1]


def set_shared_commit(text: str, product: str, index: int, commit: str, blobs: dict, files=None) -> str:
    """New shared-files commit, blobs (and file list); the old commit + blobs go first in known_commits."""
    sf = wvt.load_yaml(text)["products"][product]["shared_files"][index]
    old_commit, old_blobs = sf["source"]["commit"], dict(sf["source"].get("blobs") or {})
    lines = text.split("\n")
    i0, i1 = _shared_item(lines, product, index)
    c = next(i for i in range(i0, i1) if re.match(r"^\s+commit:", lines[i]))
    lines[c] = re.sub(r'commit:\s*"?[0-9a-f]{40}"?', "commit: %s" % _q(commit), lines[c])
    if files and list(files) != list(sf["source"]["files"]):
        fl = next(i for i in range(i0, i1) if re.match(r"^\s+files:\s*\[", lines[i]))
        lines[fl] = re.sub(r"files:\s*\[[^\]]*\]", "files: [%s]" % ", ".join(files), lines[fl])
    bl = next((i for i in range(i0, i1) if re.match(r"^\s+blobs:", lines[i])), None)
    if bl is None:
        raise WvtError("%s.shared_files[%d] has no blobs to update" % (product, index))
    ind = len(lines[bl]) - len(lines[bl].lstrip(" "))
    lines[bl + 1:_block_end(lines, bl, ind)] = ["%s  %s: %s" % (" " * ind, n, _q(v)) for n, v in blobs.items()]
    i0, i1 = _shared_item(lines, product, index)
    entry = ["          - commit: %s" % _q(old_commit)]
    if old_blobs:
        entry += ["            blobs:"] + ["              %s: %s" % (n, _q(v)) for n, v in old_blobs.items()]
    kc = next((i for i in range(i0, i1) if re.match(r"^        known_commits:", lines[i])), None)
    if kc is None:
        end = i1
        while end > i0 and not lines[end - 1].strip():
            end -= 1
        lines[end:end] = ["        known_commits:"] + entry
    elif lines[kc].split(":", 1)[1].split("#")[0].strip() == "[]":
        lines[kc] = "        known_commits:"
        lines[kc + 1:kc + 1] = entry
    else:
        lines[kc + 1:kc + 1] = entry
    return "\n".join(lines)


def check_manifest_text(text: str, today) -> dict:
    m = wvt.load_yaml(text)
    errors, _ = wvt.validate_manifest(m, today, expiry=False)
    if errors:
        raise WvtError("edited manifest is invalid: %s" % "; ".join(errors[:5]))
    return m


# --------------------------------------------------------------------------
# propose
# --------------------------------------------------------------------------


def _marker(kind: str, **kw) -> str:
    return "<!-- wvt:bot %s %s -->" % (kind, " ".join("%s=%s" % (k, v) for k, v in sorted(kw.items())))


def _parse_marker(body: str):
    m = MARKER_RE.search(body or "")
    if not m:
        return None, {}
    return m.group(1), dict(kv.split("=", 1) for kv in m.group(2).split() if "=" in kv)


def new_target_for(p: dict, version: str, today, registry: Registry, get=None) -> dict:
    """Registry-verified target for `version` (index + per-arch digests, artifact sha256s)."""
    up = p.get("upstream") or {}
    old = p.get("target") or {}
    tag = (up.get("image_tag") or "{version}").format(version=version)
    image = up.get("image")
    res = registry.resolve(image, tag) if image else {"index_digest": None, "platforms": {}}
    new = {"version": version, "since": today.isoformat()}
    if old.get("index_digest"):
        if not res["index_digest"]:
            raise WvtError("%s:%s is not a multi-platform index" % (image, tag))
        new["index_digest"] = res["index_digest"]
    if old.get("platforms"):
        missing = [a for a in old["platforms"] if a not in res["platforms"]]
        if missing:
            raise WvtError("%s:%s has no %s image" % (image, tag, "/".join(missing)))
        new["platforms"] = {a: res["platforms"][a] for a in old["platforms"]}
    sources = ["registry %s:%s" % (image, tag)]
    if old.get("artifacts"):
        urls = up.get("artifact_sha256") or {}
        new["artifacts"] = {}
        for name in old["artifacts"]:
            if name not in urls:
                raise WvtError("upstream.artifact_sha256.%s missing: cannot verify the artifact" % name)
            url = urls[name].format(version=version)
            new["artifacts"][name] = fetch_sha256_file(url, get)
            sources.append(url)
    new["verified"] = "%s wvt propose：%s" % (today.isoformat(), "；".join(sources))
    return new


def plan_propose(m: dict, rep: dict, f, registry: Registry, today, only=None, force_version=None, get=None):
    """[{product, version, target, shared, reasons, notes}] for products to bump in the manifest."""
    plans = []
    for pname, p in m["products"].items():
        if only and pname != only:
            continue
        tv = p["target"]["version"]
        plan = {"product": pname, "old_version": tv, "version": None, "target": None, "shared": [], "reasons": [], "notes": []}
        ahead = [r for r in rep["rows"] if r["product"] == pname and r.get("source") == "branch"
                 and r["compare"] not in ("info", "shared_files") and any(x["status"] == "ahead" for x in r["findings"])
                 and wvt.parse_version(r.get("version")) and wvt.cmp_versions(r["version"], tv) > 0]
        version = force_version if force_version and only == pname else None
        if not version and ahead:
            version = max((r["version"] for r in ahead), key=wvt.parse_version)
            plan["reasons"] = ["%s %s：%s（%s）" % (wvt.LEG_NAMES[r["leg"]], r["point"], r["version"], r["file"])
                               for r in ahead if r["version"] == version]
        elif version:
            plan["reasons"] = ["手動指定（workflow_dispatch）：%s" % version]
        if version and version != tv:
            ceiling = (p.get("policy") or {}).get("license_ceiling")
            if ceiling and not wvt.satisfies(version, ceiling):
                plan["notes"].append("%s 超過授權上限 %s，不提案" % (version, ceiling))
            elif wvt.cmp_versions(version, tv) < 0 and (p.get("policy") or {}).get("downgrade") == "forbidden":
                plan["notes"].append("%s 低於清冊 %s，而此產品禁止降版，不提案" % (version, tv))
            else:
                try:
                    plan["target"] = new_target_for(p, version, today, registry, get)
                    plan["version"] = version
                except WvtError as exc:
                    plan["notes"].append("查不到 %s 的上游身分：%s" % (version, exc))
            # a leg that pins a digest must hold the registry's digest for that version
            if plan["target"]:
                for r in ahead:
                    d = (r.get("observed") or {}).get("digest")
                    want = wvt.target_value({"target": plan["target"]}, r["compare"]) if r["compare"] != "version" else None
                    if d and want and d != want:
                        plan["notes"].append("%s 的 digest %s ≠ registry %s：先查清楚再提案" % (r["file"], wvt.short(d), wvt.short(want)))
                        plan["target"], plan["version"] = None, None
                        break
        for idx, sf in enumerate(p.get("shared_files") or []):
            srow = next((r for r in rep["rows"] if r["product"] == pname and r["pin"] == "shared_source"), None)
            if not srow or srow["status"] != "ahead":
                continue
            src = sf["source"]
            try:
                blobs = f.blob_shas(src["repo"], src["path"], src.get("branch", "main"))
                commit = f.last_commit(src["repo"], src["path"], src.get("branch", "main"))
            except WvtError as exc:
                plan["notes"].append("讀不到共用檔來源：%s" % exc)
                continue
            missing = [n for n in src["files"] if n not in blobs]
            if missing:
                plan["notes"].append("來源少了共用檔 %s：要先決定清冊的檔案清單（人工）" % missing)
                continue
            try:
                files, added = import_closure(f, src, commit, blobs)
            except WvtError as exc:
                plan["notes"].append("讀不到共用檔內容：%s" % exc)
                continue
            new_blobs = {n: blobs[n] for n in files}
            plan["shared"].append({"index": idx, "commit": commit, "blobs": new_blobs, "files": files, "added": added,
                                   "old_commit": src["commit"],
                                   "changed": [n for n in src["files"] if new_blobs[n] != (src.get("blobs") or {}).get(n)],
                                   "extra": sorted(set(blobs) - set(files))})
        if plan["target"] or plan["shared"] or plan["notes"]:
            plans.append(plan)
    return plans


_PY_IMPORT = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_]\w*)", re.M)


def import_closure(f, src: dict, commit: str, blobs: dict):
    """The shared files plus every module of the same directory they import (transitively).

    A consumer that copies only the listed files would miss a module a shared file imports.
    """
    files, added, queue = list(src["files"]), [], list(src["files"])
    base = src["path"].rstrip("/") + "/"
    while queue:
        name = queue.pop(0)
        if not name.endswith(".py"):
            continue
        data = f.read_bytes(src["repo"], base + name, commit)
        if data is None:
            raise WvtError("%s@%s 沒有 %s" % (src["repo"], commit[:10], name))
        for mod in _PY_IMPORT.findall(data.decode("utf-8", "replace")):
            fn = mod + ".py"
            if fn in blobs and fn not in files:
                files.append(fn)
                added.append(fn)
                queue.append(fn)
    return files, added


def render_propose(m: dict, plan: dict, manifest_rev: str | None) -> tuple:
    p = m["products"][plan["product"]]
    name = p.get("display_name") or plan["product"]
    parts = []
    if plan["target"]:
        parts.append("清冊目標 %s → %s" % (plan["old_version"], plan["version"]))
    for s in plan["shared"]:
        parts.append("共用檔 %s → %s" % (s["old_commit"][:7], s["commit"][:7]))
    title = GITEA_TITLE_PREFIX + "release train：%s %s" % (name, "、".join(parts))
    pol = p.get("policy") or {}
    lead = ("某一邊已經比清冊新" if plan["target"] else "共用檔的來源（PaaS）先改了") + \
        "，所以把清冊往上調。**合併由版本負責人決定**；合併後 fanout 會到還沒跟上的那一邊開 PR。"
    b = [_marker("propose", product=plan["product"], version=plan["version"] or "-",
                 shared=",".join(s["commit"][:12] for s in plan["shared"]) or "-"),
         "## 機器人提案（propose）", "", lead, ""]
    if plan["target"]:
        t = plan["target"]
        b += ["### %s 清冊目標 %s → %s" % (name, plan["old_version"], plan["version"]), ""]
        b += ["- 為什麼：" + "；".join(plan["reasons"])]
        b += ["- 上游身分（registry 實查）：index `%s`" % (t.get("index_digest") or "—")]
        for a, d in (t.get("platforms") or {}).items():
            b.append("  - %s `%s`" % (a, d))
        for n, d in (t.get("artifacts") or {}).items():
            b.append("  - %s `%s`" % (n, d))
        b.append("- 舊目標 %s 移到 `known_versions`（落後的一邊會顯示「落後」而不是「不符」）。" % plan["old_version"])
        if pol.get("mode") == "range":
            b.append("- range：`target.since` 改為今天，落後天數從今天起算（上限 %s 天）。" % pol.get("max_days_behind"))
        if pol.get("downgrade") == "forbidden" or pol.get("upgrade_evidence") == "required":
            b += ["", "**合併前（此產品資料遷移單向）**：", "- [ ] 附升級證據：clone → 升級 → 驗證 → 還原（比照 woow_paas_platform docs/verification）"]
        b.append("")
    for s in plan["shared"]:
        b += ["### 共用檔 commit %s → %s" % (s["old_commit"][:10], s["commit"][:10]), "",
              "- 改了：%s" % ("、".join(s["changed"]) or "（只換 commit）"),
              "- 舊 commit 移到 `known_commits`：HA 的 lock 在同步前只算落後，不擋 HA 其他 PR。"]
        if s["added"]:
            b.append("- **共用清單新增 %d 個檔**：%s——共用檔會 import 它們，只複製原本的檔案 HA 會缺模組。不需要的話請在這張 PR 刪掉。"
                     % (len(s["added"]), "、".join(s["added"])))
        if s["extra"]:
            b.append("- 來源另有 %d 個檔沒被共用檔 import，維持 PaaS 專用：%s。" % (len(s["extra"]), "、".join(s["extra"])))
        b.append("")
    for n in plan["notes"]:
        b.append("- 註：" + n)
    b += ["", "清冊版本：`%s`。本 PR 由 `tools/wvtbot.py propose` 產生，重跑只更新這張、不另開。" % (manifest_rev or "?"), "",
          "🤖 Generated with [Claude Code](https://claude.com/claude-code)"]
    return title, "\n".join(b)


def apply_propose_text(text: str, plan: dict, today) -> str:
    if plan["target"]:
        text = set_target(text, plan["product"], plan["target"])
    for s in plan["shared"]:
        text = set_shared_commit(text, plan["product"], s["index"], s["commit"], s["blobs"], s.get("files"))
    check_manifest_text(text, today)
    return text


# --------------------------------------------------------------------------
# fanout
# --------------------------------------------------------------------------


def fanout_ref(pin: dict) -> str | None:
    """The branch a fanout PR targets: the first plain ref of the pin (never $prod_platform)."""
    for ref in [pin.get("ref")] + list(pin.get("also_refs") or []):
        if isinstance(ref, str) and not ref.startswith("$"):
            return ref
    return None


def desired_value(p: dict, pin: dict, obs: dict, jcr_tag=None):
    """New raw value for a pin that is behind / not equal to the manifest (None = leave it)."""
    tgt = p["target"]
    compare = pin.get("compare", "version")
    parse = pin.get("parse", "version")
    raw = str(obs.get("raw") or "")
    if pin["kind"] == "seed_image":
        digest = wvt.target_value(p, compare)
        return ("%s@%s" % (jcr_tag, digest)) if jcr_tag else None
    if parse == "image_ref":
        digest = wvt.target_value(p, compare) if compare != "version" else wvt.parse_image_ref(raw)["digest"]
        return new_image_ref(raw, tgt["version"], digest, jcr_tag)
    if parse == "digest":
        d = wvt.target_value(p, compare)
        return d[len("sha256:"):] if d and not raw.startswith("sha256:") else d
    v = tgt["version"]
    return ("v" + v) if raw[:1] in ("v", "V") else v


def needs_mirror(pin: dict, obs: dict) -> bool:
    """A PaaS pin that points into JCR: its new value exists only after mirror-image ran."""
    if pin.get("mirror") or pin["kind"] == "seed_image":
        return True
    if pin.get("parse") == "image_ref":
        return _in_jcr(wvt.parse_image_ref(str(obs.get("raw") or ""))["repository"])
    return False


def _template(pin, p, jcr_tag):
    w = pin["write"]
    if "{jcr_tag}" in w and not jcr_tag:
        raise WvtError("%s：需要 JCR 鏡像 tag" % pin["id"])
    return w.format(version=p["target"]["version"], jcr_tag=jcr_tag or "", digest=wvt.target_value(p, pin.get("compare", "index_digest")) or "")


def leg_repos(p: dict, leg: str) -> dict:
    """{(repo, branch): [pins]} a fanout of this product may touch on this leg."""
    by_repo = {}
    for pin in wvt.iter_pins(p, leg):
        ref = fanout_ref(pin)
        if ref and (pin.get("compare", "version") != "info" or pin.get("write")):
            by_repo.setdefault((pin["repo"], ref), []).append(pin)
    for sf in p.get("shared_files") or []:
        for c in sf.get("consumers") or []:
            if c["leg"] == leg:
                by_repo.setdefault((c["repo"], c.get("ref", "main")), [])
    return by_repo


def plan_fanout(m: dict, f, today, leg: str, only=None, mirrors=None):
    """[{product, leg, repo, base, files, changes, package, shared, blocked, needs_mirror}], one per
    product and repo whose pins are behind (range) or not equal (lockstep) to the manifest.

    `mirrors` = {product: jcr_tag} once that product's target digest is mirrored into JCR.
    """
    mirrors = mirrors or {}
    plans = []
    for pname, p in m["products"].items():
        if only and pname != only:
            continue
        legd = (p.get("legs") or {}).get(leg) or {}
        mode = (p.get("policy") or {}).get("mode")
        fo = legd.get("fanout") or {}
        for (repo, ref), pins in leg_repos(p, leg).items():
            plan = {"product": pname, "leg": leg, "repo": repo, "base": ref, "files": {}, "changes": [],
                    "package": [], "shared": None, "blocked": None, "needs_mirror": False}
            texts = {}

            def read(path, _repo=repo, _ref=ref):
                if path not in texts:
                    t = f.read(_repo, path, _ref)
                    if t is None:
                        raise WvtError("%s@%s 沒有 %s" % (_repo, _ref, path))
                    texts[path] = t
                return texts[path]

            def put(path, text):
                texts[path] = plan["files"][path] = text
            try:
                jcr = mirrors.get(pname)
                for pin in [x for x in pins if x.get("compare", "version") != "info"]:
                    obs = wvt.observe(pin, wvt.run_extractor(pin, read(pin["file"])))
                    res = wvt.evaluate(p, leg, pin, obs, today)
                    sts = {x["status"] for x in res["findings"]}
                    if "ahead" in sts or not sts & ({"behind"} if mode == "range" else {"behind", "mismatch"}):
                        continue
                    if fo.get("enabled") is False:
                        plan["blocked"] = "fanout 關閉：%s（%s %s）" % (fo.get("reason") or "需要人工", pin["id"], res.get("version") or obs.get("value"))
                        break
                    if needs_mirror(pin, obs):
                        plan["needs_mirror"] = True
                        if not jcr:
                            plan["blocked"] = "等上游 digest 鏡像進 JCR（mirror-image，人工 /approve）"
                            continue
                    new = desired_value(p, pin, obs, jcr)
                    if pin["kind"] == "seed_image":
                        cur = wvt.extract_seed_image(read(pin["file"]), pin["record"], pin.get("image_path", "image"))
                        old = cur["tag"] + ("@" + cur["digest"] if cur["digest"] else "")
                        put(pin["file"], write_seed_image_tag(read(pin["file"]), pin["record"], old, new))
                    else:
                        old = str(obs["raw"])
                        put(pin["file"], write_pin(read(pin["file"]), pin, new))
                    plan["changes"].append({"pin": pin["id"], "file": pin["file"], "old": old, "new": new, "version": res.get("version")})
                if plan["changes"] and not plan["blocked"]:
                    for pin in [x for x in pins if x.get("compare", "version") == "info" and x.get("write")]:
                        new = _template(pin, p, jcr)
                        old = wvt.run_extractor(pin, read(pin["file"]))
                        if str(old) != new:
                            put(pin["file"], write_pin(read(pin["file"]), pin, new))
                            plan["changes"].append({"pin": pin["id"], "file": pin["file"], "old": str(old), "new": new, "version": None})
                for sf in p.get("shared_files") or []:
                    for c in sf.get("consumers") or []:
                        if c["leg"] == leg and c["repo"] == repo:
                            sync = sync_shared(sf, c, read(c["lock"]), f, today)
                            if sync:
                                plan["shared"] = sync
                                for path, text in sync["files"].items():
                                    put(path, text)
                if (plan["changes"] or plan["shared"]) and not plan["blocked"]:
                    bump_packages(p, legd, repo, plan, read, put)
                    write_changelog(p, legd, plan, read, put)
            except WvtError as exc:
                plan["blocked"] = str(exc)
            if plan["changes"] or plan["shared"] or plan["blocked"]:
                plans.append(plan)
    return plans


def bump_packages(p, legd, repo, plan, read, put):
    """package_version entries of this repo with `bump: same` (= upstream version) or `patch`."""
    pkgs = legd.get("package_version") or []
    for pkg in ([pkgs] if isinstance(pkgs, dict) else pkgs):
        rule = pkg.get("bump", "none")
        if rule == "none" or (pkg.get("repo") or legd.get("repo")) != repo:
            continue
        old = wvt.run_extractor(pkg, read(pkg["file"]))
        if old is None:
            continue
        old = str(old)
        new = p["target"]["version"] if rule == "same" else bump_patch(old)
        if rule == "same" and old[:1] in "vV":
            new = "v" + new
        if old != new:
            put(pkg["file"], write_pin(read(pkg["file"]), pkg, new))
            plan["package"].append({"label": pkg.get("label", "版號"), "file": pkg["file"], "old": old, "new": new})


def write_changelog(p, legd, plan, read, put):
    fo = legd.get("fanout") or {}
    path = fo.get("changelog")
    if not path:
        return
    pkg = next((x["new"] for x in plan["package"] if x["file"].endswith(("config.yaml", "Chart.yaml"))), None)
    heading = (pkg or p["target"]["version"]).lstrip("vV")
    old = next((c.get("version") or c["old"] for c in plan["changes"] if c.get("version")), None)
    tmpl = fo.get("changelog_entry") or "- {name} {old} → {new} (woow release train). Replace this line with the release notes before merging."
    if plan["shared"] and not plan["changes"]:
        tmpl = fo.get("changelog_shared") or "- Shared files synced to {commit} (woow release train). Replace this line with the release notes before merging."
    entry = tmpl.format(name=p.get("display_name") or plan["product"], old=old or "?", new=p["target"]["version"],
                        commit=(plan["shared"] or {}).get("commit", "")[:10])
    try:
        put(path, changelog_with_entry(read(path), heading, entry))
    except WvtError:
        pass


def sync_shared(sf, consumer, lock_text, f, today):
    """Files + lock that bring a consumer's copies to the manifest's shared-files commit."""
    src = sf["source"]
    try:
        lock = json.loads(lock_text)
    except ValueError:
        raise WvtError("lock %s 不是 JSON" % consumer["lock"])
    if (lock.get("source") or {}).get("commit") == src["commit"]:
        return None
    dest = lock.get("destination")
    if not dest:
        raise WvtError("lock %s 沒有 destination，無法同步" % consumer["lock"])
    files, meta = {}, {}
    for name in src["files"]:
        data = f.read_bytes(src["repo"], src["path"].rstrip("/") + "/" + name, src["commit"])
        if data is None:
            raise WvtError("%s@%s 沒有 %s" % (src["repo"], src["commit"][:10], name))
        blob = wvt.git_blob_sha(data)
        want = (src.get("blobs") or {}).get(name)
        if want and blob != want:
            raise WvtError("%s 的 blob %s ≠ 清冊 %s" % (name, wvt.short(blob, 10), wvt.short(want, 10)))
        files[os.path.join(dest, name)] = data.decode("utf-8")
        meta[name] = {"git_blob": blob, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    chart = f.read(src["repo"], "charts/%s/Chart.yaml" % src["path"].strip("/").split("/")[1], src["commit"]) if src["path"].startswith("charts/") else None
    new_lock = dict(lock)
    new_lock["source"] = dict(lock.get("source") or {}, commit=src["commit"], branch=src.get("branch", "main"),
                              merged_to_main=True, chart_version=str(wvt.extract_yaml(chart, "version")) if chart else (lock.get("source") or {}).get("chart_version"))
    new_lock["synced_at"] = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_lock["files"] = meta
    files[consumer["lock"]] = json.dumps(new_lock, indent=2) + "\n"  # same as the HA repo's tools/sync-woow-plugin.sh
    old_commit = (lock.get("source") or {}).get("commit")
    return {"files": files, "old_commit": old_commit, "commit": src["commit"], "names": list(src["files"])}


def render_fanout(m, plan, manifest_rev, forge_kind):
    p = m["products"][plan["product"]]
    name = p.get("display_name") or plan["product"]
    tv = p["target"]["version"]
    olds = sorted({c.get("version") or c["old"] for c in plan["changes"]} - {None}, key=lambda v: wvt.parse_version(v) or (0,))
    what = "%s → %s" % ("/".join(olds) or "?", tv) if plan["changes"] else "共用檔同步 %s" % (plan["shared"]["commit"][:7])
    if forge_kind == "github":
        title = "release train: %s %s" % (name, what)
    else:
        title = GITEA_TITLE_PREFIX + "release train：%s %s" % (name, what)
    pol = p.get("policy") or {}
    b = [_marker("fanout", product=plan["product"], version=tv, repo=plan["repo"].split(":", 1)[1]),
         "## 依版本清冊同步（fanout）", "",
         "清冊（woow-release-train `woow-versions.yaml`）已經是 **%s %s**，這個 repo 還沒跟上。這張 PR 只改清冊知道的釘點；**由人審核合併**。" % (name, tv), ""]
    if plan["changes"]:
        b += ["| 釘點 | 檔案 | 舊值 | 新值 |", "|---|---|---|---|"]
        b += ["| %s | `%s` | `%s` | `%s` |" % (c["pin"], c["file"], wvt.short(c["old"], 40) if c["old"].startswith("sha256:") else c["old"], c["new"]) for c in plan["changes"]]
        b.append("")
    for x in plan["package"]:
        b.append("- %s：%s → %s（`%s`）" % (x["label"], x["old"], x["new"], x["file"]))
    if plan["shared"]:
        s = plan["shared"]
        b.append("- 共用檔：%s 從清冊 commit `%s` 重新複製（舊 `%s`），lock 一併重寫。" % ("、".join(s["names"]), s["commit"][:10], (s["old_commit"] or "?")[:10]))
    b += ["", "合併前請確認：", "- [ ] 本 repo 的 CI（建置、smoke）全綠；CHANGELOG 把機器人那一行換成實際變更說明",
          "- [ ] 其他寫死這個版本的地方（文件、測試、README）一併更新"]
    if pol.get("downgrade") == "forbidden" or pol.get("upgrade_evidence") == "required":
        b.append("- [ ] 資料遷移單向：附「備份 → 升級 → 驗證 → 還原」的證據")
    if plan["needs_mirror"]:
        b.append("- [ ] JCR 已有鏡像 tag（mirror-image 核可單已放行且成功）")
    b += ["", "清冊版本：`%s`。重跑只更新這張 PR；這邊對齊清冊後機器人會自動關掉它。" % (manifest_rev or "?"), "",
          "🤖 Generated with [Claude Code](https://claude.com/claude-code)"]
    return title, "\n".join(b)


# --------------------------------------------------------------------------
# PaaS mirror state: read the mirror-image approval issues, dispatch when none exists
# --------------------------------------------------------------------------


def mirror_source(p: dict, legd: dict) -> str | None:
    mir = legd.get("mirror")
    if not mir:
        return None
    digest = wvt.target_value(p, mir["digest"])
    return "%s@%s" % (p["upstream"]["image"], digest) if digest else None


def mirror_state(gitea: Gitea, source: str) -> dict:
    """{state: none|pending|approved|rejected, issue, tag} of the newest approval issue for source."""
    issues = gitea.call("GET", "/repos/%s/issues?state=all&type=issues&limit=50&q=%s" % (MIRROR_REPO, urllib.parse.quote(MIRROR_TITLE))) or []
    for iss in sorted(issues, key=lambda x: -x["number"]):
        if not iss.get("title", "").startswith(MIRROR_TITLE):
            continue
        ctx = dict(re.findall(r"^(\w+)=(.*)$", (iss.get("body") or "").split("<!-- approval-context", 1)[-1], re.M))
        if ctx.get("ADDON_REF") != source:
            continue
        tag = ctx.get("IMAGE_TAG")
        if iss["state"] == "open":
            return {"state": "pending", "issue": iss["number"], "tag": tag}
        comments = gitea.call("GET", "/repos/%s/issues/%d/comments" % (MIRROR_REPO, iss["number"])) or []
        if any("放行" in (c.get("body") or "") for c in comments):
            return {"state": "approved", "issue": iss["number"], "tag": tag}
        return {"state": "rejected", "issue": iss["number"], "tag": tag}
    return {"state": "none", "issue": None, "tag": None}


def mirror_running(gitea: Gitea) -> bool:
    """A mirror-image run is queued or running: its approval issue may not exist yet."""
    data = gitea.call("GET", "/repos/%s/actions/runs?limit=20" % MIRROR_REPO) or {}
    return any(str(r.get("path", "")).startswith(MIRROR_WORKFLOW + "@") and r.get("status") in ("queued", "waiting", "in_progress", "running")
               for r in data.get("workflow_runs") or [])


def dispatch_mirror(gitea: Gitea, source: str, dest_name: str, now=None) -> str:
    tag = (now or _dt.datetime.now(_dt.timezone.utc)).strftime("%Y%m%d.%H%M")
    gitea.call("POST", "/repos/%s/actions/workflows/%s/dispatches" % (MIRROR_REPO, MIRROR_WORKFLOW),
               {"ref": "main", "inputs": {"source_image": source, "dest_name": dest_name, "dest_tag": tag}})
    return tag


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


class BotFetcher(wvt.Fetcher):
    """wvt.Fetcher + the extra reads the bots need."""

    def read_bytes(self, repo, path, ref):
        host, full = repo.split(":", 1)
        if host == "github":
            st, _, body = http("https://raw.githubusercontent.com/%s/%s/%s" % (full, ref, urllib.parse.quote(path)), {})
        else:
            st, _, body = http("%s/repos/%s/raw/%s?ref=%s" % (self.gitea_api, full, urllib.parse.quote(path), urllib.parse.quote(ref)), self._gitea_hdr())
        if st == 404:
            return None
        if st != 200:
            raise WvtError("%s %s@%s: HTTP %d" % (repo, path, ref, st))
        return body

    def last_commit(self, repo, path, ref):
        full = repo.split(":", 1)[1]
        data = self.gitea_json("/repos/%s/commits?sha=%s&path=%s&limit=1&stat=false&verification=false&files=false"
                               % (full, urllib.parse.quote(ref), urllib.parse.quote(path.rstrip("/"))))
        if not data:
            raise WvtError("%s: no commit touches %s" % (repo, path))
        return data[0]["sha"]


def run(args, env=None) -> int:
    env = env if env is not None else os.environ
    today = wvt._today(args)
    m = wvt.load_manifest(args.manifest)
    errors, _ = wvt.validate_manifest(m, today, expiry=False)
    if errors:
        for e in errors:
            print("ERROR manifest: %s" % e)
        return 2
    modes = bot_modes(m)
    if args.mode:
        modes = {b: args.mode for b in modes}
    selected = BOTS if args.bot == "all" else (args.bot,)
    rev = wvt._manifest_rev(args.manifest)
    gitea_api = env.get("GITEA_API_URL", "https://git-prod.woowtech.io/api/v1")
    f = BotFetcher(gitea_api, env.get("GITEA_READ_TOKEN") or env.get("GITEA_TOKEN"), env.get("GITHUB_TOKEN"))
    rep = wvt.collect_drift(m, f, today)
    report = ["## woow release train 機器人（%s）" % today.isoformat(), ""]
    rc = 0
    for bot in selected:
        mode = modes[bot]
        if mode == "off":
            report.append("- %s：off" % bot)
            continue
        try:
            if bot == "propose":
                report += do_propose(m, rep, f, today, mode, args, env, rev)
            else:
                report += do_fanout(m, f, today, "ha" if bot == "fanout_ha" else "paas", mode, args, env, rev)
        except WvtError as exc:
            report.append("- %s：失敗：%s" % (bot, exc))
            rc = 1
    text = "\n".join(report) + "\n"
    print(text)
    if args.plan:
        with open(args.plan, "w", encoding="utf-8") as fh:
            fh.write(text)
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(text)
    return rc


def _gitea_bot(env, write_token_name="WVT_GITEA_BOT_TOKEN"):
    token = env.get(write_token_name) or env.get("GITEA_TOKEN")
    if not token:
        raise WvtError("沒有 Gitea 寫入權杖（%s／GITEA_TOKEN）" % write_token_name)
    return Gitea(env.get("GITEA_API_URL", "https://git-prod.woowtech.io/api/v1"), token), token


def _git_url(env, repo_full):
    base = env.get("GITEA_GIT_URL") or env.get("GITEA_API_URL", "https://git-prod.woowtech.io/api/v1").rsplit("/api/v1", 1)[0]
    return "%s/%s.git" % (base.rstrip("/"), repo_full)


def do_propose(m, rep, f, today, mode, args, env, rev):
    out = ["### propose（%s）" % mode]
    plans = plan_propose(m, rep, f, Registry(), today, args.product or None, args.version or None)
    repo = env.get("WVT_MANIFEST_REPO", "woow-paas/woow-release-train")
    if mode == "open-pr":
        live = {pl["product"] for pl in plans if pl["target"] or pl["shared"]}
        gitea = _gitea_bot(env)[0]
        for pname in m["products"]:
            if pname not in live and not (args.product and pname != args.product):
                for n in close_stale(gitea, repo, "wvt/propose-" + pname, "清冊已涵蓋，或已沒有比清冊新的一邊；機器人關閉這張 PR。"):
                    out.append("- %s：關閉過時的提案 #%d" % (pname, n))
    if not plans:
        return out + ["- 沒有一邊比清冊新，也沒有共用檔要換 commit。", ""]
    with open(args.manifest, encoding="utf-8") as fh:
        text = fh.read()
    for plan in plans:
        name = plan["product"]
        if not (plan["target"] or plan["shared"]):
            out += ["- %s：%s" % (name, "；".join(plan["notes"]))]
            continue
        title, body = render_propose(m, plan, rev)
        new_text = apply_propose_text(text, plan, today)
        out.append("- %s：%s" % (name, title.replace(GITEA_TITLE_PREFIX, "")))
        if mode != "open-pr":
            continue
        gitea, token = _gitea_bot(env)
        branch = "wvt/propose-" + name
        closed = [pr for pr in gitea.find_prs(repo, branch, "closed") if not pr.get("merged")]
        _kind, kv = _parse_marker((closed[0] if closed else {}).get("body"))
        if closed and kv.get("version") == (plan["version"] or "-") and kv.get("shared") == (",".join(s["commit"][:12] for s in plan["shared"]) or "-"):
            out.append("  - 同一提案上次被關掉（#%d），不再重開；要重提請手動 workflow_dispatch 並指定版本。" % closed[0]["number"])
            continue
        status, _ = push_branch(_git_url(env, repo), basic(env.get("WVT_GITEA_BOT_USER", "wvt-bot"), token), "main", branch,
                                {os.path.basename(args.manifest): new_text}, title.replace(GITEA_TITLE_PREFIX, ""))
        opened = gitea.find_prs(repo, branch)
        if opened:
            gitea.update_pr(repo, opened[0]["number"], title, body)
            out.append("  - 更新 #%d（%s）" % (opened[0]["number"], status))
        else:
            pr = gitea.create_pr(repo, branch, "main", title, body)
            out.append("  - 開 #%d" % pr["number"])
    return out + [""]


def forge_for(repo: str, env):
    """(forge, git url, auth header) for a bot push to github:… (GitHub App) or gitea:…"""
    kind, full = repo.split(":", 1)
    if kind == "github":
        app_id, key = env.get("WVT_GH_APP_ID"), env.get("WVT_GH_APP_PRIVATE_KEY")
        if not (app_id and key):
            raise WvtError("沒有 GitHub App（WVT_GH_APP_ID／WVT_GH_APP_PRIVATE_KEY）")
        token = github_installation_token(app_id, key, full)
        return GitHub("https://api.github.com", token), "https://github.com/%s.git" % full, basic("x-access-token", token)
    forge, token = _gitea_bot(env)
    return forge, _git_url(env, full), basic(env.get("WVT_GITEA_BOT_USER", "wvt-bot"), token)


def close_stale(forge, full, branch, why):
    closed = []
    for pr in forge.find_prs(full, branch):
        forge.close_pr(full, pr["number"], why)
        closed.append(pr["number"])
    return closed


def do_fanout(m, f, today, leg, mode, args, env, rev):
    out, notes = ["### fanout_%s（%s）" % (leg, mode)], []
    mirrors, states = {}, {}
    if leg == "paas":
        reader = Gitea(env.get("GITEA_API_URL", "https://git-prod.woowtech.io/api/v1"),
                       env.get("WVT_GITEA_BOT_TOKEN") or env.get("GITEA_READ_TOKEN") or env.get("GITEA_TOKEN") or "")
        for pname, p in m["products"].items():
            src = mirror_source(p, (p.get("legs") or {}).get("paas") or {})
            if not src or (args.product and pname != args.product):
                continue
            try:
                states[pname] = (src, mirror_state(reader, src))
            except WvtError as exc:
                notes.append("- %s：讀不到鏡像核可單：%s" % (pname, exc))
                continue
            if states[pname][1]["state"] == "approved":
                mirrors[pname] = states[pname][1]["tag"]
    plans = plan_fanout(m, f, today, leg, args.product or None, mirrors)
    asked = set()
    for plan in plans:
        pname = plan["product"]
        if plan["blocked"]:
            if plan["needs_mirror"] and pname in states and pname not in asked:
                asked.add(pname)
                src, st = states[pname]
                if st["state"] == "pending":
                    notes.append("- %s：鏡像核可單 paas-odoo-ci#%d 等 /approve（%s）" % (pname, st["issue"], src))
                elif st["state"] == "rejected":
                    notes.append("- %s：鏡像核可單 paas-odoo-ci#%d 被拒或逾時，不自動重送（手動 dispatch mirror-image）" % (pname, st["issue"]))
                elif mode != "open-pr":
                    notes.append("- %s：會送出 mirror-image（%s）" % (pname, src))
                else:
                    gitea = _gitea_bot(env)[0]
                    if mirror_running(gitea):
                        notes.append("- %s：已有 mirror-image 在跑，下次再送" % pname)
                    else:
                        tag = dispatch_mirror(gitea, src, m["products"][pname]["legs"]["paas"]["mirror"]["dest_name"])
                        notes.append("- %s：已送出 mirror-image（%s → %s），等 /approve" % (pname, src, tag))
            elif not plan["needs_mirror"]:
                out.append("- %s @ %s：擋住：%s" % (pname, plan["repo"], plan["blocked"]))
            continue
        forge_kind = plan["repo"].split(":", 1)[0]
        title, body = render_fanout(m, plan, rev, forge_kind)
        out.append("- %s @ %s：%s（%d 個檔）" % (pname, plan["repo"], title.replace(GITEA_TITLE_PREFIX, ""), len(plan["files"])))
        if mode != "open-pr":
            continue
        full = plan["repo"].split(":", 1)[1]
        branch = "wvt/fanout-" + pname
        try:
            forge, url, auth = forge_for(plan["repo"], env)
        except WvtError as exc:
            out.append("  - %s，只出計畫" % exc)
            continue
        status, _ = push_branch(url, auth, plan["base"], branch, plan["files"], title.replace(GITEA_TITLE_PREFIX, ""))
        opened = forge.find_prs(full, branch)
        if status == "unchanged":
            for pr in opened:
                forge.close_pr(full, pr["number"], "已對齊清冊，機器人關閉這張 PR。")
            out.append("  - 已對齊（無變更）")
        elif opened:
            forge.update_pr(full, opened[0]["number"], title, body)
            out.append("  - 更新 #%d（%s）" % (opened[0]["number"], status))
        else:
            pr = forge.create_pr(full, branch, plan["base"], title, body)
            out.append("  - 開 #%d" % pr["number"])
    if not plans:
        out.append("- 這一邊都已對齊清冊。")
    if mode == "open-pr":
        touched = {(pl["product"], pl["repo"]) for pl in plans}
        for pname, p in m["products"].items():
            if args.product and pname != args.product:
                continue
            for (repo, _ref) in leg_repos(p, leg):
                if (pname, repo) in touched:
                    continue
                try:
                    forge, _, _ = forge_for(repo, env)
                    for n in close_stale(forge, repo.split(":", 1)[1], "wvt/fanout-" + pname, "已對齊清冊，機器人關閉這張 PR。"):
                        out.append("- %s @ %s：已對齊，關閉 #%d" % (pname, repo, n))
                except WvtError:
                    pass
    return out + notes + [""]


def main(argv=None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(prog="wvtbot", description=__doc__.split("\n")[0])
    ap.add_argument("--manifest", default=os.environ.get("WVT_MANIFEST", os.path.join(os.path.dirname(here), "woow-versions.yaml")))
    ap.add_argument("--today", help="override today's date (YYYY-MM-DD) for tests")
    ap.add_argument("--bot", choices=("all",) + BOTS, default="all")
    ap.add_argument("--mode", choices=BOT_MODES, help="override the manifest's bot modes (e.g. dry-run locally)")
    ap.add_argument("--product", default="")
    ap.add_argument("--version", default="", help="propose: put this upstream version up for the product (manual upgrade)")
    ap.add_argument("--plan", help="also write the report here")
    args = ap.parse_args(argv)
    if args.version and not args.product:
        ap.error("--version needs --product")
    try:
        return run(args)
    except WvtError as exc:
        print("wvtbot: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
