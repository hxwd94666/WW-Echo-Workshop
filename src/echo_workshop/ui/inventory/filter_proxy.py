# 将共享卡片筛选交给后台原生作业，Qt 只冻结输入和映射行号。
from PySide6.QtCore import QSortFilterProxyModel, Qt, QTimer, Signal

from echo_workshop.application.inventory_view import InventoryViewJob, ViewMetadata
from echo_workshop.core.filters import InventoryFilters


class InventoryFilterProxy(QSortFilterProxyModel):
    projectionAboutToChange = Signal()
    projectionChanged = Signal()
    stateChanged = Signal()

    def __init__(self, parent=None, *, queries=None):
        super().__init__(parent)
        self.filters = InventoryFilters()
        self._query = ''
        self._queries = queries
        self._job = None
        self._closed = False
        self._pending = False
        self.error = ''
        self._accepted = None
        self._epoch = self.generation = 0
        self._request = None
        self._connections = []
        self._record_reader = self._search_reader = None
        self.setDynamicSortFilter(False)
        self._dispatch = QTimer(self)
        self._dispatch.setSingleShot(True)
        self._dispatch.timeout.connect(self._submit)
        self._poller = QTimer(self)
        self._poller.setInterval(20)
        self._poller.timeout.connect(self._poll)

    @property
    def projection_ready(self):
        return not (self._closed or self._pending or self.error)

    @property
    def request_context(self):
        """Input identity, unchanged by publishing its completed projection."""
        return id(self.sourceModel()), self._epoch

    @property
    def closed(self):
        return self._closed

    @property
    def _identity(self):
        return not self._query and self.filters == InventoryFilters()

    def setSourceModel(self, model):
        for signal, callback in self._connections:
            signal.disconnect(callback)
        self._connections.clear()
        self._record_reader = getattr(model, 'filter_record', None)
        self._search_reader = getattr(model, 'search_text', None)
        super().setSourceModel(model)
        if model is not None:
            for signal in (
                model.modelAboutToBeReset,
                model.rowsAboutToBeInserted,
                model.rowsAboutToBeRemoved,
                model.layoutAboutToBeChanged,
            ):
                signal.connect(self._source_changing)
                self._connections.append((signal, self._source_changing))
            for signal in (
                model.modelReset,
                model.rowsInserted,
                model.rowsRemoved,
                model.dataChanged,
                model.layoutChanged,
            ):
                signal.connect(self._source_changed)
                self._connections.append((signal, self._source_changed))
        self._source_changed()

    def _source_changing(self, *_):
        if not self._identity and not self._closed:
            self._mark_pending()

    def _source_changed(self, *_):
        self._epoch += 1
        self.generation += 1
        if not self._identity:
            self._schedule()

    def _mark_pending(self):
        self._pending, self.error = True, ''
        self.stateChanged.emit()

    def _schedule(self):
        if self._closed:
            return
        self._mark_pending()
        if self._job is not None:
            self._job.cancel()
        self._dispatch.start(0)

    def set_filter_spec(self, text, filters):
        text = str(text).strip()
        if text == self._query and filters == self.filters:
            return False
        self._mark_pending()
        self._query, self.filters = text, filters
        self._epoch += 1
        self.generation += 1
        if self._identity:
            self._dispatch.stop()
            self._poller.stop()
            if self._job is not None:
                self._job.cancel()
            self._publish(None)
        else:
            self._schedule()
        return True

    def setFilterFixedString(self, text):
        self.set_filter_spec(text, self.filters)

    def filterAcceptsRow(self, row, parent):
        if self._accepted is not None:
            return row in self._accepted
        # An unfiltered view is structural identity, not a business predicate.
        model = self.sourceModel()
        return model is not None and (
            callable(self._record_reader)
            or model.index(row, 0, parent).data(int(Qt.UserRole) + 1) is not None
        )

    def _freeze_rows(self):
        model = self.sourceModel()
        if model is None:
            return (), ()
        if model.rowCount() > 20000:
            raise ValueError('视图超过单批规模限额')
        rows, positions = [], []
        claims = getattr(model, 'claims', {})
        for row in range(model.rowCount()):
            index = model.index(row, 0)
            if callable(self._record_reader):
                record, kind = self._record_reader(row)
            else:
                value = index.data(int(Qt.UserRole) + 1)
                if value is None:
                    continue
                record, kind, _ = value
            text = (
                self._search_reader(row)
                if callable(self._search_reader)
                else index.data(self.filterRole()) or ''
            )
            reservation = claims.get(record.item_id, '') or index.data(int(Qt.UserRole) + 6)
            reservation = (
                reservation if isinstance(reservation, str) else '1' if reservation else ''
            )
            rows.append((record, kind, ViewMetadata(text, reservation)))
            positions.append(row)
        return tuple(rows), tuple(positions)

    def _submit(self):
        if self._closed:
            self._notify_closed()
            return
        if self._identity:
            return
        try:
            if self._queries is None:
                raise ValueError('卡片筛选缺少 runtime 查询服务')
            rows, positions = self._freeze_rows()
            if self._job is None:
                self._job = InventoryViewJob(queries=self._queries)
                self.destroyed.connect(lambda _=None, job=self._job: job.close())
            context = (id(self.sourceModel()), self._epoch)
            generation = self._job.request(rows, self.filters, text=self._query, context=context)
            self._request = generation, context, positions
            self._poller.start()
        except Exception as error:
            self._publish(frozenset(), error=str(error))

    def _poll(self):
        if self._closed:
            self._notify_closed()
            return
        if self._request is None:
            return
        generation, context, positions = self._request
        if context != (id(self.sourceModel()), self._epoch):
            return
        status = self._job.status
        if (status.generation, status.context) != (
            generation,
            context,
        ) or status.state == 'running':
            return
        self._poller.stop()
        if status.state != 'ready':
            self._publish(frozenset(), error=status.message or '卡片筛选未完成')
            return
        if any(not 0 <= index < len(positions) for index in status.indices):
            self._publish(frozenset(), error='原生视图返回了越界行号')
            return
        self._publish(frozenset(positions[index] for index in status.indices))

    def _publish(self, accepted, *, error=''):
        self.projectionAboutToChange.emit()
        self._accepted = accepted
        self.invalidateFilter()
        self._pending, self.error = False, error
        self.generation += 1
        self.projectionChanged.emit()
        self.stateChanged.emit()

    def close_job(self):
        # May be called from host unload's executor: no Qt calls or joins.
        self._closed = True
        if self._job is not None:
            self._job.close()

    def _notify_closed(self):
        # Run only from a GUI timer; close_job itself remains executor-safe.
        self._poller.stop()
        self._dispatch.stop()
        self._pending, self.error = False, '卡片筛选已关闭，请重新打开后确认。'
        self.stateChanged.emit()

    def resume(self):
        if not self._closed:
            return
        self._closed, self._job = False, None
        self._epoch += 1
        if self._identity:
            self._publish(None)
        else:
            self._schedule()
