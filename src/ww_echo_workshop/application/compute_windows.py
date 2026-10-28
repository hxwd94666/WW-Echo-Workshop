# 将计算子进程放入有内存上限且随父进程关闭的 Windows Job。
import ctypes
from ctypes import wintypes
import os
import threading


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ('process_time', ctypes.c_int64),
        ('job_time', ctypes.c_int64),
        ('flags', wintypes.DWORD),
        ('minimum_working_set', ctypes.c_size_t),
        ('maximum_working_set', ctypes.c_size_t),
        ('active_processes', wintypes.DWORD),
        ('affinity', ctypes.c_size_t),
        ('priority_class', wintypes.DWORD),
        ('scheduling_class', wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            'read_operations',
            'write_operations',
            'other_operations',
            'read_bytes',
            'write_bytes',
            'other_bytes',
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ('basic', _BasicLimits),
        ('io', _IoCounters),
        ('process_memory', ctypes.c_size_t),
        ('job_memory', ctypes.c_size_t),
        ('peak_process_memory', ctypes.c_size_t),
        ('peak_job_memory', ctypes.c_size_t),
    ]


class _CompletionPort(ctypes.Structure):
    _fields_ = [('key', ctypes.c_void_p), ('port', wintypes.HANDLE)]


class ComputeJob:
    """A non-inheritable Job handle owns only the plugin's compute child."""

    def __init__(self, process, memory_bytes):
        self.handle = None
        self._port = None
        self._lock = threading.Lock()
        if os.name != 'nt':
            raise OSError('当前计算进程隔离仅支持 Windows')
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.CreateIoCompletionPort.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.c_size_t,
            wintypes.DWORD,
        ]
        kernel.CreateIoCompletionPort.restype = wintypes.HANDLE
        kernel.GetQueuedCompletionStatus.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.DWORD,
        ]
        kernel.GetQueuedCompletionStatus.restype = wintypes.BOOL
        self._kernel = kernel
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = handle
        limits = _ExtendedLimits()
        # KILL_ON_JOB_CLOSE | PROCESS_MEMORY | ACTIVE_PROCESS.
        limits.basic.flags = 0x2000 | 0x100 | 0x8
        limits.basic.active_processes = 1
        limits.process_memory = memory_bytes
        try:
            if not kernel.SetInformationJobObject(
                handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            port = kernel.CreateIoCompletionPort(wintypes.HANDLE(-1), None, 0, 1)
            if not port:
                raise ctypes.WinError(ctypes.get_last_error())
            self._port = port
            association = _CompletionPort(None, port)
            if not kernel.SetInformationJobObject(
                handle, 7, ctypes.byref(association), ctypes.sizeof(association)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            # The engine only emits hello and waits for input before assignment.
            # No business request is sent until the Job is established.
            if not kernel.AssignProcessToJobObject(handle, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def resource_error(self):
        """Poll Job notifications without waiting; only the owned child is affected."""
        with self._lock:
            return self._resource_error_locked()

    def _resource_error_locked(self):
        port = self._port
        if port is None:
            return None
        for _ in range(16):
            message = wintypes.DWORD()
            key = ctypes.c_size_t()
            process_id = ctypes.c_void_p()
            if not self._kernel.GetQueuedCompletionStatus(
                port, ctypes.byref(message), ctypes.byref(key), ctypes.byref(process_id), 0
            ):
                break
            if message.value in (9, 10):
                return 'memory_limit'
        return None

    def close(self):
        with self._lock:
            self._close_locked()

    def _close_locked(self):
        handle, self.handle = self.handle, None
        if handle is not None:
            self._kernel.CloseHandle(handle)
        port, self._port = self._port, None
        if port is not None:
            self._kernel.CloseHandle(port)
