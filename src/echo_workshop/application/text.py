# 在识别与输入边界执行完整繁简转换。
from functools import lru_cache
from echo_workshop.core.text import simplify_echo_text as simplify_labels


@lru_cache(maxsize=1)
def _converter():
    try:
        from opencc import OpenCC
    except ImportError:
        return None
    return OpenCC('t2s')


def simplify_echo_text(text):
    value = str(text).replace('撃', '擊')
    converter = _converter()
    return converter.convert(value) if converter is not None else simplify_labels(value)
