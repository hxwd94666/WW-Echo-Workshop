// 仅由显式故障测试启动参数启用的子进程故障注入。
use crate::{MAX_FRAME, MAX_MESSAGE, PROTOCOL_VERSION, wire};
use serde_json::{Value, json};
use std::io::Write;

pub fn before_hello() {
    // Called only after main checks --fault-test; release startup has no injected delay.
    for argument in std::env::args() {
        match argument.as_str() {
            "--fault-startup-hang" => loop {
                std::thread::sleep(std::time::Duration::from_secs(1));
            },
            "--fault-startup-abort" => std::process::exit(93),
            "--fault-startup-version" => {
                let _ = wire::write_frame(
                    &mut std::io::stdout(),
                    &json!({"type":"hello","protocol_version":0,"capabilities":[]}),
                );
                std::process::exit(94);
            }
            _ => {}
        }
    }
}

pub fn run(op: &str, request_id: &str) -> Option<Value> {
    if op.starts_with("fault_chunk_") {
        let mut frame = json!({"type":"chunk","protocol_version":PROTOCOL_VERSION,
            "request_id":request_id,"sequence":0,"total_bytes":100,"data":"{","last":false});
        if op == "fault_chunk_limit" {
            frame["total_bytes"] = json!(MAX_MESSAGE + 1);
        }
        if op == "fault_chunk_identity" {
            let body=json!({"protocol_version":PROTOCOL_VERSION,"request_id":"wrong","type":"result","payload":{}}).to_string();
            frame["total_bytes"] = json!(body.len());
            frame["data"] = json!(body);
            frame["last"] = json!(true);
        }
        let _ = wire::write_frame(&mut std::io::stdout(), &frame);
        match op {
            "fault_chunk_sequence" => frame["sequence"] = json!(2),
            "fault_chunk_foreign" => {
                frame["request_id"] = json!("foreign");
                frame["sequence"] = json!(1);
            }
            "fault_chunk_duplicate" => (),
            _ => std::process::exit(95),
        }
        let _ = wire::write_frame(&mut std::io::stdout(), &frame);
        std::process::exit(95);
    }
    match op {
        "fault_large_response" => Some(json!({"text":"声骸🎮\\\"\n".repeat(500000)})),
        "fault_result_limit" => Some(json!({"text":"x".repeat(MAX_MESSAGE)})),
        "fault_panic" => panic!("injected child panic"),
        "fault_abort" => std::process::exit(91),
        "fault_hang" => loop {
            std::thread::sleep(std::time::Duration::from_secs(1));
        },
        "fault_frame_limit" => {
            let mut out = std::io::stdout().lock();
            let _ = out.write_all(&((MAX_FRAME + 1) as u32).to_le_bytes());
            let _ = out.flush();
            std::process::exit(92);
        }
        "fault_wrong_id" => {
            let _ = wire::write_frame(
                &mut std::io::stdout(),
                &json!({"type":"result","protocol_version":PROTOCOL_VERSION,"request_id":"wrong","payload":{}}),
            );
            Some(json!({}))
        }
        "fault_stderr" => {
            let _ = std::io::stderr().write_all(&vec![b'x'; 128 * 1024]);
            Some(json!({"drained":true}))
        }
        "fault_memory" => {
            let buffer = vec![42_u8; 600 * 1024 * 1024];
            std::hint::black_box(&buffer);
            Some(json!({"allocated":buffer.len()}))
        }
        _ => None,
    }
}
