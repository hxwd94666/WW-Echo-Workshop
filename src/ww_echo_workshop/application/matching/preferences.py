# 读取并按档案保存既有匹配配置，保留其他档案和原格式。
import json


class MatchingPreferences:
    def __init__(self, read, write):
        self._read, self._write = read, write

    def _load(self):
        raw = self._read()
        if not raw:
            return {'schema_version': 1, 'profiles': {}}
        if len(raw) > 1024 * 1024:
            raise ValueError('比较配置过大，请备份并清理该配置')
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError('比较配置损坏；原配置保留') from error
        if (
            not isinstance(data, dict)
            or data.get('schema_version') != 1
            or not isinstance(data.get('profiles'), dict)
        ):
            raise ValueError('比较配置格式不兼容；原配置保留')
        return data

    def for_profile(self, profile_id):
        return self._load()['profiles'].get(profile_id, {})

    def save_profile(self, profile_id, row):
        if not profile_id:
            raise ValueError('请选择本地档案')
        data = self._load()
        data['profiles'][profile_id] = row
        self._write(json.dumps(data, ensure_ascii=False, allow_nan=False))
