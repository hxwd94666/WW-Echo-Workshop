# 独立枚举固定样例的五件组合并验证当前达标让分策略。
from fractions import Fraction


def rank(totals, targets, complete, moves):
    positive = [(total, target) for total, target in zip(totals, targets) if target > 0]
    ratios = [min(Fraction(total, target), 1) for total, target in positive]
    return (
        sum(total >= target for total, target in positive),
        min(ratios, default=1),
        complete,
        sum(min(total, target) if target else total for total, target in zip(totals, targets)),
        sum(totals),
        -moves,
    )


def objective(rows, *, exact=False):
    value = rank(
        [r.total_cents for r in rows],
        [r.target_cents for r in rows],
        sum(len(r.item_ids) == 5 for r in rows),
        sum(r.moves for r in rows),
    )
    return value if exact else (value[0], float(value[1]), *value[2:])
