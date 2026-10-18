# 判断声骸与角色明确要求的相性。
"""Declarative per-modal compatibility and reproducible inventory filters."""

from dataclasses import dataclass

from echo_workshop.core.scoring import ELEMENT_NAMES
from echo_workshop.core.text import simplify_echo_text


@dataclass(frozen=True)
class CompatibilityRule:
    element: str = ''
    sonatas: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.element, str) or (
            self.element and self.element not in ELEMENT_NAMES
        ):
            raise ValueError('Unknown explicit element')
        if not isinstance(self.sonatas, tuple) or any(
            not isinstance(name, str) or not name.strip() for name in self.sonatas
        ):
            raise ValueError('Sonata requirements must be explicit names')
        if len(self.sonatas) != len(set(self.sonatas)):
            raise ValueError('Duplicate sonata requirement')

    def to_dict(self):
        return {'element': self.element, 'sonatas': list(self.sonatas)}

    @classmethod
    def from_dict(cls, row):
        if (
            not isinstance(row, dict)
            or set(row) != {'element', 'sonatas'}
            or not isinstance(row['sonatas'], list)
        ):
            raise ValueError('Malformed explicit compatibility rule')
        return cls(row['element'], tuple(row['sonatas']))


def compatibility(record, rule):
    elements = [
        stat.stat_name.removesuffix('伤害加成')
        for stat in record.main_stats
        if stat.stat_name.removesuffix('伤害加成') in ELEMENT_NAMES
    ]
    element = (
        'unknown'
        if not rule.element
        else 'mismatch'
        if any(value != rule.element for value in elements)
        else 'match'
    )
    canonical_set = simplify_echo_text(record.sonata).strip()
    sonata = (
        'unknown'
        if not rule.sonatas or not canonical_set
        else 'match'
        if canonical_set in {simplify_echo_text(name).strip() for name in rule.sonatas}
        else 'mismatch'
    )
    overall = (
        'mismatch'
        if 'mismatch' in (element, sonata)
        else 'match'
        if (element, sonata) == ('match', 'match')
        else 'unknown'
    )
    return element, sonata, overall
