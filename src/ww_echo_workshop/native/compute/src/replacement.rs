// 在冻结方案中批量评估单槽替换，其他四件和原要求保持固定。
use crate::allocation_checks;
use crate::allocation_model::{Input as AllocationInput, Problem};
use crate::domain::{PreparedRule, Record, uuid};
use crate::objective::Objective;
use crate::rating::{self, Rating, Standard};
use crate::scoring;
use crate::{ComputeError, ComputeResult, checkpoint};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::sync::atomic::AtomicBool;

#[derive(Deserialize)]
struct Input {
    source: Value,
    records: Vec<Record>,
    profile_id: String,
    source_plan_id: String,
    weight_version: String,
    inventory_revision: u64,
    loadout_revision: u64,
    claims: Vec<(String, String)>,
    character_id: String,
    slot_index: usize,
    #[serde(default)]
    item_id: Option<String>,
}

fn invalid(message: &str) -> ComputeError {
    ComputeError::new("invalid_request", message)
}

fn same_physical(left: &Record, right: &Record) -> bool {
    let stats = |rows: &[scoring::Stat], sorted: bool| {
        let mut values: Vec<_> = rows
            .iter()
            .map(|s| (s.stat_name.clone(), s.value))
            .collect();
        if sorted {
            values.sort_by(|a, b| a.0.cmp(&b.0).then(a.1.total_cmp(&b.1)));
        }
        values
    };
    left.name == right.name
        && left.sonata == right.sonata
        && left.cost == right.cost
        && left.level == right.level
        && stats(&left.main_stats, false) == stats(&right.main_stats, false)
        && stats(&left.sub_stats, true) == stats(&right.sub_stats, true)
}

struct Context {
    problem: Problem,
    ids: Vec<usize>,
    scores: Vec<i64>,
    row: Value,
    template: Value,
    pins: HashSet<String>,
    claims: HashMap<String, String>,
    usable: Vec<bool>,
    compatible: Vec<bool>,
    fully_tuned: bool,
    slot: usize,
    single: Option<Standard>,
    total: Option<Standard>,
    observed: Option<scoring::Prepared>,
}

impl Context {
    fn new(input: &mut Input, cancel: &AtomicBool) -> ComputeResult<Self> {
        if !uuid(&input.source_plan_id)
            || input.weight_version.is_empty()
            || input.slot_index >= 5
            || input.records.len() > 20000
        {
            return Err(invalid("请选择五个槽位中的一个，并提供完整冻结身份"));
        }
        let verified = allocation_checks::validate(input.source.clone(), cancel)?;
        let mut source: AllocationInput =
            serde_json::from_value(input.source.clone()).map_err(|e| invalid(&e.to_string()))?;
        if source.request.profile_id != input.profile_id {
            return Err(invalid("替换库存属于其他档案"));
        }
        let r = source
            .request
            .roles
            .iter()
            .position(|r| r.character_id == input.character_id)
            .ok_or_else(|| invalid("请选择已配齐五件的角色"))?;
        let mut row = verified["roles"][r].clone();
        row["label"] = verified["summaries"][r]["label"].clone();
        let reasons: Vec<String> =
            serde_json::from_value(input.source["allocations"][r]["reasons"].clone())
                .map_err(|e| invalid(&e.to_string()))?;
        row["reasons"] = json!(reasons);
        let ids: Vec<String> = serde_json::from_value(row["item_ids"].clone()).unwrap();
        if ids.len() != 5 {
            return Err(invalid("请选择已配齐五件的角色"));
        }
        let scores: Vec<i64> = serde_json::from_value(row["scores"].clone()).unwrap();
        let role = source.request.roles[r].clone();
        let template = source
            .templates
            .get(&format!("{}/{}", role.character_id, role.modal_id))
            .cloned()
            .ok_or_else(|| invalid("缺少冻结角色模板"))?;
        let pins = source
            .request
            .roles
            .iter()
            .flat_map(|r| &r.slots)
            .filter(|s| !s.fixed_item_id.is_empty())
            .map(|s| s.fixed_item_id.clone())
            .collect();
        let mut live = HashMap::new();
        for (i, record) in input.records.iter().enumerate() {
            checkpoint(cancel)?;
            record.validate_identity()?;
            if record.profile_id != input.profile_id || live.insert(&record.item_id, i).is_some() {
                return Err(invalid("替换库存含跨档案或重复 UUID"));
            }
        }
        for id in &ids {
            let frozen = source.records.iter().find(|r| &r.item_id == id).unwrap();
            if live
                .get(id)
                .is_none_or(|i| !same_physical(frozen, &input.records[*i]))
            {
                return Err(invalid("原方案声骸已缺失或内容已变化，请重新生成"));
            }
        }
        let mut claims = HashMap::new();
        for (id, owner) in &input.claims {
            if !uuid(id) || claims.insert(id.clone(), owner.clone()).is_some() {
                return Err(invalid("保护声明重复或 UUID 无效"));
            }
        }
        let selected_cost = role.slots[input.slot_index].cost;
        let fully_tuned = source.request.fully_tuned;
        source.records = input
            .records
            .iter()
            .filter(|r| r.cost == selected_cost || ids.contains(&r.item_id))
            .cloned()
            .collect();
        source.request.roles = vec![role];
        source.request.priority_groups.clear();
        source.reservations = None;
        source.edit_plans.clear();
        source.replace_plans.clear();
        source.inventory_revision = input.inventory_revision;
        let problem = Problem::compile(source, None, cancel, true)?;
        let map: HashMap<_, _> = problem
            .records
            .iter()
            .enumerate()
            .map(|(i, r)| (r.item_id.as_str(), i))
            .collect();
        let indices = ids.iter().map(|id| map[id.as_str()]).collect();
        let rule = PreparedRule::new(&problem.roles[0].rule)?;
        let usable = problem.records.iter().map(Record::usable).collect();
        let compatible = problem
            .records
            .iter()
            .map(|r| rule.compatibility(r).2 == "match")
            .collect();
        Ok(Self {
            problem,
            ids: indices,
            scores,
            row,
            pins,
            claims,
            usable,
            compatible,
            fully_tuned,
            slot: input.slot_index,
            single: rating::standard(&template, Some(selected_cost)),
            total: rating::standard(&template, None),
            observed: scoring::prepare(&template, selected_cost),
            template,
        })
    }

