"""The deployed platform release (sources: CD image pin → build record) and the PaaS MCP image leg
(woow-mcp-server: 8 seed templates × production / staging / develop vs the target commit).

2026-10-26: the drift report said production ran v18.0.1.0.258 because it read the newest release-note
comment of values-prod.yaml; the CD actually pinned the .265 image (paas-odoo-ci#420), and the MCP image
rows were info only, so a production seed still on the pre-all-on images would have shown ✅."""

import contextlib
import datetime as dt
import io
import json
import os
import re
import shutil
import tempfile
import unittest
import urllib.parse

from helpers import FIX, MANIFEST_PATH, FakeFetcher, approval_issues, fixture, manifest, write_manifest, wvt

import wvtbot  # noqa: E402

TODAY = dt.date(2026, 10, 26)
CD = "gitea:woow-paas/paas-app-bundle-cd"
PROD_VALUES = "paas-odoo-platform/values-prod.yaml"
STG_VALUES = "paas-odoo-platform-stg/values-stg.yaml"
PLATFORM = "gitea:odoo-addons/woow_paas_platform"
SEED = "src/data/cloud_app_templates.xml"
MCP_SERVER = "gitea:WOOWTECH/woow-mcp-server"
FC5 = "fc5fa02e3ff48225a1a2bdc5dfc74269a57a1b6f"
OLD_4D9 = "4d90de3bd26e959045973e022457331514a85330"
MCP_RECORDS = ["cloud_app_mcp_hermes", "cloud_app_mcp_odoo", "cloud_app_mcp_n8n", "cloud_app_mcp_k8s", "cloud_app_mcp_vk",
               "cloud_app_mcp_opendesign", "cloud_app_mcp_litellm", "cloud_app_mcp_nextcloud"]

# the layout of values-prod.yaml at cd main 429a5332 (2026-10-26): the newest note still names .258
REAL_1026 = ("# 2026-10-24: image-only .257 → .258; addon release v18.0.1.0.258 resolves to 11b2ed8b\n"
             "# 官方 image：approval paas-odoo-ci#402 /approve，build run 27310：clone＝image＝18.0.1.0.258\n"
             "# paas-odoo-platform — prod 平台 Odoo 的 values。\n"
             "profile: platform\n"
             "# ── image ──\n# 249: v18.0.1.0.249 / source b47af288\n"
             "image:\n  registry: jcr-prod.woowtech.io\n  repository: woow-paas-docker-local/paas-odoo-official\n"
             "  tag: \"20261026.0722\"\n  digest: \"sha256:0bf0f27b2886962345e4404a887a85a0ddf1864e71932bf23046194aab6c7c30\"\n"
             "  pullPolicy: IfNotPresent\n\nimagePullSecrets:\n  - name: jfrog-registry\n")


def rows(rep, product, pin=None, source=None):
    return [r for r in rep["rows"] if r["product"] == product and (pin is None or r["pin"] == pin)
            and (source is None or r.get("source") == source)]


def seed_with(record, tag, digest=None):
    """The fixture seed with one MCP record re-pinned (digest kept unless given)."""
    text = fixture("paas_platform", "src", "data", "cloud_app_templates.xml")
    cur = wvt.extract_seed_image(text, record)
    old = '"tag": "%s@%s"' % (cur["tag"], cur["digest"])
    assert text.count(old) == 1, record
    return text.replace(old, '"tag": "%s@%s"' % (tag, digest or cur["digest"]))


def issue(number, image, tag, ref, author="gitea-actions", workflow="build-image.yml", state="closed"):
    return {"number": number, "state": state, "user": {"login": author}, "pull_request": None,
            "title": "[prod-approval:%s] Build %s:%s" % (workflow, image, tag),
            "body": "<!-- approval-context\nWORKFLOW_MARKER=%s\nIMAGE_TAG=%s\nIMAGE_NAME=%s\nADDON_REF=%s\n-->" % (workflow, tag, image, ref)}


OFFICIAL = "woow-paas-docker-local/paas-odoo-official"


