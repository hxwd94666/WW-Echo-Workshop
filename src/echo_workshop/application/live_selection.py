# 解析实时评分的明确角色与模版选择。
"""Resolve current single-item scoring against the same immutable provider."""

from dataclasses import dataclass
import re

from echo_workshop.core.text import simplify_echo_text


@dataclass(frozen=True)
class LiveSelection:
    ids: tuple[str, str]
    label: str
    template: object
    version: str
    reason: str


def resolve_live_selection(
    snapshot, boxes, *, fixed, auto=False, remembered=None, page_kind='single'
):
    key = tuple(fixed)
    reason = '固定角色 / 模态'
    if auto:
        compact = re.sub(
            r'\s+', '', ''.join(simplify_echo_text(getattr(box, 'name', box)) for box in boxes)
        )
        observed = snapshot.characters.in_display_text(compact)
        if observed.character_ids:
            if observed.state != 'equipped':
                raise ValueError('装配角色对应多个 ID，请固定选择角色 / 模态')
            char_id = observed.character_id
            # A single item does not establish sonata_5/build modal context.
            key = (char_id, key[1] if key[0] == char_id else 'default')
            reason = '自动识别角色；模态为固定选择或通用'
        elif page_kind == 'tuning' and remembered:
            key = tuple(remembered)
            reason = '沿用已识别的角色 / 固定模态'
        else:
            reason = '未识别装配角色，使用固定选择'
    if len(key) != 2 or not all(isinstance(value, str) and value for value in key):
        raise ValueError('请选择有效角色 / 固定模态')
    data = snapshot.scoring_template(*key)
    if key[0] != 'default' and not snapshot.character_elements.get(key[0]):
        reason += '；角色属性未获取，属性适配待确认，请检查权重与属性更新'
    return LiveSelection(key, data['name'], data, snapshot.version, reason)
