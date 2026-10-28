// 使用整数比率精确比较当前六级目标，不保留旧政策求值。
use crate::numeric::{ROLE_CENTS_LIMIT, TARGET_CENTS_LIMIT};
use crate::{ComputeError, ComputeResult, checkpoint};
use serde::Deserialize;
use serde_json::{Value, json};
use std::cmp::Ordering;
use std::sync::atomic::AtomicBool;

pub fn policy_version(explicit_groups: bool) -> &'static str {
    if explicit_groups {
        "exclusive-five-priority-target-cap-v2"
    } else {
        "exclusive-five-target-cap-v2"
    }
}

#[derive(Clone, Debug)]
pub struct Objective {
    pub met: usize,
    pub numerator: i64,
    pub denominator: i64,
    pub complete: usize,
    pub utility: i64,
    pub total: i64,
    pub moves: i64,
}

impl Ord for Objective {
    fn cmp(&self, other: &Self) -> Ordering {
        self.met
            .cmp(&other.met)
            .then_with(|| {
                (i128::from(self.numerator) * i128::from(other.denominator))
                    .cmp(&(i128::from(other.numerator) * i128::from(self.denominator)))
            })
            .then(self.complete.cmp(&other.complete))
            .then(self.utility.cmp(&other.utility))
            .then(self.total.cmp(&other.total))
            .then(other.moves.cmp(&self.moves))
    }
}
impl PartialOrd for Objective {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl PartialEq for Objective {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other) == Ordering::Equal
    }
}
impl Eq for Objective {}

impl Objective {
    pub fn new(totals: &[i64], targets: &[i64], complete: usize, moves: i64) -> Self {
        let mut value = Self {
            met: 0,
            numerator: 1,
            denominator: 1,
            complete,
            utility: 0,
            total: 0,
            moves,
        };
        for (&total, &target) in totals.iter().zip(targets) {
            value.total += total;
            value.utility += if target > 0 { total.min(target) } else { total };
            if target > 0 {
                value.met += usize::from(total >= target);
                if i128::from(total) * i128::from(value.denominator)
                    < i128::from(value.numerator) * i128::from(target)
                {
                    value.numerator = total;
                    value.denominator = target;
                }
            }
        }
        value
    }
    pub fn as_json(&self) -> Value {
        json!([
            self.met,
            self.numerator as f64 / self.denominator as f64,
            self.complete,
            self.utility,
            self.total,
            -self.moves
        ])
    }

    pub fn as_model_json(&self, totals: &[i64], targets: &[i64]) -> Value {
        let mut value = self.as_json();
        // Preserve the current frozen-model JSON spelling of min(ratio, 1):
        // an exceeded first positive target supplied integer 1, equality supplied 1.0.
        // Ranking still uses the same exact integer ratio, never this representation.
        if self.numerator == self.denominator
            && totals
                .iter()
                .zip(targets)
                .find(|(_, target)| **target > 0)
                .is_some_and(|(total, target)| total > target)
        {
            value[1] = json!(1);
        }
        value
    }
}

#[derive(Deserialize)]
struct Input {
    totals: Vec<i64>,
    targets: Vec<i64>,
    complete: usize,
    moves: i64,
}

pub fn batch(payload: Value, cancel: &AtomicBool) -> ComputeResult<Value> {
    let rows: Vec<Input> = serde_json::from_value(payload["items"].clone())
        .map_err(|e| ComputeError::new("invalid_request", e.to_string()))?;
    let mut result = Vec::new();
    for row in rows {
        checkpoint(cancel)?;
        if row.totals.len() != row.targets.len()
            || row.totals.len() > 8
            || row.complete > row.totals.len()
            || row.moves < 0
            || row.moves > 40
            || row
                .totals
                .iter()
                .any(|n| !(0..=ROLE_CENTS_LIMIT).contains(n))
            || row
                .targets
                .iter()
                .any(|n| !(0..=TARGET_CENTS_LIMIT).contains(n))
        {
            return Err(ComputeError::new(
                "invalid_request",
                "目标比较输入或范围错误",
            ));
        }
        let value = Objective::new(&row.totals, &row.targets, row.complete, row.moves);
        result.push(json!({"objective":value.as_json(),"minimum_ratio":[value.numerator,value.denominator]}));
    }
    Ok(json!({"items":result}))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn model_json_keeps_capped_integer_and_uncapped_float_spelling_without_changing_rank() {
        for (totals, targets, integer) in [
            (vec![151, 150], vec![150, 150], true),
            (vec![150, 151], vec![150, 150], false),
            (vec![151, 149], vec![150, 150], false),
            (vec![151], vec![0], false),
        ] {
            let rank = Objective::new(&totals, &targets, totals.len(), 0);
            let model = rank.as_model_json(&totals, &targets);
            assert_eq!(model[1].as_number().unwrap().is_i64(), integer);
            assert_eq!(model[1].as_f64(), rank.as_json()[1].as_f64());
        }
    }
}
