// 限制长度的 JSON 帧编解码；协议错误不进入业务求值。
use crate::{ComputeError, ComputeResult, MAX_FRAME, MAX_MESSAGE, PROTOCOL_VERSION};
use serde_json::{Value, json};
use std::io::{Read, Write};

pub fn read_frame(reader: &mut impl Read) -> ComputeResult<Option<Value>> {
    let mut size = [0_u8; 4];
    match reader.read(&mut size[..1]) {
        Ok(0) => return Ok(None),
        Ok(_) => (),
        Err(e) => return Err(ComputeError::new("transport", e.to_string())),
    }
    reader
        .read_exact(&mut size[1..])
        .map_err(|e| ComputeError::new("truncated_frame", e.to_string()))?;
    let size = u32::from_le_bytes(size) as usize;
    if size == 0 || size > MAX_FRAME {
        return Err(ComputeError::new("frame_limit", "协议帧超过长度限制"));
    }
    let mut data = vec![0; size];
    reader
        .read_exact(&mut data)
        .map_err(|e| ComputeError::new("truncated_frame", e.to_string()))?;
    serde_json::from_slice(&data)
        .map(Some)
        .map_err(|e| ComputeError::new("invalid_json", e.to_string()))
}

pub fn write_frame(writer: &mut impl Write, value: &Value) -> ComputeResult<()> {
    let body = serde_json::to_vec(value)
        .map_err(|e| ComputeError::new("invalid_result", e.to_string()))?;
    if body.len() > MAX_FRAME {
        return Err(ComputeError::new("frame_limit", "结果帧超过长度限制"));
    }
    write_body(writer, &body)
}

fn write_body(writer: &mut impl Write, body: &[u8]) -> ComputeResult<()> {
    writer
        .write_all(&(body.len() as u32).to_le_bytes())
        .and_then(|_| writer.write_all(body))
        .and_then(|_| writer.flush())
        .map_err(|e| ComputeError::new("transport", e.to_string()))
}

pub const CHUNK_BYTES: usize = 512 * 1024;
pub const MAX_CHUNKS: u64 = 256;

pub fn write_message(writer: &mut impl Write, value: &Value) -> ComputeResult<()> {
    let body = serde_json::to_vec(value)
        .map_err(|e| ComputeError::new("invalid_result", e.to_string()))?;
    if body.len() > MAX_MESSAGE {
        return Err(ComputeError::new(
            "message_limit",
            "计算消息超过 64 MiB 上限",
        ));
    }
    if body.len() <= MAX_FRAME {
        return write_body(writer, &body);
    }
    let id = value["request_id"]
        .as_str()
        .filter(|id| !id.is_empty() && id.len() <= 128)
        .ok_or_else(|| ComputeError::new("invalid_chunk", "分块消息缺少作业身份"))?;
    let mut offset = 0;
    let mut sequence = 0;
    while offset < body.len() {
        let mut end = (offset + CHUNK_BYTES).min(body.len());
        while end < body.len() && body[end] & 0xC0 == 0x80 {
            end -= 1;
        }
        let data = std::str::from_utf8(&body[offset..end])
            .map_err(|e| ComputeError::new("invalid_result", e.to_string()))?;
        write_frame(
            writer,
            &json!({"type":"chunk","protocol_version":PROTOCOL_VERSION,"request_id":id,
            "sequence":sequence,"total_bytes":body.len(),"last":end==body.len(),"data":data}),
        )?;
        offset = end;
        sequence += 1;
    }
    Ok(())
}

struct Assembly {
    id: String,
    total: usize,
    sequence: u64,
    body: Vec<u8>,
}

#[derive(Default)]
pub struct MessageAssembler {
    pending: Option<Assembly>,
}

