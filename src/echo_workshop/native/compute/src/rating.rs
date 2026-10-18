// 根据明确冻结模板计算单件与五件评级，不读取当前资源。
use crate::numeric::{decimal_scale, python_float};
use crate::{ComputeError, ComputeResult, checkpoint};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::sync::atomic::AtomicBool;

const LABELS: [&str; 6] = ["C", "B", "A", "S", "SS", "SSS"];
const POLICY: &str = "xwuid-published-grade-lower-bounds-v1";

#[derive(Clone, Serialize)]
pub struct Standard {
    pub scale: u32,
    pub thresholds: Vec<f64>,
    pub cost: Option<u8>,
    pub standard_id: String,
    pub source: String,
}

#[derive(Serialize)]
pub struct Rating {
    pub label: &'static str,
    pub score: f64,
    #[serde(flatten)]
    pub standard: Standard,
}

fn table(values: &Value) -> Option<Vec<f64>> {
    let values: Vec<f64> = serde_json::from_value(values.clone()).ok()?;
    if values.len() != 6
        || values[0] != 0.0
        || values
            .iter()
            .any(|v| !v.is_finite() || !(0.0..=1.0).contains(v))
        || values.windows(2).any(|p| p[0] >= p[1])
    {
        return None;
    }
    Some(values)
}

pub fn standard(template: &Value, cost: Option<u8>) -> Option<Standard> {
    let (ratios, scale, source) = if let Some(cost) = cost {
        let position = [1, 3, 4].iter().position(|c| *c == cost)?;
        let rows = template.get("props_grade")?.as_array()?;
        if rows.len() != 3 || rows.iter().any(|r| table(r).is_none()) {
            return None;
        }
        (table(&rows[position])?, 50, "XWUID props_grade")
    } else {
        (
            table(template.get("total_grade")?)?,
            250,
            "XWUID total_grade",
        )
    };
    let thresholds = ratios
        .iter()
        .map(|v| decimal_scale(*v, scale))
        .collect::<Result<Vec<_>, _>>()
        .ok()?;
    let text = format!(
        "[\"{POLICY}\",\"{source}\",{},{scale},[{}]]",
        cost.map_or_else(|| "null".into(), |c| c.to_string()),
        ratios
            .iter()
            .map(|v| python_float(*v))
            .collect::<Vec<_>>()
            .join(",")
    );
    let digest = format!("{:x}", Sha256::digest(text.as_bytes()));
    Some(Standard {
        scale,
        thresholds,
        cost,
        standard_id: digest[..16].into(),
        source: source.into(),
    })
}

pub fn rate(standard: &Standard, score: f64) -> Option<Rating> {
    if !score.is_finite() || !(0.0..=1e12).contains(&score) {
        return None;
    }
    let index = standard
        .thresholds
        .iter()
        .rposition(|lower| score >= *lower)?;
    Some(Rating {
        label: LABELS[index],
        score,
        standard: standard.clone(),
    })
}

pub(crate) fn loadout_points(scores: &[i64]) -> Option<f64> {
    if scores.len() != 5 || scores.iter().any(|v| *v < 0) {
        return None;
    }
    let total: i64 = scores
        .iter()
        .map(|v| if *v > 4995 { 5000 } else { *v })
        .sum();
    Some(if total > 24990 {
        250.0
    } else {
        total as f64 / 100.0
    })
}

pub fn loadout(template: &Value, scores: &[i64]) -> Option<Rating> {
    loadout_points(scores)
        .and_then(|points| standard(template, None).and_then(|s| rate(&s, points)))
}

#[derive(Deserialize)]
struct RatingInput {
    template: Value,
    #[serde(default)]
    cost: Option<u8>,
    #[serde(default)]
    score: Option<f64>,
    #[serde(default)]
    scores_cents: Option<Vec<i64>>,
}

pub fn batch(payload: Value, cancelled: &AtomicBool) -> ComputeResult<Value> {
    let inputs: Vec<RatingInput> =
        serde_json::from_value(payload.get("items").cloned().unwrap_or(Value::Null))
            .map_err(|e| ComputeError::new("invalid_request", e.to_string()))?;
    let mut result = Vec::with_capacity(inputs.len());
    for input in inputs {
        checkpoint(cancelled)?;
        let points = if let Some(scores) = input.scores_cents {
            loadout_points(&scores)
        } else {
            input.score
        };
        let rating =
            standard(&input.template, input.cost).and_then(|s| points.and_then(|p| rate(&s, p)));
        result.push(json!(rating));
    }
    Ok(json!({"items": result}))
}
