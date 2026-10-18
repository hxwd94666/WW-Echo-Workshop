# 只投影实时评分数值，诊断与收录反馈留在工坊页面和日志。
def _value(analysis, prefix):
    return next(
        (
            line.split('：', 1)[1]
            for line in analysis.summary.splitlines()
            if line.startswith(prefix + '：')
        ),
        '',
    )


def live_summary(
    analysis, reference=None, comparisons=(), *, reference_scores=None, matching_only=False
):
    if matching_only and not comparisons:
        return ''
    if analysis.current_score is None and not comparisons:
        return ''
    current = _value(analysis, '当前评分')
    potential = _value(analysis, '理论最高')
    lines = []
    paired = reference is not None and reference.current_score is not None
    if not comparisons:
        label = _value(analysis, '评分模版').replace(' (自动匹配)', '')
        lines.append(label if paired else '模版：' + label)
        if paired:
            lines.extend(
                (
                    '当前评分：',
                    f'左 {reference.current_score:.2f} / 右 {analysis.current_score:.2f}',
                    '理论最高：',
                    f'左 {_value(reference, "理论最高")} / 右 {potential}',
                )
            )
        else:
            label = '当前评分' if analysis.observed_cost is not None else '参考评分'
            lines.extend((f'{label} {current}', f'理论最高 {potential}'))
    if comparisons:
        for row in comparisons[:3]:
            name = row.label.replace('-', '·')
            if len(name) > 12:
                name = name[:11] + '…'
            grade = f' · {row.rating.label}' if row.rating is not None else ''
            mismatch = ' [不适配]' if row.state == 'mismatch' else ''
            left = (
                (reference_scores or {}).get((row.character_id, row.modal_id)) if paired else None
            )
            if paired:
                # Never mix the fixed settings template into the selected matching roles.
                if left is None:
                    continue
                lines.extend(
                    (
                        row.label + mismatch,
                        '当前评分：',
                        f'左 {left.current_score:.2f} / 右 {row.current_score:.2f}',
                        '理论最高：',
                        f'左 {left.potential_score:.2f} / 右 {row.potential_score:.2f}',
                    )
                )
            else:
                score_label = '当前评分' if analysis.observed_cost is not None else '参考评分'
                lines.extend(
                    (
                        name + mismatch,
                        f'{score_label} {row.current_score:.2f}{grade}',
                        f'理论最高 {row.potential_score:.2f}',
                    )
                )
    return '\n'.join(lines)
