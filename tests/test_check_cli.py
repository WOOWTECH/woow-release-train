import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from helpers import FIX, MANIFEST_PATH, litellm_repo, manifest, write_manifest, wvt


def run(*argv, manifest_path=MANIFEST_PATH, today="2026-10-16"):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = wvt.main(["--manifest", manifest_path, "--today", today] + list(argv))
    return rc, buf.getvalue()


def _sub(path, old, new):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert old in text
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace(old, new))


class CheckCliTest(unittest.TestCase):
    def tmpdir(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        return tmp

    def copy_fixture(self, name):
        dst = os.path.join(self.tmpdir(), "r")
        shutil.copytree(os.path.join(FIX, name), dst)
        return dst

    def test_ha_n8n_aligned(self):
        rc, out = run("check", "--leg", "ha", "--repo-dir", os.path.join(FIX, "ha_n8n"),
                      "--repo", "github:WOOWTECH/Woow_ha_n8n")
        self.assertEqual(rc, 0)
        self.assertIn("2.43.6", out)
        self.assertIn("一致", out)

    def test_paas_seed_behind_only_warns(self):
        # phase 1 (enforcement hard-rules): n8n behind is not a hard rule
        rc, out = run("check", "--leg", "paas", "--repo-dir", os.path.join(FIX, "paas_platform"),
                      "--repo", "gitea:odoo-addons/woow_paas_platform")
        self.assertEqual(rc, 0)
        self.assertIn("落後", out)
        self.assertIn("1 項警告", out)

    def test_fail_on_none_is_report_only(self):
        rc, out = run("check", "--leg", "paas", "--repo-dir", os.path.join(FIX, "paas_platform"),
                      "--repo", "gitea:odoo-addons/woow_paas_platform", "--fail-on", "none")
        self.assertEqual(rc, 0)
        self.assertIn("只報告", out)

    def test_strict_fails_on_drift(self):
        rc, _ = run("check", "--leg", "paas", "--repo-dir", os.path.join(FIX, "paas_platform"),
                    "--repo", "gitea:odoo-addons/woow_paas_platform", "--strict")
        self.assertEqual(rc, 1)

    def test_enforcement_switch_lives_in_the_manifest(self):
        r = self.copy_fixture("ha_emqx")
        _sub(os.path.join(r, "emqx", "Dockerfile"), 'EMQX_VERSION="v5.8.9"', 'EMQX_VERSION="v5.10.5"')
        argv = ("check", "--leg", "ha", "--repo-dir", r, "--repo", "github:WOOWTECH/Woow_ha_emqx")
        rc, out = run(*argv)
        self.assertEqual(rc, 1)
        self.assertIn("授權上限", out)
        self.assertIn("硬規則", out)
        m = manifest()
        m["enforcement"] = "report-only"  # the emergency switch: one manifest PR, every gate
        rc, _ = run(*argv, manifest_path=write_manifest(self.tmpdir(), m))
        self.assertEqual(rc, 0)
        rc, _ = run(*argv, "--fail-on", "none")
        self.assertEqual(rc, 0)

    def test_soft_findings_do_not_fail_hard_rules(self):
        # an unknown EMQX tarball digest is a warning (digest is not one of EMQX's hard rules)
        r = self.copy_fixture("ha_emqx")
        _sub(os.path.join(r, "emqx", "Dockerfile"),
             "c3b5a8f7c83601867d0fc1ef19a851f6f16f56a9436de74cb05f81f188a44a90", "0" * 64)
        rc, out = run("check", "--leg", "ha", "--repo-dir", r, "--repo", "github:WOOWTECH/Woow_ha_emqx")
        self.assertEqual(rc, 0)
        self.assertIn("不是清冊目標", out)

    def test_unknown_seed_digest_fails_license_closed(self):
        # a seed digest the manifest does not know: the version (so the license) cannot be told
        r = self.copy_fixture("paas_platform")
        _sub(os.path.join(r, "src", "data", "cloud_app_templates.xml"),
             "8b92a101d1a1642a191507b9133c46aad095bb64300dba1770108a2c7261b11a", "1" * 64)
        rc, out = run("check", "--leg", "paas", "--repo-dir", r, "--repo", "gitea:odoo-addons/woow_paas_platform")
        self.assertEqual(rc, 1)
        self.assertIn("無法確認授權上限", out)

    def test_missing_pin_file_fails_closed_only_with_hard_rules(self):
        r = self.copy_fixture("ha_emqx")
        os.remove(os.path.join(r, "emqx", "Dockerfile"))
        rc, out = run("check", "--leg", "ha", "--repo-dir", r, "--repo", "github:WOOWTECH/Woow_ha_emqx")
        self.assertEqual(rc, 1)
        self.assertIn("找不到 emqx/Dockerfile", out)
        r = self.copy_fixture("ha_n8n")
        os.remove(os.path.join(r, "n8n", "Dockerfile"))
        rc, _ = run("check", "--leg", "ha", "--repo-dir", r, "--repo", "github:WOOWTECH/Woow_ha_n8n")
        self.assertEqual(rc, 0)  # n8n has no hard rules in phase 1

    def test_litellm_lock_and_copies_checked(self):
        repo, mpath, _ = litellm_repo(self.tmpdir())
        rc, out = run("check", "--leg", "ha", "--repo-dir", repo, "--repo", "github:WOOWTECH/Woow_ha_litellm",
                      manifest_path=mpath)
        self.assertEqual(rc, 0, out)
        self.assertIn("shared_files", out)
        self.assertIn("3 個檔案與清冊一致", out)

    def test_lock_commit_drift_detected(self):
        repo, mpath, _ = litellm_repo(self.tmpdir())
        _sub(os.path.join(repo, "litellm", "woow-plugin.lock.json"), "4ad95a11d8dad671b342d6cc9454c87a3130e8b6", "0" * 40)
        rc, out = run("check", "--leg", "ha", "--repo-dir", repo, "--repo", "github:WOOWTECH/Woow_ha_litellm",
                      manifest_path=mpath)
        self.assertEqual(rc, 1)
        self.assertIn("lock commit", out)

    def test_hand_edited_copy_detected(self):
        repo, mpath, _ = litellm_repo(self.tmpdir())
        with open(os.path.join(repo, "litellm/rootfs/usr/share/woow-litellm/py/woow_chatgpt.py"), "ab") as fh:
            fh.write(b"# patched by hand\n")
        rc, out = run("check", "--leg", "ha", "--repo-dir", repo, "--repo", "github:WOOWTECH/Woow_ha_litellm",
                      manifest_path=mpath)
        self.assertEqual(rc, 1)
        self.assertIn("複本只能用同步工具更新", out)

    def test_lock_blob_must_match_manifest(self):
        tmp = self.tmpdir()
        repo, _, m = litellm_repo(tmp)
        m["products"]["litellm"]["shared_files"][0]["source"]["blobs"]["woow_chatgpt.py"] = "e" * 40
        rc, out = run("check", "--leg", "ha", "--repo-dir", repo, "--repo", "github:WOOWTECH/Woow_ha_litellm",
                      manifest_path=write_manifest(tmp, m))
        self.assertEqual(rc, 1)
        self.assertIn("woow_chatgpt.py：lock blob", out)

    def test_lock_at_known_commit_is_only_behind(self):
        # the manifest moved the shared files on; HA's lock waits for the sync PR without blocking HA
        tmp = self.tmpdir()
        repo, _, m = litellm_repo(tmp)
        sf = m["products"]["litellm"]["shared_files"][0]
        sf["known_commits"] = [{"commit": sf["source"]["commit"], "blobs": dict(sf["source"]["blobs"])}]
        sf["source"]["commit"] = "a" * 40
        sf["source"]["blobs"] = {n: "b" * 40 for n in sf["source"]["files"]}
        rc, out = run("check", "--leg", "ha", "--repo-dir", repo, "--repo", "github:WOOWTECH/Woow_ha_litellm",
                      manifest_path=write_manifest(tmp, m))
        self.assertEqual(rc, 0, out)
        self.assertIn("等同步 PR", out)

    def test_missing_lock_fails_closed(self):
        repo, mpath, _ = litellm_repo(self.tmpdir())
        os.remove(os.path.join(repo, "litellm", "woow-plugin.lock.json"))
        rc, out = run("check", "--leg", "ha", "--repo-dir", repo, "--repo", "github:WOOWTECH/Woow_ha_litellm",
                      manifest_path=mpath)
        self.assertEqual(rc, 1)
        self.assertIn("找不到 lock", out)

    def test_source_side_change_only_warns(self):
        tmp = self.tmpdir()
        r = os.path.join(tmp, "charts")
        shutil.copytree(os.path.join(FIX, "charts"), r)
        files = os.path.join(r, "charts", "litellm", "files")
        os.makedirs(files)
        contents = {"sitecustomize.py": b"# a\n", "woow_chatgpt.py": b"# b\n", "woow_chatgpt_plugin.py": b"# c\n"}
        for name, data in contents.items():
            with open(os.path.join(files, name), "wb") as fh:
                fh.write(data)
        m = manifest()
        m["products"]["litellm"]["shared_files"][0]["source"]["blobs"] = {n: wvt.git_blob_sha(d) for n, d in contents.items()}
        mpath = write_manifest(tmp, m)
        argv = ("check", "--leg", "paas", "--repo-dir", r, "--repo", "gitea:woow-paas/woow-paas-charts")
        rc, out = run(*argv, manifest_path=mpath)
        self.assertEqual(rc, 0, out)
        self.assertIn("3 個共用檔與清冊 commit", out)
        with open(os.path.join(files, "woow_chatgpt.py"), "ab") as fh:
            fh.write(b"# PaaS first\n")
        rc, out = run(*argv, manifest_path=mpath)
        self.assertEqual(rc, 0)  # the source may change first
        self.assertIn("領先", out)
        self.assertIn("要開清冊 PR 換 commit", out)

    def test_expired_exception_fails_only_its_product(self):
        repo, mpath, _ = litellm_repo(self.tmpdir())
        rc, out = run("check", "--leg", "ha", "--repo-dir", repo, "--repo", "github:WOOWTECH/Woow_ha_litellm",
                      manifest_path=mpath, today="2026-11-16")
        self.assertEqual(rc, 1)
        self.assertIn("已於 2026-11-15 到期", out)
        rc, _ = run("check", "--leg", "ha", "--repo-dir", os.path.join(FIX, "ha_emqx"),
                    "--repo", "github:WOOWTECH/Woow_ha_emqx", today="2026-11-16")
        self.assertEqual(rc, 0)  # not a manifest error for every other product's CI

    def test_json_and_summary(self):
        tmp = self.tmpdir()
        js, md = os.path.join(tmp, "out.json"), os.path.join(tmp, "summary.md")
        rc, _ = run("check", "--leg", "ha", "--repo-dir", os.path.join(FIX, "ha_n8n"),
                    "--repo", "github:WOOWTECH/Woow_ha_n8n", "--json", js, "--summary", md)
        self.assertEqual(rc, 0)
        with open(js, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual((doc["result"], doc["phase"], doc["enforcement"], doc["fail_on"]), ("pass", 1, "hard-rules", "violation"))
        self.assertEqual(doc["results"][0]["component"], "woow-n8n")
        self.assertEqual(doc["results"][0]["version"], "2.43.6")
        with open(md, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("✅ 通過", text)
        self.assertIn("| n8n | upstream | `n8n/Dockerfile` | 2.43.6 | ✅ 一致 |", text)

    def test_blocking_findings_are_marked_in_json(self):
        r = self.copy_fixture("ha_emqx")
        _sub(os.path.join(r, "emqx", "Dockerfile"), 'EMQX_VERSION="v5.8.9"', 'EMQX_VERSION="v5.10.5"')
        js = os.path.join(self.tmpdir(), "out.json")
        rc, _ = run("check", "--leg", "ha", "--repo-dir", r, "--repo", "github:WOOWTECH/Woow_ha_emqx", "--json", js)
        self.assertEqual(rc, 1)
        with open(js, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["result"], "fail")
        hits = [f for r_ in doc["results"] for f in r_["findings"] if f["blocking"]]
        self.assertEqual([f["rule"] for f in hits], ["license_ceiling"])

    def test_github_annotations(self):
        r = self.copy_fixture("ha_emqx")
        _sub(os.path.join(r, "emqx", "Dockerfile"), 'EMQX_VERSION="v5.8.9"', 'EMQX_VERSION="v5.10.5"')
        old = os.environ.get("GITHUB_ACTIONS")
        os.environ["GITHUB_ACTIONS"] = "true"
        self.addCleanup(lambda: os.environ.pop("GITHUB_ACTIONS") if old is None else os.environ.update(GITHUB_ACTIONS=old))
        rc, out = run("check", "--leg", "ha", "--repo-dir", r, "--repo", "github:WOOWTECH/Woow_ha_emqx",
                      "--summary", os.path.join(self.tmpdir(), "s.md"))
        self.assertEqual(rc, 1)
        self.assertIn("::error file=emqx/Dockerfile::wvt emqx/ha 5.10.5 超過授權上限", out)
        self.assertIn("::warning file=emqx/Dockerfile::", out)  # lockstep ahead stays a warning

    def test_repo_not_in_manifest(self):
        rc, out = run("check", "--leg", "ha", "--repo-dir", FIX, "--repo", "github:WOOWTECH/Woow_ha_other")
        self.assertEqual(rc, 0)
        self.assertIn("沒有任何釘點", out)

    def test_validate_cli(self):
        rc, out = run("validate")
        self.assertEqual(rc, 0)
        self.assertIn("OK", out)


if __name__ == "__main__":
    unittest.main()
