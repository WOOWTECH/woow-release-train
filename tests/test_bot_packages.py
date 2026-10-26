"""propose for package products (upstream.package pypi:／npm:): hashes from the PyPI JSON and the npm
registry, yanked／deprecated releases refused, and a `verified` that names the real source.

The registry answers are recordings (tests/fixtures/pypi, tests/fixtures/npm) served offline."""

import contextlib
import datetime as dt
import io
import json
import os
import unittest

from helpers import FIX, MANIFEST_PATH, FakeFetcher, fixture, manifest, wvt

import wvtbot  # noqa: E402

TODAY = dt.date(2026, 10, 24)
MCP_SERVER = "gitea:WOOWTECH/woow-mcp-server"
ODOO_110_SDIST = "sha256:335ff70d850af6a13416869217c656089032759624b6fe60b22732dc7b29debb"
ODOO_130_SDIST = "sha256:5c1fb57c2ce6aadf354639da3438ebf700839eeb6f06cd87be42cbf9cdee76d3"
ODOO_130_WHEEL = "sha256:a49855b694cd4ed4025e929f899f281becb5f42a129d403a18c0f17f8759bb43"
N8N_MCP_INTEGRITY = "sha512-fnpvAEuXOBRP0J6Uy3BjIyRja3y4Ox9rsmohqPb1qUEshso9aBt4L5/aYgSEl6lq/HoBPJ+DmZV0hP/GS+B/Dg=="
N8N_MCP_SHASUM = "af2f94f805e43c254de88f4228b3dda6eb1fa17b"
ODOO_JSON = "https://pypi.org/pypi/odoo-mcp/1.3.0/json"
MCP_JSON = "https://pypi.org/pypi/mcp/2.3.0/json"
REQUESTS_JSON = "https://pypi.org/pypi/requests/2.32.0/json"

RECORDED = {
    ODOO_JSON: "pypi/odoo-mcp-1.3.0.json",
    MCP_JSON: "pypi/mcp-2.3.0.json",
    REQUESTS_JSON: "pypi/requests-2.32.0.json",                           # yanked
    "https://registry.npmjs.org/n8n-mcp/2.96.2": "npm/n8n-mcp-2.96.2.json",
    "https://registry.npmjs.org/request/2.88.2": "npm/request-2.88.2.json",  # deprecated
}


def manifest_text():
    with open(MANIFEST_PATH, encoding="utf-8") as fh:
        return fh.read()


class Registries:
    """Offline PyPI／npm: serves the recordings (optionally edited per URL); anything else is a 404."""

    def __init__(self, edits=None):
        self.edits = edits or {}
        self.urls = []

    def __call__(self, url, headers):
        self.urls.append(url)
        if url not in RECORDED:
            return 404, {}, b'{"message": "Not Found"}'
        with open(os.path.join(FIX, RECORDED[url]), encoding="utf-8") as fh:
            doc = json.load(fh)
        doc = self.edits.get(url, lambda d: d)(doc)
        return 200, {"content-type": "application/json"}, json.dumps(doc).encode()


class Oci:
    """OCI registry stand-in; a package product must never reach it."""

    def __init__(self, table=None):
        self.table = table or {}

    def resolve(self, image, tag):
        if (image, tag) not in self.table:
            raise AssertionError("unexpected registry lookup %s:%s" % (image, tag))
        return self.table[(image, tag)]


def target(product, version, get, oci=None, edit=None):
    p = manifest()["products"][product]
    if edit:
        edit(p)
    return wvtbot.new_target_for(p, version, TODAY, oci or Oci(), get)


