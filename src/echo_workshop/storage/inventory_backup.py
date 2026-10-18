# 在同一库存事务中合并备份并重建隔离恢复快照。
import json
import logging

from echo_workshop.core.models import EchoRecord, canonical_json, validate_id
from echo_workshop.core.loadout import validate_loadouts

_PROFILE_LOGGER = logging.getLogger('ok.EchoInventoryProfiles')


class InventoryBackupMixin:
    def export_backup(self, *, cancel_event=None):
        # One transaction-consistent snapshot; raw data, not cached scores.
        with self._lock, self._db:
            self._db.execute('BEGIN')
            profiles = self.profiles()
            from echo_workshop.application.operations import checkpoint

            def decode(text):
                checkpoint(cancel_event)
                return json.loads(text)

            checkpoint(cancel_event)
            items = [
                decode(row[0]) for row in self._db.execute('SELECT payload FROM items ORDER BY id')
            ]
            history = [
                dict(item_id=row[0], record=decode(row[1]), changed_at=row[2])
                for row in self._db.execute(
                    'SELECT item_id,payload,changed_at FROM history ORDER BY sequence'
                )
            ]
            pending = [
                dict(record=decode(row[0]), reason=row[1], candidates=decode(row[2]))
                for row in self._db.execute(
                    'SELECT payload,reason,candidates FROM pending_captures ORDER BY id'
                )
            ]
            backup = {
                'schema_version': self.SCHEMA_VERSION,
                'revision': self.revision,
                'profiles': profiles,
                'items': items,
                'history': history,
                'pending_captures': pending,
                'current_loadouts': self._loadout_rows(),
                'loadout_revision': self.loadout_revision,
            }
            return backup

    def restore_new_snapshot(self, backup, *, cancel_event=None, progress=None):
        """Reconstruct revision only in a fresh isolated recovery database."""
        from echo_workshop.application.operations import checkpoint

        if type(backup.get('revision')) is not int or not 0 <= backup['revision'] < 2**63:
            raise ValueError('Invalid snapshot revision')
        with self._lock:
            if (
                self.revision != 0
                or self.profiles()
                or self._db.execute('SELECT 1 FROM items LIMIT 1').fetchone()
            ):
                raise ValueError('Snapshot restoration requires a new empty database')
            self.import_backup(backup, cancel_event=cancel_event, progress=progress)
            checkpoint(cancel_event)
            with self._db:
                self._db.execute(
                    "UPDATE metadata SET value=? WHERE key='revision'", (backup['revision'],)
                )
                self._db.execute(
                    "UPDATE metadata SET value=? WHERE key='loadout_revision'",
                    (backup.get('loadout_revision', 0),),
                )
            normalized = dict(
                backup,
                schema_version=self.SCHEMA_VERSION,
                current_loadouts=backup.get('current_loadouts', []),
                loadout_revision=backup.get('loadout_revision', 0),
            )
            if canonical_json(self.export_backup(cancel_event=cancel_event)) != canonical_json(
                normalized
            ):
                raise ValueError('Recovered inventory snapshot differs from source')

    def import_backup(self, backup, *, expected_revision=None, cancel_event=None, progress=None):
        from echo_workshop.application.operations import checkpoint, report_progress

        checkpoint(cancel_event)
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 0
        ):
            raise ValueError('Invalid expected inventory revision')
        if (
            not isinstance(backup, dict)
            or type(backup.get('schema_version')) is not int
            or backup['schema_version'] != self.SCHEMA_VERSION
        ):
            raise ValueError('Unsupported backup schema; use a current export')
        try:
            profiles = backup['profiles']
            if not isinstance(profiles, dict):
                raise ValueError('Invalid profiles')
            for key, name in profiles.items():
                validate_id(key)
                if not isinstance(name, str) or not name.strip():
                    raise ValueError('Invalid profile name')

            def decode(row):
                checkpoint(cancel_event)
                return EchoRecord.from_dict(row)

            records = [decode(row) for row in backup['items']]
            if len({r.item_id for r in records}) != len(records) or any(
                r.profile_id not in profiles for r in records
            ):
                raise ValueError('Invalid backup identity')
            history = [
                (row['item_id'], decode(row['record']), row['changed_at'])
                for row in backup.get('history', [])
            ]
            by_id = {record.item_id: record for record in records}
            for item_id, record, changed_at in history:
                if (
                    item_id != record.item_id
                    or item_id not in by_id
                    or record.profile_id != by_id[item_id].profile_id
                    or not isinstance(changed_at, str)
                ):
                    raise ValueError('Invalid history identity')
            pending = [
                (decode(row['record']), row['reason'], row['candidates'])
                for row in backup.get('pending_captures', [])
            ]
            identities = {r.item_id for r in records}
            for record, reason, candidates in pending:
                if (
                    record.item_id in identities
                    or record.profile_id not in profiles
                    or reason not in ('duplicate', 'upgrade')
                ):
                    raise ValueError('Invalid pending capture')
                if (
                    not isinstance(candidates, list)
                    or not candidates
                    or not all(isinstance(item_id, str) for item_id in candidates)
                    or len(set(candidates)) != len(candidates)
                    or not all(
                        item_id in by_id and by_id[item_id].profile_id == record.profile_id
                        for item_id in candidates
                    )
                ):
                    raise ValueError('Invalid capture candidates')
                identities.add(record.item_id)
            loadouts = validate_loadouts(backup.get('current_loadouts', []), profiles, by_id)
            loadout_revision = backup.get('loadout_revision', 0)
            if type(loadout_revision) is not int or not 0 <= loadout_revision < 2**63:
                raise ValueError('Invalid loadout revision')
        except (KeyError, TypeError) as error:
            raise ValueError('Malformed backup') from error
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            checkpoint(cancel_event)
            if expected_revision is not None and self.revision != expected_revision:
                raise ValueError('库存在恢复期间已变化；未导入，请核对后重新开始')
            existing_profiles = self.profiles()
            added = 0
            changed = False
            preserved_names = 0
            for key, name in profiles.items():
                checkpoint(cancel_event)
                if key in existing_profiles and existing_profiles[key] != name:
                    # A profile's UUID, not its mutable display label, is the
                    # merge identity. Preserve local renaming; all item,
                    # pending and loadout conflicts remain strict below.
                    preserved_names += 1
                if key not in existing_profiles:
                    self._db.execute('INSERT INTO profiles VALUES(?,?)', (key, name))
                    changed = True
            new_ids = set()
            for index, record in enumerate(records):
                report_progress(
                    progress, '合并库存（提交前可取消）', index, len(records), cancel_event
                )
                try:
                    existing = self.get(record.item_id)
                except KeyError:
                    self._insert(record)
                    new_ids.add(record.item_id)
                    added += 1
                    changed = True
                else:
                    if existing != record:
                        raise ValueError('Instance conflict; no records were overwritten')
            for item_id, record, changed_at in history:
                checkpoint(cancel_event)
                if item_id in new_ids:
                    self._db.execute(
                        'INSERT INTO history(item_id,payload,changed_at) VALUES(?,?,?)',
                        (item_id, canonical_json(record.to_dict()), changed_at),
                    )
            for record, reason, candidates in pending:
                checkpoint(cancel_event)
                if self._db.execute('SELECT 1 FROM items WHERE id=?', (record.item_id,)).fetchone():
                    raise ValueError('Pending capture conflicts with an inventory instance')
                existing = self._db.execute(
                    'SELECT payload,reason,candidates FROM pending_captures WHERE id=?',
                    (record.item_id,),
                ).fetchone()
                values = (canonical_json(record.to_dict()), reason, canonical_json(candidates))
                if existing is not None and existing != values:
                    raise ValueError('Pending capture conflict')
                if existing is None:
                    self._db.execute(
                        'INSERT INTO pending_captures VALUES(?,?,?,?,?)',
                        (record.item_id, record.profile_id, *values),
                    )
                    changed = True
            changed = self._merge_loadout_backup(loadouts, cancel_event) or changed
            if changed:
                self._bump()
            # This final checkpoint precedes the indivisible SQLite commit.
            # Cancellation arriving after it does not undo a successful commit.
            report_progress(progress, '提交库存', len(records), len(records), cancel_event)
        if preserved_names:
            _PROFILE_LOGGER.info(
                'backup profile labels preserved count=%s imported_items=%s', preserved_names, added
            )
        return added
