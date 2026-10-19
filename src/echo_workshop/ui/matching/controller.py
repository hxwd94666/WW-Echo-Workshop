# 管理多角色单件匹配计算和结果。
"""Multi-template matrix and retrieval cards; scoring stays off the Qt thread."""

from concurrent.futures import ThreadPoolExecutor
import json
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot

from echo_workshop.core.compatibility import CompatibilityRule
from echo_workshop.application.scoring import ComparisonCancelled
from echo_workshop.application.operations import OperationCancelled
from echo_workshop.application.pins import prepare_pin_catalog
from echo_workshop.ui.matching.results import ComparisonUX
from echo_workshop.application.matching.feedback import (
    MatchRequirementsError,
    feedback_from_code,
    validate_match_requirements,
)


class _ComparisonPayload(tuple):
    """Keep the seven-field compatibility payload with an immutable catalogue."""

    def __new__(cls, values, catalog, claims=None):
        result = super().__new__(cls, values)
        result.catalog = tuple(catalog)
        result.claims = dict(claims or {})
        return result


class _ResultSignals(QObject):
    completed = Signal(int, object, str)


class _StopToken:
    def __init__(self, local, runtime):
        self.local, self.runtime = local, runtime

    def is_set(self):
        return self.local.is_set() or self.runtime.stop_event.is_set()


