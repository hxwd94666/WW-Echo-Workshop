# 为主备游戏资源提供直连 IPv4 HTTPS，避免不可达 IPv6 消耗更新期限。
import http.client
import socket
import time
from urllib.parse import urlsplit

from echo_workshop import __version__
from echo_workshop.resources.download import read_body, remaining_time
from echo_workshop.resources.game_sources import approve


def get(url, headers, limit, timeout, *, check=lambda: None):
    approve(url)
    deadline = time.monotonic() + timeout
    remaining_time(deadline, check)
    parsed = urlsplit(url)
    connection = http.client.HTTPSConnection(parsed.hostname, timeout=timeout)

    def connect(address, _timeout, *_args):
        # Per-connection address selection only; do not monkeypatch global socket/DNS behavior.
        addresses = socket.getaddrinfo(address[0], address[1], socket.AF_INET, socket.SOCK_STREAM)
        error = None
        for family, kind, proto, _, target in addresses[:2]:
            remaining = remaining_time(deadline, check)
            stream = socket.socket(family, kind, proto)
            try:
                stream.settimeout(remaining)
                stream.connect(target)
                stream.settimeout(remaining_time(deadline, check))
                return stream
            except OSError as exc:
                stream.close()
                error = exc
        raise error or OSError('游戏资源 IPv4 地址解析为空')

    connection._create_connection = connect
    try:
        connection.request(
            'GET', parsed.path, headers={'User-Agent': 'Echo-Workshop/' + __version__, **headers}
        )
        if connection.sock:
            connection.sock.settimeout(remaining_time(deadline, check))
        with connection.getresponse() as response:
            remaining_time(deadline, check)
            if response.status == 304:
                return 304, b'', {}
            if response.status != 200:
                # No redirect following, no proxy, no disabled certificate verification.
                raise OSError('游戏资源 HTTP ' + str(response.status))
            body = read_body(response, limit, check=check, deadline=deadline)
            validators = {
                key: response.headers[source]
                for key, source in (
                    ('If-None-Match', 'ETag'),
                    ('If-Modified-Since', 'Last-Modified'),
                )
                if response.headers.get(source)
            }
            return 200, body, validators
    finally:
        connection.close()
