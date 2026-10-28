// 批量筛选冻结库存和复核行并统计可用性，不读写库存或生成评分。
use crate::inventory_snapshot::{Row, resolve};
use crate::{ComputeError, ComputeResult, checkpoint};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::HashSet;
use std::sync::atomic::AtomicBool;

const DAY_US: i128 = 86_400_000_000;
const MIN_DATETIME_US: i128 = -62_135_596_800_000_000;

#[derive(Deserialize)]
struct Matching {
    cost: Option<u8>,
    mode: String,
    fully_tuned: bool,
    minimum_level: u8,
    exclude_locked: bool,
    include_equipped: bool,
    protected_ids: Vec<String>,
    text: String,
    compact: String,
    max_age_days: f64,
    now_us: String,
    now_wall_us: String,
    cutoff_offset_us: String,
}

#[derive(Deserialize)]
struct Input {
    query_id: String,
    mode: String,
    snapshot_id: String,
    rows: Option<Vec<Row>>,
    matching: Option<Matching>,
    review_mode: u8,
    fully_tuned: bool,
    view: Option<crate::inventory_view::View>,
    scope: Option<Scope>,
}

#[derive(Deserialize)]
struct Scope {
    selected_ids: Option<Vec<String>>,
    // Raw search text retains the existing feedback distinction for whitespace.
    text: String,
}

fn age_microseconds(days: f64) -> ComputeResult<i128> {
    if !days.is_finite() || days < 0.0 {
        return Err(ComputeError::new(
            "invalid_filter",
            "Invalid capture age limit",
        ));
    }
    if days >= 1_000_000_000.0 {
        return Err(ComputeError::new(
            "datetime_overflow",
            "days exceeds timedelta limit",
        ));
    }
    let whole = days.trunc() as i128;
    let fraction = (days - days.trunc()) * DAY_US as f64;
    let integral = fraction.trunc() as i128;
    let remainder = fraction - fraction.trunc();
    let base = whole * DAY_US + integral;
    let extra = if remainder == 0.5 {
        if base % 2 == 0 { 0 } else { 1 }
    } else {
        remainder.round() as i128
    };
    Ok(base + extra)
}

fn micros(value: &str) -> ComputeResult<i128> {
    value
        .parse()
        .map_err(|_| ComputeError::new("invalid_request", "UTC 时间传输结构错误"))
}

fn matches_text(row: &Row, options: &Matching) -> bool {
    options.text.is_empty()
        || row.search.literal.contains(&options.text)
        || row.search.plain.contains(&options.text)
        || (!options.compact.is_empty()
            && options.compact.bytes().all(|c| c.is_ascii_alphabetic())
            && row.search.keys.contains(&options.compact))
}

pub fn clock(payload: Value, cancel: &AtomicBool) -> ComputeResult<Value> {
    checkpoint(cancel)?;
    let id = payload["query_id"]
        .as_str()
        .ok_or_else(|| ComputeError::new("invalid_request", "缺少时间查询身份"))?;
    let now = micros(
        payload["now_wall_us"]
            .as_str()
            .ok_or_else(|| ComputeError::new("invalid_request", "缺少本地时间表示"))?,
    )?;
    let age = age_microseconds(
        payload["max_age_days"]
            .as_f64()
            .ok_or_else(|| ComputeError::new("invalid_filter", "Invalid capture age limit"))?,
    )?;
    let cutoff = now
        .checked_sub(age)
        .ok_or_else(|| ComputeError::new("datetime_overflow", "date value out of range"))?;
    if cutoff < MIN_DATETIME_US {
        return Err(ComputeError::new(
            "datetime_overflow",
            "date value out of range",
        ));
    }
    Ok(json!({"query_id":id,"cutoff_wall_us":cutoff.to_string()}))
}