class CdImageTest(unittest.TestCase):
    def test_structured_pin(self):
        img = wvt.extract_cd_image(fixture("cd", "paas-odoo-platform", "values-prod.yaml"))
        self.assertEqual((img["registry"], img["repository"], img["tag"]),
                         ("jcr-prod.woowtech.io", OFFICIAL, "20261015.1258"))
        self.assertTrue(img["digest"].startswith("sha256:b523b7c2"))
        stg = wvt.extract_cd_image(fixture("cd", "paas-odoo-platform-stg", "values-stg.yaml"))  # single quotes, bare digest
        self.assertEqual((stg["tag"], stg["digest"][:15]), ("20261016.0331", "sha256:a978a970"))

    def test_only_the_top_level_block(self):
        text = "backup:\n  image:\n    tag: nope\nimage:\n  # pinned\n  tag: '1'\n\n  digest: sha256:x\nother:\n  tag: 2\n"
        self.assertEqual(wvt.extract_cd_image(text), {"registry": None, "repository": None, "tag": "1", "digest": "sha256:x"})
        self.assertEqual(wvt.extract_cd_image("profile: x\n")["tag"], None)


class BuildRecordTest(unittest.TestCase):
    def test_recorded_issues(self):
        issues = approval_issues()
        self.assertEqual(wvt.build_record(issues, "build-image.yml", OFFICIAL, "20261026.0722"), ("v18.0.1.0.265", [420], None))
        # 20261026.1100 is also a pi-agent build (#428): only the platform image counts
        self.assertEqual(wvt.build_record(issues, "build-image.yml", OFFICIAL, "20261026.1100"), ("v18.0.1.0.266", [426], None))

    def test_unknown_and_untrusted(self):
        tag, nums, why = wvt.build_record(approval_issues(), "build-image.yml", OFFICIAL, "20991231.0000")
        self.assertEqual((tag, nums), (None, []))
        self.assertIn("沒有 %s:20991231.0000" % OFFICIAL, why)
        for bad in (issue(1, OFFICIAL, "t", "v18.0.1.0.999", author="someone"),            # not the workflow's own issue
                    issue(2, OFFICIAL, "t", "v18.0.1.0.999", workflow="mirror-image.yml"),  # another workflow
                    dict(issue(3, OFFICIAL, "t", "v18.0.1.0.999"), pull_request={"url": "x"})):
            self.assertIsNone(wvt.build_record([bad], "build-image.yml", OFFICIAL, "t")[0])

    def test_conflicting_or_not_a_release(self):
        tag, nums, why = wvt.build_record([issue(5, OFFICIAL, "t", "v18.0.1.0.264"), issue(6, OFFICIAL, "t", "v18.0.1.0.265")],
                                          "build-image.yml", OFFICIAL, "t")
        self.assertEqual((tag, nums), (None, [5, 6]))
        self.assertIn("互相矛盾", why)
        self.assertEqual(wvt.build_record([issue(5, OFFICIAL, "t", "v18.0.1.0.265"), issue(9, OFFICIAL, "t", "v18.0.1.0.265")],
                                          "build-image.yml", OFFICIAL, "t"), ("v18.0.1.0.265", [5, 9], None))
        tag, _, why = wvt.build_record([issue(7, OFFICIAL, "t", "develop")], "build-image.yml", OFFICIAL, "t")
        self.assertIsNone(tag)
        self.assertIn("不是平台發版 tag", why)