class PypiTest(unittest.TestCase):
    def test_sdist_comes_from_the_pypi_json(self):
        reg = Registries()
        t = target("mcp-odoo", "1.3.0", reg)   # snapshot target: odoo-mcp 1.1.0 with artifacts.sdist
        self.assertEqual(t["artifacts"], {"sdist": ODOO_130_SDIST})
        self.assertEqual(t["verified"], "2026-10-24 wvt propose：pypi odoo-mcp 1.3.0（sdist sha256 取自 PyPI JSON）")
        self.assertEqual(reg.urls, [ODOO_JSON])
        self.assertEqual(set(t), {"version", "since", "artifacts", "verified"})

    def test_missing_sdist_is_a_clear_error(self):
        no_sdist = {ODOO_JSON: lambda d: dict(d, urls=[u for u in d["urls"] if u["packagetype"] != "sdist"])}
        with self.assertRaises(wvt.WvtError) as cm:
            target("mcp-odoo", "1.3.0", Registries(no_sdist))
        self.assertEqual(str(cm.exception), "PyPI odoo-mcp 1.3.0 沒有 sdist（.tar.gz），無法核對 artifacts.sdist")
        # what the version owner reads in the bot summary
        m = manifest()
        plan = wvtbot.plan_propose(m, wvt.collect_drift(m, FakeFetcher(), TODAY), FakeFetcher(), Oci(), TODAY,
                                   only="mcp-odoo", force_version="1.3.0", get=Registries(no_sdist))[0]
        self.assertIsNone(plan["target"])
        self.assertEqual(plan["notes"], ["查不到 1.3.0 的上游身分：PyPI odoo-mcp 1.3.0 沒有 sdist（.tar.gz），無法核對 artifacts.sdist"])

    def test_wheel_must_be_the_only_one(self):
        def both(p):
            p["target"]["artifacts"] = {"sdist": ODOO_110_SDIST, "wheel": "sha256:" + "0" * 64}
        t = target("mcp-odoo", "1.3.0", Registries(), edit=both)
        self.assertEqual(t["artifacts"], {"sdist": ODOO_130_SDIST, "wheel": ODOO_130_WHEEL})
        self.assertIn("（sdist、wheel sha256 取自 PyPI JSON）", t["verified"])
        linux = {ODOO_JSON: lambda d: dict(d, urls=d["urls"] + [dict(d["urls"][0], filename="odoo_mcp-1.3.0-cp312-manylinux_x86_64.whl")])}
        with self.assertRaises(wvt.WvtError) as cm:
            target("mcp-odoo", "1.3.0", Registries(linux), edit=both)
        self.assertIn("有 2 個 wheel", str(cm.exception))

    def test_other_artifact_names_and_unknown_versions(self):
        def odd(p):
            p["target"]["artifacts"] = {"server_py": ODOO_110_SDIST}
        with self.assertRaises(wvt.WvtError) as cm:
            target("mcp-odoo", "1.3.0", Registries(), edit=odd)
        self.assertIn("只對得到 sdist、wheel", str(cm.exception))
        with self.assertRaises(wvt.WvtError) as cm:
            target("mcp-odoo", "9.9.9", Registries())
        self.assertIn("PyPI odoo-mcp 9.9.9 不存在", str(cm.exception))

    def test_yanked_release_is_refused(self):
        def requests(p, artifacts=True):
            p["upstream"] = {"package": "pypi:requests"}
            p["target"] = {"version": "2.31.0", "since": "2026-10-01"}
            if artifacts:
                p["target"]["artifacts"] = {"sdist": ODOO_110_SDIST}
        for artifacts in (True, False):   # with or without hashes to fill: a yanked release is never proposed
            with self.assertRaises(wvt.WvtError) as cm:
                target("mcp-odoo", "2.32.0", Registries(), edit=lambda p, a=artifacts: requests(p, a))
            self.assertEqual(str(cm.exception), "PyPI requests 2.32.0 已撤回（yanked：Yanked due to conflicts with "
                                                "CVE-2024-35195 mitigation），不提案")
        # a yanked file inside a release that is not yanked as a whole
        file_only = {REQUESTS_JSON: lambda d: dict(d, info=dict(d["info"], yanked=False, yanked_reason=None))}
        with self.assertRaises(wvt.WvtError) as cm:
            target("mcp-odoo", "2.32.0", Registries(file_only), edit=requests)
        self.assertIn("PyPI requests-2.32.0.tar.gz 已撤回", str(cm.exception))

    def test_yanked_proposal_becomes_a_note(self):
        m = manifest()
        yank = {MCP_JSON: lambda d: dict(d, info=dict(d["info"], yanked=True, yanked_reason="broken release"))}
        plan = wvtbot.plan_propose(m, wvt.collect_drift(m, FakeFetcher(), TODAY), FakeFetcher(), Oci(), TODAY,
                                   only="mcp-python-sdk", force_version="2.3.0", get=Registries(yank))[0]
        self.assertIsNone(plan["target"])
        self.assertEqual(plan["notes"], ["查不到 2.3.0 的上游身分：PyPI mcp 2.3.0 已撤回（yanked：broken release），不提案"])


