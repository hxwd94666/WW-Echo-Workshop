# 验证握手停滞、迟到启动和取消反馈的有界隔离与进程回收。
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from echo_workshop.application.compute_jobs import ComputeJobs
from echo_workshop.application.compute_protocol import ComputeFailure, ComputeWorker

ENGINE = Path(__file__).resolve().parents[2] / 'build/native/release/echo-workshop-compute.exe'
REAL_POPEN = subprocess.Popen


class TestComputeStartup(unittest.TestCase):
    def test_hello_hang_cancellation_reaps_child_without_waiting_three_seconds(self):
        launched, cancel = threading.Event(), threading.Event()
        children = []

        def start(args, **kwargs):
            child = REAL_POPEN([*args, '--fault-startup-hang'], **kwargs)
            children.append(child)
            launched.set()
            return child

        with (
            ComputeWorker(ENGINE, fault_tests=True) as worker,
            patch('echo_workshop.application.compute_protocol.subprocess.Popen', side_effect=start),
            patch.object(worker, '_send', wraps=worker._send) as send,
            ThreadPoolExecutor(max_workers=1) as calls,
        ):
            work = calls.submit(worker.call, 'ping', {}, timeout=10, cancel_event=cancel)
            self.assertTrue(launched.wait(2))
            started = time.monotonic()
            cancel.set()
            with self.assertRaises(ComputeFailure) as error:
                work.result(timeout=1)
            self.assertEqual('cancelled', error.exception.kind)
            self.assertLess(time.monotonic() - started, 1)
            send.assert_not_called()
            self.assertIsNone(worker.pid)
            self.assertIsNotNone(children[0].poll())

    def test_startup_faults_are_gated_and_reported_before_business_send(self):
        for flag, kind in (
            ('--fault-startup-abort', 'unexpected_eof'),
            ('--fault-startup-version', 'protocol_version'),
        ):
            children = []

            def start(args, **kwargs):
                child = REAL_POPEN([*args, flag], **kwargs)
                children.append(child)
                return child

            with (
                self.subTest(flag=flag),
                patch(
                    'echo_workshop.application.compute_protocol.subprocess.Popen', side_effect=start
                ),
                ComputeWorker(ENGINE, fault_tests=True) as worker,
                patch.object(worker, '_send', wraps=worker._send) as send,
            ):
                with self.assertRaises(ComputeFailure) as error:
                    worker.call('ping', {}, timeout=1)
                self.assertEqual(kind, error.exception.kind)
                send.assert_not_called()
                self.assertIsNotNone(children[0].poll())

        # The same test-only startup flag has no effect without --fault-test.
        def unarmed(args, **kwargs):
            return REAL_POPEN([*args, '--fault-startup-hang'], **kwargs)

        with (
            patch(
                'echo_workshop.application.compute_protocol.subprocess.Popen', side_effect=unarmed
            ),
            ComputeWorker(ENGINE) as worker,
        ):
            self.assertEqual({'alive': True}, worker.call('ping', {'alive': True}, timeout=1))

    def test_stalled_hello_has_fast_cancel_feedback_and_other_lane_still_works(self):
        launched, cancel = threading.Event(), threading.Event()
        children = []

        def start(args, **kwargs):
            # Only the first lane is stalled; subsequent starts use the real engine.
            child = REAL_POPEN([*args, '--fault-startup-hang'] if not children else args, **kwargs)
            children.append(child)
            launched.set()
            return child

        jobs = ComputeJobs(ENGINE, fault_tests=True)
        try:
            with patch(
                'echo_workshop.application.compute_protocol.subprocess.Popen', side_effect=start
            ):
                work = jobs.submit('ping', dict, lane='search', cancel_event=cancel)
                self.assertTrue(launched.wait(2))
                self.assertEqual({}, jobs.submit('ping', dict).result(timeout=2))
                started = time.monotonic()
                cancel.set()
                with self.assertRaises(ComputeFailure) as error:
                    work.result(timeout=0.5)
                self.assertEqual('cancelled', error.exception.kind)
                self.assertLess(time.monotonic() - started, 0.25)
                # The single search owner reaps first; then a new explicit job may start.
                self.assertEqual({}, jobs.submit('ping', dict, lane='search').result(timeout=2))
                self.assertEqual(0, jobs._failures['search'])
                self.assertIsNotNone(children[0].poll())
        finally:
            jobs.close()
            self.assertTrue(jobs.wait_closed())
        self.assertTrue(all(child.poll() is not None for child in children))

    def test_late_popen_cancel_timeout_and_close_never_send_or_publish(self):
        for reason in ('cancelled', 'timeout', 'closed'):
            launched, release, cancel = threading.Event(), threading.Event(), threading.Event()
            children = []

            def late(args, **kwargs):
                child = REAL_POPEN(args, **kwargs)
                children.append(child)
                launched.set()
                release.wait(5)
                return child

            jobs = ComputeJobs(ENGINE)
            try:
                with (
                    self.subTest(reason=reason),
                    patch(
                        'echo_workshop.application.compute_protocol.subprocess.Popen',
                        side_effect=late,
                    ),
                    patch.object(ComputeWorker, '_send', autospec=True) as send,
                ):
                    work = jobs.submit(
                        'ping',
                        dict,
                        timeout=0.5 if reason == 'timeout' else 10,
                        cancel_event=cancel,
                    )
                    self.assertTrue(launched.wait(2))
                    started = time.monotonic()
                    if reason == 'cancelled':
                        cancel.set()
                    elif reason == 'closed':
                        jobs.close()
                    with self.assertRaises(ComputeFailure) as error:
                        work.result(timeout=0.8)
                    self.assertEqual(reason, error.exception.kind)
                    if reason != 'timeout':
                        self.assertLess(time.monotonic() - started, 0.25)
                    else:
                        self.assertLess(time.monotonic() - started, 0.65)
                    # Closing the owner is nonblocking, but not reported reaped before Popen returns.
                    jobs.close()
                    self.assertFalse(jobs.wait_closed(timeout=0.02))
                    release.set()
                    self.assertTrue(jobs.wait_closed())
                    send.assert_not_called()
                    self.assertIsNotNone(children[0].poll())
                    self.assertEqual(reason, work.exception().kind)
            finally:
                release.set()
                jobs.close()
                self.assertTrue(jobs.wait_closed())

    def test_running_hang_cancel_feedback_precedes_child_reap_without_extra_lane_thread(self):
        jobs = ComputeJobs(ENGINE, fault_tests=True)
        cancel = threading.Event()
        entered = threading.Event()
        original_receive = ComputeWorker._receive

        def receive(worker, deadline, token=None):
            if worker.capabilities and token is not None:
                entered.set()
            return original_receive(worker, deadline, token)

        try:
            with patch.object(ComputeWorker, '_receive', receive):
                work = jobs.submit('fault_hang', dict, cancel_event=cancel)
                self.assertTrue(entered.wait(2))
                owner = jobs._threads['interactive']
                started = time.monotonic()
                cancel.set()
                with self.assertRaises(ComputeFailure) as error:
                    work.result(timeout=0.5)
                self.assertEqual('cancelled', error.exception.kind)
                self.assertLess(time.monotonic() - started, 0.25)
                self.assertEqual({}, jobs.submit('ping', dict).result(timeout=2))
                self.assertIs(owner, jobs._threads['interactive'])
                self.assertEqual(0, jobs._failures['interactive'])
        finally:
            jobs.close()
            self.assertTrue(jobs.wait_closed())


if __name__ == '__main__':
    unittest.main()
