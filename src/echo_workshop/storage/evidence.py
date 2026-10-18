# 保存并有界读取声骸截图证据。
"""Immutable, content-checked PNG evidence storage (no OCR or device calls)."""

import hashlib
import os
from pathlib import Path
import re
import struct
import tempfile
import zlib
import time
from contextlib import contextmanager

from echo_workshop.core.models import validate_id
from echo_workshop.application.evidence_jobs import EvidenceViewJobs


MAX_IMAGE_BYTES = 16 * 1024 * 1024
_REF = re.compile(r'screenshots/([0-9a-f-]{36})-([0-9a-f]{64})\.png\Z')


def validate_png(data):
    if (
        not isinstance(data, bytes)
        or len(data) > MAX_IMAGE_BYTES
        or data[:8] != b'\x89PNG\r\n\x1a\n'
    ):
        raise ValueError('Expected bounded PNG bytes')
    offset, shape, compressed, ended, seen_data = 8, None, bytearray(), False, False
    while offset + 12 <= len(data):
        length = struct.unpack('>I', data[offset : offset + 4])[0]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError('Truncated PNG chunk')
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : end - 4]
        crc = struct.unpack('>I', data[end - 4 : end])[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != crc:
            raise ValueError('PNG checksum mismatch')
        if shape is None and kind != b'IHDR':
            raise ValueError('PNG header must come first')
        if kind == b'IHDR':
            if shape is not None or length != 13:
                raise ValueError('Invalid PNG header')
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                '>IIBBBBB', payload
            )
            if not 0 < width <= 8192 or not 0 < height <= 8192 or width * height > 36_000_000:
                raise ValueError('PNG dimensions exceed limits')
            if depth != 8 or color not in (2, 6) or compression or filtering or interlace:
                raise ValueError('Evidence requires non-interlaced 8-bit RGB/RGBA PNG')
            shape = (width, height, 3 if color == 2 else 4)
        elif kind == b'IDAT':
            seen_data = True
            compressed.extend(payload)
        elif kind == b'IEND':
            if length or not seen_data or end != len(data):
                raise ValueError('Invalid PNG ending')
            ended = True
            break
        elif kind[:1].isupper():
            raise ValueError('Unsupported critical PNG chunk')
        offset = end
    if not ended or shape is None:
        raise ValueError('Incomplete PNG')
    width, height, channels = shape
    stride = 1 + width * channels
    expected = stride * height
    inflater = zlib.decompressobj()
    try:
        pixels = inflater.decompress(bytes(compressed), expected + 1)
    except zlib.error as error:
        raise ValueError('Corrupted PNG pixels') from error
    if (
        len(pixels) != expected
        or not inflater.eof
        or inflater.unused_data
        or any(pixels[i] > 4 for i in range(0, expected, stride))
    ):
        raise ValueError('Invalid PNG pixel layout')
    return shape[:2]


class EvidenceStore:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.folder = self.data_dir / 'screenshots'
        self.folder.mkdir(parents=True, exist_ok=True)
        self.views = EvidenceViewJobs(lambda reference: self.read(reference))

    @contextmanager
    def operation(self, *, wait=True):
        from echo_workshop.storage.locks import plan_file_lock, PlanBusyError

        if self.folder.is_symlink() or self.folder.resolve() != self.data_dir / 'screenshots':
            raise ValueError('截图目录不是工作区内的实际目录')
        end = time.monotonic() + 5
        while True:
            lock = plan_file_lock(self.folder, '55b13d4b-96b7-483b-a2c4-b0d7312f6a42')
            try:
                lock.__enter__()
                break
            except PlanBusyError:
                if not wait or time.monotonic() >= end:
                    raise
                time.sleep(0.02)
        try:
            yield
        finally:
            lock.__exit__(None, None, None)

    def exists(self, reference):
        path, _ = self._path(reference)
        return path.is_file()

    def _path(self, reference):
        match = _REF.fullmatch(reference)
        if not match:
            raise ValueError('Invalid screenshot reference')
        validate_id(match[1])
        path = (self.data_dir / reference).resolve()
        if path.parent != self.folder.resolve():
            raise ValueError('Screenshot path left the data directory')
        return path, match[2]

    def put(self, item_id, data):
        validate_id(item_id)
        validate_png(data)
        content_hash = hashlib.sha256(data).hexdigest()
        reference = f'screenshots/{item_id}-{content_hash}.png'
        path, _ = self._path(reference)
        if path.exists():
            if self.read(reference) != data:
                raise ValueError('Existing screenshot content conflict')
            return reference
        fd, temporary = tempfile.mkstemp(prefix='.evidence-', dir=self.folder)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self.read(reference)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return reference

    def read(self, reference):
        path, expected = self._path(reference)
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError('Screenshot exceeds size limit')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('Screenshot hash mismatch')
        validate_png(data)
        return data