class NpmTest(unittest.TestCase):
    def test_integrity_and_shasum_come_from_the_registry(self):
        def pinned(p):
            p["target"]["identity"] = {"integrity": "sha512-old", "shasum": "0" * 40}
        reg = Registries()
        t = target("mcp-n8n", "2.96.2", reg, edit=pinned)
        self.assertEqual(t["identity"], {"integrity": N8N_MCP_INTEGRITY, "shasum": N8N_MCP_SHASUM})
        self.assertEqual(t["verified"], "2026-10-24 wvt propose：npm n8n-mcp 2.96.2（integrity、shasum 取自 npm registry）")
        self.assertEqual(reg.urls, ["https://registry.npmjs.org/n8n-mcp/2.96.2"])
        m = manifest()
        pinned(m["products"]["mcp-n8n"])
        m["products"]["mcp-n8n"]["target"] = t
        self.assertEqual(wvt.validate_manifest(m, TODAY, expiry=False)[0], [])

    def test_deprecated_is_refused(self):
        def request(p):
            p["upstream"] = {"package": "npm:request"}
            p["target"] = {"version": "2.88.0", "identity": {"integrity": "sha512-old"}}
        with self.assertRaises(wvt.WvtError) as cm:
            target("mcp-n8n", "2.88.2", Registries(), edit=request)
        self.assertEqual(str(cm.exception), "npm request@2.88.2 已棄用（deprecated：request has been deprecated, "
                                            "see https://github.com/request/request/issues/3142），不提案")

    def test_unknown_version_is_refused_without_identity(self):
        # what L3-4c ran on 10-26: `propose --product mcp-n8n --version 9.9.9` planned 2.96.2 → 9.9.9 unchecked
        with self.assertRaises(wvt.WvtError) as cm:
            target("mcp-n8n", "9.9.9", Registries())
        self.assertIn("npm n8n-mcp@9.9.9 不存在", str(cm.exception))
        m = manifest()
        plan = wvtbot.plan_propose(m, wvt.collect_drift(m, FakeFetcher(), TODAY), FakeFetcher(), Oci(), TODAY,
                                   only="mcp-n8n", force_version="9.9.9", get=Registries())[0]
        self.assertIsNone(plan["target"])
        self.assertEqual(len(plan["notes"]), 1)
        self.assertTrue(plan["notes"][0].startswith("查不到 9.9.9 的上游身分：npm n8n-mcp@9.9.9 不存在"), plan["notes"])

    def test_deprecated_is_refused_without_identity(self):
        def request(p):
            p["upstream"] = {"package": "npm:request"}
            p["target"] = {"version": "2.88.0", "since": "2026-10-01"}
        with self.assertRaises(wvt.WvtError) as cm:
            target("mcp-n8n", "2.88.2", Registries(), edit=request)
        self.assertIn("npm request@2.88.2 已棄用", str(cm.exception))

    def test_registry_answer_must_be_that_version(self):
        # a dist-tag (`latest`) or a registry answering for another version is not a version to pin
        reg = Registries({"https://registry.npmjs.org/n8n-mcp/2.96.2": lambda d: dict(d, version="2.99.1")})
        with self.assertRaises(wvt.WvtError) as cm:
            target("mcp-n8n", "2.96.2", reg)
        self.assertIn("registry 回的是 2.99.1", str(cm.exception))

    def test_no_sha256_artifacts_and_scoped_names(self):
        def artifact(p):
            p["target"]["artifacts"] = {"tarball": "sha256:" + "0" * 64}
        with self.assertRaises(wvt.WvtError) as cm:
            target("mcp-n8n", "2.96.2", Registries(), edit=artifact)
        self.assertIn("沒有 sha256", str(cm.exception))
        urls = []

        def get(url, headers):
            urls.append(url)
            return 200, {}, b'{"dist": {"integrity": "sha512-x"}}'
        wvtbot.npm_release("@modelcontextprotocol/sdk", "1.32.2", get)
        self.assertEqual(urls, ["https://registry.npmjs.org/@modelcontextprotocol%2Fsdk/1.32.2"])


