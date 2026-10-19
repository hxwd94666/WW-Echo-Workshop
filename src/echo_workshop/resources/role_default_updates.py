# 从国内资源镜像更新角色默认套装，复用统一资料作业而不另启线程。
from dataclasses import dataclass, replace
import threading
import time
import http.client

from echo_workshop.core.cancellation import checkpoint, OperationCancelled
from echo_workshop.resources.download import conditional_get, remaining_time
from echo_workshop.resources.role_defaults import (
    SOURCES,
    MAX_BYTES,
    digest,
    parse_upstream,
    snapshot,
    checked_validators,
)


def approved_url(url):
    if url not in SOURCES:
        raise ValueError('未批准的角色默认套装来源')
    return url


class RoleDefaultsHttpClient:
    def __init__(self, *, transport=None, budget=24):
        self.transport, self.budget = transport, budget
        self.requests = self.cache_hits = 0

    def fetch(self, provider, *, cancel_event=None):
        self.requests = self.cache_hits = 0
        deadline, errors = time.monotonic() + self.budget, []
        active, validators = provider.active, dict(provider.validators)
        for source in sorted(SOURCES, key=lambda url: url != active.source):
            try:
                timeout = min(8, remaining_time(deadline, lambda: checkpoint(cancel_event)))
                headers = validators if source == active.source else {}
                self.requests += 1
                if self.transport is None:
                    status, body, new_headers = conditional_get(
                        source,
                        headers,
                        MAX_BYTES,
                        timeout,
                        approve=approved_url,
                        check=lambda: checkpoint(cancel_event),
                    )
                else:
                    status, body, new_headers = self.transport(source, headers, MAX_BYTES, timeout)
                remaining_time(deadline, lambda: checkpoint(cancel_event))
                if status == 304:
                    if source != active.source or not headers:
                        raise ValueError('缺少同源默认套装缓存')
                    self.cache_hits += 1
                    return active, validators
                if status != 200:
                    raise ValueError('角色默认套装响应状态错误')
                entries = parse_upstream(body)
                if set(active.entries) - set(entries):
                    raise ValueError('来源缺少已有角色')
                value = snapshot(entries, source, digest(body))
                return value, checked_validators(new_headers)
            except OperationCancelled:
                raise
            except (OSError, ValueError, TypeError, KeyError, http.client.HTTPException) as exc:
                errors.append(source.split('/')[2] + ': ' + str(exc)[:160])
        raise ValueError('默认套装来源未完成校验，已有资料保留：' + '; '.join(errors))


@dataclass(frozen=True)
class RoleDefaultsStatus:
    state: str = 'idle'
    message: str = '角色默认套装离线可用'
    last_checked: float = 0
    next_check: float = 0
    requests: int = 0
    cache_hits: int = 0


class _Token:
    def __init__(self, *events):
        self.events = tuple(event for event in events if event is not None)

    def is_set(self):
        return any(event.is_set() for event in self.events)


class RoleDefaultsUpdateService:
    def __init__(self, provider, *, client=None, stop_event=None, clock=time.time):
        self.provider, self.client = provider, client or RoleDefaultsHttpClient()
        self.stop_event, self.clock = stop_event or threading.Event(), clock
        self._lock = threading.RLock()
        self.coordinator = None
        self.last_failure = None
        self._status = RoleDefaultsStatus(
            message=provider.warning or '角色默认套装离线可用',
            next_check=max(clock() + 30, provider.last_check + 86400),
        )

    @property
    def status(self):
        with self._lock:
            return self._status

    def check_once(self, *, cancel_event=None):
        token = _Token(self.stop_event, cancel_event)
        with self._lock:
            if token.is_set() or self._status.state == 'checking':
                return False
            self._status = replace(self._status, state='checking', message='正在检查角色默认套装')
        self.last_failure = None
        try:
            value, validators = self.client.fetch(self.provider, cancel_event=token)
            changed = self.provider.activate(
                value, validators, checked_at=self.clock(), cancel_event=token
            )
            state, message, delay = (
                ('updated', '角色默认套装已更新；手动配置保持不变', 86400)
                if changed
                else ('current', '角色默认套装资料已是最新', 86400)
            )
        except OperationCancelled:
            state, message, delay = 'stopped', '默认套装检查已取消，已有资料保留', 300
        except Exception as exc:
            self.last_failure = exc
            state, message, delay = (
                'failed',
                '默认套装检查未完成，已有资料保留：' + str(exc)[:400],
                1800,
            )
        now = self.clock()
        with self._lock:
            self._status = RoleDefaultsStatus(
                state,
                message,
                now,
                now + delay,
                getattr(self.client, 'requests', 0),
                getattr(self.client, 'cache_hits', 0),
            )
        return state == 'updated'
