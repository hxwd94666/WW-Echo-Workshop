// 配装预检和保存前业务重验，所有操作仅返回证据而不写入。
use crate::allocation_model::{Build, Input, Problem};
use crate::objective::Objective;
use crate::{ComputeError, ComputeResult, checkpoint};
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::sync::atomic::AtomicBool;

pub fn precheck(payload: Value, cancel: &AtomicBool) -> ComputeResult<Value> {
    let input: Input = serde_json::from_value(payload)
        .map_err(|e| ComputeError::new("invalid_request", e.to_string()))?;
    let profile = input.request.profile_id.clone();
    let fully_tuned = input.request.fully_tuned;
    let fake = vec![vec![Some(0); input.records.len()]; input.request.roles.len()];
    let problem = Problem::compile(input, Some(fake), cancel, true)?;
    let mut rows = Vec::new();
    for (r, role) in problem.roles.iter().enumerate() {
        checkpoint(cancel)?;
        let rejected = problem.diagnostics[r];
        let confirmed = problem.records.len() - rejected[0];
        let tuned = confirmed - rejected[1];
        let matched = tuned - rejected[2];
        let protected = matched - rejected[3];
        let sets: Vec<_> = problem.requirements[r]
            .iter()
            .enumerate()
            .map(|(j, (set, minimum))| {
                let available = problem
                    .records
                    .iter()
                    .enumerate()
                    .filter(|(i, _)| problem.allowed[r][*i] && problem.sets[*i] == *set)
                    .map(|(i, _)| problem.names[i])
                    .collect::<HashSet<_>>()
                    .len();
                json!([role.set_requirements[j].0, minimum, available])
            })
            .collect();
        let slots: Vec<_> = (0..5)
            .map(|slot| {
                (0..problem.records.len())
                    .filter(|i| problem.masks[r][*i] & (1 << slot) != 0)
                    .map(|i| &problem.records[i].item_id)
                    .collect::<Vec<_>>()
            })
            .collect();
        rows.push(json!({"key":role.key(),"label":problem.labels[r],"total":problem.records.len(),
            "confirmed":confirmed,"tuned":tuned,"matched":matched,"protected":protected,"slot_ids":slots,
            "costs":role.slots.iter().map(|s|s.cost).collect::<Vec<_>>(),"sets":sets}));
    }
    Ok(
        json!({"profile_id":profile,"fully_tuned":fully_tuned,"pending":problem.records.iter().filter(|r|!r.usable()).count(),
        "missing_pins":problem.missing_pins,"roles":rows}),
    )
}

