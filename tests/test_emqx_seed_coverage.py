"""EMQX 的獨立映像來源須可見，不由八映像的共同 commit 代替。"""
import copy
import datetime as dt
import json
import unittest
from helpers import LIVE_MANIFEST_PATH, wvt

class EmqxSeedCoverageTest(unittest.TestCase):
    def setUp(self):
        self.product = wvt.load_manifest(LIVE_MANIFEST_PATH)['products']['woow-mcp-server']
        self.pin = next(p for p in self.product['legs']['paas']['pins'] if p['id'] == 'emqx')

    def image(self, tag=None, digest=None):
        return {'repository': 'registry/emqx-mcp-admin',
                'tag': tag or self.product['target']['identity']['emqx_commit'],
                'digest': digest or self.product['target']['artifacts']['emqx-mcp-admin'],
                'raw': 'registry/emqx-mcp-admin:test'}

    def evaluate(self, **kwargs):
        return wvt.evaluate(self.product, 'paas', self.pin,
                            wvt.observe(self.pin, self.image(**kwargs)), dt.date(2026, 10, 30))

    def test_actual_independent_source_and_digest_are_checked(self):
        self.assertEqual(self.pin['compare'], 'identity.emqx_commit')
        self.assertEqual(self.pin['digest'], 'artifacts.emqx-mcp-admin')
        self.assertEqual(self.evaluate()['status'], 'ok')
        self.assertNotEqual(self.evaluate(tag=self.product['target']['identity']['commit'])['status'], 'ok')
        self.assertEqual(self.evaluate(digest='sha256:' + '0' * 64)['status'], 'mismatch')

    def test_coverage_is_a_pin_not_an_exclusion(self):
        value = json.dumps({'image': self.image()})
        xml = '<odoo><record id="cloud_app_mcp_emqx"><field name="helm_default_values">%s</field></record></odoo>' % value
        self.assertNotIn('cloud_app_mcp_emqx', self.product['legs']['paas']['coverage']['exclude'])
        self.assertFalse(any(f['status'] == 'unknown' for f in wvt.coverage_findings(self.product, 'paas', xml)))
        missing = copy.deepcopy(self.product)
        missing['legs']['paas']['pins'] = [p for p in missing['legs']['paas']['pins'] if p['id'] != 'emqx']
        self.assertTrue(any(f['status'] == 'unknown' for f in wvt.coverage_findings(missing, 'paas', xml)))

    def test_eight_common_source_pins_stay_separate(self):
        common = [p for p in self.product['legs']['paas']['pins'] if p['id'] != 'emqx']
        self.assertEqual({p['id'] for p in common}, {'hermes', 'odoo', 'n8n', 'k8s', 'vk', 'opendesign', 'litellm', 'nextcloud'})
        self.assertTrue(all(p['compare'] == 'identity.commit' for p in common))
        self.assertFalse(self.product['legs']['paas']['fanout']['enabled'])
        self.assertEqual(self.product['policy']['hard_rules'], [])

    def test_next_eight_target_preserves_emqx_without_duplicating_history(self):
        m = wvt.load_manifest(LIVE_MANIFEST_PATH)
        p = m['products']['woow-mcp-server']
        old = copy.deepcopy(p['target'])
        del old['identity']['emqx_commit'], old['artifacts']['emqx-mcp-admin']
        p.setdefault('known_versions', []).insert(0, old)
        p['target']['version'] = '2026.10.30.2359'
        p['target']['identity']['commit'] = 'e' * 40
        for i, name in enumerate(k for k in p['target']['artifacts'] if k != 'emqx-mcp-admin'):
            p['target']['artifacts'][name] = 'sha256:' + ('%064x' % (100 + i))
        errors, _ = wvt.validate_manifest(m, dt.date(2026, 10, 30), expiry=False)
        self.assertEqual(errors, [])
        self.assertEqual(p['target']['identity']['emqx_commit'], self.product['target']['identity']['emqx_commit'])
        self.assertEqual(p['target']['artifacts']['emqx-mcp-admin'], self.product['target']['artifacts']['emqx-mcp-admin'])

if __name__ == '__main__':
    unittest.main()
