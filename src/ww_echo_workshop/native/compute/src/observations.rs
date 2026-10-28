// 计算冻结收录词条的 COST 推断、档位及显式模板下的评分，不处理 OCR 几何。
use crate::scoring::{Stat, observed_score};
use crate::{ComputeError, ComputeResult, checkpoint, constants};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::HashSet;
use std::sync::atomic::AtomicBool;

#[derive(Deserialize)]
struct Selection {
    character_id: String,
    modal_id: String,
    template: Value,
}

#[derive(Deserialize)]
struct Input {
    weight_version: String,
    cost: Option<u8>,
    cost_key: Option<String>,
    main_rows: Vec<Stat>,
    sub_rows: Vec<Stat>,
    selections: Vec<Selection>,
}

fn inferred_cost(main: &[Stat]) -> ComputeResult<u8> {
    let first = main
        .first()
        .ok_or_else(|| ComputeError::new("invalid_request", "推断 COST 需要明确的首主词条"))?;
    if first.stat_name.contains("伤害加成") || first.stat_name == "共鸣效率" {
        return Ok(3);
    }
    if ["暴击", "暴击伤害"].contains(&first.stat_name.as_str()) || first.stat_name.contains("治疗")
    {
        return Ok(4);
    }
    let targets = if first.stat_name == "防御%" {
        [22.8, 38.0, 41.5]
    } else {
        [18.0, 30.0, 33.0]
    };
    let mut closest = 0;
    for i in 1..targets.len() {
        if (first.value - targets[i]).abs() < (first.value - targets[closest]).abs() {
            closest = i;
        }
    }
    Ok([1, 3, 4][closest])
}

fn cost_key(cost: u8, main: &[Stat]) -> String {
    if cost != 3 {
        return format!("{cost}C");
    }
    let name = main.first().map_or("", |r| r.stat_name.as_str());
    if name.contains("伤害加成")
        && constants()["elements"]
            .as_array()
            .unwrap()
            .iter()
            .any(|element| name.contains(element.as_str().unwrap()))
    {
        "3C属伤".into()
    } else if name.contains("攻击") {
        "3C攻击".into()
    } else {
        "3C其它".into()
    }
}

fn tier(stat: &Stat) -> Value {
    let Some(tiers) = constants()["tiers"][&stat.stat_name].as_array() else {
        return Value::Null;
    };
    let mut closest = 0;
    for i in 1..tiers.len() {
        if (stat.value - tiers[i].as_f64().unwrap()).abs()
            < (stat.value - tiers[closest].as_f64().unwrap()).abs()
        {
            closest = i;
        }
    }
    json!({"index":closest+1,"total":tiers.len(),"kind":
        if closest==0 { "lowest" } else if closest+1==tiers.len() { "highest" } else { "normal" }})
}

pub fn evaluate(payload: Value, cancel: &AtomicBool) -> ComputeResult<Value> {
    checkpoint(cancel)?;
    let input: Input = serde_json::from_value(payload)
        .map_err(|e| ComputeError::new("invalid_request", e.to_string()))?;
    if input.weight_version.is_empty()
        || input.selections.len() > 128
        || input.main_rows.len() > 16
        || input.sub_rows.len() > 16
        || input.cost.is_some_and(|cost| ![1, 3, 4].contains(&cost))
        || input.cost_key.as_ref().is_some_and(|key| key.len() > 128)
    {
        return Err(ComputeError::new(
            "input_limit",
            "观测输入超出明确结构或规模限额",
        ));
    }
    if input
        .main_rows
        .iter()
        .chain(&input.sub_rows)
        .any(|s| !s.value.is_finite())
    {
        return Err(ComputeError::new("invalid_numeric", "观测数值须为有限值"));
    }
    let cost = if let Some(cost) = input.cost {
        cost
    } else {
        inferred_cost(&input.main_rows)?
    };
    let key = input
        .cost_key
        .unwrap_or_else(|| cost_key(cost, &input.main_rows));
    let mut seen = HashSet::new();
    let mut scores = Vec::new();
    for selection in input.selections {
        checkpoint(cancel)?;
        if selection.character_id.is_empty()
            || selection.modal_id.is_empty()
            || !selection.template.is_object()
            || !seen.insert((selection.character_id.clone(), selection.modal_id.clone()))
        {
            return Err(ComputeError::new(
                "invalid_request",
                "观测模板身份重复或缺失",
            ));
        }
        let mut score =
            observed_score(cost, &input.main_rows, &input.sub_rows, &selection.template)?;
        if let Some(score) = &mut score {
            score.cost_key.clone_from(&key);
        }
        scores.push(json!({"character_id":selection.character_id,"modal_id":selection.modal_id,"score":score}));
    }
    let tiers: Vec<_> = input.sub_rows.iter().map(tier).collect();
    checkpoint(cancel)?;
    Ok(
        json!({"weight_version":input.weight_version,"cost":cost,"cost_key":key,"scores":scores,"tiers":tiers}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn inference_and_tier_ties_keep_first_value_and_unknown_is_not_a_tier() {
        let stat = |name: &str, value| Stat {
            stat_name: name.into(),
            value,
        };
        assert_eq!(inferred_cost(&[stat("攻击%", 24.0)]).unwrap(), 1);
        assert_eq!(inferred_cost(&[stat("防御%", 30.4)]).unwrap(), 1);
        assert_eq!(inferred_cost(&[stat("治疗效果加成", 1.0)]).unwrap(), 4);
        assert_eq!(inferred_cost(&[stat("热熔伤害加成", 1.0)]).unwrap(), 3);
        assert_eq!(tier(&stat("攻击", 35.0))["index"], 1);
        assert_eq!(tier(&stat("未知", 35.0)), Value::Null);
        assert_eq!(cost_key(3, &[stat("热熔伤害加成", 30.0)]), "3C属伤");
        assert!(inferred_cost(&[]).is_err());
    }
}
