"""Phase 2 bots (tools/wvtbot.py): writers, manifest edits, registry, GitHub App JWT, plans, push."""

import base64
import contextlib
import datetime as dt
import io
import json
import os
import random
import shutil
import subprocess
import tempfile
import unittest

from helpers import FIX, MANIFEST_PATH, FakeFetcher, fixture, manifest, wvt

import wvtbot  # noqa: E402  (tools/ is on sys.path via helpers)

TODAY = dt.date(2026, 10, 24)
N8N_244 = {"index_digest": "sha256:" + "1" * 64, "platforms": {"amd64": "sha256:" + "2" * 64, "arm64": "sha256:" + "3" * 64}}


def manifest_text():
    with open(MANIFEST_PATH, encoding="utf-8") as fh:
        return fh.read()


class FakeRegistry:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def resolve(self, image, tag):
        self.calls.append((image, tag))
        if (image, tag) not in self.table:
            raise wvt.WvtError("registry: %s:%s not found" % (image, tag))
        return self.table[(image, tag)]


class WriterTest(unittest.TestCase):
    def test_dockerfile_arg_keeps_quotes(self):
        text = 'ARG BUILD_FROM=x\nARG EMQX_VERSION="v5.8.9"\nARG EMQX_VERSION="v1"\n'
        out = wvtbot.write_dockerfile_arg(text, "EMQX_VERSION", "v5.8.10")
        self.assertEqual(out, 'ARG BUILD_FROM=x\nARG EMQX_VERSION="v5.8.10"\nARG EMQX_VERSION="v1"\n')
        self.assertEqual(wvt.extract_dockerfile_arg(out, "EMQX_VERSION"), "v5.8.10")
        with self.assertRaises(wvt.WvtError):
            wvtbot.write_dockerfile_arg(text, "NOPE", "1")

    def test_yaml_path_keeps_comment_and_quotes(self):
        text = fixture("charts", "charts", "litellm", "values.yaml")
        out = wvtbot.write_yaml_path(text, "image.digest", "sha256:" + "a" * 64)
        self.assertEqual(wvt.extract_yaml(out, "image.digest"), "sha256:" + "a" * 64)
        self.assertEqual(wvt.extract_yaml(out, "image.tag"), "20261009.1344")
        self.assertIn('digest: "sha256:' + "a" * 64 + '"', out)
        self.assertEqual(len(out.splitlines()), len(text.splitlines()))
        out = wvtbot.write_yaml_path("a:\n  b: 1  # keep\n", "a.b", "2")
        self.assertEqual(out, "a:\n  b: 2  # keep\n")
        with self.assertRaises(wvt.WvtError):
            wvtbot.write_yaml_path(text, "image.nope", "x")

    def test_json_path_keeps_style(self):
        text = fixture("ha_litellm", "litellm", "upstream.lock.json")
        out = wvtbot.write_json_path(text, "tag", "1.105.0")
        self.assertEqual(json.loads(out)["tag"], "1.105.0")
        self.assertEqual(len(out.splitlines()), len(text.splitlines()))
        with self.assertRaises(wvt.WvtError):
            wvtbot.write_json_path('{"a":  1}\n', "a", "2")  # not a json.dumps style: refuse to reformat

    def test_seed_image_and_field(self):
        text = fixture("paas_platform", "src", "data", "cloud_app_templates.xml")
        cur = wvt.extract_seed_image(text, "cloud_app_n8n")
        old = cur["tag"] + "@" + cur["digest"]
        new = "20261101.0101@sha256:" + "2" * 64
        out = wvtbot.write_seed_image_tag(text, "cloud_app_n8n", old, new)
        got = wvt.extract_seed_image(out, "cloud_app_n8n")
        self.assertEqual((got["tag"], got["digest"]), ("20261101.0101", "sha256:" + "2" * 64))
        self.assertEqual(wvt.extract_seed_image(out, "cloud_app_emqx"), wvt.extract_seed_image(text, "cloud_app_emqx"))
        out = wvtbot.write_seed_field(text, "cloud_app_emqx", "helm_chart_version", "9.9.9")
        self.assertEqual(wvt.extract_seed_field(out, "cloud_app_emqx", "helm_chart_version"), "9.9.9")

    def test_changelog_follows_heading_style(self):
        out = wvtbot.changelog_with_entry("# Changelog\n\n## v5.9.1\n\nold\n", "5.9.2", "- new")
        self.assertTrue(out.startswith("# Changelog\n\n## v5.9.2\n\n- new\n\n## v5.9.1"))
        self.assertEqual(wvtbot.changelog_with_entry(out, "5.9.2", "- again"), out)
        out = wvtbot.changelog_with_entry("# Changelog\n\n## 2.43.6\n\nx\n", "2.44.0", "- n")
        self.assertIn("## 2.44.0\n\n- n\n\n## 2.43.6", out)

    def test_versions_and_refs(self):
        self.assertEqual(wvtbot.bump_patch("5.9.1"), "5.9.2")
        self.assertEqual(wvtbot.bump_patch("v0.1.11"), "v0.1.12")
        d = "sha256:" + "c" * 64
        self.assertEqual(wvtbot.new_image_ref("ghcr.io/berriai/litellm-non_root:1.104.0@sha256:" + "a" * 64, "1.105.0", d),
                         "ghcr.io/berriai/litellm-non_root:1.105.0@" + d)
        self.assertEqual(wvtbot.new_image_ref("docker.io/x/y:v0.22.5@sha256:" + "a" * 64, "0.23.0", d), "docker.io/x/y:v0.23.0@" + d)
        self.assertEqual(wvtbot.new_image_ref("jcr-prod.woowtech.io/woow-paas-docker-local/litellm@sha256:" + "a" * 64, "1.105.0", d),
                         "jcr-prod.woowtech.io/woow-paas-docker-local/litellm@" + d)
        with self.assertRaises(wvt.WvtError):
            wvtbot.new_image_ref("jcr-prod.woowtech.io/x/n8n:20261015.2215@sha256:" + "a" * 64, "2.44.0", d)
        self.assertEqual(wvtbot.new_image_ref("jcr-prod.woowtech.io/x/n8n:20261015.2215", "2.44.0", d, "20261101.0101"),
                         "jcr-prod.woowtech.io/x/n8n:20261101.0101@" + d)


