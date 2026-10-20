# 计算角色、武器与声骸的静态面板总值。
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re

from echo_workshop.core.models import EchoRecord


_STAT_NAMES = {
    '普攻': '普攻伤害加成',
    '重击': '重击伤害加成',
    '共鸣技能': '共鸣技能伤害加成',
    '共鸣解放': '共鸣解放伤害加成',
    '变奏技能': '变奏技能伤害加成',
    '延奏技能': '延奏技能伤害加成',
    '声骸技能': '声骸技能伤害加成',
    '技能': '技能伤害加成',
    '治疗': '治疗效果加成',
    '热熔': '热熔伤害加成',
    '冷凝': '冷凝伤害加成',
    '导电': '导电伤害加成',
    '气动': '气动伤害加成',
    '衍射': '衍射伤害加成',
    '湮灭': '湮灭伤害加成',
    '全属性': '全属性伤害加成',
}


def display_stat_name(name):
    """Restore complete stat labels used by the game panel and source lists."""
    return _STAT_NAMES.get(name, name)


@dataclass(frozen=True)
class PanelBreakdown:
    character: dict
    skill_bonus: dict
    weapon: dict
    weapon_passive: dict
    echoes: dict
    sonata: dict
    totals: PanelTotals


@dataclass(frozen=True)
class PanelTotals:
    hp: float
    atk: float
    defense: float
    crit_rate: float
    crit_dmg: float
    energy_regen: float


def _number(row, key, label):
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{label}无效')
    return float(value)


