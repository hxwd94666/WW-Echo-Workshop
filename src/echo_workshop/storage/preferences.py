# 恢复并规范化插件设置意图。
"""Explicit next-start plugin preference restore/undo with a durable intent.

Config setters/validators remain authoritative. Their per-key save hook is
temporarily batched, then the same Config file is atomically published once.
The ledger and Config are two files: interrupted publication is reconciled
by exact before/after hashes, never described as a global transaction.
"""

from pathlib import Path

from echo_workshop.core.allocation.models import AllocationRequest
from echo_workshop.core.search_budget import SearchBudget
from echo_workshop.core.allocation_preferences import pending_role_keys
from echo_workshop.core.compatibility import CompatibilityRule
from echo_workshop.core.matching_filters import validate_filter_options
from echo_workshop.core.costs import cost_layouts_from_rows
from echo_workshop.core.matching_calculation import (
    MAX_CANDIDATE_IDS,
    MatchCalculationOptions,
    links_to_groups,
    stable_keys,
)
from echo_workshop.core.requirements import requirements_from_rows
from echo_workshop.core.models import validate_id
from echo_workshop.resources.http import strict_json
from echo_workshop.storage.workspace_bundle import validate_preferences

LOCK_ID = 'ad8a3dc9-3c8e-417e-a97f-680055c90a59'
MAX_LEDGER = 32 * 1024 * 1024


def _validate_candidate_ids(raw, message):
    """A restored scope only needs shape limits; the reader tolerates stale UUIDs."""
    if raw is None:
        return
    if (
        not isinstance(raw, list)
        or len(raw) > MAX_CANDIDATE_IDS
        or any(not isinstance(value, str) or not 0 < len(value) <= 64 for value in raw)
    ):
        raise ValueError(message)


def validate_application(values, profiles):
    values = validate_preferences(values)
    if values['InventoryProfileID'] not in profiles:
        raise ValueError('恢复设置的所选档案不在当前工作区中，请核对副本')
    for key in ('ComparisonSettingsJSON', 'AllocationSettingsJSON'):
        if not values[key]:
            continue
        data = strict_json(values[key])
        if (
            set(data) != {'schema_version', 'profiles'}
            or type(data['schema_version']) is not int
            or data['schema_version'] != 1
            or not isinstance(data['profiles'], dict)
        ):
            raise ValueError('比较/配装设置版本或结构不兼容：' + key)
        for profile, row in data['profiles'].items():
            validate_id(profile)
            if profile not in profiles:
                raise ValueError('设置引用其他工作区档案；请核对后重试')
            if not isinstance(row, dict):
                raise ValueError('档案设置须为对象')
            if key == 'AllocationSettingsJSON':
                required = {'roles', 'fully_tuned'}
                if not required <= set(row) <= required | {
                    'priority_groups',
                    'search_budget',
                    'pending_roles',
                    'candidate_item_ids',
                } or not isinstance(row['roles'], list):
                    raise ValueError('配装设置字段错误')
                if 'search_budget' in row:
                    SearchBudget.from_dict(row['search_budget'])
                pending_role_keys(row.get('pending_roles', []), row['roles'])
                _validate_candidate_ids(row.get('candidate_item_ids'), '配装候选声骸配置错误')
                request_row = {
                    name: value
                    for name, value in row.items()
                    if name not in ('search_budget', 'pending_roles', 'candidate_item_ids')
                }
                if row['roles']:
                    try:
                        AllocationRequest.from_dict(dict(profile_id=profile, **request_row))
                    except TypeError as error:
                        raise ValueError('配装设置字段或类型错误') from error
                else:
                    if type(row['fully_tuned']) is not bool:
                        raise ValueError('完整调谐设置类型错误')
                    if 'priority_groups' in row and row['priority_groups'] != []:
                        raise ValueError('空配装角色只接受空优先级分组')
            else:
                required = {'selections', 'filters', 'rules'}
                if (
                    not required <= set(row) <= required | {'calculation'}
                    or not isinstance(row['selections'], list)
                    or not isinstance(row['rules'], list)
                ):
                    raise ValueError('比较设置字段错误')
                selections = stable_keys(row['selections'])
                if 'calculation' in row:
                    calculation = row['calculation']
                    fields = {
                        'role_order',
                        'display_order',
                        'links',
                        'options',
                        'cost_layouts',
                        'role_requirements',
                        'candidate_item_ids',
                    }
                    if not isinstance(calculation, dict) or not set(calculation) <= fields:
                        raise ValueError('比较计算设置字段错误')
                    order = stable_keys(calculation.get('role_order', selections))
                    if 'display_order' in calculation:
                        stable_keys(calculation['display_order'])
                    links = calculation.get('links', ['='] * max(0, len(order) - 1))
                    if not isinstance(links, list):
                        raise ValueError('比较优先级关系须为列表')
                    links_to_groups(order, links)
                    MatchCalculationOptions.from_dict(calculation.get('options', {}))
                    cost_layouts_from_rows(calculation.get('cost_layouts', []))
                    requirements_from_rows(calculation.get('role_requirements', []))
                    _validate_candidate_ids(
                        calculation.get('candidate_item_ids'), '比较候选声骸配置错误'
                    )
                    # Validate only: keep the exact saved JSON, including cached
                    # unselected roles and historical optional field shapes.
                for rule in row['rules']:
                    if (
                        not isinstance(rule, dict)
                        or set(rule) != {'character_id', 'modal_id', 'element', 'sonatas'}
                        or not all(
                            isinstance(rule[k], str) and rule[k]
                            for k in ('character_id', 'modal_id')
                        )
                    ):
                        raise ValueError('比较适配规则字段错误')
                    CompatibilityRule.from_dict({k: rule[k] for k in ('element', 'sonatas')})
                filters = row['filters']
                filter_fields = {'text', 'cost', 'mode', 'fully_tuned', 'max_age_days'}
                extra_filters = {'minimum_level', 'exclude_locked', 'include_equipped'}
                if (
                    not isinstance(filters, dict)
                    or not filter_fields <= set(filters) <= filter_fields | extra_filters
                    or not isinstance(filters['text'], str)
                    or type(filters['fully_tuned']) is not bool
                ):
                    raise ValueError('比较筛选字段错误')
                # The existing embedded controller persists its calculation
                # filters too. Validate field structure without querying inventory;
                # keep legacy five-field profiles and reject arbitrary keys.
                validate_filter_options(**filters)
    return values