class PlatformDriftTest(unittest.TestCase):
    def setUp(self):
        self.m = manifest()

    def collect(self, **kw):
        return wvt.collect_drift(self.m, FakeFetcher(**kw), TODAY)

    def test_versions_come_from_the_build_record(self):
        rep = self.collect()
        self.assertEqual((rep["context"]["prod_platform"], rep["context"]["stg_platform"]), ("v18.0.1.0.242", "v18.0.1.0.243"))
        self.assertEqual(rep["context"]["prod_platform_info"]["build_issues"], [343])
        md = wvt.render_markdown(self.m, rep)
        self.assertIn("- 正式站平台版：`v18.0.1.0.242`（`paas-app-bundle-cd:paas-odoo-platform/values-prod.yaml` 釘 image "
                      "`paas-odoo-official:20261015.1258@b523b7c24af4`；建置紀錄 paas-odoo-ci#343 的 ADDON_REF）", md)
        self.assertIn("- staging 平台版：`v18.0.1.0.243`", md)
        seed = rows(rep, "n8n", "seed")
        self.assertEqual({r["ref"] for r in seed}, {"v18.0.1.0.242", "develop"})

    def test_stale_release_note_does_not_win(self):
        # the 10-26 incident: the note says .258, the pinned image is the .265 build
        rep = self.collect(overrides={(CD, PROD_VALUES, "main"): REAL_1026})
        info = rep["context"]["prod_platform_info"]
        self.assertEqual((info["tag"], info["comment_tag"], info["build_issues"]), ("v18.0.1.0.265", "v18.0.1.0.258", [420]))
        self.assertTrue(all(r["ref"] == "v18.0.1.0.265" for r in rep["rows"] if r.get("source") == "prod"))
        self.assertTrue(any("檔頭最新的發版註記寫 v18.0.1.0.258，但釘的 image 20261026.0722 依建置紀錄 paas-odoo-ci#420 是 "
                            "v18.0.1.0.265" in n for n in rep["notes"]), rep["notes"])
        self.assertIn("正式站平台版：`v18.0.1.0.265`", wvt.render_markdown(self.m, rep))

    def test_no_record_is_unknown_never_the_note(self):
        rep = self.collect(issues=[])
        self.assertIsNone(rep["context"]["prod_platform"])
        self.assertEqual(rep["context"]["prod_platform_info"]["comment_tag"], "v18.0.1.0.242")  # it is there, and ignored
        prod = rows(rep, "n8n", "seed", "prod")
        self.assertEqual(len(prod), 1)
        self.assertEqual((prod[0]["status"], prod[0]["ref"]), ("unknown", None))
        self.assertIn("正式站平台版未知（建置紀錄（build-image.yml 核可單）沒有", prod[0]["findings"][0]["message"])
        self.assertIn("（正式站 未知）", prod[0]["point"])
        self.assertTrue(rep["drift"])
        md = wvt.render_markdown(self.m, rep)
        self.assertIn("- 正式站平台版：**未知**（建置紀錄", md)
        self.assertNotIn("正式站平台版：`", md)
        self.assertTrue(any("正式站平台版**未知**" in n for n in rep["notes"]))
        # develop is still compared
        self.assertEqual({r["status"] for r in rows(rep, "n8n", "seed", "branch")}, {"behind"})

    def test_conflicting_records_are_unknown(self):
        extra = issue(999, OFFICIAL, "20261015.1258", "v18.0.1.0.241")
        rep = self.collect(issues=approval_issues() + [extra])
        self.assertIsNone(rep["context"]["prod_platform"])
        self.assertIn("互相矛盾：#343＝v18.0.1.0.242、#999＝v18.0.1.0.241", rep["context"]["prod_platform_info"]["reason"])

    def test_unreadable_build_record_is_unreachable(self):
        class NoSearch(FakeFetcher):
            def search_issues(self, repo, query):
                raise wvt.WvtError("gitea: HTTP 403")
        rep = wvt.collect_drift(self.m, NoSearch(), TODAY)
        prod = rows(rep, "n8n", "seed", "prod")[0]
        self.assertEqual(prod["status"], "unreachable")
        self.assertIn("讀不到建置紀錄 gitea:woow-paas/paas-odoo-ci（gitea: HTTP 403）", prod["findings"][0]["message"])

    def staging_qa_build(self):
        return fixture("cd", "paas-odoo-platform-stg", "values-stg.yaml").replace(
            "woow-paas-docker-local/paas-odoo-official", "woow-paas-docker-local/paas-odoo-official-stg")

    def mcp_only(self):
        """Only the products whose rows are all aligned in the fixtures: drift then comes from staging or not at all."""
        self.m["products"] = {"woow-mcp-server": self.m["products"]["woow-mcp-server"]}

    def test_staging_qa_build_is_info_not_drift(self):
        # staging normally runs addon-only builds (build-paas-official-staging.yml → paas-odoo-official-stg, no
        # build record): all 7 staging releases 10-23..26 were such builds (CD history of values-stg.yaml, read 10-26).
        # Counted as drift, they would have kept the tracking issue open for the whole QA period.
        rep = self.collect(overrides={(CD, STG_VALUES, "main"): self.staging_qa_build()})
        self.assertIsNone(rep["context"]["stg_platform"])
        self.assertEqual(rep["context"]["prod_platform"], "v18.0.1.0.242")
        md = wvt.render_markdown(self.m, rep)
        self.assertIn("- staging 平台版：**未知**（建置紀錄（build-image.yml 核可單）沒有 woow-paas-docker-local/paas-odoo-official-stg:20261016.0331）"
                      "；用 `$stg_platform` 的列不比對，只列參考、不算漂移（`sources.stg_platform.on_unknown: info`）", md)
        stg = rows(rep, "woow-mcp-server", source="stg")
        self.assertEqual(({r["status"] for r in stg}, len(stg)), ({"info"}, 8))
        self.assertIn("staging 平台版未知（建置紀錄（build-image.yml 核可單）沒有", stg[0]["findings"][0]["message"])
        self.assertIn("只列參考、不算漂移", stg[0]["findings"][0]["message"])
        self.assertFalse(any(r["status"] in wvt.DRIFT_STATUSES for r in rep["rows"] if r.get("source") == "stg"))
        # the compared rows (production, develop) still decide the overview
        self.assertIn("| woow-mcp-server | lockstep | 2026.10.25.0927 | —（沒有這一邊） | ✅ 一致 2026.10.25.0927 | ✅ 一致 |", md)
        self.mcp_only()
        self.assertFalse(self.collect(overrides={(CD, STG_VALUES, "main"): self.staging_qa_build()})["drift"])

    def test_production_unknown_still_counts(self):
        self.mcp_only()
        rep = self.collect(issues=[])  # no build record for either pin
        self.assertEqual(({r["status"] for r in rows(rep, "woow-mcp-server", source="prod")},
                          {r["status"] for r in rows(rep, "woow-mcp-server", source="stg")}), ({"unknown"}, {"info"}))
        self.assertTrue(rep["drift"])

    def test_unreadable_staging_still_counts(self):
        class NoSearch(FakeFetcher):
            def search_issues(self, repo, query):
                raise wvt.WvtError("gitea: 讀不到 woow-paas/paas-odoo-ci 的 issue（HTTP 404）")
        self.mcp_only()
        rep = wvt.collect_drift(self.m, NoSearch(), TODAY)
        self.assertEqual({r["status"] for r in rows(rep, "woow-mcp-server", source="stg")}, {"unreachable"})
        self.assertTrue(rep["drift"])


