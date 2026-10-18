// 投影自定义权重、构造五十分归一化上限并验证显式编辑，不读写资料文件。
use crate::scoring::weight;
use crate::{ComputeError, ComputeResult, checkpoint, constants};
use serde::Deserialize;
use serde_json::{Map, Value, json};
use std::sync::atomic::AtomicBool;

const MAIN_NAMES: [&str; 10] = [
    "攻击%",
    "生命%",
    "防御%",
    "生命",
    "共鸣效率",
    "属性伤害加成",
    "攻击",
    "暴击",
    "暴击伤害",
    "治疗效果加成",
];
const SUB_NAMES: [&str; 13] = [
    "暴击",
    "暴击伤害",
    "攻击%",
    "防御%",
    "生命%",
    "攻击",
    "防御",
    "生命",
    "共鸣效率",
    "普攻",
    "重击",
    "共鸣技能",
    "共鸣解放",
];
const SKILLS: [&str; 4] = ["普攻", "重击", "共鸣技能", "共鸣解放"];

#[derive(Deserialize)]
struct Input {
    mode: String,
    identity: Vec<String>,
    weight_version: String,
    original: Value,
    main: Option<Value>,
    sub: Option<Value>,
    edited: Option<Value>,
}

fn invalid(message: &str) -> ComputeError {
    ComputeError::new("invalid_custom_weights", message)
}

fn main_name(name: &str) -> &str {
    if name.strip_suffix("伤害加成").is_some_and(|prefix| {
        constants()["elements"]
            .as_array()
            .unwrap()
            .iter()
            .any(|v| v.as_str() == Some(prefix))
    }) {
        "属性伤害加成"
    } else {
        name
    }
}

fn editable(template: &Value) -> (Value, Value) {
    let mut main = Map::new();
    for cost in [1, 3, 4] {
        let mut values = Map::new();
        for name in MAIN_NAMES {
            if constants()["max_main"][cost.to_string()]
                .as_array()
                .unwrap()
                .iter()
                .any(|slot| {
                    slot.as_object()
                        .unwrap()
                        .keys()
                        .any(|raw| main_name(raw) == name)
                })
            {
                values.insert(
                    name.into(),
                    json!(
                        template["main_props"][cost.to_string()][name]
                            .as_f64()
                            .unwrap_or(0.0)
                    ),
                );
            }
        }
        main.insert(cost.to_string(), Value::Object(values));
    }
    let sub = SUB_NAMES
        .into_iter()
        .map(|name| (name.into(), json!(weight(template, 4, name, false))))
        .collect::<Map<_, _>>();
    (Value::Object(main), Value::Object(sub))
}

fn same_keys(left: &Value, right: &Value) -> bool {
    match (left.as_object(), right.as_object()) {
        (Some(a), Some(b)) => a.len() == b.len() && a.keys().all(|k| b.contains_key(k)),
        _ => false,
    }
}

fn sum_frozen(values: impl IntoIterator<Item = f64>) -> f64 {
    // Preserve the current CPython 3.12 frozen reference's compensated sum,
    // including the separate main/sub sums and final addition (no fused ops).
    let (mut high, mut low) = (0.0_f64, 0.0_f64);
    for value in values {
        let next = high + value;
        low += if high.abs() >= value.abs() {
            (high - next) + value
        } else {
            (value - next) + high
        };
        high = next;
    }
    if low != 0.0 && low.is_finite() {
        high + low
    } else {
        high
    }
}

fn maxima(template: &Value) -> ComputeResult<Value> {
    let mut sub = SUB_NAMES
        .iter()
        .map(|name| {
            constants()["max_sub"][*name].as_f64().unwrap() * weight(template, 4, name, false)
        })
        .collect::<Vec<_>>();
    if sub.iter().any(|v| !v.is_finite()) {
        return Err(invalid("请至少为可用词条设置一个大于 0 的权重"));
    }
    sub.sort_by(|a, b| b.total_cmp(a));
    let sub_total = sum_frozen(sub.into_iter().take(5));
    let mut output = Vec::new();
    for cost in [1, 3, 4] {
        let values = constants()["max_main"][cost.to_string()]
            .as_array()
            .unwrap()
            .iter()
            .map(|slot| {
                slot.as_object()
                    .unwrap()
                    .iter()
                    .map(|(name, value)| {
                        value.as_f64().unwrap() * weight(template, cost, name, true)
                    })
                    .max_by(f64::total_cmp)
                    .unwrap()
            });
        let maximum = sum_frozen(values) + sub_total;
        if !maximum.is_finite() || !(1e-9..=1e12).contains(&maximum) {
            return Err(invalid("请至少为可用词条设置一个大于 0 的权重"));
        }
        output.push(maximum);
    }
    Ok(json!(output))
}

