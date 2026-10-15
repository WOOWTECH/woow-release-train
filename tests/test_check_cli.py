import contextlib
import io
import os
import shutil
import tempfile
import unittest

from helpers import FIX, MANIFEST_PATH, wvt


def run(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = wvt.main(["--manifest", MANIFEST_PATH, "--today", "2026-10-16"] + list(argv))
    return rc, buf.getvalue()


def _sub(path, old, new):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert old in text
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace(old, new))


class CheckCliTest(unittest.TestCase):
    def test_ha_n8n_aligned(self):
        rc, out = run("check", "--leg", "ha", "--repo-dir", os.path.join(FIX, "ha_n8n"),
                      "--repo", "github:WOOWTECH/Woow_ha_n8n")
        self.assertEqual(rc, 0)
        self.assertIn("2.43.6", out)
        self.assertIn("一致", out)

    def test_paas_seed_behind_is_report_only(self):
        rc, out = run("check", "--leg", "paas", "--repo-dir", os.path.join(FIX, "paas_platform"),
                      "--repo", "gitea:odoo-addons/woow_paas_platform")
        self.assertEqual(rc, 0)  # phase 0
        self.assertIn("落後", out)
        self.assertIn("只報告", out)

    def test_strict_fails_on_drift(self):
        rc, _ = run("check", "--leg", "paas", "--repo-dir", os.path.join(FIX, "paas_platform"),
                    "--repo", "gitea:odoo-addons/woow_paas_platform", "--strict")
        self.assertEqual(rc, 1)

    def test_fail_on_violation_ignores_soft_drift(self):
        # n8n behind is not a hard rule -> passes even with --fail-on violation
        rc, _ = run("check", "--leg", "paas", "--repo-dir", os.path.join(FIX, "paas_platform"),
                    "--repo", "gitea:odoo-addons/woow_paas_platform", "--fail-on", "violation")
        self.assertEqual(rc, 0)

    def test_license_breach_fails_with_fail_on_violation(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        shutil.copytree(os.path.join(FIX, "ha_emqx"), os.path.join(tmp, "r"))
        df = os.path.join(tmp, "r", "emqx", "Dockerfile")
        _sub(df, 'EMQX_VERSION="v5.8.9"', 'EMQX_VERSION="v5.10.5"')
        rc, out = run("check", "--leg", "ha", "--repo-dir", os.path.join(tmp, "r"),
                      "--repo", "github:WOOWTECH/Woow_ha_emqx")
        self.assertEqual(rc, 0)
        self.assertIn("授權上限", out)
        rc, _ = run("check", "--leg", "ha", "--repo-dir", os.path.join(tmp, "r"),
                    "--repo", "github:WOOWTECH/Woow_ha_emqx", "--fail-on", "violation")
        self.assertEqual(rc, 1)

    def test_litellm_lock_checked(self):
        rc, out = run("check", "--leg", "ha", "--repo-dir", os.path.join(FIX, "ha_litellm"),
                      "--repo", "github:WOOWTECH/Woow_ha_litellm")
        self.assertEqual(rc, 0)
        self.assertIn("shared_files", out)
        self.assertIn("3 個檔案與清冊一致", out)

    def test_lock_commit_drift_detected(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        shutil.copytree(os.path.join(FIX, "ha_litellm"), os.path.join(tmp, "r"))
        lf = os.path.join(tmp, "r", "litellm", "woow-plugin.lock.json")
        _sub(lf, "4ad95a11d8dad671b342d6cc9454c87a3130e8b6", "0" * 40)
        rc, out = run("check", "--leg", "ha", "--repo-dir", os.path.join(tmp, "r"),
                      "--repo", "github:WOOWTECH/Woow_ha_litellm", "--fail-on", "violation")
        self.assertEqual(rc, 1)
        self.assertIn("lock commit", out)

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
