# 验证双向分块、累计上限、传输取消及乱序和截断故障不影响父进程。
import io
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
from uuid import UUID

from echo_workshop.application.compute_jobs import ComputeJobs
from echo_workshop.application.compute_messages import (
    CHUNK_BYTES,
    MAX_CHUNKS,
    MAX_FRAME,
    MAX_MESSAGE,
    PROTOCOL_VERSION,
    ComputeFailure,
    MessageAssembler,
    encode_frame,
    encode_messages,
    read_frame,
)
from echo_workshop.application.compute_protocol import ComputeWorker

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / 'build/native/release/echo-workshop-compute.exe'


def chunk():
    return dict(
        type='chunk',
        protocol_version=PROTOCOL_VERSION,
        request_id='chunk-test',
        sequence=0,
        total_bytes=100,
        data='{',
        last=False,
    )


class TestComputeChunks(unittest.TestCase):
    def test_python_frame_roundtrip_preserves_unicode_escaping_and_bounded_chunks(self):
        value = dict(
            protocol_version=PROTOCOL_VERSION,
            request_id='chunk-test',
            op='ping',
            payload={'text': '声骸🎮\\"\n' * 400000},
        )
        assembler, count = MessageAssembler(), 0
        actual = None
        for raw in encode_messages(value):
            self.assertLessEqual(len(raw) - 4, MAX_FRAME)
            frame = read_frame(io.BytesIO(raw))
            self.assertEqual(count, frame['sequence'])
            self.assertLessEqual(len(frame['data'].encode()), CHUNK_BYTES)
            actual = assembler.push(frame)
            count += 1
        self.assertGreater(count, 1)
        self.assertEqual(value, actual)
        self.assertEqual(bytearray(), assembler.body)
        self.assertEqual({'small': True}, assembler.push({'small': True}))

    def test_python_rejects_malformed_chunks_and_different_logical_identity(self):
        cases = [
            ({'sequence': True}, 'invalid_chunk'),
            ({'sequence': MAX_CHUNKS}, 'chunk_limit'),
            ({'sequence': 1}, 'chunk_sequence'),
            ({'total_bytes': MAX_MESSAGE + 1}, 'chunk_limit'),
            ({'data': ''}, 'chunk_limit'),
            ({'data': 'x' * (CHUNK_BYTES + 1)}, 'chunk_limit'),
            ({'last': 1}, 'invalid_chunk'),
            ({'last': True}, 'chunk_sequence'),
            ({'protocol_version': float(PROTOCOL_VERSION)}, 'invalid_chunk'),
        ]
        for changes, kind in cases:
            with self.subTest(changes=list(changes)):
                frame = chunk()
                frame.update(changes)
                with self.assertRaises(ComputeFailure) as error:
                    MessageAssembler().push(frame)
                self.assertEqual(kind, error.exception.kind)
        for changes in (
            {'sequence': 0},
            {'sequence': 2},
            {'request_id': 'foreign'},
            {'total_bytes': 101},
        ):
            assembler = MessageAssembler()
            assembler.push(chunk())
            frame = chunk()
            frame.update(sequence=1)
            frame.update(changes)
            with self.assertRaises(ComputeFailure) as error:
                assembler.push(frame)
            self.assertEqual('chunk_sequence', error.exception.kind)
        body = json.dumps(dict(protocol_version=PROTOCOL_VERSION, request_id='foreign')).encode()
        frame = chunk()
        frame.update(total_bytes=len(body), data=body.decode(), last=True)
        with self.assertRaises(ComputeFailure) as error:
            MessageAssembler().push(frame)
        self.assertEqual('chunk_identity', error.exception.kind)
        assembler = MessageAssembler()
        assembler.push(chunk())
        with self.assertRaises(ComputeFailure):
            assembler.push({'type': 'result'})

    def test_native_bidirectional_large_payload_is_exact_and_worker_stays_reusable(self):
        payload = {'text': '声骸🎮\\"\n' * 400000, 'identity': 'synthetic-only'}
        self.assertGreater(len(json.dumps(payload, ensure_ascii=False).encode()), MAX_FRAME)
        with ComputeWorker(ENGINE) as worker:
            result = worker.call('ping', payload, timeout=10)
            self.assertEqual(payload, result)
            pid = worker.pid
            self.assertEqual({'small': True}, worker.call('ping', {'small': True}))
            self.assertEqual(pid, worker.pid)
        print('CHUNK_BIDIRECTIONAL_OK unicode_exact=True frames_bounded=True')

    def test_native_large_result_and_result_limit_are_explicit_not_truncated(self):
        with ComputeWorker(ENGINE, lane='search', fault_tests=True) as worker:
            result = worker.call('fault_large_response', {}, timeout=10)
            self.assertEqual('声骸🎮\\"\n' * 500000, result['text'])
            pid = worker.pid
            with self.assertRaises(ComputeFailure) as error:
                worker.call('fault_result_limit', {}, timeout=10)
            self.assertEqual('message_limit', error.exception.kind)
            self.assertEqual({}, worker.call('ping', {}))
            self.assertEqual(pid, worker.pid)

    def test_real_matching_request_over_single_frame_keeps_all_frozen_cells(self):
        data = json.loads(
            (ROOT / 'tests/calculation/fixtures/native_matching.json').read_text(encoding='utf-8')
        )
        sample = data['expected']['cells'][0]
        sample_id = data['expected']['ids'][0]
        expected_standards = data['expected']['standards']
        record = next(row for row in data['payload']['records'] if row['item_id'] == sample_id)
        # 列式单元按记录优先排列：第 0 格对应第 0 个选择。
        selection = data['payload']['selections'][0]
        records = [dict(record, item_id=str(UUID(int=900000 + index))) for index in range(10000)]
        payload = dict(records=records, selections=[selection])
        self.assertGreater(len(json.dumps(payload, ensure_ascii=False).encode()), MAX_FRAME)
        with ComputeWorker(ENGINE) as worker:
            result = worker.call('match_batch', payload, timeout=15)
        cells = result['cells']
        # 只发送一个选择：标准表只含该选择的 COST 1/3/4 三项。
        self.assertEqual(expected_standards[:3], result['standards'])
        self.assertEqual(len(records), len(cells))
        self.assertEqual([row['item_id'] for row in records], result['ids'])
        for cell in cells:
            self.assertEqual(sample, cell)
        print('CHUNK_REAL_MATCH_OK records=10000 cells=10000 exact_frozen_cells=True')

    def test_outgoing_message_limit_sends_nothing_and_does_not_poison_transport(self):
        self.assertEqual(64 * 1024 * 1024, MAX_MESSAGE)
        with ComputeWorker(ENGINE) as worker:
            worker.call('ping', {})
            pid = worker.pid
            # Exercise pre-send failure at a tiny synthetic cap without allocating 64 MiB here.
            with patch('echo_workshop.application.compute_messages.MAX_MESSAGE', 128):
                with self.assertRaises(ComputeFailure) as error:
                    worker.call('ping', {'text': 'x' * 256})
            self.assertEqual('message_limit', error.exception.kind)
            self.assertEqual({}, worker.call('ping', {}))
            self.assertEqual(pid, worker.pid)

    def test_native_bad_input_chunks_are_reported_before_dispatch(self):
        for changes, kind in [
            ({'sequence': 1}, 'chunk_sequence'),
            ({'total_bytes': MAX_MESSAGE + 1}, 'chunk_limit'),
            ({'last': True}, 'chunk_sequence'),
        ]:
            with self.subTest(changes=changes), ComputeWorker(ENGINE) as worker:
                deadline = time.monotonic() + 2
                worker._start(deadline)
                frame = chunk()
                frame.update(changes)
                worker._writes.put(encode_frame(frame), timeout=1)
                response = worker._receive(deadline)
                self.assertEqual('error', response['type'])
                self.assertEqual(kind, response['error']['kind'])
                self.assertNotIn('payload', response)

    def test_native_cancel_discards_partial_input_and_accepts_next_explicit_request(self):
        with ComputeWorker(ENGINE) as worker:
            deadline = time.monotonic() + 2
            worker._start(deadline)
            worker._writes.put(encode_frame(chunk()), timeout=1)
            worker._writes.put(
                encode_frame(
                    dict(protocol_version=PROTOCOL_VERSION, request_id='chunk-test', op='cancel')
                ),
                timeout=1,
            )
            self.assertEqual('cancelled', worker._receive(deadline)['error']['kind'])
            self.assertEqual({'next': True}, worker.call('ping', {'next': True}))

    def test_native_input_eof_during_assembly_never_dispatches_partial_payload(self):
        with ComputeWorker(ENGINE) as worker:
            deadline = time.monotonic() + 2
            worker._start(deadline)
            # Direct raw write is intentional: close only after the first physical frame flushed.
            worker._process.stdin.write(encode_frame(chunk()))
            worker._process.stdin.close()
            with self.assertRaises(ComputeFailure) as error:
                worker._receive(deadline)
            self.assertEqual('unexpected_eof', error.exception.kind)

    def test_bad_native_result_chunks_poison_worker_without_publishing_partial_data(self):
        for name, kind in [
            ('sequence', 'chunk_sequence'),
            ('duplicate', 'chunk_sequence'),
            ('foreign', 'chunk_sequence'),
            ('identity', 'chunk_identity'),
            ('limit', 'chunk_limit'),
            ('truncate', 'unexpected_eof'),
        ]:
            with self.subTest(fault=name), ComputeWorker(ENGINE, fault_tests=True) as worker:
                with self.assertRaises(ComputeFailure) as error:
                    worker.call('fault_chunk_' + name, {}, timeout=2)
                self.assertEqual(kind, error.exception.kind)
                self.assertIsNone(worker.pid)

    def test_cancel_during_chunk_send_finishes_feedback_and_reaps_owned_process(self):
        jobs, cancel, entered = ComputeJobs(ENGINE), threading.Event(), threading.Event()
        original = encode_messages

        def interrupted(value):
            for index, frame in enumerate(original(value)):
                yield frame
                if index == 0:
                    entered.set()
                    cancel.wait(2)

        try:
            with patch('echo_workshop.application.compute_protocol.encode_messages', interrupted):
                work = jobs.submit(
                    'ping', lambda: {'text': 'x' * (MAX_FRAME + 100)}, cancel_event=cancel
                )
                self.assertTrue(entered.wait(2))
                started = time.monotonic()
                cancel.set()
                with self.assertRaises(ComputeFailure) as error:
                    work.result(timeout=0.5)
                self.assertEqual('cancelled', error.exception.kind)
                self.assertLess(time.monotonic() - started, 0.25)
            self.assertEqual({}, jobs.submit('ping', dict).result(timeout=2))
        finally:
            jobs.close()
            self.assertTrue(jobs.wait_closed())


if __name__ == '__main__':
    unittest.main()
