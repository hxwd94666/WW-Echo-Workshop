# 编解码有界 JSON 帧和有序分块消息，拒绝身份、长度及序号不一致。
import json
import struct

PROTOCOL_VERSION = 2
MAX_FRAME = 4 * 1024 * 1024
MAX_MESSAGE = 64 * 1024 * 1024
CHUNK_BYTES = 512 * 1024
MAX_CHUNKS = 256


class ComputeFailure(RuntimeError):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


class ComputeCancelled(ComputeFailure):
    def __init__(self):
        super().__init__('cancelled', '计算已取消')


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode(
        'utf-8'
    )


def encode_frame(value):
    body = _json_bytes(value)
    if not 0 < len(body) <= MAX_FRAME:
        raise ComputeFailure('frame_limit', '计算协议帧超过单帧上限')
    return struct.pack('<I', len(body)) + body


def encode_messages(value):
    body = _json_bytes(value)
    if not 0 < len(body) <= MAX_MESSAGE:
        raise ComputeFailure('message_limit', '计算消息超过 64 MiB 上限')
    if len(body) <= MAX_FRAME:
        yield struct.pack('<I', len(body)) + body
        return
    identity = value.get('request_id')
    if not isinstance(identity, str) or not 0 < len(identity.encode('utf-8')) <= 128:
        raise ComputeFailure('invalid_chunk', '分块消息缺少明确的作业身份')
    offset, sequence = 0, 0
    while offset < len(body):
        end = min(offset + CHUNK_BYTES, len(body))
        while end < len(body) and body[end] & 0xC0 == 0x80:
            end -= 1
        yield encode_frame(
            dict(
                type='chunk',
                protocol_version=PROTOCOL_VERSION,
                request_id=identity,
                sequence=sequence,
                total_bytes=len(body),
                last=end == len(body),
                data=body[offset:end].decode('utf-8'),
            )
        )
        offset, sequence = end, sequence + 1


def _read_exact(stream, count):
    body = bytearray()
    while len(body) < count:
        part = stream.read(count - len(body))
        if not part:
            raise ComputeFailure('unexpected_eof', '计算子进程输出提前结束')
        body.extend(part)
    return body


def _invalid_constant(value):
    raise ComputeFailure('protocol', '计算消息包含非有限 JSON 数值')


def read_frame(stream):
    size = struct.unpack('<I', _read_exact(stream, 4))[0]
    if not 0 < size <= MAX_FRAME:
        raise ComputeFailure('frame_limit', '计算子进程返回超长帧')
    value = json.loads(_read_exact(stream, size), parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ComputeFailure('protocol', '计算响应须为对象')
    return value


class MessageAssembler:
    def __init__(self):
        self.identity, self.total, self.sequence = None, 0, 0
        self.body = bytearray()

    def push(self, frame):
        if frame.get('type') != 'chunk':
            if self.identity is not None:
                raise ComputeFailure('chunk_sequence', '分块尚未结束就收到其他消息')
            return frame
        identity, total, sequence = (
            frame.get(key) for key in ('request_id', 'total_bytes', 'sequence')
        )
        data, last = frame.get('data'), frame.get('last')
        if (
            type(frame.get('protocol_version')) is not int
            or frame['protocol_version'] != PROTOCOL_VERSION
            or not isinstance(identity, str)
            or not 0 < len(identity.encode('utf-8')) <= 128
            or type(total) is not int
            or total <= 0
            or type(sequence) is not int
            or sequence < 0
            or not isinstance(data, str)
            or type(last) is not bool
        ):
            raise ComputeFailure('invalid_chunk', '分块消息字段无效')
        if total > MAX_MESSAGE:
            raise ComputeFailure('chunk_limit', '分块消息超过 64 MiB 上限')
        if sequence >= MAX_CHUNKS:
            raise ComputeFailure('chunk_limit', '分块消息数量超过上限')
        raw = data.encode('utf-8')
        if not 0 < len(raw) <= CHUNK_BYTES:
            raise ComputeFailure('chunk_limit', '单块数据长度无效')
        if self.identity is None:
            self.identity, self.total = identity, total
        if identity != self.identity or total != self.total or sequence != self.sequence:
            raise ComputeFailure('chunk_sequence', '分块作业身份、总长或序号不一致')
        if len(self.body) + len(raw) > total:
            raise ComputeFailure('chunk_limit', '分块累计长度超过声明值')
        self.body.extend(raw)
        self.sequence += 1
        if last != (len(self.body) == total):
            raise ComputeFailure('chunk_sequence', '分块终止标记与累计长度不一致')
        if not last:
            return None
        value = json.loads(self.body, parse_constant=_invalid_constant)
        if (
            not isinstance(value, dict)
            or value.get('request_id') != identity
            or type(value.get('protocol_version')) is not int
            or value['protocol_version'] != PROTOCOL_VERSION
            or value.get('type') == 'chunk'
        ):
            raise ComputeFailure('chunk_identity', '重组消息与分块身份不一致')
        self.identity, self.total, self.sequence = None, 0, 0
        self.body.clear()
        return value
