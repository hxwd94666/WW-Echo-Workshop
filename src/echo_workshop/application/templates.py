# 提供宿主控件的内置模版选项与搜索。
from collections import Counter

from echo_workshop.resources.snapshot import TEMPLATES


def _build_template_options():
    rows = [
        (str(char_id), variant, template)
        for char_id, variants in TEMPLATES.items()
        for variant, template in variants.items()
    ]
    duplicate_names = Counter(template['name'] for _, _, template in rows)
    options = {}
    for char_id, variant, template in rows:
        label = template['name']
        if duplicate_names[label] > 1:
            label = f'{label}（{char_id}）'
        options[label] = (char_id, variant, template)
    return options


TEMPLATE_OPTIONS = _build_template_options()
DEFAULT_TEMPLATE = TEMPLATES['default']['default']['name']


def template_names():
    """Return character/modal templates as unique display labels."""
    return list(TEMPLATE_OPTIONS)


def matching_template_names(text):
    """Literal and offline pinyin matching used by the searchable selector."""
    from echo_workshop.resources.search import search_matches

    return [name for name in template_names() if search_matches(name, text)]
