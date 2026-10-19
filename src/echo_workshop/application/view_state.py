# 定义页面选择和异步结果的状态合同。
"""User-facing summaries only; never alter record validation or scoring."""


def capture_hint(status, ready):
    if getattr(status, 'mode', '') == 'manual':
        return (
            status.message,
            '停止收录'
            if status.state in ('single', 'armed')
            else '去核对'
            if status.added or status.pending
            else '重新收录',
        )
    if status.state == 'paused':
        return '已暂停，已有记录保留。', '继续收录'
    if status.state == 'active':
        if not ready:
            return '游戏画面中断或宿主已暂停；请在 OKWW 恢复捕获，已有记录保留。', '查看连接说明'
        if status.added or status.pending:
            return (
                f'本次新增 {status.added} 件、疑似重复/升级 {status.pending} 件，保存后仍需核对。继续查看下一件即可。',
                '去核对',
            )
        return '请在背包或角色声骸展示页打开一件，停留片刻，等待稳定识别。', '暂停收录'
    if not ready:
        return '尚未连接游戏画面。先在 OKWW 选择鸣潮窗口并启动捕获。', '查看连接说明'
    if status.added or status.pending:
        return (
            f'本次收录已结束：新增 {status.added} 件、疑似重复/升级 {status.pending} 件。已有记录保留。',
            '去核对',
        )
    return '打开一件声骸详情后开始；仅看实时评分不会保存库存。', '开始收录'


def review_reason(record, kind='item'):
    """Fallback for callers without native facts; never re-evaluate quality rules."""
    return _review_reason(record, kind, tuple(record.issues), record.review == 'confirmed')


def review_checked_reason(record, quality, kind='item'):
    from echo_workshop.application.record_quality import quality_for

    quality = quality_for(record, quality)
    return _review_reason(record, kind, quality.issues, quality.usable)


_FIELD_LABELS = {
    'name': '名称',
    'header_name': '标题名称',
    'sonata': '套装',
    'sonata_heading': '套装标题',
    'level': '等级',
    'header_level': '标题等级',
    'owner': '装备者',
    'cost': 'COST',
}

_ISSUE_DETAILS = {
    'missing:name': '声骸名称缺失',
    'missing:sonata': '套装缺失',
    'missing:level': '等级缺失',
    'main:expected_two_slots': '主词条不是两条',
    'sub:duplicate_or_excess': '副词条重复或超过五条',
    'sub:exceeds_level_capacity': '副词条数量超过等级允许',
    'ocr:header_name_needs_review': '标题中的声骸名称需要确认',
    'ocr:conflicting_header_level': '等级有多个候选值',
    'ocr:invalid_header_level': '等级不在有效范围',
    'ocr:sonata_needs_review': '套装名称需要确认',
    'ocr:conflicting_sonata_heading': '套装标题有冲突',
    'ocr:sonata_icon_conflict': '套装文字与图标不一致',
    'ocr:cost_needs_review': 'COST 无法从截图确定',
    'ocr:cost_conflict': 'COST 与主词条推断不一致',
    'ocr:ambiguous_value': '数字有歧义，可能是小数点或千位分隔',
    'ocr:missing_tuned_rows': '已显示全部调谐，但副词条没有读全',
    'ocr:layout_needs_review': '页面布局未确认',
    'ocr:conflicting_owner': '装备者信息有冲突',
    'ocr:metadata_geometry_changed': '二次核对发现词条布局变化',
}


def review_issue_details(issues):
    """Translate native/OCR issue codes into short user-facing reasons."""
    details = []
    for issue in issues:
        text = _ISSUE_DETAILS.get(issue)
        if text is None and issue.startswith('main:invalid:'):
            text = '主词条数值超出允许范围'
        elif text is None and issue.startswith('sub:invalid_roll:'):
            name = issue.split(':', 2)[2]
            text = f'副词条「{name}」不是游戏合法档位'
        elif text is None and issue.startswith('ocr:metadata_disagreement:'):
            field = issue.split(':', 2)[2]
            text = f'{_FIELD_LABELS.get(field, field)}两次读取不一致'
        elif text is None and issue.startswith('ocr:metadata_missing:'):
            field = issue.split(':', 2)[2]
            text = f'{_FIELD_LABELS.get(field, field)}第二次读取缺失，已保留第一次结果'
        elif text is None and issue.startswith('ocr:metadata_recovered:'):
            field = issue.split(':', 2)[2]
            text = f'{_FIELD_LABELS.get(field, field)}第二次读取补全'
        elif text is None and issue.startswith('ocr:conflicting_'):
            field = issue.split(':', 1)[1].removeprefix('conflicting_')
            text = f'{_FIELD_LABELS.get(field, field)}有冲突或需要确认'
        elif text is None and issue.startswith('ocr:metadata_'):
            text = '二次核对发现额外识别差异'
        elif text is None:
            text = '存在未分类的识别提示'
        if text not in details:
            details.append(text)
    return tuple(details)


def _review_reason(record, kind, issues, usable):
    if kind == 'duplicate':
        return '疑似同一件，请区分是否为另一件'
    if kind == 'upgrade':
        return '疑似升级，请对照旧新记录'
    if any(
        issue.startswith('missing:')
        or issue in ('ocr:cost_needs_review', 'ocr:missing_header_name', 'ocr:missing_header_level')
        for issue in issues
    ):
        return '信息缺失，请对照游戏补填'
    if issues:
        return '识别信息或数值待核对'
    return '已确认' if usable else '等待人工核对'
