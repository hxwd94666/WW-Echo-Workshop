// 在同一冻结模板下计算配装差异、评级和同 COST 展示配对。
use crate::domain::{Record, Rule, uuid};
use crate::matching::{Evaluator, Selection};
use crate::numeric::ECHO_CENTS_LIMIT;
use crate::rating::{Rating, loadout, rate, standard};
use crate::{ComputeError, ComputeResult, checkpoint};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::sync::atomic::AtomicBool;

#[derive(Deserialize)]
struct Key {
    character_id: String,
    modal_id: String,
}

#[derive(Deserialize)]
struct Request {
    profile_id: String,
    roles: Vec<Key>,
}

#[derive(Deserialize)]
struct Allocation {
    character_id: String,
    modal_id: String,
    label: String,
    item_ids: Vec<String>,
    scores: Vec<i64>,
}

#[derive(Deserialize)]
struct ResultInput {
    inventory_revision: u64,
    weight_version: String,
    request: Request,
    roles: Vec<Allocation>,
}

#[derive(Deserialize)]
struct Current {
    character_id: String,
    modal_id: String,
    plan_id: String,
    item_ids: Vec<String>,
    #[serde(default)]
    issues: Vec<String>,
}

#[derive(Deserialize)]
struct Input {
    profile_id: String,
    inventory_revision: u64,
    weight_version: String,
    result: ResultInput,
    records: Vec<Record>,
    current: Vec<Current>,
    templates: HashMap<String, Value>,
}

#[derive(Serialize)]
struct Item {
    item_id: String,
    score_cents: Option<i64>,
    rating: Option<Rating>,
    #[serde(skip_serializing)]
    cost: Option<u8>,
}

fn error(message: &str) -> ComputeError {
    ComputeError::new("invalid_request", message)
}

fn items(
    ids: &[String],
    values: &HashMap<String, i64>,
    records: &HashMap<String, &Record>,
    template: &Value,
) -> Vec<Item> {
    let standards: HashMap<_, _> = [1, 3, 4]
        .into_iter()
        .map(|cost| (cost, standard(template, Some(cost))))
        .collect();
    ids.iter()
        .map(|id| {
            let points = values.get(id).copied();
            let cost = records.get(id).map(|record| record.cost);
            let rating = cost
                .and_then(|cost| standards[&cost].as_ref())
                .and_then(|standard| points.and_then(|value| rate(standard, value as f64 / 100.0)));
            Item {
                item_id: id.clone(),
                score_cents: points,
                rating,
                cost,
            }
        })
        .collect()
}

fn pair(kind: &str, old: Option<&Item>, new: Option<&Item>) -> Value {
    let delta = old
        .and_then(|old| old.score_cents)
        .zip(new.and_then(|new| new.score_cents))
        .map(|(old, new)| new - old);
    json!({"kind":kind,"old_id":old.map(|item|&item.item_id),
           "new_id":new.map(|item|&item.item_id),"delta_cents":delta})
}

fn pairs(old: &[Item], new: &[Item]) -> Vec<Value> {
    let new_by_id: HashMap<_, _> = new.iter().map(|item| (&item.item_id, item)).collect();
    let kept: HashSet<_> = old
        .iter()
        .filter(|item| new_by_id.contains_key(&item.item_id))
        .map(|item| &item.item_id)
        .collect();
    let mut output: Vec<_> = old
        .iter()
        .filter(|item| kept.contains(&item.item_id))
        .map(|item| pair("retained", Some(item), Some(new_by_id[&item.item_id])))
        .collect();
    let added: Vec<_> = new
        .iter()
        .filter(|item| !kept.contains(&item.item_id))
        .collect();
    let mut matched = HashSet::new();
    for old in old.iter().filter(|item| !kept.contains(&item.item_id)) {
        let candidate = added
            .iter()
            .find(|new| {
                !matched.contains(&new.item_id) && old.cost.is_some() && old.cost == new.cost
            })
            .copied();
        if let Some(new) = candidate {
            matched.insert(&new.item_id);
            output.push(pair("replaced", Some(old), Some(new)));
        } else {
            output.push(pair("removed", Some(old), None));
        }
    }
    output.extend(
        added
            .into_iter()
            .filter(|item| !matched.contains(&item.item_id))
            .map(|item| pair("added", None, Some(item))),
    );
    output
}

fn points(items: &[Item]) -> Option<Vec<i64>> {
    if items.len() != 5 {
        return None;
    }
    items.iter().map(|item| item.score_cents).collect()
}

