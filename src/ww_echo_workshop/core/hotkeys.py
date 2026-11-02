# 统一快捷键配置的声明式格式、解析与序列化。
from dataclasses import dataclass
import json


ACTIONS = {
    'capture': '收录当前声骸',
    'stop': '停止收录',
}

MODIFIERS = {'Ctrl': 0x11, 'Alt': 0x12, 'Shift': 0x10}

KEY_CODES = {
    **{chr(vk): vk for vk in range(0x41, 0x5B)},
    **{str(number): 0x30 + number for number in range(10)},
    **{f'F{number}': 0x6F + number for number in range(1, 13)},
}


def _strict_json(text):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result

    def constant(value):
        raise ValueError('Non-finite JSON value: ' + value)

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


@dataclass(frozen=True)
class Chord:
    modifiers: tuple[str, ...]
    key: str

    @property
    def text(self):
        return '+'.join((*self.modifiers, self.key))

    @property
    def vk(self):
        return KEY_CODES[self.key]


def parse_chord(text):
    if not isinstance(text, str) or len(text) > 64:
        raise ValueError('快捷键须为简短组合文本')
    text = text.strip().replace(' ', '').replace('＋', '+')
    if not text:
        return None
    parts = text.upper().split('+')
    if not 1 <= len(parts) <= 4 or len(set(parts)) != len(parts) or parts[-1] not in KEY_CODES:
        raise ValueError('请选择字母、数字或 F1–F12，可搭配 Ctrl/Alt/Shift；留空关闭')
    if any(value not in ('CTRL', 'ALT', 'SHIFT') for value in parts[:-1]):
        raise ValueError('仅支持 Ctrl/Alt/Shift 修饰键')
    modifiers = tuple(name for name in MODIFIERS if name.upper() in parts[:-1])
    return Chord(modifiers, parts[-1])


def parse_bindings(raw):
    if not isinstance(raw, str) or len(raw) > 4096:
        raise ValueError('快捷键配置格式/大小无效')
    if not raw:
        return {}
    try:
        data = _strict_json(raw)
        if (
            not isinstance(data, dict)
            or type(data.get('schema_version')) is not int
            or data['schema_version'] != 1
            or not isinstance(data.get('bindings'), dict)
        ):
            raise ValueError('快捷键配置格式不兼容；原配置保留')
        parsed = {
            action: parse_chord(text)
            for action, text in data['bindings'].items()
            if action in ACTIONS
        }
        parsed = {action: chord for action, chord in parsed.items() if chord is not None}
        if len(set(parsed.values())) != len(parsed):
            raise ValueError('不同操作不可使用同一组合键')
        return parsed
    except (KeyError, TypeError) as error:
        raise ValueError('快捷键配置损坏；原配置保留') from error


def encode_bindings(values):
    raw = json.dumps(dict(schema_version=1, bindings=values), ensure_ascii=False)
    parsed = parse_bindings(raw)
    return json.dumps(
        dict(
            schema_version=1,
            bindings={
                action: parsed[action].text if action in parsed else '' for action in ACTIONS
            },
        ),
        ensure_ascii=False,
    )
