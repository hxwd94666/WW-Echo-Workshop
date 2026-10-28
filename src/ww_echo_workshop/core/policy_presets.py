# 定义五套策略评分体系的默认参数、数据包校验与预设读取。
from dataclasses import dataclass
import math

from ww_echo_workshop.core.policy import PolicyError

WEIGHT_KEYS = (
    'Crit_Rate',
    'Crit_Damage',
    'Attack',
    'Defence',
    'HP',
    'Attack_Flat',
    'Defence_Flat',
    'HP_Flat',
    'ER',
    'Basic_Attack_Damage',
    'Heavy_Attack_Damage',
    'Skill_Damage',
    'Ult_Damage',
)
SYSTEMS = {
    'linear': {
        'label': '线性数值评分',
        'weights': (100.0, 100.0, 70.0, 0.0, 0.0, 36.0, 0.0, 0.0, 40.0, 0.0, 0.0, 0.0, 0.0),
        'target': 70.0,
        'main_score': 0.0,
        'max_score': 100.0,
        'fixed': False,
    },
    'fixed': {
        'label': '固定词条评分',
        'weights': (3, 3, 1, 0, 0, 0, 0, 0, 1, 1, 0, 0, 0),
        'target': 7.0,
        'main_score': 0.0,
        'max_score': 100.0,
        'fixed': True,
    },
    'wuwa_echo_tool': {
        'label': 'Wuwa Echo Tool',
        'weights': (100.0, 100.0, 70.0, 0.0, 0.0, 36.0, 0.0, 0.0, 40.0, 0.0, 0.0, 0.0, 0.0),
        'target': 70.0,
        'main_score': 0.0,
        'max_score': 100.0,
        'fixed': False,
    },
    'mc_boost_assistant': {
        'label': '漂泊者强化助手',
        'weights': (1.0, 1.0, 0.7, 0.0, 0.0, 0.36, 0.0, 0.0, 0.25, 0.0, 0.0, 0.0, 0.0),
        'target': 95.0,
        'main_score': 0.0,
        'max_score': 120.0,
        'fixed': False,
    },
    'qq_bot': {
        'label': 'XutheringWavesUID',
        'weights': (2.0, 1.0, 1.1, 0.0, 0.0, 0.1, 0.0, 0.0, 0.2, 0.0, 0.0, 0.0, 0.0),
        'target': 35.0,
        'main_score': 0.0,
        'max_score': 50.0,
        'fixed': False,
    },
}


@dataclass(frozen=True)
class PolicyPreset:
    system: str
    character: str
    variant: str
    preset_id: str
    weights: tuple[float, ...]
    target: float
    main_score: float
    max_score: float
    cost: int | None = None
    main_stats: tuple[str, ...] = ()
    source_score_max: float | None = None


def default_scoring(system):
    if system not in SYSTEMS:
        raise PolicyError('invalid_scoring', '未知评分体系')
    spec = SYSTEMS[system]
    return {
        'system': system,
        'weights': tuple(spec['weights']),
        'target': float(spec['target']),
        'main_score': float(spec['main_score']),
        'max_score': float(spec['max_score']),
        'fixed': bool(spec['fixed']),
    }


def validate_data_pack(pack):
    if not isinstance(pack, dict) or pack.get('schema_version') != 1:
        raise PolicyError('invalid_data', '策略数据包 schema 不兼容')
    if not isinstance(pack.get('version'), str) or not pack['version'].strip():
        raise PolicyError('invalid_data', '策略数据包缺少版本')
    characters = pack.get('characters')
    presets = pack.get('presets')
    if not isinstance(characters, dict) or not isinstance(presets, dict):
        raise PolicyError('invalid_data', '策略数据包结构不完整')
    for system in SYSTEMS:
        if system not in presets:
            raise PolicyError('invalid_data', f'策略数据缺少评分体系：{system}')
    for system, value in presets.items():
        if system not in SYSTEMS or not isinstance(value, dict):
            raise PolicyError('invalid_data', f'未知评分体系：{system}')
        groups = value.get('presets', [])
        if not isinstance(groups, list):
            raise PolicyError('invalid_data', f'预设列表无效：{system}')
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get('presetName'), str):
                raise PolicyError('invalid_data', f'预设角色无效：{system}')
            variants = group.get('variants')
            if not isinstance(variants, list):
                raise PolicyError('invalid_data', f'预设变体无效：{system}')
            base_weights = None
            for index, variant in enumerate(variants):
                if not isinstance(variant, dict):
                    raise PolicyError('invalid_data', f'预设变体无效：{system}')
                raw = variant.get('weights')
                if index == 0:
                    if not isinstance(raw, dict) or any(key not in raw for key in WEIGHT_KEYS):
                        raise PolicyError('invalid_data', f'首个变体权重不完整：{system}')
                    base_weights = dict(raw)
                    weights = base_weights
                else:
                    if raw is not None and not isinstance(raw, dict):
                        raise PolicyError('invalid_data', f'预设权重不完整：{system}')
                    weights = {**base_weights, **(raw or {})}
                for key in WEIGHT_KEYS:
                    value = weights.get(key)
                    if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                        raise PolicyError('invalid_data', f'预设权重无效：{system}/{key}')
    return pack


def iter_presets(pack, system):
    validate_data_pack(pack)
    defaults = default_scoring(system)
    for group in pack['presets'][system].get('presets', []):
        base_weights = None
        for index, variant in enumerate(group.get('variants', [])):
            raw = variant.get('weights')
            if index == 0:
                base_weights = dict(raw)
                weights = base_weights
            else:
                weights = {**base_weights, **(raw or {})}
            values = tuple(float(weights[key]) for key in WEIGHT_KEYS)
            yield PolicyPreset(
                system=system,
                character=group['presetName'],
                variant=variant.get('variantName', '默认'),
                preset_id=variant.get('id', ''),
                weights=values,
                target=float(variant.get('target', defaults['target'])),
                main_score=float(variant.get('mainBuffScore', defaults['main_score'])),
                max_score=float(variant.get('normalizedMaxScore', defaults['max_score'])),
                cost=variant.get('cost'),
                main_stats=tuple(variant.get('mainStats', ())),
                source_score_max=variant.get('sourceScoreMax'),
            )


def preset_by_id(pack, system, preset_id):
    for preset in iter_presets(pack, system):
        if preset.preset_id == preset_id:
            return preset
    raise PolicyError('invalid_preset', '策略预设不存在或已更新')
