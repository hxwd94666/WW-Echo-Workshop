// 批量计算冻结模板下的词条贡献、当前分和潜力分。
use crate::numeric::entry_cents;
use crate::rating::{Rating, Standard, rate, standard};
use crate::{ComputeError, ComputeResult, checkpoint, constants};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::HashMap;
use std::sync::atomic::AtomicBool;

#[derive(Clone, Deserialize, Serialize)]
pub struct Stat {
    pub stat_name: String,
    pub value: f64,
}

#[derive(Deserialize)]
pub struct ScoreInput {
    pub template_key: String,
    pub cost: u8,
    pub cost_key: String,
    pub main_rows: Vec<Stat>,
    pub sub_rows: Vec<Stat>,
}

#[derive(Serialize)]
pub struct Score {
    pub cost: u8,
    pub cost_key: String,
    pub row_scores: Vec<f64>,
    pub current_score: f64,
    pub potential_score: f64,
    pub current_cents: i64,
    pub potential_cents: i64,
    pub rating: Option<Rating>,
}

pub(crate) struct Prepared {
    maximum: f64,
    main: HashMap<String, f64>,
    sub: HashMap<String, f64>,
    standard: Option<Standard>,
}

pub(crate) fn weight(template: &Value, cost: u8, name: &str, main: bool) -> f64 {
    if main {
        let element = name.strip_suffix("伤害加成").filter(|n| {
            constants()["elements"]
                .as_array()
                .unwrap()
                .iter()
                .any(|e| e.as_str() == Some(*n))
        });
        let key = if let Some(element) = element {
            let known = template["_character_element"].as_str().unwrap_or("");
            if (!known.is_empty() && element != known)
                || (known.is_empty()
                    && template["_require_character_element"].as_bool() == Some(true))
            {
                return 0.0;
            }
            "属性伤害加成"
        } else {
            name
        };
        return template["main_props"][cost.to_string()][key]
            .as_f64()
            .unwrap_or(0.0);
    }
    let name = match name {
        "普攻" => "普攻伤害加成",
        "重击" => "重击伤害加成",
        "共鸣技能" => "共鸣技能伤害加成",
        "共鸣解放" => "共鸣解放伤害加成",
        _ => name,
    };
    if let Some(index) = constants()["skills"][name].as_u64() {
        return template["sub_props"]["技能伤害加成"]
            .as_f64()
            .unwrap_or(0.0)
            * template["skill_weight"][index as usize]
                .as_f64()
                .unwrap_or(0.0);
    }
    template["sub_props"][name].as_f64().unwrap_or(0.0)
}

pub(crate) fn prepare(template: &Value, cost: u8) -> Option<Prepared> {
    let position = [1, 3, 4].iter().position(|c| *c == cost)?;
    let maximum = template["score_max"][position].as_f64()?;
    if !maximum.is_finite() || maximum <= 0.0 {
        return None;
    }
    let mut main = HashMap::new();
    for slot in constants()["max_main"][cost.to_string()].as_array()? {
        for name in slot.as_object()?.keys() {
            main.insert(name.clone(), weight(template, cost, name, true));
        }
    }
    let mut sub = HashMap::new();
    for name in constants()["max_sub"]
        .as_object()?
        .keys()
        .chain(constants()["skills"].as_object()?.keys())
    {
        sub.insert(name.clone(), weight(template, cost, name, false));
    }
    Some(Prepared {
        maximum,
        main,
        sub,
        standard: standard(template, Some(cost)),
    })
}

pub(crate) fn score(
    input: ScoreInput,
    template: &Value,
    prepared: &Prepared,
) -> ComputeResult<Score> {
    if input.main_rows.len() != 2 || input.sub_rows.len() > 5 {
        return Err(ComputeError::new(
            "invalid_request",
            "评分需要两个主词条及最多五个副词条",
        ));
    }
    let mut cents = Vec::with_capacity(7);
    for (index, row) in input.main_rows.iter().enumerate() {
        let value = constants()["max_main"][input.cost.to_string()][index][&row.stat_name]
            .as_f64()
            .unwrap_or(row.value);
        let w = prepared
            .main
            .get(&row.stat_name)
            .copied()
            .unwrap_or_else(|| weight(template, input.cost, &row.stat_name, true));
        cents.push(entry_cents(value, w, prepared.maximum)?);
    }
    for row in &input.sub_rows {
        let w = prepared
            .sub
            .get(&row.stat_name)
            .copied()
            .unwrap_or_else(|| weight(template, input.cost, &row.stat_name, false));
        cents.push(entry_cents(row.value, w, prepared.maximum)?);
    }
    let current: i64 = cents.iter().sum();
    let mut unrolled = Vec::new();
    if input.sub_rows.len() < 5 {
        for (name, value) in constants()["max_sub"].as_object().unwrap() {
            if !input.sub_rows.iter().any(|row| row.stat_name == *name) {
                unrolled.push(entry_cents(
                    value.as_f64().unwrap(),
                    prepared.sub[name],
                    prepared.maximum,
                )?);
            }
        }
    }
    unrolled.sort_unstable_by(|a, b| b.cmp(a));
    let potential: i64 = current + unrolled.iter().take(5 - input.sub_rows.len()).sum::<i64>();
    Ok(Score {
        cost: input.cost,
        cost_key: input.cost_key,
        row_scores: cents.iter().map(|v| *v as f64 / 100.0).collect(),
        current_score: current as f64 / 100.0,
        potential_score: potential as f64 / 100.0,
        current_cents: current,
        potential_cents: potential,
        rating: prepared
            .standard
            .as_ref()
            .and_then(|s| rate(s, current as f64 / 100.0)),
    })
}

