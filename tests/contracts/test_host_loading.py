# 校验宿主薄入口、重复发现及卸载保留稳定身份。
import importlib
import json
from shutil import copytree
import sys
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4


class HostLoadingContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PySide6.QtWidgets import QApplication
            from ok.core.task_manager import TaskManager
        except ImportError as error:
            raise unittest.SkipTest('Requires the optional OKWW host dependency group') from error
        cls.app = QApplication.instance() or QApplication([])
        cls.manager_type = TaskManager
        cls.entries = Path(__file__).resolve().parents[2] / 'plugin' / 'okww'

    def manager(self):
        manager = self.manager_type.__new__(self.manager_type)
        manager.task_executor = SimpleNamespace(
            scene=None,
            paused=True,
            onetime_tasks=[],
            trigger_tasks=[],
            trigger_task_index=-1,
            remove_onetime_task=Mock(),
            _wake_executor=Mock(),
        )
        manager.task_executor.get_all_tasks = lambda: (
            manager.task_executor.onetime_tasks + manager.task_executor.trigger_tasks
        )
        manager.scene = None
        manager.debug = False
        manager.task_map = {}
        manager.task_errors = {}
        manager.imported_scripts = {}
        manager.defer_plugin_validation = True
        manager._update_debug_watched_files = Mock()
        manager.app = None
        return manager

    def test_manifest_loads_and_opens_shared_workbench(self):
        from dataclasses import replace

        from PySide6.QtWidgets import QWidget
        from shiboken6 import isValid
        from ww_echo_workshop import __version__
        from ww_echo_workshop.adapters.okww.lifecycle import host_owns_imported_pages
        from ww_echo_workshop.adapters.okww import settings_task
        from ww_echo_workshop.application.compute_jobs import ComputeJobs
        from ww_echo_workshop.application.inventory_intake import IntakeStart
        from ww_echo_workshop.application.matching.inventory import (
            InventoryViewState,
            MatchInventorySession,
        )
        from ok import og
        from ok.core import script_packager
        from ok.util.config import Config
        from tests.calculation.test_quick_quality import record_fixture
        from tests.native_runtime import ENGINE

        manager = self.manager()
        engine = ENGINE if ENGINE.is_file() else None
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            import_root = directory / 'ok_import'
            plugin_root = import_root / 'ww-echo-workshop'
            copytree(self.entries, plugin_root, ignore=lambda *_: ['__pycache__'])
            manifest_file = plugin_root / 'manifest.json'
            manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
            manifest['version'] = __version__
            manifest_file.write_text(json.dumps(manifest), encoding='utf-8')
            configs = directory / 'configs'
            configs.mkdir()
            config_file = configs / 'WWEchoWorkshopSettingsTask.json'
            config_file.write_text(json.dumps({'CaptureHotkeysJSON': '{'}), encoding='utf-8')
            parent = QWidget()
            host_page = None
            card = None
            with (
                patch.object(
                    script_packager, 'get_ok_import_folder', return_value=str(import_root)
                ),
                patch.object(Config, 'config_folder', str(configs)),
                patch('ww_echo_workshop.resources.weight_updates.WeightUpdateService.start'),
                patch('ww_echo_workshop.resources.sonata.SonataUpdateService.start'),
                patch('ww_echo_workshop.resources.effects.SonataEffectsUpdateService.start'),
                patch(
                    'ww_echo_workshop.application.runtime.ComputeJobs',
                    side_effect=lambda: ComputeJobs(engine),
                ),
            ):
                try:
                    manager.load_imported_tasks()
                    manager.load_imported_tasks()
                    tasks = manager.imported_scripts['ww-echo-workshop']['tasks']
                    self.assertCountEqual(
                        [cls for _, cls in manifest['tasks']],
                        [type(task).__name__ for task in tasks],
                    )
                    self.assertEqual(3, len(manager.task_map))
                    settings = next(
                        task
                        for task in tasks
                        if isinstance(task, settings_task.WWEchoWorkshopSettingsTask)
                    )
                    runtime = settings.runtime
                    self.assertIsNotNone(runtime)
                    first = runtime.inventory.create_profile('合成库存甲')
                    second = runtime.inventory.create_profile('合成库存乙')
                    settings.config['InventoryProfileID'] = first
                    record = replace(record_fixture(), profile_id=first)
                    if engine is not None:
                        runtime.repository.add(record)
                    if host_owns_imported_pages():
                        settings.run()
                    else:
                        from ok.ui.qt.tasks.TaskCard import TaskCard

                        with (
                            patch.object(og, 'app', SimpleNamespace(tr=str)),
                            patch.object(
                                og,
                                'executor',
                                SimpleNamespace(waiting_for_task=Mock(return_value=None)),
                            ),
                        ):
                            card = TaskCard(settings, onetime=True)
                            card._echo_assistant_button.click()
                    self.app.processEvents()
                    self.assertIsNotNone(settings.assistant_window)
                    self.assertIs(runtime, settings.assistant_window.panel.runtime)
                    settings.assistant_window.close()
                    self.assertIsNone(settings.assistant_window)
                    if card is not None:
                        settings.open_inventory(card)
                        legacy = settings.inventory_window
                        self.assertEqual(first, legacy.page.profile_box.currentData())
                        self.assertNotIn('_task', vars(legacy))
                        legacy._echo_home_button.click()
                        self.assertIsNone(settings.inventory_window)
                        self.assertIs(runtime, settings.assistant_window.panel.runtime)
                        settings.assistant_window.close()
                    if host_owns_imported_pages():
                        from ok.ui.qt.plugins.imported_tabs import ImportedPluginPage

                        host_page = ImportedPluginPage(settings)
                        page = host_page.page
                        page_parent = host_page.view
                    else:
                        page_parent = parent
                        page = settings.create_import_page(page_parent)
                    self.assertIs(page_parent, page.parentWidget())
                    self.assertNotIn('task', vars(page))
                    self.assertEqual(
                        '{', json.loads(config_file.read_text('utf-8'))['CaptureHotkeysJSON']
                    )
                    self.assertIsNone(runtime.shortcuts)
                    self.assertIn('快捷键配置待修正', settings.shortcut_message)
                    self.assertEqual(
                        configs / 'plugin_data' / 'ww-echo-workshop', runtime.base_data_dir
                    )
                    for page_name in ('inventory', 'comparison', 'allocation'):
                        self.assertIsNotNone(page._activate(page_name))
                    inventory = page._page('inventory')
                    self.assertNotIn('task', vars(inventory))
                    self.assertNotIn('runtime', vars(inventory))
                    self.assertTrue(inventory.navigation.open_inventory())
                    self.assertIs(inventory, page.stack.currentWidget())
                    page._match_inventory_session = MatchInventorySession(
                        first, frozenset(), InventoryViewState(first)
                    )
                    try:
                        inventory.profile_box.setCurrentIndex(
                            inventory.profile_box.findData(second)
                        )
                        self.assertEqual(first, inventory.profile_box.currentData())
                        self.assertEqual(first, settings.config['InventoryProfileID'])
                        self.assertIn('正在选择候选声骸', page.feedback.text())
                        with self.assertRaisesRegex(ValueError, '正在选择候选声骸'):
                            page._open_profile_route('comparison', second)
                    finally:
                        page._match_inventory_session = None
                    self.assertIs(page, settings.create_import_page(page_parent))
                    settings.apply_shortcuts('')
                    self.assertEqual('', settings.shortcut_message)
                    with (
                        patch.object(settings, '_capture_ready', return_value=False),
                        patch.object(settings, 'start_scan') as start_scan,
                        patch.object(settings, 'arm_capture') as arm_capture,
                    ):
                        intake = inventory.services.intake
                        self.assertIs(IntakeStart.NEED_SHORTCUT, intake.start(first))
                        intake.set_mode('automatic')
                        self.assertIs(IntakeStart.DISCONNECTED, intake.start(first))
                        self.assertTrue(intake.idle)
                        start_scan.assert_not_called()
                        arm_capture.assert_not_called()
                        intake.set_mode('manual')
                    if engine is not None:
                        self._verify_native_inventory_page(inventory, runtime, record)
                        self._verify_native_matching_page(page, settings, record)
                        self._verify_native_allocation_page(page, settings, record)
                    self._verify_resource_settings(page, settings, native=engine is not None)
                    self._verify_workbench_settings(page, settings, first, second, directory)
                    self._verify_live_settings(page, settings, record, native=engine is not None)
                    hud_timer = page._owned_pages['workspace'].hud.timer
                    page.show_settings()
                    manager.unload_task(settings)
                    self.assertTrue(runtime._closed)
                    self.assertTrue(page._stopped)
                    self._wait_for(lambda: not isValid(hud_timer) or not hud_timer.isActive())
                finally:
                    for task in tuple(manager.task_map):
                        manager.unload_task(task)
                    if host_page is not None:
                        host_page.dispose()
                        host_page.close()
                    if card is not None:
                        card.close()
                        card.deleteLater()
                    parent.close()
                    self.app.processEvents()
                    if str(plugin_root) in sys.path:
                        sys.path.remove(str(plugin_root))
        print('HOST_WORKBENCH_OK: manifest, three tasks, shared Qt pages and runtime shutdown')

    def _wait_for(self, predicate):
        limit = time.monotonic() + 5.0
        while not predicate() and time.monotonic() < limit:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(predicate())

    def _verify_workbench_settings(self, panel, settings, first, second, directory):
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        runtime = settings.runtime
        settings.config['CaptureHotkeysJSON'] = '{'
        settings._restore_shortcuts()
        settings.open_shortcuts(None)
        self.assertIn('快捷键配置待修正', panel.shortcuts.info.text())
        self.assertIn('合成库存甲', panel.shortcuts.info.text())
        with patch.object(settings, '_configure_shortcuts') as configure:
            panel.shortcuts.fields['capture'].setText('Ctrl+K')
            panel.shortcuts.apply()
            self.assertIn('Ctrl+K', settings.config['CaptureHotkeysJSON'])
            configure.assert_called_once_with(settings.config['CaptureHotkeysJSON'])
            panel.shortcuts.fields['capture'].clear()
            panel.shortcuts.apply()
        self.assertEqual('', settings.shortcut_message)
        self.assertEqual('stopped', runtime.capture.status.state)
        self.assertIsNone(runtime.shortcuts)

        panel.open_settings_section('profiles')
        self._wait_for(lambda: not panel.profile_mutation_error())
        panel.profile_manage_box.setCurrentIndex(panel.profile_manage_box.findData(second))
        panel.profile_rename_edit.setText('合成库存乙（改名）')
        panel.rename_profile()
        self.assertEqual('合成库存乙（改名）', runtime.inventory.profiles()[second])
        self.assertEqual(first, settings.config['InventoryProfileID'])
        path = directory / 'portable-profile.zip'
        with patch.object(QFileDialog, 'getSaveFileName', return_value=(str(path), 'zip')):
            panel.export_managed_profile()
        self._wait_for(lambda: runtime.backups.status.state == 'completed')
        panel.poll_profile_jobs()
        self.assertTrue(path.is_file())
        with (
            patch.object(QFileDialog, 'getOpenFileName', return_value=(str(path), 'zip')),
            patch.object(QMessageBox, 'question', return_value=QMessageBox.No) as confirm,
        ):
            panel.import_managed_profile()
            self._wait_for(lambda: not runtime.backups.status.busy)
            panel.poll_profile_jobs()
            confirm.assert_called_once()
        self.assertEqual('profile-preview', runtime.backups.status.kind)
        self.assertEqual(first, settings.config['InventoryProfileID'])

        calls = []
        with (
            patch.object(QMessageBox, 'question', return_value=QMessageBox.No) as confirm,
            patch.object(runtime, 'stop_intake', side_effect=lambda: calls.append('stop')),
            patch.object(
                runtime.screenshot_cleanup,
                'request_all',
                side_effect=lambda: calls.append('cleanup') or True,
            ),
        ):
            panel.screenshot_settings.clear()
            self.assertEqual([], calls)
            confirm.return_value = QMessageBox.Yes
            panel.screenshot_settings.clear()
            self.assertEqual(['stop', 'cleanup'], calls)

        self._verify_repair_settings(panel, settings)
        print(
            'HOST_SETTINGS_OK: rename, export, cancelled import, explicit shortcuts, cleanup and repair'
        )

    def _verify_repair_settings(self, panel, settings):
        from PySide6.QtWidgets import QMessageBox

        runtime = settings.runtime
        settings.open_control_repair(None)
        repair = panel.repair
        self.assertIs(repair.service.compute, runtime.compute)
        self.assertIsNone(settings.control_repair_window)
        marker = runtime.workspaces.control / 'preferences-recovery.json'
        marker.parent.mkdir(exist_ok=True)
        damaged = b'{'
        marker.write_bytes(damaged)
        config_before = Path(settings.config.config_file).read_bytes()
        revision_before = runtime.inventory.revision
        repair.kinds.setCurrentIndex(repair.kinds.findData('preferences'))
        repair.inspect()

        def repair_done():
            repair.poll()
            return repair.future is None

        self._wait_for(repair_done)
        self.assertIsNotNone(repair.preview)
        self.assertEqual(damaged, marker.read_bytes())
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
            with patch.object(
                runtime, 'backups', SimpleNamespace(status=SimpleNamespace(busy=True))
            ):
                repair.apply()
                self.assertIn('先完成或取消', repair.status_label.text())
                self.assertIsNone(repair.future)
                self.assertEqual(damaged, marker.read_bytes())
            repair.apply()
        self._wait_for(repair_done)
        result = repair.service.status.result
        self.assertIsNotNone(result)
        self.assertEqual(damaged, (Path(result.archive) / 'original.bin').read_bytes())
        self.assertIsNone(json.loads(marker.read_bytes())['pending'])
        self.assertEqual(config_before, Path(settings.config.config_file).read_bytes())
        self.assertEqual(revision_before, runtime.inventory.revision)
        with (
            patch.object(settings, 'embedded_panel', None),
            patch.object(settings, 'runtime', None),
        ):
            # The startup recovery fallback must also work before a runtime exists.
            settings.open_control_repair(panel)
            standalone = settings.control_repair_window
            self.assertTrue(standalone.service.accepting)
            self.assertIsNot(standalone.service, repair.service)
            standalone.close()
            self.assertFalse(standalone.service.accepting)
            self.assertIsNone(settings.control_repair_window)

    def _verify_live_settings(self, panel, settings, record, *, native):
        from dataclasses import replace

        from ww_echo_workshop.core.policy_presets import PolicyPreset

        runtime = settings.runtime
        page = panel._activate('workspace')
        live, test, hud = page.live_card, page.test_card, page.hud
        for widget in (page, live, test, hud, hud.window):
            self.assertNotIn('task', vars(widget))
            self.assertNotIn('runtime', vars(widget))

        config = settings.config
        key = ('1304', 'default')
        live.picker.reload(runtime.weights.active, key)
        live.apply_selection()
        self.assertEqual(key, (config['TemplateCharacterID'], config['TemplateModalID']))
        self.assertEqual(runtime.weights.active.template(*key)['name'], config['角色评分模板'])
        live.hud_follow_check.setChecked(False)
        hud.window._last_layout = ('bag', (0, 0, 2560, 1440))
        live.hud_follow_check.setChecked(True)
        self.assertTrue(config['策略悬浮窗自动定位'])
        self.assertIsNone(hud.window._last_layout)
        hud.window.font_size.setValue(23)
        self.assertEqual(23, config['策略悬浮窗字号'])
        hud.window._save_position()
        self.assertEqual(
            {'x': hud.window.x(), 'y': hud.window.y()}, json.loads(config['策略悬浮窗位置'])
        )
        focus_before = config['InventoryProfileID']
        revision_before = runtime.inventory.revision
        live.resource_boxes[0].setValue(2.125)
        self.assertEqual('[2.125,1.0,0.0,1000.0]', config['强化策略资源权重'])
        preset = PolicyPreset(
            'fixed', '合成角色', '通用', 'fixture', (3, 2, 1, *([0] * 10)), 4.4, 0.0, 100.0
        )
        with patch.object(test, '_solve', wraps=runtime.policy.solve) as solve:
            live._apply_preset(preset)
            self.assertEqual(('fixed', 4.4), (test.scoring.currentData(), test.target.value()))
            test._apply_preset(replace(preset, target=3.1))
            self.assertEqual(('fixed', 3.1), (live.scoring.currentData(), live.target.value()))
            self.assertEqual('fixture', config['策略评分预设'])
            self.assertEqual('合成角色 · 通用', config['策略评分预设名称'])
            runtime.live_snapshots.capture(SimpleNamespace(sub_rows=record.sub_stats))
            test.capture_candidate()
            self.assertEqual(5, len(test._candidate()))
            test._fill_rows(test._rows, record.sub_stats)
            test.mode.setCurrentIndex(test.mode.findData('reroll'))
            solve.assert_not_called()
            if native:
                test.calculate()
                self._wait_for(lambda: test._last_result is not None)
                solve.assert_called_once()
                payload = solve.call_args.args[0]
                self.assertEqual(
                    ('fixed', 'reroll', 3),
                    (payload['scoring'], payload['mode'], payload['target_internal']),
                )
                self.assertEqual(list(preset.weights), payload['fixed_weights'])
                self.assertEqual([2.125, 1.0, 0.0, 1000.0], payload['costs'])
                self.assertEqual(payload['sub'], payload['candidate'])
                self.assertEqual('keep', test._last_result.decision)
        self.assertEqual(focus_before, config['InventoryProfileID'])
        self.assertEqual(revision_before, runtime.inventory.revision)
        print('HOST_LIVE_SETTINGS_OK: role, shared preset, explicit policy, HUD position and font')

    def _verify_resource_settings(self, panel, settings, *, native):
        runtime = settings.runtime
        data = panel._page('workspace').data_card
        with (
            patch.object(runtime.resources, 'request', return_value=True) as check,
            patch.object(runtime.resources, 'cancel') as cancel,
            patch.object(runtime.resources, 'set_auto') as auto,
            patch.object(runtime.updates, 'request_reload', return_value=True) as reload_local,
        ):
            data._install_controls()
            weights = panel.ensure_weight_settings()
            self.assertIs(weights, data.weights)
            self.assertIs(data.controls_area, weights.resource_controls.parentWidget())
            self.assertNotIn('task', vars(data))
            self.assertNotIn('task', vars(weights))
            panel.open_settings_section('weights')
            editor = weights.ensure_custom_editor()
            self.assertNotIn('task', vars(editor))
            check.assert_not_called()
            reload_local.assert_not_called()
            previous = settings.config.get('AutoWeightUpdates', True)
            weights.auto.setChecked(not previous)
            self.assertEqual(not previous, settings.config['AutoWeightUpdates'])
            auto.assert_called_once_with(not previous)
            weights.auto.setChecked(previous)
            weights.allow_removals.setChecked(True)
            weights.check_button.click()
            check.assert_called_once_with(allow_removals=True)
            weights.cancel_update()
            cancel.assert_called_once()
            weights.reload_local()
            reload_local.assert_called_once()
            if native:
                key = ('1304', 'default')
                panel._page('comparison').navigation.edit_weights(key)
                self.assertEqual(key, editor.selection)

                def projected():
                    editor._poll_projection()
                    return editor._ready

                self._wait_for(projected)
                with patch.object(runtime.updates, 'request_custom', return_value=True) as save:
                    editor.save()
                    self.assertTrue(editor.pending)
                    self.assertEqual(key, save.call_args.args)
                    self.assertEqual(
                        runtime.weights.active.version, save.call_args.kwargs['expected_version']
                    )
        print(
            'HOST_RESOURCE_SETTINGS_OK: shared controls, explicit check, cancel, reload, custom projection'
        )

    def _verify_native_inventory_page(self, inventory, runtime, record):
        from dataclasses import replace

        from ww_echo_workshop.ui.inventory.evidence import EvidenceDialog
        from tests.contracts.test_capture_persistence import synthetic_png

        self._wait_for(lambda: inventory._reader.status.state == 'ready')
        self._wait_for(lambda: inventory.table.proxy.projection_ready and len(inventory._rows) == 1)
        inventory.table.selectRow(0)
        self.assertEqual(record, inventory.selected()[0])
        revision = runtime.inventory.revision
        inventory.edit_selected()
        self.assertTrue(inventory.lifecycle_draft())
        self.assertIn(record.name, inventory.services.review_choices(record).names)
        editor = inventory._editor
        editor.result_record = replace(record, note='取消的核对草稿')
        inventory._finish_edit()
        editor.saveRequested.emit()  # A late quality reply must not save a cancelled draft.
        self.assertFalse(inventory.lifecycle_draft())
        self.assertEqual(record, runtime.inventory.get(record.item_id))
        inventory.quick_score_selected()
        dialog = inventory._quick_score_dialog
        self.assertNotIn('task', vars(dialog))
        self.assertNotIn('runtime', vars(dialog))
        self.assertIsNone(dialog.result)
        dialog.picker.reload(dialog.snapshot, ('1304', 'default'))
        self.assertIsNone(dialog.result)
        dialog.start_score()
        self._wait_for(lambda: dialog.result is not None)
        self.assertEqual(29.96, dialog.result.score.current_score)
        self.assertEqual(record, runtime.inventory.get(record.item_id))
        self.assertEqual(revision, runtime.inventory.revision)
        dialog.close()
        png = runtime.evidence.put(record.item_id, synthetic_png())
        viewer = EvidenceDialog(png, inventory.services.evidence, inventory)
        self._wait_for(lambda: not viewer.image.pixmap().isNull())
        viewer.close()
        print('HOST_INVENTORY_NATIVE_OK: review=1, cancelled_draft, quick_score=29.96, evidence=1')

    def _verify_native_matching_page(self, panel, settings, record):
        matching = panel._activate('comparison')
        inventory = panel._page('inventory')
        allocation_settings = settings.config.get('AllocationSettingsJSON', '')
        self.assertNotIn('task', vars(matching))
        self.assertNotIn('runtime', vars(matching))
        key = ('1304', 'default')
        index = matching.participants.findData(key)
        self.assertGreaterEqual(index, 0)
        matching.choose_participant(index)
        self._wait_for(lambda: matching._fact_key is not None and matching._future is None)
        self.assertIsNone(matching._result_key)
        self.assertFalse(matching.matrix_model.records)

        matching.library_select_button.click()
        self.assertIs(inventory, panel.stack.currentWidget())
        self._wait_for(lambda: inventory.table.proxy.projection_ready)
        inventory.table.selectRow(0)
        inventory.match_apply_selected.click()
        self._wait_for(lambda: panel.stack.currentWidget() is matching)
        self.assertEqual(frozenset({record.item_id}), matching.item_picker.selected_ids())
        self.assertIsNone(matching._result_key)
        matching.start_comparison()
        self._wait_for(lambda: matching._result_key is not None)
        self.assertEqual(
            (record.item_id,), tuple(row.item_id for row in matching.matrix_model.records)
        )
        score = matching.matrix_model.scores[0].score
        self.assertEqual((29.96, 'B'), (score.current_score, score.rating.label))
        saved = json.loads(settings.config['ComparisonSettingsJSON'])
        self.assertEqual(
            [record.item_id],
            saved['profiles'][record.profile_id]['calculation']['candidate_item_ids'],
        )
        self.assertEqual(allocation_settings, settings.config.get('AllocationSettingsJSON', ''))
        print('HOST_MATCH_NATIVE_OK: explicit candidate, explicit calculation, 29.96/B')

    def _verify_native_allocation_page(self, panel, settings, record):
        from dataclasses import replace

        from ww_echo_workshop.core.allocation.models import SlotRule
        from tests.calculation.helpers import role

        runtime = settings.runtime
        matching = settings.config['ComparisonSettingsJSON']
        clones = tuple(
            replace(record, item_id=str(uuid4()), name=f'离线声骸 {index}') for index in range(5)
        )
        for clone in clones:
            runtime.repository.add(clone)
        selected = frozenset(row.item_id for row in (record, *clones[:4]))
        page = panel._activate('allocation')
        self.assertNotIn('task', vars(page))
        self.assertNotIn('runtime', vars(page))
        page.roles = [role('1304', slots=(SlotRule(1),) * 5, cost_budget=5, set_requirements=())]
        page.set_candidate_scope(selected)
        page.changed()
        self._wait_for(lambda: page._io_future is None)
        page.refresh_saved()
        self._wait_for(lambda: page._io_future is None)
        page.generate_from_precheck()
        self._wait_for(lambda: page._result is not None and page._future is None)
        self.assertEqual(selected, frozenset(page._result.roles[0].item_ids))
        revision = runtime.inventory.revision
        page.backup_loadout()
        self._wait_for(lambda: page.saved.count() > 0 and page._io_future is None)
        self.assertEqual(revision, runtime.inventory.revision)
        page.save_loadout()
        self._wait_for(lambda: page._current_loadouts.current and page._io_future is None)
        self.assertEqual(selected, frozenset(page._current_loadouts.current[0]['item_ids']))
        page._show_history()
        history = page.saved_workspace
        self._wait_for(lambda: page._io_future is None)
        history.project(force=True)
        row = history.selection()
        if not history.current_detail() and page._io_future is None:
            # The imported host test surface can be kept hidden offscreen.
            page.open_saved_role(
                row['plan_id'], row['character_id'], row['modal_id'], history.view_token
            )
        self._wait_for(lambda: history.detail_context() is not None and page._io_future is None)
        history.project(force=True)
        frozen_revision = runtime.inventory.revision
        # Row zero is the non-selectable role panel; row one is the first echo.
        history.detail_cards.cards.selectRow(1)
        self.assertIn(history.selected_item_id(), selected)
        history._replace()
        self._wait_for(lambda: history._dialog is not None and page._io_future is None)
        replacement = clones[-1].item_id
        unchanged = set(selected) - {history.selected_item_id()}
        self.assertEqual(4, len(unchanged))
        history._dialog.preview_requested.emit(replacement)
        self._wait_for(lambda: history._draft_key() in history._drafts and page._io_future is None)
        draft = history._drafts[history._draft_key()]
        self.assertEqual(unchanged | {replacement}, set(draft.result.roles[0].item_ids))
        self.assertEqual(frozen_revision, runtime.inventory.revision)
        self.assertEqual(matching, settings.config['ComparisonSettingsJSON'])
        print('HOST_ALLOCATION_NATIVE_OK: 5 items, saved, history, one slot preview only')

    def test_real_host_discovery_is_canonical_and_keeps_old_config_identity(self):
        from ok import BaseTask
        from ww_echo_workshop.core.models import EchoRecord
        from ww_echo_workshop.core.compatibility import CompatibilityRule
        from ww_echo_workshop.adapters.okww.private_ocr import PLUGIN_OCR
        from ok.util.config import Config

        entry_names = {
            'ww_echo_workshop_settings.py': 'WWEchoWorkshopSettingsTask',
            'ww_echo_workshop_overlay.py': 'WWEchoWorkshopOverlayTask',
            'ww_echo_workshop_scan.py': 'WWEchoWorkshopScanTask',
        }
        self.assertEqual(set(entry_names), {path.name for path in self.entries.glob('*.py')})
        retained = []
        for _ in range(2):
            manager = self.manager()
            for filename, name in entry_names.items():
                task = manager.find_and_instantiate_class(str(self.entries / filename), BaseTask)
                self.assertIsNotNone(task)
                self.assertEqual(name, type(task).__name__)
                self.assertIsNone(getattr(task, 'runtime', None))
                retained.append(task)
        self.assertIs(
            EchoRecord, importlib.import_module('ww_echo_workshop.core.models').EchoRecord
        )
        self.assertIs(
            CompatibilityRule,
            importlib.import_module('ww_echo_workshop.core.compatibility').CompatibilityRule,
        )
        self.assertIs(
            PLUGIN_OCR,
            importlib.import_module('ww_echo_workshop.adapters.okww.private_ocr').PLUGIN_OCR,
        )
        with tempfile.TemporaryDirectory() as folder, patch.object(Config, 'config_folder', folder):
            retained[0].load_config()
            self.assertEqual(
                'WWEchoWorkshopSettingsTask.json', Path(retained[0].config.config_file).name
            )
            self.assertEqual(
                Path(folder).resolve(), Path(retained[0].config.config_file).resolve().parent
            )
        # Retired icon detection has no API/pixel access; historical model fields still roundtrip.
        from ww_echo_workshop.capture import game_status

        self.assertFalse(hasattr(game_status, 'observe_game_icons'))
        record = EchoRecord(
            str(uuid4()), 1, (), owner='测试角色', game_discarded=True, game_locked=False
        )
        analysis = SimpleNamespace(layout='bag', layout_provisional=False)
        updated = game_status.attach_game_status(record, analysis, (), 100, 100, object())
        self.assertEqual((True, False), (updated.game_discarded, updated.game_locked))
        restored = EchoRecord.from_dict(updated.to_dict())
        self.assertEqual((True, False), (restored.game_discarded, restored.game_locked))
        from ok import Box
        import numpy as np
        from ww_echo_workshop.capture.scan.crop import read_fields, field_regions

        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        viewport = (0, 0, 1280, 720)

        from ww_echo_workshop.adapters.okww.private_ocr import frozen_reader

        def infer(box, pixels, match, scale, threshold, lib):
            self.assertIsInstance(box, Box)
            self.assertEqual('echo-bag-packed', box.name)
            return [Box(2, 2, 5, 5, name='fixture')], []

        reader = frozen_reader(SimpleNamespace(ocr_default_threshold=0.5), infer)
        result = read_fields(frame, viewport, reader, mode='packed')
        first_region = field_regions(frame, viewport)[0]
        self.assertEqual((first_region[0] + 2, first_region[1] + 2), (result[0].x, result[0].y))
        print(
            'HOST_DISCOVERY_OK: real fresh-spec helper loaded 3 stable task names twice; canonical models/private OCR unchanged; old Config class filename; no retired pixels or historical field loss'
        )

    def test_actual_unload_hook_closes_owned_runtime_once(self):
        from ok import BaseTask
        from ww_echo_workshop.adapters.okww.lifecycle import install_unload_cleanup
        from ok.core import task_manager

        manager = self.manager()
        task = manager.find_and_instantiate_class(
            str(self.entries / 'ww_echo_workshop_settings.py'), BaseTask
        )
        stop = threading.Event()
        runtime = SimpleNamespace(
            stop_event=stop, capture=SimpleNamespace(stop=Mock()), close=Mock()
        )
        task.runtime = runtime
        task.disable = Mock()
        manager.task_map = {
            task: [str(self.entries / 'ww_echo_workshop_settings.py'), 'fixture-hash']
        }
        manager.task_executor.onetime_tasks.append(task)
        communicate = SimpleNamespace(task_list_updated=SimpleNamespace(emit=Mock()))
        install_unload_cleanup()
        with patch.object(task_manager, 'communicate', communicate):
            manager.unload_task(task)
            manager.unload_task(task)
        self.assertTrue(stop.is_set())
        runtime.capture.stop.assert_called_once()
        runtime.close.assert_called_once()
        self.assertNotIn(task, manager.task_map)
        self.assertNotIn(task, manager.task_executor.onetime_tasks)
        print(
            'HOST_UNLOAD_OK: host unload cancels then closes owned runtime once; repeated unload idempotent; no game after_init/on_create'
        )
