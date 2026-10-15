import datetime as dt
import unittest

from helpers import manifest, wvt

TODAY = dt.date(2026, 10, 16)
N8N = {
    "2.43.6": "sha256:371dd463f02e8c35a4bc5aaf9855fc4cfec1babab97e5a02a1e31923a2ec8f5a",
    "2.38.0": "sha256:e0e7b603b43adc48d7123716b9eee7394dca675b410f574e4d24830e114564ce",
    "2.35.3": "sha256:a3693cff4b9fff5ffce6d20470f2d87a905c073c0de46ea65404e89947c6dfa2",
    "2.35.3-index": "sha256:31375a4f730081a139bba2d379bf2efaa8e662cc9e131d12bb5e15859d4f0282",
}
EMQX_AMD64 = "sha256:8b92a101d1a1642a191507b9133c46aad095bb64300dba1770108a2c7261b11a"
LITELLM_INDEX = "sha256:37066242e6b8327fdd627682a66a9ae2a599553b4d5a84c905c6d8b9eb35c01e"


def ev(prod, leg, pin, obs, today=TODAY):
    return wvt.evaluate(prod, leg, pin, obs, today)


def statuses(res):
    return [f["status"] for f in res["findings"]]


def rules(res):
    return [f["rule"] for f in res["findings"]]


class VersionHelpersTest(unittest.TestCase):
    def test_parse_and_compare(self):
        self.assertEqual(wvt.parse_version("v5.8.9"), (5, 8, 9, 0))
        self.assertEqual(wvt.cmp_versions("2.38.0", "2.43.6"), -1)
        self.assertEqual(wvt.cmp_versions("1.104.0", "1.104.0"), 0)
        self.assertEqual(wvt.cmp_versions("5.10.5", "5.9.0"), 1)
        self.assertIsNone(wvt.cmp_versions("latest", "1.0"))

    def test_satisfies(self):
        self.assertTrue(wvt.satisfies("5.8.9", "<5.9.0"))
        self.assertFalse(wvt.satisfies("5.9.0", "<5.9.0"))
        self.assertFalse(wvt.satisfies("5.10.5", "<5.9.0"))
        self.assertTrue(wvt.satisfies("2.40.0", ">=2.38.0,<3"))
        with self.assertRaises(wvt.WvtError):
            wvt.satisfies("1.0", "~1.0")


class LockstepTest(unittest.TestCase):
    def setUp(self):
        self.m = manifest()
        self.emqx = self.m["products"]["emqx"]
        self.litellm = self.m["products"]["litellm"]

    def test_version_equal_ok(self):
        res = ev(self.emqx, "ha", {"compare": "version"}, {"raw": "5.8.9", "version": "5.8.9"})
        self.assertEqual(res["status"], "ok")

    def test_version_behind(self):
        res = ev(self.emqx, "ha", {"compare": "version"}, {"raw": "5.8.8", "version": "5.8.8"})
        self.assertEqual(res["status"], "behind")
        self.assertIn("lockstep", rules(res))

    def test_license_ceiling_violation_uses_upstream_version(self):
        res = ev(self.emqx, "ha", {"compare": "version"}, {"raw": "5.10.5", "version": "5.10.5"})
        self.assertEqual(res["status"], "violation")
        self.assertIn("license_ceiling", rules(res))
        self.assertTrue(wvt.is_hard(self.emqx, res["findings"][0]))

    def test_package_version_is_never_policy_checked(self):
        # the HA add-on label 5.9.1 is a package version (compare: info), not upstream
        res = ev(self.emqx, "ha", {"compare": "info"}, {"raw": "5.9.1", "version": "5.9.1"})
        self.assertEqual(res["status"], "info")

    def test_platform_digest_ok(self):
        res = ev(self.emqx, "paas", {"compare": "platforms.amd64"}, {"raw": "x", "digest": EMQX_AMD64})
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["version"], "5.8.9")

    def test_unknown_digest_is_mismatch(self):
        res = ev(self.litellm, "paas", {"compare": "index_digest"}, {"raw": "x", "digest": "sha256:" + "0" * 64})
        self.assertEqual(res["status"], "mismatch")
        self.assertTrue(wvt.is_hard(self.litellm, res["findings"][0]))

    def test_tag_and_digest_disagree(self):
        res = ev(self.litellm, "ha", {"compare": "index_digest"}, {"raw": "x", "version": "1.103.3", "digest": LITELLM_INDEX})
        # digest is the target; the tag disagrees -> the pin itself is inconsistent
        self.assertEqual(res["status"], "mismatch")
        self.assertEqual(res["version"], "1.104.0")

    def test_digest_without_version_takes_target(self):
        res = ev(self.litellm, "paas", {"compare": "index_digest"}, {"raw": "x", "digest": LITELLM_INDEX})
        self.assertEqual((res["status"], res["version"]), ("ok", "1.104.0"))

    def test_missing_digest_unknown(self):
        res = ev(self.litellm, "paas", {"compare": "index_digest"}, {"raw": "x", "version": "1.104.0"})
        self.assertEqual(res["status"], "unknown")

    def test_nothing_extracted(self):
        res = ev(self.litellm, "paas", {"compare": "version"}, {"raw": None})
        self.assertEqual(res["status"], "unknown")


