# 生成冻结匹配输入的阻断原因和对应操作。
from dataclasses import dataclass

from echo_workshop.application.pins import pin_error_text


@dataclass(frozen=True)
class MatchFeedback:
    text: str
    action: str = ''
    role_key: tuple | None = None

    @property
    def action_text(self):
        return {
            'add-role': '添加角色',
            'inventory': '打开声骸库',
            'review': '核对声骸',
            'selection': '选择声骸',
            'filters': '调整评分范围',
            'role': '编辑角色要求',
        }.get(self.action, '')


class MatchRequirementsError(ValueError):
    def __init__(self, key, label, error):
        self.feedback = MatchFeedback(f'{label}：{error}', 'role', tuple(key))
        super().__init__(self.feedback.text)


def validate_match_requirements(requirements, records, snapshot, stop=None, *, pin_checks):
    """Keep strict whole-request validation, adding the exact role to failures."""
    errors = pin_checks.check(requirements, records, cancel_event=stop)
    if errors:
        key, slot, code = errors[0]
        label = snapshot.template(*key)['name']
        raise MatchRequirementsError(key, label, pin_error_text(slot, code))


_FEEDBACK_MESSAGES = {
    'inventory_empty': ('声骸库为空，请先收录或导入声骸。', 'inventory'),
    'review_required': ('暂无已核对的可评分声骸，请先核对。', 'review'),
    'selection_empty': ('尚未选择声骸，请明确选择本次候选。', 'selection'),
    'selection_filtered_empty': ('当前筛选没有可评分声骸，请重新选择候选。', 'selection'),
    'filters_empty': ('评分范围排除了全部声骸，请检查等级、保护等选项。', 'filters'),
    'selection_no_match': ('当前选择没有可评分声骸，请重新选择。', 'selection'),
}


def feedback_from_code(code):
    """Render a native conclusion; do not inspect records or infer priorities."""
    if code == '':
        return None
    try:
        text, action = _FEEDBACK_MESSAGES[code]
    except (KeyError, TypeError) as error:
        from echo_workshop.application.compute_protocol import ComputeFailure

        raise ComputeFailure('protocol', '未知匹配反馈原因码') from error
    return MatchFeedback(text, action)
