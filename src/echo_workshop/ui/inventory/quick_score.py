# 展示角色选择和一对一评分结果。
"""One Echo, one explicit role/modal and one read-only asynchronous score."""

from concurrent.futures import ThreadPoolExecutor
import threading
from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QBoxLayout,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    PrimaryPushButton,
    PushButton,
    isDarkTheme,
    qconfig,
)

from echo_workshop.ui.inventory.cards import EchoCardWidget
from echo_workshop.application.quick_score import QuickScoreStale, refresh_record, score_one
from echo_workshop.application.record_quality import quality_for
from echo_workshop.core.cancellation import OperationCancelled
from echo_workshop.ui.widgets.roles import RolePicker
from echo_workshop.ui.widgets.rating import RatingValueLabel, RatingStandardsButton
from echo_workshop.application.scoring import ComparisonCancelled
from echo_workshop.ui.widgets.layout import fit_dialog


class _ScoreSignals(QObject):
    completed = Signal(int, object, object)


class QuickScoreDialog(QDialog):
    _basis = '与匹配单件评分相同，主词条按 +25 口径；分数不是伤害提升。只读，不更改库存或角色设置。'

    def __init__(self, task, record, parent=None, *, quality, inventory_revision):
        super().__init__(parent)
        self.task, self.runtime, self.record = task, task.runtime, record
        self.snapshot = self.runtime.weights.active
        self._preferences = task.config.get('ComparisonSettingsJSON', '')
        self.result = None
        self._result_notice = ''
        self._quality = quality_for(record, quality)
        self._seen_revision = inventory_revision
        self._future = self._pool = None
        self._quality_future = None
        self._quality_revision = self._quality_pending_revision = None
        self._quality_failed_revision = None
        self._quality_cancel = threading.Event()
        self._deferred_score = None
        self._cancel = threading.Event()
        self._generation = 0
        self._running_generation = None
        self._closing = False
        self._record_available = quality.usable
        self._signals = _ScoreSignals(self)
        self._signals.completed.connect(self._completed, Qt.QueuedConnection)
        self.setObjectName('EchoQuickScoreDialog')
        self.setWindowTitle('声骸快速评分')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(10)
        self.heading = BodyLabel('快速评分', self)
        font = self.heading.font()
        font.setPointSize(15)
        font.setBold(True)
        self.heading.setFont(font)
        layout.addWidget(self.heading)
        area = self.area = QScrollArea(self)
        area.setWidgetResizable(True)
        area.viewport().installEventFilter(self)
        area.setFrameShape(QScrollArea.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget(area)
        body.setObjectName('quickScoreBody')
        content = self.content_layout = QBoxLayout(QBoxLayout.LeftToRight, body)
        content.setContentsMargins(0, 2, 0, 2)
        content.setSpacing(18)
        left = QWidget(body)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        left_layout.addWidget(CaptionLabel('当前声骸', left))
        self.card = EchoCardWidget(left)
        self.card.set_record(record, quality=quality)
        self.card.setFixedWidth(260)
        self.card.avatar_cache = self.runtime.avatars
        left_layout.addWidget(self.card)
        content.addWidget(left, 0, Qt.AlignTop)
        shell = QWidget(body)
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(8)
        shell_layout.addWidget(CaptionLabel('评分设置', shell))
        right = self.controls_panel = QFrame(shell)
        right.setObjectName('quickScorePanel')
        right.setMinimumWidth(0)
        right.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        form = QVBoxLayout(right)
        form.setContentsMargins(16, 14, 16, 14)
        form.setSpacing(12)
        self.picker = RolePicker(self.snapshot, right)
        self.picker.setMinimumWidth(0)
        self.picker.hint.hide()
        self.picker.characters.lineEdit().setPlaceholderText('搜索角色（支持拼音）')
        self.picker.characters.setPlaceholderText('选择角色')
        self.picker.modals.setPlaceholderText('先选择角色')
        role_label = self.picker.layout().labelForField(self.picker.characters)
        if role_label is not None:
            role_label.setText('角色')
        for combo in (self.picker.characters, self.picker.modals):
            combo.setMinimumWidth(0)
            combo.setMinimumContentsLength(7)
            combo.setSizeAdjustPolicy(
                type(combo).SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
        form.addWidget(self.picker)
        separator = QFrame(right)
        separator.setObjectName('quickScoreSeparator')
        separator.setFixedHeight(1)
        form.addWidget(separator)
        form.addWidget(CaptionLabel('词条评分', right))
        self.result_row = QHBoxLayout()
        self.score_label = RatingValueLabel('', right)
        self.result_row.addWidget(self.score_label)
        self.rating_standards = RatingStandardsButton(right)
        self.result_row.addWidget(self.rating_standards)
        self.result_row.addStretch()
        form.addLayout(self.result_row)
        self.score_label.hide()
        self.score_label.setToolTip('')
        self.result_placeholder = BodyLabel('尚未评分', right)
        self.result_placeholder.setObjectName('quickScorePlaceholder')
        form.addWidget(self.result_placeholder)
        controls = QHBoxLayout()
        self.score_button = PrimaryPushButton('评分', right)
        self.score_button.setToolTip(self._basis)
        self.score_button.clicked.connect(self.start_score)
        controls.addWidget(self.score_button, 1)
        self.cancel_button = PushButton('取消评分', right)
        self.cancel_button.clicked.connect(self.cancel_score)
        controls.addWidget(self.cancel_button)
        form.addLayout(controls)
        self.summary = BodyLabel('', right)
        self.summary.hide()
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addWidget(self.summary)
        shell_layout.addWidget(right)
        content.addWidget(shell, 1, Qt.AlignTop)
        area.setWidget(body)
        layout.addWidget(area, 1)
        buttons = self.close_buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        buttons.button(QDialogButtonBox.Close).setText('关闭')
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.picker.changed.connect(self._picker_changed)
        self.timer = QTimer(self)
        self.timer.setInterval(350)
        self.timer.timeout.connect(self.refresh_context)
        self.timer.start()
        self.quality_timer = QTimer(self)
        self.quality_timer.setInterval(20)
        self.quality_timer.timeout.connect(self._poll_quality)
        qconfig.themeChanged.connect(self.update_theme)
        qconfig.themeChangedFinished.connect(self.update_theme)
        fit_dialog(self, 720, max(440, self.card.height() + 180))
        self.update_theme()
        self._update_controls()
        self._reflow()
        self.refresh_context()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'content_layout'):
            self._reflow()

    def eventFilter(self, watched, event):
        if (
            hasattr(self, 'content_layout')
            and watched is self.area.viewport()
            and event.type() == QEvent.Resize
        ):
            self._reflow()
        return super().eventFilter(watched, event)

    def _reflow(self):
        width = self.area.viewport().width()
        narrow = width < 590
        self.content_layout.setDirection(
            QBoxLayout.TopToBottom if narrow else QBoxLayout.LeftToRight
        )
        self.card.setFixedWidth(max(180, width - 4) if narrow else 260)

    def _show_message(self, text):
        self.summary.setText(text)
        self.summary.setVisible(bool(text))

    def _picker_changed(self):
        searching = (
            bool(self.picker.characters.currentText()) and self.picker.characters.currentIndex() < 0
        )
        self.mark_stale('请从搜索结果选择角色。' if searching else '')

    def update_theme(self, *_):
        dark = isDarkTheme()
        foreground = QColor('#f3f3f3' if dark else '#202020')
        background = QColor('#202020' if dark else '#fafafa')
        palette = self.palette()
        for role in (QPalette.Window, QPalette.Base):
            palette.setColor(role, background)
        for role in (QPalette.WindowText, QPalette.Text):
            palette.setColor(role, foreground)
        for surface in (self, self.area, self.area.viewport(), self.area.widget(), self.picker):
            surface.setPalette(palette)
        self.setAutoFillBackground(True)
        self.area.widget().setAutoFillBackground(True)
        control = '#2d2d2d' if dark else '#ffffff'
        border = '#494949' if dark else '#d6d6d6'
        muted = '#a0a0a0' if dark else '#767676'
        self.setStyleSheet(f"""
            #EchoQuickScoreDialog {{ background:{background.name()}; color:{foreground.name()}; }}
            #EchoQuickScoreDialog QWidget#quickScoreBody {{ background:{background.name()}; }}
            #EchoQuickScoreDialog QLabel {{ color:{foreground.name()}; background:transparent; }}
            #EchoQuickScoreDialog QFrame#quickScorePanel {{ background:{control}; border:1px solid {border}; border-radius:10px; }}
            #EchoQuickScoreDialog QFrame#quickScoreSeparator {{ background:{border}; border:none; }}
            #EchoQuickScoreDialog QLabel#quickScorePlaceholder {{ color:{muted}; }}
            #EchoQuickScoreDialog QScrollArea {{ border:none; background:transparent; }}
            #EchoQuickScoreDialog QComboBox {{ color:{foreground.name()}; background:{control};
                border:1px solid {border}; border-radius:4px; padding:3px 6px; min-height:22px; }}
            #EchoQuickScoreDialog QDialogButtonBox QPushButton {{ color:{foreground.name()}; background:{control};
                border:1px solid {border}; border-radius:4px; padding:5px 10px; }}
        """)
        self.score_label.update_rating_colour()

    def _update_controls(self):
        quality_busy = (
            self._quality_future is not None
            or self._quality_pending_revision is not None
            or self._deferred_score is not None
        )
        busy = self._future is not None or quality_busy
        box = self.picker.characters
        exact = (
            box.currentIndex() >= 0
            and box.currentText() == box.itemText(box.currentIndex())
            and box.currentData() in self.snapshot.templates
        )
        self.picker.modals.setEnabled(exact)
        self.picker.modals.setPlaceholderText('请选择模版' if exact else '先选择角色')
        self.score_button.setEnabled(
            not busy
            and (self._record_available or self._quality_failed_revision is not None)
            and not self._closing
            and self.picker.key() is not None
        )
        self.cancel_button.setVisible(busy)
        self.score_button.setText(
            '正在核对'
            if quality_busy
            else '正在评分'
            if busy
            else '重新核对'
            if self._quality_failed_revision is not None
            else '评分'
        )
        self.result_placeholder.setVisible(self.result is None and not busy)

    def mark_stale(self, message):
        self._cancel.set()
        self._generation += 1
        self.result = None
        self._result_notice = ''
        self._deferred_score = None
        self.score_label.clear()
        self.score_label.hide()
        self.score_label.setToolTip('')
        self.rating_standards.set_entries(())
        self._show_message(message)
        self._update_controls()

    def refresh_context(self):
        if self._closing or self.runtime._closed or self.task._destroyed:
            self.stop_worker()
            return
        active = self.runtime.weights.active
        if active.version != self.snapshot.version:
            key = self.picker.key()
            self.snapshot = active
            self.picker.reload(active, key)
            self.mark_stale('评分权重已更新，请点击评分重新计算。')
        preferences = self.task.config.get('ComparisonSettingsJSON', '')
        if preferences != self._preferences:
            self._preferences = preferences
            if self.result is not None or self._future is not None:
                self.mark_stale('角色适配设置已变化，请点击评分重新计算。')
        revision = self.runtime.inventory.try_revision()
        if revision is None:
            return
        if revision != self._seen_revision:
            if revision != self._quality_failed_revision:
                self._queue_quality(revision)
            return
        self._flush_score()

    def _work_pool(self):
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='echo-quick-score')
        return self._pool

    def _queue_quality(self, revision):
        if self._closing or revision in (self._quality_revision, self._quality_pending_revision):
            return
        self._quality_pending_revision = revision
        self._quality_cancel.set()
        self._quality_failed_revision = None
        self._show_message('库存已变化，正在后台核对当前记录；不会自动评分。')
        self._submit_quality()
        self._update_controls()

    def _submit_quality(self):
        if self._closing or self._quality_future is not None:
            return
        revision = self._quality_pending_revision
        if revision is None:
            return
        self._quality_revision, self._quality_pending_revision = revision, None
        self._quality_cancel = threading.Event()
        self._quality_future = self._work_pool().submit(
            refresh_record,
            self.runtime,
            self.record,
            self._quality,
            revision=revision,
            cancel_event=self._quality_cancel,
        )
        self.quality_timer.start()

    def _poll_quality(self):
        if self._closing:
            self.quality_timer.stop()
            return
        future = self._quality_future
        if future is None or not future.done():
            return
        observed = self.runtime.inventory.try_revision()
        if observed is None:
            return
        revision = self._quality_revision
        self._quality_future = self._quality_revision = None
        self.quality_timer.stop()
        if observed != revision:
            self._quality_pending_revision = observed
        if self._quality_pending_revision is not None:
            self._submit_quality()
            return  # Never publish an old reply or queue more than one latest draft.
        try:
            if self._quality_cancel.is_set():
                raise OperationCancelled('记录核对已取消。')
            state = future.result()  # Already finished; no wait on the GUI thread.
            if state.revision != revision:
                raise QuickScoreStale('库存核对返回修订不一致，请重新核对。')
        except Exception as error:
            self._record_available = False
            self._quality_failed_revision = revision
            self.mark_stale(
                '记录核对已取消，请点击重新核对。'
                if isinstance(error, (OperationCancelled, ComparisonCancelled))
                else '记录核对未完成，请点击重新核对或查看宿主日志。'
            )
            return
        self._seen_revision = revision
        self._quality = state.quality
        if state.quality is None:
            self._record_available = False
            self.card.clear()
            self.mark_stale('声骸已删除，请关闭后重新选择。')
            return
        current = state.quality.record
        changed = current != self.record or not self._record_available
        self.record, self._record_available = current, state.quality.usable
        self.card.set_record(current, quality=state.quality)
        if changed:
            self.mark_stale(
                '声骸记录已变化，请点击评分重新计算。'
                if self._record_available
                else '声骸尚未完整核对，请先核对并编辑。'
            )
        else:
            self._show_message(self._result_notice if self.result is not None else '')
        self._flush_score()
        self._update_controls()

    def _flush_score(self):
        if self._deferred_score is None or self._quality_future is not None:
            return
        if self._quality_pending_revision is not None or self._quality_failed_revision is not None:
            return
        observed = self.runtime.inventory.try_revision()
        if observed is None:
            return
        if observed != self._seen_revision:
            self._queue_quality(observed)
            return
        pending, self._deferred_score = self._deferred_score, None
        self._present_score(*pending)

    def start_score(self):
        self.refresh_context()
        if self._closing or self._future is not None or self._quality_future is not None:
            return
        if self._deferred_score is not None:
            return
        if self._quality_failed_revision is not None:
            revision = self.runtime.inventory.try_revision()
            if revision is not None:
                self._queue_quality(revision)
            return
        if not self._record_available or self._quality_pending_revision is not None:
            return
        key = self.picker.key()
        if key is None:
            self._show_message('请选择角色和模版。')
            return
        self.result = None
        self.score_label.clear()
        self.score_label.hide()
        self.score_label.setToolTip('')
        self.rating_standards.set_entries(())
        self._generation += 1
        generation = self._generation
        self._running_generation = generation
        self._cancel = threading.Event()
        record, snapshot, cancel = self.record, self.snapshot, self._cancel
        self._show_message('正在评分，可取消。')
        self._future = self._work_pool().submit(
            score_one,
            self.runtime,
            record,
            key,
            snapshot=snapshot,
            preferences=self._preferences,
            cancel_event=cancel,
        )
        self._future.add_done_callback(lambda future: self._deliver(generation, future))
        self._update_controls()

    def _deliver(self, generation, future):
        try:
            result, error = future.result(), None
        except Exception as failure:
            result, error = None, failure
        if self._closing:
            return
        try:
            self._signals.completed.emit(generation, result, error)
        except RuntimeError:
            pass  # The owning inventory may already be disposed.

    @Slot(int, object, object)
    def _completed(self, generation, result, error):
        if generation != self._running_generation or self._closing:
            return
        self._future = None
        self._running_generation = None
        self._deferred_score = generation, result, error
        self.refresh_context()
        self._update_controls()

    def _present_score(self, generation, result, error):
        if self._closing or self.runtime._closed or generation != self._generation:
            self._update_controls()
            return
        if error is not None:
            self.result = None
            self._show_message(
                str(error)
                if isinstance(error, QuickScoreStale)
                else '评分已取消，请点击评分重新计算。'
                if isinstance(error, ComparisonCancelled)
                else '评分未完成，请重新选择角色和模版或查看宿主日志。'
            )
            self._update_controls()
            return
        notice, result = result.notice, result.comparison
        key = self.picker.key()
        try:
            if not self._record_available or self._quality is None:
                raise QuickScoreStale('声骸尚未完整核对，请先核对并编辑。')
            quality_for(self.record, self._quality)
            if (
                result.character_id,
                result.modal_id,
            ) != key or result.weight_version != self.snapshot.version:
                raise QuickScoreStale('评分条件已变化，请点击评分重新计算。')
        except (ValueError, KeyError, ComparisonCancelled) as failure:
            self.mark_stale(str(failure))
            return
        self.result = result
        self._result_notice = notice
        rating = getattr(result.score, 'rating', None)
        self.score_label.set_value(result.score.current_score, rating)
        self.rating_standards.set_entries(
            (
                (
                    self.picker.characters.currentText() + ' · ' + self.picker.modals.currentText(),
                    rating,
                    result.weight_version,
                ),
            )
        )
        self.score_label.show()
        self._show_message(notice)
        self.score_label.setToolTip('')
        self._update_controls()

    def cancel_score(self):
        if self._quality_future is not None:
            self._quality_cancel.set()
            self._quality_failed_revision = self._quality_revision
            self._quality_pending_revision = None
        self.mark_stale('评分已取消，请点击评分重新计算。')

    def stop_worker(self):
        self._closing = True
        self._cancel.set()
        self._quality_cancel.set()
        self._quality_pending_revision = None
        self._deferred_score = None
        self._generation += 1
        pool = self._pool
        self._pool = None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def done(self, result):
        self.timer.stop()
        self.quality_timer.stop()
        self.stop_worker()
        super().done(result)

    def closeEvent(self, event):
        self.timer.stop()
        self.quality_timer.stop()
        self.stop_worker()
        super().closeEvent(event)