pub fn evaluate(payload: Value, cancel: &AtomicBool) -> ComputeResult<Value> {
    checkpoint(cancel)?;
    let input: Input = serde_json::from_value(payload)
        .map_err(|e| ComputeError::new("invalid_request", e.to_string()))?;
    if input.query_id.is_empty()
        || !["matching", "review", "pins", "view"].contains(&input.mode.as_str())
        || input.review_mode > 2
    {
        return Err(ComputeError::new(
            "input_limit",
            "库存查询结构或规模超出限额",
        ));
    }
    let matching = input.matching.as_ref();
    if let Some(scope) = &input.scope
        && (input.mode != "matching"
            || scope.selected_ids.as_ref().is_some_and(|ids| {
                ids.len() > 20_000 || ids.iter().any(|id| id.is_empty() || id.len() > 128)
            }))
    {
        return Err(ComputeError::new("invalid_request", "匹配选择范围结构无效"));
    }
    if input.mode == "view" {
        input
            .view
            .as_ref()
            .ok_or_else(|| ComputeError::new("invalid_filter", "缺少库存视图筛选"))?
            .validate()?;
    }
    let mut cutoff = None;
    let mut now = 0;
    let mut protected = HashSet::new();
    if input.mode == "matching" {
        let options =
            matching.ok_or_else(|| ComputeError::new("invalid_request", "缺少明确候选筛选"))?;
        if !["confirmed", "pending", "all"].contains(&options.mode.as_str())
            || options.cost.is_some_and(|c| ![1, 3, 4].contains(&c))
            || ![0, 5, 10, 15, 20, 25].contains(&options.minimum_level)
        {
            return Err(ComputeError::new(
                "invalid_filter",
                "Unknown inventory filter",
            ));
        }
        now = micros(&options.now_us)?;
        let age = age_microseconds(options.max_age_days)?;
        if options.max_age_days != 0.0 {
            let start = micros(&options.now_wall_us)?
                .checked_sub(age)
                .ok_or_else(|| ComputeError::new("datetime_overflow", "date value out of range"))?;
            if start < MIN_DATETIME_US {
                return Err(ComputeError::new(
                    "datetime_overflow",
                    "date value out of range",
                ));
            }
            let offset = micros(&options.cutoff_offset_us)?;
            if offset.unsigned_abs() >= DAY_US as u128 {
                return Err(ComputeError::new("invalid_request", "UTC 偏移超出结构限额"));
            }
            cutoff = Some(start - offset);
        }
        protected.extend(options.protected_ids.iter().map(String::as_str));
    }
    let (snapshot, retained) = resolve(&input.snapshot_id, input.rows, cancel)?;
    let (mut pending, mut confirmed, mut eligible) = (0_usize, 0_usize, 0_usize);
    let mut indices = Vec::new();
    let mut ids = Vec::new();
    let mut facts = Vec::new();
    let mut quality = Vec::new();
    let mut cost_positions = json!({"1":[0],"3":[0],"4":[0]});
    let mut any_usable = false;
    for (index, row) in snapshot.rows.iter().enumerate() {
        checkpoint(cancel)?;
        let usable = snapshot.usable[index];
        any_usable |= usable;
        let tuned = row.record.sub_stats.len() == 5;
        if row.kind != "item" || !usable {
            pending += 1;
        }
        if row.kind == "item" && usable {
            confirmed += 1;
            if !row.record.locked && (!input.fully_tuned || tuned) {
                eligible += 1;
            }
        }
        let accepted = if input.mode == "view" {
            input.view.as_ref().unwrap().accepts(row, usable)
        } else if input.mode == "pins" {
            input.review_mode == 0 || usable
        } else if input.mode == "review" {
            input.review_mode == 0
                || (input.review_mode == 1 && (row.kind != "item" || !usable))
                || (input.review_mode == 2 && usable)
        } else {
            let options = matching.unwrap();
            // The existing candidate API rejects an unknown level before all
            // other predicates; review queries still retain such observations.
            let level = row.record.level.ok_or_else(|| {
                ComputeError::new(
                    "unknown_level",
                    "'<' not supported between instances of 'NoneType' and 'int'",
                )
            })?;
            let base = level >= options.minimum_level
                && (!options.exclude_locked
                    || (!row.record.locked && !protected.contains(row.record.item_id.as_str())))
                && (options.include_equipped || row.record.owner.is_empty())
                && options.cost.is_none_or(|c| row.record.cost == c)
                && (options.mode != "confirmed" || usable)
                && (options.mode != "pending" || !usable)
                && (!options.fully_tuned || tuned)
                && matches_text(row, options);
            if base {
                if let Some(cutoff) = cutoff {
                    row.captured_us
                        .as_ref()
                        .and_then(|t| t.parse::<i128>().ok())
                        .is_some_and(|t| cutoff <= t && t <= now)
                } else {
                    true
                }
            } else {
                false
            }
        };
        if accepted {
            indices.push(index);
            ids.push(&row.record.item_id);
            if input.mode == "review" {
                quality.push(json!({"item_id":row.record.item_id,
                    "issues":snapshot.issues[index],"usable":usable}));
            }
            if input.mode == "pins" {
                facts.push(
                    json!({"usable":usable,"needs_correction":snapshot.needs_correction[index]}),
                );
                cost_positions[row.record.cost.to_string()]
                    .as_array_mut()
                    .unwrap()
                    .push(json!(indices.len()));
            }
        }
    }
    checkpoint(cancel)?;
    let scope_result = if let Some(scope) = &input.scope {
        let selected: Option<HashSet<&str>> = scope
            .selected_ids
            .as_ref()
            .map(|ids| ids.iter().map(String::as_str).collect());
        let mut selected_indices = Vec::new();
        let mut selected_ids = Vec::new();
        for &index in &indices {
            checkpoint(cancel)?;
            let id = snapshot.rows[index].record.item_id.as_str();
            if selected.as_ref().is_none_or(|ids| ids.contains(id)) {
                selected_indices.push(index);
                selected_ids.push(id);
            }
        }
        let options = matching.unwrap();
        let feedback = if snapshot.rows.is_empty() {
            "inventory_empty"
        } else if !any_usable {
            "review_required"
        } else if scope.selected_ids.as_ref().is_some_and(Vec::is_empty) {
            "selection_empty"
        } else if indices.is_empty() {
            if !scope.text.is_empty()
                || options.cost.is_some()
                || options.fully_tuned
                || options.max_age_days != 0.0
                || options.mode != "confirmed"
            {
                "selection_filtered_empty"
            } else {
                "filters_empty"
            }
        } else if selected_indices.is_empty() {
            "selection_no_match"
        } else {
            ""
        };
        Some(json!({"indices":selected_indices,"item_ids":selected_ids,"feedback":feedback}))
    } else {
        None
    };
    let mut response = json!({"query_id":input.query_id,"mode":input.mode,"snapshot_id":input.snapshot_id,
        "snapshot_cached":retained,"indices":indices,"item_ids":ids,
        "counts":{"saved":snapshot.rows.len(),"pending":pending,"confirmed":confirmed,"eligible":eligible}});
    if input.mode == "pins" {
        response["facts"] = json!(facts);
        response["cost_positions"] = cost_positions;
    }
    if input.mode == "review" {
        response["quality"] = json!(quality);
    }
    if let Some(value) = scope_result {
        response["scope"] = value;
    }
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn age_fraction_preserves_microsecond_half_even_and_integral_days() {
        assert_eq!(age_microseconds(3.0).unwrap(), 3 * DAY_US);
        assert_eq!(age_microseconds(0.5 / DAY_US as f64).unwrap(), 0);
        assert_eq!(age_microseconds(1.5 / DAY_US as f64).unwrap(), 2);
        assert!(age_microseconds(1_000_000_000.0).is_err());
    }
}
