# 在配装结果页复用已保存配装的单槽替换候选与预览。
from echo_workshop.ui.allocation.replacement import ReplacementDialog


class ResultReplacementMixin:
    def _result_replacement_payload(self):
        if self._payload is not None:
            return dict(self._payload)
        result = self.result_data()
        snapshot = self._snapshot
        if result is None or snapshot is None:
            raise ValueError('当前配装结果尚未完成，无法替换优化')
        return {
            'schema_version': 1,
            'plan_id': result['plan_id'],
            'request': result['request'],
            'weight_snapshot': snapshot.to_dict(),
            'result': result,
            'records': [record.to_dict() for record in self._records],
            'completed': [],
            'progress_revision': 0,
        }

    def replace_result(self, key, identity):
        result = self.result_data()
        if result is None or not identity:
            raise ValueError('请先点击一件声骸，再使用替换优化')
        row = next(
            (
                item
                for item in result.get('roles', ())
                if (item.get('character_id'), item.get('modal_id')) == key
            ),
            None,
        )
        if row is None or identity not in row.get('item_ids', ()):
            raise ValueError('请选择当前配装中的声骸')
        slot_index = row['item_ids'].index(identity)
        payload = self._result_replacement_payload()
        self._start_plan_job(
            'replacement-candidates',
            dict(
                payload=payload,
                character_id=key[0],
                slot_index=slot_index,
                result_replacement=True,
            ),
        )
        self.status.setText('正在计算单槽替换候选…')

    def publish_result_candidates(self, options):
        dialog = ReplacementDialog(
            options,
            self.window(),
            avatar_cache=self.runtime.avatars,
            queries=self.runtime.inventory_queries,
        )
        self._result_replacement_dialog = dialog
        dialog.preview_requested.connect(
            lambda item_id: self._start_plan_job(
                'preview-replacement',
                dict(options=options, item_id=item_id, result_replacement=True),
            )
        )
        dialog.rejected.connect(self.cancel_job)
        dialog.finished.connect(lambda _code: setattr(self, '_result_replacement_dialog', None))
        dialog.open()

    def publish_result_preview(self, preview):
        self._result = preview.result
        self._records = tuple(preview.records)
        self._snapshot = preview.snapshot
        self._payload = None
        if self._result_replacement_dialog is not None:
            self._result_replacement_dialog.accept()
            self._result_replacement_dialog = None
        self.render_result(self._result.to_dict(), [])
        self.status.setText('替换预览已更新；未写入当前配装')