class RangeTest(unittest.TestCase):
    def setUp(self):
        self.n8n = manifest()["products"]["n8n"]
        self.pin = {"compare": "platforms.amd64"}

    def test_target_ok(self):
        res = ev(self.n8n, "paas", self.pin, {"raw": "x", "digest": N8N["2.43.6"]})
        self.assertEqual(res["status"], "ok")

    def test_behind_within_window(self):
        res = ev(self.n8n, "paas", self.pin, {"raw": "x", "digest": N8N["2.38.0"]})
        self.assertEqual(res["status"], "behind")
        self.assertEqual(res["version"], "2.38.0")
        self.assertIn("已 1 天", res["findings"][-1]["message"])

    def test_behind_overdue(self):
        res = ev(self.n8n, "paas", self.pin, {"raw": "x", "digest": N8N["2.38.0"]}, today=dt.date(2026, 11, 20))
        self.assertIn("max_days_behind", rules(res))
        self.assertEqual(res["status"], "violation")
        self.assertFalse(wvt.is_hard(self.n8n, res["findings"][0]))  # range products stay advisory

    def test_below_min_version(self):
        res = ev(self.n8n, "paas", self.pin, {"raw": "x", "digest": N8N["2.35.3"]})
        self.assertIn("min_version", rules(res))
        self.assertEqual(res["status"], "violation")

    def test_digest_form_note(self):
        res = ev(self.n8n, "paas", self.pin, {"raw": "x", "digest": N8N["2.35.3-index"]})
        self.assertIn("digest_form", rules(res))
        self.assertEqual(res["version"], "2.35.3")

    def test_ahead(self):
        res = ev(self.n8n, "ha", {"compare": "version"}, {"raw": "2.44.0", "version": "2.44.0"})
        self.assertEqual(res["status"], "ahead")

    def test_unparseable_version(self):
        res = ev(self.n8n, "ha", {"compare": "version"}, {"raw": "latest", "version": "latest"})
        self.assertEqual(res["status"], "unknown")


class ExceptionTest(unittest.TestCase):
    def setUp(self):
        self.p = manifest()["products"]["n8n"]
        self.p["exceptions"] = [{"leg": "paas", "what": "wait for /approve", "reason": "r", "approved_by": "x",
                                 "expires": "2026-11-01", "waives": ["behind"]}]
        self.pin = {"compare": "platforms.amd64"}
        self.obs = {"raw": "x", "digest": N8N["2.38.0"]}

    def test_waived_while_active(self):
        res = ev(self.p, "paas", self.pin, self.obs)
        self.assertEqual(res["status"], "excepted")

    def test_other_leg_not_waived(self):
        res = ev(self.p, "ha", {"compare": "version"}, {"raw": "2.38.0", "version": "2.38.0"})
        self.assertEqual(res["status"], "behind")

    def test_expired_becomes_violation(self):
        res = ev(self.p, "paas", self.pin, self.obs, today=dt.date(2026, 11, 2))
        self.assertIn("exception_expired", rules(res))
        self.assertEqual(res["status"], "violation")


if __name__ == "__main__":
    unittest.main()
