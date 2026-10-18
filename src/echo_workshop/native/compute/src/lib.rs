// 纯业务计算与有界协议共用的类型，不持有宿主或玩家存储对象。
pub mod allocation;
pub mod allocation_checks;
pub mod allocation_model;
pub mod custom_weights;
pub mod difference;
pub mod domain;
pub mod faults;
pub mod inventory_query;
mod inventory_snapshot;
mod inventory_view;
pub mod match_projection;
pub mod matching;
pub mod numeric;
pub mod objective;
pub mod observations;
pub mod pin_checks;
pub mod rating;
pub mod recommendations;
pub mod replacement;
pub mod requirement_checks;
pub mod scoring;
pub mod wire;

use serde::Serialize;
use serde_json::Value;
use std::sync::OnceLock;
use std::sync::atomic::{AtomicBool, Ordering};

pub const PROTOCOL_VERSION: u32 = 2;
pub const ENGINE_VERSION: &str = "rust-compute-20";
pub const PRODUCT_VERSION: &str = env!("ECHO_PRODUCT_VERSION");
pub const SOURCE_ID: &str = env!("ECHO_SOURCE_ID");
pub const MAX_FRAME: usize = 4 * 1024 * 1024;
pub const MAX_MESSAGE: usize = 64 * 1024 * 1024;
pub const MAX_PENDING: usize = 8;

#[derive(Debug, Serialize)]
pub struct ComputeError {
    pub kind: &'static str,
    pub message: String,
}

impl ComputeError {
    pub fn new(kind: &'static str, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
        }
    }
}

pub type ComputeResult<T> = Result<T, ComputeError>;

pub fn constants() -> &'static Value {
    static DATA: OnceLock<Value> = OnceLock::new();
    DATA.get_or_init(|| {
        serde_json::from_str(include_str!("../data/constants.json")).expect("compiled constants")
    })
}

pub fn checkpoint(cancelled: &AtomicBool) -> ComputeResult<()> {
    if cancelled.load(Ordering::Relaxed) {
        Err(ComputeError::new("cancelled", "计算已取消"))
    } else {
        Ok(())
    }
}

pub fn dispatch(op: &str, payload: Value, cancelled: &AtomicBool) -> ComputeResult<Value> {
    checkpoint(cancelled)?;
    match op {
        "ping" => Ok(payload),
        "score_batch" => scoring::batch(payload, cancelled),
        "rating_batch" => rating::batch(payload, cancelled),
        "match_batch" => matching::batch(payload, cancelled),
        "match_projection" => match_projection::evaluate(payload, cancelled),
        "pin_check" => pin_checks::evaluate(payload, cancelled),
        "requirements_check" => requirement_checks::evaluate(payload, cancelled),
        "allocate" => allocation::batch(payload, cancelled),
        "objective_batch" => objective::batch(payload, cancelled),
        "allocation_precheck" => allocation_checks::precheck(payload, cancelled),
        "validate_allocation" => allocation_checks::validate(payload, cancelled),
        "loadout_diff" => difference::batch(payload, cancelled),
        "replacement_candidates" => replacement::evaluate(payload, cancelled, false),
        "replace_preview" => replacement::evaluate(payload, cancelled, true),
        "current_comparison" => recommendations::evaluate(payload, cancelled),
        "observe_stats" => observations::evaluate(payload, cancelled),
        "custom_weights" => custom_weights::evaluate(payload, cancelled),
        "inventory_query" => inventory_query::evaluate(payload, cancelled),
        "inventory_clock" => inventory_query::clock(payload, cancelled),
        _ => Err(ComputeError::new("unknown_operation", "未注册的计算操作")),
    }
}
