# 校验五评分数据包结构、默认参数与预设读取。
import json
import tempfile
import unittest
from pathlib import Path

from ww_echo_workshop.application.policy_data import PolicyDataStore
from ww_echo_workshop.core.policy import PolicyError
from ww_echo_workshop.core.policy_presets import (
    SYSTEMS,
    WEIGHT_KEYS,
    default_scoring,
    iter_presets,
    preset_by_id,
    validate_data_pack,
)


def _weights(value=1.0):
    return {key: value for key in WEIGHT_KEYS}


def _pack():
    return {
        'schema_version': 1,
        'version': 'test-1',
        'characters': {'测试角色': {'en': 'Test', 'release_date': ''}},
        'variant_names': {'默认': 'Default'},
        'presets': {system: {'presets': []} for system in SYSTEMS}
        | {
            'linear': {
                'presets': [
                    {
                        'presetName': '测试角色',
                        'variants': [
                            {
                                'id': 'linear-test',
                                'variantName': '默认',
                                'weights': _weights(),
                                'mainBuffScore': 0.0,
                                'normalizedMaxScore': 100.0,
                            }
                        ],
                    }
                ]
            }
        },
    }


class PolicyPresetContract(unittest.TestCase):
    def test_defaults_cover_all_supported_systems(self):
        self.assertEqual(set(SYSTEMS), set(default_scoring(system)['system'] for system in SYSTEMS))
        fixed = default_scoring('fixed')
        self.assertTrue(fixed['fixed'])
        self.assertEqual(fixed['target'], 7.0)
        self.assertEqual(default_scoring('mc_boost_assistant')['max_score'], 120.0)

    def test_pack_validation_and_preset_lookup(self):
        pack = validate_data_pack(_pack())
        preset = next(iter_presets(pack, 'linear'))
        self.assertEqual(preset.character, '测试角色')
        self.assertEqual(preset.preset_id, 'linear-test')
        self.assertEqual(len(preset.weights), 13)
        self.assertEqual(preset_by_id(pack, 'linear', 'linear-test').max_score, 100.0)

    def test_data_store_roundtrip_and_bad_pack_preserves_old(self):
        try:
            context = tempfile.TemporaryDirectory()
        except OSError:
            self.skipTest('当前解释器没有可写临时目录')
        with context as folder:
            store = PolicyDataStore(folder, builtin_path=Path(folder) / 'missing.json')
            self.assertIsNone(store.load())
            store.import_bytes(json.dumps(_pack(), ensure_ascii=False).encode())
            self.assertEqual(store.load()['version'], 'test-1')
            bad = _pack()
            bad['schema_version'] = 2
            with self.assertRaises(PolicyError):
                store.import_bytes(json.dumps(bad, ensure_ascii=False).encode())
            self.assertEqual(store.load()['version'], 'test-1')

    def test_bundled_starting_pack_is_valid(self):
        store = PolicyDataStore('unused')
        pack, source = store.load_with_source()
        self.assertEqual(source, 'builtin')
        self.assertEqual(pack['schema_version'], 1)
        self.assertTrue(set(SYSTEMS).issubset(pack['presets']))

    def test_incompatible_schema_and_missing_weights_are_rejected(self):
        bad = _pack()
        bad['schema_version'] = 2
        with self.assertRaises(PolicyError):
            validate_data_pack(bad)
        bad = _pack()
        bad['presets']['linear']['presets'][0]['variants'][0]['weights'].pop('Crit_Rate')
        with self.assertRaises(PolicyError):
            validate_data_pack(bad)
