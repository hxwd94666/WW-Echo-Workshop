# 只投影实时评分数值，诊断与收录反馈留在工坊页面和日志。
POLICY_ACTION_COLOR = (125, 211, 252)
POLICY_DETAIL_COLOR = POLICY_ACTION_COLOR


def _value(analysis, prefix):
    return next(
        (
            line.split('：', 1)[1]
            for line in analysis.summary.splitlines()
            if line.startswith(prefix + '：')
        ),
        '',
    )


def live_summary(analysis, reference=None, policy_text='', left_policy_text=''):
    if analysis.current_score is None:
        return '', ()
    current = f'{analysis.current_score:.2f}' if analysis.current_score is not None else ''
    potential = f'{analysis.potential_score:.2f}' if analysis.potential_score is not None else ''
    lines = []
    colours = []

    def add(text, colour=()):
        lines.append(text)
        colours.append(colour)

    paired = reference is not None and reference.current_score is not None
    label = (analysis.selected_template or _value(analysis, '评分模版')).replace(' (自动匹配)', '')
    add(label if paired else '模版：' + label, ((255, 110, 220),))
    if paired:
        left_score = (
            f'{reference.current_score:.2f}' if reference.current_score is not None else '--'
        )
        right_score = (
            f'{analysis.current_score:.2f}' if analysis.current_score is not None else '--'
        )
        left_potential = (
            f'{reference.potential_score:.2f}' if reference.potential_score is not None else '--'
        )
        right_potential = (
            f'{analysis.potential_score:.2f}' if analysis.potential_score is not None else '--'
        )
        add('当前评分：')
        add(
            f'{left_score} / {right_score}',
            (reference.rating_color, analysis.rating_color),
        )
        add('理论最高：')
        add(f'{left_potential} / {right_potential}')
    else:
        score_label = '当前评分' if analysis.observed_cost is not None else '参考评分'
        add(f'{score_label} {analysis.score_text or current}', (analysis.rating_color,))
        add(f'理论最高 {potential}')

    def add_policy(text, label=''):
        policy_lines = [line for line in str(text or '').splitlines() if line.strip()]
        for index, line in enumerate(policy_lines):
            if index == 0 and label:
                line = line.replace('策略：', label + '策略：', 1)
            # summary_colours stores one tuple per rendered row; a bare RGB tuple
            # would be unpacked as three ints by the native painter.
            colour = POLICY_ACTION_COLOR if index == 0 else POLICY_DETAIL_COLOR
            add(line, (colour,))

    if paired:
        add_policy(left_policy_text, '左')
        add_policy(policy_text, '右')
    else:
        add_policy(policy_text)
    return chr(10).join(lines), tuple(colours)
