# 读取工作台评分预设目录；预设来自策略数据包，不维护第二份自定义存储。
from ww_echo_workshop.application.policy_data import PolicyDataStore
from ww_echo_workshop.core.policy_presets import SYSTEMS, iter_presets


class PresetCatalog:
    def __init__(self, data_dir):
        self.store = PolicyDataStore(data_dir)
        self._pack = None
        self.version = ''

    def load(self):
        try:
            self._pack = self.store.load()
            self.version = self.store.version(self._pack) if self._pack is not None else ''
        except Exception:
            self._pack = None
            self.version = ''
        return self._pack

    def presets(self, system):
        if system in ('workshop_xwuid', 'qq_bot'):
            return []
        if self._pack is None:
            self.load()
        if self._pack is None:
            return []
        try:
            return list(iter_presets(self._pack, system))
        except Exception:
            return []

    def systems(self):
        return tuple(key for key in SYSTEMS if key not in ('workshop_xwuid', 'qq_bot'))

    def get(self, preset_id, system):
        return next((item for item in self.presets(system) if item.preset_id == preset_id), None)
