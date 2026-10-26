"""Phase 3 reads (Hermes, OpenDesign, MCP): FROM, regex, PEP 508, uv.lock, JSON records, file sha256,
identity compares — on snippets shaped like the real files (research 2026-10-24)."""

import datetime as dt
import unittest

from helpers import manifest, wvt

import wvtbot  # noqa: E402

TODAY = dt.date(2026, 10, 24)
H = "9c5561579f3ffdf333401797094ac3a743738d6c25d40f4331c7bf77b694238d"
OD = "c5496453136b5ddc1ec3376c0f7518bfb2ba5091ff1ba75bfadf645d1c52a683"


class FromTest(unittest.TestCase):
    def test_digest_only_from(self):
        text = "# Hermes 0.22.5\nFROM nousresearch/hermes-agent@sha256:%s\nUSER root\n" % H
        ref = wvt.extract_dockerfile_from(text)
        self.assertEqual(wvt.parse_image_ref(ref), {"repository": "nousresearch/hermes-agent", "tag": None, "digest": "sha256:" + H})

    def test_arg_substitution_and_stages(self):
        text = ("ARG BUILD_FROM=ghcr.io/woowtech/open-design-core:20261015.0617@sha256:%s\n"
                "FROM ${BUILD_FROM} AS woow-mcp-ui\nRUN true\nFROM ${BUILD_FROM}\n" % OD)
        want = "ghcr.io/woowtech/open-design-core:20261015.0617@sha256:" + OD
        self.assertEqual(wvt.extract_dockerfile_from(text), want)
        self.assertEqual(wvt.extract_dockerfile_from(text, "woow-mcp-ui"), want)
        multi = "FROM node:20-alpine AS frontend-builder\nFROM python:3.12-slim\n"
        self.assertEqual(wvt.extract_dockerfile_from(multi, 0), "node:20-alpine")
        self.assertEqual(wvt.extract_dockerfile_from(multi), "python:3.12-slim")
        self.assertIsNone(wvt.extract_dockerfile_from(multi, 5))

    def test_writer(self):
        text = "# base\nFROM nousresearch/hermes-agent@sha256:%s AS base\nRUN x\n" % H
        out = wvtbot.write_dockerfile_from(text, None, "nousresearch/hermes-agent@sha256:" + "1" * 64)
        self.assertEqual(out, "# base\nFROM nousresearch/hermes-agent@sha256:%s AS base\nRUN x\n" % ("1" * 64))
        with self.assertRaises(wvt.WvtError):
            wvtbot.write_dockerfile_from("ARG B=x\nFROM ${B}\n", None, "y")


class RegexTest(unittest.TestCase):
    def test_anchored_values(self):
        env = ("OD_SOURCE_TAG=woow-od-v0.24.1-w2.2\nOD_SOURCE_SHA=1c7e697ef8a3d0aadd7b736b7110a3df83cf6ff5\n"
               "OD_UPSTREAM_SHA=89e64d813bb1c7a11519b3f668f011f7017637d7\nOD_UPSTREAM_VERSION=0.24.1\n")
        self.assertEqual(wvt.extract_regex(env, r"^OD_SOURCE_SHA=([0-9a-f]{40})\s*$"), "1c7e697ef8a3d0aadd7b736b7110a3df83cf6ff5")
        self.assertEqual(wvt.extract_regex(env, r"^OD_UPSTREAM_VERSION=(\S+)\s*$"), "0.24.1")
        self.assertIsNone(wvt.extract_regex(env, r"^NOPE=(\S+)$"))
        with self.assertRaises(wvt.WvtError):
            wvt.extract_regex('VERSION = "1"\nVERSION = "2"\n', r'^VERSION = "([^"]+)"')
        self.assertEqual(wvt.extract_regex('VERSION = "2.92.1"\n# VERSION = "x"\n', r'^VERSION = "([^"]+)"'), "2.92.1")

    def test_writer(self):
        text = 'A = 1\nVERSION = "2.92.1"\n'
        self.assertEqual(wvtbot.write_regex(text, r'^VERSION = "([^"]+)"', "2.93.0"), 'A = 1\nVERSION = "2.93.0"\n')
        with self.assertRaises(wvt.WvtError):
            wvtbot.write_regex(text, r"^NOPE=(.*)$", "x")


