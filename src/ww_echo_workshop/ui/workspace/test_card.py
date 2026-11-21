# 工作台强化测试卡：手动构造声骸并异步求解，不写库存或角色设置。
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtWidgets import QLabel, QPushButton
from ww_echo_workshop.ui.widgets.controls import (
    CaptionLabel,
    CardWidget,
    PrimaryPushButton,
    SubtitleLabel,
)

from ww_echo_workshop.application.live_preferences import LiveOption as Option
from ww_echo_workshop.application.policy import build_payload
from ww_echo_workshop.core.policy import (
    POLICY_SOURCE,
    PolicyError,
    check_target,
    format_probability,
)
from ww_echo_workshop.core.policy_presets import SYSTEMS, default_scoring
from ww_echo_workshop.core.scoring import MAX_MAINSTAT_VALUES, SUBSTAT_TIERS
from ww_echo_workshop.ui.widgets.inputs import QComboBox, QDoubleSpinBox
from ww_echo_workshop.ui.widgets.roles import RolePicker
from ww_echo_workshop.ui.workspace.preset_picker import PresetPickerDialog

STAT_NAMES = tuple(SUBSTAT_TIERS)


def _main_stats(cost):
    rows = []
    for slot in MAX_MAINSTAT_VALUES[cost]:
        name, value = next(iter(slot.items()))
        rows.append(SimpleNamespace(stat_name=name, value=float(value)))
    return tuple(rows)


