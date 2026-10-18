# 发布档案专属配置并消费已提交数据作业回执。
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import tempfile

from echo_workshop.core.cancellation import OperationCancelled, checkpoint
from echo_workshop.core.models import canonical_json, validate_id
from echo_workshop.resources.http import strict_json
from echo_workshop.storage.profile_transfer import merge_profile_settings
from echo_workshop.storage.workspace_bundle import PREFERENCE_KEYS

PROFILE_SETTINGS = ('ComparisonSettingsJSON', 'AllocationSettingsJSON')
_LOG = logging.getLogger('ok.EchoInventoryProfiles')


@dataclass(frozen=True)
class ProfilePublication:
    active_id: str
    changed_keys: tuple = ()


@dataclass(frozen=True)
class ReceiptReport:
    consumed: int = 0
    deferred: tuple = ()
    cancelled: bool = False


class ProfileSettingsService:
    """Explicit Config port: validation/setters remain authoritative, no UI state."""

    def __init__(self, config, inventory, data_dir):
        self.config = config
        self.inventory = inventory
        self.data_dir = Path(data_dir).resolve()

    def apply(self, settings=(), *, removed=None, active=None, cancel_event=None):
        checkpoint(cancel_event)
        profiles = self.inventory.profiles()
        if not profiles:
            raise ValueError('请至少保留一个档案。')
        before = dict(self.config)
        preferences = {key: before.get(key) for key in PREFERENCE_KEYS}
        target = active or before.get('InventoryProfileID')
        if target not in profiles:
            target = next(iter(profiles))
        preferences['InventoryProfileID'] = target
        if removed is not None:
            validate_id(removed)
        for key in PROFILE_SETTINGS:
            if removed and preferences[key]:
                value = strict_json(preferences[key])
                value['profiles'].pop(removed, None)
                preferences[key] = canonical_json(value)
        updates = merge_profile_settings(
            preferences, settings or tuple((key, '') for key in PROFILE_SETTINGS), profiles
        )
        updates['InventoryProfileID'] = target
        changed = {key: value for key, value in updates.items() if before.get(key) != value}
        self.publish(changed, before, cancel_event=cancel_event)
        return ProfilePublication(target, tuple(changed))

    def publish(self, changed, before, *, cancel_event=None):
        """CAS the complete original file while changing only three account keys."""
        checkpoint(cancel_event)
        if not changed:
            return
        if set(changed) - {*PROFILE_SETTINGS, 'InventoryProfileID'}:
            raise ValueError('档案设置字段异常。')
        config = self.config
        original = Path(config.config_file).absolute()
        if original.is_symlink():
            raise ValueError('插件设置路径或大小异常。')
        path = original.resolve()
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('插件设置路径或大小异常。')
        raw = path.read_bytes()
        if len(raw) > 16 * 1024 * 1024 or strict_json(raw) != before:
            raise ValueError('插件设置已变化，请重启后重新导入。')
        if any(not config.validate(key, value) for key, value in changed.items()):
            raise ValueError('插件设置校验未通过。')
        had_hook = 'save_file' in config.__dict__
        hook = config.save_file
        temporary = None
        try:
            config.save_file = lambda: None
            for key, value in changed.items():
                config[key] = value
            if any(config.get(key) != value for key, value in changed.items()):
                raise ValueError('设置未接受全部档案字段。')
            data = canonical_json(dict(config)).encode('utf8')
            fd, temporary = tempfile.mkstemp(prefix='.profile-config-', dir=path.parent)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if path.read_bytes() != raw:
                raise ValueError('保存前设置已变化，原设置保留。')
            checkpoint(cancel_event)
            os.replace(temporary, path)
            temporary = None
        except Exception:
            # The save hook is still suppressed while restoring in-memory values.
            for key in changed:
                config[key] = before.get(key)
            raise
        finally:
            if had_hook:
                config.save_file = hook
            else:
                del config.save_file
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def repair_active(self, profiles, preferred=None, *, visible=None, cancel_event=None):
        """Repair only the active ID, independent of malformed unrelated rule rows."""
        if not profiles:
            raise ValueError('请至少保留一个档案。')
        before = dict(self.config)
        target = preferred or before.get('InventoryProfileID')
        if target not in profiles:
            target = visible if visible in profiles else next(iter(profiles))
        if before.get('InventoryProfileID') != target:
            self.publish({'InventoryProfileID': target}, before, cancel_event=cancel_event)
        return target

    def consume_import_receipts(self, *, cancel_event=None):
        return self._consume('import', cancel_event)

    def consume_delete_receipts(self, *, cancel_event=None):
        return self._consume('delete', cancel_event)

    def _consume(self, kind, cancel_event):
        name = 'profile-import-receipts' if kind == 'import' else 'profile-delete-receipts'
        folder = self.data_dir / name
        if not folder.exists():
            return ReceiptReport()
        if folder.is_symlink() or folder.resolve().parent != self.data_dir:
            message = (
                '匹配配置恢复目录异常，原设置保留。'
                if kind == 'import'
                else '档案删除回执目录异常，原设置保留。'
            )
            return ReceiptReport(deferred=(message,))
        limit = 16 * 1024 * 1024 if kind == 'import' else 8192
        consumed = 0
        deferred = []
        for path in sorted(folder.glob('*.json'))[:100]:
            try:
                checkpoint(cancel_event)
                if path.is_symlink() or path.stat().st_size > limit:
                    raise ValueError('档案回执路径或大小异常。')
                raw = path.read_bytes()
                if len(raw) > limit:
                    raise ValueError('档案回执超过大小限制。')
                data = strict_json(raw)
                if kind == 'import':
                    self._apply_import_receipt(data, cancel_event)
                else:
                    self._apply_delete_receipt(data, cancel_event)
                # A successful publication consumes only this exact receipt version.
                if path.read_bytes() == raw:
                    path.unlink()
                    consumed += 1
                    _LOG.info('committed profile receipt consumed kind=%s', kind)
                else:
                    deferred.append('回执已变化，已提交的档案配置保留，新回执待重试。')
                    _LOG.warning('profile receipt deferred kind=%s error=ReceiptChanged', kind)
            except OperationCancelled:
                return ReceiptReport(consumed, tuple(deferred), True)
            except (ValueError, OSError, TypeError, KeyError) as error:
                prefix = (
                    '声骸已保留，匹配配置待重试：'
                    if kind == 'import'
                    else '档案删除配置清理待重试：'
                )
                deferred.append(prefix + str(error))
                _LOG.warning(
                    'profile receipt deferred kind=%s error=%s', kind, type(error).__name__
                )
        return ReceiptReport(consumed, tuple(deferred))

    def _apply_import_receipt(self, data, cancel_event):
        if (
            not isinstance(data, dict)
            or set(data) != {'schema_version', 'profiles', 'settings_json'}
            or type(data['schema_version']) is not int
            or data['schema_version'] != 1
            or not isinstance(data['profiles'], list)
            or not isinstance(data['settings_json'], list)
        ):
            raise ValueError('档案配置回执格式异常。')
        profiles = self.inventory.profiles()
        if not all(isinstance(key, str) and key in profiles for key in data['profiles']):
            raise ValueError('恢复配置的档案已变化。')
        self.apply(tuple(tuple(row) for row in data['settings_json']), cancel_event=cancel_event)

    def _apply_delete_receipt(self, data, cancel_event):
        if (
            not isinstance(data, dict)
            or set(data) != {'schema_version', 'deleted_profile', 'archive'}
            or type(data['schema_version']) is not int
            or data['schema_version'] != 1
        ):
            raise ValueError('档案删除回执格式异常。')
        validate_id(data['deleted_profile'])
        if data['deleted_profile'] not in self.inventory.profiles():
            self.apply(removed=data['deleted_profile'], cancel_event=cancel_event)
        # An imported-again UUID supersedes its old deletion receipt; never remove its new rows.
