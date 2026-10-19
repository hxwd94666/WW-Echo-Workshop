# 持久化库存档案及修订信息。
"""Transactional local inventory. Data lives outside the script install folder."""

from __future__ import annotations

from dataclasses import replace
import json
import logging
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4

from echo_workshop.core.models import EchoRecord, canonical_json, utc_now, validate_id
from echo_workshop.storage.loadouts import LoadoutRepositoryMixin
from echo_workshop.core.loadout import claim_labels, unprotected_loadout
from echo_workshop.storage.inventory_backup import InventoryBackupMixin
from echo_workshop.storage.revision import RevisionProbe
from echo_workshop.storage.screenshot_cleanup import ScreenshotCleanupMixin


_PROFILE_LOGGER = logging.getLogger('ok.EchoInventoryProfiles')


class EchoRepository(InventoryBackupMixin, LoadoutRepositoryMixin, ScreenshotCleanupMixin):
    SCHEMA_VERSION = 4

    def __init__(self, data_dir, *, read_only=False, plan_evaluator=None):
        self.plan_evaluator = plan_evaluator
        self.data_dir = Path(data_dir).resolve()
        if not read_only:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        database = self.data_dir / 'inventory.sqlite3'
        self._revision_probe = RevisionProbe(database)
        self._connection = sqlite3.connect(
            database.as_uri() + '?mode=ro' if read_only else database,
            uri=read_only,
            check_same_thread=False,
        )
        self._db.execute('PRAGMA foreign_keys=ON')
        self._db.execute('PRAGMA busy_timeout=5000')
        try:
            version = self._db.execute('PRAGMA user_version').fetchone()[0]
            self._has_loadouts = version == self.SCHEMA_VERSION
            if read_only:
                if version != self.SCHEMA_VERSION:
                    raise ValueError('只读工作区需要当前库存 schema；旧版不再支持')
                return
            if version not in (0, self.SCHEMA_VERSION):
                raise ValueError(
                    f'旧版库存 schema 不再支持：{version}；原文件未改动，请使用当前版本的备份'
                )
            with self._db:
                self._db.executescript("""
                    CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, name TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS items(
                        id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES profiles(id),
                        fingerprint TEXT NOT NULL, payload TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS items_fingerprint ON items(profile_id, fingerprint);
                    CREATE TABLE IF NOT EXISTS history(
                        sequence INTEGER PRIMARY KEY, item_id TEXT NOT NULL,
                        payload TEXT NOT NULL, changed_at TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                    INSERT OR IGNORE INTO metadata VALUES('revision', 0);
                    CREATE TABLE IF NOT EXISTS pending_captures(
                        id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES profiles(id),
                        payload TEXT NOT NULL, reason TEXT NOT NULL, candidates TEXT NOT NULL);
                """)
            self._initialize_loadouts()
            self._has_loadouts = True
        except Exception:
            self._db.close()
            raise

    def close(self):
        with self._lock:
            if not self._closed:
                self._revision_probe.close()
                self._connection.close()
                self._closed = True

    @property
    def _db(self):
        if self._closed:
            raise ValueError('Inventory service has closed; reopen the plugin')
        return self._connection

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @property
    def revision(self):
        with self._lock:
            return self._db.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0]

    def _bump(self):
        self._db.execute("UPDATE metadata SET value=value+1 WHERE key='revision'")

    def create_profile(self, name):
        if not isinstance(name, str) or not name.strip():
            raise ValueError('Profile name is required')
        profile_id = str(uuid4())
        with self._lock, self._db:
            self._db.execute('INSERT INTO profiles VALUES(?,?)', (profile_id, name.strip()))
            self._bump()
        return profile_id

    def profiles(self):
        with self._lock:
            return dict(self._db.execute('SELECT id,name FROM profiles ORDER BY id').fetchall())

    def rename_profile(self, profile_id, name, *, expected_revision):
        """Change a display label only; every physical/plan/profile ID remains.

        The optimistic revision is checked in the write transaction, including
        for a normalized no-op. Older backups with this UUID retain this local
        label when merged, rather than constructing another profile.
        """
        validate_id(profile_id)
        if not isinstance(name, str):
            raise ValueError('请输入有效档案名称。')
        name = name.strip()
        if not name or len(name) > 80 or any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise ValueError('档案名称应为 1–80 个字符，且不含换行或控制字符。')
        try:
            name.encode('utf8')
        except UnicodeEncodeError as error:
            raise ValueError('档案名称包含无效字符。') from error
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('档案修订无效；请刷新后重试。')
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            if self.revision != expected_revision:
                raise ValueError('声骸库已变化，请刷新后重新命名。')
            row = self._db.execute('SELECT name FROM profiles WHERE id=?', (profile_id,)).fetchone()
            if row is None:
                raise ValueError('当前档案已变化，请重新选择。')
            if row[0] == name:
                return False
            self._db.execute('UPDATE profiles SET name=? WHERE id=?', (name, profile_id))
            self._bump()
            revision = self.revision
        _PROFILE_LOGGER.info('profile rename committed renamed=1 inventory_revision=%s', revision)
        return True

    def _insert(self, record, *, from_capture=False):
        if (
            not from_capture
            and self._db.execute(
                'SELECT 1 FROM pending_captures WHERE id=?', (record.item_id,)
            ).fetchone()
        ):
            raise ValueError('Instance ID already belongs to a pending capture')
        self._db.execute(
            'INSERT INTO items VALUES(?,?,?,?)',
            (
                record.item_id,
                record.profile_id,
                record.fingerprint,
                canonical_json(record.to_dict()),
            ),
        )

    def add(self, record):
        # Deliberately permits equal fingerprints with different UUIDs.
        if not isinstance(record, EchoRecord):
            raise ValueError('Expected EchoRecord')
        with self._lock, self._db:
            self._insert(record)
            self._bump()
        return record.item_id

    def get(self, item_id):
        with self._lock:
            row = self._db.execute('SELECT payload FROM items WHERE id=?', (item_id,)).fetchone()
        if row is None:
            raise KeyError(item_id)
        return EchoRecord.from_dict(json.loads(row[0]))

    def list(self, profile_id):
        with self._lock:
            rows = self._db.execute(
                'SELECT payload FROM items WHERE profile_id=? ORDER BY rowid', (profile_id,)
            ).fetchall()
        return [EchoRecord.from_dict(json.loads(row[0])) for row in rows]

    def duplicate_candidates(self, record):
        with self._lock:
            rows = self._db.execute(
                'SELECT id FROM items WHERE profile_id=? AND fingerprint=?',
                (record.profile_id, record.fingerprint),
            ).fetchall()
        return [self.get(row[0]) for row in rows if row[0] != record.item_id]

    def profile_counts(self, profile_id, *, expected_revision):
        validate_id(profile_id)
        with self._lock:
            if type(expected_revision) is not int or self.revision != expected_revision:
                raise ValueError('声骸库已变化，请刷新后重试')
            if not self._db.execute('SELECT 1 FROM profiles WHERE id=?', (profile_id,)).fetchone():
                raise ValueError('档案已变化，请重新选择')
            return {
                name: self._db.execute(
                    f'SELECT COUNT(*) FROM {table} WHERE profile_id=?', (profile_id,)
                ).fetchone()[0]
                for name, table in (
                    ('items', 'items'),
                    ('pending', 'pending_captures'),
                    ('loadouts', 'current_loadouts'),
                )
            }

    def clear_profile(self, profile_id, *, expected_revision):
        """Clear one profile atomically; retain other profiles and evidence files."""
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            counts = self.profile_counts(profile_id, expected_revision=expected_revision)
            self._db.execute(
                'DELETE FROM history WHERE item_id IN (SELECT id FROM items WHERE profile_id=?)',
                (profile_id,),
            )
            self._db.execute('DELETE FROM pending_captures WHERE profile_id=?', (profile_id,))
            self._db.execute('DELETE FROM current_loadouts WHERE profile_id=?', (profile_id,))
            self._db.execute('DELETE FROM items WHERE profile_id=?', (profile_id,))
            if counts['loadouts']:
                self._db.execute("UPDATE metadata SET value=value+1 WHERE key='loadout_revision'")
            if any(counts.values()):
                self._bump()
            return counts

    def delete_profile(self, profile_id, *, expected_revision):
        """Remove one DB identity only; the caller preserves a recovery archive.

        Frozen plans stay intact; runtime cleanup removes unreferenced images after commit.
        Reservations disappear with their current-loadout relationships, in
        the same transaction. At least one profile must remain.
        """
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('档案修订无效；请刷新后重试。')
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            counts = self.profile_counts(profile_id, expected_revision=expected_revision)
            if self._db.execute('SELECT COUNT(*) FROM profiles').fetchone()[0] <= 1:
                raise ValueError('请至少保留一个档案；最后一个档案请保留。')
            counts['history'] = self._db.execute(
                'SELECT COUNT(*) FROM history WHERE item_id IN (SELECT id FROM items WHERE profile_id=?)',
                (profile_id,),
            ).fetchone()[0]
            counts['reservations'] = self._db.execute(
                'SELECT COUNT(*) FROM loadout_reservations WHERE profile_id=?', (profile_id,)
            ).fetchone()[0]
            self._db.execute(
                'DELETE FROM history WHERE item_id IN (SELECT id FROM items WHERE profile_id=?)',
                (profile_id,),
            )
            self._db.execute('DELETE FROM pending_captures WHERE profile_id=?', (profile_id,))
            self._db.execute('DELETE FROM current_loadouts WHERE profile_id=?', (profile_id,))
            self._db.execute('DELETE FROM items WHERE profile_id=?', (profile_id,))
            self._db.execute('DELETE FROM profiles WHERE id=?', (profile_id,))
            if counts['loadouts'] or counts['reservations']:
                self._db.execute("UPDATE metadata SET value=value+1 WHERE key='loadout_revision'")
            self._bump()
            counts['profiles'] = 1
            revision = self.revision
        _PROFILE_LOGGER.info(
            'profile delete committed profiles=1 items=%s pending=%s loadouts=%s inventory_revision=%s',
            counts['items'],
            counts['pending'],
            counts['loadouts'],
            revision,
        )
        return counts

    def inventory_snapshot(self, profile_id, *, cancel_event=None, include_loadouts=False):
        """Pin records and revision together, without exposing a DB handle."""
        from echo_workshop.core.cancellation import checkpoint

        while not self._lock.acquire(timeout=0.05):
            checkpoint(cancel_event)
        try:
            checkpoint(cancel_event)
            with self._db:
                self._db.execute('BEGIN')
                revision = self.revision
                payloads = []
                for row in self._db.execute(
                    'SELECT payload FROM items WHERE profile_id=? ORDER BY rowid', (profile_id,)
                ):
                    checkpoint(cancel_event)
                    payloads.append(row[0])
                loadouts = self._loadout_rows(profile_id) if include_loadouts else ()
                checkpoint(cancel_event)
        finally:
            self._lock.release()
        # The immutable JSON strings and revision were pinned in the same read transaction.
        # Decoding/validation needs no database mutex; other Qt windows can read status meanwhile.
        records = []
        for payload in payloads:
            checkpoint(cancel_event)
            records.append(EchoRecord.from_dict(json.loads(payload)))
        checkpoint(cancel_event)
        if include_loadouts:
            return revision, tuple(records), claim_labels(loadouts, profile_id, records)
        return revision, tuple(records)

    def try_revision(self):
        """Nonblocking GUI probe; None means busy, not unchanged or empty."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            db = self._db
            # A same-thread transaction must see its own uncommitted revision.
            # Ordinary GUI polling reads a separate zero-wait connection and
            # never changes the writer's timeout or returns a cached old value.
            if db.in_transaction:
                return self._revision_probe.read(db)
            return self._revision_probe.read()
        finally:
            self._lock.release()

    def review_snapshot(self, profile_id, *, cancel_event=None, include_loadouts=False):
        """Inventory and pending decisions in one transaction; decode off-lock."""
        from echo_workshop.core.cancellation import checkpoint

        while not self._lock.acquire(timeout=0.05):
            checkpoint(cancel_event)
        try:
            checkpoint(cancel_event)
            with self._db:
                self._db.execute('BEGIN')
                revision = self.revision
                items, pending = [], []
                for row in self._db.execute(
                    'SELECT payload FROM items WHERE profile_id=? ORDER BY rowid', (profile_id,)
                ):
                    checkpoint(cancel_event)
                    items.append(row[0])
                for row in self._db.execute(
                    'SELECT payload,reason,candidates FROM pending_captures WHERE profile_id=? ORDER BY rowid',
                    (profile_id,),
                ):
                    checkpoint(cancel_event)
                    pending.append(row)
                loadouts = self._loadout_rows(profile_id) if include_loadouts else ()
                checkpoint(cancel_event)
        finally:
            self._lock.release()
        rows = []
        for payload in items:
            checkpoint(cancel_event)
            rows.append((EchoRecord.from_dict(json.loads(payload)), 'item', ()))
        for payload, reason, candidates in pending:
            checkpoint(cancel_event)
            rows.append(
                (EchoRecord.from_dict(json.loads(payload)), reason, tuple(json.loads(candidates)))
            )
        checkpoint(cancel_event)
        if include_loadouts:
            return (
                revision,
                tuple(rows),
                claim_labels(
                    loadouts, profile_id, (record for record, kind, _ in rows if kind == 'item')
                ),
            )
        return revision, tuple(rows)

    def update(self, record, *, expected_revision):
        if not isinstance(record, EchoRecord):
            raise ValueError('Expected EchoRecord')
        with self._lock, self._db:
            # BEGIN IMMEDIATE also protects the revision check across processes.
            self._db.execute('BEGIN IMMEDIATE')
            if self.revision != expected_revision:
                raise ValueError('Inventory changed; reload before updating')
            previous = self.get(record.item_id)
            if previous.profile_id != record.profile_id:
                raise ValueError('Moving an instance between profiles requires explicit migration')
            self._db.execute(
                'INSERT INTO history(item_id,payload,changed_at) VALUES(?,?,?)',
                (record.item_id, canonical_json(previous.to_dict()), utc_now()),
            )
            self._db.execute(
                'UPDATE items SET fingerprint=?,payload=? WHERE id=?',
                (record.fingerprint, canonical_json(record.to_dict()), record.item_id),
            )
            self._bump()

    def _selection_records(self, profile_id, identities, expected_revision):
        if type(expected_revision) is not int or self.revision != expected_revision:
            raise ValueError('声骸库已变化，请刷新后重新选择。')
        identities = tuple(dict.fromkeys(identities))
        if not identities or len(identities) > 10000:
            raise ValueError('请先选择声骸。')
        rows = []
        for identity in identities:
            validate_id(identity)
            try:
                record = self.get(identity)
                kind = 'item'
            except KeyError:
                pending = self._db.execute(
                    'SELECT payload FROM pending_captures WHERE id=?', (identity,)
                ).fetchone()
                if pending is None:
                    raise ValueError('所选声骸已变化，请刷新。')
                record, kind = EchoRecord.from_dict(json.loads(pending[0])), 'pending'
            if record.profile_id != profile_id:
                raise ValueError('所选声骸不属于当前档案。')
            rows.append((record, kind))
        return tuple(rows)

    def selection_impact(self, profile_id, identities, *, expected_revision):
        with self._lock:
            rows = self._selection_records(profile_id, identities, expected_revision)
            identities = {record.item_id for record, _ in rows}
            loadouts = tuple(
                row['label']
                for row in self._loadout_rows(profile_id)
                if identities.intersection(row['item_ids'])
            )
            pending = sum(
                identity not in identities and bool(identities.intersection(json.loads(candidates)))
                for identity, candidates in self._db.execute(
                    'SELECT id,candidates FROM pending_captures WHERE profile_id=?', (profile_id,)
                )
            )
            return dict(
                loadouts=loadouts, related_pending=pending, loadout_revision=self.loadout_revision
            )

    def mutate_selection(
        self, profile_id, identities, action, *, expected_revision, expected_loadout_revision
    ):
        """One transaction for the whole selection, never a partial batch."""
        if action not in ('lock', 'unlock', 'delete'):
            raise ValueError('未知声骸操作。')
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            rows = self._selection_records(profile_id, identities, expected_revision)
            if self.loadout_revision != expected_loadout_revision:
                raise ValueError('当前配装已变化，请重新确认。')
            identities = {record.item_id for record, _ in rows}
            changed = 0
            if action != 'delete':
                if any(kind != 'item' for _, kind in rows):
                    raise ValueError('请先核对待判定记录，再保护。')
                for record, _ in rows:
                    locked = action == 'lock'
                    if record.locked == locked:
                        continue
                    self._db.execute(
                        'INSERT INTO history(item_id,payload,changed_at) VALUES(?,?,?)',
                        (record.item_id, canonical_json(record.to_dict()), utc_now()),
                    )
                    self._db.execute(
                        'UPDATE items SET payload=? WHERE id=?',
                        (canonical_json(replace(record, locked=locked).to_dict()), record.item_id),
                    )
                    changed += 1
            else:
                affected = [
                    row
                    for row in self._loadout_rows(profile_id)
                    if identities.intersection(row['item_ids'])
                ]
                for row in affected:
                    self._db.execute(
                        'DELETE FROM current_loadouts WHERE profile_id=? AND character_id=?',
                        (profile_id, row['character_id']),
                    )
                    self._write_loadout(unprotected_loadout(row, updated_at=utc_now()))
                if affected:
                    self._bump_loadouts()
                related = [
                    identity
                    for identity, candidates in self._db.execute(
                        'SELECT id,candidates FROM pending_captures WHERE profile_id=?',
                        (profile_id,),
                    )
                    if identities.intersection(json.loads(candidates))
                ]
                for identity in identities | set(related):
                    self._db.execute('DELETE FROM history WHERE item_id=?', (identity,))
                    self._db.execute(
                        'DELETE FROM pending_captures WHERE id=? AND profile_id=?',
                        (identity, profile_id),
                    )
                    self._db.execute(
                        'DELETE FROM items WHERE id=? AND profile_id=?', (identity, profile_id)
                    )
                changed = len(rows)
            if changed:
                self._bump()
            return changed

    def confirm(self, item_id, *, expected_revision):
        # The application layer validates native quality before this write.
        record = self.get(item_id)
        self.update(replace(record, review='confirmed'), expected_revision=expected_revision)

    def history(self, item_id):
        with self._lock:
            rows = self._db.execute(
                'SELECT payload FROM history WHERE item_id=? ORDER BY sequence', (item_id,)
            ).fetchall()
        return [EchoRecord.from_dict(json.loads(row[0])) for row in rows]

    def stage_capture(self, record, reason, candidates=()):
        if not isinstance(record, EchoRecord) or reason not in ('duplicate', 'upgrade'):
            raise ValueError('Unknown capture review reason')
        candidates = tuple(candidates)
        if not candidates or len(set(candidates)) != len(candidates):
            raise ValueError('Choose distinct existing capture candidates')
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            if self._db.execute('SELECT 1 FROM items WHERE id=?', (record.item_id,)).fetchone():
                raise ValueError('Capture ID already belongs to an inventory instance')
            for item_id in candidates:
                try:
                    candidate = self.get(item_id)
                except KeyError as error:
                    raise ValueError('Capture candidate is missing') from error
                if candidate.profile_id != record.profile_id:
                    raise ValueError('Capture candidate is unavailable in this profile')
            self._db.execute(
                'INSERT INTO pending_captures VALUES(?,?,?,?,?)',
                (
                    record.item_id,
                    record.profile_id,
                    canonical_json(record.to_dict()),
                    reason,
                    canonical_json(list(candidates)),
                ),
            )
            self._bump()
        return record.item_id

    def pending_captures(self, profile_id):
        with self._lock:
            rows = self._db.execute(
                'SELECT payload,reason,candidates FROM pending_captures WHERE profile_id=? ORDER BY rowid',
                (profile_id,),
            ).fetchall()
        return [
            (EchoRecord.from_dict(json.loads(payload)), reason, tuple(json.loads(candidates)))
            for payload, reason, candidates in rows
        ]

    def resolve_capture(
        self, capture_id, action, *, expected_revision, corrected=None, target_id=None
    ):
        if action not in ('new', 'upgrade', 'refresh', 'discard'):
            raise ValueError('Unknown review action')
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            if self.revision != expected_revision:
                raise ValueError('Inventory changed; reload before resolving')
            row = self._db.execute(
                'SELECT payload,reason,candidates FROM pending_captures WHERE id=?', (capture_id,)
            ).fetchone()
            if row is None:
                raise KeyError(capture_id)
            original = EchoRecord.from_dict(json.loads(row[0]))
            record = corrected or original
            if record.item_id != capture_id or record.profile_id != original.profile_id:
                raise ValueError('Capture identity cannot change during correction')
            if action != 'discard':
                # The editor obtains native quality facts before this write; the
                # storage layer must not re-evaluate a second rule set.
                record = replace(record, review='confirmed')
                if action == 'new':
                    self._insert(record, from_capture=True)
                else:
                    expected_kind = 'duplicate' if action == 'refresh' else 'upgrade'
                    if row[1] != expected_kind or target_id not in json.loads(row[2]):
                        raise ValueError('Choose an explicit suggested upgrade target')
                    previous = self.get(target_id)
                    if previous.profile_id != record.profile_id:
                        raise ValueError('Upgrade target is no longer available')
                    if action == 'refresh' and previous.fingerprint != record.fingerprint:
                        raise ValueError('Refresh requires an unchanged attribute fingerprint')
                    self._db.execute(
                        'INSERT INTO history(item_id,payload,changed_at) VALUES(?,?,?)',
                        (target_id, canonical_json(previous.to_dict()), utc_now()),
                    )
                    observed_owner = bool(record.owner) or record.owner_state == 'unequipped'
                    from echo_workshop.capture.game_status import merge_game_status

                    record = merge_game_status(previous, record)
                    record = replace(
                        record,
                        item_id=target_id,
                        locked=previous.locked,
                        note=previous.note,
                        handled=previous.handled,
                        owner=record.owner if observed_owner else previous.owner,
                        owner_state=record.owner_state if observed_owner else previous.owner_state,
                        owner_character_id=record.owner_character_id
                        if observed_owner
                        else previous.owner_character_id,
                        owner_updated_at=record.owner_updated_at
                        if observed_owner
                        else previous.owner_updated_at,
                    )
                    self._db.execute(
                        'UPDATE items SET fingerprint=?,payload=? WHERE id=?',
                        (record.fingerprint, canonical_json(record.to_dict()), target_id),
                    )
            self._db.execute('DELETE FROM pending_captures WHERE id=?', (capture_id,))
            self._bump()
            return None if action == 'discard' else record
