# 准备声骸卡片的完整展示内容。
"""Read-only, geometry-free card projection. Preserve observed row order."""

from dataclasses import dataclass
from echo_workshop.application.record_quality import quality_for
from echo_workshop.core.scoring import MAX_MAINSTAT_VALUES, SUBSTAT_TIERS

FLAT_STATS = frozenset(('攻击', '生命', '防御'))
PERCENT_STATS = (
    frozenset(name for slots in MAX_MAINSTAT_VALUES.values() for slot in slots for name in slot)
    | frozenset(SUBSTAT_TIERS)
) - FLAT_STATS
DAMAGE_LABELS = {name: name + '伤害加成' for name in ('普攻', '重击', '共鸣技能', '共鸣解放')}


def checked_protection_text(record, quality, reservation=''):
    return _protection_text(record, not quality_for(record, quality).usable, reservation)


def _protection_text(record, pending, reservation):
    parts = ['待核对'] if pending else []
    if record.locked:
        parts.append('手动保护')
    if reservation:
        label = reservation.removeprefix('已预留：')
        warning = '（待复核）' if label.endswith('（待复核）') else ''
        # 仅缩短卡片显示，不更改已保存模版、角色身份或占用关系。
        name = label.removesuffix('（待复核）').split('-', 1)[0].split('－', 1)[0].strip()
        parts.append('配装占用：' + name + warning)
    return ' · '.join(parts)


def stat_value_text(stat):
    unit = (
        ''
        if stat.stat_name in FLAT_STATS
        else '%'
        if stat.stat_name in PERCENT_STATS
        else '（单位待核对）'
    )
    return f'{stat.value:g}{unit}'


@dataclass(frozen=True)
class CardRow:
    name: str
    value: str
    main: bool
    uncertain: bool = False


@dataclass(frozen=True)
class CardData:
    item_id: str
    title: str
    metadata: str
    rows: tuple[CardRow, ...]
    empty_sub: str
    state: str
    issues: tuple[str, ...]
    owner: str = ''
    owner_character_id: str = ''
    locked: bool = False

    @property
    def text(self):
        parts = [self.title, self.metadata]
        parts.extend(
            f'{row.name} {row.value}' + (' · 待核对' if row.uncertain else '') for row in self.rows
        )
        if self.empty_sub:
            parts.append(self.empty_sub)
        if self.owner:
            parts.append(self.owner)
        parts.append(self.state)
        return '\n'.join(parts)


def project_card(record, kind='item'):
    """No scoring normalization, deduplication, sorting, image IO or OCR."""
    issues = record.validation_issues
    return _project_card(record, kind, issues, record.review == 'confirmed' and not issues)


def project_checked_card(record, quality, kind='item'):
    """Use the native verdict from this exact frozen inventory read."""
    quality = quality_for(record, quality)
    return _project_card(record, kind, quality.issues, quality.usable)


def _project_card(record, kind, issues, usable):
    ocr_uncertain = any(
        issue in ('ocr:ambiguous_value', 'ocr:missing_tuned_rows') for issue in issues
    )
    rows = []
    for main, stats in ((True, record.main_stats), (False, record.sub_stats)):
        for index, stat in enumerate(stats):
            prefix = 'main:' if main else 'sub:'
            uncertain = (
                ocr_uncertain
                or stat.stat_name not in FLAT_STATS | PERCENT_STATS
                or any(
                    issue.startswith(prefix)
                    and (not main or issue in ('main:expected_two_slots', f'main:invalid:{index}'))
                    for issue in issues
                )
            )
            rows.append(
                CardRow(
                    DAMAGE_LABELS.get(stat.stat_name, stat.stat_name.removesuffix('%')),
                    stat_value_text(stat),
                    main,
                    uncertain,
                )
            )
    state = (
        '疑似升级 · 待核对'
        if kind == 'upgrade'
        else '疑似重复 · 待核对'
        if kind != 'item'
        else '待核对'
        if not usable
        else '已确认'
    )
    if record.locked:
        state += ' · 已保护'
    if record.handled:
        state += ' · 已处理'
    level = '等级待核对' if record.level is None else f'+{record.level}'
    metadata = f'COST {record.cost} · {level} · {record.sonata or "套装待核对"}'
    empty = (
        '' if record.sub_stats else '未开启副词条' if record.level == 0 else '暂无副词条 · 待核对'
    )
    owner = record.owner if record.owner else ('游戏已装备' if record.game_equipped is True else '')
    return CardData(
        record.item_id,
        record.name or '名称待核对',
        metadata,
        tuple(rows),
        empty,
        state,
        issues,
        owner,
        record.owner_character_id,
        record.locked,
    )
