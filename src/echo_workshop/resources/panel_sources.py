# 解析角色与武器 90 级数据，并提供受限来源地址。
from __future__ import annotations

import math
from urllib.parse import urlsplit

from echo_workshop.resources.game_sources import approve

NANOKA = 'https://static.nanoka.cc'
XWUID_HOSTS = ('https://ww1.loping151.top', 'https://ww3.loping151.cn')
ENCORE = 'https://api-v2.encore.moe'
PERCENT_NAMES = frozenset(
    {
        '攻击',
        '生命',
        '防御',
        '暴击',
        '暴击伤害',
        '共鸣效率',
        '治疗效果加成',
        '冷凝伤害加成',
        '热熔伤害加成',
        '导电伤害加成',
        '气动伤害加成',
        '衍射伤害加成',
        '湮灭伤害加成',
    }
)


def _finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{label}不是有限数值')
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f'{label}不是有限数值')
    return number


def _identifier(value, label='ID'):
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and value.isdigit() and value != '0':
        return value
    raise ValueError(f'{label}无效')


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError('名称无效')
    return value.strip()


def _level_90(stats):
    if not isinstance(stats, dict):
        raise ValueError('角色/武器 stats 无效')
    levels = stats.get('6')
    if not isinstance(levels, dict) or '90' not in levels:
        raise ValueError('缺少 90 级 stats')
    value = levels['90']
    if not isinstance(value, (dict, list)):
        raise ValueError('90 级 stats 无效')
    return value


def _stat_entries(stats):
    value = _level_90(stats)
    if isinstance(value, dict):
        return value
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError('武器 90 级 stats 无效')
    return value


def _parse_percent(value, *, is_ratio=False, is_percent=False, label='百分比'):
    number = _finite(value, label)
    if is_percent:
        return number / 100.0
    if is_ratio:
        return number * 100.0
    return number


def _parse_text_percent(value, label='百分比'):
    if not isinstance(value, str) or not value.endswith('%'):
        raise ValueError(f'{label}无效')
    try:
        return float(value[:-1])
    except ValueError as error:
        raise ValueError(f'{label}无效') from error


def _nanoka_stat(row, label):
    if not isinstance(row, dict):
        raise ValueError(f'{label}无效')
    return (
        _finite(row.get('value'), label),
        bool(row.get('is_ratio', row.get('isRatio'))),
        bool(row.get('is_percent', row.get('isPercent'))),
        str(row.get('name') or '').strip(),
    )


def nanoka_manifest_release(manifest):
    if not isinstance(manifest, dict):
        raise ValueError('Nanoka manifest 无效')
    release = manifest.get('ww', {}).get('live')
    if not isinstance(release, str) or not release or len(release) > 32:
        raise ValueError('Nanoka 正式服版本无效')
    return release


def nanoka_list_urls(release):
    if not isinstance(release, str) or not release:
        raise ValueError('Nanoka 版本无效')
    return (
        f'{NANOKA}/ww/{release}/character.json',
        f'{NANOKA}/ww/{release}/weapon.json',
    )


def nanoka_character_url(release, character_id):
    return f'{NANOKA}/ww/{release}/zh/character/{_identifier(character_id)}.json'


def nanoka_weapon_url(release, weapon_id):
    return f'{NANOKA}/ww/{release}/zh/weapon/{_identifier(weapon_id)}.json'


def xwuid_character_url(host, character_id):
    return (
        f'{host}/XutheringWavesUID/resource/map/detail_json/char/{_identifier(character_id)}.json'
    )


def xwuid_weapon_url(host, weapon_id):
    return f'{host}/XutheringWavesUID/resource/map/detail_json/weapon/{_identifier(weapon_id)}.json'


def xwuid_default_url(host):
    return f'{host}/XutheringWavesUID/resource/map/1.json'


def encore_character_url(character_id):
    return f'{ENCORE}/api/zh-Hans/character/{_identifier(character_id)}?ver=latest'


def encore_weapon_url(weapon_id):
    return f'{ENCORE}/api/zh-Hans/weapon/{_identifier(weapon_id)}?ver=latest'


def approve_panel_url(url):
    if not isinstance(url, str):
        raise ValueError('面板资源 URL 无效')
    parts = urlsplit(url)
    if parts.query and not (
        parts.netloc == 'api-v2.encore.moe'
        and parts.query == 'ver=latest'
        and parts.path.startswith(('/api/zh-Hans/character/', '/api/zh-Hans/weapon/'))
    ):
        raise ValueError('面板资源查询参数无效')
    return approve(url)


