import datetime as dt
import unittest

from helpers import manifest, wvt

TODAY = dt.date(2026, 10, 16)


def validate(m, today=TODAY):
    return wvt.validate_manifest(m, today)


class RealManifestTest(unittest.TestCase):
    def test_real_manifest_is_valid(self):
        errors, warnings = validate(manifest())
        self.assertEqual(errors, [])

    def test_only_ha_and_paas_legs(self):
        for p in manifest()["products"].values():
            self.assertLessEqual(set(p["legs"]), {"ha", "paas"})

    def test_phase0_report_only(self):
        m = manifest()
        self.assertEqual((m["phase"], m["enforcement"]), (0, "report-only"))

    def test_policies_match_owner_decision(self):
        prods = manifest()["products"]
        self.assertEqual(prods["emqx"]["policy"]["mode"], "lockstep")
        self.assertEqual(prods["litellm"]["policy"]["mode"], "lockstep")
        self.assertEqual(prods["n8n"]["policy"]["mode"], "range")
        self.assertEqual(prods["n8n"]["target"]["version"], "2.43.6")
        self.assertEqual(prods["n8n"]["policy"]["min_version"], "2.38.0")
        self.assertEqual(prods["emqx"]["policy"]["license_ceiling"], "<5.9.0")
        self.assertEqual(prods["litellm"]["exceptions"][0]["expires"], "2026-11-15")


class MutationTest(unittest.TestCase):
    def assertError(self, m, needle, today=TODAY):
        errors, _ = validate(m, today)
        self.assertTrue(any(needle in e for e in errors), "%r not in %r" % (needle, errors))

    def test_podman_leg_rejected(self):
        m = manifest()
        m["products"]["emqx"]["legs"]["podman"] = {"pins": []}
        self.assertError(m, "out of scope")

    def test_bad_schema(self):
        m = manifest()
        m["schema"] = 2
        self.assertError(m, "schema must be 1")

    def test_bad_digest(self):
        m = manifest()
        m["products"]["litellm"]["target"]["index_digest"] = "sha256:abc"
        self.assertError(m, "index_digest is not sha256")

    def test_unquoted_version(self):
        m = manifest()
        m["products"]["emqx"]["target"]["version"] = 5.8
        self.assertError(m, "quoted version")

    def test_target_breaks_license_ceiling(self):
        m = manifest()
        m["products"]["emqx"]["target"]["version"] = "5.9.1"
        self.assertError(m, "breaks license_ceiling")

    def test_range_needs_floor_and_clock(self):
        m = manifest()
        pol = m["products"]["n8n"]["policy"]
        del pol["min_version"], pol["max_days_behind"]
        del m["products"]["n8n"]["target"]["since"]
        errors, _ = validate(m)
        self.assertTrue(any("min_version required" in e for e in errors))
        self.assertTrue(any("max_days_behind" in e for e in errors))
        self.assertTrue(any("since required" in e for e in errors))

    def test_floor_above_target(self):
        m = manifest()
        m["products"]["n8n"]["policy"]["min_version"] = "2.50.0"
        self.assertError(m, "above target")

    def test_duplicate_digest_between_versions(self):
        m = manifest()
        kv = m["products"]["n8n"]["known_versions"][0]
        kv["platforms"]["amd64"] = m["products"]["n8n"]["target"]["platforms"]["amd64"]
        self.assertError(m, "appears in both")

    def test_compare_needs_target_digest(self):
        m = manifest()
        m["products"]["emqx"]["legs"]["paas"]["pins"][0]["compare"] = "platforms.s390x"
        self.assertError(m, "target has no such digest")

    def test_unknown_extractor(self):
        m = manifest()
        m["products"]["n8n"]["legs"]["ha"]["pins"][0]["kind"] = "quadlet_image"
        self.assertError(m, "kind must be one of")

    def test_unknown_source_ref(self):
        m = manifest()
        m["products"]["n8n"]["legs"]["paas"]["pins"][0]["ref"] = "$nope"
        self.assertError(m, "unknown source")

    def test_exception_expired(self):
        self.assertError(manifest(), "expired on 2026-11-15", today=dt.date(2026, 11, 16))

    def test_exception_expiring_soon_warns(self):
        errors, warnings = validate(manifest(), dt.date(2026, 11, 5))
        self.assertEqual(errors, [])
        self.assertTrue(any("expires in 10 days" in w for w in warnings))

    def test_exception_needs_approval(self):
        m = manifest()
        del m["products"]["litellm"]["exceptions"][0]["approved_by"]
        self.assertError(m, "approved_by required")

    def test_shared_files_commit_full_sha(self):
        m = manifest()
        m["products"]["litellm"]["shared_files"][0]["source"]["commit"] = "4ad95a11"
        self.assertError(m, "full 40-hex")

    def test_stale_observation_warns(self):
        _, warnings = validate(manifest(), dt.date(2026, 11, 5))
        self.assertTrue(any("days old" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