class PythonPinsTest(unittest.TestCase):
    def test_pep508(self):
        ha = 'dependencies = ["odoo-mcp==1.1.0", "httpx==0.28.1", "mcp==1.28.1"]\n'
        self.assertEqual(wvt.extract_pep508(ha, "odoo-mcp"), "1.1.0")
        self.assertEqual(wvt.extract_pep508(ha, "mcp"), "1.28.1")  # never the tail of odoo-mcp
        self.assertEqual(wvt.extract_pep508(ha, "odoo_mcp"), "1.1.0")  # - _ . are the same name
        self.assertIsNone(wvt.extract_pep508('deps = [\n  "mcp[cli]",\n  "mcp>=1.30.0",\n]\n', "mcp"))
        with self.assertRaises(wvt.WvtError):
            wvt.extract_pep508('a = ["mcp==1.1"]\nb = ["mcp==1.2"]\n', "mcp")

    def test_uv_lock(self):
        lock = ('version = 1\n\n[[package]]\nname = "mcp"\nversion = "1.30.0"\nsource = { registry = "https://pypi.org/simple" }\n\n'
                '[[package]]\nname = "odoo-mcp"\nversion = "1.1.0"\nsource = { registry = "https://pypi.org/simple" }\n'
                'sdist = { url = "https://files/odoo_mcp-1.1.0.tar.gz", hash = "sha256:%s", size = 1 }\n' % ("3" * 64))
        self.assertEqual(wvt.extract_uv_lock(lock, "mcp"), "1.30.0")
        self.assertEqual(wvt.extract_uv_lock(lock, "odoo_mcp"), "1.1.0")
        self.assertEqual(wvt.extract_uv_lock(lock, "odoo-mcp", "sdist_sha256"), "sha256:" + "3" * 64)
        self.assertIsNone(wvt.extract_uv_lock(lock, "httpx"))
        with self.assertRaises(wvt.WvtError):
            wvt.extract_uv_lock(lock + '\n[[package]]\nname = "mcp"\nversion = "1.28.1"\n', "mcp")


class JsonAndFileTest(unittest.TestCase):
    SOURCES = ('[{"product": "litellm", "source": "WOOWTECH/Woow_litellm_mcp_server/4d4190369216a2d068d1100d53406a67a1d81609",'
               ' "path": "woow_litellm_mcp_server/server.py", "upstream_sha256": "%s"},'
               ' {"product": "hermes", "source": "local:614ae663", "path": "hermes_mcp_server/server.py",'
               ' "upstream_sha256": "%s"}]' % ("85c3cb8f" + "0" * 56, "e1c3f7be" + "0" * 56))

    def test_json_select(self):
        self.assertEqual(wvt.extract_json_select(self.SOURCES, {"product": "litellm"}, "source", r"([0-9a-f]{40})$"),
                         "4d4190369216a2d068d1100d53406a67a1d81609")
        self.assertEqual(wvt.extract_json_select(self.SOURCES, {"product": "hermes", "path": "hermes_mcp_server/server.py"}, "upstream_sha256"),
                         "e1c3f7be" + "0" * 56)
        self.assertIsNone(wvt.extract_json_select(self.SOURCES, {"product": "emqx"}, "source"))
        self.assertIsNone(wvt.extract_json_select('{"a": 1}', {"product": "x"}, "source"))

    def test_file_sha256(self):
        self.assertEqual(wvt.extract_file_sha256(b"a\r\nb\n"), "sha256:" + __import__("hashlib").sha256(b"a\r\nb\n").hexdigest())
        self.assertEqual(wvt.extract_file_sha256("x"), wvt.extract_file_sha256(b"x"))


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.od = manifest()["products"]["opendesign"]

    def ev(self, pin, value):
        return wvt.evaluate(self.od, "paas", pin, wvt.observe(pin, value), TODAY)

    def test_equal_prefix_and_mismatch(self):
        fork = self.od["target"]["identity"]["fork_commit"]
        pin = {"compare": "identity.fork_commit", "kind": "regex"}
        self.assertEqual(self.ev(pin, fork)["status"], "ok")
        self.assertEqual(self.ev(dict(pin, match="prefix"), fork[:7])["status"], "ok")
        self.assertEqual(self.ev(pin, fork[:7])["status"], "mismatch")  # no prefix match unless asked
        res = self.ev(pin, "f" * 40)
        self.assertEqual(res["status"], "mismatch")
        self.assertIn("identity.fork_commit", res["findings"][0]["message"])

    def test_upstream_version_identity_orders(self):
        pin = {"compare": "identity.upstream_version", "kind": "yaml"}
        self.assertEqual(self.ev(pin, "0.24.1")["status"], "ok")
        self.assertEqual(self.ev(pin, "0.23.1")["status"], "behind")
        self.assertEqual(self.ev(pin, "0.26.1")["status"], "ahead")

    def test_shell_tag_as_version(self):
        pin = {"compare": "version", "kind": "seed_image", "tag_as_version": True}
        obs = wvt.observe(pin, {"repository": "jcr", "tag": "20261018.1219", "digest": "sha256:" + "5" * 64, "raw": "x"})
        self.assertEqual(obs["version"], "20261018.1219")
        self.assertEqual(wvt.evaluate(self.od, "paas", pin, obs, TODAY)["status"], "ok")

    def test_build_id_is_a_version_not_a_jcr_tag_outside_jcr(self):
        old = "ghcr.io/woowtech/open-design-core:20261015.0617@sha256:" + OD
        new = wvtbot.new_image_ref(old, "20261018.1219", "sha256:" + "6" * 64)
        self.assertEqual(new, "ghcr.io/woowtech/open-design-core:20261018.1219@sha256:" + "6" * 64)
        self.assertFalse(wvtbot.needs_mirror({"kind": "dockerfile_arg", "parse": "image_ref"}, {"raw": old}))