    fn reason(&self, ids: &[usize]) -> &'static str {
        if ids.len() != 5 || ids.iter().collect::<HashSet<_>>().len() != 5 {
            return "替换必须保留五个独立声骸";
        }
        let role = &self.problem.roles[0];
        let mut names = HashSet::new();
        for (slot, &i) in role.slots.iter().zip(ids) {
            let record = &self.problem.records[i];
            if !self.usable[i] {
                return "声骸缺失、待核对或已失效";
            }
            if record.locked {
                return "声骸已保护";
            }
            if self.fully_tuned && record.sub_stats.len() != 5 {
                return "此方案要求完整调谐声骸";
            }
            if record.cost != slot.cost {
                return "COST 与所选槽位不一致";
            }
            if !slot.main_names.is_empty()
                && !slot.main_names.contains(&record.main_stats[0].stat_name)
            {
                return "主词条不符合此槽要求";
            }
            if !slot.fixed_item_id.is_empty() && slot.fixed_item_id != record.item_id {
                return "此槽已有固定声骸要求";
            }
            if self.pins.contains(&record.item_id) && slot.fixed_item_id != record.item_id {
                return "声骸已被其他槽位固定";
            }
            if self
                .claims
                .get(&record.item_id)
                .is_some_and(|owner| !owner.is_empty() && owner != &role.character_id)
            {
                return "此声骸已被其他角色配装预留";
            }
            if !self.compatible[i] {
                return "不符合角色属性或允许套装";
            }
            if role.distinct_names && !names.insert(self.problem.names[i]) {
                return "违反同名声骸去重要求";
            }
        }
        if !self.problem.legal_names_sets(0, ids) {
            return "替换后套装件数不足";
        }
        ""
    }

    fn item_rating(&self, cents: i64) -> Option<Rating> {
        self.single
            .as_ref()
            .and_then(|s| rating::rate(s, cents as f64 / 100.0))
    }
    fn total_rating(&self, scores: &[i64]) -> Option<Rating> {
        self.total
            .as_ref()
            .and_then(|s| rating::loadout_points(scores).and_then(|p| rating::rate(s, p)))
    }

    fn candidate(&self, index: usize) -> ComputeResult<Value> {
        let record = &self.problem.records[index];
        let mut ids = self.ids.clone();
        ids[self.slot] = index;
        let mut reason = self.reason(&ids);
        let mut cents = self.problem.scores[0][index];
        if cents.is_none()
            && let Some(prepared) = &self.observed
        {
            cents = Some(scoring::observed_current(
                record.cost,
                &record.main_stats,
                &record.sub_stats,
                &self.template,
                prepared,
            )?);
        }
        if cents.is_none() && reason.is_empty() {
            reason = "声骸评分不可用";
        }
        let old = self.scores[self.slot];
        let mut scores = self.scores.clone();
        let total = cents.map(|value| {
            scores[self.slot] = value;
            scores.iter().sum::<i64>()
        });
        Ok(
            json!({"item_id":record.item_id,"name":record.name,"current_cents":cents,"old_cents":old,
            "delta_cents":cents.map(|value|value-old),"total_cents":total,
            "item_rating":cents.and_then(|value|self.item_rating(value)),
            "total_rating":cents.and_then(|_|self.total_rating(&scores)),"available":reason.is_empty(),"reason":reason}),
        )
    }
}

