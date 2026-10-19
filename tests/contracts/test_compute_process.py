# 验证子进程故障、超时、双队列与有界关闭不阻塞提交线程。
from pathlib import Path
from concurrent.futures import CancelledError
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from echo_workshop.application.compute_jobs import ComputeJobs
from echo_workshop.application.compute_protocol import ComputeFailure, ComputeWorker, encode_frame

ENGINE = Path(__file__).resolve().parents[2] / 'build/native/release/echo-workshop-compute.exe'


class TestComputeProcess(unittest.TestCase):
    def test_missing_capability_does_not_send_a_request_or_restart_a_healthy_worker(self):
        with ComputeWorker(ENGINE) as worker:
            worker.call('ping', {})
            pid = worker.pid
            worker.capabilities = ('ping',)
            with patch.object(worker, '_send', wraps=worker._send) as send:
                with self.assertRaises(ComputeFailure) as error:
                    worker.call('loadout_diff', {})
            self.assertEqual('unknown_operation', error.exception.kind)
            send.assert_not_called()
            self.assertEqual({}, worker.call('ping', {}))
            self.assertEqual(pid, worker.pid)

    def test_queued_short_deadline_expires_while_long_search_is_still_running(self):
        jobs = ComputeJobs(ENGINE, fault_tests=True)
        try:
            long = jobs.submit('fault_hang', dict, lane='search', timeout=2)
            short = jobs.submit('ping', dict, lane='search', timeout=0.1)
            started = time.monotonic()
            with self.assertRaises(ComputeFailure) as error:
                short.result(timeout=0.5)
            self.assertEqual('timeout', error.exception.kind)
            self.assertLess(time.monotonic() - started, 0.3)
            self.assertFalse(long.done())
        finally:
            jobs.close()
            self.assertTrue(jobs.wait_closed())

    def test_cancel_during_stuck_preparation_finishes_without_waiting_for_builder(self):
        jobs = ComputeJobs(ENGINE)
        release, entered, cancel = threading.Event(), threading.Event(), threading.Event()
        try:

            def stuck():
                entered.set()
                release.wait()
                return {}

            work = jobs.submit('ping', stuck, timeout=10, cancel_event=cancel)
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            cancel.set()
            with self.assertRaises(ComputeFailure) as error:
                work.result(timeout=0.5)
            self.assertEqual('cancelled', error.exception.kind)
            self.assertLess(time.monotonic() - started, 0.25)
        finally:
            jobs.close()
            self.assertTrue(jobs.wait_closed())
            release.set()

    def test_cancelled_queued_futures_do_not_leak_queue_slots(self):
        jobs = ComputeJobs(ENGINE)
        release, entered = threading.Event(), threading.Event()
        try:

            def wait():
                entered.set()
                release.wait()
                return {}

            jobs.submit('ping', wait)
            self.assertTrue(entered.wait(1))
            queued = [jobs.submit('ping', dict) for _ in range(7)]
            for future in queued:
                self.assertTrue(future.cancel())
            extra = jobs.submit('ping', dict)
            self.assertTrue(extra.cancel())
            with self.assertRaises(ComputeFailure) as error:
                jobs.submit('ping', dict)
            self.assertEqual('queue_limit', error.exception.kind)
            release.set()
            deadline = time.monotonic() + 2
            while not jobs._queues['interactive'].empty() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual({}, jobs.submit('ping', dict).result(timeout=2))
        finally:
            release.set()
            jobs.close()
            self.assertTrue(jobs.wait_closed())

    def test_preparation_hang_has_deadline_and_does_not_block_other_lane(self):
        jobs, release, entered = (
            ComputeJobs(ENGINE, prepare_grace=0.1),
            threading.Event(),
            threading.Event(),
        )
        try:

            def stuck():
                entered.set()
                release.wait()
                return {}

            first = jobs.submit('ping', stuck, timeout=0.15)
            self.assertTrue(entered.wait(1))
            queued = jobs.submit('ping', dict, timeout=1)
            with self.assertRaises(ComputeFailure) as error:
                first.result(timeout=1)
            self.assertEqual('timeout', error.exception.kind)
            # The queued ticket is drained once the preparation grace expires.
            with self.assertRaises(ComputeFailure) as error:
                queued.result(timeout=1)
            self.assertEqual('restart_limit', error.exception.kind)
            with self.assertRaises(ComputeFailure) as error:
                jobs.submit('ping', dict)
            self.assertEqual('restart_limit', error.exception.kind)
            self.assertEqual({}, jobs.submit('ping', dict, lane='search').result(timeout=2))
        finally:
            jobs.close()
            self.assertTrue(jobs.wait_closed())
            release.set()

    def test_cancel_during_preparation_keeps_lane_healthy(self):
        jobs, release, entered, cancel = (
            ComputeJobs(ENGINE),
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        try:

            def blocked():
                entered.set()
                release.wait()
                return {}

            work = jobs.submit('ping', blocked, timeout=10, cancel_event=cancel)
            self.assertTrue(entered.wait(1))
            cancel.set()
            with self.assertRaises(ComputeFailure) as error:
                work.result(timeout=1)
            self.assertEqual('cancelled', error.exception.kind)
            release.set()
            self.assertEqual({}, jobs.submit('ping', dict).result(timeout=3))
        finally:
            release.set()
            jobs.close()
            self.assertTrue(jobs.wait_closed())

    def test_stuck_preparation_does_not_prevent_interpreter_exit(self):
        source = ENGINE.parents[3] / 'src'
        code = (
            'import sys,threading; '
            f'sys.path.insert(0,{str(source)!r}); '
            'from echo_workshop.application.compute_jobs import ComputeJobs; '
            'entered=threading.Event(); release=threading.Event(); '
            'jobs=ComputeJobs(); '
            'jobs.submit("ping",lambda:(entered.set(),release.wait(),{})[-1]); '
            'assert entered.wait(1); jobs.close(); assert jobs.wait_closed(); '
            'print("PREPARATION_EXIT_OK")'
        )
        result = subprocess.run(
            [sys.executable, '-B', '-c', code], capture_output=True, text=True, timeout=3
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('PREPARATION_EXIT_OK', result.stdout.strip())

    def test_engine_fault_recreation_has_a_finite_limit(self):
        jobs = ComputeJobs(ENGINE, fault_tests=True)
        try:
            for _ in range(2):
                with self.assertRaises(ComputeFailure):
                    jobs.submit('fault_abort', dict).result(timeout=2)
            with self.assertRaises(ComputeFailure) as error:
                jobs.submit('ping', dict)
            self.assertEqual('restart_limit', error.exception.kind)
        finally:
            jobs.close()
            self.assertTrue(jobs.wait_closed())

    def test_typed_request_errors_keep_transport_while_faults_close_it(self):
        with ComputeWorker(ENGINE) as worker:
            worker.call('ping', {})
            pid = worker.pid
            with self.assertRaises(ComputeFailure) as error:
                worker.call('unknown-operation', {})
            self.assertEqual('unknown_operation', error.exception.kind)
            self.assertEqual({}, worker.call('ping', {}))
            self.assertEqual(pid, worker.pid)
        with ComputeWorker(ENGINE, fault_tests=True) as worker:
            with self.assertRaises(ComputeFailure):
                worker.call('fault_frame_limit', {})
            self.assertIsNone(worker.pid)

    def test_handshake_roundtrip_and_job_assignment(self):
        with ComputeWorker(ENGINE) as worker:
            self.assertEqual({'text': '声骸'}, worker.call('ping', {'text': '声骸'}))
            self.assertIsNotNone(worker._job.handle)
            self.assertIn('score_batch', worker.capabilities)

    def test_child_panic_and_abort_are_contained(self):
        for operation, kind in [('fault_panic', 'panic'), ('fault_abort', 'unexpected_eof')]:
            with (
                self.subTest(operation=operation),
                ComputeWorker(ENGINE, fault_tests=True) as worker,
            ):
                with self.assertRaises(ComputeFailure) as error:
                    worker.call(operation, {})
                self.assertEqual(kind, error.exception.kind)
        with ComputeWorker(ENGINE) as worker:
            self.assertEqual({'alive': True}, worker.call('ping', {'alive': True}))

    def test_hang_timeout_and_search_do_not_block_interactive_lane(self):
        jobs = ComputeJobs(ENGINE, fault_tests=True)
        try:
            started = time.monotonic()
            search = jobs.submit('fault_hang', dict, lane='search', timeout=0.4)
            quick = jobs.submit('ping', lambda: {'responsive': True})
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertEqual({'responsive': True}, quick.result(timeout=2))
            with self.assertRaises(ComputeFailure) as error:
                search.result(timeout=2)
            self.assertEqual('timeout', error.exception.kind)
            started = time.monotonic()
            jobs.close()
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertTrue(jobs.wait_closed())
        finally:
            jobs.close()
            jobs.wait_closed()

    def test_cancel_hung_child_is_bounded(self):
        jobs = ComputeJobs(ENGINE, fault_tests=True)
        cancel = threading.Event()
        try:
            work = jobs.submit('fault_hang', dict, timeout=5, cancel_event=cancel)
            time.sleep(0.1)
            started = time.monotonic()
            cancel.set()
            with self.assertRaises(ComputeFailure) as error:
                work.result(timeout=2)
            self.assertEqual('cancelled', error.exception.kind)
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            jobs.close()
            jobs.wait_closed()

    def test_oversized_input_and_missing_engine_fail_without_fallback(self):
        with self.assertRaises(ComputeFailure) as error:
            encode_frame({'data': 'x' * (4 * 1024 * 1024)})
        self.assertEqual('frame_limit', error.exception.kind)
        with ComputeWorker(ENGINE.with_name('missing.exe')) as worker:
            with self.assertRaises(ComputeFailure) as error:
                worker.call('ping', {})
            self.assertEqual('engine_missing', error.exception.kind)

    def test_close_during_start_and_repeated_jobs_leave_no_children(self):
        for _ in range(50):
            jobs = ComputeJobs(ENGINE)
            work = jobs.submit('ping', lambda: {'alive': True})
            jobs.close()
            self.assertTrue(jobs.wait_closed())
            try:
                work.result(timeout=2)
            except (ComputeFailure, CancelledError):
                pass
            self.assertTrue(all(worker.pid is None for worker in jobs._workers.values()))

    def test_preparation_and_queue_time_count_toward_deadline(self):
        jobs = ComputeJobs(ENGINE)
        try:

            def slow_payload():
                time.sleep(0.1)
                return {}

            work = jobs.submit('ping', slow_payload, timeout=0.05)
            with self.assertRaises(ComputeFailure) as error:
                work.result(timeout=2)
            self.assertEqual('timeout', error.exception.kind)
        finally:
            jobs.close()
            jobs.wait_closed()

    def test_oversized_response_wrong_id_and_memory_limit_are_contained(self):
        for operation, kind in [
            ('fault_frame_limit', 'frame_limit'),
            ('fault_wrong_id', 'request_identity'),
            ('fault_memory', 'memory_limit'),
        ]:
            with (
                self.subTest(operation=operation),
                ComputeWorker(ENGINE, fault_tests=True) as worker,
            ):
                with self.assertRaises(ComputeFailure) as error:
                    worker.call(operation, {})
                self.assertEqual(kind, error.exception.kind)

    def test_stderr_is_drained_and_bounded(self):
        with ComputeWorker(ENGINE, fault_tests=True) as worker:
            self.assertEqual({'drained': True}, worker.call('fault_stderr', {}))
            time.sleep(0.05)
            self.assertEqual(64 * 1024, len(worker._stderr))
            self.assertEqual({}, worker.call('ping', {}))

    def test_pending_queue_has_a_hard_limit(self):
        jobs = ComputeJobs(ENGINE)
        release = threading.Event()
        try:

            def wait_payload():
                release.wait(2)
                return {}

            jobs.submit('ping', wait_payload)
            for _ in range(7):
                jobs.submit('ping', dict)
            with self.assertRaises(ComputeFailure) as error:
                jobs.submit('ping', dict)
            self.assertEqual('queue_limit', error.exception.kind)
        finally:
            release.set()
            jobs.close()
            self.assertTrue(jobs.wait_closed())
