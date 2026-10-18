# 持久化当前配装、实例预留和冻结方案关联。
from contextlib import contextmanager
from dataclasses import replace
import json
import logging
from echo_workshop.storage.plans import AllocationStore, validate_plan
from echo_workshop.core.models import EchoRecord, canonical_json, utc_now, validate_id
from echo_workshop.core.loadout import (
    LoadoutSnapshot,
    validate_loadouts,
    loadout_issues,
    protected_item_ids,
)
from echo_workshop.core.cancellation import checkpoint

logger = logging.getLogger('ok.EchoLoadouts')


def validate_current_plan_references(rows, plans, *, plan_evaluator=None, cancel_event=None):
    """Relationships must reference frozen evidence, not today's OCR values.

    A missing/changed current item is retained for review. A missing or
    mismatched historical plan is an incomplete backup, not an empty loadout.
    """
    validated = {}
    if not isinstance(rows, (list, tuple)):
        raise ValueError('当前配装关联集合无效')
    for row in rows:
        checkpoint(cancel_event)
        if (
            not isinstance(row, dict)
            or not {
                'plan_id',
                'profile_id',
                'character_id',
                'modal_id',
                'label',
                'item_ids',
                'fingerprints',
            }
            <= row.keys()
        ):
            raise ValueError('当前配装方案关联字段无效')
        plan_id = row['plan_id']
        validate_id(plan_id)
        if plan_id not in plans:
            raise ValueError('当前配装引用的历史方案缺失：' + plan_id)
        if plan_id not in validated:
            validated[plan_id] = validate_plan(
                plans[plan_id], plan_evaluator=plan_evaluator, cancel_event=cancel_event
            )
        payload = validated[plan_id]
        role = next(
            (
                value
                for value in payload['result']['roles']
                if value['character_id'] == row['character_id']
            ),
            None,
        )
        evidence = {value['item_id']: EchoRecord.from_dict(value) for value in payload['records']}
        if (
            payload['plan_id'] != plan_id
            or payload['request']['profile_id'] != row['profile_id']
            or role is None
            or role['modal_id'] != row['modal_id']
            or role['label'] != row['label']
            or list(role['item_ids']) != row['item_ids']
            or [evidence[item_id].fingerprint for item_id in role['item_ids']]
            != row['fingerprints']
        ):
            raise ValueError('当前配装关联与历史方案证据不一致：' + plan_id)
    checkpoint(cancel_event)


