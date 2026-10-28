# 静态解析 WuwaEchoTool 的 base.js，提取评分权重；不执行下载的 JavaScript。
import copy
import json
import math
import re

from ww_echo_workshop.core.policy import PolicyError
from ww_echo_workshop.core.policy_presets import WEIGHT_KEYS, validate_data_pack

MAX_VALUES = (105, 210, 116, 147, 116, 60, 70, 580, 124, 116, 116, 116, 116)
DISPLAY_SCALE = (0.1, 0.1, 0.1, 0.1, 0.1, 1.0, 1.0, 1.0, 0.1, 0.1, 0.1, 0.1, 0.1)
NEW_IDS = (57, 58, 59, 60, 61, 62, 63)
PRIMARY = {57: '导电', 58: '导电', 59: '湮灭', 60: '冷凝', 61: '气动', 62: '热熔', 63: '导电'}
MODE_LABELS = {
    'default': '默认',
    'tune': '震谐',
    'fusion': '聚爆',
    'harmony': '集谐',
    'quick': '短按超速切',
    'critical': '长按临界',
    'cycle': '完整循环',
    'support': '治疗辅助',
    'damage': '自身输出',
    'sustained': '持续循环',
    'opening': '首轮爆发',
    'unison': '同奏',
    'electro': '电磁',
}
MODES = {
    49: ('tune', 'fusion'),
    53: ('fusion', 'harmony'),
    57: ('quick', 'critical'),
    58: ('quick', 'critical'),
    59: ('cycle',),
    60: ('support', 'damage'),
    61: ('sustained', 'opening'),
    62: ('sustained', 'opening'),
    63: ('unison', 'electro'),
}