class PolicyTestCard(CardWidget):
    preset_changed = Signal()

    def __init__(
        self,
        parent=None,
        *,
        preferences,
        weight_snapshot,
        live_snapshot,
        presets,
        state_store,
        solve,
    ):
        super().__init__(parent)
        self.preferences = preferences
        self._weight_snapshot = weight_snapshot
        self._live_snapshot = live_snapshot
        self._solve = solve
        self._loading = True
        self._state_store = state_store
        self._preset_catalog = presets
        self._future = None
        self._last_result = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='policy-test')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        heading = SubtitleLabel('声骸强化测试', self)
        layout.addWidget(heading)

        snapshot = self._weight_snapshot()
        configured = self.preferences.role_key()
        self.picker = RolePicker(snapshot, self, configured if all(configured) else None)
        layout.addWidget(self.picker)

        self.scoring = QComboBox(self)
        self.scoring.setMinimumWidth(180)
        self.scoring.addItem('XWUID（工坊默认）', userData='workshop_xwuid')
        for key, spec in SYSTEMS.items():
            if key == 'qq_bot':
                continue
            self.scoring.addItem(spec['label'], userData=key)
        self.scoring.setAccessibleName('策略评分体系')

        row = QHBoxLayout()
        row.addWidget(QLabel('评分体系', self))
        row.addWidget(self.scoring, 2)
        row.addSpacing(8)
        row.addWidget(QLabel('COST', self))
        self.cost = QComboBox(self)
        self.cost.setAccessibleName('声骸 COST')
        for cost in (1, 3, 4):
            self.cost.addItem(f'{cost}C', userData=cost)
        self.cost.setCurrentIndex(self.cost.findData(4))
        self.cost.currentIndexChanged.connect(self._cost_changed)
        row.addWidget(self.cost)
        self.main_label = CaptionLabel('', self)
        self.main_label.setWordWrap(True)
        row.addWidget(self.main_label, 1)
        layout.addLayout(row)
        self.preset_box = QWidget(self)
        preset_row = QHBoxLayout(self.preset_box)
        preset_row.setContentsMargins(0, 0, 0, 0)
        preset_row.addWidget(CaptionLabel('角色预设', self.preset_box))
        self.preset_name = CaptionLabel('未选择', self.preset_box)
        self.preset_name.setWordWrap(True)
        preset_row.addWidget(self.preset_name, 1)
        self.preset_button = QPushButton('选择预设', self.preset_box)
        self.preset_button.clicked.connect(self._pick_preset)
        preset_row.addWidget(self.preset_button)
        self.preset_match = QPushButton('从当前角色匹配', self.preset_box)
        self.preset_match.clicked.connect(self._match_current_role)
        preset_row.addWidget(self.preset_match)
        self.preset_box.hide()
        layout.addWidget(self.preset_box)
        self.preset_note = CaptionLabel('评分与权重来自实时评分设置', self)
        self.preset_note.setWordWrap(True)
        self.preset_note.hide()
        layout.addWidget(self.preset_note)

        layout.addWidget(QLabel('副词条（按实际档位填写；留空表示未揭示）', self))
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)
        self._rows = []
        defaults = ('暴击', '暴击伤害', None, None, None)
        for index, name in enumerate(defaults):
            combo = QComboBox(self)
            combo.addItem('不使用', userData=None)
            for stat_name in STAT_NAMES:
                combo.addItem(stat_name, userData=stat_name)
            spin = QDoubleSpinBox(self)
            spin.setRange(0.0, 10000.0)
            spin.setDecimals(1)
            spin.setSingleStep(0.1)
            combo.setAccessibleName(f'副词条 {index + 1} 类型')
            spin.setAccessibleName(f'副词条 {index + 1} 数值')
            combo.currentIndexChanged.connect(
                lambda _index, c=combo, s=spin: self._stat_changed(c, s)
            )
            grid.addWidget(combo, index, 0)
            grid.addWidget(spin, index, 1)
            self._rows.append((combo, spin))
            if name is not None:
                combo.setCurrentIndex(combo.findData(name))
                spin.setValue(float(SUBSTAT_TIERS[name][-1]))
        layout.addLayout(grid)

        self.candidate_box = QWidget(self)
        candidate_layout = QVBoxLayout(self.candidate_box)
        candidate_layout.setContentsMargins(0, 0, 0, 0)
        candidate_layout.addWidget(QLabel('重抽候选（仅重抽模式；留空不比较）', self.candidate_box))
        candidate_grid = QGridLayout()
        candidate_grid.setHorizontalSpacing(8)
        candidate_grid.setVerticalSpacing(6)
        candidate_grid.setColumnStretch(0, 3)
        candidate_grid.setColumnStretch(1, 2)
        self._candidate_rows = []
        for index in range(5):
            combo = QComboBox(self.candidate_box)
            combo.addItem('不使用', userData=None)
            for stat_name in STAT_NAMES:
                combo.addItem(stat_name, userData=stat_name)
            spin = QDoubleSpinBox(self.candidate_box)
            spin.setRange(0.0, 10000.0)
            spin.setDecimals(1)
            spin.setSingleStep(0.1)
            combo.setAccessibleName(f'候选词条 {index + 1} 类型')
            spin.setAccessibleName(f'候选词条 {index + 1} 数值')
            combo.currentIndexChanged.connect(
                lambda _index, c=combo, s=spin: self._stat_changed(c, s)
            )
            combo.currentIndexChanged.connect(lambda _index: self._sync_action_buttons())
            candidate_grid.addWidget(combo, index, 0)
            candidate_grid.addWidget(spin, index, 1)
            self._candidate_rows.append((combo, spin))
        candidate_layout.addLayout(candidate_grid)
        self.capture_button = QPushButton('捕获当前游戏声骸', self.candidate_box)
        self.capture_button.setToolTip('从最近一次实时识别结果填入五条候选；不自动接受。')
        self.capture_button.clicked.connect(self.capture_candidate)
        candidate_layout.addWidget(self.capture_button)
        self.candidate_box.hide()
        layout.addWidget(self.candidate_box)

        self.target = QDoubleSpinBox(self)
        self.target.setRange(0.0, 50.0)
        self.target.setDecimals(1)
        self.target.setSingleStep(0.5)
        self.target.setSpecialValueText('未设置')
        self.target.setAccessibleName('策略目标分')
        configured_target = float(self.preferences.get(Option.TARGET) or 0.0)
        self.target.setValue(configured_target if configured_target > 0 else 35.0)
        self.mode = QComboBox(self)
        self.mode.setAccessibleName('策略模式')
        for label, value in (('自动', 'auto'), ('仅强化', 'upgrade'), ('仅重抽', 'reroll')):
            self.mode.addItem(label, userData=value)
        self.mode.currentIndexChanged.connect(self._sync_candidate_visibility)
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.target_form = form
        form.addRow('目标分', self.target)
        self.main_score = QDoubleSpinBox(self)
        self.main_score.setRange(0.0, 65535.0)
        self.main_score.setDecimals(2)
        self.main_score.setValue(float(self.preferences.get(Option.MAIN_SCORE) or 0.0))
        self.main_score.setAccessibleName('策略主词条分')
        form.addRow('主词条分', self.main_score)
        self.max_score = QDoubleSpinBox(self)
        self.max_score.setRange(0.01, 655.35)
        self.max_score.setDecimals(2)
        self.max_score.setValue(float(self.preferences.get(Option.MAX_SCORE) or 100.0))
        self.max_score.setAccessibleName('策略归一满分')
        form.addRow('归一满分', self.max_score)
        form.addRow('策略模式', self.mode)
        layout.addLayout(form)

        self.calculate_button = PrimaryPushButton('计算策略', self)
        self.calculate_button.setAccessibleName('计算策略')
        self.calculate_button.clicked.connect(self.calculate)
        layout.addWidget(self.calculate_button)
        self.feedback = CaptionLabel('', self)
        self.feedback.setWordWrap(True)
        self.feedback.setAccessibleName('策略反馈')
        layout.addWidget(self.feedback)
        actions = QHBoxLayout()
        self.accept_button = PrimaryPushButton('接受候选', self)
        self.accept_button.setAccessibleName('接受候选')
        self.accept_button.clicked.connect(self.accept_candidate)
        self.accept_button.hide()
        actions.addWidget(self.accept_button)
        self.joint_button = QPushButton('转到重抽', self)
        self.joint_button.setAccessibleName('转到重抽策略')
        self.joint_button.clicked.connect(self.joint_to_reroll)
        self.joint_button.hide()
        actions.addWidget(self.joint_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.result_label = CaptionLabel('结果', self)
        layout.addWidget(self.result_label)
        self.result = QTextEdit(self)
        self.result.setReadOnly(True)
        self.result.setMinimumHeight(120)
        self.result.setAccessibleName('策略结果')
        self.result.setPlaceholderText('选择角色、目标和词条后点击计算策略。')
        layout.addWidget(self.result)
        self.result_label.hide()
        self.result.hide()
        layout.addWidget(CaptionLabel('策略数据：' + POLICY_SOURCE, self))

        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._poll)
        self._loading = False
        self._cost_changed()
        for combo, spin in self._rows:
            self._stat_changed(combo, spin)
        for label, value in (('联合（实验）', 'joint'),):
            self.mode.addItem(label, userData=value)
        self.mode.setCurrentIndex(self.mode.findData('auto'))
        configured_scoring = self.preferences.get(Option.SCORING_SYSTEM, 'workshop_xwuid')
        index = self.scoring.findData(configured_scoring)
        self.scoring.setCurrentIndex(index if index >= 0 else 0)
        self.scoring.currentIndexChanged.connect(self._scoring_changed)
        self._scoring_changed(initial=True)
        self.reload_policy_data()
        self._restore_state()

    def _scoring_changed(self, *_, initial=False):
        if self._loading:
            return
        system = self.scoring.currentData() or 'workshop_xwuid'
        self.preferences.set(Option.SCORING_SYSTEM, system)
        self.picker.setVisible(system == 'workshop_xwuid')
        self.preset_note.setVisible(system != 'workshop_xwuid')
        self.preset_box.setVisible(system != 'workshop_xwuid')
        self.refresh_presets()
        if system == 'workshop_xwuid':
            self.target.setRange(0.0, 50.0)
        elif system == 'fixed':
            self.target.setRange(0.0, 65535.0)
        else:
            self.target.setRange(0.0, 655.35)
        if system != 'workshop_xwuid':
            spec = default_scoring(system)
            self.target.setValue(float(spec['target']))
            self.main_score.setValue(float(spec['main_score']))
            self.max_score.setValue(float(spec['max_score']))
        elif not initial:
            self.target.setValue(float(self.preferences.get(Option.TARGET) or 0.0))
        if system != 'workshop_xwuid':
            self.preferences.set(Option.MAIN_SCORE, float(self.main_score.value()))
            self.preferences.set(Option.MAX_SCORE, float(self.max_score.value()))
        fixed = system == 'fixed'
        editable = system != 'workshop_xwuid'
        self.main_score.setEnabled(editable and not fixed)
        self.max_score.setEnabled(editable and not fixed)
        show_detail = editable and not fixed
        self.target_form.setRowVisible(1, show_detail)
        self.target_form.setRowVisible(2, show_detail)

    def _cost(self):
        return int(self.cost.currentData() or 4)

    def _cost_changed(self):
        if self._loading:
            return
        mains = ' / '.join(f'{row.stat_name} {row.value:g}' for row in _main_stats(self._cost()))
        self.main_label.setText('主词条（+25 口径）：' + mains)

    def refresh_presets(self):
        self._preset_catalog.load()
        system = self.scoring.currentData() or 'workshop_xwuid'
        preset_id = self.preferences.get(Option.PRESET_ID, '')
        preset = self._preset_catalog.get(preset_id, system) if preset_id else None
        if system == 'workshop_xwuid':
            self.preset_name.setText('')
            self.preset_note.setText('')
        elif preset is None:
            self.preset_name.setText('未选择' if not preset_id else '预设已失效')
            self.preset_note.setText('评分与权重来自实时评分设置。')
        else:
            self.preset_name.setText(f'{preset.character} · {preset.variant}')
            self.preset_note.setText('评分与权重来自实时评分设置。')

    def _pick_preset(self):
        system = self.scoring.currentData() or 'workshop_xwuid'
        dialog = PresetPickerDialog(self._preset_catalog, system, self)
        if dialog.exec() and dialog.selected is not None:
            self._apply_preset(dialog.selected)

    def _apply_preset(self, preset):
        self._loading = True
        index = self.scoring.findData(preset.system)
        if index >= 0:
            self.scoring.setCurrentIndex(index)
        self._loading = False
        self._scoring_changed(initial=True)
        self.preferences.apply_preset(preset)
        self.target.setValue(float(preset.target))
        self.main_score.setValue(float(preset.main_score))
        self.max_score.setValue(float(preset.max_score))
        self.refresh_presets()
        self.preset_changed.emit()

    def _match_current_role(self):
        system = self.scoring.currentData() or 'workshop_xwuid'
        if system == 'workshop_xwuid':
            return
        snapshot = self._live_snapshot()
        ids = getattr(snapshot, 'selected_ids', ()) if snapshot is not None else ()
        if len(ids) != 2:
            self.preset_note.setText('当前没有可匹配的游戏角色。')
            return
        try:
            name = self._weight_snapshot().scoring_template(*ids)['name']
        except (AttributeError, KeyError, ValueError):
            self.preset_note.setText('当前角色没有可用的工坊模版。')
            return
        character = name.split('-', 1)[0]
        preset = next(
            (item for item in self._preset_catalog.presets(system) if item.character == character),
            None,
        )
        if preset is None:
            self.preset_note.setText(f'没有找到“{character}”的当前体系预设。')
            return
        self._apply_preset(preset)

    def _sync_candidate_visibility(self):
        mode = self.mode.currentData()
        if self._last_result is not None and mode != self._last_result.mode:
            self._last_result = None
        self.candidate_box.setVisible(mode == 'reroll')
        self._sync_action_buttons()

    def _stat_changed(self, combo, spin):
        enabled = combo.currentData() is not None
        spin.setEnabled(enabled)
        if enabled and spin.value() == 0:
            spin.setValue(float(SUBSTAT_TIERS[combo.currentData()][-1]))

    def _record(self):
        selected = []
        for combo, spin in self._rows:
            name = combo.currentData()
            if name is None:
                continue
            if name in selected:
                raise PolicyError('invalid_record', '副词条类型重复')
            selected.append(name)
        subs = tuple(
            SimpleNamespace(stat_name=combo.currentData(), value=float(spin.value()))
            for combo, spin in self._rows
            if combo.currentData() is not None
        )
        return SimpleNamespace(
            cost=self._cost(), main_stats=_main_stats(self._cost()), sub_stats=subs
        )

    def reload_policy_data(self):
        return

    def sync_preset(self):
        system = self.preferences.get(Option.SCORING_SYSTEM, 'workshop_xwuid')
        index = self.scoring.findData(system)
        if index < 0:
            return
        self._loading = True
        self.scoring.setCurrentIndex(index)
        self._loading = False
        self._scoring_changed(initial=True)
        self.target.setValue(float(self.preferences.get(Option.TARGET) or self.target.value()))
        self.main_score.setValue(float(self.preferences.get(Option.MAIN_SCORE) or 0.0))
        self.max_score.setValue(float(self.preferences.get(Option.MAX_SCORE) or 100.0))

    def _candidate(self):
        selected = []
        rows = []
        for combo, spin in self._candidate_rows:
            name = combo.currentData()
            if name is None:
                continue
            if name in selected:
                raise PolicyError('invalid_record', '候选词条类型重复')
            selected.append(name)
            rows.append(SimpleNamespace(stat_name=name, value=float(spin.value())))
        if not rows:
            return None
        if len(rows) != 5:
            raise PolicyError('invalid_record', '候选需要完整的五条副词条')
        return tuple(rows)

    def _candidate_or_none(self):
        try:
            return self._candidate()
        except PolicyError:
            return None

    def _sync_action_buttons(self):
        result = self._last_result
        self.accept_button.setVisible(
            bool(
                result is not None
                and result.mode == 'reroll'
                and result.comparison is not None
                and result.comparison.accept
                and self._candidate_or_none() is not None
            )
        )
        self.joint_button.setVisible(
            bool(result is not None and result.mode == 'joint' and result.decision == 'reroll')
        )

    def capture_candidate(self):
        snapshot = self._live_snapshot()
        if snapshot is None:
            self.feedback.setText('还没有实时识别快照；请先在游戏中打开声骸详情。')
            return
        rows = snapshot.sub_rows
        if len(rows) != 5:
            self.feedback.setText('当前识别结果没有完整五条副词条，无法捕获候选。')
            return
        for (combo, spin), (name, value) in zip(self._candidate_rows, rows):
            index = combo.findData(name)
            if index < 0:
                self.feedback.setText(f'当前识别包含未知词条：{name}')
                return
            combo.setCurrentIndex(index)
            spin.setValue(float(value))
        self.feedback.setText('已捕获当前游戏声骸为候选，请确认后计算。')
        self._sync_action_buttons()

    def _fill_rows(self, rows, source):
        for (combo, spin), row in zip(rows, source):
            index = combo.findData(row.stat_name)
            if index < 0:
                raise PolicyError('invalid_record', f'未知词条：{row.stat_name}')
            combo.setCurrentIndex(index)
            spin.setValue(float(row.value))

    def _clear_candidate(self):
        for combo, spin in self._candidate_rows:
            combo.setCurrentIndex(0)
            spin.setValue(0.0)

    def accept_candidate(self):
        result = self._last_result
        candidate = self._candidate_or_none()
        if (
            result is None
            or result.mode != 'reroll'
            or result.comparison is None
            or not result.comparison.accept
            or candidate is None
        ):
            return
        self._fill_rows(self._rows, candidate)
        self._clear_candidate()
        self.feedback.setText('已接受候选，正在计算下一轮。')
        self.calculate()

    def joint_to_reroll(self):
        result = self._last_result
        if result is None or result.mode != 'joint' or result.decision != 'reroll':
            return
        self._clear_candidate()
        index = self.mode.findData('reroll')
        self.mode.setCurrentIndex(index)
        self.feedback.setText('已转入重抽策略。')
        self.calculate()

    def calculate(self):
        if self._future is not None:
            return
        system = self.scoring.currentData() or 'workshop_xwuid'
        try:
            key = self.picker.key()
            if system == 'workshop_xwuid':
                if key is None:
                    self.feedback.setText('请选择角色和模版。')
                    return
                check_target(self.target.value())
                template = self._weight_snapshot().scoring_template(*key)
            else:
                maximum = 65535.0 if system == 'fixed' else self.max_score.value()
                check_target(self.target.value(), maximum)
                template = None
            record = self._record()
            candidate = self._candidate()
            costs = self.preferences.costs()
            if system == 'workshop_xwuid':
                payload = build_payload(
                    template,
                    record,
                    target=self.target.value(),
                    mode=self.mode.currentData() or 'auto',
                    costs=costs,
                    candidate=candidate,
                )
            else:
                fixed = system == 'fixed'
                weights = self.preferences.policy_weights(system, fixed=fixed)
                self.preferences.set(Option.MAIN_SCORE, float(self.main_score.value()))
                self.preferences.set(Option.MAX_SCORE, float(self.max_score.value()))
                payload = build_payload(
                    None,
                    record,
                    target=self.target.value(),
                    mode=self.mode.currentData() or 'auto',
                    costs=costs,
                    scoring=system,
                    weights=None if fixed else weights,
                    fixed_weights=weights if fixed else None,
                    main_score=self.main_score.value(),
                    max_score=self.max_score.value(),
                    candidate=candidate,
                )
        except PolicyError as error:
            self.feedback.setText(error.message)
            return
        except (KeyError, ValueError) as error:
            self.feedback.setText('输入无效：' + str(error))
            return
        self.feedback.setText('策略计算中…')
        self.result_label.show()
        self.result.show()
        self._last_result = None
        self._sync_action_buttons()
        self.calculate_button.setEnabled(False)
        self._future = self._pool.submit(self._solve, payload)
        self._timer.start()

    def _poll(self):
        if self._future is None or not self._future.done():
            return
        future, self._future = self._future, None
        self._timer.stop()
        self.calculate_button.setEnabled(True)
        try:
            result = future.result()
        except BaseException as error:
            message = getattr(error, 'message', str(error))
            self.feedback.setText('策略不可用：' + message)
            return
        self.feedback.setText('')
        self._last_result = result
        self._sync_action_buttons()
        self.result.setPlainText(self._format(result))

    @staticmethod
    def _format(result):
        decision = {
            'upgrade': '继续强化',
            'abandon': '放弃，重新找胚子',
            'keep': '已达标，保留',
            'reroll': '重抽',
        }.get(result.decision, result.decision)
        label = SYSTEMS.get(result.scoring, {}).get('label', result.scoring)
        lines = [
            f'评分体系：{label}',
            f'建议：{decision}',
            f'当前分：{result.current_score:.2f} / 目标内部分 {result.target_internal}',
            f'副词条分：{result.sub_score:.2f}',
        ]
        if result.lock_types:
            lines.append('锁法：' + '、'.join(result.lock_types))
        elif result.mode in ('reroll', 'joint'):
            lines.append('锁法：不锁（全部重抽）')
        if result.resources is not None:
            resources = result.resources
            lines.append(
                f'期望资源：{resources.echoes:.1f} 胚子 / {resources.tuners:.1f} 调谐器 / '
                f'{resources.tubes:.1f} 密音筒 / {resources.frequencies:.1f} 频整器'
            )
            lines.append(f'加权期望：{resources.weighted:.1f}')
            lines.append(f'单次达标率：{format_probability(resources.attempt_probability)}')
        if result.current_probability is not None:
            lines.append(f'当前状态达标率：{format_probability(result.current_probability)}')
        for lock in result.locks[:5]:
            locks = '、'.join(lock.types) or '不锁'
            lines.append(
                f'备选锁法：{locks} · 期望 {lock.expected:.1f} · '
                f'成功率 {format_probability(lock.probability)}'
            )
        if result.comparison is not None:
            comparison = result.comparison
            lines.append(
                '候选：'
                + ('接受' if comparison.accept else '保留原声骸')
                + f'（{comparison.current_cost:.1f} → {comparison.candidate_cost:.1f}）'
            )
        if result.note:
            lines.append('说明：' + result.note)
        return '\n'.join(lines)

    def _row_state(self, rows):
        return [{'name': combo.currentData(), 'value': float(spin.value())} for combo, spin in rows]

    def _save_state(self):
        state = {
            'scoring': self.scoring.currentData() or 'workshop_xwuid',
            'mode': self.mode.currentData() or 'auto',
            'cost': self._cost(),
            'rows': self._row_state(self._rows),
            'candidate': self._row_state(self._candidate_rows),
            'target': self.target.value(),
            'main_score': self.main_score.value(),
            'max_score': self.max_score.value(),
        }
        try:
            self._state_store.save(state)
        except OSError:
            pass

    def _restore_state(self):
        state = self._state_store.load()
        if state is None:
            return
        scoring = state['scoring']
        if scoring in ('workshop_xwuid', 'linear', 'fixed', 'wuwa_echo_tool', 'mc_boost_assistant'):
            self.scoring.blockSignals(True)
            self.scoring.setCurrentIndex(max(0, self.scoring.findData(scoring)))
            self.scoring.blockSignals(False)
        self._scoring_changed(initial=True)
        self.target.setRange(
            0.0,
            50.0 if scoring == 'workshop_xwuid' else (65535.0 if scoring == 'fixed' else 655.35),
        )
        self.cost.setCurrentIndex(max(0, self.cost.findData(state['cost'])))
        self.target.setValue(state['target'])
        self.main_score.setValue(state['main_score'])
        self.max_score.setValue(state['max_score'])
        self.mode.setCurrentIndex(max(0, self.mode.findData(state['mode'])))
        for rows, saved in (
            (self._rows, state['rows']),
            (self._candidate_rows, state['candidate']),
        ):
            for (combo, spin), row in zip(rows, saved):
                name = row['name']
                combo.setCurrentIndex(combo.findData(name) if name is not None else 0)
                spin.setValue(row['value'])
        self._cost_changed()
        self._sync_candidate_visibility()
        self.feedback.setText('已恢复上次测试状态。')

    def shutdown(self):
        self._save_state()
        self._timer.stop()
        self._future = None
        self._pool.shutdown(wait=False, cancel_futures=True)

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)
