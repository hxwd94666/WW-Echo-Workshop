# 准备核对表单使用的声骸名称、套装和装备者候选。
from collections import Counter
from dataclasses import dataclass

from ww_echo_workshop.core.filters import sonata_options
from ww_echo_workshop.resources.echo_names import builtin_echo_names


OwnerValue = tuple[str, str, str]


def owner_label(owner):
    return '保留原值：' + owner[0] if owner[0] else '未记录'


@dataclass(frozen=True)
class ReviewChoices:
    names: tuple[str, ...]
    sonatas: tuple[str, ...]
    owners: tuple[tuple[str, OwnerValue], ...]

    @classmethod
    def from_inventory(cls, records, record, *, catalog, sonata):
        records = (*records, record)
        names = tuple(
            sorted(
                {*catalog.echo_names, *(row.name.strip() for row in records if row.name.strip())}
            )
        )
        characters = {key: row['name'] for key, row in catalog.characters.items()}
        counts = Counter(characters.values())
        owners = [('未装备', ('', 'unequipped', ''))]
        for identity, name in sorted(characters.items(), key=lambda row: (row[1], row[0])):
            label = name if counts[name] == 1 else f'{name}（{identity}）'
            owners.append((label, (name, 'equipped', identity)))
        return cls(names, sonata_options(sonata, records), tuple(owners))

    @classmethod
    def from_record(cls, record):
        owner = (record.owner, record.owner_state, record.owner_character_id)
        owners = [('未装备', ('', 'unequipped', ''))]
        if owner != owners[0][1]:
            owners.append((owner_label(owner), owner))
        return cls(
            tuple(dict.fromkeys((*builtin_echo_names(), *((record.name,) if record.name else ())))),
            (record.sonata,) if record.sonata else (),
            tuple(owners),
        )
