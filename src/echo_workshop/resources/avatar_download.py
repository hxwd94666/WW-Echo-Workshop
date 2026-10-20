# 在总期限内按验证过的镜像优先级请求头像，区分暂缓与不可信资料。
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import math
import re
import struct
import time

from echo_workshop.core.cancellation import checkpoint, OperationCancelled
from echo_workshop.resources.download import conditional_get
from echo_workshop.storage.evidence import validate_png

AVATAR_BASES = tuple(
    'https://' + host + '/XutheringWavesUID/resource/avatar/'
    for host in ('ww3.loping151.cn', 'ww1.loping151.top', 'ww2.loping151.top')
)
MAX_AVATAR_BYTES = 512 * 1024
AVATAR_TOTAL_SECONDS = 6.0
AVATAR_MIRROR_SECONDS = 3.0


class AvatarDeferred(OSError):
    """Optional image is temporarily unavailable; old validated bytes remain."""

    def __init__(self, reason, errors=()):
        self.reason = (
            reason
            if reason in ('deadline', 'network', 'http-unavailable', 'busy', 'not-found')
            else 'unavailable'
        )
        self.error_types = tuple(
            dict.fromkeys(
                value
                for value in errors
                if isinstance(value, str) and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,63}', value)
            )
        )[:3]
        super().__init__('头像检查暂缓；旧图保留（' + reason + '）')


def checked_avatar(body):
    if (
        not isinstance(body, bytes)
        or not 45 <= len(body) <= MAX_AVATAR_BYTES
        or body[:8] != b'\x89PNG\r\n\x1a\n'
    ):
        raise ValueError('Invalid avatar PNG')
    width, height = struct.unpack('>II', body[16:24])
    if not 1 <= width <= 1024 or not 1 <= height <= 1024:
        raise ValueError('Avatar dimensions exceed limits')
    validate_png(body)
    return body


def avatar_validators(ident, values):
    if not isinstance(ident, str) or not re.fullmatch(r'[0-9]{1,8}', ident):
        raise ValueError('Invalid avatar identity')
    if values is None or values == {}:
        return {}
    if (
        not isinstance(values, dict)
        or set(values) - {'source', 'sha256', 'If-None-Match', 'If-Modified-Since'}
        or values.get('source')
        not in {base + 'role_head_' + ident + '.png' for base in AVATAR_BASES}
        or not isinstance(values.get('sha256'), str)
        or not re.fullmatch(r'[a-f0-9]{64}', values.get('sha256', ''))
        or any(
            not isinstance(value, str) or len(value) > 1024 or '\r' in value or '\n' in value
            for value in values.values()
        )
    ):
        raise ValueError('Invalid avatar conditional metadata')
    return dict(values)


def fetch_avatar(ident, stop):
    return fetch_avatar_resource(ident, stop)[0]


def fetch_avatar_resource(
    ident,
    stop,
    *,
    previous=None,
    validators=None,
    deadline=None,
    transport=None,
    on_request=None,
    clock=time.monotonic,
):
    validators = avatar_validators(ident, validators)
    if previous is not None:
        checked_avatar(previous)
    now = clock()
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise ValueError('Invalid avatar deadline')
    deadline = (
        min(now + AVATAR_TOTAL_SECONDS, deadline)
        if deadline is not None
        else now + AVATAR_TOTAL_SECONDS
    )
    previous_hash = hashlib.sha256(previous).hexdigest() if previous is not None else ''
    urls = [base + 'role_head_' + ident + '.png' for base in AVATAR_BASES]
    preferred = validators.get('source')
    if preferred in urls:
        urls.remove(preferred)
        urls.insert(0, preferred)
    errors = []

    def fetch(url):
        remaining = min(AVATAR_MIRROR_SECONDS, deadline - clock())
        if remaining <= 0:
            return 'deferred', 'deadline', None
        headers = {}
        if validators.get('source') == url and validators.get('sha256') == previous_hash:
            headers = {
                key: value
                for key, value in validators.items()
                if key in ('If-None-Match', 'If-Modified-Since')
            }
        try:
            if on_request is not None:
                on_request()
            status, body, reply = (transport or _avatar_get)(url, headers, remaining, stop)
            checkpoint(stop)
            if clock() >= deadline:
                return 'deferred', 'deadline', None
            if status == 304:
                if previous is None or not headers:
                    return 'fatal', 'Avatar 304 has no matching validated cache', None
                return 'not-modified', url, None
            if status != 200:
                if (
                    status == 404
                    or status == 408
                    or status == 429
                    or (type(status) is int and status >= 500)
                ):
                    return 'deferred', 'http-unavailable', None
                return 'fatal', 'Avatar HTTP status', None
            checked_avatar(body)
            if not isinstance(reply, dict):
                return 'fatal', 'Invalid avatar response metadata', None
            meta = {
                key: value
                for key, value in reply.items()
                if key in ('If-None-Match', 'If-Modified-Since')
            }
            meta.update(source=url, sha256=hashlib.sha256(body).hexdigest())
            avatar_validators(ident, meta)
            return 'ok', url, (body, meta, False)
        except (OperationCancelled, TimeoutError):
            raise
        except ValueError as error:
            return 'fatal', str(error), None
        except OSError as error:
            return 'deferred', type(error).__name__, None

    fatal = None
    with ThreadPoolExecutor(max_workers=min(3, len(urls))) as pool:
        futures = [pool.submit(fetch, url) for url in urls]
        for future in as_completed(futures):
            checkpoint(stop)
            kind, value, payload = future.result()
            if kind == 'not-modified':
                return previous, validators, True
            if kind == 'ok':
                return payload
            if kind == 'fatal' and fatal is None:
                fatal = value
            elif kind == 'deferred':
                errors.append(value)
    if fatal is not None:
        raise ValueError(fatal)
    raise AvatarDeferred('network', errors)


def _avatar_get(url, headers, timeout, stop):
    def approve(value):
        if not any(
            value.startswith(base)
            and re.fullmatch(r'role_head_[0-9]{1,8}\.png', value[len(base) :])
            for base in AVATAR_BASES
        ):
            raise ValueError('Unapproved avatar URL')

    return conditional_get(
        url, headers, MAX_AVATAR_BYTES, timeout, approve=approve, check=lambda: checkpoint(stop)
    )
