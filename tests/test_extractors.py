import unittest

from helpers import fixture, wvt

N8N_238_AMD64 = "sha256:e0e7b603b43adc48d7123716b9eee7394dca675b410f574e4d24830e114564ce"
LITELLM_INDEX = "sha256:37066242e6b8327fdd627682a66a9ae2a599553b4d5a84c905c6d8b9eb35c01e"


class DockerfileArgTest(unittest.TestCase):
    def test_quoted_value(self):
        self.assertEqual(wvt.extract_dockerfile_arg(fixture("ha_emqx", "emqx", "Dockerfile"), "EMQX_VERSION"), "v5.8.9")

    def test_plain_value(self):
        self.assertEqual(wvt.extract_dockerfile_arg(fixture("ha_n8n", "n8n", "Dockerfile"), "N8N_UPSTREAM_VERSION"), "2.43.6")

    def test_substitution_and_first_definition(self):
        text = "ARG V=1.2.3\nARG IMG=repo:${V}\nFROM x\nARG V\nARG V=9.9.9\n"
        self.assertEqual(wvt.extract_dockerfile_arg(text, "IMG"), "repo:1.2.3")
        self.assertEqual(wvt.extract_dockerfile_arg(text, "V"), "1.2.3")

    def test_missing(self):
        self.assertIsNone(wvt.extract_dockerfile_arg("FROM scratch\n", "NOPE"))

    def test_image_ref_observation(self):
        raw = wvt.extract_dockerfile_arg(fixture("ha_litellm", "litellm", "Dockerfile"), "LITELLM_IMAGE")
        obs = wvt.observe({"parse": "image_ref"}, raw)
        self.assertEqual(obs["version"], "1.104.0")
        self.assertEqual(obs["digest"], LITELLM_INDEX)

    def test_digest_only_ref(self):
        raw = wvt.extract_dockerfile_arg(fixture("ci", "litellm", "Dockerfile"), "BASE")
        obs = wvt.observe({"parse": "image_ref"}, raw)
        self.assertIsNone(obs["version"])
        self.assertEqual(obs["digest"], LITELLM_INDEX)

    def test_bare_sha256_normalised(self):
        raw = wvt.extract_dockerfile_arg(fixture("ha_emqx", "emqx", "Dockerfile"), "EMQX_SHA256_AMD64")
        obs = wvt.observe({"parse": "digest"}, raw)
        self.assertEqual(obs["digest"], "sha256:" + raw)


class YamlJsonExtractorTest(unittest.TestCase):
    def test_yaml_path(self):
        self.assertEqual(wvt.extract_yaml(fixture("charts", "charts", "litellm", "Chart.yaml"), "appVersion"), "1.104.0")
        self.assertEqual(wvt.extract_yaml(fixture("charts", "charts", "litellm", "values.yaml"), "image.digest"), LITELLM_INDEX)
        self.assertIsNone(wvt.extract_yaml("a: {b: 1}\n", "a.c"))

    def test_yaml_package_version_is_string_when_quoted(self):
        self.assertEqual(wvt.extract_yaml(fixture("ha_emqx", "emqx", "config.yaml"), "version"), "5.9.1")

    def test_json_path(self):
        self.assertEqual(wvt.extract_json(fixture("ha_litellm", "litellm", "woow-plugin.lock.json"), "source.chart_version"), "0.3.5")


class SeedExtractorTest(unittest.TestCase):
    def setUp(self):
        self.xml = fixture("paas_platform", "src", "data", "cloud_app_templates.xml")

    def test_seed_image_tag_and_digest(self):
        img = wvt.extract_seed_image(self.xml, "cloud_app_n8n")
        self.assertEqual(img["repository"], "jcr-prod.woowtech.io/woow-paas-docker-local/n8n")
        self.assertEqual(img["tag"], "20261013.2012")
        self.assertEqual(img["digest"], N8N_238_AMD64)

    def test_seed_field(self):
        self.assertEqual(wvt.extract_seed_field(self.xml, "cloud_app_emqx", "helm_chart_version"), "0.1.7")
        self.assertEqual(wvt.extract_seed_field(self.xml, "cloud_app_n8n", "helm_chart_version"), "2.0.1")

    def test_unknown_record(self):
        self.assertIsNone(wvt.extract_seed_image(self.xml, "cloud_app_nope"))
        self.assertIsNone(wvt.extract_seed_field(self.xml, "cloud_app_nope", "x"))


class CatalogAndCdTest(unittest.TestCase):
    def test_catalog(self):
        c = wvt.extract_catalog(fixture("store", ".addon-sync", "catalog.json"), "woow-n8n")
        self.assertEqual(c["version"], "2.43.6")
        self.assertEqual(c["source_sha"], "d20a13315958499789d3843e787e6cae86cf3a34")
        self.assertIsNone(wvt.extract_catalog(fixture("store", ".addon-sync", "catalog.json"), "nope"))

    def test_cd_addon_ref_takes_first_mention(self):
        info = wvt.extract_addon_ref(fixture("cd", "paas-odoo-platform", "values-prod.yaml"))
        self.assertEqual(info["tag"], "v18.0.1.0.242")
        self.assertEqual(info["image_tag"], "20261015.1258")
        self.assertTrue(info["image_digest"].startswith("sha256:b523b7c2"))

    def test_cd_addon_ref_missing(self):
        self.assertIsNone(wvt.extract_addon_ref("image:\n  tag: x\n")["tag"])


class ImageRefTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(wvt.parse_image_ref("a.io/r/x:1.0@sha256:" + "a" * 64),
                         {"repository": "a.io/r/x", "tag": "1.0", "digest": "sha256:" + "a" * 64})
        self.assertEqual(wvt.parse_image_ref("localhost:5000/x")["tag"], None)
        self.assertEqual(wvt.parse_image_ref("20261013.2012@sha256:" + "b" * 64)["tag"], "20261013.2012")


if __name__ == "__main__":
    unittest.main()
