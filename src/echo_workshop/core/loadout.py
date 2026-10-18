# 定义当前配装与保护约束。
"""Pure current-loadout relationships and reservation validation."""

from dataclasses import dataclass
import re

from echo_workshop.core.models import validate_id


@dataclass(frozen=True)
class LoadoutSnapshot:
    profile_id: str
    inventory_revision: int
    revision: int
    current: tuple
    claims: tuple
    records: tuple

    @property
    def owners(self):
        return dict(self.claims)


def protected_item_ids(row):
    """Legacy rows reserve all five; an explicit empty subset reserves none."""
    if 'protected_item_ids' not in row:
        return tuple(row['item_ids']) if row['protected'] else ()
    values = row['protected_item_ids']
    if (
        not isinstance(values, list)
        or any(not isinstance(value, str) for value in values)
        or len(values) != len(set(values))
        or not set(values) <= set(row['item_ids'])
        or bool(values) != row['protected']
    ):
        raise ValueError('配装保护声骸范围无效')
    return tuple(values)


def validate_loadouts(rows, profiles, by_id):
    """Validate persisted relationships without treating OCR IDs as game UIDs."""
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError('当前配装列表格式错误')
    seen, claimed = set(), set()
    fields = {
        'profile_id',
        'character_id',
        'modal_id',
        'plan_id',
        'label',
        'item_ids',
        'fingerprints',
        'protected',
        'updated_at',
    }
    for row in rows:
        if not isinstance(row, dict) or set(row) not in (fields, fields | {'protected_item_ids'}):
            raise ValueError('当前配装字段错误')
        validate_id(row['profile_id'])
        validate_id(row['plan_id'])
        for key in ('character_id', 'modal_id', 'label', 'updated_at'):
            if not isinstance(row[key], str) or not row[key] or len(row[key]) > 1024:
                raise ValueError('当前配装身份/名称格式错误')
        identity = row['profile_id'], row['character_id']
        if (
            row['profile_id'] not in profiles
            or identity in seen
            or type(row['protected']) is not bool
        ):
            raise ValueError('当前配装档案/角色重复或保护状态错误')
        seen.add(identity)
        ids, prints = row['item_ids'], row['fingerprints']
        if (
            not isinstance(ids, list)
            or len(ids) != 5
            or not all(isinstance(x, str) for x in ids)
            or len(set(ids)) != 5
        ):
            raise ValueError('当前配装须有五件不同声骸')
        if (
            not isinstance(prints, list)
            or len(prints) != 5
            or not all(isinstance(x, str) and re.fullmatch('[0-9a-f]{64}', x) for x in prints)
        ):
            raise ValueError('当前配装物品指纹错误')
        protected = set(protected_item_ids(row))
        for item_id in ids:
            validate_id(item_id)
            record = by_id.get(item_id)
            # Missing evidence retains its relationship for review, never redirects.
            if record is not None and record.profile_id != row['profile_id']:
                raise ValueError('当前配装引用其他档案声骸')
            if item_id in protected:
                claim = row['profile_id'], item_id
                if claim in claimed:
                    raise ValueError('多个角色预留了同一声骸')
                claimed.add(claim)
    return rows


def loadout_issues(row, by_id):
    issues = []
    for item_id, fingerprint in zip(row['item_ids'], row['fingerprints']):
        record = by_id.get(item_id)
        if record is None:
            issues.append('missing_item')
        elif record.profile_id != row['profile_id']:
            issues.append('wrong_profile')
        else:
            if not record.usable:
                issues.append('unusable_item')
            if record.fingerprint != fingerprint:
                issues.append('content_changed')
    return tuple(dict.fromkeys(issues))


def claim_labels(rows, profile_id, records):
    """Presentation labels from the same frozen read as the inventory rows."""
    by_id = {record.item_id: record for record in records}
    validate_loadouts(rows, {profile_id}, by_id)
    labels = {}
    for row in rows:
        protected = protected_item_ids(row)
        if protected:
            issue = '（待复核）' if loadout_issues(row, by_id) else ''
            for item_id in protected:
                labels[item_id] = '已预留：' + row['label'] + issue
    return labels
