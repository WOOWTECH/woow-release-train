import datetime as dt
import unittest

from helpers import LIVE_MANIFEST_PATH, manifest, wvt

TODAY = dt.date(2026, 10, 16)


def validate(m, today=TODAY):
    return wvt.validate_manifest(m, today)


class LiveManifestTest(unittest.TestCase):
    """The live woow-versions.yaml changes through bot and owner PRs: only its validity is tested."""

    def test_live_manifest_is_valid(self):
        errors, _ = wvt.validate_manifest(wvt.load_manifest(LIVE_MANIFEST_PATH), dt.date.today(), expiry=False)
        self.assertEqual(errors, [])

    def test_live_manifest_keeps_the_owner_decisions(self):
        m = wvt.load_manifest(LIVE_MANIFEST_PATH)
        self.assertEqual(set(m["products"]) >= {"emqx", "litellm", "n8n"}, True)
        self.assertEqual(m["products"]["emqx"]["policy"]["license_ceiling"], "<5.9.0")
        self.assertIn(m["enforcement"], wvt.ENFORCEMENT)


class RealManifestTest(unittest.TestCase):
    """The frozen snapshot (tests/fixtures/woow-versions.yaml) the behaviour tests run on."""

    def test_real_manifest_is_valid(self):
        errors, warnings = validate(manifest())
        self.assertEqual(errors, [])

    def test_only_ha_and_paas_legs(self):
        for p in manifest()["products"].values():
            self.assertLessEqual(set(p["legs"]), {"ha", "paas"})

    def test_phase1_hard_rules(self):
        m = manifest()
        self.assertEqual((m["phase"], m["enforcement"]), (1, "hard-rules"))
        # phase 1 opens exactly the two hard rules of the owner decision (README); n8n stays advisory
        prods = m["products"]
        self.assertEqual(prods["emqx"]["policy"]["hard_rules"], ["license_ceiling", "exception_expired"])
        self.assertEqual(prods["litellm"]["policy"]["hard_rules"], ["digest", "shared_files", "exception_expired"])
        self.assertEqual(prods["n8n"]["policy"]["hard_rules"], [])

    def test_shared_file_blobs_recorded(self):
        src = manifest()["products"]["litellm"]["shared_files"][0]["source"]
        self.assertEqual(set(src["blobs"]), set(src["files"]))

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

    def test_enforcement_values(self):
        m = manifest()
        m["enforcement"] = "block-all"
        self.assertError(m, "enforcement must be one of")
        m = manifest()
        m["phase"] = 0
        self.assertError(m, "phase 0 is report-only")
        m = manifest()
        m["enforcement"] = "report-only"
        errors, warnings = validate(m)
        self.assertEqual(errors, [])
        self.assertTrue(any("no CI gate fails" in w for w in warnings))

    def test_shared_blobs_must_cover_files(self):
        m = manifest()
        del m["products"]["litellm"]["shared_files"][0]["source"]["blobs"]["woow_chatgpt.py"]
        self.assertError(m, "blobs must list exactly the shared files")
        m = manifest()
        m["products"]["litellm"]["shared_files"][0]["source"]["blobs"]["woow_chatgpt.py"] = "527c16c6"
        self.assertError(m, "40-hex git blob")

    def test_known_commit_cannot_repeat_current(self):
        m = manifest()
        sf = m["products"]["litellm"]["shared_files"][0]
        sf["known_commits"] = [{"commit": sf["source"]["commit"]}]
        self.assertError(m, "duplicates the current commit")

    def test_check_does_not_fail_manifest_on_expiry(self):
        errors, _ = wvt.validate_manifest(manifest(), dt.date(2026, 11, 16), expiry=False)
        self.assertEqual(errors, [])

    def test_stale_observation_warns(self):
        _, warnings = validate(manifest(), dt.date(2026, 11, 5))
        self.assertTrue(any("days old" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
