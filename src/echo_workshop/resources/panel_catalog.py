# 保存角色与武器 90 级紧凑快照，并提供原子缓存。
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import threading

from echo_workshop.core.cancellation import checkpoint
from echo_workshop.resources.paths import builtin_path

SCHEMA_VERSION = 1
MAX_BYTES = 2 * 1024 * 1024


def _finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{label}不是有限数值')
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f'{label}不是有限数值')
    return number


def _text(value, label, maximum=128):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f'{label}无效')
    return value.strip()


def _id(value, label='ID'):
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and value.isdigit() and value != '0':
        return value
    raise ValueError(f'{label}无效')


def _mapping(value, label):
    if not isinstance(value, dict):
        raise ValueError(f'{label}无效')
    return value


@dataclass(frozen=True)
class PanelSnapshot:
    characters: dict
    weapons: dict
    game_version: str
    sources: dict
    retrieved_at: float

    @classmethod
    def empty(cls):
        return cls({}, {}, 'builtin', {}, 0.0)

    @classmethod
    def from_data(cls, data):
        if not isinstance(data, dict) or data.get('schema_version') != SCHEMA_VERSION:
            raise ValueError('角色面板快照版本无效')
        characters = _mapping(data.get('characters'), '角色快照')
        weapons = _mapping(data.get('weapons'), '武器快照')
        normalized_characters, normalized_weapons = {}, {}
        for key, row in characters.items():
            ident = _id(key, '角色 ID')
            if not isinstance(row, dict):
                raise ValueError('角色条目无效')
            recommended = row.get('recommended_weapon_ids', [])
            if not isinstance(recommended, list):
                raise ValueError('推荐武器列表无效')
            recommended = [_id(value, '推荐武器 ID') for value in recommended]
            default = row.get('default_weapon_id', '') or ''
            if default:
                default = _id(default, '默认武器 ID')
            bonus = row.get('bonus_stats', {})
            if not isinstance(bonus, dict) or len(bonus) > 32:
                raise ValueError('技能树被动属性无效')
            bonus = {
                _text(name, '技能树被动属性', 64): _finite(value, '技能树被动属性')
                for name, value in bonus.items()
            }
            normalized_characters[ident] = {
                'name': _text(row.get('name'), '角色名称'),
                'element': row.get('element') if type(row.get('element')) is int else None,
                'weapon_type': row.get('weapon_type')
                if type(row.get('weapon_type')) is int
                else None,
                'hp_90': _finite(row.get('hp_90'), '角色生命'),
                'atk_90': _finite(row.get('atk_90'), '角色攻击'),
                'def_90': _finite(row.get('def_90'), '角色防御'),
                'crit_rate': _finite(row.get('crit_rate'), '角色暴击率'),
                'crit_dmg': _finite(row.get('crit_dmg'), '角色暴击伤害'),
                'energy_regen': _finite(row.get('energy_regen'), '角色共鸣效率'),
                'bonus_stats': bonus,
                'recommended_weapon_ids': recommended,
                'default_weapon_id': default,
                'source': _text(row.get('source'), '角色来源', 32),
            }
        for key, row in weapons.items():
            ident = _id(key, '武器 ID')
            if not isinstance(row, dict):
                raise ValueError('武器条目无效')
            normalized_weapons[ident] = {
                'name': _text(row.get('name'), '武器名称'),
                'type': row.get('type') if type(row.get('type')) is int else None,
                'rarity': row.get('rarity') if type(row.get('rarity')) is int else None,
                'atk_90': _finite(row.get('atk_90'), '武器攻击'),
                'substat': _text(row.get('substat'), '武器副词条', 32),
                'substat_value': _finite(row.get('substat_value'), '武器副词条数值'),
                'substat_is_percent': bool(row.get('substat_is_percent')),
                'effect': row.get('effect') if isinstance(row.get('effect'), str) else '',
                'param': row.get('param') if isinstance(row.get('param'), list) else [],
                'source': _text(row.get('source'), '武器来源', 32),
            }
        for row in normalized_characters.values():
            default = row['default_weapon_id']
            if default and default not in normalized_weapons:
                raise ValueError('默认武器不在武器快照中')
            if default and row['weapon_type'] is not None:
                weapon_type = normalized_weapons[default].get('type')
                if weapon_type is not None and weapon_type != row['weapon_type']:
                    raise ValueError('默认武器类型与角色不匹配')
        version = _text(data.get('game_version'), '游戏版本', 64)
        sources = _mapping(data.get('provenance'), '来源记录')
        retrieved = _finite(data.get('retrieved_at'), '抓取时间')
        return cls(normalized_characters, normalized_weapons, version, sources, retrieved)

    def to_data(self):
        return {
            'schema_version': SCHEMA_VERSION,
            'game_version': self.game_version,
            'retrieved_at': self.retrieved_at,
            'characters': self.characters,
            'weapons': self.weapons,
            'provenance': self.sources,
        }

    @property
    def version(self):
        body = json.dumps(self.to_data(), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(body.encode()).hexdigest()

    @property
    def source(self):
        for name in ('nanoka', 'xwuid', 'encore'):
            if name in self.sources:
                return name
        return 'builtin'

    @property
    def entries(self):
        return len(self.characters) + len(self.weapons)


class PanelCatalog:
    def __init__(self, data_dir, *, builtin=None):
        self.path = Path(data_dir).resolve() / 'panel-resources/current.json'
        self._lock = threading.RLock()
        snapshot = builtin or builtin_path('panel_catalog.json')
        try:
            data = json.loads(Path(snapshot).read_text(encoding='utf-8'))
            self.active = PanelSnapshot.from_data(data)
        except FileNotFoundError:
            self.active = PanelSnapshot.empty()
        self.warning = ''
        try:
            if self.path.is_symlink() or self.path.stat().st_size > MAX_BYTES:
                raise ValueError('角色面板缓存超限')
            payload = json.loads(self.path.read_text(encoding='utf-8'))
            if payload.get('schema_version') != SCHEMA_VERSION:
                raise ValueError('角色面板缓存版本无效')
            active = PanelSnapshot.from_data(payload)
            if payload.get('version') != active.version:
                raise ValueError('角色面板缓存校验失败')
            self.active = active
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError) as error:
            self.warning = '角色面板缓存校验失败，使用内置快照：' + str(error)

    def activate(self, data, *, retrieved_at=None, cancel_event=None):
        if retrieved_at is not None:
            data = dict(data, retrieved_at=float(retrieved_at))
        snapshot = PanelSnapshot.from_data(data)
        payload = snapshot.to_data()
        payload['version'] = snapshot.version
        body = json.dumps(payload, ensure_ascii=False).encode()
        if len(body) > MAX_BYTES:
            raise ValueError('角色面板缓存超限')
        with self._lock:
            checkpoint(cancel_event)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.parent.is_symlink() or self.path.is_symlink():
                raise ValueError('角色面板缓存路径无效')
            fd, name = tempfile.mkstemp(prefix='.panel-', dir=self.path.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                checkpoint(cancel_event)
                os.replace(name, self.path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            changed = snapshot.version != self.active.version
            self.active, self.warning = snapshot, ''
            return changed

    def character(self, character_id):
        return self.active.characters.get(_id(character_id))

    def weapon(self, weapon_id):
        return self.active.weapons.get(_id(weapon_id))

    def default_weapon(self, character_id):
        row = self.character(character_id)
        if not row:
            return ''
        if row['default_weapon_id'] in self.active.weapons:
            return row['default_weapon_id']
        for ident in row['recommended_weapon_ids']:
            if ident in self.active.weapons:
                return ident
        return ''