class PreferenceRecovery:
    def __init__(self, manager, config_file):
        self.manager = manager
        self.config_file = Path(config_file).resolve()
        if self.config_file.parent / 'plugin_data' / 'echo-workshop' != manager.root:
            raise ValueError('设置恢复仅绑定当前插件 Config 文件')
        self.path = manager.control / 'preferences-recovery.json'

    def _read(self):
        if self.manager.control.is_symlink() or self.path.is_symlink():
            raise ValueError('设置恢复记录应为实际文件')
        if not self.path.exists():
            return dict(schema_version=1, revision=0, pending=None, last=None)
        if self.path.stat().st_size > MAX_LEDGER:
            raise ValueError('设置恢复记录超过限制')
        state = strict_json(self.path.read_bytes())
        if (
            not isinstance(state, dict)
            or set(state) != {'schema_version', 'revision', 'pending', 'last'}
            or type(state['schema_version']) is not int
            or state['schema_version'] != 1
            or type(state['revision']) is not int
            or not 0 <= state['revision'] < 2**63
        ):
            raise ValueError('设置恢复记录不兼容；原设置保留')
        for name in ('pending', 'last'):
            row = state[name]
            if row is None:
                continue
            if (
                not isinstance(row, dict)
                or set(row)
                != {
                    'workspace',
                    'kind',
                    'before',
                    'after',
                    'before_hash',
                    'after_hash',
                    'preferences_hash',
                }
                or row['kind'] not in ('apply', 'undo')
            ):
                raise ValueError('设置恢复意图字段错误')
            self.manager.target_path(row['workspace'])
            validate_preferences(row['before'])
            validate_preferences(row['after'])
            for key in ('before_hash', 'after_hash', 'preferences_hash'):
                value = row[key]
                if key == 'preferences_hash' and value == '':
                    continue
                if (
                    not isinstance(value, str)
                    or len(value) != 64
                    or any(c not in '0123456789abcdef' for c in value)
                ):
                    raise ValueError('设置恢复哈希错误')
        return state

    @property
    def state(self):
        return self._read()
