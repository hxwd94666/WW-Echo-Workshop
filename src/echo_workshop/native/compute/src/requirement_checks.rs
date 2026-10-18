// 统一校验要求布局、主词条和套装总量；可随既有匹配/固定件请求执行。
use crate::domain::normalize;
use crate::{ComputeError, ComputeResult, checkpoint, constants};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::HashSet;
use std::sync::atomic::AtomicBool;

pub fn layout(costs: &[u8], main_names: &[Vec<String>]) -> Result<(), String> {
    if costs.len() != 5 || costs.iter().any(|cost| ![1, 3, 4].contains(cost)) {
        return Err("请为五个槽位各选择 COST 1、3 或 4。".into());
    }
    let total: u32 = costs.iter().map(|cost| u32::from(*cost)).sum();
    if total > 12 {
        return Err(format!("五槽位 COST 合计 {total}，不得超过 12。"));
    }
    if main_names.len() != 5 {
        return Err("主词条要求须对应五个槽位；空列表表示该槽不限。".into());
    }
    for (slot, (&cost, names)) in costs.iter().zip(main_names).enumerate() {
        let valid = &constants()["max_main"][cost.to_string()][0];
        if names.iter().any(|name| valid.get(name).is_none()) {
            return Err(format!(
                "第 {} 槽主词条要求无效：主词条要求与槽位 COST 不兼容",
                slot + 1
            ));
        }
        if names.iter().collect::<HashSet<_>>().len() != names.len() {
            return Err(format!("第 {} 槽主词条要求无效：重复主词条要求", slot + 1));
        }
    }
    Ok(())
}

pub fn sets(allowed: &[String], rows: &[(String, usize)]) -> Result<(), String> {
    if allowed.len() > 512 || allowed.iter().any(|name| name.trim().is_empty()) {
        return Err("允许套装须为明确名称列表。".into());
    }
    let canonical: HashSet<_> = allowed.iter().map(|name| normalize(name)).collect();
    if canonical.len() != allowed.len() {
        return Err("允许套装名称重复。".into());
    }
    if rows.len() > 5 {
        return Err("套装件数要求须为列表，总件数不超过五件。".into());
    }
    let mut seen = HashSet::new();
    let mut total = 0;
    for (name, count) in rows {
        let normalized = normalize(name);
        if name.trim().is_empty()
            || !canonical.contains(&normalized)
            || !seen.insert(normalized)
            || !(1..=5).contains(count)
        {
            return Err("套装件数须为 1–5，名称不重复且已列入允许套装。".into());
        }
        total += count;
    }
    if total > 5 {
        return Err("套装最少件数合计超过五件。".into());
    }
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Model {
    costs: Vec<u8>,
    main_names: Vec<Vec<String>>,
    allowed_sonatas: Vec<String>,
    set_requirements: Vec<(String, usize)>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Input {
    identity: String,
    models: Vec<Model>,
}

pub fn evaluate(payload: Value, cancel: &AtomicBool) -> ComputeResult<Value> {
    let input: Input = serde_json::from_value(payload)
        .map_err(|e| ComputeError::new("invalid_request", e.to_string()))?;
    if input.models.len() > 512 || input.identity.is_empty() || input.identity.len() > 128 {
        return Err(ComputeError::new("input_limit", "要求校验规模或身份错误"));
    }
    let mut errors = Vec::with_capacity(input.models.len());
    for model in &input.models {
        checkpoint(cancel)?;
        errors.push(
            layout(&model.costs, &model.main_names)
                .and_then(|_| sets(&model.allowed_sonatas, &model.set_requirements))
                .err(),
        );
    }
    Ok(json!({"identity":input.identity,"errors":errors}))
}
