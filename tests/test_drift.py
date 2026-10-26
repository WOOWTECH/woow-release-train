import datetime as dt
import socket
import unittest
import urllib.request

from helpers import FakeFetcher, LITELLM_MAIN_BLOBS, NetworkBlocked, fixture, manifest, wvt

TODAY = dt.date(2026, 10, 16)
ORIGINAL = ("emqx", "litellm", "n8n")


def rows(rep, product, leg=None, pin=None):
    return [r for r in rep["rows"] if r["product"] == product and (leg is None or r["leg"] == leg)
            and (pin is None or r["pin"] == pin)]


class DriftTest(unittest.TestCase):
    def setUp(self):
        self.m = manifest()

    def collect(self, **kw):
        return wvt.collect_drift(self.m, FakeFetcher(**kw), TODAY)

    def test_expected_phase0_picture(self):
        rep = self.collect()
        self.assertEqual(rep["context"]["prod_platform"], "v18.0.1.0.242")
        # n8n: PaaS seed behind (2.38.0) at both the production tag and develop; HA aligned
        seed = rows(rep, "n8n", "paas", "seed")
        self.assertEqual({r["ref"] for r in seed}, {"v18.0.1.0.242", "develop"})
        self.assertTrue(all(r["status"] == "behind" and r["version"] == "2.38.0" for r in seed))
        ha = [r for r in rows(rep, "n8n", "ha") if r["compare"] != "info"]
        self.assertTrue(ha and all(r["status"] == "ok" for r in ha))
        # HA published (Store catalog) is read at the catalog's source_sha
        self.assertTrue(any("Store 已發佈 2.43.6" in r["point"] for r in ha))
        # EMQX aligned; the HA package label 5.9.1 is reported as info only
        emqx = [r for r in rows(rep, "emqx") if r["compare"] != "info"]
        self.assertTrue(all(r["status"] == "ok" for r in emqx))
        pkg = [r for r in rows(rep, "emqx", "ha", "package_version")]
        self.assertEqual(pkg[0]["version"], "5.9.1")
        self.assertEqual(pkg[0]["status"], "info")
        # LiteLLM aligned (base digest + shared files), exception listed
        lite = [r for r in rows(rep, "litellm") if r["compare"] != "info"]
        self.assertTrue(all(r["status"] == "ok" for r in lite), [(r["pin"], r["status"]) for r in lite])
        self.assertEqual(rep["exceptions"][0]["days_left"], 30)
        self.assertTrue(rep["drift"])

    def test_markdown_render(self):
        rep = self.collect()
        md = wvt.render_markdown(self.m, rep)
        self.assertIn(wvt.TRACKING_MARKER, md)
        self.assertIn("| n8n | range（下限 2.38.0、最多落後 30 天） | 2.43.6 |", md)
        self.assertIn("🟠 落後 2.38.0", md)
        self.assertIn("ChatGPT Responses", md)
        self.assertIn("v18.0.1.0.242", md)
        self.assertIn("已部署（手動快照", md)
        self.assertIn("woow_revoke_ui_sessions.py", md)

    def test_no_drift_when_paas_catches_up(self):
        seed = fixture("paas_platform", "src", "data", "cloud_app_templates.xml")
        seed = seed.replace("20261013.2012@sha256:e0e7b603b43adc48d7123716b9eee7394dca675b410f574e4d24830e114564ce",
                            "20261015.2140@sha256:371dd463f02e8c35a4bc5aaf9855fc4cfec1babab97e5a02a1e31923a2ec8f5a")
        repo, path = "gitea:odoo-addons/woow_paas_platform", "src/data/cloud_app_templates.xml"
        rep = self.collect(overrides={(repo, path, "v18.0.1.0.242"): seed, (repo, path, "develop"): seed})
        # the phase-3 products have no fixtures here: judge the three original products
        dirty = [(r["product"], r["pin"], r["status"]) for r in rep["rows"]
                 if r["product"] in ORIGINAL and r["status"] in wvt.DRIFT_STATUSES]
        self.assertEqual(dirty, [])

    def test_github_unreachable_is_reported_not_crashing(self):
        rep = self.collect(unreachable={"github"})
        ha = [r for r in rep["rows"] if r["leg"] == "ha"]
        self.assertTrue(ha and all(r["status"] == "unreachable" for r in ha))
        self.assertTrue(rep["drift"])
        self.assertIn("simulated", rep["probes"]["raw.githubusercontent.com"]["errors"][0])
        # PaaS rows still evaluated
        self.assertTrue(any(r["status"] == "behind" for r in rows(rep, "n8n", "paas")))

    def test_cd_unreadable_falls_back_to_develop(self):
        rep = self.collect(overrides={("gitea:woow-paas/paas-app-bundle-cd", "paas-odoo-platform/values-prod.yaml", "main"):
                                      wvt.WvtError("gitea: HTTP 403")})
        self.assertIsNone(rep["context"]["prod_platform"])
        self.assertTrue(any("讀不到" in n for n in rep["notes"]))
        seed = rows(rep, "n8n", "paas", "seed")
        self.assertEqual({r["status"] for r in seed}, {"unreachable", "behind"})

    def test_shared_file_changed_on_main(self):
        # PaaS changed a shared file first: the source is ahead of the manifest, HA's lock still matches it
        blobs = dict(LITELLM_MAIN_BLOBS, **{"woow_chatgpt.py": "f" * 40})
        rep = self.collect(blobs=blobs)
        src = rows(rep, "litellm", "paas", "shared_source")[0]
        self.assertEqual(src["status"], "ahead")
        self.assertTrue(any("woow_chatgpt.py" in f["message"] for f in src["findings"]))
        self.assertEqual(rows(rep, "litellm", "ha", "shared_files")[0]["status"], "ok")
        self.assertTrue(rep["drift"])

    def test_lock_at_known_commit_is_behind(self):
        sf = self.m["products"]["litellm"]["shared_files"][0]
        sf["known_commits"] = [{"commit": sf["source"]["commit"], "blobs": dict(sf["source"]["blobs"])}]
        sf["source"]["commit"] = "a" * 40
        rep = self.collect()
        self.assertEqual(rows(rep, "litellm", "ha", "shared_files")[0]["status"], "behind")

    def test_connectivity_probe_does_not_change_drift(self):
        class ProbeFail(FakeFetcher):
            def _get(self, url, headers=None, endpoint=None):
                self._probe(endpoint, "URLError: simulated")
                raise wvt.WvtError("simulated")
        self.m["probes"] = {"api.github.com": "https://api.github.com/rate_limit"}
        base = self.collect()
        rep = wvt.collect_drift(self.m, ProbeFail(), TODAY)
        self.assertEqual(rep["probes"]["api.github.com"]["errors"], ["URLError: simulated"])
        self.assertEqual([r["status"] for r in rep["rows"]], [r["status"] for r in base["rows"]])

    def test_expired_exception_marks_drift(self):
        rep = wvt.collect_drift(self.m, FakeFetcher(), dt.date(2026, 11, 20))
        self.assertTrue(any(e["days_left"] < 0 for e in rep["exceptions"]))
        md = wvt.render_markdown(self.m, rep)
        self.assertIn("**已過期**", md)


