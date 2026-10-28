# 保存工作台测试声骸、候选与评分设置；只写插件数据目录，不碰库存和角色配置。
import json
import math
import os
import tempfile
from pathlib import Path

MAX_BYTES = 64 * 1024
ROWS = 5
SCORINGS = ('workshop_xwuid', 'linear', 'fixed', 'wuwa_echo_tool', 'mc_boost_assistant')
MODES = ('auto', 'upgrade', 'reroll', 'joint')


class PolicyUiStateStore:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / 'policy' / 'ui_state.json'

    def load(self):
        if not self.path.is_file():
            return None
        try:
            if self.path.stat().st_size > MAX_BYTES:
                return None
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return self._validate(data)

    def save(self, state):
        data = self._validate(state)
        if data is None:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode='w',
            encoding='utf-8',
            dir=self.path.parent,
            prefix='.ui-state-',
            suffix='.tmp',
            delete=False,
        )
        temporary = Path(handle.name)
        try:
            with handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return True

    def _validate(self, state):
        if not isinstance(state, dict):
            return None
        scoring = state.get('scoring', 'workshop_xwuid')
        mode = state.get('mode', 'auto')
        cost = state.get('cost', 4)
        if scoring not in SCORINGS or mode not in MODES or cost not in (1, 3, 4):
            return None
        rows = self._rows(state.get('rows'))
        candidate = self._rows(state.get('candidate'))
        if rows is None or candidate is None:
            return None
        numbers = {}
        for key, default in (
            ('target', 0.0),
            ('main_score', 0.0),
            ('max_score', 100.0),
        ):
            value = state.get(key, default)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                return None
            numbers[key] = float(value)
        if numbers['max_score'] <= 0:
            return None
        weights = state.get('weights', '')
        if not isinstance(weights, str) or len(weights) > 4096:
            return None
        return {
            'scoring': scoring,
            'mode': mode,
            'cost': cost,
            'rows': rows,
            'candidate': candidate,
            'target': numbers['target'],
            'main_score': numbers['main_score'],
            'max_score': numbers['max_score'],
            'weights': weights,
        }

    @staticmethod
    def _rows(value):
        if value is None:
            return []
        if not isinstance(value, list) or len(value) != ROWS:
            return None
        rows = []
        for item in value:
            if not isinstance(item, dict):
                return None
            name = item.get('name')
            number = item.get('value', 0.0)
            if name is not None and (not isinstance(name, str) or len(name) > 20):
                return None
            if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
                return None
            rows.append({'name': name, 'value': float(number)})
        return rows
