# 统一工作台及 HUD 的实时评分设置和既有配置格式。
from enum import Enum
import json

from ww_echo_workshop.application.policy import costs_from_config
from ww_echo_workshop.core.policy_presets import default_scoring


class LiveOption(Enum):
    DISPLAY_SCORE = '启用声骸评分'
    FOLLOW_ROLE = '自动匹配评分模板'
    SHOW_POLICY = '启用强化策略'
    CONFIRMED_OCR = '实时评分OCR提示已确认'
    TARGET = '强化策略目标分'
    POLICY_MODE = '强化策略模式'
    SCORING_SYSTEM = '策略评分体系'
    MAIN_SCORE = '策略主词条分'
    MAX_SCORE = '策略归一满分'
    PRESET_ID = '策略评分预设'
    HUD_AUTO_POSITION = '策略悬浮窗自动定位'
    HUD_FONT_SIZE = '策略悬浮窗字号'
    HUD_MODE = '策略浮层模式'


class LivePreferences:
    def __init__(self, preferences):
        self._preferences = preferences

    def get(self, option: LiveOption, default=None):
        return self._preferences.get(option.value, default)

    def set(self, option: LiveOption, value):
        self._preferences[option.value] = value

    def role_key(self):
        return (
            self._preferences.get('TemplateCharacterID'),
            self._preferences.get('TemplateModalID'),
        )

    def save_role(self, key, name):
        self._preferences['TemplateCharacterID'] = key[0]
        self._preferences['TemplateModalID'] = key[1]
        self._preferences['角色评分模板'] = name

    def costs(self):
        return costs_from_config(self._preferences)

    def save_costs(self, values):
        self._preferences['强化策略资源权重'] = json.dumps(
            [round(float(value), 3) for value in values], separators=(',', ':')
        )

    def clear_preset(self):
        self.set(LiveOption.PRESET_ID, '')
        self._preferences['策略评分预设名称'] = ''

    def apply_preset(self, preset):
        self.set(LiveOption.SCORING_SYSTEM, preset.system)
        self._preferences['策略评分权重'] = json.dumps(
            [float(value) for value in preset.weights], separators=(',', ':')
        )
        self.set(LiveOption.MAIN_SCORE, float(preset.main_score))
        self.set(LiveOption.MAX_SCORE, float(preset.max_score))
        self.set(LiveOption.TARGET, round(float(preset.target), 1))
        self.set(LiveOption.PRESET_ID, preset.preset_id)
        self._preferences['策略评分预设名称'] = f'{preset.character} · {preset.variant}'

    def policy_weights(self, system, *, fixed=False):
        raw = self._preferences.get('策略评分权重', '')
        try:
            values = json.loads(raw) if isinstance(raw, str) and raw.strip() else []
        except (TypeError, ValueError):
            values = []
        if len(values) != 13:
            values = list(default_scoring(system)['weights'])
        if fixed:
            return [int(value) for value in values]
        return [float(value) for value in values]

    def position(self):
        raw = self._preferences.get('策略悬浮窗位置', '')
        try:
            data = json.loads(raw) if raw else {}
            return int(data.get('x', 80)), int(data.get('y', 120))
        except (TypeError, ValueError):
            return 80, 120

    def save_position(self, x, y):
        self._preferences['策略悬浮窗位置'] = json.dumps(
            {'x': x, 'y': y}, ensure_ascii=False, separators=(',', ':')
        )
