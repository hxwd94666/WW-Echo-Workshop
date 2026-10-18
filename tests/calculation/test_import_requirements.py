# 验证档案导入前的原生角色要求复核接线。
import json
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

from echo_workshop.application.compute_jobs import ComputeJobs
from echo_workshop.application.requirements_check import RequirementsCheckService
from echo_workshop.storage.profile_transfer import _validate_imported_requirements

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / 'build/native/release/echo-workshop-compute.exe'


def settings_with(requirements):
    row = {
        'character_id': '1304',
        'modal_id': 'default',
        'costs': [4, 3, 3, 1, 1],
        'main_names': [[], [], [], [], []],
        'allowed_sonatas': [],
        'set_requirements': [],
    }
    body = {
        'schema_version': 1,
        'profiles': {'profile-1': {'calculation': {'role_requirements': requirements or [row]}}},
    }
    return (('ComparisonSettingsJSON', json.dumps(body, ensure_ascii=False)),)


class FakeChecker:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def start(self, specs, cancel_event=None):
        self.calls.append(specs)
        future = Future()
        future.set_result(None)
        return SimpleNamespace(future=future, keys=tuple(specs))

    def response(self, request):
        return tuple((key, self.error) for key in request.keys)


class ImportRequirementBoundary(unittest.TestCase):
    def test_valid_settings_pass_and_models_are_sent_once(self):
        checker = FakeChecker()
        _validate_imported_requirements(settings_with([]), checker, None)
        self.assertEqual(1, len(checker.calls))
        self.assertEqual((('1304', 'default'),), tuple(checker.calls[0]))

    def test_native_rejection_blocks_the_import(self):
        checker = FakeChecker('五槽位 COST 合计 20，不得超过 12。')
        with self.assertRaises(ValueError) as error:
            _validate_imported_requirements(settings_with([]), checker, None)
        self.assertIn('角色要求无效', str(error.exception))

    def test_no_requirements_or_checker_skips_the_round_trip(self):
        checker = FakeChecker()
        _validate_imported_requirements((('AllocationSettingsJSON', '{}'),), checker, None)
        self.assertEqual([], checker.calls)
        _validate_imported_requirements(settings_with([]), None, None)

    @unittest.skipUnless(ENGINE.is_file(), '需要先运行 tools/compute.py build 生成原生计算程序')
    def test_real_service_accepts_a_valid_row(self):
        jobs = ComputeJobs(ENGINE)
        self.addCleanup(jobs.wait_closed)
        self.addCleanup(jobs.close)
        _validate_imported_requirements(
            settings_with([]), RequirementsCheckService(compute=jobs), None
        )
