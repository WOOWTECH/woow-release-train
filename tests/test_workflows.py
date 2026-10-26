"""Contract tests for the workflows: the reusable GitHub check the HA repositories call, bots.yml."""

import os
import re
import unittest

from helpers import ROOT, wvt

REUSABLE = os.path.join(ROOT, ".github", "workflows", "wvt-check.yml")
BOTS = os.path.join(ROOT, ".gitea", "workflows", "bots.yml")


def load(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    return text, wvt.load_yaml(text)


class BotsWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.text, self.wf = load(BOTS)
        self.step = self.wf["jobs"]["bots"]["steps"][-1]

    def test_downgrade_is_an_explicit_input(self):
        inputs = self.wf["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["allow_downgrade"]["default"], "false")
        self.assertEqual(self.step["env"]["ALLOW_DOWNGRADE"], "${{ inputs.allow_downgrade }}")
        run = self.step["run"]
        # the flag is added only for the literal "true"; the inputs never reach the shell unquoted
        self.assertIn('if [ "${ALLOW_DOWNGRADE}" = "true" ]; then set -- "$@" --allow-downgrade; fi', run)
        self.assertEqual(run.count("--allow-downgrade"), 1)
        self.assertIn('python3 tools/wvtbot.py "$@"', run)
        self.assertNotIn("${{", run)

    def test_triggers_unchanged(self):
        self.assertEqual(self.wf["on"]["push"], {"branches": ["main"], "paths": ["woow-versions.yaml"]})


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
