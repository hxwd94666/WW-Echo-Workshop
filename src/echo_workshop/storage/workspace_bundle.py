# 读取兼容完整工作区归档。
"""Portable full snapshots and isolated recovery, never an in-place merge.

Recovery publishes a new directory only after inventory/history, plan score
proofs, preferences and every PNG have passed validation. Runtime activation
is intentionally separate; this module does not change host configuration.
"""

import hashlib

from echo_workshop.storage.plans import MAX_PLAN_BYTES, validate_plan
from echo_workshop.core.models import canonical_json, validate_id
from echo_workshop.application.operations import checkpoint
from echo_workshop.storage.locks import plan_file_lock
from echo_workshop.application.hotkeys import parse_bindings
from echo_workshop.resources.http import strict_json

KIND = 'okww-echo-workshop-workspace'
MAX_PLANS = 1000
MAX_JSON = 16 * 1024 * 1024
BOOL_KEYS = (
    '启用声骸评分',
    '自动匹配评分模板',
    'Show Debug Boxes',
    'AutoWeightUpdates',
)
LEGACY_TEXT_KEYS = (
    '角色评分模板',
    'InventoryProfileID',
    'TemplateCharacterID',
    'TemplateModalID',
    'ComparisonSettingsJSON',
    'AllocationSettingsJSON',
    'CaptureHotkeysJSON',
)
INTAKE_DEFAULTS = {'IntakeMode': 'manual', 'ScanMinimumLevel': '25'}
TEXT_KEYS = LEGACY_TEXT_KEYS + tuple(INTAKE_DEFAULTS)
PREFERENCE_KEYS = BOOL_KEYS + TEXT_KEYS


def validate_preferences(values):
    if (
        not isinstance(values, dict)
        or not set(BOOL_KEYS + LEGACY_TEXT_KEYS) <= set(values)
        or not set(values) <= set(PREFERENCE_KEYS)
    ):
        raise ValueError('完整快照只接受插件设置白名单，不包含其他宿主设置')
    values = dict(INTAKE_DEFAULTS, **values)
    for key in BOOL_KEYS:
        if type(values[key]) is not bool:
            raise ValueError('插件布尔设置类型错误：' + key)
    for key in TEXT_KEYS:
        if not isinstance(values[key], str) or len(values[key]) > 1024 * 1024:
            raise ValueError('插件文本设置类型/大小错误：' + key)
    if values['InventoryProfileID']:
        validate_id(values['InventoryProfileID'])
    for key in ('ComparisonSettingsJSON', 'AllocationSettingsJSON'):
        if values[key] and not isinstance(strict_json(values[key]), dict):
            raise ValueError('插件内嵌设置须为 JSON 对象：' + key)
    parse_bindings(values['CaptureHotkeysJSON'])
    if values['IntakeMode'] not in ('manual', 'automatic') or values['ScanMinimumLevel'] not in (
        '5',
        '10',
        '15',
        '20',
        '25',
    ):
        raise ValueError('收录方式或最低强化等级错误')
    return dict(values)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _plan(raw, name, cancel_event=None, *, plan_evaluator=None):
    if (
        not name.startswith('allocation-plans/')
        or not name.endswith('.json')
        or len(raw) > MAX_PLAN_BYTES
    ):
        raise ValueError('完整快照方案路径/大小错误')
    plan_id = name[len('allocation-plans/') : -5]
    validate_id(plan_id)
    envelope = strict_json(raw)
    if not isinstance(envelope, dict) or set(envelope) != {'sha256', 'payload'}:
        raise ValueError('方案封装格式错误')
    payload = envelope['payload']
    if (
        _digest(canonical_json(payload).encode('utf-8')) != envelope['sha256']
        or payload['plan_id'] != plan_id
    ):
        raise ValueError('完整快照方案哈希/UUID 不一致')
    return validate_plan(payload, cancel_event=cancel_event, plan_evaluator=plan_evaluator)


def _plan_paths(store, cancel_event=None):
    if store.folder.is_symlink():
        raise ValueError('方案目录应为实际用户目录')
    paths = sorted(store.folder.glob('*.json'))
    if len(paths) > MAX_PLANS:
        raise ValueError('完整快照方案数超过 1000；未截断候选')
    for path in paths:
        checkpoint(cancel_event)
        validate_id(path.stem)
        if path.is_symlink():
            raise ValueError('方案文件不接受符号链接')
    return paths


def _plan_bytes(store, path, cancel_event):
    with store._operation_lock(cancel_event), plan_file_lock(store.folder, path.stem):
        checkpoint(cancel_event)
        if path.stat().st_size > MAX_PLAN_BYTES:
            raise ValueError('方案快照超过 4 MiB')
        raw = path.read_bytes()
        _plan(
            raw, 'allocation-plans/' + path.name, cancel_event, plan_evaluator=store.plan_evaluator
        )
        return raw