def _block(source, name):
    start = source.index('const ' + name + ' =')
    opening = source.index('[', start)
    depth = 0
    quote = None
    escaped = False
    for index in range(opening, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ('"', "'"):
            quote = char
        elif char == '[':
            depth += 1
        elif char == ']':
            depth -= 1
            if depth == 0:
                return source[opening : index + 1]
    raise PolicyError('invalid_data', f'base.js 缺少 {name}')


def _clean(raw):
    out = []
    index = 0
    quote = None
    escaped = False
    while index < len(raw):
        char = raw[index]
        if quote:
            out.append(char)
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in ('"', "'"):
            quote = char
            out.append(char)
            index += 1
            continue
        if char == '/' and index + 1 < len(raw) and raw[index + 1] == '/':
            end = raw.find('\n', index)
            index = len(raw) if end < 0 else end
            continue
        if char == '/' and index + 1 < len(raw) and raw[index + 1] == '*':
            end = raw.find('*/', index + 2)
            index = len(raw) if end < 0 else end + 2
            continue
        out.append(char)
        index += 1
    text = re.sub(r',\s*([}\]])', r'\1', ''.join(out))
    out = []
    index = 0
    quote = None
    escaped = False
    while index < len(text):
        char = text[index]
        if quote:
            out.append(char)
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in ('"', "'"):
            quote = char
            out.append(char)
            index += 1
            continue
        if (char.isalpha() or char == '_') and (
            index == 0 or not (text[index - 1].isalnum() or text[index - 1] in '_$')
        ):
            end = index
            while end < len(text) and (text[end].isalnum() or text[end] in '_$'):
                end += 1
            colon = end
            while colon < len(text) and text[colon].isspace():
                colon += 1
            if colon < len(text) and text[colon] == ':':
                out.append('"' + text[index:end] + '"')
                index = end
                continue
        out.append(char)
        index += 1
    return ''.join(out)


def _literal(source, name):
    try:
        return json.loads(_clean(_block(source, name)))
    except (ValueError, json.JSONDecodeError) as error:
        raise PolicyError('invalid_data', f'base.js {name} 解析失败：{error}') from error


def _clamp(value, low, high):
    return max(low, min(high, value))


def _round(value):
    return round(value, 6)


def _packets(role_id, chain, mode, health=40000):
    parts = []
    attack = 2500.0
    bonus = 2.0
    crit_damage = 2.8
    crit = 0.8

    def add(kind, budget, **extra):
        part = {
            'type': kind,
            'budget': budget,
            'attack': attack,
            'bonus': bonus,
            'cd': crit_damage,
            'crit': crit,
            'element': PRIMARY[role_id],
        }
        part.update(extra)
        parts.append(part)

    if role_id in (57, 58):
        add('normal', 12 if mode == 'quick' else 6)
        add('skill', 8 if mode == 'quick' else 4)
        add('skill', 32 * (1.2 if chain >= 3 else 1))
        add('liberate', 18 * (1.2 if chain >= 4 else 1))
        add('other', 5)
        add('other', 5 * (1.5 if chain >= 2 else 1), fixed=True)
        if mode == 'critical':
            for element, budget in (('衍射', 12), ('湮灭', 12), ('气动', 16), ('导电', 15)):
                add(
                    'skill',
                    budget * (1.2 if chain >= 6 else 1),
                    element=element,
                    bonus=bonus + 0.2,
                    cd=crit_damage + (0.2 if chain >= 5 else 0),
                )
    elif role_id == 59:
        if chain >= 4:
            attack += 200
        heavy = 1.4 if chain >= 6 else 1
        add('normal', 8)
        add('skill', 2)
        add('heavy', 10 * heavy)
        add('heavy', 55 * (2 if chain >= 2 else 1) * heavy, cd=crit_damage + 3.1)
        add('liberate', 18 * (2.75 if chain >= 3 else 1))
        add('other', 5)
        add('other', 2 * (1.5 if chain >= 3 else 1), fixed=True)
        if chain >= 1:
            add('heavy', 2 * 337.98 / 60 * heavy)
        if chain >= 6:
            add('heavy', 5 * 337.98 / 60 * heavy, crit=1, guaranteed=True)
    elif role_id == 60:
        if chain >= 2:
            crit_damage += 0.5
        add('normal', 6 if chain >= 3 else 12)
        add('normal', 10 * (2 if chain >= 5 else 1))
        add('heavy', 5 * (2 if chain >= 5 else 1))
        add('skill', 8)
        add(
            'other',
            55,
            health=True,
            crit=1,
            guaranteed=True,
            cd=crit_damage + (5 if chain >= 6 else 0),
            bonus=bonus + 2.4,
        )
        add('other', 5)
        add('other', 5, fixed=True)
    elif role_id == 61:
        if chain >= 1:
            crit = 0.96
        if chain >= 4:
            attack += 200
        stacks = 25 if chain >= 2 else 15
        lock = 1 + stacks * 0.02 + 0.35
        final = 1.4 if chain >= 6 else 1
        add('normal', 6)
        add('normal', 24, multiplier=lock * lock)
        add('heavy', 10 * (1.4 if chain >= 2 else 1) * final, multiplier=lock * lock)
        add('heavy', 30 * (1 + stacks * 0.03 if chain >= 3 else 1) * final, multiplier=lock * lock)
        add('skill', 8 * (2 if chain >= 5 else 1))
        add(
            'liberate',
            17 * final,
            cd=crit_damage + (1 if chain >= 3 else 0),
            multiplier=lock * lock,
        )
        add('other', 5)
        if chain >= 1 and (mode == 'opening' or chain >= 6):
            add(
                'normal',
                400 / 60 * (1 + 25 * 0.04) * final,
                multiplier=lock * lock if chain >= 6 else 1,
            )
    elif role_id == 62:
        health = _clamp(health, 15000, 70000)
        capped = min(health, 50000)
        attack += capped * (0.05 if chain >= 3 else 0.036)
        bonus += capped * 0.000015 + (0.2 if chain >= 4 else 0)
        heavy = 1.4 if chain >= 6 else 1
        fire = 1 + _clamp(health - 25000, 0, 25000) * 0.00009
        add('normal', 3)
        add('heavy', 10 * heavy)
        add('skill', 12 * (1.8 if chain >= 1 else 1))
        add(
            'heavy',
            40
            * fire
            * (1.46 if chain >= 2 else 1)
            * (1.45 if mode == 'opening' and chain >= 2 else 1)
            * heavy,
        )
        add('heavy', 18 * heavy)
        add('heavy', 12 * (1.8 * 3 if chain >= 6 else 1) * heavy)
        add('other', 5)
    elif role_id == 63:
        if mode == 'unison':
            attack += 500
        else:
            bonus += 0.5
        if chain >= 4:
            bonus += 0.2
        skill = 1.4 * (2 / 1.8) if chain >= 6 else 1
        stacks = 4 if chain >= 6 else 3
        add('normal', 12 if mode == 'unison' else 17)
        add('skill', 10 * skill)
        add('skill', 34 * (1.6 if chain >= 2 else 1) * skill)
        add(
            'skill',
            25 * (1.7 if chain >= 3 else 1) * skill,
            cd=crit_damage + ((0.2 + 0.15 * stacks) if mode == 'unison' and chain >= 3 else 0),
        )
        add('liberate', 6)
        add(
            'other',
            (13 if mode == 'unison' else 8)
            * (1.15 + 0.1 * stacks if mode == 'unison' and chain >= 1 else 1),
        )
        if mode == 'electro':
            fixed_crit = 1 + 0.8 * (2.3 - 1) if chain >= 6 else 1
            add(
                'other',
                20 * ((21 if chain >= 1 else 17.5) + (15 if chain >= 3 else 0)) / 17.5 * fixed_crit,
                fixed=True,
            )
    damaged = []
    for part in parts:
        value = part['budget']
        if not part.get('fixed'):
            value *= 1 if part.get('health') else part['attack'] / 2500
            value *= part['bonus'] / 2
            value *= (1 + part['crit'] * (part['cd'] - 1)) / 2.44
            value *= part.get('multiplier') or 1
        damaged.append((part, value))
    return damaged


def _new_coefficients(role_id, chain, mode):
    parts = _packets(role_id, chain, mode)
    total = sum(value for _, value in parts)
    types = ('normal', 'skill', 'heavy', 'liberate')

    def weighted(function):
        return (
            sum((0 if part.get('fixed') else value) * function(part) for part, value in parts)
            / total
        )

    rule = {
        'attack01': 2500 * weighted(lambda part: 0 if part.get('health') else 1 / part['attack']),
        'attack02': 250 * weighted(lambda part: 0 if part.get('health') else 1 / part['attack']),
        'crit': 2.5
        * weighted(
            lambda part: (
                0
                if part.get('guaranteed')
                else (part['cd'] - 1) / (1 + part['crit'] * (part['cd'] - 1))
            )
        ),
        'critDamage': 2.5
        * weighted(lambda part: part['crit'] / (1 + part['crit'] * (part['cd'] - 1))),
        'property': 2.5 * weighted(lambda part: 1 / part['bonus']),
        'health01': 0.0,
        'health02': 0.0,
        'defense01': 0.0,
        'defense02': 0.0,
        'efficiency01': 0.5,
        'elements': {
            element: 2.5
            * weighted(
                lambda part, element=element: 1 / part['bonus'] if part['element'] == element else 0
            )
            for element in ('导电', '衍射', '湮灭', '气动', '热熔', '冷凝')
        },
    }
    rule['property'] = rule['elements'][PRIMARY[role_id]]
    if role_id == 60:
        rule['health01'] = (
            250 * 167.12 / 35000 * weighted(lambda part: 1 if part.get('health') else 0)
        )
        rule['health02'] = rule['health01'] / 167.12
        if mode == 'support':
            rule.update(
                attack01=0,
                attack02=0,
                crit=0,
                critDamage=0,
                property=0,
                health01=1,
                health02=1 / 167.12,
                efficiency01=1.2,
                elements={},
            )
    if role_id == 62:
        health = 40000
        next_total = sum(value for _, value in _packets(role_id, chain, mode, health + 1))
        rule['health02'] = 0 if health >= 50000 else 250 * (next_total - total) / total
        rule['health01'] = rule['health02'] * 153.75
    type_coefficients = {
        kind: 0
        if role_id == 60 and mode == 'support'
        else 2.5
        * weighted(lambda part, kind=kind: 1 / part['bonus'] if part['type'] == kind else 0)
        for kind in types
    }
    return (
        rule['crit'],
        rule['critDamage'],
        rule['attack01'],
        rule['defense01'],
        rule['health01'],
        rule['attack02'],
        rule['defense02'],
        rule['health02'],
        rule['efficiency01'],
        type_coefficients['normal'],
        type_coefficients['heavy'],
        type_coefficients['skill'],
        type_coefficients['liberate'],
    )


def _legacy_coefficients(role, chain, mode, sums, rules):
    ref = sums[role['id'] - 1] if role['id'] <= len(sums) else None
    weights = role
    rule = rules[role['rule']]
    if chain and ref and ref.get('mzProperty') and ref.get('mzRule'):
        weights = ref['mzProperty'][chain - 1]
        rule = ref['mzRule'][chain - 1]
    if ref and ref.get('modeProfiles'):
        profiles = ref['modeProfiles'].get(mode)
        if profiles:
            profile = profiles[chain]
            weights = profile['weights']
            rule = profile['rule']
    return (
        rule['crit'],
        rule['critDamage'],
        rule['attack01'],
        rule['defense01'],
        rule['health01'],
        rule['attack02'],
        rule['defense02'],
        rule['health02'],
        rule['efficiency01'],
        rule['unike'] * weights['normal'],
        rule['unike'] * weights['heavy'],
        rule['unike'] * weights['skill'],
        rule['unike'] * weights['liberate'],
    )


def _weights(coefficients):
    return {
        key: math.floor((value * maximum * scale * 10 + 1e-12) * 100 + 0.5) / 100
        for key, value, maximum, scale in zip(WEIGHT_KEYS, coefficients, MAX_VALUES, DISPLAY_SCALE)
    }


def extract_wuwa_presets(source):
    version = re.search(r'const toolVersion = "([^"]+)"', source)
    role_list = _literal(source, 'roleList')
    rules = _literal(source, 'ruleList')
    sums = _literal(source, 'RoleSumProperty')
    groups = []
    for role in role_list:
        role_id = int(role['id'])
        modes = MODES.get(role_id, ('default',))
        variants = []
        for mode in modes:
            for chain in range(7):
                if role_id in NEW_IDS:
                    coefficients = _new_coefficients(role_id, chain, mode)
                else:
                    coefficients = _legacy_coefficients(role, chain, mode, sums, rules)
                label = f'{chain}链'
                if mode != 'default':
                    label += ' · ' + MODE_LABELS.get(mode, mode)
                variants.append(
                    {
                        'variantName': label,
                        'weights': _weights(coefficients),
                    }
                )
        groups.append({'presetName': role['name'], 'variants': variants})
    return (version.group(1) if version else 'unknown'), groups


def merge_wuwa_pack(pack, groups, version):
    data = copy.deepcopy(pack)
    data['presets']['wuwa_echo_tool'] = {'presets': groups}
    base_version = str(data.get('version', '')).split('+wuwa.')[0]
    data['version'] = f'{base_version}+wuwa.{version}'
    for group in groups:
        data['characters'].setdefault(
            group['presetName'], {'en': group['presetName'], 'release_date': ''}
        )
    return validate_data_pack(data)
