# 提供角色设置使用的默认武器解析和静态面板入口。
from __future__ import annotations

from echo_workshop.core.panel import calculate_panel, display_stat_name, panel_breakdown


class PanelService:
    def __init__(self, catalog, effects=None):
        self.catalog = catalog
        self.effects = effects

    def _effects(self):
        return getattr(self.effects, 'active', self.effects) if self.effects is not None else None

    def resolve_weapon(self, character_id, weapon_id=''):
        character = self.catalog.character(character_id)
        if character is None:
            raise ValueError('角色不存在或资料未加载')
        if weapon_id:
            weapon = self.catalog.weapon(weapon_id)
            if weapon is None:
                raise ValueError('武器不存在或资料未加载')
            return weapon_id, weapon
        default = self.catalog.default_weapon(character_id)
        if not default:
            return '', None
        return default, self.catalog.weapon(default)

    def calculate(self, character_id, weapon_id='', echoes=()):
        character = self.catalog.character(character_id)
        if character is None:
            raise ValueError('角色不存在或资料未加载')
        resolved_id, weapon = self.resolve_weapon(character_id, weapon_id)
        return resolved_id, calculate_panel(character, weapon, echoes, self._effects())

    def breakdown(self, character_id, weapon_id='', echoes=()):
        character = self.catalog.character(character_id)
        if character is None:
            raise ValueError('角色不存在或资料未加载')
        resolved_id, weapon = self.resolve_weapon(character_id, weapon_id)
        return resolved_id, panel_breakdown(character, weapon, echoes, self._effects())

    @staticmethod
    def describe(detail):
        parts = []
        if detail.skill_bonus:
            parts.append(
                '技能树 '
                + '，'.join(
                    f'{display_stat_name(name)} {value:g}'
                    for name, value in detail.skill_bonus.items()
                )
            )
        if detail.weapon.get('substat'):
            parts.append(
                f'武器 {detail.weapon.get("substat")} {detail.weapon.get("substat_value"):g}'
            )
        if detail.weapon_passive:
            parts.append(
                '武器被动 '
                + '，'.join(
                    f'{display_stat_name(name)} {value:g}'
                    for name, value in detail.weapon_passive.items()
                )
            )
        if detail.sonata:
            parts.append(
                '套装 '
                + '，'.join(
                    f'{display_stat_name(name)} {value:g}' for name, value in detail.sonata.items()
                )
            )
        if detail.echoes:
            parts.append(
                '声骸 '
                + '，'.join(
                    f'{display_stat_name(name)} {value:g}'
                    for name, value in list(detail.echoes.items())[:4]
                )
            )
        return '；分项：' + '；'.join(parts) if parts else ''

    def summary(self, character_id, weapon_id='', echoes=()):
        resolved_id, totals = self.calculate(character_id, weapon_id, echoes)
        return {
            'character_id': str(character_id),
            'weapon_id': resolved_id,
            'hp': totals.hp,
            'atk': totals.atk,
            'defense': totals.defense,
            'crit_rate': totals.crit_rate,
            'crit_dmg': totals.crit_dmg,
            'energy_regen': totals.energy_regen,
        }
