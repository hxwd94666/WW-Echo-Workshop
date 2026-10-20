# 展示并校验配装要求控件。
"""Explicit five-slot main-stat and real sonata-tier controls, without workers.

Programmatic loads are silent. Legacy multi-main/count values are retained until
the user changes them, and hidden sonata choices are not discarded by filtering.
"""

from PySide6.QtCore import QPoint, Signal
from PySide6.QtWidgets import QHBoxLayout, QMenu, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, PushButton, FluentIcon

from echo_workshop.ui.widgets.inputs import ComboBox
from echo_workshop.core.filters import STAT_LABELS, canonical_stat
from echo_workshop.core.scoring import MAX_MAINSTAT_VALUES


def _costs(values):
    values = tuple(values)
    if len(values) != 5 or any(
        type(value) is not int or value not in (1, 3, 4) for value in values
    ):
        raise ValueError('请为五个槽位设置 COST 1、3 或 4。')
    return values


def _names(values):
    if isinstance(values, str):
        raise ValueError('主词条或套装名称须为列表。')
    values = tuple(values)
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError('名称列表包含无效值。')
    return tuple(dict.fromkeys(value.strip() for value in values))


def _main_rows(values):
    values = tuple(values)
    if len(values) != 5:
        raise ValueError('请提供五个槽位的主词条要求。')
    return tuple(
        tuple(dict.fromkeys(canonical_stat(name) for name in _names(row))) for row in values
    )


def _counts(rows):
    values = {}
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            raise ValueError('套装数量要求须为名称、数量列表。')
        name, count = row
        if (
            not isinstance(name, str)
            or not name.strip()
            or type(count) is not int
            or not 0 <= count <= 5
        ):
            raise ValueError('套装数量要求格式无效。')
        name = name.strip()
        if name in values:
            raise ValueError('重复套装数量要求。')
        values[name] = count
    return values


def _main_label(name, cost):
    value = MAX_MAINSTAT_VALUES[cost][0].get(name)
    label = STAT_LABELS.get(name, name)
    return f'{label} {value:g}%' if value is not None else '旧要求：' + label


class _MainNamesButton(PushButton):
    changed = Signal()

    def __init__(self, parent=None):
        # Fluent's text constructor calls self.__init__ again; subclasses must
        # use the parent-only overload to avoid recursive dispatch.
        super().__init__(parent=parent)
        self.setText('默认')
        self.setIcon(FluentIcon.CHEVRON_DOWN_MED)
        self.cost, self.names = 1, ()
        self._menu = None
        self.setMinimumWidth(0)
        self.clicked.connect(self.show_choices)

    def set_spec(self, cost, names):
        self.cost, self.names = cost, tuple(names)
        values = [_main_label(name, cost) for name in self.names]
        text = (
            '默认'
            if not values
            else ' / '.join(values)
            if len(values) <= 2
            else f'已选 {len(values)} 种主词条'
        )
        self.setText(text)
        self.setToolTip('默认' if not values else '\n'.join(values))

    def show_choices(self):
        if self._menu is not None:
            self._menu.close()
            self._menu.deleteLater()
        menu = self._menu = QMenu(self)
        action = menu.addAction('默认')
        action.setCheckable(True)
        action.setChecked(not self.names)
        action.setData(None)
        action.triggered.connect(lambda: self._choose(None))
        menu.addSeparator()
        options = tuple(MAX_MAINSTAT_VALUES[self.cost][0])
        for name in dict.fromkeys((*options, *self.names)):
            action = menu.addAction(_main_label(name, self.cost))
            action.setCheckable(True)
            action.setChecked(name in self.names)
            action.setData(name)
            action.triggered.connect(lambda _checked=False, value=name: self._choose(value))
        menu.popup(self.mapToGlobal(QPoint(0, self.height())))

    def _choose(self, name):
        values = (
            ()
            if name is None
            else (
                tuple(value for value in self.names if value != name)
                if name in self.names
                else (*self.names, name)
            )
        )
        if values != self.names:
            self.set_spec(self.cost, values)
            self.changed.emit()


