# 在后台与有界 Rust 子进程通信并处理取消、超时和故障。
import json
import hashlib
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from uuid import uuid4

from echo_workshop.application.compute_windows import ComputeJob
from echo_workshop.application.compute_messages import (
    PROTOCOL_VERSION,
    MAX_FRAME,
    MAX_MESSAGE,
    CHUNK_BYTES,
    MAX_CHUNKS,
    ComputeFailure,
    ComputeCancelled,
    MessageAssembler,
    encode_frame as encode_frame,
    encode_messages,
    read_frame,
)
from echo_workshop import __version__

STDERR_LIMIT = 64 * 1024


def engine_path():
    """Use one pinned packaged path; no PATH search or implicit source build."""
    return Path(__file__).resolve().parents[1] / 'native' / 'bin' / 'echo-workshop-compute.exe'


def verify_engine(executable, manifest):
    try:
        if manifest.stat().st_size > 64 * 1024 or executable.stat().st_size > 16 * 1024 * 1024:
            raise ComputeFailure('engine_integrity', '计算程序或清单超过尺寸限额')
        metadata = json.loads(manifest.read_text(encoding='utf-8'))
        if (
            metadata.get('product_version') != __version__
            or type(metadata.get('protocol_version')) is not int
            or metadata.get('protocol_version') != PROTOCOL_VERSION
        ):
            raise ComputeFailure('engine_version', '计算程序与插件版本不一致')
        if hashlib.sha256(executable.read_bytes()).hexdigest() != metadata.get('sha256'):
            raise ComputeFailure('engine_integrity', '计算程序完整性检查失败')
        return metadata
    except (OSError, ValueError, AttributeError) as error:
        raise ComputeFailure('engine_integrity', '计算程序清单损坏或缺失') from error


