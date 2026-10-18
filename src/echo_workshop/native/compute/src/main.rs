// 独立进程入口：控制输入和计算分离，取消不排在长计算之后。
use echo_workshop_compute::{
    ENGINE_VERSION, MAX_FRAME, MAX_PENDING, PRODUCT_VERSION, PROTOCOL_VERSION, SOURCE_ID, dispatch,
    wire,
};
use serde_json::{Value, json};
use std::collections::HashMap;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
    mpsc,
};

struct Request {
    id: String,
    op: String,
    payload: Value,
    cancel: Arc<AtomicBool>,
}

fn main() {
    if std::env::args().any(|a| a == "--describe") {
        println!(
            "{}",
            json!({"protocol_version":PROTOCOL_VERSION,"engine_version":ENGINE_VERSION,
            "product_version":PRODUCT_VERSION,"source_id":SOURCE_ID})
        );
        return;
    }
    let fault_tests = std::env::args().any(|a| a == "--fault-test");
    if fault_tests {
        echo_workshop_compute::faults::before_hello();
    }
    let output = Arc::new(Mutex::new(std::io::stdout()));
    let send = |value: Value| wire::write_message(&mut *output.lock().unwrap(), &value);
    if send(
        json!({"type":"hello","protocol_version":PROTOCOL_VERSION,"engine_version":ENGINE_VERSION,
        "product_version":PRODUCT_VERSION,"source_id":SOURCE_ID,
        "capabilities":["ping","score_batch","rating_batch","match_batch","match_projection","pin_check","requirements_check","allocate","objective_batch","allocation_precheck","validate_allocation","loadout_diff","replacement_candidates","replace_preview","current_comparison","observe_stats","custom_weights","inventory_query","inventory_clock","chunked_messages"],
        "limits":{"max_frame_bytes":MAX_FRAME,"max_message_bytes":echo_workshop_compute::MAX_MESSAGE,
        "chunk_bytes":wire::CHUNK_BYTES,"max_chunks":wire::MAX_CHUNKS}}),
    )
    .is_err()
    {
        return;
    }
    let (tx, rx) = mpsc::sync_channel::<Request>(MAX_PENDING);
    let active = Arc::new(Mutex::new(HashMap::<String, Arc<AtomicBool>>::new()));
    let closed = Arc::new(AtomicBool::new(false));
    let reader_active = active.clone();
    let reader_closed = closed.clone();
    let reader_output = output.clone();
    std::thread::spawn(move || {
        let mut stdin = std::io::stdin().lock();
        let mut assembler = wire::MessageAssembler::default();
        while let Ok(Some(raw)) = wire::read_frame(&mut stdin) {
            let raw_id = raw["request_id"].as_str().unwrap_or("").to_owned();
            let mut frame = match assembler.push(raw) {
                Ok(Some(value)) => value,
                Ok(None) => continue,
                Err(error) => {
                    let _ = wire::write_message(
                        &mut *reader_output.lock().unwrap(),
                        &json!({"type":"error","protocol_version":PROTOCOL_VERSION,"request_id":raw_id,"error":error}),
                    );
                    break;
                }
            };
            let id = frame["request_id"].as_str().unwrap_or("").to_owned();
            let op = frame["op"].as_str().unwrap_or("").to_owned();
            if frame["protocol_version"].as_u64() != Some(PROTOCOL_VERSION as u64)
                || id.is_empty()
                || id.len() > 128
            {
                break;
            }
            if op == "cancel" {
                if let Some(token) = reader_active.lock().unwrap().get(&id) {
                    token.store(true, Ordering::Relaxed);
                } else {
                    // Cancelling an incomplete assembly must not dispatch its partial payload.
                    let _ = wire::write_message(
                        &mut *reader_output.lock().unwrap(),
                        &json!({"type":"error","protocol_version":PROTOCOL_VERSION,"request_id":id,
                        "error":{"kind":"cancelled","message":"计算输入传输已取消"}}),
                    );
                }
                continue;
            }
            let token = Arc::new(AtomicBool::new(false));
            {
                let mut jobs = reader_active.lock().unwrap();
                if jobs.contains_key(&id) || jobs.len() >= MAX_PENDING {
                    break;
                }
                jobs.insert(id.clone(), token.clone());
            }
            if tx
                .try_send(Request {
                    id,
                    op,
                    payload: frame
                        .get_mut("payload")
                        .map(Value::take)
                        .unwrap_or(Value::Null),
                    cancel: token,
                })
                .is_err()
            {
                break;
            }
        }
        reader_closed.store(true, Ordering::Relaxed);
        for token in reader_active.lock().unwrap().values() {
            token.store(true, Ordering::Relaxed);
        }
        drop(reader_output);
    });
    for request in rx {
        if closed.load(Ordering::Relaxed) {
            break;
        }
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            if fault_tests && request.op == "allocate_matrix" {
                return echo_workshop_compute::allocation::matrix_batch(
                    request.payload,
                    &request.cancel,
                );
            }
            if fault_tests
                && let Some(value) = echo_workshop_compute::faults::run(&request.op, &request.id)
            {
                return Ok(value);
            }
            dispatch(&request.op, request.payload, &request.cancel)
        }));
        let response = match result {
            Ok(Ok(value)) => {
                json!({"type":"result","protocol_version":PROTOCOL_VERSION,"request_id":request.id,"payload":value})
            }
            Ok(Err(error)) => {
                json!({"type":"error","protocol_version":PROTOCOL_VERSION,"request_id":request.id,"error":error})
            }
            Err(_) => {
                json!({"type":"error","protocol_version":PROTOCOL_VERSION,"request_id":request.id,"error":{"kind":"panic","message":"计算进程内部异常"}})
            }
        };
        active.lock().unwrap().remove(&request.id);
        if let Err(error) = send(response)
            && (error.kind != "message_limit"
                || send(json!({"type":"error",
                "protocol_version":PROTOCOL_VERSION,"request_id":request.id,"error":error}))
                .is_err())
        {
            break;
        }
    }
}