impl MessageAssembler {
    pub fn push(&mut self, frame: Value) -> ComputeResult<Option<Value>> {
        if frame["type"] != "chunk" {
            if frame["op"] == "cancel" {
                if self
                    .pending
                    .as_ref()
                    .is_some_and(|p| frame["request_id"] == p.id)
                {
                    self.pending = None;
                }
            } else if self.pending.is_some() {
                return Err(ComputeError::new(
                    "chunk_sequence",
                    "分块尚未结束就收到其他消息",
                ));
            }
            return Ok(Some(frame));
        }
        let invalid = || ComputeError::new("invalid_chunk", "分块消息字段无效");
        let id = frame["request_id"]
            .as_str()
            .filter(|id| !id.is_empty() && id.len() <= 128)
            .ok_or_else(invalid)?;
        let total = frame["total_bytes"].as_u64().ok_or_else(invalid)?;
        let sequence = frame["sequence"].as_u64().ok_or_else(invalid)?;
        let last = frame["last"].as_bool().ok_or_else(invalid)?;
        let data = frame["data"].as_str().ok_or_else(invalid)?;
        if frame["protocol_version"].as_u64() != Some(PROTOCOL_VERSION as u64) || total == 0 {
            return Err(invalid());
        }
        if total > MAX_MESSAGE as u64
            || data.is_empty()
            || data.len() > CHUNK_BYTES
            || sequence >= MAX_CHUNKS
        {
            return Err(ComputeError::new(
                "chunk_limit",
                "分块消息大小或数量超过上限",
            ));
        }
        let pending = self.pending.get_or_insert_with(|| Assembly {
            id: id.to_owned(),
            total: total as usize,
            sequence: 0,
            body: Vec::new(),
        });
        if pending.id != id || pending.total != total as usize || pending.sequence != sequence {
            return Err(ComputeError::new(
                "chunk_sequence",
                "分块作业身份、总长或序号不一致",
            ));
        }
        if pending.body.len() + data.len() > pending.total {
            return Err(ComputeError::new("chunk_limit", "分块累计长度超过声明值"));
        }
        pending.body.extend_from_slice(data.as_bytes());
        pending.sequence += 1;
        if last != (pending.body.len() == pending.total) {
            return Err(ComputeError::new(
                "chunk_sequence",
                "分块终止标记与累计长度不一致",
            ));
        }
        if !last {
            return Ok(None);
        }
        let complete = self.pending.take().unwrap();
        let value: Value = serde_json::from_slice(&complete.body)
            .map_err(|e| ComputeError::new("invalid_json", e.to_string()))?;
        if !value.is_object()
            || value["request_id"] != complete.id
            || value["type"] == "chunk"
            || value["protocol_version"].as_u64() != Some(PROTOCOL_VERSION as u64)
        {
            return Err(ComputeError::new(
                "chunk_identity",
                "重组消息与分块身份不一致",
            ));
        }
        Ok(Some(value))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn framing_and_limits() {
        let mut bytes = Vec::new();
        let value = serde_json::json!({"text":"声骸"});
        write_frame(&mut bytes, &value).unwrap();
        assert_eq!(read_frame(&mut bytes.as_slice()).unwrap(), Some(value));
        assert!(read_frame(&mut [1_u8, 0, 0].as_slice()).is_err());
        assert!(read_frame(&mut u32::MAX.to_le_bytes().as_slice()).is_err());
    }

    fn part() -> Value {
        json!({"type":"chunk","protocol_version":PROTOCOL_VERSION,"request_id":"test",
        "sequence":0,"total_bytes":100,"last":false,"data":"{"})
    }

    #[test]
    fn large_unicode_messages_have_bounded_frames_and_exact_identity() {
        let value = json!({"type":"result","protocol_version":PROTOCOL_VERSION,
            "request_id":"test","payload":{"text":"声骸🎮\\\"\n".repeat(400000)}});
        let mut bytes = Vec::new();
        write_message(&mut bytes, &value).unwrap();
        let mut stream = bytes.as_slice();
        let mut assembler = MessageAssembler::default();
        let mut count = 0;
        let mut actual = None;
        while let Some(frame) = read_frame(&mut stream).unwrap() {
            assert_eq!(frame["sequence"].as_u64(), Some(count));
            assert!(frame["data"].as_str().unwrap().len() <= CHUNK_BYTES);
            actual = assembler.push(frame).unwrap();
            count += 1;
        }
        assert!(count > 1);
        assert_eq!(actual, Some(value));
        assert!(assembler.pending.is_none());
    }

    #[test]
    fn chunk_boundaries_and_invalid_sequence_are_checked_before_allocation() {
        for (field, value, kind) in [
            ("total_bytes", json!(MAX_MESSAGE + 1), "chunk_limit"),
            ("sequence", json!(MAX_CHUNKS), "chunk_limit"),
            ("sequence", json!(1), "chunk_sequence"),
            ("sequence", json!(true), "invalid_chunk"),
            ("last", json!(1), "invalid_chunk"),
            ("last", json!(true), "chunk_sequence"),
            ("data", json!(""), "chunk_limit"),
        ] {
            let mut frame = part();
            frame[field] = value;
            assert_eq!(
                MessageAssembler::default().push(frame).unwrap_err().kind,
                kind
            );
        }
        let mut frame = part();
        frame["total_bytes"] = json!(MAX_MESSAGE);
        let mut assembler = MessageAssembler::default();
        assert!(assembler.push(frame).unwrap().is_none());
        assert_eq!(assembler.pending.as_ref().unwrap().body.len(), 1);
        for (field, value) in [
            ("request_id", json!("foreign")),
            ("sequence", json!(0)),
            ("total_bytes", json!(101)),
        ] {
            let mut assembler = MessageAssembler::default();
            assembler.push(part()).unwrap();
            let mut next = part();
            next["sequence"] = json!(1);
            next[field] = value;
            assert_eq!(assembler.push(next).unwrap_err().kind, "chunk_sequence");
        }
    }

    #[test]
    fn cancel_can_discard_partial_input_without_dispatch_and_other_cancel_preserves_it() {
        let mut assembler = MessageAssembler::default();
        assembler.push(part()).unwrap();
        let cancel =
            json!({"protocol_version":PROTOCOL_VERSION,"request_id":"foreign","op":"cancel"});
        assert_eq!(assembler.push(cancel.clone()).unwrap(), Some(cancel));
        assert!(assembler.pending.is_some());
        assembler
            .push(json!({"protocol_version":PROTOCOL_VERSION,"request_id":"test","op":"cancel"}))
            .unwrap();
        assert!(assembler.pending.is_none());
        let next = json!({"protocol_version":PROTOCOL_VERSION,"request_id":"next","op":"ping","payload":{}});
        assert_eq!(assembler.push(next.clone()).unwrap(), Some(next));
    }
}
