# 绑定待确认的库存快照与用户意图，失效时撤销连接而不扩大候选范围。
from copy import deepcopy


class PendingInventorySelection:
    def __init__(self, panel, *, filtered, selected_ids, job=None):
        self.panel = panel
        self.inventory = panel.proxy.inventory_window
        self.proxy = self.inventory.table.proxy
        self.session = panel._match_inventory_session
        self.requester = panel._match_origin
        self.filtered = filtered
        self.selected_ids = frozenset(selected_ids)
        self.job = job
        self._connections = []
        self._closed = False
        self.context = self._context()

    def _context(self):
        inventory, requester = self.inventory, self.requester
        selections = getattr(requester, 'selections', None)
        state = getattr(requester, 'state', None)
        return (
            inventory.profile_box.currentData(),
            inventory.table.proxy.request_context,
            inventory.inventory_model.revision,
            inventory.search_box.text().strip(),
            inventory.filter_drawer.filters(),
            deepcopy(self.panel._match_origin_context),
            getattr(requester, '_generation', None),
            getattr(requester, '_rule_dirty', False),
            tuple(selections()) if callable(selections) else (),
            deepcopy(
                (
                    getattr(requester, '_cost_layouts', {}),
                    getattr(requester, '_role_requirements', {}),
                    getattr(requester, '_calculation_options', None),
                    getattr(state, 'filters', {}),
                )
            ),
        )

    def start(self):
        for signal, callback in (
            (self.proxy.stateChanged, self._poll),
            (self.inventory.search_box.textChanged, self._invalidated),
            (self.inventory.filter_drawer.applied, self._invalidated),
            (self.inventory.profile_box.currentIndexChanged, self._invalidated),
        ):
            signal.connect(callback)
            self._connections.append((signal, callback))
        self.inventory.compact_status.setText('正在应用筛选，请稍候…')

    def _invalidated(self, *_):
        self.cancel('选择条件已变化，请重新确认。')

    def _disconnect(self):
        self._closed = True
        for signal, callback in self._connections:
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):
                pass  # The owning QObject may already be destroyed.
        self._connections.clear()
        if getattr(self.panel, '_inventory_confirmation', None) is self:
            self.panel._inventory_confirmation = None

    def cancel(self, message=''):
        if self._closed:
            return
        self._disconnect()
        if self.job is not None:
            self.job.finish('cancelled')
        if message:
            self.panel.feedback.setText(message)
            self.panel.feedback.show()

    def _poll(self, *_):
        if self._closed:
            return
        if (
            self.panel._match_inventory_session is not self.session
            or not self.session.active
            or self.panel._match_origin is not self.requester
            or self.inventory._reader.status.state != 'ready'
            or self._context() != self.context
        ):
            self.cancel('选择上下文已变化，请重新确认。')
            return
        if self.proxy.error:
            self.cancel(self.proxy.error)
            return
        if not self.proxy.projection_ready:
            return
        self._disconnect()
        try:
            counts = self.panel._finish_inventory_selection(
                filtered=self.filtered, _confirmation=self
            )
        except Exception as error:
            if self.job is not None:
                self.job.finish('failed', error=error)
            self.panel.feedback.setText(str(error))
            self.panel.feedback.show()
        else:
            if self.job is not None:
                self.job.finish(counters=counts)
