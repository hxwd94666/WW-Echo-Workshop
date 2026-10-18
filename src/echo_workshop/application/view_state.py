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
    return _review_reason(record, kind, record.validation_issues, record.review == 'confirmed')


def review_checked_reason(record, quality, kind='item'):
    from echo_workshop.application.record_quality import quality_for

    quality = quality_for(record, quality)
    return _review_reason(record, kind, quality.issues, quality.usable)


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