class MainRequirementsPicker(QWidget):
    changed = Signal()

    def __init__(self, parent=None, *, title='主词条自选'):
        super().__init__(parent)
        self._costs, self._main_names = (4, 3, 3, 1, 1), ((), (), (), (), ())
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        if title:
            root.addWidget(BodyLabel(title, self))
        self.labels, self.boxes = [], []
        for index in range(5):
            row = QHBoxLayout()
            label = CaptionLabel(self)
            label.setMinimumWidth(112)
            button = _MainNamesButton(self)
            button.changed.connect(lambda slot=index: self._edited(slot))
            row.addWidget(label)
            row.addWidget(button, 1)
            root.addLayout(row)
            self.labels.append(label)
            self.boxes.append(button)
        self.feedback = CaptionLabel(self)
        self.feedback.setWordWrap(True)
        root.addWidget(self.feedback)
        self._render()

    def costs(self):
        return self._costs

    def main_names(self):
        return self._main_names

    def load(self, costs, main_names):
        values, rows = _costs(costs), _main_rows(main_names)
        self._costs, self._main_names = values, rows
        self._render()

    def set_costs(self, costs):
        self._costs = _costs(costs)
        self._render()

    @property
    def validation_error(self):
        invalid = [
            str(i + 1)
            for i, (cost, names) in enumerate(zip(self._costs, self._main_names))
            if any(name not in MAX_MAINSTAT_VALUES[cost][0] for name in names)
        ]
        return (
            '第 ' + ', '.join(invalid) + ' 槽仍保留旧主词条要求；请按当前 COST 重新选择。'
            if invalid
            else ''
        )

    def validate(self):
        if self.validation_error:
            raise ValueError(self.validation_error)

    def _render(self):
        for index, (cost, names) in enumerate(zip(self._costs, self._main_names)):
            self.labels[index].setText(f'第 {index + 1} 槽 · COST {cost}')
            self.boxes[index].set_spec(cost, names)
        self.feedback.setText(self.validation_error)
        self.feedback.setVisible(bool(self.validation_error))

    def _edited(self, slot):
        values = list(self._main_names)
        values[slot] = self.boxes[slot].names
        self._main_names = tuple(values)
        self._render()
        self.changed.emit()


