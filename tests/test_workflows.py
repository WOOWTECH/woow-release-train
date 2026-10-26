"""Contract tests for the workflows: the reusable GitHub check the HA repositories call, drift.yml, bots.yml."""

import os
import re
import unittest

from helpers import ROOT, wvt

REUSABLE = os.path.join(ROOT, ".github", "workflows", "wvt-check.yml")
DRIFT = os.path.join(ROOT, ".gitea", "workflows", "drift.yml")
BOTS = os.path.join(ROOT, ".gitea", "workflows", "bots.yml")


def load(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    return text, wvt.load_yaml(text)


class DriftWorkflowTest(unittest.TestCase):
    """10-26: drift.yml ran only on its schedule, so after a manifest merge (#24) the tracking issue stayed
    closed while drift was true; and a run said success whatever it found."""

    def setUp(self):
        self.text, self.wf = load(DRIFT)
        self.job = self.wf["jobs"]["drift"]

    def test_runs_when_the_manifest_reaches_main(self):
        on = self.wf["on"]
        self.assertEqual(set(on), {"schedule", "push", "workflow_dispatch"})
        self.assertEqual(on["push"]["branches"], ["main"])
        self.assertIn("woow-versions.yaml", on["push"]["paths"])
        self.assertIn("tools/wvt.py", on["push"]["paths"])  # a change to the comparison itself refreshes the issue too
        self.assertEqual(on["schedule"], [{"cron": "41 0 * * *"}])
        self.assertEqual(self.wf["concurrency"], {"group": "version-drift", "cancel-in-progress": False})

    def test_drift_reopens_the_issue_instead_of_failing_the_job(self):
        # the choice (see the header): drift=true updates／reopens the one tracking issue; the job fails only
        # when the tool breaks (manifest invalid, Gitea API errors)
        collect, sync = [s for s in self.job["steps"] if "run" in s]
        self.assertIn("tools/wvt.py drift --out drift.md --json drift.json", collect["run"])
        self.assertNotRegex(collect["run"], r"--fail-on|\|\|\s*true|continue-on-error")
        self.assertNotIn("continue-on-error", self.text)
        self.assertIn("tools/wvt.py issue --report drift.md --json drift.json", sync["run"])
        self.assertNotIn("if", sync)  # the issue is synced on every run, drift or not
        self.assertIn("重新打開並更新追蹤 issue", self.text)

    def test_tokens(self):
        self.assertEqual(self.job["permissions"], {"contents": "read", "issues": "write"})
        collect, sync = [s for s in self.job["steps"] if "run" in s]
        self.assertEqual(collect["env"]["GITEA_READ_TOKEN"], "${{ secrets.CI_GITEA_TOKEN }}")
        self.assertNotIn("GITEA_TOKEN", collect["env"])
        self.assertEqual(sync["env"]["GITEA_TOKEN"], "${{ secrets.GITEA_TOKEN }}")


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