def _nanoka_bonus_stats(data):
    # Forte Circuit passive stat nodes are node_type 4; skill level nodes are not panel stats.
    tree = data.get('skill_trees')
    if not isinstance(tree, dict):
        return {}
    chains = {}
    for key, node in tree.items():
        if not isinstance(node, dict) or node.get('node_type') != 4:
            continue
        skill = node.get('skill')
        if not isinstance(skill, dict):
            continue
        label = str(skill.get('name') or '')
        params = skill.get('param')
        if not isinstance(params, list) or not params or not isinstance(params[0], str):
            continue
        name = {
            '攻击提升': '攻击%',
            '生命提升': '生命%',
            '防御提升': '防御%',
            '暴击提升': '暴击',
            '暴击率提升': '暴击',
            '暴击伤害加成提升': '暴击伤害',
            '共鸣效率提升': '共鸣效率',
        }.get(label, label)
        try:
            raw = params[0]
            value = float(raw[:-1] if raw.endswith('%') else raw)
        except (TypeError, ValueError):
            continue
        root = str(key)
        seen = {root}
        current = node
        while isinstance(current, dict):
            parents = current.get('parent_nodes')
            if not isinstance(parents, list) or not parents:
                break
            parent = str(parents[0])
            if parent in seen:
                break
            seen.add(parent)
            root = parent
            current = tree.get(parent)
        chains[(root, name)] = chains.get((root, name), 0.0) + value
    result = {}
    for (_, name), value in chains.items():
        # All Forte branches are permanent; the two nodes in each branch stack.
        result[name] = result.get(name, 0.0) + value
    return result


def parse_nanoka_character(data):
    if not isinstance(data, dict):
        raise ValueError('Nanoka 角色详情无效')
    stats = _stat_entries(data.get('stats'))
    if not isinstance(stats, dict):
        raise ValueError('Nanoka 角色 stats 无效')
    recommend = data.get('recommend')
    raw_weapons = recommend.get('weapon', []) if isinstance(recommend, dict) else []
    recommended = []
    for value in raw_weapons if isinstance(raw_weapons, list) else []:
        try:
            ident = _identifier(value, '推荐武器 ID')
        except ValueError:
            continue
        if ident not in recommended:
            recommended.append(ident)
    return {
        'name': _name(data.get('name')),
        'element': data.get('element') if type(data.get('element')) is int else None,
        'weapon_type': data.get('weapon') if type(data.get('weapon')) is int else None,
        'hp_90': _finite(stats.get('life'), '90 级生命'),
        'atk_90': _finite(stats.get('atk'), '90 级攻击'),
        'def_90': _finite(stats.get('def'), '90 级防御'),
        'crit_rate': 5.0,
        'crit_dmg': 150.0,
        'energy_regen': 100.0,
        'bonus_stats': _nanoka_bonus_stats(data),
        'recommended_weapon_ids': recommended,
        'default_weapon_id': recommended[0] if recommended else '',
        'source': 'nanoka',
    }


def parse_nanoka_weapon(data):
    if not isinstance(data, dict):
        raise ValueError('Nanoka 武器详情无效')
    rows = _stat_entries(data.get('stats'))
    if not isinstance(rows, list) or len(rows) != 2:
        raise ValueError('Nanoka 武器 stats 无效')
    base = _nanoka_stat(rows[0], '武器基础攻击')
    second = _nanoka_stat(rows[1], '武器副词条')
    if base[1] or base[2]:
        raise ValueError('武器基础攻击不是固定值')
    substat_is_percent = second[3] in PERCENT_NAMES or second[1] or second[2]
    substat_value = _parse_percent(
        second[0], is_ratio=second[1], is_percent=second[2], label='武器副词条'
    )
    return {
        'name': _name(data.get('name')),
        'type': data.get('type') if type(data.get('type')) is int else None,
        'rarity': next(
            (value for value in (data.get('rarity'), data.get('starLevel')) if type(value) is int),
            None,
        ),
        'atk_90': base[0],
        'substat': second[3],
        'substat_value': substat_value,
        'substat_is_percent': substat_is_percent,
        'effect': data.get('effect') if isinstance(data.get('effect'), str) else '',
        'param': data.get('param', []),
        'source': 'nanoka',
    }