pub fn batch(payload: Value, cancel: &AtomicBool) -> ComputeResult<Value> {
    let input: Input = serde_json::from_value(payload).map_err(|e| error(&e.to_string()))?;
    if !uuid(&input.profile_id)
        || input.result.request.profile_id != input.profile_id
        || input.weight_version.is_empty()
        || input.result.weight_version != input.weight_version
        || input.result.inventory_revision != input.inventory_revision
        || input.result.roles.is_empty()
        || input.result.roles.len() > 8
        || input.records.len() > 20000
        || input.current.len() > 10000
        || input.result.request.roles.len() != input.result.roles.len()
    {
        return Err(error("变动输入身份、修订或规模错误"));
    }
    let mut records = HashMap::new();
    for record in &input.records {
        checkpoint(cancel)?;
        record.validate_identity()?;
        if record.profile_id != input.profile_id
            || records.insert(record.item_id.clone(), record).is_some()
        {
            return Err(error("变动库存含重复 UUID 或跨档案记录"));
        }
    }
    let mut current = HashMap::new();
    for row in &input.current {
        if current.insert(row.character_id.clone(), row).is_some() || row.item_ids.len() > 5 {
            return Err(error("当前配装身份重复或槽位数量错误"));
        }
    }
    let mut role_keys = HashSet::new();
    let mut output = Vec::new();
    for (row, spec) in input.result.roles.iter().zip(&input.result.request.roles) {
        checkpoint(cancel)?;
        if row.character_id != spec.character_id
            || row.modal_id != spec.modal_id
            || !role_keys.insert((&row.character_id, &row.modal_id))
            || row.item_ids.len() != row.scores.len()
            || ![0, 5].contains(&row.item_ids.len())
            || row.item_ids.iter().any(|id| !uuid(id))
            || row.item_ids.iter().collect::<HashSet<_>>().len() != row.item_ids.len()
            || row
                .scores
                .iter()
                .any(|score| !(0..=ECHO_CENTS_LIMIT).contains(score))
        {
            return Err(error("变动角色身份、五件证据或分数格式错误"));
        }
        let template = input
            .templates
            .get(&format!("{}/{}", row.character_id, row.modal_id))
            .ok_or_else(|| error("变动缺少明确冻结模板"))?;
        let before = current.get(&row.character_id).copied();
        let old_ids = before.map_or(&[][..], |row| row.item_ids.as_slice());
        let mut old_values = HashMap::new();
        let mut new_values: HashMap<_, _> = row
            .item_ids
            .iter()
            .cloned()
            .zip(row.scores.iter().copied())
            .collect();
        let (mut state, mut notice) = if before.is_none() {
            ("no_current", "尚未设置当前配装，本次为新方案。")
        } else if before.is_some_and(|old| {
            !old.issues.is_empty()
                || old.modal_id != row.modal_id
                || old.item_ids.iter().any(|id| !records.contains_key(id))
        }) {
            (
                "old_unavailable",
                "当前配装已变化或模版不同，暂不计算替换差。",
            )
        } else if row.item_ids.len() != 5 {
            ("new_incomplete", "新方案未配齐，保留原配装。")
        } else {
            ("comparable", "")
        };
        let mut evaluated = 0;
        if state == "comparable" {
            let selection = Selection {
                character_id: row.character_id.clone(),
                modal_id: row.modal_id.clone(),
                template: template.clone(),
                rule: Rule::default(),
                slots: None,
            };
            let evaluator = Evaluator::new(&selection)?;
            let mut seen = HashSet::new();
            let mut values = HashMap::new();
            for id in old_ids.iter().chain(&row.item_ids) {
                checkpoint(cancel)?;
                if seen.insert(id)
                    && let Some(record) = records.get(id)
                {
                    evaluated += 1;
                    values.insert(
                        id.clone(),
                        evaluator
                            .evaluate(record)?
                            .score
                            .map(|score| score.current_cents),
                    );
                }
            }
            if old_ids
                .iter()
                .chain(&row.item_ids)
                .any(|id| values.get(id).is_none_or(Option::is_none))
            {
                state = "old_unavailable";
                notice = "存在待核对声骸，暂不计算替换差。";
            } else {
                old_values = old_ids
                    .iter()
                    .map(|id| (id.clone(), values[id].unwrap()))
                    .collect();
                new_values = row
                    .item_ids
                    .iter()
                    .map(|id| (id.clone(), values[id].unwrap()))
                    .collect();
            }
        }
        let old_items = items(old_ids, &old_values, &records, template);
        let new_items = items(&row.item_ids, &new_values, &records, template);
        let old_points = points(&old_items);
        let new_points = points(&new_items);
        let old_total = old_points.as_ref().map(|v| v.iter().sum::<i64>());
        let new_total = new_points.as_ref().map(|v| v.iter().sum::<i64>());
        let old_rating = old_points.as_ref().and_then(|v| loadout(template, v));
        let new_rating = new_points.as_ref().and_then(|v| loadout(template, v));
        let old_set: HashSet<_> = old_ids.iter().collect();
        let new_set: HashSet<_> = row.item_ids.iter().collect();
        output.push(
            json!({"key":[row.character_id,row.modal_id],"label":row.label,"state":state,
            "notice":notice,"old_plan_id":before.map_or("",|row|row.plan_id.as_str()),
            "old_items":old_items,"new_items":new_items,"pairs":pairs(&old_items,&new_items),
            "old_total_cents":old_total,"new_total_cents":new_total,
            "delta_cents":old_total.zip(new_total).map(|(old,new)|new-old),
            "old_rating":old_rating,"new_rating":new_rating,
            "retained_count":old_set.intersection(&new_set).count(),"evaluated_records":evaluated}),
        );
    }
    Ok(json!({"roles":output}))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn item(id: &str, cost: Option<u8>, score: Option<i64>) -> Item {
        Item {
            item_id: id.into(),
            cost,
            score_cents: score,
            rating: None,
        }
    }
    #[test]
    fn identity_precedes_cost_and_unmatched_pairs_have_no_delta() {
        let old = vec![item("a", Some(4), Some(100)), item("b", Some(3), None)];
        let new = vec![item("b", Some(3), Some(80)), item("c", Some(1), Some(100))];
        let result = pairs(&old, &new);
        assert_eq!(
            result,
            json!([
            {"kind":"retained","old_id":"b","new_id":"b","delta_cents":null},
            {"kind":"removed","old_id":"a","new_id":null,"delta_cents":null},
            {"kind":"added","old_id":null,"new_id":"c","delta_cents":null}])
            .as_array()
            .unwrap()
            .clone()
        );
    }
}
