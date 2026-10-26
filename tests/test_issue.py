import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from helpers import wvt

REPO = "woow-paas/woow-release-train"


class FakeGitea:
    def __init__(self, labels=None, issues=None):
        self.labels = labels if labels is not None else [{"id": 7, "name": "version-drift"}]
        self.issues = issues or []
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and "/labels" in path:
            return self.labels
        if method == "POST" and path.endswith("/labels"):
            lab = dict(body, id=99)
            self.labels.append(lab)
            return lab
        if method == "GET" and "/issues?" in path:
            state = "closed" if "state=closed" in path else "open"
            return [dict(i) for i in self.issues if i["state"] == state]
        if method == "POST" and path.endswith("/issues"):
            new = dict(body, number=len(self.issues) + 1, state="open")
            self.issues.append(new)
            return new
        if method == "PATCH":
            num = int(path.rsplit("/", 1)[1])
            issue = next(i for i in self.issues if i["number"] == num)
            issue.update(body)
            return issue
        if method == "POST" and path.endswith("/comments"):
            return {"id": 1}
        raise AssertionError((method, path))


BODY = wvt.TRACKING_MARKER + "\nreport"


class SyncIssueTest(unittest.TestCase):
    def test_creates_issue_on_drift(self):
        g = FakeGitea()
        action, num = wvt.sync_issue(g, REPO, "version-drift", "t", BODY, True)
        self.assertEqual((action, num), ("created", 1))
        self.assertEqual(g.issues[0]["labels"], [7])

    def test_creates_label_if_missing(self):
        g = FakeGitea(labels=[])
        wvt.sync_issue(g, REPO, "version-drift", "t", BODY, True)
        self.assertEqual(g.issues[0]["labels"], [99])

    def test_updates_existing(self):
        g = FakeGitea(issues=[{"number": 5, "state": "open", "body": wvt.TRACKING_MARKER + " old"}])
        action, num = wvt.sync_issue(g, REPO, "version-drift", "t", BODY, True)
        self.assertEqual((action, num), ("updated", 5))
        self.assertEqual(g.issues[0]["body"], BODY)
        self.assertEqual(len(g.issues), 1)

    def test_closes_when_clean(self):
        g = FakeGitea(issues=[{"number": 5, "state": "open", "body": wvt.TRACKING_MARKER}])
        action, num = wvt.sync_issue(g, REPO, "version-drift", "t", BODY, False, "https://run/1")
        self.assertEqual((action, num), ("closed", 5))
        self.assertEqual(g.issues[0]["state"], "closed")
        self.assertTrue(any(c[0] == "POST" and c[1].endswith("/comments") for c in g.calls))

    def test_noop_when_clean_and_nothing_open(self):
        g = FakeGitea()
        self.assertEqual(wvt.sync_issue(g, REPO, "version-drift", "t", BODY, False), ("noop", None))

    def test_drift_reopens_the_tracking_issue(self):
        # 10-26: #4 was closed at 00:41; drift came back at 07:34 (PR #24). The next run must reopen #4
        # with the new report, not open a third issue next to #1 and #4.
        g = FakeGitea(issues=[{"number": 1, "state": "closed", "body": wvt.TRACKING_MARKER + " first"},
                              {"number": 4, "state": "closed", "body": wvt.TRACKING_MARKER + " old"}])
        action, num = wvt.sync_issue(g, REPO, "version-drift", "t", BODY, True, "https://run/9")
        self.assertEqual((action, num), ("reopened", 4))
        four = next(i for i in g.issues if i["number"] == 4)
        self.assertEqual((four["state"], four["body"], four["title"]), ("open", BODY, "t"))
        self.assertEqual(next(i for i in g.issues if i["number"] == 1)["state"], "closed")
        self.assertEqual(len(g.issues), 2)
        comments = [c for c in g.calls if c[0] == "POST" and c[1].endswith("/issues/4/comments")]
        self.assertEqual(len(comments), 1)
        self.assertIn("重新打開", comments[0][2]["body"])
        self.assertIn("https://run/9", comments[0][2]["body"])

    def test_only_a_tracking_issue_is_reopened(self):
        g = FakeGitea(issues=[{"number": 3, "state": "closed", "body": "someone else's issue with the label"}])
        self.assertEqual(wvt.sync_issue(g, REPO, "version-drift", "t", BODY, True), ("created", 2))

    def test_an_open_issue_without_the_marker_is_never_touched(self):
        # review 07-issue-sync-nonmarker-probe: an OPEN label issue without the marker (#7, someone's note)
        # used to become the tracking issue (title／body replaced on drift, overwritten and closed when clean)
        theirs = {"number": 7, "state": "open", "title": "human: 請追 n8n 升版", "body": "someone else's notes (no marker)"}
        for drift, want in ((True, ("reopened", 4)), (False, ("noop", None))):
            g = FakeGitea(issues=[{"number": 4, "state": "closed", "body": wvt.TRACKING_MARKER + " old report"}, dict(theirs)])
            self.assertEqual(wvt.sync_issue(g, REPO, "version-drift", "t", BODY, drift, "https://run/1"), want, drift)
            self.assertEqual(next(i for i in g.issues if i["number"] == 7), theirs, drift)
            self.assertFalse(any("/issues/7" in c[1] for c in g.calls if c[0] != "GET"), (drift, g.calls))
            self.assertEqual(next(i for i in g.issues if i["number"] == 4)["state"], "open" if drift else "closed")
        g = FakeGitea(issues=[dict(theirs)])  # no tracking issue ever: drift opens one next to it
        self.assertEqual(wvt.sync_issue(g, REPO, "version-drift", "t", BODY, True), ("created", 2))
        self.assertEqual(g.issues[0], theirs)

    def test_an_open_issue_is_updated_not_reopened(self):
        g = FakeGitea(issues=[{"number": 4, "state": "open", "body": wvt.TRACKING_MARKER}, {"number": 1, "state": "closed",
                                                                                           "body": wvt.TRACKING_MARKER}])
        self.assertEqual(wvt.sync_issue(g, REPO, "version-drift", "t", BODY, True), ("updated", 4))
        self.assertFalse(any(c[0] == "POST" for c in g.calls))


class IssueCommandTest(unittest.TestCase):
    """`wvt issue` keeps the run green on drift (drift.yml), so the run summary says what it found."""

    def test_summary_names_the_result(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        report, rep, summary = (os.path.join(tmp, n) for n in ("drift.md", "drift.json", "summary.md"))
        with open(report, "w", encoding="utf-8") as fh:
            fh.write(BODY)
        with open(rep, "w", encoding="utf-8") as fh:
            json.dump({"drift": True}, fh)
        g = FakeGitea(issues=[{"number": 4, "state": "closed", "body": wvt.TRACKING_MARKER}])
        orig = wvt.gitea_call
        wvt.gitea_call = lambda api, token, method, path, body=None: g(method, path, body)
        self.addCleanup(setattr, wvt, "gitea_call", orig)
        env = {"GITEA_TOKEN": "t", "GITHUB_STEP_SUMMARY": summary}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()) as out:
            rc = wvt.main(["issue", "--report", report, "--json", rep, "--run-url", "https://run/1"])
        self.assertEqual(rc, 0)
        self.assertIn("tracking issue: reopened #4", out.getvalue())
        with open(summary, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "### 版本漂移：drift=true\n\n追蹤 issue：reopened #4\n")


if __name__ == "__main__":
    unittest.main()