def parse_xwuid_character(data):
    if not isinstance(data, dict):
        raise ValueError('XWUID 角色详情无效')
    stats = _level_90(data.get('stats'))
    return {
        'name': _name(data.get('name')),
        'element': data.get('attributeId') if type(data.get('attributeId')) is int else None,
        'weapon_type': data.get('weaponTypeId') if type(data.get('weaponTypeId')) is int else None,
        'hp_90': _finite(stats.get('life'), '90 级生命'),
        'atk_90': _finite(stats.get('atk'), '90 级攻击'),
        'def_90': _finite(stats.get('def'), '90 级防御'),
        'crit_rate': 5.0,
        'crit_dmg': 150.0,
        'energy_regen': 100.0,
        'recommended_weapon_ids': [],
        'default_weapon_id': '',
        'source': 'xwuid',
    }


def parse_xwuid_weapon(data):
    if not isinstance(data, dict):
        raise ValueError('XWUID 武器详情无效')
    rows = _level_90(data.get('stats'))
    if not isinstance(rows, list) or len(rows) != 2:
        raise ValueError('XWUID 武器 stats 无效')
    first, second = rows
    base = _nanoka_stat(first, '武器基础攻击')
    extra = _nanoka_stat(second, '武器副词条')
    if base[1] or base[2]:
        raise ValueError('武器基础攻击不是固定值')
    return {
        'name': _name(data.get('name')),
        'type': data.get('type') if type(data.get('type')) is int else None,
        'rarity': next(
            (value for value in (data.get('rarity'), data.get('starLevel')) if type(value) is int),
            None,
        ),
        'atk_90': base[0],
        'substat': extra[3],
        'substat_value': _parse_percent(
            extra[0], is_ratio=extra[1], is_percent=extra[2], label='武器副词条'
        ),
        'substat_is_percent': extra[3] in PERCENT_NAMES or extra[1] or extra[2],
        'effect': data.get('effect') if isinstance(data.get('effect'), str) else '',
        'param': data.get('param', []),
        'source': 'xwuid',
    }


def parse_encore_character(data):
    if not isinstance(data, dict) or not isinstance(data.get('Properties'), list):
        raise ValueError('Encore 角色详情无效')
    values = {}
    for row in data['Properties']:
        if not isinstance(row, dict):
            continue
        name = row.get('Name')
        if name not in ('生命', '攻击', '防御', '暴击', '暴击伤害'):
            continue
        growth = row.get('GrowthValues')
        if not isinstance(growth, list):
            continue
        level_90 = [
            entry for entry in growth if isinstance(entry, dict) and entry.get('level') == 90
        ]
        if not level_90:
            continue
        raw = level_90[-1].get('value')
        if isinstance(raw, str):
            values[name] = _parse_text_percent(raw, name)
        else:
            values[name] = _finite(raw, name)
    missing = {'生命', '攻击', '防御'} - set(values)
    if missing:
        raise ValueError('Encore 角色缺少 90 级基础属性')
    return {
        'name': _name(
            data.get('Name', {}).get('Content')
            if isinstance(data.get('Name'), dict)
            else data.get('Name')
        ),
        'element': data.get('ElementId') if type(data.get('ElementId')) is int else None,
        'weapon_type': data.get('WeaponType') if type(data.get('WeaponType')) is int else None,
        'hp_90': values['生命'],
        'atk_90': values['攻击'],
        'def_90': values['防御'],
        'crit_rate': values.get('暴击', 5.0),
        'crit_dmg': values.get('暴击伤害', 150.0),
        'energy_regen': 100.0,
        'recommended_weapon_ids': [],
        'default_weapon_id': '',
        'source': 'encore',
    }


def default_weapon_from_xwuid(rows):
    if not isinstance(rows, list):
        raise ValueError('XWUID 默认配装无效')
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        role = row.get('role')
        weapon = row.get('weaponData')
        if not isinstance(role, dict) or not isinstance(weapon, dict):
            continue
        ident = role.get('roleId')
        payload = weapon.get('weapon')
        if not isinstance(payload, dict):
            continue
        try:
            result[_identifier(ident)] = _identifier(payload.get('weaponId'))
        except ValueError:
            continue
    return result
