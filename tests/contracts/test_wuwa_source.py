# 校验 Wuwa base.js 的静态提取和合并逻辑，不执行 JavaScript。
import json
import unittest
from unittest.mock import patch

from ww_echo_workshop.application.wuwa_source import (
    _new_coefficients,
    _weights,
    extract_wuwa_presets,
    merge_wuwa_pack,
)
from ww_echo_workshop.core.policy_presets import SYSTEMS, validate_data_pack
from ww_echo_workshop.resources.wuwa_updates import WuwaUpdateService

SOURCE = """
const toolVersion = "test-1";
const roleList = [
    {
        "id": 1,
        "name": "今汐",
        "rule": 0,
        "normal": 0.02,
        "skill": 0.72,
        "heavy": 0,
        "liberate": 0.2,
        "other": 0.06,
        "maxscore": 490.0
    }
];
const ruleList = [
    {
        "ruleId": 1,
        "attack01": 1,
        "attack02": 0.1,
        "crit": 1.8,
        "critDamage": 0.9,
        "property": 1,
        "health01": 0,
        "health02": 0,
        "defense01": 0,
        "defense02": 0,
        "defenseLimit": 40,
        "efficiency01": 0.5,
        "efficiency02": 0,
        "unike": 1,
        "treat": 0
    }
];
const RoleSumProperty = [];
"""


class WuwaSourceContract(unittest.TestCase):
    def test_legacy_extraction_is_static_and_scaled(self):
        version, groups = extract_wuwa_presets(SOURCE)
        self.assertEqual(version, 'test-1')
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['presetName'], '今汐')
        self.assertEqual(len(groups[0]['variants']), 7)
        self.assertEqual(groups[0]['variants'][0]['weights']['Crit_Rate'], 189.0)
        self.assertEqual(groups[0]['variants'][0]['weights']['Skill_Damage'], 83.52)

    def test_new_model_coefficients_match_upstream_snapshot(self):
        coefficients = _new_coefficients(57, 0, 'quick')
        expected = [1.728996, 0.768443, 0.9375, 0, 0, 0.09375, 0, 0, 0.5, 0.1875, 0, 0.625, 0.28125]
        for actual, wanted in zip(coefficients, expected):
            self.assertAlmostEqual(actual, wanted, places=5)
        self.assertEqual(_weights(coefficients)['Crit_Rate'], 181.54)

    def test_merge_replaces_only_wuwa_presets(self):
        base = {
            'schema_version': 1,
            'version': 'base-1',
            'characters': {},
            'variant_names': {},
            'descriptions': {},
            'presets': {system: {'presets': []} for system in SYSTEMS},
        }
        _, groups = extract_wuwa_presets(SOURCE)
        merged = merge_wuwa_pack(base, groups, 'test-1')
        self.assertEqual(merged['version'], 'base-1+wuwa.test-1')
        self.assertEqual(len(merged['presets']['wuwa_echo_tool']['presets']), 1)
        self.assertEqual(merged['presets']['linear']['presets'], [])
        validate_data_pack(merged)

    def test_updater_fetches_and_merges_without_executing(self):
        base = {
            'schema_version': 1,
            'version': 'base-1',
            'characters': {},
            'variant_names': {},
            'descriptions': {},
            'presets': {system: {'presets': []} for system in SYSTEMS},
        }

        class Store:
            def __init__(self):
                self.content = None

            def load_with_source(self):
                return base, 'builtin'

            def import_bytes(self, content):
                self.content = json.loads(content)
                return self.content

        store = Store()
        with patch(
            'ww_echo_workshop.resources.wuwa_updates.fetch',
            return_value=('https://example.com/base.js', SOURCE.encode()),
        ):
            final, version, roles = WuwaUpdateService(store).update('https://example.com/base.js')
        self.assertEqual(final, 'https://example.com/base.js')
        self.assertEqual(version, 'test-1')
        self.assertEqual(roles, 1)
        self.assertEqual(store.content['version'], 'base-1+wuwa.test-1')

    def test_missing_literal_is_rejected(self):
        with self.assertRaises(Exception):
            extract_wuwa_presets('const toolVersion = "x";')


if __name__ == '__main__':
    unittest.main()