pub fn evaluate(payload: Value, cancel: &AtomicBool, preview: bool) -> ComputeResult<Value> {
    let mut input: Input = serde_json::from_value(payload).map_err(|e| invalid(&e.to_string()))?;
    let explicit = input.source["request"]["priority_groups"]
        .as_array()
        .is_some_and(|g| !g.is_empty());
    let context = Context::new(&mut input, cancel)?;
    let old_id = context.problem.records[context.ids[context.slot]]
        .item_id
        .clone();
    let cost = context.problem.records[context.ids[context.slot]].cost;
    let mut candidates = Vec::new();
    let mut record_ids = Vec::new();
    for (i, record) in context.problem.records.iter().enumerate() {
        checkpoint(cancel)?;
        if record.cost != cost || context.ids.contains(&i) {
            continue;
        }
        if !preview {
            record_ids.push(record.item_id.clone());
        }
        if !preview || input.item_id.as_ref() == Some(&record.item_id) {
            candidates.push(context.candidate(i)?);
        }
    }
    candidates.sort_by(|a, b| {
        b["available"]
            .as_bool()
            .cmp(&a["available"].as_bool())
            .then(
                b["delta_cents"]
                    .as_i64()
                    .unwrap_or(0)
                    .cmp(&a["delta_cents"].as_i64().unwrap_or(0)),
            )
            .then(a["item_id"].as_str().cmp(&b["item_id"].as_str()))
    });
    let mut output = json!({"profile_id":input.profile_id,"source_plan_id":input.source_plan_id,
        "weight_version":input.weight_version,"inventory_revision":input.inventory_revision,"loadout_revision":input.loadout_revision,
        "character_id":input.character_id,"modal_id":context.problem.roles[0].modal_id,"slot_index":input.slot_index,
        "label":context.row["label"],"old_id":old_id,"old_cents":context.scores[context.slot],
        "old_rating":context.item_rating(context.scores[context.slot]),"source_total_cents":context.row["total_cents"],
        "source_total_rating":context.total_rating(&context.scores),"record_ids":record_ids,"candidates":candidates});
    if preview {
        let candidate = candidates
            .first()
            .ok_or_else(|| invalid("请选择有效候选声骸"))?;
        if candidate["available"] != true {
            return Err(invalid(candidate["reason"].as_str().unwrap()));
        }
        let chosen = input.item_id.as_ref().unwrap();
        let index = context
            .problem
            .records
            .iter()
            .position(|r| &r.item_id == chosen)
            .unwrap();
        let mut ids = context.ids.clone();
        ids[context.slot] = index;
        let mut scores = context.scores.clone();
        scores[context.slot] = candidate["current_cents"].as_i64().unwrap();
        let total = scores.iter().sum::<i64>();
        let moves = ids
            .iter()
            .map(|i| context.problem.moves[0][*i])
            .sum::<i64>();
        let mut row = context.row.clone();
        row["item_ids"] = json!(
            ids.iter()
                .map(|i| &context.problem.records[*i].item_id)
                .collect::<Vec<_>>()
        );
        row["scores"] = json!(scores);
        row["total_cents"] = json!(total);
        row["moves"] = json!(moves);
        output["row"] = row;
        output["objective"] = Objective::new(&[total], &[context.problem.targets[0]], 1, moves)
            .as_model_json(&[total], &[context.problem.targets[0]]);
        output["policy_version"] = json!(crate::objective::policy_version(explicit));
    }
    Ok(output)
}
