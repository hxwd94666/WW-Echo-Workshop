# 缓存不可变配装投影，并在单次页面刷新内共享非阻塞修订探测。
from contextlib import contextmanager

from echo_workshop.core.allocation.models import AllocationRequest
from echo_workshop.core.models import canonical_json

_UNSET = object()


class AllocationProjection:
    def request(self):
        if self._priority_error:
            raise ValueError(self._priority_error)
        if self.incoming.count():
            raise ValueError('请先点击角色旁的齿轮设置装备要求。')
        key = (
            self.profile.currentData(),
            tuple(self.roles),
            self.tuned.isChecked(),
            self._priority_groups,
        )
        cached = getattr(self, '_request_projection', None)
        if cached is None or cached[0] != key:
            from echo_workshop.application.matching.calculation import restrict_priority_groups

            groups = (
                restrict_priority_groups(key[3], tuple(role.key for role in key[1]))
                if key[3]
                else ()
            )
            request = AllocationRequest(key[0], key[1], key[2], groups)
            self._request_projection = (key, request, request.version)
        return self._request_projection[1]

    def request_version(self):
        self.request()
        return self._request_projection[2]

    def result_data(self):
        if self._payload is not None:
            return self._payload['result']
        source = self._result
        if source is None:
            return None
        cached = getattr(self, '_result_projection', None)
        if cached is None or cached[0] is not source:
            self._result_projection = (source, source.to_dict())
        return self._result_projection[1]

    def result_request_version(self, data):
        # A serialized request may be a legacy dict. Its canonical identity
        # also detects in-place changes, without revalidating eight roles per tick.
        key = canonical_json(data['request'])
        cached = getattr(self, '_result_request_projection', None)
        if cached is None or cached[0] != key:
            self._result_request_projection = (
                key,
                AllocationRequest.from_dict(data['request']).version,
            )
        return self._result_request_projection[1]

    def inventory_revision(self):
        revision = getattr(self, '_refresh_revision', _UNSET)
        return self.runtime.inventory.try_revision() if revision is _UNSET else revision

    @contextmanager
    def refresh_projection(self):
        if hasattr(self, '_refresh_revision'):
            yield
            return
        busy = self._future is not None or self._io_future is not None
        self._refresh_revision = (
            None if busy or self.runtime._closed else self.runtime.inventory.try_revision()
        )
        try:
            yield
        finally:
            del self._refresh_revision

    def fresh(self, result=None):
        if self.runtime._closed:
            return False
        data = self.result_data() if result is None else result
        if data is None:
            return False
        if not isinstance(data, dict):
            data = data.to_dict()
        try:
            return (
                data['inventory_revision'] == self.inventory_revision()
                and data['weight_version'] == self.runtime.weights.active.version
                and (
                    self._result is None
                    or data['plan_id'] != self._result.plan_id
                    or self._result_scope_generation == self._scope_generation
                )
                and self.result_request_version(data) == self.request_version()
            )
        except (ValueError, KeyError, TypeError):
            return False