def _add_stat(bucket, name, value, *, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{label}无效')
    value = float(value)
    if name in ('生命%', '攻击%', '防御%', '暴击', '暴击伤害', '共鸣效率'):
        bucket[name] += value
    elif name in ('生命', '攻击', '防御'):
        bucket[name] += value
    elif name in (
        '声骸技能伤害加成',
        '普攻伤害加成',
        '重击伤害加成',
        '共鸣技能伤害加成',
        '共鸣解放伤害加成',
        '冷凝伤害加成',
        '热熔伤害加成',
        '导电伤害加成',
        '气动伤害加成',
        '衍射伤害加成',
        '湮灭伤害加成',
        '治疗效果加成',
    ):
        # 这些属性暂不进入 HP/ATK/DEF/暴击/ER 的静态面板；保留输入但不误加。
        return
    else:
        return


def weapon_static_bonus(weapon):
    if not isinstance(weapon, dict):
        return {}
    effect = weapon.get('effect')
    params = weapon.get('param')
    if not isinstance(effect, str) or not isinstance(params, list) or not params:
        return {}
    first = effect.split('。', 1)[0]
    match = re.fullmatch(r'(攻击|生命|防御|暴击|暴击伤害|共鸣效率)提升\{0\}', first)
    if match is None or not isinstance(params[0], list) or not params[0]:
        return {}
    raw = params[0][0]
    if not isinstance(raw, str) or not raw.endswith('%'):
        return {}
    try:
        value = float(raw[:-1])
    except ValueError:
        return {}
    name = {'攻击': '攻击%', '生命': '生命%', '防御': '防御%'}.get(match.group(1), match.group(1))
    return {name: value}


def sonata_static_bonus(effects, echoes):
    """Parse only simple unconditional 2-piece effects; conditional tiers are excluded."""
    if effects is None:
        return {}
    counts = Counter(echo.sonata for echo in echoes if echo.sonata)
    result = {}
    mapping = {
        '攻击': '攻击%',
        '生命': '生命%',
        '防御': '防御%',
        '暴击': '暴击',
        '暴击伤害': '暴击伤害',
        '共鸣效率': '共鸣效率',
    }
    for name, count in counts.items():
        if count < 2:
            continue
        try:
            tiers = effects.descriptions(name)
            description = tiers.get(2) or tiers.get('2') or ''
        except (AttributeError, KeyError, TypeError):
            continue
        match = re.fullmatch(r'(.+?)(?:伤害加成|提升)([0-9]+(?:\.[0-9]+)?)%', description)
        if match is None:
            continue
        stat, raw = match.groups()
        stat = stat.removesuffix('加成')
        if stat not in mapping:
            continue
        result[mapping[stat]] = result.get(mapping[stat], 0.0) + float(raw)
    return result


def _weapon_stat(weapon):
    if not weapon:
        return None, None
    name = weapon.get('substat')
    value = weapon.get('substat_value')
    if not isinstance(name, str) or name not in (
        '生命',
        '攻击',
        '防御',
        '暴击',
        '暴击伤害',
        '共鸣效率',
    ):
        return None, None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('武器副词条数值无效')
    if weapon.get('substat_is_percent'):
        name = name + '%' if name in ('生命', '攻击', '防御') else name
    return name, float(value)


def calculate_panel(character, weapon, echoes, effects=None):
    """Return a static panel; conditional weapon/set effects are intentionally excluded."""
    if not isinstance(character, dict):
        raise ValueError('角色面板数据无效')
    bucket = {
        key: 0.0
        for key in (
            '生命%',
            '攻击%',
            '防御%',
            '生命',
            '攻击',
            '防御',
            '暴击',
            '暴击伤害',
            '共鸣效率',
        )
    }
    for name, value in character.get('bonus_stats', {}).items():
        _add_stat(bucket, name, value, label='技能树被动属性')
    for name, value in sonata_static_bonus(effects, echoes).items():
        _add_stat(bucket, name, value, label='套装无条件属性')
    for echo in echoes:
        if not isinstance(echo, EchoRecord):
            raise ValueError('声骸记录无效')
        for stat in echo.main_stats + echo.sub_stats:
            _add_stat(bucket, stat.stat_name, stat.value, label='声骸词条')
    name, value = _weapon_stat(weapon)
    if name:
        _add_stat(bucket, name, value, label='武器副词条')
    for name, value in weapon_static_bonus(weapon).items():
        _add_stat(bucket, name, value, label='武器无条件被动')
    hp_base = _number(character, 'hp_90', '角色生命')
    atk_base = _number(character, 'atk_90', '角色攻击')
    def_base = _number(character, 'def_90', '角色防御')
    if weapon:
        atk_base += _number(weapon, 'atk_90', '武器攻击')
    return PanelTotals(
        hp=hp_base * (1 + bucket['生命%'] / 100.0) + bucket['生命'],
        atk=atk_base * (1 + bucket['攻击%'] / 100.0) + bucket['攻击'],
        defense=def_base * (1 + bucket['防御%'] / 100.0) + bucket['防御'],
        crit_rate=_number(character, 'crit_rate', '角色暴击率') + bucket['暴击'],
        crit_dmg=_number(character, 'crit_dmg', '角色暴击伤害') + bucket['暴击伤害'],
        energy_regen=_number(character, 'energy_regen', '角色共鸣效率') + bucket['共鸣效率'],
    )


def panel_breakdown(character, weapon, echoes, effects=None):
    totals = calculate_panel(character, weapon, echoes, effects)
    echo_stats = {}
    for echo in echoes:
        if not isinstance(echo, EchoRecord):
            raise ValueError('声骸记录无效')
        for stat in echo.main_stats + echo.sub_stats:
            echo_stats[stat.stat_name] = echo_stats.get(stat.stat_name, 0.0) + stat.value
    return PanelBreakdown(
        character={
            'hp_90': _number(character, 'hp_90', '角色生命'),
            'atk_90': _number(character, 'atk_90', '角色攻击'),
            'def_90': _number(character, 'def_90', '角色防御'),
            'crit_rate': _number(character, 'crit_rate', '角色暴击率'),
            'crit_dmg': _number(character, 'crit_dmg', '角色暴击伤害'),
            'energy_regen': _number(character, 'energy_regen', '角色共鸣效率'),
        },
        skill_bonus=dict(character.get('bonus_stats', {})),
        weapon=dict(weapon or {}),
        weapon_passive=weapon_static_bonus(weapon),
        echoes=echo_stats,
        sonata=sonata_static_bonus(effects, echoes),
        totals=totals,
    )