pub(crate) fn observed_current(
    cost: u8,
    main: &[Stat],
    sub: &[Stat],
    template: &Value,
    prepared: &Prepared,
) -> ComputeResult<i64> {
    Ok(observed_cents(cost, main, sub, template, prepared)?
        .iter()
        .sum())
}

fn observed_cents(
    cost: u8,
    main: &[Stat],
    sub: &[Stat],
    template: &Value,
    prepared: &Prepared,
) -> ComputeResult<Vec<i64>> {
    // Rejected observations still show the existing scalar preview. Their values
    // never make a record usable; normal scoring keeps its strict two/five schema.
    let mut total = 0_i64;
    let mut rows = Vec::with_capacity(main.len() + sub.len());
    for (index, row) in main.iter().chain(sub).enumerate() {
        let value = if index < main.len() {
            constants()["max_main"][cost.to_string()][index][&row.stat_name]
                .as_f64()
                .unwrap_or(row.value)
        } else {
            row.value
        };
        let contribution = crate::numeric::observed_entry_cents(
            value,
            weight(template, cost, &row.stat_name, index < 2),
            prepared.maximum,
        )?;
        total = total
            .checked_add(contribution)
            .ok_or_else(|| ComputeError::new("numeric_limit", "观测分数累计超出限额"))?;
        rows.push(contribution);
    }
    if total.unsigned_abs() > crate::numeric::ECHO_CENTS_LIMIT as u64 {
        return Err(ComputeError::new("numeric_limit", "观测分数累计超出限额"));
    }
    Ok(rows)
}

pub(crate) fn observed_score(
    cost: u8,
    main: &[Stat],
    sub: &[Stat],
    template: &Value,
) -> ComputeResult<Option<Score>> {
    if main.len() > 16 || sub.len() > 16 {
        return Err(ComputeError::new("input_limit", "观测词条超出单件限额"));
    }
    let Some(prepared) = prepare(template, cost) else {
        return Ok(None);
    };
    let cents = observed_cents(cost, main, sub, template, &prepared)?;
    let current: i64 = cents.iter().sum();
    let mut unrolled = Vec::new();
    for (name, value) in constants()["max_sub"].as_object().unwrap() {
        if !sub.iter().any(|row| row.stat_name == *name) {
            unrolled.push(crate::numeric::observed_entry_cents(
                value.as_f64().unwrap(),
                weight(template, cost, name, false),
                prepared.maximum,
            )?);
        }
    }
    unrolled.sort_unstable_by(|a, b| b.cmp(a));
    let potential = current
        + unrolled
            .iter()
            .take(5_usize.saturating_sub(sub.len()))
            .sum::<i64>();
    if potential.unsigned_abs() > crate::numeric::ECHO_CENTS_LIMIT as u64 {
        return Err(ComputeError::new("numeric_limit", "观测潜力分超出限额"));
    }
    Ok(Some(Score {
        cost,
        cost_key: cost.to_string(),
        row_scores: cents.iter().map(|v| *v as f64 / 100.0).collect(),
        current_score: current as f64 / 100.0,
        potential_score: potential as f64 / 100.0,
        current_cents: current,
        potential_cents: potential,
        rating: prepared
            .standard
            .as_ref()
            .and_then(|s| rate(s, current as f64 / 100.0)),
    }))
}

pub fn batch(payload: Value, cancelled: &AtomicBool) -> ComputeResult<Value> {
    let templates = payload
        .get("templates")
        .and_then(Value::as_object)
        .ok_or_else(|| ComputeError::new("invalid_request", "缺少明确模板"))?;
    let inputs: Vec<ScoreInput> =
        serde_json::from_value(payload.get("items").cloned().unwrap_or(Value::Null))
            .map_err(|e| ComputeError::new("invalid_request", e.to_string()))?;
    let mut prepared = HashMap::new();
    let mut result = Vec::with_capacity(inputs.len());
    for input in inputs {
        checkpoint(cancelled)?;
        let template = templates
            .get(&input.template_key)
            .ok_or_else(|| ComputeError::new("invalid_request", "评分模板不存在"))?;
        let key = (input.template_key.clone(), input.cost);
        let value = prepared
            .entry(key)
            .or_insert_with(|| prepare(template, input.cost));
        result.push(if let Some(p) = value {
            json!(score(input, template, p)?)
        } else {
            Value::Null
        });
    }
    Ok(json!({"items": result}))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejected_observation_preview_preserves_scalar_positions_and_negative_epsilon() {
        let template =
            json!({"score_max":[50,50,50],"main_props":{"1":{"攻击%":1}},"sub_props":{"暴击":1}});
        let prepared = prepare(&template, 1).unwrap();
        let attack = Stat {
            stat_name: "攻击%".into(),
            value: 18.0,
        };
        let life = Stat {
            stat_name: "生命".into(),
            value: 2280.0,
        };
        let negative = Stat {
            stat_name: "暴击".into(),
            value: -1.0,
        };
        let mains = vec![attack.clone(), life];
        assert_eq!(
            observed_current(
                1,
                &mains,
                std::slice::from_ref(&negative),
                &template,
                &prepared
            )
            .unwrap(),
            1701
        );
        assert_eq!(
            observed_current(
                1,
                std::slice::from_ref(&attack),
                std::slice::from_ref(&negative),
                &template,
                &prepared
            )
            .unwrap(),
            1800
        );
        let error = score(
            ScoreInput {
                template_key: String::new(),
                cost: 1,
                cost_key: "1".into(),
                main_rows: mains,
                sub_rows: vec![negative],
            },
            &template,
            &prepared,
        )
        .err()
        .unwrap();
        assert_eq!(error.kind, "invalid_numeric");
    }
}
