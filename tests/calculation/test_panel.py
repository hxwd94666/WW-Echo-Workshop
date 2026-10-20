# 验证静态面板只合并角色/武器/声骸的显式属性。
import unittest
from uuid import uuid4

from echo_workshop.application.panel import PanelService
from echo_workshop.core.models import EchoRecord, StatValue
from echo_workshop.core.panel import calculate_panel


class PanelCalculationTest(unittest.TestCase):
    def test_static_panel_adds_weapon_and_echo_contributions(self):
        character = {
            'hp_90': 1000.0,
            'atk_90': 100.0,
            'def_90': 100.0,
            'crit_rate': 5.0,
            'crit_dmg': 150.0,
            'energy_regen': 100.0,
        }
        weapon = {
            'atk_90': 100.0,
            'substat': '攻击',
            'substat_value': 20.0,
            'substat_is_percent': True,
        }
        echo = EchoRecord(
            profile_id=str(uuid4()),
            cost=1,
            main_stats=(StatValue('攻击%', 10.0),),
            sub_stats=(StatValue('暴击', 5.0), StatValue('共鸣效率', 10.0)),
        )
        result = calculate_panel(character, weapon, (echo,))
        self.assertAlmostEqual(result.atk, 260.0)
        self.assertAlmostEqual(result.crit_rate, 10.0)
        self.assertAlmostEqual(result.energy_regen, 110.0)
        self.assertAlmostEqual(result.hp, 1000.0)

    def test_inherent_stats_are_applied_to_static_panel(self):
        character = {
            'hp_90': 100.0,
            'atk_90': 100.0,
            'def_90': 100.0,
            'crit_rate': 5.0,
            'crit_dmg': 150.0,
            'energy_regen': 100.0,
            'bonus_stats': {'攻击%': 6.0, '共鸣效率': 6.0},
        }
        result = calculate_panel(character, None, ())
        self.assertAlmostEqual(result.atk, 106.0)
        self.assertAlmostEqual(result.energy_regen, 106.0)

    def test_simple_two_piece_set_bonus_is_applied(self):
        class Effects:
            def descriptions(self, name):
                return {2: '攻击提升10%'} if name == '测试套装' else {}

        character = {
            'hp_90': 100.0,
            'atk_90': 100.0,
            'def_90': 100.0,
            'crit_rate': 5.0,
            'crit_dmg': 150.0,
            'energy_regen': 100.0,
        }
        echoes = tuple(
            EchoRecord(
                profile_id=str(uuid4()),
                cost=1,
                main_stats=(),
                sonata='测试套装',
            )
            for _ in range(2)
        )
        result = calculate_panel(character, None, echoes, Effects())
        self.assertAlmostEqual(result.atk, 110.0)

    def test_simple_unconditional_weapon_passive_is_applied(self):
        character = {
            'hp_90': 100.0,
            'atk_90': 100.0,
            'def_90': 100.0,
            'crit_rate': 5.0,
            'crit_dmg': 150.0,
            'energy_regen': 100.0,
        }
        weapon = {
            'atk_90': 0.0,
            'substat': '暴击',
            'substat_value': 0.0,
            'substat_is_percent': True,
            'effect': '攻击提升{0}。',
            'param': [['10%']],
        }
        result = calculate_panel(character, weapon, ())
        self.assertAlmostEqual(result.atk, 110.0)

    def test_service_uses_default_weapon_and_manual_override(self):
        class Catalog:
            def character(self, character_id):
                return {
                    'hp_90': 1000.0,
                    'atk_90': 100.0,
                    'def_90': 100.0,
                    'crit_rate': 5.0,
                    'crit_dmg': 150.0,
                    'energy_regen': 100.0,
                }

            def weapon(self, weapon_id):
                return {
                    'atk_90': 100.0 if weapon_id == 'default' else 200.0,
                    'substat': '攻击',
                    'substat_value': 0.0,
                    'substat_is_percent': True,
                }

            def default_weapon(self, character_id):
                return 'default'

        service = PanelService(Catalog())
        default_id, default = service.calculate('1102')
        self.assertEqual(default_id, 'default')
        self.assertEqual(default.atk, 200.0)
        manual_id, manual = service.calculate('1102', 'manual')
        self.assertEqual(manual_id, 'manual')
        self.assertEqual(manual.atk, 300.0)
