"""Shared test helpers: import wvt from tools/, fixture paths, a fake fetcher."""

import copy
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, os.path.join(ROOT, "tools"))

import wvt  # noqa: E402

# Behaviour tests read a frozen snapshot that matches the fixtures; the bots rewrite the live
# manifest, which is only checked for being valid (test_manifest.LiveManifestTest).
MANIFEST_PATH = os.path.join(FIX, "woow-versions.yaml")
LIVE_MANIFEST_PATH = os.path.join(ROOT, "woow-versions.yaml")


def fixture(*parts):
    with open(os.path.join(FIX, *parts), encoding="utf-8") as fh:
        return fh.read()


def manifest():
    """A fresh deep copy of the real manifest (tests may mutate it)."""
    return copy.deepcopy(wvt.load_manifest(MANIFEST_PATH))


def litellm_repo(tmp, contents=None):
    """A Woow_ha_litellm checkout with synthetic plugin copies, plus a JSON manifest whose
    shared-file blobs match them. The real copies are not vendored into these fixtures."""
    contents = contents or {"sitecustomize.py": b"# a\n", "woow_chatgpt.py": b"# b\n", "woow_chatgpt_plugin.py": b"# c\n"}
    repo = os.path.join(tmp, "repo")
    shutil.copytree(os.path.join(FIX, "ha_litellm"), repo)
    lock_path = os.path.join(repo, "litellm", "woow-plugin.lock.json")
    with open(lock_path, encoding="utf-8") as fh:
        lock = json.load(fh)
    dest = os.path.join(repo, lock["destination"])
    os.makedirs(dest)
    blobs = {}
    for name, data in contents.items():
        with open(os.path.join(dest, name), "wb") as fh:
            fh.write(data)
        blobs[name] = lock["files"][name]["git_blob"] = wvt.git_blob_sha(data)
    with open(lock_path, "w", encoding="utf-8") as fh:
        json.dump(lock, fh, indent=2)
    m = manifest()
    m["products"]["litellm"]["shared_files"][0]["source"]["blobs"] = blobs
    return repo, write_manifest(tmp, m), m


def write_manifest(tmp, m):
    path = os.path.join(tmp, "manifest.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(m, fh)
    return path


# repo -> fixture directory (both refs map to the same files unless overridden)
REPO_DIRS = {
    "github:WOOWTECH/Woow_ha_emqx": "ha_emqx",
    "github:WOOWTECH/Woow_ha_litellm": "ha_litellm",
    "github:WOOWTECH/Woow_ha_n8n": "ha_n8n",
    "github:WOOWTECH/Woow_HA_App_Store": "store",
    "gitea:odoo-addons/woow_paas_platform": "paas_platform",
    "gitea:woow-paas/woow-paas-charts": "charts",
    "gitea:woow-paas/paas-odoo-ci": "ci",
    "gitea:woow-paas/paas-app-bundle-cd": "cd",
}

LITELLM_MAIN_BLOBS = {
    "sitecustomize.py": "f5407aacd4ed0d0202da7d6c578ef6a267b8a45b",
    "woow_chatgpt.py": "527c16c68a83958cc2f124253632b98de20b56c6",
    "woow_chatgpt_plugin.py": "cfcd0f65c257f44db3e995ea6fd635e76c7255f3",
    "woow_revoke_ui_sessions.py": "47a00c53a6004311ed48d792bf095ab43b90460b",
}


def approval_issues():
    """The recorded paas-odoo-ci build approval issues (tests/fixtures/gitea)."""
    with open(os.path.join(FIX, "gitea", "paas-odoo-ci-approval-issues.json"), encoding="utf-8") as fh:
        return json.load(fh)["issues"]


class FakeFetcher(wvt.Fetcher):
    """Serves fixtures instead of the network. overrides: {(repo, path, ref): text|None|Exception}.

    issues: the paas-odoo-ci issues an issue search sees (default: the recorded approval issues)."""

    def __init__(self, overrides=None, unreachable=(), blobs=None, behind=7, issues=None):
        super().__init__()
        self.overrides = overrides or {}
        self.unreachable = set(unreachable)
        self.blobs = LITELLM_MAIN_BLOBS if blobs is None else blobs
        self.behind = behind
        self.issues = approval_issues() if issues is None else issues
        self.calls = []

    def read(self, repo, path, ref):
        self.calls.append((repo, path, ref))
        host = repo.split(":", 1)[0]
        ep = "raw.githubusercontent.com" if host == "github" else "gitea"
        if host in self.unreachable or repo in self.unreachable:
            self._probe(ep, "URLError: simulated")
            raise wvt.WvtError("%s unreachable: simulated" % ep)
        key = (repo, path, ref)
        if key in self.overrides:
            val = self.overrides[key]
            if isinstance(val, Exception):
                raise val
            self._probe(ep)
            return val
        d = REPO_DIRS.get(repo)
        full = os.path.join(FIX, d, path) if d else None
        self._probe(ep)
        if not full or not os.path.exists(full):
            return None
        with open(full, encoding="utf-8") as fh:
            return fh.read()

    def blob_shas(self, repo, path, ref):
        if "gitea" in self.unreachable:
            raise wvt.WvtError("gitea unreachable: simulated")
        return dict(self.blobs)

    def commits_between(self, repo, base, head):
        return self.behind

    def search_issues(self, repo, query):
        self.calls.append(("search", repo, query))
        if "gitea" in self.unreachable or repo in self.unreachable:
            raise wvt.WvtError("gitea unreachable: simulated")
        return [dict(i) for i in self.issues if query in (i.get("title") or "") or query in (i.get("body") or "")]

    # wvtbot.BotFetcher extras
    def read_bytes(self, repo, path, ref):
        key = (repo, path, ref)
        if key in self.bytes_overrides:
            return self.bytes_overrides[key]
        text = self.read(repo, path, ref)
        return None if text is None else text.encode("utf-8")

    def last_commit(self, repo, path, ref):
        return self.head_commit

    bytes_overrides = {}
    head_commit = "d3cd6898b29a9e094fd616bc151399c9884e5c19"