class DowngradeTest(unittest.TestCase):
    """A version below the current target is refused unless asked for with --allow-downgrade (L3 10-26:
    `--version 2.2.0` for mcp-python-sdk 2.3.0 and `--version 1.2.3` for mcp-odoo 1.3.0 were proposed silently)."""

    def plan(self, product, version, target_version, allow=False, edit=None):
        m = manifest()
        m["products"][product]["target"]["version"] = target_version
        if edit:
            edit(m["products"][product])
        reg = Registries()
        rep = wvt.collect_drift(m, FakeFetcher(), TODAY)
        plans = wvtbot.plan_propose(m, rep, FakeFetcher(), Oci(), TODAY, only=product, force_version=version, get=reg,
                                    allow_downgrade=allow)
        return m, plans[0], reg

    def test_npm_downgrade_needs_the_flag(self):
        _, plan, reg = self.plan("mcp-n8n", "2.96.2", "2.99.1")
        self.assertIsNone(plan["target"])
        self.assertFalse(plan["downgrade"])
        self.assertEqual(plan["notes"], ["2.96.2 低於清冊目標 2.99.1：降版要明確帶 --allow-downgrade（bots.yml 的 allow_downgrade=true），不提案"])
        self.assertEqual(reg.urls, [])  # refused before any registry call

    def test_npm_downgrade_with_the_flag_is_verified_and_marked(self):
        m, plan, reg = self.plan("mcp-n8n", "2.96.2", "2.99.1", allow=True)
        self.assertEqual((plan["version"], plan["downgrade"]), ("2.96.2", True))
        self.assertEqual(plan["target"]["verified"], "2026-10-24 wvt propose：npm n8n-mcp 2.96.2（npm registry 確認已發佈、未棄用）")
        self.assertEqual(reg.urls, ["https://registry.npmjs.org/n8n-mcp/2.96.2"])
        title, body = wvtbot.render_propose(m, plan, "abc1234")
        self.assertIn("清冊目標 2.99.1 → 2.96.2（降版）", title)
        self.assertIn("⚠️ **降版**", body)

    def test_pypi_downgrade(self):
        _, plan, _ = self.plan("mcp-python-sdk", "2.3.0", "2.4.0")
        self.assertIsNone(plan["target"])
        self.assertIn("降版要明確帶 --allow-downgrade", plan["notes"][0])
        _, plan, reg = self.plan("mcp-python-sdk", "2.3.0", "2.4.0", allow=True)
        self.assertEqual((plan["version"], plan["downgrade"]), ("2.3.0", True))
        self.assertEqual(reg.urls, [MCP_JSON])

    def test_the_flag_never_beats_a_forbidden_downgrade(self):
        def forbid(p):
            p["policy"]["downgrade"] = "forbidden"
        _, plan, _ = self.plan("mcp-n8n", "2.96.2", "2.99.1", allow=True, edit=forbid)
        self.assertIsNone(plan["target"])
        self.assertEqual(plan["notes"], ["2.96.2 低於清冊 2.99.1，而此產品禁止降版，不提案"])

    def test_upgrades_need_no_flag(self):
        _, plan, _ = self.plan("mcp-n8n", "2.96.2", "2.92.1")
        self.assertEqual((plan["version"], plan["downgrade"]), ("2.96.2", False))

    def test_versions_that_cannot_be_ordered_are_refused_first(self):
        # 10-26 review: `--version 2.0.0rc1` (PyPI mcp really has 2.0.0a1…rc1, all below 2.3.0) passed the
        # downgrade gate as "not lower" (cmp None → 0), reached PyPI, then crashed validate_manifest
        for version in ("2.0.0rc1", "1.3.0rc1", "2.2.0.post1", "1.0.dev3", "2.4.0rc1", "latest"):
            for allow in (False, True):  # the flag cannot help: neither gate could be applied
                _, plan, reg = self.plan("mcp-python-sdk", version, "2.3.0", allow=allow)
                self.assertIsNone(plan["target"], version)
                self.assertEqual(plan["notes"], ["%s 和清冊目標 2.3.0 排不出先後（只認 x.y.z 與 SemVer 預發版 x.y.z-rc.1；"
                                                 "PEP 440 的 a／b／rc／post／dev 寫法不認），不提案" % version])
                self.assertEqual(reg.urls, [], version)

    def test_a_semver_pre_release_is_below_its_release(self):
        # 10-26 review: 2.96.2-beta.1 compared equal to 2.96.2 (suffix ignored) and became a plain upgrade PR
        _, plan, reg = self.plan("mcp-n8n", "2.96.2-beta.1", "2.96.2")
        self.assertIsNone(plan["target"])
        self.assertEqual(plan["notes"], ["2.96.2-beta.1 低於清冊目標 2.96.2：降版要明確帶 --allow-downgrade（bots.yml 的 "
                                         "allow_downgrade=true），不提案"])
        self.assertEqual(reg.urls, [])
        beta = "https://registry.npmjs.org/n8n-mcp/2.96.2-beta.1"
        RECORDED[beta] = RECORDED["https://registry.npmjs.org/n8n-mcp/2.96.2"]
        self.addCleanup(RECORDED.pop, beta)
        m = manifest()
        m["products"]["mcp-n8n"]["target"]["version"] = "2.96.2"
        reg = Registries({beta: lambda d: dict(d, version="2.96.2-beta.1")})
        rep = wvt.collect_drift(m, FakeFetcher(), TODAY)
        plan = wvtbot.plan_propose(m, rep, FakeFetcher(), Oci(), TODAY, only="mcp-n8n", force_version="2.96.2-beta.1", get=reg,
                                   allow_downgrade=True)[0]
        self.assertEqual((plan["version"], plan["downgrade"]), ("2.96.2-beta.1", True))
        self.assertIn("清冊目標 2.96.2 → 2.96.2-beta.1（降版）", wvtbot.render_propose(m, plan, "abc1234")[0])

    def test_pre_release_order_against_the_target(self):
        for version, tv, lower in (("2.97.0-rc.1", "2.96.2", False),   # a newer core: an upgrade
                                   ("2.97.0", "2.97.0-rc.1", False),   # the release of a pre-release target
                                   ("2.97.0-rc.2", "2.97.0-rc.1", False),
                                   ("2.97.0-rc.1", "2.97.0-rc.2", True),
                                   ("2.96.1-beta.1", "2.96.2", True)):
            _, plan, reg = self.plan("mcp-n8n", version, tv)
            refused = any("降版要明確帶 --allow-downgrade" in n for n in plan["notes"])
            self.assertEqual(refused, lower, (version, tv, plan["notes"]))
            self.assertEqual(reg.urls != [], not lower, (version, tv))  # upgrades go on to the registry

    def test_the_highest_ahead_version_by_release_precedence(self):
        # two legs ahead with the same core: the release beats its pre-release whatever the row order
        m = manifest()
        m["products"]["mcp-n8n"]["target"]["version"] = "2.96.2"

        def row(leg, version):
            return {"product": "mcp-n8n", "leg": leg, "source": "branch", "compare": "version", "version": version,
                    "point": "p", "file": "f", "repo": "r", "ref": "main", "findings": [{"status": "ahead"}]}
        for order in (("2.97.0-rc.1", "2.97.0"), ("2.97.0", "2.97.0-rc.1")):
            reg = Registries()
            rep = {"rows": [row("ha", order[0]), row("paas", order[1])]}
            plan = wvtbot.plan_propose(m, rep, FakeFetcher(), Oci(), TODAY, only="mcp-n8n", get=reg)[0]
            self.assertEqual(reg.urls, ["https://registry.npmjs.org/n8n-mcp/2.97.0"], order)
            self.assertEqual(plan["reasons"], ["%s p：2.97.0（f）" % ("PaaS" if order[1] == "2.97.0" else "HA")])

    def test_cli_pre_release_does_not_crash_the_run(self):
        # 10-26 review: the same live dry-run ended in a traceback (exit 1) before
        orig_f, orig_r = wvtbot.BotFetcher, wvtbot.Registry

        class F(FakeFetcher):
            def __init__(self, *a, **k):
                super().__init__()
        wvtbot.BotFetcher, wvtbot.Registry = F, Oci
        self.addCleanup(setattr, wvtbot, "BotFetcher", orig_f)
        self.addCleanup(setattr, wvtbot, "Registry", orig_r)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = wvtbot.main(["--manifest", MANIFEST_PATH, "--today", "2026-10-24", "--mode", "dry-run", "--bot", "propose",
                              "--product", "mcp-python-sdk", "--version", "2.0.0rc1"])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("- mcp-python-sdk：2.0.0rc1 和清冊目標 1.30.0 排不出先後", out.getvalue())  # snapshot target

    def test_cli(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), self.assertRaises(SystemExit):
            wvtbot.main(["--manifest", MANIFEST_PATH, "--bot", "propose", "--mode", "dry-run", "--allow-downgrade"])
        self.assertIn("--allow-downgrade needs --product and --version", buf.getvalue())
        orig_f, orig_r = wvtbot.BotFetcher, wvtbot.Registry

        class F(FakeFetcher):
            def __init__(self, *a, **k):
                super().__init__()
        wvtbot.BotFetcher, wvtbot.Registry = F, Oci
        self.addCleanup(setattr, wvtbot, "BotFetcher", orig_f)
        self.addCleanup(setattr, wvtbot, "Registry", orig_r)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):  # 2.91.0 < snapshot target 2.92.1: refused before any registry call
            rc = wvtbot.main(["--manifest", MANIFEST_PATH, "--today", "2026-10-24", "--mode", "dry-run", "--bot", "propose",
                              "--product", "mcp-n8n", "--version", "2.91.0"])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("- mcp-n8n：2.91.0 低於清冊目標 2.92.1：降版要明確帶 --allow-downgrade", out.getvalue())


