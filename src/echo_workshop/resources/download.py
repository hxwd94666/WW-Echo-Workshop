# 共用资源请求的跳转限制、条件缓存、大小检查与绝对读取期限。
import http.client
import threading
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler

from echo_workshop import __version__


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Resource redirects are not accepted')


_CONNECTIONS = threading.local()


def _pooled_request(url, headers, timeout):
    """One per-thread HTTPS connection, recreated only after a broken exchange."""
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname:
        raise ValueError('Resource URL must be HTTPS')
    key = (parsed.hostname, parsed.port or 443)
    pool = getattr(_CONNECTIONS, 'items', None)
    if pool is None:
        pool = _CONNECTIONS.items = {}
    connection = pool.get(key)
    if connection is None:
        connection = http.client.HTTPSConnection(parsed.hostname, key[1], timeout=timeout)
        pool[key] = connection
    path = parsed.path or '/'
    if parsed.query:
        path += '?' + parsed.query
    try:
        connection.request(
            'GET', path, headers={'User-Agent': 'Echo Workshop/' + __version__, **headers}
        )
        response = connection.getresponse()
    except (OSError, http.client.HTTPException):
        pool.pop(key, None)
        try:
            connection.close()
        except OSError:
            pass
        connection = http.client.HTTPSConnection(parsed.hostname, key[1], timeout=timeout)
        pool[key] = connection
        connection.request(
            'GET', path, headers={'User-Agent': 'Echo Workshop/' + __version__, **headers}
        )
        response = connection.getresponse()
    return connection, response, key


def remaining_time(deadline, check=lambda: None):
    check()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Resource request deadline exceeded')
    return remaining


def read_body(response, limit, *, check=lambda: None, deadline=None, clock=time.monotonic):
    if type(limit) is not int or limit < 1:
        raise ValueError('Positive resource size limit required')
    declared = response.headers.get('Content-Length')
    if declared and (not declared.isdigit() or int(declared) > limit):
        raise ValueError('Resource exceeds size limit')
    reader = getattr(response, 'read1', response.read)
    body = bytearray()
    while True:
        check()
        if deadline is not None:
            remaining = deadline - clock()
            if remaining <= 0:
                raise TimeoutError('Resource request deadline exceeded')
            socket = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
            if socket is not None:
                socket.settimeout(remaining)
        part = reader(min(65536, limit + 1 - len(body)))
        if deadline is not None and clock() > deadline:
            raise TimeoutError('Resource request deadline exceeded')
        check()
        if not part:
            return bytes(body)
        body.extend(part)
        if len(body) > limit:
            raise ValueError('Resource exceeds size limit')


def conditional_get(url, headers, limit, timeout, *, approve, check=lambda: None):
    approve(url)
    check()
    if timeout <= 0:
        raise TimeoutError('Resource request deadline exceeded')
    deadline = time.monotonic() + timeout
    connection, response, key = _pooled_request(url, headers, timeout)
    try:
        remaining_time(deadline, check)
        if response.status == 304:
            response.close()
            return 304, b'', {}
        if response.status != 200:
            response.close()
            raise ValueError('Resource HTTP status ' + str(response.status))
        body = read_body(response, limit, check=check, deadline=deadline)
        validators = {
            item: response.headers[source]
            for item, source in (
                ('If-None-Match', 'ETag'),
                ('If-Modified-Since', 'Last-Modified'),
            )
            if response.headers.get(source)
        }
        response.close()
        if response.headers.get('Connection', '').lower() == 'close':
            pool = getattr(_CONNECTIONS, 'items', {})
            if pool.pop(key, None) is connection:
                connection.close()
        return 200, body, validators
    except Exception:
        pool = getattr(_CONNECTIONS, 'items', {})
        if pool.pop(key, None) is connection:
            connection.close()
        raise
