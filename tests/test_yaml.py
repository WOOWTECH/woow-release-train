import unittest

from helpers import MANIFEST_PATH, fixture, wvt

try:  # optional cross-check; the runner does not need PyYAML
    import yaml as pyyaml
except ImportError:  # pragma: no cover
    pyyaml = None


SAMPLE = """\
# comment
schema: 1
name: "quoted # not a comment"
single: 'it''s'
plain: hello world  # trailing comment
ver: "5.8.9"
num: 42
flt: 1.5
yes_bool: true
nothing: null
empty:
flow_list: [a, "b c", 3]
flow_map: {k: v, n: [1, 2]}
nested:
  a:
    b: deep
list:
  - one
  - key: v
    other: w
  -
    - inner
block: |
  line one
  line two
folded: >-
  folded one
  folded two
multi: "first
  second

  third"
plain_multi:
  continues on
  the next line
"""


class YamlSubsetTest(unittest.TestCase):
    def test_sample(self):
        d = wvt.load_yaml(SAMPLE)
        self.assertEqual(d["name"], "quoted # not a comment")
        self.assertEqual(d["single"], "it's")
        self.assertEqual(d["plain"], "hello world")
        self.assertEqual(d["ver"], "5.8.9")
        self.assertEqual(d["num"], 42)
        self.assertEqual(d["flt"], 1.5)
        self.assertIs(d["yes_bool"], True)
        self.assertIsNone(d["nothing"])
        self.assertIsNone(d["empty"])
        self.assertEqual(d["flow_list"], ["a", "b c", 3])
        self.assertEqual(d["flow_map"], {"k": "v", "n": [1, 2]})
        self.assertEqual(d["nested"]["a"]["b"], "deep")
        self.assertEqual(d["list"], ["one", {"key": "v", "other": "w"}, ["inner"]])
        self.assertEqual(d["block"], "line one\nline two\n")
        self.assertEqual(d["folded"], "folded one folded two")
        self.assertEqual(d["multi"], "first second\nthird")
        self.assertEqual(d["plain_multi"], "continues on the next line")

    def test_duplicate_key_rejected(self):
        with self.assertRaises(wvt.WvtError):
            wvt.load_yaml("a: 1\na: 2\n")

    def test_bad_indentation_rejected(self):
        with self.assertRaises(wvt.WvtError):
            wvt.load_yaml("a: 1\n   b: 2\n")

    def test_chart_with_block_description(self):
        d = wvt.load_yaml(fixture("charts", "charts", "emqx", "Chart.yaml"))
        self.assertEqual(d["appVersion"], "5.8.9")
        self.assertIn("LICENSE PIN", d["description"])

    @unittest.skipIf(pyyaml is None, "PyYAML not installed")
    def test_matches_pyyaml(self):
        with open(MANIFEST_PATH, encoding="utf-8") as fh:
            manifest_text = fh.read()
        for text in (SAMPLE, manifest_text,
                     fixture("charts", "charts", "emqx", "Chart.yaml"),
                     fixture("charts", "charts", "litellm", "Chart.yaml"),
                     fixture("charts", "charts", "litellm", "values.yaml"),
                     fixture("ha_emqx", "emqx", "config.yaml"),
                     fixture("cd", "paas-odoo-platform", "values-prod.yaml")):
            self.assertEqual(wvt.load_yaml(text), pyyaml.safe_load(text))


if __name__ == "__main__":
    unittest.main()