class LoadoutRepositoryMixin:
    """Small inventory-owned persistence surface, no runtime/service registry."""

    def _initialize_loadouts(self):
        try:
            self._db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS current_loadouts(
                    profile_id TEXT NOT NULL REFERENCES profiles(id),
                    character_id TEXT NOT NULL, payload TEXT NOT NULL,
                    protected INTEGER NOT NULL CHECK(protected IN (0,1)),
                    PRIMARY KEY(profile_id, character_id));
                CREATE TABLE IF NOT EXISTS loadout_reservations(
                    profile_id TEXT NOT NULL, item_id TEXT NOT NULL, character_id TEXT NOT NULL,
                    PRIMARY KEY(profile_id, item_id),
                    FOREIGN KEY(profile_id, character_id)
                        REFERENCES current_loadouts(profile_id, character_id) ON DELETE CASCADE);
                INSERT OR IGNORE INTO metadata VALUES('loadout_revision', 0);
                PRAGMA user_version=4;
                COMMIT;
            """)
        except BaseException:
            self._db.rollback()
            raise

    @contextmanager
    def _loadout_lock(self, cancel_event):
        while not self._lock.acquire(timeout=0.05):
            checkpoint(cancel_event)
        try:
            checkpoint(cancel_event)
            yield
        finally:
            self._lock.release()

    @property
    def loadout_revision(self):
        with self._lock:
            if not self._has_loadouts:
                return 0
            return self._db.execute(
                "SELECT value FROM metadata WHERE key='loadout_revision'"
            ).fetchone()[0]

    def _loadout_rows(self, profile_id=None):
        if not self._has_loadouts:
            return []
        sql = 'SELECT payload FROM current_loadouts'
        values = ()
        if profile_id is not None:
            sql += ' WHERE profile_id=?'
            values = (profile_id,)
        return [
            json.loads(row[0])
            for row in self._db.execute(sql + ' ORDER BY profile_id,character_id', values)
        ]

    def loadout_snapshot(self, profile_id, *, cancel_event=None):
        validate_id(profile_id)
        with self._loadout_lock(cancel_event), self._db:
            self._db.execute('BEGIN')
            if profile_id not in self.profiles():
                raise ValueError('档案已变化，请重新选择')
            revision, occupation = self.revision, self.loadout_revision
            rows = self._loadout_rows(profile_id)
            raw = []
            for data in self._db.execute(
                'SELECT payload FROM items WHERE profile_id=? ORDER BY rowid', (profile_id,)
            ):
                checkpoint(cancel_event)
                raw.append(data[0])
            claims = (
                tuple(
                    self._db.execute(
                        'SELECT item_id,character_id FROM loadout_reservations WHERE profile_id=? ORDER BY item_id',
                        (profile_id,),
                    )
                )
                if self._has_loadouts
                else ()
            )
        records = tuple(EchoRecord.from_dict(json.loads(value)) for value in raw)
        by_id = {record.item_id: record for record in records}
        validate_loadouts(rows, {profile_id}, by_id)
        expected = {
            (item_id, row['character_id']) for row in rows for item_id in protected_item_ids(row)
        }
        if expected != set(claims):
            raise ValueError('配装保护关联不一致，请核对数据副本')
        current = tuple(dict(row, issues=loadout_issues(row, by_id)) for row in rows)
        checkpoint(cancel_event)
        return LoadoutSnapshot(profile_id, revision, occupation, current, claims, records)

    def _verify_loadout_revision(self, expected_revision, expected_loadout_revision):
        if (
            type(expected_revision) is not int
            or type(expected_loadout_revision) is not int
            or self.revision != expected_revision
            or self.loadout_revision != expected_loadout_revision
        ):
            raise ValueError('库存或配装保护已变化，请刷新后重试')

    def _bump_loadouts(self):
        self._db.execute("UPDATE metadata SET value=value+1 WHERE key='loadout_revision'")
        self._bump()

    def _write_loadout(self, row):
        self._db.execute(
            'INSERT INTO current_loadouts VALUES(?,?,?,?)',
            (row['profile_id'], row['character_id'], canonical_json(row), int(row['protected'])),
        )
        for item_id in protected_item_ids(row):
            self._db.execute(
                'INSERT INTO loadout_reservations VALUES(?,?,?)',
                (row['profile_id'], item_id, row['character_id']),
            )

    def _prepare_owner_updates(self, profile, prepared, snapshot, claims, cancel_event):
        """Stage explicit local ownership changes without modifying frozen evidence."""
        wanted = {row['character_id'] for row in prepared}
        names = snapshot.characters.names
        if any(character not in names for character in wanted):
            raise ValueError('角色基础名称资料不完整，未保存装备者，请更新资料后重试。')
        assignments = {
            item_id: row['character_id'] for row in prepared for item_id in row['item_ids']
        }
        updates, skipped, now = [], 0, utc_now()
        for (raw,) in self._db.execute(
            'SELECT payload FROM items WHERE profile_id=? ORDER BY rowid', (profile,)
        ):
            checkpoint(cancel_event)
            record = EchoRecord.from_dict(json.loads(raw))
            owner = record.owner_character_id
            if not owner and record.owner:
                observed = snapshot.characters.resolve(record.owner)
                owner = observed.character_id if observed.state == 'equipped' else None
            target = assignments.get(record.item_id)
            # Explicit save assigns selected usable items even if their old
            # unprotected owner differs. Only selected roles' remaining pieces
            # are cleared; never guess an unresolved old owner from fuzzy text.
            if target is None and owner not in wanted:
                continue
            claim = claims.get(record.item_id)
            if claim is not None and claim not in wanted:
                if target is None:
                    # Older protection did not synchronize owner metadata.
                    # Preserve that unrelated protected record instead of
                    # blocking a save whose actual five candidates are free.
                    skipped += 1
                    continue
                raise ValueError('声骸仍被未选择角色保护，未修改其装备。')
            state, name, character = (
                ('equipped', names[target], target)
                if target is not None
                else ('unequipped', '', '')
            )
            if (record.owner_state, record.owner, record.owner_character_id) == (
                state,
                name,
                character,
            ):
                continue
            updated = replace(
                record,
                owner=name,
                owner_character_id=character,
                owner_state=state,
                owner_updated_at=now,
            )
            # owner / local assignment is not part of the physical fingerprint;
            # historical screenshots, scores and saved relationship hashes stay unchanged.
            if updated.fingerprint != record.fingerprint:
                raise ValueError('装备者变更意外改变声骸词条，未保存。')
            updates.append((record, updated))
        return updates, skipped

    def _write_owner_updates(self, updates, cancel_event):
        for previous, updated in updates:
            checkpoint(cancel_event)
            self._db.execute(
                'INSERT INTO history(item_id,payload,changed_at) VALUES(?,?,?)',
                (previous.item_id, canonical_json(previous.to_dict()), updated.owner_updated_at),
            )
            self._db.execute(
                'UPDATE items SET payload=? WHERE id=? AND profile_id=?',
                (canonical_json(updated.to_dict()), updated.item_id, updated.profile_id),
            )

    def commit_current_loadouts(
        self,
        payload,
        *,
        expected_revision,
        expected_loadout_revision,
        replace_plans=None,
        protected=True,
        characters=None,
        update_owners=False,
        cancel_event=None,
        before_commit=None,
    ):
        """Activate saved evidence; all selected characters publish in one DB commit.

        The caller saves/loads the immutable history file first. A failed DB
        commit leaves that file as a harmless historical alternative.
        Only explicit update_owners synchronizes local inventory ownership;
        it leaves game observations and frozen historical evidence untouched.
        """
        validate_plan(payload, plan_evaluator=self.plan_evaluator, cancel_event=cancel_event)
        saved = AllocationStore(self.data_dir, plan_evaluator=self.plan_evaluator).load(
            payload['plan_id'], cancel_event=cancel_event
        )
        if any(
            canonical_json(saved[key]) != canonical_json(payload[key])
            for key in ('request', 'result', 'records', 'weight_snapshot')
        ):
            raise ValueError('历史方案证据已变化，请重新读取')
        if type(protected) is not bool or type(update_owners) is not bool:
            raise ValueError('请明确选择配装保护状态')
        replace_plans = dict(replace_plans or {})
        profile = payload['request']['profile_id']
        rows = payload['result']['roles']
        wanted = {row['character_id'] for row in rows} if characters is None else set(characters)
        if (
            not wanted
            or not wanted <= {row['character_id'] for row in rows}
            or not set(replace_plans) <= wanted
        ):
            raise ValueError('请选择方案中的角色')
        evidence = {row['item_id']: EchoRecord.from_dict(row) for row in payload['records']}
        if update_owners:
            from echo_workshop.resources.weights import WeightSnapshot

            owner_snapshot = WeightSnapshot.from_dict(payload['weight_snapshot'])
        prepared = []
        for row in rows:
            if row['character_id'] not in wanted:
                continue
            if len(row['item_ids']) != 5:
                raise ValueError('只有完整五件套可设为当前配装并保护')
            prepared.append(
                dict(
                    profile_id=profile,
                    character_id=row['character_id'],
                    modal_id=row['modal_id'],
                    plan_id=payload['plan_id'],
                    label=row['label'],
                    item_ids=list(row['item_ids']),
                    fingerprints=[evidence[item_id].fingerprint for item_id in row['item_ids']],
                    protected=protected,
                    updated_at=utc_now(),
                )
            )
        with self._loadout_lock(cancel_event), self._db:
            self._db.execute('BEGIN IMMEDIATE')
            self._verify_loadout_revision(expected_revision, expected_loadout_revision)
            current = {row['character_id']: row for row in self._loadout_rows(profile)}
            if profile not in self.profiles():
                raise ValueError('方案所属档案已变化')
            owners = dict(
                self._db.execute(
                    'SELECT item_id,character_id FROM loadout_reservations WHERE profile_id=?',
                    (profile,),
                )
            )
            for row in prepared:
                checkpoint(cancel_event)
                character = row['character_id']
                old = current.get(character)
                if old is not None and replace_plans.get(character) != old['plan_id']:
                    raise ValueError('角色已有当前配装，请明确选择替换')
                if character in replace_plans and (
                    old is None or old['plan_id'] != replace_plans[character]
                ):
                    raise ValueError('待替换的当前配装已变化')
                for item_id, fingerprint in zip(row['item_ids'], row['fingerprints']):
                    try:
                        record = self.get(item_id)
                    except KeyError as error:
                        raise ValueError('声骸已缺失，请重新计算') from error
                    if (
                        record.profile_id != profile
                        or not record.usable
                        or record.fingerprint != fingerprint
                    ):
                        raise ValueError('声骸内容或可用状态已变化，请重新计算')
                    if record.locked:
                        raise ValueError('声骸已保护，请修改要求后重新计算')
                    if (
                        item_id in owners
                        and owners[item_id] != character
                        and (not update_owners or owners[item_id] not in wanted)
                    ):
                        raise ValueError('声骸已被其他角色预留，请先解除对应保护')
            updates, skipped = (
                self._prepare_owner_updates(profile, prepared, owner_snapshot, owners, cancel_event)
                if update_owners
                else ((), 0)
            )
            for row in prepared:
                self._db.execute(
                    'DELETE FROM current_loadouts WHERE profile_id=? AND character_id=?',
                    (profile, row['character_id']),
                )
            for row in prepared:
                self._write_loadout(row)
            self._write_owner_updates(updates, cancel_event)
            self._bump_loadouts()
            if before_commit is not None:
                before_commit()
            checkpoint(cancel_event)
        # No checkpoint after publication: late cancellation does not undo a commit.
        if skipped:
            logger.warning('loadout owner cleanup skipped protected_non_candidates=%s', skipped)
        logger.info(
            'loadout committed roles=%s owner_updates=%s inventory_revision=%s loadout_revision=%s',
            len(prepared),
            len(updates),
            self.revision,
            self.loadout_revision,
        )
        return tuple(prepared)

    def change_current_loadout(
        self,
        profile_id,
        character_id,
        action,
        *,
        expected_revision,
        expected_loadout_revision,
        cancel_event=None,
        before_commit=None,
    ):
        if action not in ('release', 'remove'):
            raise ValueError('未知配装操作')
        with self._loadout_lock(cancel_event), self._db:
            self._db.execute('BEGIN IMMEDIATE')
            self._verify_loadout_revision(expected_revision, expected_loadout_revision)
            rows = {row['character_id']: row for row in self._loadout_rows(profile_id)}
            if character_id not in rows:
                raise ValueError('当前配装已变化，请刷新')
            row = rows[character_id]
            self._db.execute(
                'DELETE FROM current_loadouts WHERE profile_id=? AND character_id=?',
                (profile_id, character_id),
            )
            if action == 'release':
                self._write_loadout(
                    dict(row, protected=False, protected_item_ids=[], updated_at=utc_now())
                )
            self._bump_loadouts()
            if before_commit is not None:
                before_commit()
            checkpoint(cancel_event)
        return True

    def release_loadout_items(
        self,
        profile_id,
        character_id,
        plan_id,
        item_ids,
        *,
        expected_revision,
        expected_loadout_revision,
        cancel_event=None,
        before_commit=None,
    ):
        """Release only explicit selected reservations, retaining the frozen role."""
        validate_id(profile_id)
        validate_id(plan_id)
        if (
            not isinstance(item_ids, (list, tuple))
            or not item_ids
            or any(not isinstance(value, str) for value in item_ids)
            or len(item_ids) != len(set(item_ids))
        ):
            raise ValueError('请选择需要解除保护的声骸')
        for item_id in item_ids:
            validate_id(item_id)
        wanted = set(item_ids)
        with self._loadout_lock(cancel_event), self._db:
            self._db.execute('BEGIN IMMEDIATE')
            self._verify_loadout_revision(expected_revision, expected_loadout_revision)
            row = next(
                (
                    value
                    for value in self._loadout_rows(profile_id)
                    if value['character_id'] == character_id
                ),
                None,
            )
            if row is None or row['plan_id'] != plan_id:
                raise ValueError('当前角色配装已变化，请重新选择')
            protected = set(protected_item_ids(row))
            if not wanted <= set(row['item_ids']):
                raise ValueError('请选择当前角色配装中的声骸')
            claims = dict(
                self._db.execute(
                    'SELECT item_id,character_id FROM loadout_reservations WHERE profile_id=?',
                    (profile_id,),
                )
            )
            updates = []
            for item_id in wanted:
                checkpoint(cancel_event)
                if claims.get(item_id) not in (None, character_id):
                    raise ValueError('所选声骸已被其他角色保护，请重新选择')
                try:
                    record = self.get(item_id)
                except KeyError:
                    continue
                if record.profile_id != profile_id:
                    raise ValueError('所选声骸不属于当前档案')
                if record.locked:
                    updates.append((record, replace(record, locked=False)))
            if not wanted <= protected | {old.item_id for old, _ in updates}:
                raise ValueError('所选声骸不再属于此配装的保护范围')
            remaining = [item_id for item_id in row['item_ids'] if item_id in protected - wanted]
            self._db.execute(
                'DELETE FROM current_loadouts WHERE profile_id=? AND character_id=?',
                (profile_id, character_id),
            )
            self._write_loadout(
                dict(
                    row,
                    protected=bool(remaining),
                    protected_item_ids=remaining,
                    updated_at=utc_now(),
                )
            )
            for old, new in updates:
                checkpoint(cancel_event)
                self._db.execute(
                    'INSERT INTO history(item_id,payload,changed_at) VALUES(?,?,?)',
                    (old.item_id, canonical_json(old.to_dict()), utc_now()),
                )
                self._db.execute(
                    'UPDATE items SET payload=? WHERE id=? AND profile_id=?',
                    (canonical_json(new.to_dict()), new.item_id, profile_id),
                )
            self._bump_loadouts()
            if before_commit is not None:
                before_commit()
            checkpoint(cancel_event)
        logger.info(
            'loadout items released count=%s inventory_revision=%s loadout_revision=%s',
            len(wanted),
            self.revision,
            self.loadout_revision,
        )
        return len(wanted)

    def _merge_loadout_backup(self, rows, cancel_event):
        """Called inside inventory import's transaction, never partially merge."""
        existing = {(row['profile_id'], row['character_id']): row for row in self._loadout_rows()}
        changed = False
        for row in rows:
            checkpoint(cancel_event)
            identity = row['profile_id'], row['character_id']
            if identity in existing:
                if existing[identity] != row:
                    raise ValueError('当前配装冲突；原数据保留')
                continue
            if row['protected']:
                for item_id in protected_item_ids(row):
                    if self._db.execute(
                        'SELECT 1 FROM loadout_reservations WHERE profile_id=? AND item_id=?',
                        (row['profile_id'], item_id),
                    ).fetchone():
                        raise ValueError('导入配装与已有预留冲突；原数据保留')
            self._write_loadout(row)
            changed = True
        if changed:
            self._db.execute("UPDATE metadata SET value=value+1 WHERE key='loadout_revision'")
        return changed