class VerifiedSourceTest(unittest.TestCase):
    """`verified` names what was looked up; it read `registry None:<version>` for packages (PR #16, #20)."""

    def test_pypi_without_artifacts_still_checks_the_release(self):
        reg = Registries()
        t = target("mcp-python-sdk", "2.3.0", reg)
        self.assertEqual(t["verified"], "2026-10-24 wvt propose：pypi mcp 2.3.0（PyPI JSON 確認已發佈、未撤回）")
        self.assertEqual(reg.urls, [MCP_JSON])

    def test_npm_without_integrity_still_checks_the_release(self):
        # 10-26 (L3-4c): with no identity recorded, propose never asked npm, so a mistyped version
        # became a manifest PR. The registry is always asked now; there is just nothing to copy.
        reg = Registries()
        t = target("mcp-n8n", "2.96.2", reg)
        self.assertEqual(t["verified"], "2026-10-24 wvt propose：npm n8n-mcp 2.96.2（npm registry 確認已發佈、未棄用）")
        self.assertEqual(reg.urls, ["https://registry.npmjs.org/n8n-mcp/2.96.2"])
        self.assertEqual(set(t), {"version", "since", "verified"})

    def test_image(self):
        n8n = {"index_digest": "sha256:" + "1" * 64, "platforms": {"amd64": "sha256:" + "2" * 64, "arm64": "sha256:" + "3" * 64}}
        reg = Registries()
        t = target("n8n", "2.44.0", reg, Oci({("docker.io/n8nio/n8n", "2.44.0"): n8n}))
        self.assertEqual(t["verified"], "2026-10-24 wvt propose：registry docker.io/n8nio/n8n:2.44.0")
        self.assertEqual(reg.urls, [])

    def test_emqx_sha256_files_unchanged(self):
        emqx = {"index_digest": "sha256:" + "4" * 64, "platforms": {"amd64": "sha256:" + "5" * 64, "arm64": "sha256:" + "6" * 64}}
        seen = []

        def get(url, headers):
            seen.append(url)
            return 200, {}, (("7" if "amd64" in url else "8") * 64).encode()
        t = target("emqx", "5.8.10", get, Oci({("docker.io/emqx/emqx", "5.8.10"): emqx}))
        url = "https://github.com/emqx/emqx/releases/download/v5.8.10/emqx-5.8.10-debian13-%s.tar.gz.sha256"
        self.assertEqual(seen, [url % "amd64", url % "arm64"])
        self.assertEqual(t["verified"], "2026-10-24 wvt propose：registry docker.io/emqx/emqx:5.8.10；%s；%s" % (url % "amd64", url % "arm64"))

    def test_identity_that_cannot_be_looked_up_is_a_note_not_a_crash(self):
        # OpenDesign keeps fork／upstream commits in target.identity: propose used to drop them, the
        # edited manifest failed validation and the whole propose run stopped
        orig_f, orig_r = wvtbot.BotFetcher, wvtbot.Registry

        class F(FakeFetcher):
            def __init__(self, *a, **k):
                super().__init__()
        wvtbot.BotFetcher, wvtbot.Registry = F, Oci
        self.addCleanup(setattr, wvtbot, "BotFetcher", orig_f)
        self.addCleanup(setattr, wvtbot, "Registry", orig_r)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = wvtbot.main(["--manifest", MANIFEST_PATH, "--today", "2026-10-24", "--mode", "dry-run", "--bot", "propose",
                              "--product", "opendesign", "--version", "20261101.0101"])
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertIn("- opendesign：查不到 20261101.0101 的上游身分：target.identity（upstream_version、fork_commit、"
                      "upstream_commit）機器人無從查證，請人工更新清冊", buf.getvalue())