pub fn validate(payload: Value, cancel: &AtomicBool) -> ComputeResult<Value> {
    let completed: Vec<String> =
        serde_json::from_value(payload.get("completed").cloned().unwrap_or(json!([])))
            .map_err(|e| ComputeError::new("invalid_evidence", e.to_string()))?;
    let rows = payload["allocations"]
        .as_array()
        .ok_or_else(|| ComputeError::new("invalid_request", "缺少明确分配"))?
        .clone();
    let require_complete = match payload.get("require_complete") {
        None => false,
        Some(value) => value
            .as_bool()
            .ok_or_else(|| ComputeError::new("invalid_request", "完整分配校验标记须为布尔值"))?,
    };
    let input: Input = serde_json::from_value(payload)
        .map_err(|e| ComputeError::new("invalid_request", e.to_string()))?;
    let templates: Vec<_> = input
        .request
        .roles
        .iter()
        .map(|role| {
            input
                .templates
                .get(&format!("{}/{}", role.character_id, role.modal_id))
                .cloned()
        })
        .collect();
    // An incomplete frozen backup may omit an unassigned role's fixed item.
    // Complete rows still have to satisfy every fixed slot and UUID constraint.
    let p = Problem::compile(input, None, cancel, true)?;
    if rows.len() != p.roles.len() {
        return Err(ComputeError::new(
            "invalid_allocation",
            "分配角色数量不一致",
        ));
    }
    let by_id: HashMap<_, _> = p
        .records
        .iter()
        .enumerate()
        .map(|(i, r)| (r.item_id.as_str(), i))
        .collect();
    let mut used = HashSet::new();
    let mut totals = Vec::new();
    let mut builds = Vec::<Build>::new();
    let mut output = Vec::new();
    let mut summaries = Vec::new();
    for (r, row) in rows.iter().enumerate() {
        checkpoint(cancel)?;
        if row["character_id"].as_str() != Some(&p.roles[r].character_id)
            || row["modal_id"].as_str() != Some(&p.roles[r].modal_id)
        {
            return Err(ComputeError::new(
                "invalid_allocation",
                "分配角色身份不一致",
            ));
        }
        let ids: Vec<String> = serde_json::from_value(row["item_ids"].clone())
            .map_err(|e| ComputeError::new("invalid_allocation", e.to_string()))?;
        if ids.len() != 5 && (!ids.is_empty() || require_complete) {
            return Err(ComputeError::new(
                "invalid_allocation",
                "分配须为空或完整五件",
            ));
        }
        let mut indices = Vec::new();
        for (slot, id) in ids.iter().enumerate() {
            let index = *by_id.get(id.as_str()).ok_or_else(|| {
                ComputeError::new("invalid_allocation", "分配 UUID 不在冻结候选中")
            })?;
            if !used.insert(index) || p.masks[r][index] & (1 << slot) == 0 {
                return Err(ComputeError::new(
                    "invalid_allocation",
                    "分配违反槽位、固定件或保护约束",
                ));
            }
            indices.push(index);
        }
        if !indices.is_empty() && !p.legal_names_sets(r, &indices) {
            return Err(ComputeError::new(
                "invalid_allocation",
                "分配违反套装或同名约束",
            ));
        }
        let build = p.build(r, indices);
        let scores: Vec<_> = build.ids.iter().map(|i| p.scores[r][*i].unwrap()).collect();
        if row
            .get("label")
            .is_some_and(|label| label.as_str() != Some(p.labels[r].as_str()))
        {
            return Err(ComputeError::new(
                "invalid_evidence",
                "冻结角色名称与模板不一致",
            ));
        }
        for (key, expected) in [
            ("total_cents", json!(build.total)),
            ("target_cents", json!(p.targets[r])),
            ("moves", json!(build.moves)),
            ("scores", json!(scores)),
        ] {
            if row.get(key).is_some_and(|value| *value != expected) {
                return Err(ComputeError::new(
                    "invalid_evidence",
                    format!("冻结证据 {key} 不一致"),
                ));
            }
        }
        output.push(json!({"character_id":p.roles[r].character_id,"modal_id":p.roles[r].modal_id,"item_ids":ids,
            "scores":scores,"total_cents":build.total,"target_cents":p.targets[r],"moves":build.moves}));
        let rating = templates[r]
            .as_ref()
            .and_then(|template| crate::rating::loadout(template, &scores));
        summaries.push(
            json!({"character_id":p.roles[r].character_id,"modal_id":p.roles[r].modal_id,
            "label":p.labels[r],"total_cents":build.total,"target_cents":p.targets[r],
            "item_count":scores.len(),"rating":rating}),
        );
        totals.push(build.total);
        builds.push(build);
    }
    let value = Objective::new(
        &totals,
        &p.targets,
        builds.iter().filter(|b| b.ids.len() == 5).count(),
        builds.iter().map(|b| b.moves).sum(),
    );
    let mut done = HashSet::new();
    for id in &completed {
        if !done.insert(id)
            || !by_id
                .get(id.as_str())
                .is_some_and(|index| used.contains(index))
        {
            return Err(ComputeError::new(
                "invalid_evidence",
                "换装进度引用重复或未分配物品",
            ));
        }
    }
    Ok(
        json!({"roles":output,"objective":value.as_json(),"model_objective":value.as_model_json(&totals,&p.targets),"minimum_ratio":[value.numerator,value.denominator],
            "summaries":summaries,"completed_count":done.len(),"item_count":used.len()}),
    )
}