class HermeticTest(unittest.TestCase):
    """The tests never reach the network (tests/helpers.py). 10-26 review: every collect_drift opened a real
    connection for the manifest's api.github.com probe (44 per run) — green offline, but up to the 20 s
    Fetcher timeout each where a runner's egress is dropped instead of refused."""

    def test_a_real_request_errors_the_test(self):
        with self.assertRaises(NetworkBlocked):
            urllib.request.urlopen("https://api.github.com/rate_limit", timeout=1)
        with self.assertRaises(NetworkBlocked):  # not swallowed into a WvtError by Fetcher._get
            wvt.Fetcher()._get("https://api.github.com/rate_limit")

    def test_loopback_still_works(self):
        srv = socket.socket()
        self.addCleanup(srv.close)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        socket.create_connection(srv.getsockname(), timeout=1).close()

    def test_the_connectivity_probe_is_served_by_the_fake(self):
        f = FakeFetcher()
        rep = wvt.collect_drift(manifest(), f, TODAY)
        self.assertIn(("get", "https://api.github.com/rate_limit"), f.calls)
        self.assertEqual(rep["probes"]["api.github.com"], {"ok": 1, "errors": []})
        down = wvt.collect_drift(manifest(), FakeFetcher(unreachable={"github"}), TODAY)
        self.assertEqual(down["probes"]["api.github.com"]["errors"], ["URLError: simulated"])


if __name__ == "__main__":
    unittest.main()
