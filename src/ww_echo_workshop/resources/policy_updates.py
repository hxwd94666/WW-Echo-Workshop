# 从 HTTPS 直连源或用户代理源下载策略数据包；只接收声明式 JSON。
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from ww_echo_workshop.application.policy_data import MAX_BYTES
from ww_echo_workshop.core.policy import PolicyError


def validate_url(url):
    if not isinstance(url, str) or not url.strip():
        raise PolicyError('invalid_url', '策略数据地址为空')
    parsed = urlsplit(url.strip())
    if parsed.scheme != 'https' or not parsed.hostname:
        raise PolicyError('invalid_url', '策略数据只接受 HTTPS 地址')
    if parsed.username or parsed.password or parsed.fragment:
        raise PolicyError('invalid_url', '策略数据地址不能包含凭据或片段')
    return url.strip()


class _RedirectLimit(HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, timeout=30):
    target = validate_url(url)
    opener = build_opener(ProxyHandler({}), HTTPSHandler(), _RedirectLimit())
    request = Request(
        target,
        headers={'User-Agent': 'WW-Echo-Workshop/1', 'Accept': 'application/json'},
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            final = validate_url(response.geturl())
            length = response.headers.get('Content-Length')
            if length is not None and int(length) > MAX_BYTES:
                raise PolicyError('invalid_data', '策略数据包超过 8 MiB')
            content = response.read(MAX_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        raise PolicyError('download_failed', f'策略数据下载失败：{error}') from error
    if len(content) > MAX_BYTES:
        raise PolicyError('invalid_data', '策略数据包超过 8 MiB')
    return final, content


class PolicyDataUpdater:
    def __init__(self, store=None, *, timeout=30):
        self.store = store
        self.timeout = timeout

    def update(self, url):
        final, content = fetch(url, timeout=self.timeout)
        store = self.store
        if store is None:
            raise PolicyError('invalid_data', '策略数据存储未配置')
        pack = store.import_bytes(content)
        return final, pack