class SonataEffectsPicker(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._allowed, self._tiers, self._counts = (), {}, {}
        self._descriptions = {}
        self._loading = False
        self.boxes = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        self.rows = QWidget(self)
        self.rows_layout = QVBoxLayout(self.rows)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(8)
        root.addWidget(self.rows)
        self.feedback = CaptionLabel(self)
        self.feedback.setWordWrap(True)
        root.addWidget(self.feedback)
        self._render()

    def allowed(self):
        return self._allowed

    def set_allowed(self, names, tiers=None, *, default_max=False):
        allowed = _names(names)
        if tiers is not None:
            self._set_tiers(tiers)
        if default_max:
            for name in allowed:
                if name not in self._counts:
                    self._counts[name] = max(self._tiers.get(name, ()), default=0)
        self._allowed = allowed
        self._render()

    def set_tiers(self, tiers):
        self._set_tiers(tiers)
        self._render()

    def set_descriptions(self, descriptions):
        """Attach display-only effect text; never infer numeric score bonuses."""
        if not isinstance(descriptions, dict):
            raise ValueError('套装效果描述须为名称字典。')
        values = {}
        for name, rows in descriptions.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(rows, dict):
                raise ValueError('套装效果描述格式无效。')
            if any(
                type(count) is not int or not isinstance(text, str) for count, text in rows.items()
            ):
                raise ValueError('套装效果描述须为整数门槛和文本。')
            values[name.strip()] = dict(rows)
        self._descriptions = values
        self._render()

    def _set_tiers(self, tiers):
        if not isinstance(tiers, dict):
            raise ValueError('套装效果门槛资料须为名称字典。')
        values = {}
        for name, counts in tiers.items():
            if not isinstance(name, str) or not name.strip() or isinstance(counts, (str, bytes)):
                raise ValueError('套装效果门槛资料格式无效。')
            counts = tuple(counts)
            if any(type(count) is not int for count in counts):
                raise ValueError('套装效果门槛须为整数列表。')
            values[name.strip()] = tuple(
                sorted(set(count for count in counts if count in (1, 2, 3, 4, 5)))
            )
        self._tiers = values

    def set_requirements(self, rows):
        self._counts = _counts(rows)
        self._render()

    def load(self, allowed, requirements, tiers=None):
        # All public load helpers are silent; preserving hidden values applies
        # within a role, while loading another role replaces its entire memory.
        names, counts = _names(allowed), _counts(requirements)
        if tiers is not None:
            self._set_tiers(tiers)
        # 已加载的未限制选择也显式记为0，避免刷新或重新勾选时覆盖用户决定。
        for name in names:
            counts.setdefault(name, 0)
        self._allowed, self._counts = names, counts
        self._render()

    def requirements(self):
        return tuple(
            (name, self._counts.get(name, 0)) for name in self._allowed if self._counts.get(name, 0)
        )

    @property
    def validation_error(self):
        total = sum(count for _, count in self.requirements())
        return f'套装数量要求合计 {total} 件，最多只能要求 5 件。' if total > 5 else ''

    def validate(self):
        if self.validation_error:
            raise ValueError(self.validation_error)

    def _render(self):
        self._loading = True
        try:
            while self.rows_layout.count():
                item = self.rows_layout.takeAt(0)
                if item.widget() is not None:
                    item.widget().hide()
                    item.widget().deleteLater()
            self.boxes = {}
            for name in self._allowed:
                row = QWidget(self.rows)
                layout = QHBoxLayout(row)
                layout.setContentsMargins(0, 0, 0, 0)
                label = CaptionLabel(name, row)
                label.setWordWrap(True)
                label.setMinimumWidth(0)
                label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
                layout.addWidget(label, 1)
                box = ComboBox(row)
                box.setMinimumWidth(0)
                box.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
                box.addItem('不要求套装效果', userData=0)
                for count in self._tiers.get(name, ()):
                    box.addItem(f'{count} 件套装效果', userData=count)
                count = self._counts.get(name, 0)
                if count and box.findData(count) < 0:
                    box.addItem(f'保留旧数量 {count} 件（效果资料待确认）', userData=count)
                box.setCurrentIndex(max(0, box.findData(count)))
                description = self._descriptions.get(name, {}).get(count, '').strip()
                box.setToolTip(description or '资料未知，仅约束件数')
                box.currentIndexChanged.connect(
                    lambda _index, identity=name, control=box: self._edited(identity, control)
                )
                layout.addWidget(box, 1)
                self.rows_layout.addWidget(row)
                self.boxes[name] = box
            unknown = any(
                count > 1 and count not in self._tiers.get(name, ())
                for name, count in self.requirements()
            )
            message = self.validation_error or (
                '已保留旧数量；对应套装效果资料尚未确认。' if unknown else ''
            )
            self.feedback.setText(message)
            self.feedback.setVisible(bool(message))
        finally:
            self._loading = False

    def _edited(self, name, box):
        if self._loading:
            return
        count, old = box.currentData(), self._counts.get(name, 0)
        if type(count) is not int or count == old:
            return
        total = sum(value for _, value in self.requirements()) - old + count
        if total > 5 and count > old:
            blocked = box.blockSignals(True)
            box.setCurrentIndex(box.findData(old))
            box.blockSignals(blocked)
            self.feedback.setText('套装数量要求总和最多 5 件，请先减少其他套装的要求。')
            self.feedback.show()
            return
        self._counts[name] = count
        self._render()
        self.changed.emit()
