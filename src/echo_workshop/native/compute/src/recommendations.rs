// 按冻结观测批量计算当前件比较、适配与展示顺序，不修改库存。
use crate::domain::{PreparedRule, Record};
use crate::matching::Selection;
use crate::scoring::{Score, Stat, observed_score};
use crate::{ComputeError, ComputeResult, checkpoint};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::HashSet;
use std::sync::atomic::AtomicBool;

#[derive(Deserialize)]
struct Observation {
    cost: u8,
    main_rows: Vec<Stat>,
    sub_rows: Vec<Stat>,
    observed_cost: bool,
}

#[derive(Deserialize)]
struct Input {
    mode: String,
    profile_id: String,
    weight_version: String,
    record: Option<Record>,
    observation: Option<Observation>,
    selections: Vec<Selection>,
    limit: usize,
    has_choices: bool,
    missing_choices: bool,
    config_error: String,
}

#[derive(Serialize)]
struct Row {
    character_id: String,
    modal_id: String,
    label: String,
    state: &'static str,
    score: Score,
}

fn rank(state: &str) -> u8 {
    match state {
        "match" => 0,
        "unknown" => 1,
        _ => 2,
    }
}

pub fn evaluate(payload: Value, cancel: &AtomicBool) -> ComputeResult<Value> {
    checkpoint(cancel)?;
    let input: Input = serde_json::from_value(payload)
        .map_err(|e| ComputeError::new("invalid_request", e.to_string()))?;
    if !["record", "live"].contains(&input.mode.as_str())
        || !(1..=3).contains(&input.limit)
        || input.selections.len() > 128
        || input.weight_version.is_empty()
    {
        return Err(ComputeError::new("invalid_request", "当前件比较输入无效"));
    }
    let live = input.mode == "live";
    let mut seen = HashSet::new();
    for selection in &input.selections {
        if selection.character_id.is_empty()
            || selection.modal_id.is_empty()
            || !selection.template.is_object()
            || !seen.insert((&selection.character_id, &selection.modal_id))
        {
            return Err(ComputeError::new(
                "invalid_request",
                "比较角色身份重复或缺失",
            ));
        }
    }
    let record = input.record.as_ref();
    if !live && record.is_none_or(|r| r.profile_id != input.profile_id) {
        return Err(ComputeError::new("invalid_record", "当前件档案身份不一致"));
    }
    let problems = record.map_or_else(Vec::new, Record::validation_issues);
    let invalid = !live
        && problems
            .iter()
            .any(|p| !p.starts_with("missing:") && p != "ocr:header_name_needs_review");
    let (cost, main, sub, show_rating) = if live {
        let observation = input
            .observation
            .as_ref()
            .ok_or_else(|| ComputeError::new("invalid_request", "实时比较缺少明确观测"))?;
        (
            observation.cost,
            &observation.main_rows,
            &observation.sub_rows,
            observation.observed_cost,
        )
    } else {
        let record = record.unwrap();
        (record.cost, &record.main_stats, &record.sub_stats, true)
    };
    let mut rows = Vec::new();
    if !invalid && input.config_error.is_empty() {
        for selection in input.selections {
            checkpoint(cancel)?;
            let rule = PreparedRule::new(&selection.rule)?;
            let Some(mut score) = observed_score(cost, main, sub, &selection.template)? else {
                continue;
            };
            let mut state = if let Some(record) = record.filter(|_| !live || problems.is_empty()) {
                rule.compatibility(record).2
            } else {
                "unknown"
            };
            if !live && !problems.is_empty() && state == "match" {
                state = "unknown";
            }
            if !show_rating {
                score.rating = None;
            }
            rows.push(Row {
                character_id: selection.character_id,
                modal_id: selection.modal_id,
                label: selection.template["name"].as_str().unwrap_or("").into(),
                state,
                score,
            });
            if live && rows.len() == input.limit {
                break;
            }
        }
    }
    if !live {
        rows.sort_by(|a, b| {
            rank(a.state)
                .cmp(&rank(b.state))
                .then_with(|| b.score.current_cents.cmp(&a.score.current_cents))
                .then_with(|| a.character_id.cmp(&b.character_id))
                .then_with(|| a.modal_id.cmp(&b.modal_id))
        });
    }
    let mut message = if input.missing_choices {
        "部分已选模版失效，请在矩阵重新选择".to_owned()
    } else {
        String::new()
    };
    if invalid {
        message = "当前件识别待纠错，暂不进行跨模版比较".into();
    } else if !input.config_error.is_empty() {
        message = input.config_error;
    } else if !input.has_choices {
        message = "请先在当前档案的评分矩阵勾选角色/固定模态".into();
    } else if !live && !problems.is_empty() && !rows.is_empty() {
        if !message.is_empty() {
            message.push('；');
        }
        message.push_str("名称/套装/等级等元数据待核对，不作确定推荐");
    }
    rows.truncate(input.limit);
    checkpoint(cancel)?;
    Ok(json!({"mode":input.mode,"profile_id":input.profile_id,
        "weight_version":input.weight_version,"rows":rows,"message":message}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn explicit_limits_and_cancel_are_checked_before_result_publication() {
        let input = json!({"mode":"live","profile_id":"profile","weight_version":"frozen",
            "record":null,"observation":{"cost":1,"main_rows":[],"sub_rows":[],"observed_cost":false},
            "selections":[],"limit":3,"has_choices":false,"missing_choices":false,"config_error":""});
        let normal = AtomicBool::new(false);
        assert_eq!(evaluate(input.clone(), &normal).unwrap()["rows"], json!([]));
        let mut invalid = input.clone();
        invalid["limit"] = json!(4);
        assert_eq!(
            evaluate(invalid, &normal).unwrap_err().kind,
            "invalid_request"
        );
        assert_eq!(
            evaluate(input, &AtomicBool::new(true)).unwrap_err().kind,
            "cancelled"
        );
    }
}