fn build(original: &Value, main: &Value, sub: &Value) -> ComputeResult<Value> {
    let (expected_main, expected_sub) = editable(original);
    if !same_keys(main, &expected_main)
        || !same_keys(sub, &expected_sub)
        || ["1", "3", "4"]
            .iter()
            .any(|cost| !same_keys(&main[*cost], &expected_main[*cost]))
    {
        return Err(invalid("请填写完整的主词条和副词条权重"));
    }
    let values = main
        .as_object()
        .unwrap()
        .values()
        .flat_map(|v| v.as_object().unwrap().values())
        .chain(sub.as_object().unwrap().values());
    if values.into_iter().any(|v| {
        v.as_f64()
            .is_none_or(|n| !n.is_finite() || !(0.0..=1e6).contains(&n))
    }) {
        return Err(invalid("权重须为 0 到 1000000 之间的数字"));
    }
    let mut template = original.clone();
    for (cost, rows) in main.as_object().unwrap() {
        for (name, value) in rows.as_object().unwrap() {
            template["main_props"][cost][name] = value.clone();
        }
    }
    for (name, value) in sub.as_object().unwrap() {
        if !SKILLS.contains(&name.as_str()) {
            template["sub_props"][name] = value.clone();
        }
    }
    template["sub_props"]["技能伤害加成"] = json!(1);
    template["skill_weight"] = json!(SKILLS.map(|name| sub[name].clone()));
    template["score_max"] = maxima(&template)?;
    Ok(template)
}

fn same_values(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::Number(a), Value::Number(b)) => {
            let integer = |n: &serde_json::Number| {
                n.as_i64()
                    .map(i128::from)
                    .or_else(|| n.as_u64().map(i128::from))
            };
            match (integer(a), integer(b)) {
                (Some(a), Some(b)) => a == b,
                (None, None) => a.as_f64() == b.as_f64(),
                (Some(value), None) | (None, Some(value)) => {
                    let float = if a.is_f64() {
                        a.as_f64().unwrap()
                    } else {
                        b.as_f64().unwrap()
                    };
                    float.is_finite()
                        && float.abs() < 2.0_f64.powi(127)
                        && float.fract() == 0.0
                        && float as i128 == value
                }
            }
        }
        (Value::Array(a), Value::Array(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(a, b)| same_values(a, b))
        }
        (Value::Object(a), Value::Object(b)) => {
            a.len() == b.len()
                && a.iter()
                    .all(|(k, v)| b.get(k).is_some_and(|other| same_values(v, other)))
        }
        _ => left == right,
    }
}

pub fn evaluate(payload: Value, cancel: &AtomicBool) -> ComputeResult<Value> {
    checkpoint(cancel)?;
    let input: Input = serde_json::from_value(payload)
        .map_err(|e| ComputeError::new("invalid_request", e.to_string()))?;
    if input.identity.len() != 2
        || input.identity.iter().any(|v| v.is_empty())
        || input.weight_version.is_empty()
        || !input.original.is_object()
    {
        return Err(ComputeError::new(
            "invalid_request",
            "自定义权重需要明确模板和资料身份",
        ));
    }
    if !input.original["sub_props"].is_object()
        || ["1", "3", "4"]
            .iter()
            .any(|cost| !input.original["main_props"][*cost].is_object())
        || input.original["skill_weight"]
            .as_array()
            .is_none_or(|rows| rows.len() != 4)
    {
        return Err(invalid("自定义权重须保留角色、模版名称和模版结构"));
    }
    let mut output = json!({"identity":input.identity,"weight_version":input.weight_version});
    match input.mode.as_str() {
        "project" => {
            let (main, sub) = editable(&input.original);
            output["main"] = main;
            output["sub"] = sub;
        }
        "build" => {
            output["template"] = build(
                &input.original,
                &input.main.unwrap_or(Value::Null),
                &input.sub.unwrap_or(Value::Null),
            )?;
        }
        "validate" => {
            let edited = input.edited.unwrap_or(Value::Null);
            if !same_keys(&input.original, &edited) || edited["name"] != input.original["name"] {
                return Err(invalid("自定义权重须保留角色、模版名称和模版结构"));
            }
            let (main, sub) = editable(&edited);
            let expected = build(&input.original, &main, &sub)?;
            if !same_values(&edited, &expected) {
                return Err(invalid("自定义权重包含未支持的字段或评分上限不一致"));
            }
            output["template"] = edited;
        }
        _ => return Err(ComputeError::new("invalid_request", "未知自定义权重操作")),
    }
    checkpoint(cancel)?;
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn frozen_sum_retains_small_positive_terms_and_numeric_json_equality() {
        assert_eq!(sum_frozen([1e16, 1.0, 1.0]), 10000000000000002.0);
        assert!(same_values(&json!({"a":[1,2.0]}), &json!({"a":[1.0,2]})));
        assert!(!same_values(&json!(true), &json!(1)));
        assert!(!same_values(
            &json!(9007199254740993_u64),
            &json!(9007199254740992.0)
        ));
        assert!(!same_values(&json!(u64::MAX), &json!(u64::MAX - 1)));
    }
}