class Phase3ManifestTest(unittest.TestCase):
    def test_products_and_policies(self):
        prods = manifest()["products"]
        self.assertEqual(sorted(prods), sorted(["emqx", "litellm", "n8n", "hermes", "opendesign", "mcp-n8n", "mcp-odoo",
                                                "mcp-litellm", "mcp-hermes", "mcp-emqx", "mcp-nextcloud", "mcp-python-sdk",
                                                "woow-mcp-server"]))
        for name in ("hermes", "opendesign", "mcp-n8n", "mcp-odoo", "mcp-litellm", "mcp-hermes", "mcp-emqx", "mcp-nextcloud", "mcp-python-sdk",
                     "woow-mcp-server"):
            self.assertEqual(prods[name]["policy"]["hard_rules"], [], name)  # phase 3 observes first
        self.assertIs(prods["woow-mcp-server"]["legs"]["paas"]["fanout"]["enabled"], False)
        self.assertEqual(prods["hermes"]["policy"]["mode"], "range")
        self.assertEqual(prods["hermes"]["upstream"]["image_tag"], "v{version}")
        self.assertEqual(prods["mcp-python-sdk"]["policy"]["min_version"], "1.30.0")
        for name in ("opendesign", "mcp-n8n", "mcp-nextcloud"):
            for leg, legd in prods[name]["legs"].items():
                self.assertIs(legd["fanout"]["enabled"], False, (name, leg))

    def test_fanout_off_reports_instead_of_rewriting(self):
        m = manifest()
        f = __import__("helpers").FakeFetcher(overrides={
            ("github:WOOWTECH/Woow_ha_mcp_addons", "apps/n8n/package.json", "claude-delivery"): '{"dependencies": {"n8n-mcp": "2.91.0"}}',
            ("github:WOOWTECH/Woow_ha_mcp_addons", "apps/n8n/package-lock.json", "claude-delivery"): '{"packages": {}}'})
        plans = [p for p in wvtbot.plan_fanout(m, f, TODAY, "ha", only="mcp-n8n")]
        self.assertTrue(plans and plans[0]["blocked"].startswith("fanout 關閉"))
        self.assertEqual(plans[0]["files"], {})


if __name__ == "__main__":
    unittest.main()