class ComparisonContent(ComparisonUX):
    def lifecycle_busy(self):
        return self._future is not None or self._publication is not None or self._facts_busy

    def lifecycle_draft(self):
        return bool(getattr(self, '_rule_dirty', False))

    def lifecycle_rule_dirty(self):
        return bool(getattr(self, '_rule_dirty', False))

    def request_stop(self):
        self.stop_worker()

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.task, self.runtime = task, task.runtime
        self._pool = None
        self._future = None
        self._cancel = threading.Event()
        self._generation = 0
        self._closing = False
        self._worker_lock = threading.RLock()
        self._stop_lock = threading.Lock()
        self._publication = None
        self._facts_busy = False
        self._loading = True
        self._queued = False
        self._queued_compute = False
        self._worker_kind = None
        self._rules = {}
        self._result_key = None
        self._fact_key = None
        self._seen_defaults_version = self._defaults_version()
        self._match_feedback = None
        self._signals = _ResultSignals(self)
        self._signals.completed.connect(self._completed, Qt.QueuedConnection)
        self.build_comparison_ui()
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh_facts)
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(600)
        self.debounce.timeout.connect(lambda: self.action(self.refresh_match))
        self.publication_timer = QTimer(self)
        self.publication_timer.setInterval(40)
        self.publication_timer.timeout.connect(self._retry_publication)
        self.item_picker.changed.connect(self.changed)
        self.profile_box.currentIndexChanged.connect(self.load_profile)
        selected = self.profile_box.findData(task.config.get('InventoryProfileID', ''))
        self.profile_box.setCurrentIndex(selected)
        self.load_profile()

    def action(self, callback):
        if self.runtime._closed or self.task._destroyed:
            return
        try:
            callback()
        except (ValueError, KeyError, OSError) as error:
            self.status.setText(str(error))

    def preferences(self):
        raw = self.task.config.get('ComparisonSettingsJSON', '')
        if not raw:
            return {'schema_version': 1, 'profiles': {}}
        if len(raw) > 1024 * 1024:
            raise ValueError('比较配置过大，请备份并清理该配置')
        try:
            data = json.loads(raw)
            if (
                not isinstance(data, dict)
                or data.get('schema_version') != 1
                or not isinstance(data.get('profiles'), dict)
            ):
                raise ValueError('比较配置格式不兼容；原配置保留')
            return data
        except json.JSONDecodeError as error:
            raise ValueError('比较配置损坏；原配置保留') from error

    def selections(self):
        return tuple(self.state.roles)

    def filter_options(self):
        return dict(self.state.filters)

    def _preference_data(self):
        profile = self.profile_box.currentData()
        if not profile:
            raise ValueError('请选择本地档案')
        data = self.preferences()
        data['profiles'][profile] = dict(
            selections=self.selections(),
            filters=self.filter_options(),
            rules=[
                dict(character_id=key[0], modal_id=key[1], **rule.to_dict())
                for key, rule in self._rules.items()
            ],
        )
        return data

    def save_preferences(self):
        self.task.config['ComparisonSettingsJSON'] = json.dumps(
            self._preference_data(), ensure_ascii=False, allow_nan=False
        )

    def load_profile(self, *_):
        self._loading = True
        self._cancel.set()
        self._clear_publication()
        self._rules = {}
        self._result_key = self._fact_key = None
        self._match_feedback = None
        self.matrix_model.reset_matrix()
        self.item_picker.reset_profile()
        error = ''
        saved = {}
        try:
            saved = self.preferences()['profiles'].get(self.profile_box.currentData(), {})
            if not isinstance(saved, dict):
                raise ValueError('档案比较配置损坏')
            fallback = (
                self.task.config.get('TemplateCharacterID'),
                self.task.config.get('TemplateModalID'),
            )
            initial = (
                [fallback]
                if all(isinstance(x, str) and x for x in fallback) and fallback[0] != 'default'
                else []
            )
            selections = saved.get('selections', initial)
            if not isinstance(selections, (list, tuple)) or any(
                not isinstance(key, (list, tuple))
                or len(key) != 2
                or not all(isinstance(value, str) for value in key)
                for key in selections
            ):
                raise ValueError('模版选择配置损坏')
            selected = {tuple(key) for key in selections}
            for row in saved.get('rules', []):
                self._rules[(row['character_id'], row['modal_id'])] = CompatibilityRule.from_dict(
                    {key: row[key] for key in ('element', 'sonatas')}
                )
        except (ValueError, KeyError, TypeError) as caught:
            selected = set()
            error = str(caught)
        available = tuple(
            (char, modal) for char, modal, _ in self.runtime.weights.active.options.values()
        )
        self.state.roles = tuple(key for key in available if key in selected) + tuple(
            sorted(selected - set(available))
        )
        self.state.current_role = self.state.roles[0] if self.state.roles else None
        self._template_version = self.runtime.weights.active.version
        filters = saved.get('filters', {}) if isinstance(saved, dict) else {}
        defaults = dict(text='', cost=None, mode='confirmed', fully_tuned=False, max_age_days=0)
        if isinstance(filters, dict):
            defaults.update({key: filters[key] for key in defaults if key in filters})
        age = defaults['max_age_days']
        defaults['max_age_days'] = age if type(age) is int and 0 <= age <= 3650 else 0
        defaults['fully_tuned'] = defaults['fully_tuned'] is True
        self.state.filters = defaults
        self._loading = False
        self.status.setText(error or '勾选角色 / 固定模态，刷新比较；换角色只重算库存，不执行 OCR')
        self.sync_participants()
        self.load_rule()
        self.publish_ux_results()
        self.show_details()
        self._schedule_comparison_refresh()  # Catalogue only in the embedded page.

    def _defaults_snapshot(self):
        provider = getattr(self.runtime, 'role_defaults', None)
        return provider.active if provider is not None else None

    def _defaults_version(self):
        return getattr(self._defaults_snapshot(), 'version', '')

    def effective_rule(self, key, defaults=None):
        from echo_workshop.resources.role_defaults import default_rule

        if key in self._rules:
            return self._rules[key]
        saved = getattr(self, '_role_requirements', {}).get(key)
        if saved is not None:
            return CompatibilityRule(sonatas=saved.allowed_sonatas)
        return default_rule(key[0] if key else '', defaults or self._defaults_snapshot())

    def load_rule(self, *_):
        key = self.state.current_role
        rule = self.effective_rule(key)
        character = key[0] if key else ''
        rule = self.runtime.weights.active.compatibility_rule(character, rule)
        self.element_rule.setCurrentIndex(self.element_rule.findData(rule.element))
        known = self.runtime.weights.active.character_elements.get(character, '')
        self.element_rule.setEnabled(not known)
        self.element_rule.setToolTip(
            '角色属性已自动获取：' + known if known else '可明确指定角色属性'
        )
        self.sonata_rule.setText(', '.join(rule.sonatas))

    def changed(self, *_, persist=True):
        if self._loading or self._closing:
            return
        self._match_feedback = None
        self._cancel.set()
        self._clear_publication()
        self._result_key = None
        # 切换/移除角色会同步触发投影；先失效旧结果，避免混用新角色与旧矩阵。
        self.sync_participants()
        self.status.setText('选择或条件已变；请点击计算。')
        if persist:
            self.action(self.save_preferences)
        self.project_result()
        self._schedule_comparison_refresh()

    def _schedule_comparison_refresh(self):
        """Configuration changes refresh candidates, never imply user compute."""
        self._queued_compute = False
        if self._future is not None:
            self._queued = True
            self.debounce.stop()
            return
        self.debounce.start()

    def refresh_match(self):
        with self._worker_lock:
            self._start_comparison(_allow_empty=True, _catalogue_only=True)

    def start_comparison(self, *, _allow_empty=False):
        with self._worker_lock:
            self.debounce.stop()
            self._start_comparison(_allow_empty=_allow_empty)

    def _start_comparison(self, *, _allow_empty=False, _catalogue_only=False):
        if self._closing or self.runtime._closed or self.task._destroyed:
            return
        if self._future is not None:
            if _catalogue_only:
                if self._cancel.is_set():
                    self._queued = True
            else:
                self._queued_compute = True
                self._queued = False
                self._cancel.set()
            return
        selections = self.selections()
        if not selections and not _allow_empty:
            raise ValueError('请先添加至少一个角色与模版。')
        snapshot = self.runtime.weights.active
        if not _catalogue_only:
            for key in selections:
                snapshot.template(*key)
        if selections and not _catalogue_only:
            self.save_preferences()
        filters = self.filter_options()
        selected_ids = self.item_picker.selected_ids()
        defaults = self._defaults_snapshot()
        defaults_version = getattr(defaults, 'version', '')
        rules = {key: self.effective_rule(key, defaults) for key in selections}
        requirements = self.role_requirements() if not _catalogue_only else {}
        self._match_feedback = None
        self._generation += 1
        self._clear_publication()
        self._result_key = None
        generation = self._generation
        profile = self.profile_box.currentData()
        self._cancel = threading.Event()
        stop = _StopToken(self._cancel, self.runtime)
        self._queued = False
        self._queued_compute = False
        self._worker_kind = 'catalogue' if _catalogue_only else 'compute'
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='echo-comparison')
        self.status.setText(
            '正在读取匹配候选…'
            if _catalogue_only
            else f'正在计算 {len(selections)} 个角色模版，后台读取声骸…'
        )
        self.project_result()

        def calculate():
            try:
                revision, records, claims = self.runtime.inventory.inventory_snapshot(
                    profile, cancel_event=stop, include_loadouts=True
                )
                if not _catalogue_only:
                    validate_match_requirements(
                        requirements, records, snapshot, stop, pin_checks=self.runtime.pin_checks
                    )
                scope = self.runtime.inventory_queries.matching_scope(
                    records,
                    selected_ids=selected_ids,
                    **filters,
                    protected_ids=claims,
                    cancel_event=stop,
                )
                catalog, records = scope.catalog, scope.records
                scores = (
                    self.runtime.scores.compare(
                        records,
                        selections,
                        inventory_revision=revision,
                        rules=rules,
                        requirements=requirements,
                        cancel_event=stop,
                        snapshot=snapshot,
                    )
                    if selections and not _catalogue_only
                    else ()
                )
                labels = (
                    tuple(
                        f'{snapshot.template(*key)["name"]}\n{key[0]}/{key[1]}'
                        for key in selections
                    )
                    if not _catalogue_only
                    else ()
                )
                result = _ComparisonPayload(
                    (profile, revision, snapshot, records, selections, scores, labels),
                    catalog,
                    claims,
                )
                result.defaults_version = defaults_version
                result.projections = (
                    self.runtime.match_projections.project(
                        records, selections, scores, cancel_event=stop
                    )
                    if selections and not _catalogue_only
                    else None
                )
                result.fixed_catalog = prepare_pin_catalog(
                    catalog,
                    queries=self.runtime.inventory_queries,
                    usable_only=True,
                    cancel_event=stop,
                )
                result.catalogue_only = _catalogue_only
                result.feedback = feedback_from_code(scope.feedback_code)
                error = ''
            except (ComparisonCancelled, OperationCancelled):
                result, error = None, '比较已取消'
            except MatchRequirementsError as caught:
                result, error = caught, str(caught)
            except Exception as caught:
                message = str(caught)
                if message == 'Expected explicit compatibility rules':
                    message = (
                        '角色适配规则格式不一致；请保存角色设置，更新插件后需完全退出并重启 OKWW。'
                    )
                result, error = None, f'比较失败：{message}'
            try:
                self._signals.completed.emit(generation, result, error)
            except RuntimeError:
                pass  # Qt parent was already removed by the host.

        self._future = self._pool.submit(calculate)
        self.cancel_button.setEnabled(True)

    def cancel_comparison(self):
        self._cancel.set()
        self._clear_publication()
        self._queued = False
        self._queued_compute = False
        self._result_key = None
        self.cancel_button.setEnabled(self._future is not None)
        self.debounce.stop()
        self.status.setText('已请求取消；等待读取/计算检查点，不发布部分矩阵')
        self.project_result()

    @Slot(int, object, str)
    def _completed(self, generation, result, error):
        if generation != self._generation:
            return
        self._future = None
        self._worker_kind = None
        self.cancel_button.setEnabled(False)
        if self._closing or self.runtime._closed or self.task._destroyed:
            self._clear_publication()
            return
        if self._queued_compute:
            self._clear_publication()
            self._queued_compute = False
            self._queued = False
            self.action(self.start_comparison)
            return
        if self._queued:
            self._clear_publication()
            self._queued = False
            self.action(self.refresh_match)
            return
        if error:
            self._clear_publication()
            self._match_feedback = getattr(result, 'feedback', None)
            self.status.setText(error)
            self.project_result()
            return
        if self._cancel.is_set():
            self._clear_publication()
            self.status.setText('比较已取消；旧矩阵仅供历史查看')
            return
        profile, revision, snapshot, records, selections, scores, labels = result
        version = snapshot.version
        defaults_version = getattr(result, 'defaults_version', self._defaults_version())
        current_revision = self.runtime.inventory.try_revision()
        if current_revision is None:
            self._publication = (generation, result, error)
            self.publication_timer.start()
            self.cancel_button.setEnabled(True)
            self.status.setText('库存事务忙；完整计算结果等待复核，旧矩阵仅供历史查看')
            return
        self._clear_publication()
        if (
            profile != self.profile_box.currentData()
            or revision != current_revision
            or version != self.runtime.weights.active.version
            or defaults_version != self._defaults_version()
        ):
            self.status.setText('库存、权重或选择已变；旧计算结果已丢弃')
            self._schedule_comparison_refresh()
            return
        self._match_feedback = getattr(result, 'feedback', None)
        if getattr(result, 'catalogue_only', False):
            # Candidate refresh preserves UUID checks and must never make an
            # empty score payload look like a fresh calculation.
            self.item_picker.reload(result.catalog, fixed_catalog=result.fixed_catalog)
            self._fact_key = (profile, revision, version, defaults_version)
            self._result_key = None
            self.sync_participants()
            self.project_result()
            self.status.setText('候选已更新；请点击计算。')
            return
        current = self.state.selected_identity
        self.item_picker.reload(result.catalog, fixed_catalog=result.fixed_catalog)
        self.matrix_model.claims = dict(getattr(result, 'claims', {}))
        self.matrix_model.reset_matrix(
            records, selections, scores, labels, projections=result.projections
        )
        self.state.selected_identity = (
            current
            if current and self.matrix_model.index_for_identity(*current).isValid()
            else None
        )
        self._result_key = (profile, revision, version, defaults_version)
        self._fact_key = self._result_key
        self.status.setText(
            f'仅已收录：{len(records)} 件 × {len(selections)} 模态；库存修订 {revision}；快照 {version[:12]}；未知适配不作确定推荐'
        )
        self.sync_participants()
        self.publish_ux_results()
        self.show_details()

    def _clear_publication(self):
        self._publication = None
        self.publication_timer.stop()

    def _retry_publication(self):
        if (
            self._closing
            or self.runtime._closed
            or self.task._destroyed
            or self._publication is None
        ):
            self._clear_publication()
            return
        self._completed(*self._publication)

    def selected(self):
        identity = self.state.selected_identity
        return (
            self.matrix_model.entry(self.matrix_model.index_for_identity(*identity))
            if identity
            else (None, None)
        )

    def refresh_facts(self):
        if self.runtime._closed or self.task._destroyed:
            self.close()
            return
        if self._future is not None:
            return
        try:
            defaults_version = self._defaults_version()
            if defaults_version != self._seen_defaults_version:
                self._seen_defaults_version = defaults_version
                if not self._rule_dirty:
                    self.load_rule()
            revision = self.runtime.inventory.try_revision()
            if revision is None:
                self._facts_busy = True
                self.status.setText('库存事务忙；旧矩阵仅供历史查看，标记前须复核')
                return
            key = (
                self.profile_box.currentData(),
                revision,
                self.runtime.weights.active.version,
                self._defaults_version(),
            )
            if self.runtime.weights.active.version != getattr(self, '_template_version', None):
                self.reload_templates()
            if self._fact_key is not None and key != self._fact_key:
                self._fact_key = key
                self._result_key = None
                self.status.setText('库存或评分资料已变；请点击计算。')
                self._cancel.set()
                self._clear_publication()
                self.project_result()
                self._schedule_comparison_refresh()
            elif self._facts_busy and self._result_key == key:
                self.status.setText(
                    f'库存复核完成；仅已收录 {len(self.matrix_model.records)} 件；修订 {revision}；快照 {key[2][:12]}'
                )
            self._facts_busy = False
        except ValueError:
            if self.runtime._closed:
                self.close()
            else:
                raise

    def reload_templates(self):
        self._template_version = self.runtime.weights.active.version
        self.sync_participants()

    def showEvent(self, event):
        with self._worker_lock:
            if not self.runtime._closed and not self.task._destroyed:
                self._closing = False
                self.timer.start()
        super().showEvent(event)

    def stop_worker(self):
        # Executor unload must wait without a Qt event pump or touching controls.
        with self._stop_lock:
            with self._worker_lock:
                self._closing = True
                self._cancel.set()
                self._queued = False
                self._queued_compute = False
                self._publication = None
                self._generation += 1
                pool = self._pool
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
            with self._worker_lock:
                self._pool = None
                self._future = None
                self._worker_kind = None

    def closeEvent(self, event):
        self.timer.stop()
        self.debounce.stop()
        self._clear_publication()
        self.stop_worker()
        self.cancel_button.setEnabled(False)
        super().closeEvent(event)