class ComputeWorker:
    """Blocking transport primitive: only ComputeJobs/background callers use it."""

    def __init__(self, executable=None, *, lane='interactive', fault_tests=False):
        if lane not in ('interactive', 'search'):
            raise ValueError('未知计算队列')
        self.executable = Path(executable) if executable is not None else engine_path()
        self._packaged = executable is None
        self._expected_metadata = None
        self.lane = lane
        self._fault_tests = fault_tests
        self._lock = threading.Lock()
        self._lifecycle = threading.RLock()
        self._process = self._job = None
        self._closed = threading.Event()
        self._failure = None
        self._messages = queue.Queue(maxsize=1)
        self._writes = queue.Queue(maxsize=2)
        self._stderr = bytearray()
        self._stderr_lock = threading.Lock()
        self._threads = []
        self.capabilities = ()

    @property
    def pid(self):
        return self._process.pid if self._process is not None else None

    def _fail(self, error):
        self._failure = error

    def _reader(self, process, messages):
        try:
            assembler = MessageAssembler()
            while True:
                value = assembler.push(read_frame(process.stdout))
                if value is not None:
                    messages.put_nowait(value)
        except Exception as error:
            self._fail(
                error
                if isinstance(error, ComputeFailure)
                else ComputeFailure('protocol', str(error))
            )

    def _writer(self, process, writes):
        try:
            while True:
                data = writes.get()
                if data is None:
                    return
                remaining = memoryview(data)
                while remaining:
                    written = process.stdin.write(remaining)
                    if not written:
                        raise OSError('计算管道写入提前结束')
                    remaining = remaining[written:]
                process.stdin.flush()
        except (OSError, ValueError) as error:
            self._fail(ComputeFailure('transport', str(error)))

    def _drain_stderr(self, process):
        try:
            while part := process.stderr.read(4096):
                with self._stderr_lock:
                    self._stderr.extend(part)
                    if len(self._stderr) > STDERR_LIMIT:
                        del self._stderr[:-STDERR_LIMIT]
        except (OSError, ValueError):
            pass

    def _checkpoint(self, deadline, cancel_event=None):
        if self._closed.is_set():
            raise ComputeFailure('closed', '计算服务已关闭')
        if cancel_event is not None and cancel_event.is_set():
            raise ComputeCancelled()
        if time.monotonic() >= deadline:
            raise ComputeFailure('timeout', '计算进程超过作业截止时间')

    def _start(self, deadline, cancel_event=None):
        with self._lifecycle:
            self._start_locked(deadline, cancel_event)

    def _start_locked(self, deadline, cancel_event):
        self._checkpoint(deadline, cancel_event)
        if self._process is not None:
            return
        if not self.executable.is_absolute() or not self.executable.is_file():
            raise ComputeFailure('engine_missing', '缺少随包 Rust 计算程序')
        if self._packaged:
            self._expected_metadata = verify_engine(
                self.executable, self.executable.parent.parent / 'manifest.json'
            )
        args = [str(self.executable)]
        if self._fault_tests:
            args.append('--fault-test')
        self._checkpoint(deadline, cancel_event)
        try:
            process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                bufsize=0,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
        except OSError as error:
            raise ComputeFailure('engine_start', str(error)) from error
        self._process = process
        try:
            # Popen may return after cancellation/close/deadline: own and reap that late child.
            self._checkpoint(deadline, cancel_event)
            self._job = ComputeJob(process, (256 if self.lane == 'interactive' else 512) * 1024**2)
            for target, arguments in (
                (self._reader, (process, self._messages)),
                (self._writer, (process, self._writes)),
                (self._drain_stderr, (process,)),
            ):
                thread = threading.Thread(
                    target=target, args=arguments, daemon=True, name='echo-compute-io'
                )
                self._threads.append(thread)
                thread.start()
            hello = self._receive(min(deadline, time.monotonic() + 3), cancel_event)
            if (
                hello.get('type') != 'hello'
                or type(hello.get('protocol_version')) is not int
                or hello['protocol_version'] != PROTOCOL_VERSION
            ):
                raise ComputeFailure('protocol_version', '计算程序协议版本不匹配')
            if self._expected_metadata is not None and any(
                hello.get(field) != self._expected_metadata.get(field)
                for field in ('product_version', 'source_id', 'engine_version')
            ):
                raise ComputeFailure('engine_identity', '计算程序握手身份不匹配')
            caps = hello.get('capabilities')
            if not isinstance(caps, list) or any(not isinstance(c, str) for c in caps):
                raise ComputeFailure('protocol', '计算能力清单错误')
            if 'chunked_messages' not in caps or hello.get('limits') != dict(
                max_frame_bytes=MAX_FRAME,
                max_message_bytes=MAX_MESSAGE,
                chunk_bytes=CHUNK_BYTES,
                max_chunks=MAX_CHUNKS,
            ):
                raise ComputeFailure('protocol', '计算分块能力或资源上限不匹配')
            self.capabilities = tuple(caps)
        except BaseException:
            self.close()
            raise

    def _receive(self, deadline, cancel_event=None):
        while True:
            self._checkpoint(deadline, cancel_event)
            job = self._job
            if job is not None and job.resource_error() == 'memory_limit':
                raise ComputeFailure('memory_limit', '计算子进程超过内存限额')
            try:
                return self._messages.get(
                    timeout=min(0.02, max(0.001, deadline - time.monotonic()))
                )
            except queue.Empty:
                if self._failure is not None:
                    raise self._failure
                if self._closed.is_set():
                    raise ComputeFailure('closed', '计算服务已关闭')

    def _send(self, value, *, deadline, cancel_event=None):
        for data in encode_messages(value):
            while True:
                self._checkpoint(deadline, cancel_event)
                if self._failure is not None:
                    raise self._failure
                try:
                    self._writes.put(data, timeout=0.02)
                    break
                except queue.Full:
                    continue

    def call(self, op, payload, *, timeout=10, cancel_event=None):
        if not 0 < timeout <= 125:
            raise ValueError('计算通信截止时间须为 0–125 秒')
        deadline = time.monotonic() + timeout
        with self._lock:
            if cancel_event is not None and cancel_event.is_set():
                raise ComputeCancelled()
            try:
                self._start(deadline, cancel_event)
                self._checkpoint(deadline, cancel_event)
                if not self._fault_tests and op not in self.capabilities:
                    raise ComputeFailure(
                        'unknown_operation', '随包计算程序未声明此入口，请使用完整一致的插件包'
                    )
                request_id = str(uuid4())
                self._send(
                    dict(
                        protocol_version=PROTOCOL_VERSION,
                        request_id=request_id,
                        op=op,
                        payload=payload,
                    ),
                    deadline=deadline,
                    cancel_event=cancel_event,
                )
                try:
                    message = self._receive(deadline, cancel_event)
                except ComputeCancelled:
                    self._send(
                        dict(protocol_version=PROTOCOL_VERSION, request_id=request_id, op='cancel'),
                        deadline=min(deadline, time.monotonic() + 1),
                    )
                    try:
                        self._receive(min(deadline, time.monotonic() + 1))
                    finally:
                        raise ComputeCancelled() from None
                if message.get('request_id') != request_id:
                    raise ComputeFailure('request_identity', '计算返回了其他作业的结果')
                if (
                    type(message.get('protocol_version')) is not int
                    or message['protocol_version'] != PROTOCOL_VERSION
                ):
                    raise ComputeFailure('protocol_version', '计算结果协议版本不匹配')
                if message.get('type') == 'error':
                    error = message.get('error', {})
                    raise ComputeFailure(
                        error.get('kind', 'engine_error'), error.get('message', '计算出错')
                    )
                if message.get('type') != 'result' or not isinstance(message.get('payload'), dict):
                    raise ComputeFailure('protocol', '计算结果结构错误')
                return message['payload']
            except ComputeFailure as error:
                # Typed input errors leave framing intact; transport/native faults poison the worker.
                if error.kind not in {
                    'invalid_request',
                    'invalid_record',
                    'invalid_rule',
                    'invalid_numeric',
                    'invalid_allocation',
                    'invalid_evidence',
                    'retired_policy',
                    'unknown_operation',
                    'input_limit',
                    'numeric_limit',
                    'invalid_grade',
                    'invalid_custom_weights',
                    'invalid_filter',
                    'unknown_level',
                    'datetime_overflow',
                    'cache_miss',
                    'message_limit',
                }:
                    self.close()
                raise
            except BaseException:
                # A poisoned worker is never reused or replaced by Python computation.
                self.close()
                raise

    def close(self):
        """Called from a background owner; never wait for this on a Qt thread."""
        self._closed.set()
        with self._lifecycle:
            process, self._process = self._process, None
            if self._job is not None:
                self._job.close()
                self._job = None
        if process is not None:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        try:
            self._writes.put_nowait(None)
        except queue.Full:
            pass
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=0.2)
        if process is not None:
            for stream in (process.stdin, process.stdout, process.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
