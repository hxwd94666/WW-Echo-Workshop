# 验证角色/武器面板快照、来源解析和受限地址。
import json
from pathlib import Path
import tempfile
import unittest

from echo_workshop.resources import panel_sources
from echo_workshop.resources.panel_catalog import PanelCatalog, PanelSnapshot
from echo_workshop.resources.panel_updates import PanelCatalogUpdates


def snapshot_data():
    return {
        'schema_version': 1,
        'game_version': '3.7',
        'retrieved_at': 1,
        'characters': {
            '1102': {
                'name': '散华',
                'element': 1,
                'weapon_type': 2,
                'hp_90': 10062.5,
                'atk_90': 275.0,
                'def_90': 941.1094,
                'crit_rate': 5.0,
                'crit_dmg': 150.0,
                'energy_regen': 100.0,
                'recommended_weapon_ids': ['21020015'],
                'default_weapon_id': '21020015',
                'source': 'nanoka',
            }
        },
        'weapons': {
            '21020015': {
                'name': '千古洑流',
                'type': 2,
                'rarity': 5,
                'atk_90': 587.5,
                'substat': '暴击',
                'substat_value': 24.3,
                'substat_is_percent': True,
                'effect': '共鸣效率提升{0}。',
                'param': [['12.8%']],
                'source': 'nanoka',
            }
        },
        'provenance': {'nanoka': '3.7'},
    }


class PanelSourcesContract(unittest.TestCase):
    def test_nanoka_character_selects_level_90_and_default_weapon(self):
        row = panel_sources.parse_nanoka_character(
            {
                'name': '散华',
                'element': 1,
                'weapon': 2,
                'stats': {'6': {'90': {'life': 10062.5, 'atk': 275, 'def': 941.1094}}},
                'recommend': {'weapon': [21020015, 21020044]},
            }
        )
        self.assertEqual(row['hp_90'], 10062.5)
        self.assertEqual(row['default_weapon_id'], '21020015')
        self.assertEqual(row['recommended_weapon_ids'], ['21020015', '21020044'])

    def test_nanoka_bonus_stats_stack_all_forte_branches(self):
        row = panel_sources.parse_nanoka_character(
            {
                'name': '测试角色',
                'stats': {'6': {'90': {'life': 1, 'atk': 100, 'def': 1}}},
                'skill_trees': {
                    '1': {'node_type': 2},
                    '2': {'node_type': 2},
                    '9': {
                        'node_type': 4,
                        'parent_nodes': ['1'],
                        'skill': {'name': '攻击提升', 'param': ['1.80%']},
                    },
                    '12': {
                        'node_type': 4,
                        'parent_nodes': ['2'],
                        'skill': {'name': '攻击提升', 'param': ['1.80%']},
                    },
                    '13': {
                        'node_type': 4,
                        'parent_nodes': ['9'],
                        'skill': {'name': '攻击提升', 'param': ['4.20%']},
                    },
                    '16': {
                        'node_type': 4,
                        'parent_nodes': ['12'],
                        'skill': {'name': '攻击提升', 'param': ['4.20%']},
                    },
                },
            }
        )
        self.assertEqual(row['bonus_stats']['攻击%'], 12.0)

    def test_nanoka_weapon_normalizes_percent_stats(self):
        row = panel_sources.parse_nanoka_weapon(
            {
                'name': '千古洑流',
                'type': 2,
                'rarity': 5,
                'stats': {
                    '6': {
                        '90': [
                            {
                                'name': '攻击',
                                'value': 587.5,
                                'is_ratio': False,
                                'is_percent': False,
                            },
                            {'name': '暴击', 'value': 2430, 'is_ratio': False, 'is_percent': True},
                        ]
                    }
                },
            }
        )
        self.assertEqual(row['atk_90'], 587.5)
        self.assertEqual(row['substat_value'], 24.3)
        self.assertTrue(row['substat_is_percent'])

    def test_encore_character_reads_level_90_properties(self):
        row = panel_sources.parse_encore_character(
            {
                'Name': {'Content': '秧秧'},
                'WeaponType': 2,
                'Properties': [
                    {'Name': '生命', 'GrowthValues': [{'level': 90, 'value': 10200}]},
                    {'Name': '攻击', 'GrowthValues': [{'level': 90, 'value': 250}]},
                    {'Name': '防御', 'GrowthValues': [{'level': 90, 'value': 1099.998}]},
                    {'Name': '暴击', 'GrowthValues': [{'level': 90, 'value': '5%'}]},
                    {'Name': '暴击伤害', 'GrowthValues': [{'level': 90, 'value': '150%'}]},
                ],
            }
        )
        self.assertEqual(row['hp_90'], 10200)
        self.assertEqual(row['crit_rate'], 5.0)
        self.assertEqual(row['crit_dmg'], 150.0)

    def test_panel_url_allowlist(self):
        panel_sources.approve_panel_url('https://static.nanoka.cc/ww/3.7/zh/character/1102.json')
        panel_sources.approve_panel_url(
            'https://ww1.loping151.top/XutheringWavesUID/resource/map/detail_json/char/1108.json'
        )
        panel_sources.approve_panel_url(
            'https://api-v2.encore.moe/api/zh-Hans/character/1402?ver=latest'
        )
        with self.assertRaises(ValueError):
            panel_sources.approve_panel_url('https://example.com/char.json')
        with self.assertRaises(ValueError):
            panel_sources.approve_panel_url(
                'https://api-v2.encore.moe/api/zh-Hans/character/1402?ver=other'
            )


class PanelCatalogContract(unittest.TestCase):
    def test_snapshot_roundtrip_and_default_weapon(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            builtin = root / 'builtin.json'
            builtin.write_text(json.dumps(snapshot_data(), ensure_ascii=False), encoding='utf-8')
            catalog = PanelCatalog(root / 'data', builtin=builtin)
            self.assertEqual(catalog.default_weapon('1102'), '21020015')
            self.assertEqual(catalog.weapon('21020015')['atk_90'], 587.5)
            changed = catalog.activate(snapshot_data(), retrieved_at=2)
            self.assertTrue(changed)
            self.assertEqual(catalog.active.game_version, '3.7')
            self.assertTrue((root / 'data/panel-resources/current.json').is_file())

    def test_update_service_skips_same_release_without_full_fetch(self):
        snapshot = PanelSnapshot.from_data(snapshot_data())

        class Provider:
            active = snapshot

        class Client:
            requests = 0

            def release(self, cancel_event=None):
                return '3.7'

            def fetch(self, **kwargs):
                raise AssertionError('same release must not fetch details')

        updates = PanelCatalogUpdates(Provider(), client=Client())
        self.assertFalse(updates.check_once())
        self.assertEqual(updates.status.state, 'current')

    def test_snapshot_rejects_default_weapon_missing_from_catalog(self):
        data = snapshot_data()
        data['weapons'] = {}
        with self.assertRaises(ValueError):
            PanelSnapshot.from_data(data)