class ProposeMcpOdooTest(unittest.TestCase):
    """2026-10-24: PaaS (woow-mcp-server #82) moved to odoo-mcp 1.3.0 first; propose could not verify the sdist."""

    def fetcher(self, sdist=ODOO_130_SDIST):
        lock = ('version = 1\n\n[[package]]\nname = "mcp"\nversion = "2.3.0"\nsource = { registry = "https://pypi.org/simple" }\n\n'
                '[[package]]\nname = "odoo-mcp"\nversion = "1.3.0"\nsource = { registry = "https://pypi.org/simple" }\n'
                'sdist = { url = "https://files.pythonhosted.org/packages/4c/f9/x/odoo_mcp-1.3.0.tar.gz", hash = "%s", size = 305955 }\n' % sdist)
        return FakeFetcher(overrides={(MCP_SERVER, "apps/odoo/pyproject.toml", "main"): 'dependencies = ["odoo-mcp==1.3.0", "mcp==2.3.0"]\n',
                                      (MCP_SERVER, "uv.lock", "main"): lock})

    def plans(self, f):
        m = manifest()
        return m, {p["product"]: p for p in wvtbot.plan_propose(m, wvt.collect_drift(m, f, TODAY), f, Oci(), TODAY, get=Registries())}

    def test_pypi_verified_target_matching_the_lock(self):
        m, plans = self.plans(self.fetcher())
        plan = plans["mcp-odoo"]
        self.assertEqual((plan["version"], plan["notes"]), ("1.3.0", []))
        self.assertEqual(plan["target"]["artifacts"], {"sdist": ODOO_130_SDIST})
        self.assertEqual(plan["target"]["verified"],
                         "2026-10-24 wvt propose：pypi odoo-mcp 1.3.0（sdist sha256 取自 PyPI JSON）；與 PaaS uv.lock 相同")
        # the shared uv.lock moved mcp to 2.3.0 as well: that proposal asks PyPI too
        self.assertEqual(plans["mcp-python-sdk"]["target"]["verified"],
                         "2026-10-24 wvt propose：pypi mcp 2.3.0（PyPI JSON 確認已發佈、未撤回）")
        title, body = wvtbot.render_propose(m, plan, "abc1234")
        self.assertIn("清冊目標 1.1.0 → 1.3.0", title)
        self.assertIn("- 上游身分：pypi odoo-mcp 1.3.0（sdist sha256 取自 PyPI JSON）；與 PaaS uv.lock 相同\n  - sdist `%s`" % ODOO_130_SDIST, body)
        self.assertNotIn("None", body)
        new = wvt.load_yaml(wvtbot.apply_propose_text(manifest_text(), plan, TODAY))["products"]["mcp-odoo"]
        self.assertEqual(new["target"]["artifacts"], {"sdist": ODOO_130_SDIST})
        self.assertEqual((new["known_versions"][0]["version"], new["known_versions"][0]["artifacts"]), ("1.1.0", {"sdist": ODOO_110_SDIST}))

    def test_lock_hash_other_than_pypi_is_not_proposed(self):
        _, plans = self.plans(self.fetcher(sdist="sha256:" + "9" * 64))
        plan = plans["mcp-odoo"]
        self.assertIsNone(plan["target"])
        self.assertEqual(plan["notes"], ["uv.lock 的 digest 999999999999 ≠ registry 5c1fb57c2ce6：先查清楚再提案"])

    def test_tarball_sha256_next_to_the_version_that_is_ahead(self):
        # the same check on an image product: HA pins the EMQX tarball sha256 beside EMQX_VERSION
        emqx = {"index_digest": "sha256:" + "4" * 64, "platforms": {"amd64": "sha256:" + "5" * 64, "arm64": "sha256:" + "6" * 64}}
        get = lambda url, h: (200, {}, (("7" if "amd64" in url else "8") * 64).encode())  # noqa: E731
        df = fixture("ha_emqx", "emqx", "Dockerfile").replace('EMQX_VERSION="v5.8.9"', 'EMQX_VERSION="v5.8.10"')
        df = df.replace("524b5dd8ccad1f7dc5e5c15171328618e1ea7703fed56e17006a27258a13ecd4", "8" * 64)
        for amd64, ok in (("7" * 64, True), ("6" * 64, False)):
            f = FakeFetcher(overrides={("github:WOOWTECH/Woow_ha_emqx", "emqx/Dockerfile", "main"):
                                       df.replace("c3b5a8f7c83601867d0fc1ef19a851f6f16f56a9436de74cb05f81f188a44a90", amd64)})
            m = manifest()
            plan = next(p for p in wvtbot.plan_propose(m, wvt.collect_drift(m, f, TODAY), f, Oci({("docker.io/emqx/emqx", "5.8.10"): emqx}),
                                                       TODAY, get=get) if p["product"] == "emqx")
            if ok:
                self.assertTrue(plan["target"]["verified"].endswith("；與 HA emqx/Dockerfile 相同"), plan["target"]["verified"])
            else:
                self.assertIsNone(plan["target"])
                self.assertEqual(plan["notes"], ["emqx/Dockerfile 的 digest 666666666666 ≠ registry 777777777777：先查清楚再提案"])


if __name__ == "__main__":
    unittest.main()
