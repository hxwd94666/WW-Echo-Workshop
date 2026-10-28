# 代理下载 WuwaEchoTool 的 base.js 并静态提取权重，不执行远程 JavaScript。
import json


from ww_echo_workshop.application.wuwa_source import extract_wuwa_presets, merge_wuwa_pack
from ww_echo_workshop.core.policy import PolicyError
from ww_echo_workshop.resources.policy_updates import fetch

WUWA_BASE_URL = 'https://raw.githubusercontent.com/GQin404/WuwaEchoTool/main/js/base.js'


class WuwaUpdateService:
    def __init__(self, store):
        self.store = store

    def update(self, url):
        final, content = fetch(url, timeout=30)
        try:
            source = content.decode('utf-8-sig')
        except UnicodeDecodeError as error:
            raise PolicyError('invalid_data', 'Wuwa base.js 不是 UTF-8') from error
        version, groups = extract_wuwa_presets(source)
        pack, _ = self.store.load_with_source()
        if pack is None:
            raise PolicyError('invalid_data', '没有可合并的策略数据包')
        merged = merge_wuwa_pack(pack, groups, version)
        self.store.import_bytes(json.dumps(merged, ensure_ascii=False).encode('utf-8'))
        return final, version, len(groups)
