# 协调计划文件的跨进程写入锁。
"""Nonblocking OS locks for cooperating local plan readers/writers.

Lock files stay in place: removing one could let another process lock a new
inode while an existing handle still owns the old file. OS handle closure,
including process termination, releases ownership; file age is not ownership.
"""

from contextlib import contextmanager
import errno
import os
from pathlib import Path


class PlanBusyError(OSError):
    pass


@contextmanager
def plan_file_lock(folder, plan_id):
    from echo_workshop.core.models import validate_id

    validate_id(plan_id)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if folder.is_symlink():
        raise OSError('方案目录是符号链接，请使用实际用户数据目录')
    lock_dir = folder / '.locks'
    lock_dir.mkdir(exist_ok=True)
    path = lock_dir / (plan_id + '.lock')
    if lock_dir.is_symlink() or path.is_symlink():
        raise OSError('方案锁路径是符号链接，原方案保留')
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    owned = False
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if os.name == 'nt':
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) or getattr(
                error, 'winerror', None
            ) in (32, 33):
                raise PlanBusyError(
                    '此方案正被其他操作/进程使用；原进度保留，请稍后重新加载'
                ) from error
            raise
        owned = True
        # Initialize only after acquiring the lock; even empty files use byte 0.
        if os.fstat(fd).st_size == 0:
            os.write(fd, b'0')
        yield
    finally:
        try:
            if owned:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == 'nt':
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
