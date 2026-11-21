# 只投影实时评分数值，诊断与收录反馈留在工坊页面和日志。
from ww_echo_workshop.application.hud_state import HUD_POLICY_BUSY, HUD_POLICY_HEADER


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


def live_summary(
    analysis, reference=None, policy_text='', left_policy_text='', *, policy_enabled=None
):
    if policy_enabled is None:
        policy_enabled = bool(policy_text or left_policy_text)
    if analysis.current_score is None and (
        not policy_enabled or not getattr(analysis, 'layout', '')
    ):
        return '', ()
    current = f'{analysis.current_score:.2f}' if analysis.current_score is not None else '--'
    potential = f'{analysis.potential_score:.2f}' if analysis.potential_score is not None else '--'
    lines = []
    colours = []

    def add(text, colour=()):
        lines.append(text)
        colours.append(colour)

    left_ready = reference is not None and reference.current_score is not None
    paired = getattr(analysis, 'comparison', False) or left_ready
    label = (analysis.selected_template or _value(analysis, '评分模版')).replace(' (自动匹配)', '')
    add(label if paired else '模版：' + label, ((255, 110, 220),))
    if paired:
        left_score = f'{reference.current_score:.2f}' if left_ready else '--'
        left_potential = (
            f'{reference.potential_score:.2f}'
            if left_ready and reference.potential_score is not None
            else '--'
        )
        add('当前评分：')
        add(
            f'{left_score} / {current}',
            (reference.rating_color if left_ready else None, analysis.rating_color),
        )
        add('理论最高：')
        add(f'{left_potential} / {potential}', (None, None))
    else:
        confirmed_cost = (
            analysis.observed_cost is not None
            or getattr(analysis, 'retained_cost', None) is not None
        )
        score_label = '当前评分' if confirmed_cost else '参考评分'
        add(f'{score_label} {analysis.score_text or current}', (analysis.rating_color,))
        add(f'理论最高 {potential}')

    def add_policy(text, label=''):
        policy_lines = [line for line in str(text or '').splitlines() if line.strip()]
        busy = '策略：' + HUD_POLICY_BUSY
        action = policy_lines[0] if policy_lines else busy
        expected = HUD_POLICY_BUSY if action == busy else '—'
        detail = next(
            (line for line in policy_lines[1:] if line.startswith('期望：')), '期望：' + expected
        )
        if label:
            action = action.replace('策略：', label + '策略：', 1)
        # Keep both field names visible in Qt and GDI before a result is ready.
        add(action, (POLICY_ACTION_COLOR,))
        add(detail, (POLICY_DETAIL_COLOR,))

    if policy_enabled:
        add(HUD_POLICY_HEADER, (POLICY_ACTION_COLOR,))
        if paired:
            add_policy(left_policy_text, '左')
            add_policy(policy_text, '右')
        else:
            add_policy(policy_text)
    return chr(10).join(lines), tuple(colours)