class McpImageLegTest(unittest.TestCase):
    def setUp(self):
        self.m = manifest()

    def collect(self, **kw):
        return wvt.collect_drift(self.m, FakeFetcher(**kw), TODAY)

    def test_eight_templates_three_points_aligned(self):
        f = FakeFetcher()
        rep = wvt.collect_drift(self.m, f, TODAY)
        rs = rows(rep, "woow-mcp-server")
        self.assertEqual(len(rs), 24)
        self.assertEqual({r["source"] for r in rs}, {"prod", "stg", "branch"})
        self.assertEqual(sorted({r["pin"] for r in rs}), sorted(["hermes", "odoo", "n8n", "k8s", "vk", "opendesign", "litellm", "nextcloud"]))
        self.assertTrue(all(r["status"] == "ok" and r["version"] == "2026.10.25.0927" for r in rs), [(r["point"], r["status"]) for r in rs])
        self.assertTrue(any("（staging v18.0.1.0.243）" in r["point"] for r in rs))
        self.assertIn("| woow-mcp-server | lockstep | 2026.10.25.0927 | —（沒有這一邊） | ✅ 一致 2026.10.25.0927 | ✅ 一致 |",
                      wvt.render_markdown(self.m, rep))
        # aligned rows never ask Gitea for the commit order (the shared-files row asks about charts only)
        self.assertFalse(any(c[0] == "compare" and c[1] == MCP_SERVER for c in f.calls))

    def test_prod_seed_on_a_known_older_commit_is_behind(self):
        # platform .258 pinned all eight to 4d90de3 (before all-on); this used to be an info row (✅)
        old = seed_with("cloud_app_mcp_odoo", OLD_4D9)
        rep = self.collect(overrides={(PLATFORM, SEED, "v18.0.1.0.242"): old})
        r = next(x for x in rows(rep, "woow-mcp-server", "odoo", "prod"))
        self.assertEqual((r["status"], r["version"]), ("behind", "2026.10.24.1009"))
        self.assertIn("identity.commit：4d90de3bd26e（清冊記為 2026.10.24.1009）≠ 清冊目標 fc5fa02e3ff4（2026.10.25.0927）",
                      r["findings"][0]["message"])
        self.assertEqual({x["status"] for x in rows(rep, "woow-mcp-server", "odoo", "branch")}, {"ok"})
        self.assertTrue(rep["drift"])
        md = wvt.render_markdown(self.m, rep)
        self.assertIn("| woow-mcp-server | lockstep | 2026.10.25.0927 | —（沒有這一邊） | 🟠 落後 2026.10.24.1009 / 2026.10.25.0927 | 🟠 落後 |", md)

    def test_unlisted_commits_are_placed_by_git_history(self):
        cases = {"behind": (0, None), "ahead": (3, 0), "diverged": (3, 2), "unknown": (None, None)}
        for name, (fwd, back) in cases.items():
            other = {"behind": "b" * 40, "ahead": "a" * 40, "diverged": "d" * 40, "unknown": "e" * 40}[name]
            compares = {(MCP_SERVER, FC5, other): fwd}
            if back is not None:
                compares[(MCP_SERVER, other, FC5)] = back
            rep = self.collect(overrides={(PLATFORM, SEED, "develop"): seed_with("cloud_app_mcp_k8s", other)}, compares=compares)
            r = next(x for x in rows(rep, "woow-mcp-server", "k8s", "branch"))
            want = {"behind": "behind", "ahead": "ahead", "diverged": "mismatch", "unknown": "mismatch"}[name]
            self.assertEqual(r["status"], want, (name, r["findings"]))
            msg = r["findings"][0]["message"]
            self.assertIn({"behind": "是清冊目標 fc5fa02e3ff4 之前的 commit（WOOWTECH/woow-mcp-server）",
                           "ahead": "是清冊目標 fc5fa02e3ff4 之後的 commit（WOOWTECH/woow-mcp-server）：清冊 target 要跟上",
                           "diverged": "和清冊目標 fc5fa02e3ff4 分岔",
                           "unknown": "不是 WOOWTECH/woow-mcp-server 裡的 commit"}[name], msg)

    def test_right_tag_wrong_digest(self):
        swapped = seed_with("cloud_app_mcp_vk", FC5, "sha256:" + "9" * 64)
        rep = self.collect(overrides={(PLATFORM, SEED, "develop"): swapped})
        r = next(x for x in rows(rep, "woow-mcp-server", "vk", "branch"))
        self.assertEqual(r["status"], "mismatch")
        self.assertEqual(r["findings"][0]["rule"], "digest")
        self.assertIn("tag 是清冊目標 fc5fa02e3ff4，但 digest 999999999999 ≠ 清冊 artifacts.vk-mcp-admin 0c7227cf00c0", r["findings"][0]["message"])

    def test_offline_check_on_a_platform_pr(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        repo = os.path.join(tmp, "p")
        shutil.copytree(os.path.join(FIX, "paas_platform"), repo)
        path = os.path.join(repo, SEED)

        def check(seed_text):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(seed_text)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = wvt.main(["--manifest", MANIFEST_PATH, "--today", "2026-10-26", "check", "--leg", "paas", "--repo-dir", repo,
                               "--repo", PLATFORM, "--product", "woow-mcp-server"])
            return rc, buf.getvalue()
        rc, out = check(fixture("paas_platform", "src", "data", "cloud_app_templates.xml"))
        self.assertEqual(rc, 0)
        self.assertEqual(out.count("✅ 一致"), 8, out)
        rc, out = check(seed_with("cloud_app_mcp_hermes", OLD_4D9))
        self.assertEqual(rc, 0)  # no hard rules: a warning
        self.assertIn("🟠 落後", out)
        rc, out = check(seed_with("cloud_app_mcp_hermes", "c" * 40))  # offline: no history to ask
        self.assertEqual(rc, 0)
        self.assertIn("identity.commit：cccccccccccc ≠ 清冊 fc5fa02e3ff4", out)


SHA_258 = "11b2ed8b86c2eb97176836c74b3eb8710d98515f"   # v18.0.1.0.258 (seed: 4d90de3, before all-on)
SHA_266 = "528df25d8711b5faca51814ad90ebdb105af0138"   # v18.0.1.0.266
SHA_DEV = "ccbb4a053eecdad164726dcc6a185682078859ca"   # develop (seed: fc5fa02)


class FakeGiteaApi:
    """Gitea 1.27 as it answered read-only probes of git-prod on 2026-10-26:
    commits?sha= resolves a branch, a tag, a full or a SHORT sha to the full sha and 404s a missing ref;
    raw?ref= serves a branch, a tag or a full sha (404 for a missing full sha) and the DEFAULT branch for
    anything else — a missing tag, or a short sha that exists. A repo the token cannot see (`hidden`)
    answers 404 for everything: the repo itself, its commits, raw files, issues and compares."""

    def __init__(self):
        self.repos = {}
        self.hidden = set()
        self.urls = []
        self.add("odoo-addons/woow_paas_platform", "develop",
                 {"develop": SHA_DEV, "v18.0.1.0.266": SHA_266, "v18.0.1.0.258": SHA_258},
                 {SHA_258: {SEED: "seed at .258"}, SHA_266: {SEED: "seed at .266"}, SHA_DEV: {SEED: "seed at develop"}})

    def add(self, full, default, refs, files, issues=(), history=()):
        self.repos[full] = {"default": default, "refs": refs, "files": files, "issues": list(issues), "history": list(history)}

    def resolve(self, repo, ref):
        if ref in repo["refs"]:
            return repo["refs"][ref]
        hits = [s for s in repo["files"] if len(ref) >= 7 and s.startswith(ref)]
        return hits[0] if len(hits) == 1 else None

    def __call__(self, url):
        self.urls.append(url)
        parts = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qs(parts.query)
        full, rest = re.match(r"^/api/v1/repos/([^/]+/[^/]+)(.*)$", parts.path).groups()
        if full in self.hidden or full not in self.repos:
            return None  # 404
        repo = self.repos[full]
        if rest == "":
            return json.dumps({"full_name": full})
        if rest == "/commits":
            sha = self.resolve(repo, q["sha"][0])
            return json.dumps([{"sha": sha}]) if sha else None
        if rest.startswith("/raw/"):
            ref = q["ref"][0]
            if ref in repo["refs"]:
                sha = repo["refs"][ref]
            elif re.fullmatch(r"[0-9a-f]{40}", ref):
                sha = ref
            else:
                sha = repo["refs"][repo["default"]]  # the trap: the default branch, HTTP 200
            return (repo["files"].get(sha) or {}).get(urllib.parse.unquote(rest[len("/raw/"):]))
        if rest == "/issues":
            return json.dumps([i for i in repo["issues"] if q["q"][0] in i["title"] + i["body"]])
        if rest.startswith("/compare/"):  # a linear history: commits head has and base lacks
            base, head = rest[len("/compare/"):].split("...")
            h = repo["history"]
            if base not in h or head not in h:
                return None
            return json.dumps({"total_commits": max(0, h.index(head) - h.index(base))})
        raise AssertionError(url)


class GiteaRefTest(unittest.TestCase):
    """Every Gitea read resolves its ref to the full commit sha first, then reads raw at that sha: raw serves
    the default branch for a ref it does not know (2026-10-26: ?ref=v18.0.1.0.999 and ?ref=11b2ed8 both
    returned develop's seed), so a wrong platform tag or a short sha would compare develop."""

    def fetcher(self):
        api = FakeGiteaApi()

        class F(wvt.Fetcher):
            def _get(self, url, headers=None, endpoint=None):
                return api(url)
        return F("https://gitea.example/api/v1", "t"), api

    def test_missing_ref_is_not_read(self):
        f, api = self.fetcher()
        self.assertIsNone(f.read(PLATFORM, SEED, "v18.0.1.0.999"))
        self.assertFalse(any("/raw/" in u for u in api.urls))

    def test_tags_and_branches_are_read_at_their_commit(self):
        f, api = self.fetcher()
        self.assertEqual(f.read(PLATFORM, SEED, "v18.0.1.0.266"), "seed at .266")
        self.assertEqual(f.read(PLATFORM, SEED, "develop"), "seed at develop")
        raws = [u for u in api.urls if "/raw/" in u]
        self.assertEqual([u.rsplit("ref=", 1)[1] for u in raws], [SHA_266, SHA_DEV])  # never the ref name itself
        f.read(PLATFORM, "other.xml", "v18.0.1.0.266")
        self.assertEqual(sum("/commits?sha=v18.0.1.0.266" in u for u in api.urls), 1)  # one lookup per repo and ref

    def test_a_short_sha_reads_that_commit_not_the_default_branch(self):
        # 10-26 review: read(..., "11b2ed8") passed the old existence check and returned develop's seed
        f, api = self.fetcher()
        for ref in (SHA_258[:7], SHA_258[:12]):
            self.assertEqual(f.read(PLATFORM, SEED, ref), "seed at .258", ref)
        self.assertEqual(f.resolve_commit(PLATFORM, SHA_258[:7]), SHA_258)
        self.assertTrue(all(u.endswith("ref=" + SHA_258) for u in api.urls if "/raw/" in u))

    def test_a_full_sha_needs_no_lookup(self):
        f, api = self.fetcher()
        self.assertEqual(f.read(PLATFORM, SEED, SHA_258), "seed at .258")
        self.assertIsNone(f.read(PLATFORM, SEED, "0" * 40))  # a full sha that does not exist: raw 404s
        self.assertEqual([u for u in api.urls if "/commits?" in u], [])

    def test_bot_reads_bytes_at_the_resolved_commit(self):
        f, api = self.fetcher()
        bf = wvtbot.BotFetcher("https://gitea.example/api/v1", "t")
        bf._get = f._get
        seen = []
        orig = wvtbot.http

        def http(url, headers=None, **kw):
            seen.append(url)
            body = api(url)
            return (200, {}, body.encode()) if body is not None else (404, {}, b"")
        wvtbot.http = http
        self.addCleanup(setattr, wvtbot, "http", orig)
        self.assertEqual(bf.read_bytes(PLATFORM, SEED, SHA_258[:7]), b"seed at .258")
        self.assertIsNone(bf.read_bytes(PLATFORM, SEED, "v18.0.1.0.999"))
        self.assertEqual(seen, ["https://gitea.example/api/v1/repos/odoo-addons/woow_paas_platform/raw/src/data/cloud_app_templates.xml?ref="
                                + SHA_258])


SHA_CD = "64c42360ec" + "0" * 30


class GiteaNotFoundTest(unittest.TestCase):
    """A 404 is "not there" only inside a repo the token can see. Gitea hides a private repo from a token
    without access behind the same 404 (10-26: anonymous search of paas-odoo-ci gave [] → "建置紀錄沒有…"),
    so a lost CI_GITEA_TOKEN permission must read ⚠️ unreachable, never ❔ no record／file missing."""

    def fetcher(self, hidden=()):
        api = FakeGiteaApi()
        api.add("woow-paas/paas-app-bundle-cd", "main", {"main": SHA_CD},
                {SHA_CD: {PROD_VALUES: fixture("cd", "paas-odoo-platform", "values-prod.yaml")}})
        api.add("woow-paas/paas-odoo-ci", "main", {"main": SHA_CD}, {}, issues=approval_issues())
        api.add("WOOWTECH/woow-mcp-server", "main", {"main": FC5}, {}, history=[OLD_4D9, FC5])
        api.hidden.update(hidden)

        class F(wvt.Fetcher):
            def _get(self, url, headers=None, endpoint=None):
                return api(url)
        return F("https://gitea.example/api/v1", "t"), api

    def src(self):
        return manifest()["sources"]["prod_platform"]

    def test_visible_repos_resolve_the_platform(self):
        f, _ = self.fetcher()
        info = wvt.resolve_platform(f, "prod_platform", self.src())
        self.assertEqual((info["tag"], info["build_issues"], info["unreachable"]), ("v18.0.1.0.242", [343], False))

    def test_build_records_the_token_cannot_read_are_unreachable(self):
        f, _ = self.fetcher(hidden={"woow-paas/paas-odoo-ci"})
        with self.assertRaises(wvt.WvtError) as cm:
            f.search_issues("gitea:woow-paas/paas-odoo-ci", "20261015.1258")
        self.assertIn("讀不到 woow-paas/paas-odoo-ci 的 issue（HTTP 404：repo 不存在、權杖沒有讀取權限，或沒開 issue）", str(cm.exception))
        info = wvt.resolve_platform(f, "prod_platform", self.src())
        self.assertEqual((info["tag"], info["unreachable"]), (None, True))
        self.assertIn("讀不到建置紀錄 gitea:woow-paas/paas-odoo-ci（gitea: 讀不到 woow-paas/paas-odoo-ci 的 issue", info["reason"])
        self.assertNotIn("沒有 woow-paas-docker-local", info["reason"])  # not "no build record"

    def test_a_cd_repo_the_token_cannot_see_is_unreachable(self):
        f, _ = self.fetcher(hidden={"woow-paas/paas-app-bundle-cd"})
        info = wvt.resolve_platform(f, "prod_platform", self.src())
        self.assertTrue(info["unreachable"])
        self.assertIn("讀不到 woow-paas/paas-app-bundle-cd（HTTP 404：repo 不存在，或權杖沒有讀取權限）", info["reason"])
        self.assertNotIn("檔案不存在", info["reason"])

    def test_inside_a_visible_repo_a_404_is_still_not_there(self):
        f, api = self.fetcher()
        self.assertIsNone(f.read(PLATFORM, "missing.xml", "develop"))
        self.assertIsNone(f.read(PLATFORM, SEED, "v18.0.1.0.999"))
        self.assertIsNone(f.read(PLATFORM, SEED, "0" * 40))
        self.assertIsNone(f.commits_between(MCP_SERVER, FC5, "e" * 40))  # a commit the repo does not have
        self.assertEqual(f.commit_order(MCP_SERVER, FC5, OLD_4D9), "behind")  # 4d90de3 is before fc5fa02
        self.assertEqual(sum(u.endswith("/repos/odoo-addons/woow_paas_platform") for u in api.urls), 0)  # commits 200 proved it
        self.assertEqual(sum(u.endswith("/repos/WOOWTECH/woow-mcp-server") for u in api.urls), 1)  # asked once

    def test_every_read_of_a_hidden_repo_raises(self):
        f, _ = self.fetcher(hidden={"odoo-addons/woow_paas_platform", "WOOWTECH/woow-mcp-server", "woow-paas/paas-app-bundle-cd"})
        for call in (lambda: f.read(PLATFORM, SEED, "develop"), lambda: f.read(PLATFORM, SEED, SHA_258),
                     lambda: f.commits_between(MCP_SERVER, FC5, OLD_4D9), lambda: f.blob_shas(CD, "paas-odoo-platform", "main")):
            with self.assertRaises(wvt.WvtError):
                call()
        # an identity row whose history repo is hidden says why it cannot place the commit
        p = manifest()["products"]["woow-mcp-server"]
        pin = p["legs"]["paas"]["pins"][1]
        res = wvt.evaluate(p, "paas", pin, {"raw": {"tag": "b" * 40}, "value": "b" * 40, "digest": None}, TODAY, order=f.commit_order)
        self.assertEqual(res["status"], "mismatch")
        self.assertIn("無法向 WOOWTECH/woow-mcp-server 確認先後：gitea: 讀不到 WOOWTECH/woow-mcp-server（HTTP 404", res["findings"][0]["message"])


class SourcesManifestTest(unittest.TestCase):
    def errors(self, m):
        return wvt.validate_manifest(m, TODAY, expiry=False)[0]

    def test_sources_need_a_build_record(self):
        m = manifest()
        m["sources"]["prod_platform"]["kind"] = "cd_addon_ref"  # the comment reader is gone
        del m["sources"]["stg_platform"]["builds"]
        errs = self.errors(m)
        self.assertTrue(any("sources.prod_platform.kind must be one of cd_image" in e for e in errs), errs)
        self.assertTrue(any("sources.stg_platform.builds needs" in e for e in errs), errs)

    def test_on_unknown(self):
        m = manifest()
        self.assertEqual((m["sources"]["prod_platform"].get("on_unknown", "drift"), m["sources"]["stg_platform"]["on_unknown"]),
                         ("drift", "info"))  # production stays fail-closed
        m["sources"]["stg_platform"]["on_unknown"] = "ignore"
        self.assertTrue(any("sources.stg_platform.on_unknown must be one of drift, info" in e for e in self.errors(m)))

    def test_identity_pin_keys(self):
        m = manifest()
        pins = m["products"]["woow-mcp-server"]["legs"]["paas"]["pins"]
        pins[0]["also_refs"] = ["$nope"]
        pins[1]["digest"] = "artifacts.missing"
        pins[2]["history"] = "github:WOOWTECH/woow-mcp-server"
        version_pin = m["products"]["mcp-n8n"]["legs"]["paas"]["pins"][0]   # a version compare: no digest key
        version_pin["digest"] = "artifacts.sdist"
        m["products"]["woow-mcp-server"]["known_versions"].append({"version": "2026.1.1", "identity": {"commit": FC5}})
        errs = self.errors(m)
        for needle in ("pins[hermes]: ref $nope refers to an unknown source", "pins[odoo]: digest must name a target digest",
                       "pins[n8n]: history must be gitea:OWNER/REPO", "mcp-n8n.legs.paas.pins[%s]: digest must name" % version_pin["id"],
                       "known_versions[2026.1.1].identity.commit duplicates the target"):
            self.assertTrue(any(needle in e for e in errs), (needle, errs))

    def test_json_manifest_round_trip(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        m = manifest()
        self.assertEqual(wvt.validate_manifest(wvt.load_manifest(write_manifest(tmp, m)), TODAY, expiry=False)[0], [])


if __name__ == "__main__":
    unittest.main()
