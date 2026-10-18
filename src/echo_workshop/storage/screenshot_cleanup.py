# 在库存事务与截图写入互斥下清理截图，不修改库存、历史或冻结方案。
import json
import logging

from echo_workshop.application.operations import checkpoint
from echo_workshop.storage.evidence import _REF


class ScreenshotCleanupMixin:
    def clean_screenshots(self, evidence, *, remove_all=False, cancel_event=None):
        if evidence.data_dir != self.data_dir:
            raise ValueError('截图库与声骸库不属于同一工作区')
        removed = failed = size = 0
        # Capture/import take the same evidence lock before publishing images and DB rows.
        # BEGIN IMMEDIATE also excludes other DB writers while references are inspected.
        with evidence.operation(wait=False), self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            references = set()
            if not remove_all:
                for table in ('items', 'pending_captures'):
                    for (payload,) in self._db.execute(f'SELECT payload FROM {table}'):
                        checkpoint(cancel_event)
                        row = json.loads(payload)
                        if not isinstance(row, dict) or not isinstance(
                            row.get('screenshot_ref', ''), str
                        ):
                            raise ValueError('库存截图引用格式异常，已停止自动清理')
                        if row.get('screenshot_ref'):
                            references.add(row['screenshot_ref'])
            for path in evidence.folder.iterdir():
                checkpoint(cancel_event)
                reference = 'screenshots/' + path.name
                if not _REF.fullmatch(reference) or reference in references:
                    continue
                # Never follow links/junctions or traverse directories during deletion.
                if path.is_symlink() or path.resolve().parent != evidence.folder.resolve():
                    continue
                try:
                    if not path.is_file():
                        continue
                    length = path.stat().st_size
                    path.unlink()
                    removed += 1
                    size += length
                except FileNotFoundError:
                    pass
                except OSError:
                    failed += 1
            revision = self.revision
        if removed or failed:
            logging.getLogger(__name__).info(
                'screenshot cleanup all=%s removed=%s failed=%s bytes=%s',
                remove_all,
                removed,
                failed,
                size,
            )
        return dict(removed=removed, failed=failed, bytes=size, revision=revision)
