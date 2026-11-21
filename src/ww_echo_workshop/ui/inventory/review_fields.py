# 提供核对字段的本地候选和严格搜索选择框。
from ww_echo_workshop.application.inventory_review import owner_label
from ww_echo_workshop.ui.widgets.inputs import QComboBox


def _retained_label(value):
    if isinstance(value, tuple):
        return owner_label(value)
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