class ManifestEditTest(unittest.TestCase):
    def test_set_target_moves_old_into_known_versions(self):
        text = manifest_text()
        new = dict(version="2.44.0", since="2026-10-24", verified="test", **N8N_244)
        out = wvtbot.set_target(text, "n8n", new)
        m = wvtbot.check_manifest_text(out, TODAY)
        n8n = m["products"]["n8n"]
        self.assertEqual(n8n["target"]["version"], "2.44.0")
        self.assertEqual(n8n["target"]["platforms"]["amd64"], "sha256:" + "2" * 64)
        kv = n8n["known_versions"]
        self.assertEqual([k["version"] for k in kv], ["2.43.6", "2.38.0", "2.35.3"])
        self.assertEqual(kv[0]["platforms"]["amd64"], "sha256:371dd463f02e8c35a4bc5aaf9855fc4cfec1babab97e5a02a1e31923a2ec8f5a")
        self.assertNotIn("since", kv[0])
        # everything outside the n8n block stays byte for byte
        self.assertEqual(out.split("  n8n:")[0], text.split("  n8n:")[0])

    def test_set_target_creates_known_versions(self):
        new = {"version": "5.8.10", "since": "2026-10-24", "index_digest": "sha256:" + "4" * 64,
               "platforms": {"amd64": "sha256:" + "5" * 64, "arm64": "sha256:" + "6" * 64},
               "artifacts": {"debian13-amd64": "sha256:" + "7" * 64, "debian13-arm64": "sha256:" + "8" * 64}}
        m = wvtbot.check_manifest_text(wvtbot.set_target(manifest_text(), "emqx", new), TODAY)
        self.assertEqual(m["products"]["emqx"]["known_versions"][0]["version"], "5.8.9")
        self.assertEqual(m["products"]["emqx"]["known_versions"][0]["artifacts"]["debian13-amd64"],
                         "sha256:c3b5a8f7c83601867d0fc1ef19a851f6f16f56a9436de74cb05f81f188a44a90")

    def test_rollback_drops_the_duplicate_known_version(self):
        old = manifest()["products"]["n8n"]["known_versions"][0]
        new = {"version": "2.38.0", "since": "2026-10-24", "index_digest": old["index_digest"], "platforms": old["platforms"]}
        m = wvtbot.check_manifest_text(wvtbot.set_target(manifest_text(), "n8n", new), TODAY)
        self.assertEqual([k["version"] for k in m["products"]["n8n"]["known_versions"]], ["2.43.6", "2.35.3"])

    def test_set_shared_commit(self):
        files = ["sitecustomize.py", "woow_chatgpt.py", "woow_chatgpt_plugin.py", "woow_usage_limit.py"]
        blobs = {n: "%040x" % i for i, n in enumerate(files, 1)}
        out = wvtbot.set_shared_commit(manifest_text(), "litellm", 0, "b" * 40, blobs, files)
        sf = wvtbot.check_manifest_text(out, TODAY)["products"]["litellm"]["shared_files"][0]
        self.assertEqual((sf["source"]["commit"], sf["source"]["files"], sf["source"]["blobs"]), ("b" * 40, files, blobs))
        self.assertEqual(sf["known_commits"][0]["commit"], "4ad95a11d8dad671b342d6cc9454c87a3130e8b6")
        self.assertEqual(sf["known_commits"][0]["blobs"]["woow_chatgpt.py"], "527c16c68a83958cc2f124253632b98de20b56c6")
        self.assertEqual(sf["consumers"][0]["lock"], "litellm/woow-plugin.lock.json")
        again = wvtbot.set_shared_commit(out, "litellm", 0, "c" * 40, blobs, files)
        kc = wvt.load_yaml(again)["products"]["litellm"]["shared_files"][0]["known_commits"]
        self.assertEqual([k["commit"] for k in kc], ["b" * 40, "4ad95a11d8dad671b342d6cc9454c87a3130e8b6"])


