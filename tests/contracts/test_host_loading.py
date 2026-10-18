# 校验宿主薄入口、重复发现及卸载保留稳定身份。
import importlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
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
        manager.task_executor = SimpleNamespace(scene=None, onetime_tasks=[], trigger_tasks=[])
        manager.app = None
        return manager

    def test_real_host_discovery_is_canonical_and_keeps_old_config_identity(self):
        from ok import BaseTask
        from echo_workshop.core.models import EchoRecord
        from echo_workshop.core.compatibility import CompatibilityRule
        from echo_workshop.capture.scan.pipeline import OCR_LOCK
        from ok.util.config import Config

        entry_names = {
            'echo_workshop_settings.py': 'EchoWorkshopSettingsTask',
            'echo_workshop_overlay.py': 'EchoWorkshopOverlayTask',
            'echo_workshop_scan.py': 'EchoWorkshopScanTask',
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
        self.assertIs(EchoRecord, importlib.import_module('echo_workshop.core.models').EchoRecord)
        self.assertIs(
            CompatibilityRule,
            importlib.import_module('echo_workshop.core.compatibility').CompatibilityRule,
        )
        self.assertIs(
            OCR_LOCK, importlib.import_module('echo_workshop.capture.scan.pipeline').OCR_LOCK
        )
        with tempfile.TemporaryDirectory() as folder, patch.object(Config, 'config_folder', folder):
            retained[0].load_config()
            self.assertEqual(
                'EchoWorkshopSettingsTask.json', Path(retained[0].config.config_file).name
            )
            self.assertEqual(
                Path(folder).resolve(), Path(retained[0].config.config_file).resolve().parent
            )
        # Retired icon detection has no API/pixel access; historical model fields still roundtrip.
        from echo_workshop.capture import game_status

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
        from echo_workshop.capture.scan.crop import read_fields, field_regions

        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        viewport = (0, 0, 1280, 720)

        def read(*, frame, box):
            self.assertIsInstance(box, Box)
            self.assertEqual('echo-bag-packed', box.name)
            return [Box(2, 2, 5, 5, name='fixture')]

        result = read_fields(frame, viewport, read, mode='packed', box_factory=Box)
        first_region = field_regions(frame, viewport)[0]
        self.assertEqual((first_region[0] + 2, first_region[1] + 2), (result[0].x, result[0].y))
        print(
            'HOST_DISCOVERY_OK: real fresh-spec helper loaded 3 stable task names twice; canonical models/OCR lock unchanged; old Config class filename; no retired pixels or historical field loss'
        )

    def test_actual_unload_hook_closes_owned_runtime_once(self):
        from ok import BaseTask
        from echo_workshop.adapters.okww.lifecycle import install_unload_cleanup
        from ok.core import task_manager

        manager = self.manager()
        task = manager.find_and_instantiate_class(
            str(self.entries / 'echo_workshop_settings.py'), BaseTask
        )
        stop = threading.Event()
        runtime = SimpleNamespace(
            stop_event=stop, capture=SimpleNamespace(stop=Mock()), close=Mock()
        )
        task.runtime = runtime
        task.disable = Mock()
        manager.task_map = {task: [str(self.entries / 'echo_workshop_settings.py'), 'fixture-hash']}
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
            'HOST_UNLOAD_OK: actual TaskManager unload wrapper cancels then closes owned runtime once; repeated unload idempotent; no game after_init/on_create'
        )
