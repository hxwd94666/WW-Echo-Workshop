# 投影固定声骸候选目录。
"""One model for five UUID selectors; editable search never silently pins a row."""

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    Qt,
    QTimer,
    Signal,
    QSortFilterProxyModel,
)
from PySide6.QtWidgets import QListView, QFormLayout, QWidget
from echo_workshop.ui.widgets.inputs import PinyinCompleter as QCompleter
from echo_workshop.ui.widgets.inputs import QComboBox


class PinModel(QAbstractListModel):
    SEARCH_ROLE = Qt.UserRole + 1

    def __init__(self, catalog, missing_ids=(), parent=None):
        super().__init__(parent)
        self.catalog = catalog
        self.missing_ids = tuple(
            dict.fromkeys(
                value for value in missing_ids if value and value not in catalog.positions
            )
        )
        self.missing_positions = {
            value: len(catalog.labels) + index for index, value in enumerate(self.missing_ids)
        }
        self.data_calls = 0

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.catalog.labels) + len(self.missing_ids)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < self.rowCount():
            return None
        self.data_calls += 1
        row = index.row()
        if role in (Qt.DisplayRole, Qt.EditRole):
            if row < len(self.catalog.labels):
                return self.catalog.display_labels[row]
            value = self.missing_ids[row - len(self.catalog.labels)]
            return '缺失固定实例：' + value[:8] + '…' + value[-4:]
        if role in (Qt.ToolTipRole, self.SEARCH_ROLE):
            return (
                self.catalog.labels[row]
                if row < len(self.catalog.labels)
                else '缺失固定实例：' + self.missing_ids[row - len(self.catalog.labels)]
            )
        if role == Qt.UserRole:
            if row == 0:
                return ''
            return (
                self.catalog.records[row - 1].item_id
                if row < len(self.catalog.labels)
                else self.missing_ids[row - len(self.catalog.labels)]
            )
        return None

    def position(self, item_id):
        return self.catalog.positions.get(item_id, self.missing_positions.get(item_id, -1))


class CostPinModel(QSortFilterProxyModel):
    def __init__(self, source, parent):
        super().__init__(parent)
        self.cost = None
        self.setSourceModel(source)

    def filterAcceptsRow(self, row, parent):
        if row == 0 or self.cost is None:
            return True
        return row in self.sourceModel().catalog.cost_positions[self.cost]

    def position(self, item_id):
        source = self.sourceModel()
        return self.mapFromSource(source.index(source.position(item_id), 0)).row()


class PinSelector(QComboBox):
    automatic_pinyin_completion = False

    def __init__(self, model, item_id='', parent=None):
        super().__init__(parent)
        self.pin_model = model
        self.filtered_model = CostPinModel(model, self)
        self._retained = None
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(20)
        self.setMaxVisibleItems(16)
        view = QListView(self)
        view.setUniformItemSizes(True)
        self.setView(view)
        self.setModel(self.filtered_model)
        self.setEditable(True)
        self.setCompleter(
            None
        )  # Native QComboBox scans completions even for programmatic selection.
        self.setInsertPolicy(QComboBox.NoInsert)
        self.lineEdit().setPlaceholderText('搜索名称/套装/词条/完整 UUID；选择结果')
        completion = QCompleter(self.filtered_model, self, search_role=PinModel.SEARCH_ROLE)
        completion.setCaseSensitivity(Qt.CaseInsensitive)
        completion.setFilterMode(Qt.MatchContains)
        completion.setCompletionMode(QCompleter.PopupCompletion)
        popup = QListView()
        popup.setUniformItemSizes(True)
        completion.setPopup(popup)
        completion.activated[QModelIndex].connect(self._completion_selected)
        completion.setWidget(self.lineEdit())
        self._pin_completer = completion
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(120)
        self.search_timer.timeout.connect(self._search)
        self.lineEdit().textEdited.connect(lambda _text: self.search_timer.start())
        self.currentIndexChanged.connect(self._tooltip)
        self.setCurrentIndex(model.position(item_id))
        self._tooltip()

    def _tooltip(self, *_):
        self.setToolTip(self.currentData(Qt.ToolTipRole) or '')

    def set_cost(self, cost):
        if self.filtered_model.cost == cost:
            return
        try:
            item_id = self.selected_id()
        except ValueError:
            item_id = None
        raw = self.currentText()
        blocked = self.blockSignals(True)
        try:
            self.filtered_model.cost = cost
            self.filtered_model.invalidateFilter()
            position = self.filtered_model.position(item_id) if item_id is not None else -1
            self.setCurrentIndex(position)
            self._retained = None
            if position < 0:
                # Preserve incompatible/missing fixed IDs until explicitly replaced.
                self.setEditText(raw)
                if item_id:
                    self._retained = (item_id, raw)
                self.setToolTip('原固定件与当前槽位不匹配，请重新选择或解除固定')
        finally:
            self.blockSignals(blocked)

    def completer(self):
        return self._pin_completer

    def _search(self):
        if not self.isVisible():
            return
        self._pin_completer.setCompletionPrefix(self.lineEdit().text())
        self._pin_completer.complete()

    def _completion_selected(self, index):
        position = self.filtered_model.position(index.data(Qt.UserRole))
        self.setCurrentIndex(position)
        self.setEditText(self.currentData(Qt.DisplayRole) or '')

    def selected_id(self):
        raw = self.currentText()
        expected = self.currentData(Qt.DisplayRole)
        if raw == expected:
            return self.currentData()
        if self.currentIndex() < 0 and self._retained and raw == self._retained[1]:
            return self._retained[0]
        text = raw.strip()
        if not text:
            return ''
        # Exact full UUID is unambiguous; partial/name searches need selection.
        position = self.filtered_model.position(text)
        if position >= 0:
            return text
        raise ValueError('固定实例搜索尚未选择；请选择结果或输入完整 UUID，清空表示不固定')


class FixedItemsPicker(QWidget):
    """Five UUID selectors sharing one catalogue; programmatic loads are silent."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.form = QFormLayout(self)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.boxes = []
        self.catalog = self.model = None

    def load(self, catalog, costs, item_ids=None):
        self.catalog = catalog
        pins = tuple(item_ids or ('',) * 5)
        key = (id(catalog), tuple(costs), pins)
        if key == getattr(self, '_loaded_key', None) and self.boxes:
            try:
                if self.item_ids() == pins:
                    return
            except ValueError:
                pass  # A pending ambiguous search must still be reset by load.
        self._loaded_key = key
        old_model = self.model
        self.model = PinModel(self.catalog, pins, self)
        while self.form.rowCount():
            self.form.removeRow(0)
        self.boxes = []
        for index, (cost, item_id) in enumerate(zip(costs, pins), 1):
            box = PinSelector(self.model, item_id, self)
            box.set_cost(cost)
            box.setMinimumContentsLength(0)
            box.setMinimumWidth(0)
            box.currentIndexChanged.connect(lambda *_: self.changed.emit())
            box.lineEdit().textEdited.connect(lambda *_: self.changed.emit())
            self.form.addRow(f'第 {index} 槽 · C{cost}', box)
            self.boxes.append(box)
        if old_model is not None:
            old_model.deleteLater()

    def item_ids(self):
        return tuple(box.selected_id() for box in self.boxes)

    def set_costs(self, costs):
        for index, cost in enumerate(costs):
            self.form.labelForField(self.boxes[index]).setText(f'第 {index + 1} 槽 · C{cost}')
            self.boxes[index].set_cost(cost)