class RegistryTest(unittest.TestCase):
    def test_token_challenge_and_index(self):
        calls = []
        index = json.dumps({"manifests": [
            {"digest": "sha256:" + "a" * 64, "platform": {"os": "linux", "architecture": "amd64"}},
            {"digest": "sha256:" + "b" * 64, "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
            {"digest": "sha256:" + "c" * 64, "platform": {"os": "unknown", "architecture": "unknown"}}]}).encode()

        def get(url, headers):
            calls.append((url, dict(headers)))
            if url.startswith("https://auth.docker.io"):
                return 200, {}, b'{"token": "t0k"}'
            if headers.get("Authorization") != "Bearer t0k":
                return 401, {"www-authenticate": 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io",scope="repository:n8nio/n8n:pull"'}, b""
            return 200, {"docker-content-digest": "sha256:" + "d" * 64}, index
        res = wvtbot.Registry(get).resolve("docker.io/n8nio/n8n", "2.44.0")
        self.assertEqual(res, {"index_digest": "sha256:" + "d" * 64,
                               "platforms": {"amd64": "sha256:" + "a" * 64, "arm64": "sha256:" + "b" * 64}})
        self.assertEqual(calls[0][0], "https://registry-1.docker.io/v2/n8nio/n8n/manifests/2.44.0")
        self.assertIn("scope=repository%3An8nio%2Fn8n%3Apull", calls[1][0])

    def test_single_platform_manifest(self):
        def get(url, headers):
            if url.endswith("/manifests/20261018.1219"):
                return 200, {"docker-content-digest": "sha256:" + "e" * 64}, json.dumps({"config": {"digest": "sha256:" + "f" * 64}}).encode()
            return 200, {}, b'{"architecture": "amd64"}'
        res = wvtbot.Registry(get).resolve("ghcr.io/woowtech/open-design-core", "20261018.1219")
        self.assertEqual(res, {"index_digest": None, "platforms": {"amd64": "sha256:" + "e" * 64}})

    def test_split_image(self):
        self.assertEqual(wvtbot.split_image("postgres"), ("https://registry-1.docker.io", "library/postgres"))
        self.assertEqual(wvtbot.split_image("nousresearch/hermes-agent"), ("https://registry-1.docker.io", "nousresearch/hermes-agent"))
        self.assertEqual(wvtbot.split_image("ghcr.io/berriai/litellm-non_root"), ("https://ghcr.io", "berriai/litellm-non_root"))

    def test_sha256_file(self):
        get = lambda url, h: (200, {}, b"c3b5a8f7c83601867d0fc1ef19a851f6f16f56a9436de74cb05f81f188a44a90  emqx.tar.gz\n")  # noqa: E731
        self.assertEqual(wvtbot.fetch_sha256_file("https://x/y.sha256", get),
                         "sha256:c3b5a8f7c83601867d0fc1ef19a851f6f16f56a9436de74cb05f81f188a44a90")


def _der_len(n):
    if n < 128:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _der_int(x):
    b = x.to_bytes((x.bit_length() + 8) // 8, "big")
    return b"\x02" + _der_len(len(b)) + b


def _der_seq(*items):
    body = b"".join(items)
    return b"\x30" + _der_len(len(body)) + body


def _test_key(bits=512, seed=7):
    """A throwaway RSA key for the signature tests (never a real credential)."""
    rnd = random.Random(seed)

    def is_prime(n):
        if n % 2 == 0:
            return n == 2
        d, s = n - 1, 0
        while d % 2 == 0:
            d, s = d // 2, s + 1
        for _ in range(20):
            x = pow(rnd.randrange(2, n - 2), d, n)
            if x in (1, n - 1):
                continue
            for _ in range(s - 1):
                x = pow(x, 2, n)
                if x == n - 1:
                    break
            else:
                return False
        return True

    def prime(b):
        while True:
            c = rnd.getrandbits(b) | (1 << (b - 1)) | 1
            if is_prime(c):
                return c
    e = 65537
    while True:
        p, q = prime(bits // 2), prime(bits // 2)
        if p != q and (p - 1) % e and (q - 1) % e:
            break
    n, d = p * q, pow(e, -1, (p - 1) * (q - 1))
    der = _der_seq(*[_der_int(x) for x in (0, n, e, d, p, q, d % (p - 1), d % (q - 1), pow(q, -1, p))])
    return n, e, der


def _pem(der, label):
    b64 = base64.b64encode(der).decode()
    return "-----BEGIN %s-----\n%s\n-----END %s-----\n" % (label, "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64)), label)


class GitHubAppJwtTest(unittest.TestCase):
    def test_rs256_signature_verifies(self):
        n, e, der = _test_key()
        pem = _pem(der, "RSA PRIVATE KEY")
        self.assertEqual(wvtbot.rsa_private_key(pem)[:2], (n, e))
        sig = wvtbot.rs256(pem, b"hello")
        k = (n.bit_length() + 7) // 8
        em = pow(int.from_bytes(sig, "big"), e, n).to_bytes(k, "big")
        self.assertTrue(em.startswith(b"\x00\x01\xff"))
        self.assertTrue(em.endswith(wvtbot._SHA256_DIGEST_INFO + __import__("hashlib").sha256(b"hello").digest()))

    def test_pkcs8_wrapper(self):
        n, e, der = _test_key(seed=11)
        algo = _der_seq(b"\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x01\x01", b"\x05\x00")
        pkcs8 = _der_seq(_der_int(0), algo, b"\x04" + _der_len(len(der)) + der)
        self.assertEqual(wvtbot.rsa_private_key(_pem(pkcs8, "PRIVATE KEY"))[:2], (n, e))

    def test_jwt_shape(self):
        _, _, der = _test_key()
        jwt = wvtbot.app_jwt(123456, _pem(der, "RSA PRIVATE KEY"), now=1_800_000_000)
        head, body, _sig = jwt.split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        self.assertEqual(json.loads(base64.urlsafe_b64decode(pad(head))), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(json.loads(base64.urlsafe_b64decode(pad(body))), {"iat": 1_799_999_940, "exp": 1_800_000_540, "iss": "123456"})

    @unittest.skipUnless(shutil.which("openssl"), "openssl not installed")
    def test_openssl_agrees(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        key = os.path.join(tmp, "k.pem")
        if subprocess.run(["openssl", "genrsa", "-traditional", "-out", key, "1024"], capture_output=True).returncode:
            subprocess.run(["openssl", "genrsa", "-out", key, "1024"], capture_output=True, check=True)
        with open(key) as fh:
            pem = fh.read()
        want = subprocess.run(["openssl", "dgst", "-sha256", "-sign", key], input=b"msg", capture_output=True, check=True).stdout
        self.assertEqual(wvtbot.rs256(pem, b"msg"), want)


def drift(m, **kw):
    return wvt.collect_drift(m, FakeFetcher(**kw), TODAY)


class ProposeTest(unittest.TestCase):
    def test_ha_ahead_opens_a_registry_verified_target(self):
        m = manifest()
        df = fixture("ha_n8n", "n8n", "Dockerfile").replace("N8N_UPSTREAM_VERSION=2.43.6", "N8N_UPSTREAM_VERSION=2.44.0")
        f = FakeFetcher(overrides={("github:WOOWTECH/Woow_ha_n8n", "n8n/Dockerfile", "main"): df})
        rep = wvt.collect_drift(m, f, TODAY)
        reg = FakeRegistry({("docker.io/n8nio/n8n", "2.44.0"): N8N_244})
        plans = wvtbot.plan_propose(m, rep, f, reg, TODAY)
        plan = next(p for p in plans if p["product"] == "n8n")
        self.assertEqual(plan["version"], "2.44.0")
        self.assertEqual(plan["target"]["platforms"], N8N_244["platforms"])
        self.assertEqual(plan["target"]["since"], "2026-10-24")
        self.assertTrue(any("HA" in r for r in plan["reasons"]))
        title, body = wvtbot.render_propose(m, plan, "abc1234")
        self.assertIn("2.43.6 → 2.44.0", title)
        self.assertIn("附升級證據", body)  # n8n migrates one way
        new = wvtbot.apply_propose_text(manifest_text(), plan, TODAY)
        self.assertEqual(wvt.load_yaml(new)["products"]["n8n"]["target"]["version"], "2.44.0")

    def test_published_and_prod_rows_do_not_propose(self):
        m = manifest()
        rep = drift(m)
        for r in rep["rows"]:
            if r["product"] == "n8n" and r.get("source") != "branch":
                r["findings"] = [wvt.finding("ahead", "range", "x")]
                r["version"] = "9.9.9"
        self.assertEqual([p for p in wvtbot.plan_propose(m, rep, FakeFetcher(), FakeRegistry({}), TODAY) if p["product"] == "n8n"], [])

    def test_license_ceiling_is_never_proposed(self):
        m = manifest()
        df = fixture("ha_emqx", "emqx", "Dockerfile").replace('EMQX_VERSION="v5.8.9"', 'EMQX_VERSION="v5.10.5"')
        f = FakeFetcher(overrides={("github:WOOWTECH/Woow_ha_emqx", "emqx/Dockerfile", "main"): df})
        plan = next(p for p in wvtbot.plan_propose(m, wvt.collect_drift(m, f, TODAY), f, FakeRegistry({}), TODAY) if p["product"] == "emqx")
        self.assertIsNone(plan["target"])
        self.assertTrue(any("授權上限" in n for n in plan["notes"]))

    def test_registry_digest_must_match_the_side_that_is_ahead(self):
        m = manifest()
        df = fixture("ha_litellm", "litellm", "Dockerfile").replace(
            "litellm-non_root:1.104.0@sha256:37066242e6b8327fdd627682a66a9ae2a599553b4d5a84c905c6d8b9eb35c01e",
            "litellm-non_root:1.105.0@sha256:" + "9" * 64)
        f = FakeFetcher(overrides={("github:WOOWTECH/Woow_ha_litellm", "litellm/Dockerfile", "main"): df})
        reg = FakeRegistry({("ghcr.io/berriai/litellm-non_root", "1.105.0"): N8N_244})
        plan = next(p for p in wvtbot.plan_propose(m, wvt.collect_drift(m, f, TODAY), f, reg, TODAY) if p["product"] == "litellm")
        self.assertIsNone(plan["target"])
        self.assertTrue(any("registry" in n for n in plan["notes"]))

    def test_manual_version(self):
        m = manifest()
        f = FakeFetcher()
        reg = FakeRegistry({("docker.io/n8nio/n8n", "2.44.0"): N8N_244})
        plans = wvtbot.plan_propose(m, wvt.collect_drift(m, f, TODAY), f, reg, TODAY, only="n8n", force_version="2.44.0")
        self.assertEqual(plans[0]["version"], "2.44.0")
        self.assertIn("手動指定", plans[0]["reasons"][0])

    def test_emqx_artifacts_come_from_the_sha256_files(self):
        m = manifest()
        reg = FakeRegistry({("docker.io/emqx/emqx", "5.8.10"): {"index_digest": "sha256:" + "4" * 64,
                                                                 "platforms": {"amd64": "sha256:" + "5" * 64, "arm64": "sha256:" + "6" * 64}}})
        seen = []

        def get(url, headers):
            seen.append(url)
            return 200, {}, (("7" if "amd64" in url else "8") * 64).encode()
        t = wvtbot.new_target_for(m["products"]["emqx"], "5.8.10", TODAY, reg, get)
        self.assertEqual(t["artifacts"], {"debian13-amd64": "sha256:" + "7" * 64, "debian13-arm64": "sha256:" + "8" * 64})
        self.assertIn("https://github.com/emqx/emqx/releases/download/v5.8.10/emqx-5.8.10-debian13-amd64.tar.gz.sha256", seen)

    def test_shared_source_ahead_takes_the_import_closure(self):
        m = manifest()
        src = "gitea:woow-paas/woow-paas-charts"
        base = "charts/litellm/files/"
        head = "d3cd6898b29a9e094fd616bc151399c9884e5c19"
        contents = {"sitecustomize.py": b"import woow_body_limit\nimport woow_chatgpt\n", "woow_chatgpt.py": b"import woow_usage_limit\n",
                    "woow_chatgpt_plugin.py": b"import woow_chatgpt as core\n", "woow_body_limit.py": b"x = 1\n",
                    "woow_usage_limit.py": b"import woow_body_limit\n", "woow_revoke_ui_sessions.py": b"y = 2\n"}
        blobs = {n: wvt.git_blob_sha(c) for n, c in contents.items()}
        f = FakeFetcher(blobs=blobs)
        f.bytes_overrides = {(src, base + n, head): c for n, c in contents.items()}
        plan = next(p for p in wvtbot.plan_propose(m, wvt.collect_drift(m, f, TODAY), f, FakeRegistry({}), TODAY) if p["product"] == "litellm")
        s = plan["shared"][0]
        self.assertEqual(s["commit"], head)
        self.assertEqual(s["files"], ["sitecustomize.py", "woow_chatgpt.py", "woow_chatgpt_plugin.py", "woow_body_limit.py", "woow_usage_limit.py"])
        self.assertEqual(s["extra"], ["woow_revoke_ui_sessions.py"])
        title, body = wvtbot.render_propose(m, plan, None)
        self.assertIn("共用檔 4ad95a1 → d3cd689", title)
        self.assertIn("共用清單新增 2 個檔", body)
        sf = wvt.load_yaml(wvtbot.apply_propose_text(manifest_text(), plan, TODAY))["products"]["litellm"]["shared_files"][0]
        self.assertEqual(sf["source"]["blobs"]["woow_usage_limit.py"], blobs["woow_usage_limit.py"])


class FanoutTest(unittest.TestCase):
    def moved(self, product="n8n", version="2.44.0", target=None):
        new = target or dict(version=version, since="2026-10-24", **N8N_244)
        return wvtbot.check_manifest_text(wvtbot.set_target(manifest_text(), product, new), TODAY)

    def test_ha_n8n_pr_files(self):
        m = self.moved()
        plans = wvtbot.plan_fanout(m, FakeFetcher(), TODAY, "ha")
        plan = next(p for p in plans if p["product"] == "n8n")
        self.assertIsNone(plan["blocked"])
        self.assertEqual(plan["repo"], "github:WOOWTECH/Woow_ha_n8n")
        df = plan["files"]["n8n/Dockerfile"]
        self.assertEqual(wvt.extract_dockerfile_arg(df, "N8N_UPSTREAM_VERSION"), "2.44.0")
        self.assertEqual(wvt.extract_dockerfile_arg(df, "BUILD_VERSION"), "2.44.0")
        self.assertEqual(wvt.extract_yaml(plan["files"]["n8n/config.yaml"], "version"), "2.44.0")
        self.assertEqual([x["new"] for x in plan["package"]], ["2.44.0", "2.44.0"])
        title, body = wvtbot.render_fanout(m, plan, "abc1234", "github")
        self.assertEqual(title, "release train: n8n 2.43.6 → 2.44.0")
        self.assertIn("備份 → 升級 → 驗證 → 還原", body)

    def test_paas_seed_waits_for_the_mirror(self):
        m = self.moved()
        plan = next(p for p in wvtbot.plan_fanout(m, FakeFetcher(), TODAY, "paas") if p["product"] == "n8n")
        self.assertTrue(plan["needs_mirror"])
        self.assertIn("鏡像", plan["blocked"])
        plan = next(p for p in wvtbot.plan_fanout(m, FakeFetcher(), TODAY, "paas", mirrors={"n8n": "20261101.0101"}) if p["product"] == "n8n")
        self.assertIsNone(plan["blocked"])
        got = wvt.extract_seed_image(plan["files"]["src/data/cloud_app_templates.xml"], "cloud_app_n8n")
        self.assertEqual((got["tag"], got["digest"]), ("20261101.0101", N8N_244["platforms"]["amd64"]))

    def test_aligned_and_ahead_sides_are_left_alone(self):
        self.assertEqual([p for p in wvtbot.plan_fanout(manifest(), FakeFetcher(), TODAY, "ha")], [])
        older = manifest()["products"]["n8n"]["known_versions"][0]
        m = self.moved(target={"version": "2.38.0", "since": "2026-10-24", "index_digest": older["index_digest"], "platforms": older["platforms"]})
        self.assertEqual([p for p in wvtbot.plan_fanout(m, FakeFetcher(), TODAY, "ha") if p["product"] == "n8n"], [])

    def test_litellm_lock_resync(self):
        names = ["sitecustomize.py", "woow_chatgpt.py", "woow_chatgpt_plugin.py"]
        contents = {n: ("# %s at the new commit\n" % n).encode() for n in names}
        blobs = {n: wvt.git_blob_sha(c) for n, c in contents.items()}
        m = wvtbot.check_manifest_text(wvtbot.set_shared_commit(manifest_text(), "litellm", 0, "b" * 40, blobs, names), TODAY)
        f = FakeFetcher()
        f.bytes_overrides = {("gitea:woow-paas/woow-paas-charts", "charts/litellm/files/" + n, "b" * 40): c for n, c in contents.items()}
        plan = next(p for p in wvtbot.plan_fanout(m, f, TODAY, "ha") if p["product"] == "litellm")
        self.assertIsNone(plan["blocked"], plan["blocked"])
        lock = json.loads(plan["files"]["litellm/woow-plugin.lock.json"])
        self.assertEqual(lock["source"]["commit"], "b" * 40)
        self.assertEqual({n: v["git_blob"] for n, v in lock["files"].items()}, blobs)
        self.assertEqual(plan["files"]["litellm/rootfs/usr/share/woow-litellm/py/woow_chatgpt.py"], contents["woow_chatgpt.py"].decode())
        self.assertEqual(wvt.extract_yaml(plan["files"]["litellm/config.yaml"], "version"), "0.1.12")
        # the resynced copies pass the consumer check against the new manifest
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        shutil.copytree(os.path.join(FIX, "ha_litellm"), os.path.join(tmp, "r"))
        for path, text in plan["files"].items():
            os.makedirs(os.path.dirname(os.path.join(tmp, "r", path)), exist_ok=True)
            with open(os.path.join(tmp, "r", path), "w", encoding="utf-8") as fh:
                fh.write(text)
        sf = m["products"]["litellm"]["shared_files"][0]
        with open(os.path.join(tmp, "r", "litellm/woow-plugin.lock.json"), encoding="utf-8") as fh:
            fs = wvt.check_lock(m["products"]["litellm"], sf, sf["consumers"][0], fh.read(), os.path.join(tmp, "r"))
        self.assertEqual([x["status"] for x in fs], ["ok"], fs)


class PushBranchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.remote = os.path.join(self.tmp, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", self.remote], check=True)
        seed = os.path.join(self.tmp, "seed")
        subprocess.run(["git", "init", "-q", "-b", "main", seed], check=True)
        with open(os.path.join(seed, "a.txt"), "w") as fh:
            fh.write("one\n")
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
        subprocess.run(["git", "-C", seed, "add", "-A"], check=True)
        subprocess.run(["git", "-C", seed, "commit", "-q", "-m", "seed"], check=True, env=env)
        subprocess.run(["git", "-C", seed, "push", "-q", self.remote, "main"], check=True)

    def test_push_then_same_then_unchanged(self):
        auth = wvtbot.basic("bot", "not-a-real-token")
        st, sha = wvtbot.push_branch(self.remote, auth, "main", "wvt/fanout-n8n", {"a.txt": "two\n", "b/c.txt": "new\n"}, "msg")
        self.assertEqual(st, "pushed")
        show = subprocess.run(["git", "--git-dir", self.remote, "show", "wvt/fanout-n8n:a.txt"], capture_output=True, text=True).stdout
        self.assertEqual(show, "two\n")
        st2, sha2 = wvtbot.push_branch(self.remote, auth, "main", "wvt/fanout-n8n", {"a.txt": "two\n", "b/c.txt": "new\n"}, "msg")
        self.assertEqual((st2, sha2), ("same", sha))
        self.assertEqual(wvtbot.push_branch(self.remote, auth, "main", "wvt/x", {"a.txt": "one\n"}, "msg"), ("unchanged", None))


class MirrorStateTest(unittest.TestCase):
    def gitea(self, issues, comments):
        def call(method, path, body=None):
            if "/comments" in path:
                return comments.get(int(path.split("/issues/")[1].split("/")[0]), [])
            return issues
        return wvtbot.Gitea("http://gitea/api/v1", "t", call)

    def issue(self, n, state, ref, tag="20261101.0101"):
        return {"number": n, "state": state, "title": wvtbot.MIRROR_TITLE + " Build woow-paas-docker-local/n8n:" + tag,
                "body": "x\n<!-- approval-context\nIMAGE_TAG=%s\nADDON_REF=%s\n-->" % (tag, ref)}

    def test_states(self):
        ref = "docker.io/n8nio/n8n@sha256:" + "2" * 64
        self.assertEqual(wvtbot.mirror_state(self.gitea([], {}), ref)["state"], "none")
        self.assertEqual(wvtbot.mirror_state(self.gitea([self.issue(9, "open", ref)], {}), ref),
                         {"state": "pending", "issue": 9, "tag": "20261101.0101"})
        ok = {9: [{"body": "/approve"}, {"body": "✅ 已由 @eugene 放行，CI build 開始。"}]}
        self.assertEqual(wvtbot.mirror_state(self.gitea([self.issue(9, "closed", ref)], ok), ref)["state"], "approved")
        self.assertEqual(wvtbot.mirror_state(self.gitea([self.issue(9, "closed", ref)], {9: [{"body": "/reject"}]}), ref)["state"], "rejected")
        other = self.issue(10, "open", "docker.io/n8nio/n8n@sha256:" + "9" * 64)
        self.assertEqual(wvtbot.mirror_state(self.gitea([other], {}), ref)["state"], "none")


class RunTest(unittest.TestCase):
    def test_dry_run_reports_without_writing(self):
        orig_f, orig_r = wvtbot.BotFetcher, wvtbot.Registry

        class F(FakeFetcher):
            def __init__(self, *a, **k):
                super().__init__()
        wvtbot.BotFetcher, wvtbot.Registry = F, lambda: FakeRegistry({})
        self.addCleanup(setattr, wvtbot, "BotFetcher", orig_f)
        self.addCleanup(setattr, wvtbot, "Registry", orig_r)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = wvtbot.main(["--manifest", MANIFEST_PATH, "--today", "2026-10-24", "--mode", "dry-run", "--bot", "fanout_ha"])
        self.assertEqual(rc, 0)
        self.assertIn("fanout_ha（dry-run）", buf.getvalue())

    def test_manifest_bot_modes(self):
        m = manifest()
        self.assertEqual(wvtbot.bot_modes(m), {"propose": "open-pr", "fanout_ha": "dry-run", "fanout_paas": "dry-run"})
        m["bots"]["fanout_ha"] = "merge"
        self.assertTrue(any("bots.fanout_ha" in e for e in wvt.validate_manifest(m, TODAY)[0]))


if __name__ == "__main__":
    unittest.main()
