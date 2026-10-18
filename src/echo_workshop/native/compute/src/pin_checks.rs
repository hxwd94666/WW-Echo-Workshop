// 检查明确固定 UUID 的可用性、COST 和主词条，不扩展候选或重新评分。
use crate::domain::{Record, uuid};
use crate::{ComputeError, ComputeResult, checkpoint};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::sync::atomic::AtomicBool;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Requirement {
    key: [String; 2],
    costs: [u8; 5],
    main_names: [Vec<String>; 5],
    fixed_item_ids: [String; 5],
    #[serde(default)]
    allowed_sonatas: Vec<String>,
    #[serde(default)]
    set_requirements: Vec<(String, usize)>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Input {
    identity: String,
    requirements: Vec<Requirement>,
    records: Vec<Record>,
}

pub fn evaluate(payload: Value, cancel: &AtomicBool) -> ComputeResult<Value> {
    let input: Input = serde_json::from_value(payload)
        .map_err(|e| ComputeError::new("invalid_request", e.to_string()))?;
    if input.requirements.len() > 128 || input.records.len() > 640 {
        return Err(ComputeError::new("input_limit", "固定要求校验超过单批限额"));
    }
    if input.identity.is_empty() || input.identity.len() > 128 {
        return Err(ComputeError::new("invalid_request", "固定要求身份无效"));
    }
    let by_id: HashMap<_, _> = input
        .records
        .iter()
        .map(|r| (r.item_id.as_str(), r))
        .collect();
    if by_id.len() != input.records.len() {
        return Err(ComputeError::new(
            "invalid_request",
            "固定要求输入 UUID 重复",
        ));
    }
    let mut keys = HashSet::new();
    let mut verdicts = Vec::with_capacity(input.requirements.len());
    for requirement in input.requirements {
        checkpoint(cancel)?;
        crate::requirement_checks::layout(&requirement.costs, &requirement.main_names)
            .and_then(|_| {
                crate::requirement_checks::sets(
                    &requirement.allowed_sonatas,
                    &requirement.set_requirements,
                )
            })
            .map_err(|message| ComputeError::new("invalid_request", message))?;
        let mut pins = HashSet::new();
        if requirement
            .key
            .iter()
            .any(|key| key.is_empty() || key.len() > 128)
            || !keys.insert(requirement.key.clone())
            || requirement
                .costs
                .iter()
                .any(|cost| ![1, 3, 4].contains(cost))
            || requirement.main_names.iter().any(|names| {
                names.len() > 64 || names.iter().any(|name| name.is_empty() || name.len() > 256)
            })
            || requirement
                .fixed_item_ids
                .iter()
                .any(|id| !id.is_empty() && (!uuid(id) || !pins.insert(id)))
        {
            return Err(ComputeError::new(
                "invalid_request",
                "固定要求结构或 UUID 无效",
            ));
        }
        let mut failure = None;
        for (slot, id) in requirement.fixed_item_ids.iter().enumerate() {
            checkpoint(cancel)?;
            if id.is_empty() {
                continue;
            }
            let record = by_id.get(id.as_str()).copied();
            if record.is_none_or(|record| !record.usable()) {
                failure = Some((slot + 1, "missing"));
                break;
            }
            let record = record.unwrap();
            let names = &requirement.main_names[slot];
            if record.cost != requirement.costs[slot]
                || (!names.is_empty()
                    && !record
                        .main_stats
                        .first()
                        .is_some_and(|stat| names.contains(&stat.stat_name)))
            {
                failure = Some((slot + 1, "conflict"));
                break;
            }
        }
        verdicts.push(match failure {
            Some((slot, code)) => json!({"key":requirement.key,"slot":slot,"code":code}),
            None => json!({"key":requirement.key,"slot":null,"code":""}),
        });
    }
    checkpoint(cancel)?;
    Ok(json!({"identity":input.identity,"verdicts":verdicts}))
}
