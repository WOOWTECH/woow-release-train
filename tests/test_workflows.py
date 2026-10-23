"""Contract tests for the reusable GitHub workflow the HA repositories call."""

import os
import re
import unittest

from helpers import ROOT, wvt

REUSABLE = os.path.join(ROOT, ".github", "workflows", "wvt-check.yml")


class ReusableWorkflowTest(unittest.TestCase):
    def setUp(self):
        with open(REUSABLE, encoding="utf-8") as fh:
            self.text = fh.read()
        self.wf = wvt.load_yaml(self.text)

    def test_called_only(self):
        self.assertEqual(set(self.wf["on"]), {"workflow_call"})

    def test_read_only(self):
        job = self.wf["jobs"]["wvt"]
        self.assertEqual(job["permissions"], {"contents": "read"})
        self.assertNotIn("secrets.", self.text)
        for step in job["steps"]:
            if "uses" in step:
                self.assertRegex(step["uses"], r"^actions/checkout@[0-9a-f]{40}$")
                self.assertIs(step["with"]["persist-credentials"], False)

    def test_manifest_decides_what_fails(self):
        # no --fail-on / --strict: the manifest's enforcement is the only switch
        run = self.wf["jobs"]["wvt"]["steps"][-1]["run"]
        self.assertIn("release-train/tools/wvt.py check", run)
        self.assertNotRegex(run, r"--fail-on|--strict")
        checkout = self.wf["jobs"]["wvt"]["steps"][1]["with"]
        self.assertEqual((checkout["repository"], checkout["ref"]), ("WOOWTECH/woow-release-train", "main"))

    def test_readme_hooks_call_check_without_fail_on(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        self.assertIn("uses: WOOWTECH/woow-release-train/.github/workflows/wvt-check.yml@main", readme)
        paas = [ln for ln in readme.splitlines() if "wvt.py\" check --leg paas" in ln]
        self.assertTrue(paas)
        self.assertFalse(any(re.search(r"--fail-on|--strict", ln) for ln in paas))


if __name__ == "__main__":
    unittest.main()
