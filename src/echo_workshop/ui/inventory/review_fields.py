# 提供核对字段的本地候选和严格搜索选择框。
from collections import Counter
from dataclasses import dataclass

from echo_workshop.core.filters import sonata_options
from echo_workshop.resources.echo_names import builtin_echo_names
from echo_workshop.ui.widgets.inputs import QComboBox


OwnerValue = tuple[str, str, str]


@dataclass(frozen=True)
class ReviewChoices:
    names: tuple[str, ...]
    sonatas: tuple[str, ...]
    owners: tuple[tuple[str, OwnerValue], ...]

    @classmethod
    def from_runtime(cls, runtime, record):
        records = (*runtime.inventory.list(record.profile_id), record)
        names = tuple(
            sorted(
                {
                    *runtime.game_catalog.active.echo_names,
                    *(row.name.strip() for row in records if row.name.strip()),
                }
            )
        )
        characters = {k: v['name'] for k, v in runtime.game_catalog.active.characters.items()}
        counts = Counter(characters.values())
        owners = [('未装备', ('', 'unequipped', ''))]
        for identity, name in sorted(characters.items(), key=lambda row: (row[1], row[0])):
            label = name if counts[name] == 1 else f'{name}（{identity}）'
            owners.append((label, (name, 'equipped', identity)))
        return cls(names, sonata_options(runtime.sonata.active, records), tuple(owners))

    @classmethod
    def from_record(cls, record):
        owner = (record.owner, record.owner_state, record.owner_character_id)
        owners = [('未装备', ('', 'unequipped', ''))]
        if owner != owners[0][1]:
            owners.append((_retained_label(owner), owner))
        return cls(
            tuple(dict.fromkeys((*builtin_echo_names(), *((record.name,) if record.name else ())))),
            (record.sonata,) if record.sonata else (),
            tuple(owners),
        )


def _retained_label(value):
    if isinstance(value, tuple):
        return '保留原值：' + value[0] if value[0] else '未记录'
    return '保留原值：' + str(value)


class ChoiceBox(QComboBox):
    def __init__(self, entries, current_value, *, field_name, parent=None):
        super().__init__(parent)
        self.field_name = field_name
        self.setAccessibleName(field_name)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setMinimumWidth(0)
        self.setMinimumContentsLength(8)
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.lineEdit().setPlaceholderText(f'搜索{field_name}（中文 / 拼音 / 首字母）')
        self.addItem(f'请选择{field_name}', None)
        selected = 0
        for label, value in entries:
            self.addItem(label, value)
            if value == current_value:
                selected = self.count() - 1
        if selected == 0 and current_value not in ('', None):
            self.addItem(_retained_label(current_value), current_value)
            selected = self.count() - 1
        self.setCurrentIndex(selected)

    def selected_value(self):
        index = self.currentIndex()
        if index < 0 or self.currentText() != self.itemText(index) or self.itemData(index) is None:
            raise ValueError(f'请从列表选择{self.field_name}。')
        return self.itemData(index)
