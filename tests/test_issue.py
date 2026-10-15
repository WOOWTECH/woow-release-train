import unittest

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
            return [i for i in self.issues if i["state"] == "open"]
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


if __name__ == "__main__":
    unittest.main()
