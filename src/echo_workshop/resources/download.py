# 共用资源请求的跳转限制、条件缓存、大小检查与绝对读取期限。
import time
from urllib.error import HTTPError
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request

from echo_workshop import __version__


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Resource redirects are not accepted')


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
    request = Request(url, headers={'User-Agent': 'Echo Workshop/' + __version__, **headers})
    try:
        response = build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout)
    except HTTPError as error:
        status = error.code
        error.close()
        if status == 304:
            remaining_time(deadline, check)
            return 304, b'', {}
        raise
    with response:
        remaining_time(deadline, check)
        if response.status == 304:
            return 304, b'', {}
        if response.status != 200:
            raise ValueError('Resource HTTP status ' + str(response.status))
        body = read_body(response, limit, check=check, deadline=deadline)
        validators = {
            key: response.headers[source]
            for key, source in (('If-None-Match', 'ETag'), ('If-Modified-Since', 'Last-Modified'))
            if response.headers.get(source)
        }
        return 200, body, validators
