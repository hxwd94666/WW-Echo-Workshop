// 将当前配装请求编译为密集索引、评分矩阵和严格独占候选。
use crate::domain::{PreparedRule, Record, Rule, canonical_stat, normalize, uuid};
use crate::matching::{Evaluator, Selection};
use crate::numeric::{ECHO_CENTS_LIMIT, TARGET_CENTS_LIMIT, python_float};
use crate::{ComputeError, ComputeResult, checkpoint, constants};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::sync::atomic::AtomicBool;

#[derive(Clone, Deserialize, PartialEq, Eq)]
pub struct Slot {
    pub cost: u8,
    #[serde(default)]
    pub main_names: Vec<String>,
    #[serde(default)]
    pub fixed_item_id: String,
}
fn yes() -> bool {
    true
}
fn cost_cap() -> u8 {
    12
}
#[derive(Clone, Deserialize)]
pub struct Role {
    pub character_id: String,
    pub modal_id: String,
    pub target: f64,
    pub rule: Rule,
    pub slots: Vec<Slot>,
    #[serde(default)]
    pub set_requirements: Vec<(String, usize)>,
    #[serde(default = "yes")]
    pub distinct_names: bool,
    #[serde(default = "yes")]
    pub set_unique_names: bool,
    #[serde(default = "cost_cap")]
    pub cost_budget: u8,
    #[serde(default)]
    pub owner_label: String,
    #[serde(default = "energy_base")]
    pub base_energy_regen: f64,
    #[serde(default = "energy_min")]
    pub energy_regen_min: f64,
    #[serde(default)]
    pub energy_regen_max: Option<f64>,
}
impl Role {
    pub fn key(&self) -> [String; 2] {
        [self.character_id.clone(), self.modal_id.clone()]
    }
}

#[derive(Deserialize)]
pub struct Request {
    pub profile_id: String,
    pub roles: Vec<Role>,
    #[serde(default = "yes")]
    pub fully_tuned: bool,
    #[serde(default)]
    pub priority_groups: Vec<Vec<[String; 2]>>,
}
#[derive(Deserialize)]
pub struct Current {
    pub character_id: String,
    pub modal_id: String,
    pub plan_id: String,
    #[serde(default)]
    pub issues: Vec<String>,
}
#[derive(Deserialize)]
pub struct Reservations {
    pub profile_id: String,
    pub inventory_revision: u64,
    pub claims: Vec<(String, String)>,
    pub current: Vec<Current>,
}
fn seconds() -> f64 {
    10.0
}
fn nodes() -> u64 {
    200_000
}
fn energy_base() -> f64 {
    100.0
}
fn energy_min() -> f64 {
    100.0
}
#[derive(Deserialize)]
pub struct Input {
    pub request: Request,
    pub records: Vec<Record>,
    #[serde(default)]
    pub templates: HashMap<String, Value>,
    #[serde(default)]
    pub inventory_revision: u64,
    #[serde(default)]
    pub reservations: Option<Reservations>,
    #[serde(default)]
    pub edit_plans: HashMap<String, String>,
    #[serde(default)]
    pub replace_plans: HashMap<String, String>,
    #[serde(default = "seconds")]
    pub seconds: f64,
    #[serde(default = "nodes")]
    pub max_nodes: u64,
}

#[derive(Default, Clone, Serialize)]
pub struct Build {
    pub ids: Vec<usize>,
    pub total: i64,
    pub moves: i64,
}

pub struct Problem {
    pub records: Vec<Record>,
    pub templates: HashMap<String, Value>,
    pub roles: Vec<Role>,
    pub groups: Vec<Vec<usize>>,
    pub explicit_groups: bool,
    pub labels: Vec<String>,
    pub targets: Vec<i64>,
    pub scores: Vec<Vec<Option<i64>>>,
    pub pools: Vec<[Vec<usize>; 5]>,
    pub masks: Vec<Vec<u8>>,
    pub names: Vec<usize>,
    pub sets: Vec<usize>,
    pub requirements: Vec<Vec<(usize, usize)>>,
    pub moves: Vec<Vec<i64>>,
    pub uuid_rank: Vec<usize>,
    pub seconds: f64,
    pub max_nodes: u64,
    pub diagnostics: Vec<[usize; 4]>,
    pub allowed: Vec<Vec<bool>>,
    pub missing_pins: Vec<String>,
    pub energy: Vec<i64>,
    pub energy_base: Vec<i64>,
    pub energy_min: Vec<i64>,
    pub energy_max: Vec<Option<i64>>,
}

fn error(message: &str) -> ComputeError {
    ComputeError::new("invalid_request", message)
}

fn record_energy(record: &Record) -> i64 {
    record
        .main_stats
        .iter()
        .chain(record.sub_stats.iter())
        .filter(|stat| canonical_stat(&stat.stat_name) == "共鸣效率")
        .map(|stat| (stat.value * 10.0).round() as i64)
        .sum()
}

pub fn target_cents(target: f64) -> ComputeResult<i64> {
    if !target.is_finite() || !(0.0..=1e12).contains(&target) {
        return Err(error("目标分超出范围"));
    }
    let raw = python_float(target);
    let (digits, exponent) = raw
        .split_once('e')
        .map_or((raw.as_str(), 0), |(m, e)| (m, e.parse::<i32>().unwrap()));
    let fractional = digits.split_once('.').map_or(0, |(_, d)| d.len() as i32);
    let number: u128 = digits
        .trim_start_matches('-')
        .replace('.', "")
        .parse()
        .map_err(|_| error("目标分格式错误"))?;
    let shift = exponent - fractional + 2;
    if shift.abs() > 36 {
        return Err(error("目标分精度错误"));
    }
    let value = if shift >= 0 {
        number
            .checked_mul(10_u128.pow(shift as u32))
            .ok_or_else(|| error("目标分溢出"))?
    } else {
        let divisor = 10_u128.pow((-shift) as u32);
        if !number.is_multiple_of(divisor) {
            return Err(error("目标分最多两位小数"));
        }
        number / divisor
    };
    if value > TARGET_CENTS_LIMIT as u128 {
        return Err(error("目标分超出范围"));
    }
    Ok(value as i64)
}

impl Problem {
    pub fn compile(
        input: Input,
        matrix: Option<Vec<Vec<Option<i64>>>>,
        cancel: &AtomicBool,
        allow_missing_pins: bool,
    ) -> ComputeResult<Self> {
        let Input {
            request,
            records,
            templates,
            inventory_revision,
            reservations,
            edit_plans,
            replace_plans,
            seconds,
            max_nodes,
        } = input;
        let Request {
            profile_id,
            roles,
            fully_tuned,
            priority_groups,
        } = request;
        if !uuid(&profile_id)
            || !(1..=8).contains(&roles.len())
            || records.len() > 20_000
            || !seconds.is_finite()
            || !(0.0..=120.0).contains(&seconds)
            || seconds == 0.0
            || !(1..=5_000_000).contains(&max_nodes)
        {
            return Err(error("配装档案、规模或预算错误"));
        }
        let mut ids = HashMap::new();
        for (i, record) in records.iter().enumerate() {
            checkpoint(cancel)?;
            record.validate_identity()?;
            if record.profile_id != profile_id || ids.insert(record.item_id.clone(), i).is_some() {
                return Err(error("跨档案或重复库存 UUID"));
            }
        }
        let mut role_keys = HashMap::new();
        let mut characters = HashSet::new();
        let mut pins = HashMap::new();
        let mut targets = Vec::new();
        let mut energy_base = Vec::new();
        let mut energy_min = Vec::new();
        let mut energy_max = Vec::new();
        for (r, role) in roles.iter().enumerate() {
            role.rule.validate()?;
            targets.push(target_cents(role.target)?);
            if role.character_id.is_empty()
                || role.modal_id.is_empty()
                || !characters.insert(&role.character_id)
                || role.slots.len() != 5
                || role.rule.element.is_empty()
                || role.rule.sonatas.is_empty()
                || role.cost_budget > 12
                || role.cost_budget < 5
                || role.slots.iter().map(|s| u32::from(s.cost)).sum::<u32>()
                    > u32::from(role.cost_budget)
            {
                return Err(error("角色或五槽 COST 要求错误"));
            }
            if !role.set_unique_names {
                return Err(ComputeError::new(
                    "retired_policy",
                    "仅支持当前套装不同名称计件规则",
                ));
            }
            if !role.base_energy_regen.is_finite()
                || !(0.0..=1000.0).contains(&role.base_energy_regen)
                || !role.energy_regen_min.is_finite()
                || !(0.0..=1000.0).contains(&role.energy_regen_min)
                || role.energy_regen_max.is_some_and(|value| {
                    !value.is_finite() || value < role.energy_regen_min || value > 1000.0
                })
            {
                return Err(error("共鸣效率范围错误"));
            }
            energy_base.push((role.base_energy_regen * 10.0).round() as i64);
            energy_min.push((role.energy_regen_min * 10.0).round() as i64);
            energy_max.push(
                role.energy_regen_max
                    .map(|value| (value * 10.0).round() as i64),
            );
            role_keys.insert(role.key(), r);
            for slot in &role.slots {
                if ![1, 3, 4].contains(&slot.cost)
                    || slot.main_names.iter().any(|s| {
                        constants()["max_main"][slot.cost.to_string()][0]
                            .get(s)
                            .is_none()
                    })
                    || slot.main_names.iter().collect::<HashSet<_>>().len() != slot.main_names.len()
                {
                    return Err(error("槽位 COST 或主词条错误"));
                }
                if !slot.fixed_item_id.is_empty()
                    && (!uuid(&slot.fixed_item_id)
                        || (!allow_missing_pins && !ids.contains_key(&slot.fixed_item_id))
                        || pins.insert(slot.fixed_item_id.clone(), r).is_some())
                {
                    return Err(error("固定 UUID 不在当前档案快照中或被重复固定"));
                }
            }
            let mut seen = HashSet::new();
            let allowed: HashSet<_> = role.rule.sonatas.iter().map(|s| normalize(s)).collect();
            if role.set_requirements.iter().any(|(s, n)| {
                !(1..=5).contains(n)
                    || !allowed.contains(&normalize(s))
                    || !seen.insert(normalize(s))
            }) || role.set_requirements.iter().map(|(_, n)| n).sum::<usize>() > 5
            {
                return Err(error("套装件数要求错误"));
            }
        }
        let mut owners = HashMap::new();
        let mut editable = HashSet::new();
        if edit_plans.keys().any(|key| replace_plans.contains_key(key)) {
            return Err(error("同一角色同时调整和替换"));
        }
        if let Some(reservations) = reservations {
            if reservations.profile_id != profile_id
                || reservations.inventory_revision != inventory_revision
            {
                return Err(error("配装保护与库存快照修订不一致"));
            }
            for (id, owner) in reservations.claims {
                if !uuid(&id) || owners.insert(id, owner).is_some() {
                    return Err(error("保护声明重复或 UUID 错误"));
                }
            }
            for (character, plan) in edit_plans.iter().chain(&replace_plans) {
                let role = roles
                    .iter()
                    .find(|r| &r.character_id == character)
                    .ok_or_else(|| error("调整角色不在本次请求中"))?;
                let current = reservations
                    .current
                    .iter()
                    .find(|c| &c.character_id == character)
                    .ok_or_else(|| error("调整来源不在当前配装中"))?;
                if current.plan_id != *plan {
                    return Err(error("调整来源已变化，请重新打开当前配装"));
                }
                if edit_plans.contains_key(character) && current.modal_id != role.modal_id {
                    return Err(error("调整当前配装须保持原模版；切换玩法请选择替换"));
                }
                if current.issues.iter().any(|s| {
                    ["missing_item", "wrong_profile", "unusable_item"].contains(&s.as_str())
                }) {
                    return Err(error("调整来源已变化或损坏"));
                }
                editable.insert(character.clone());
            }
        } else if !edit_plans.is_empty() || !replace_plans.is_empty() {
            return Err(error("调整和替换需要明确保护快照"));
        }
        for (id, &r) in &pins {
            if owners
                .get(id)
                .is_some_and(|owner| *owner != roles[r].character_id || !editable.contains(owner))
            {
                return Err(error("固定件已被其他配装保护"));
            }
        }
        let explicit_groups = !priority_groups.is_empty();
        let groups = if explicit_groups {
            let mut seen = HashSet::new();
            let mut groups = Vec::new();
            for group in priority_groups {
                if group.is_empty() {
                    return Err(error("优先级组为空"));
                }
                let mut indices = Vec::new();
                for key in group {
                    let index = *role_keys
                        .get(&key)
                        .ok_or_else(|| error("优先级角色不存在"))?;
                    if !seen.insert(index) {
                        return Err(error("优先级角色重复"));
                    }
                    indices.push(index);
                }
                groups.push(indices);
            }
            if seen.len() != roles.len() {
                return Err(error("优先级未覆盖所有角色"));
            }
            groups
        } else {
            vec![(0..roles.len()).collect()]
        };
        let mut labels = Vec::new();
        let mut scores = Vec::new();
        if let Some(matrix) = matrix {
            if matrix.len() != roles.len()
                || matrix.iter().any(|row| {
                    row.len() != records.len()
                        || row
                            .iter()
                            .flatten()
                            .any(|v| !(0..=ECHO_CENTS_LIMIT).contains(v))
                })
            {
                return Err(error("测试评分矩阵维度或范围错误"));
            }
            scores = matrix;
            labels = roles
                .iter()
                .map(|r| {
                    templates
                        .get(&format!("{}/{}", r.character_id, r.modal_id))
                        .and_then(|t| t["name"].as_str())
                        .unwrap_or(&r.character_id)
                        .to_owned()
                })
                .collect();
        } else {
            for role in &roles {
                checkpoint(cancel)?;
                let template = templates
                    .get(&format!("{}/{}", role.character_id, role.modal_id))
                    .ok_or_else(|| error("缺少明确评分模板"))?;
                if template["_character_element"]
                    .as_str()
                    .is_some_and(|element| !element.is_empty() && element != role.rule.element)
                {
                    return Err(error("角色属性与模板不一致"));
                }
                labels.push(
                    template["name"]
                        .as_str()
                        .unwrap_or(&role.character_id)
                        .to_owned(),
                );
                let selection = Selection {
                    character_id: role.character_id.clone(),
                    modal_id: role.modal_id.clone(),
                    template: template.clone(),
                    rule: role.rule.clone(),
                    slots: None,
                };
                let evaluator = Evaluator::new(&selection)?;
                let mut row = Vec::new();
                for record in &records {
                    checkpoint(cancel)?;
                    row.push(evaluator.evaluate(record)?.score.map(|s| s.current_cents));
                }
                scores.push(row);
            }
        }
        let mut name_map = HashMap::new();
        let mut set_map = HashMap::new();
        let mut names = Vec::new();
        let mut sets = Vec::new();
        for record in &records {
            let n = name_map.len();
            names.push(*name_map.entry(normalize(&record.name)).or_insert(n));
            let s = set_map.len();
            sets.push(*set_map.entry(normalize(&record.sonata)).or_insert(s));
        }
        let requirements = roles
            .iter()
            .map(|role| {
                role.set_requirements
                    .iter()
                    .map(|(name, n)| {
                        let next = set_map.len();
                        (*set_map.entry(normalize(name)).or_insert(next), *n)
                    })
                    .collect()
            })
            .collect();
        let mut pools = Vec::new();
        let mut masks = Vec::new();
        let mut moves = Vec::new();
        let mut diagnostics = Vec::new();
        let mut allowed = Vec::new();
        let usable: Vec<_> = records.iter().map(Record::usable).collect();
        for (r, role) in roles.iter().enumerate() {
            let rule = PreparedRule::new(&role.rule)?;
            let mut pool: [Vec<usize>; 5] = std::array::from_fn(|_| Vec::new());
            let mut mask = vec![0_u8; records.len()];
            let mut rejected = [0; 4];
            let mut eligibility = vec![false; records.len()];
            let moved: Vec<_> = records
                .iter()
                .map(|record| {
                    i64::from(role.owner_label.is_empty() || record.owner != role.owner_label)
                })
                .collect();
            for (i, record) in records.iter().enumerate() {
                checkpoint(cancel)?;
                let reason = if !usable[i] || scores[r][i].is_none() {
                    Some(0)
                } else if fully_tuned && record.sub_stats.len() != 5 {
                    Some(1)
                } else if rule.compatibility(record).2 != "match" {
                    Some(2)
                } else if record.locked
                    || pins.get(&record.item_id).is_some_and(|p| *p != r)
                    || owners.get(&record.item_id).is_some_and(|owner| {
                        owner != &role.character_id || !editable.contains(owner)
                    })
                {
                    Some(3)
                } else {
                    None
                };
                if let Some(reason) = reason {
                    rejected[reason] += 1;
                    continue;
                }
                eligibility[i] = true;
                for (j, slot) in role.slots.iter().enumerate() {
                    if record.cost == slot.cost
                        && (slot.main_names.is_empty()
                            || slot.main_names.contains(&record.main_stats[0].stat_name))
                        && (slot.fixed_item_id.is_empty() || slot.fixed_item_id == record.item_id)
                        && (!pins.contains_key(&record.item_id)
                            || slot.fixed_item_id == record.item_id)
                    {
                        pool[j].push(i);
                        mask[i] |= 1 << j;
                    }
                }
            }
            for slot in &mut pool {
                slot.sort_by(|&a, &b| {
                    scores[r][b]
                        .cmp(&scores[r][a])
                        .then(moved[a].cmp(&moved[b]))
                        .then(records[a].item_id.cmp(&records[b].item_id))
                });
            }
            pools.push(pool);
            masks.push(mask);
            moves.push(moved);
            diagnostics.push(rejected);
            allowed.push(eligibility);
        }
        let energy: Vec<i64> = records.iter().map(record_energy).collect();
        let mut ranks: Vec<_> = (0..records.len()).collect();
        ranks.sort_by_key(|i| &records[*i].item_id);
        let mut uuid_rank = vec![0; records.len()];
        for (rank, index) in ranks.into_iter().enumerate() {
            uuid_rank[index] = rank;
        }
        let mut missing_pins: Vec<_> = pins
            .keys()
            .filter(|id| !ids.contains_key(*id))
            .cloned()
            .collect();
        missing_pins.sort();
        Ok(Self {
            records,
            templates,
            roles,
            groups,
            explicit_groups,
            labels,
            targets,
            scores,
            pools,
            masks,
            names,
            sets,
            requirements,
            moves,
            uuid_rank,
            seconds,
            max_nodes,
            diagnostics,
            allowed,
            missing_pins,
            energy,
            energy_base,
            energy_min,
            energy_max,
        })
    }
    pub fn energy_total(&self, role: usize, ids: &[usize]) -> i64 {
        self.energy_base[role] + ids.iter().map(|index| self.energy[*index]).sum::<i64>()
    }

    pub fn energy_ok(&self, role: usize, ids: &[usize]) -> bool {
        let total = self.energy_total(role, ids);
        total >= self.energy_min[role]
            && self.energy_max[role].is_none_or(|maximum| total <= maximum)
    }

    pub fn legal_names_sets(&self, role: usize, ids: &[usize]) -> bool {
        if self.roles[role].distinct_names
            && ids
                .iter()
                .map(|i| self.names[*i])
                .collect::<HashSet<_>>()
                .len()
                != ids.len()
        {
            return false;
        }
        self.requirements[role].iter().all(|(set, n)| {
            ids.iter()
                .filter(|i| self.sets[**i] == *set)
                .map(|i| self.names[*i])
                .collect::<HashSet<_>>()
                .len()
                >= *n
        }) && self.energy_ok(role, ids)
    }
    pub fn build(&self, role: usize, ids: Vec<usize>) -> Build {
        let total = ids.iter().map(|i| self.scores[role][*i].unwrap()).sum();
        let moves = ids.iter().map(|i| self.moves[role][*i]).sum();
        Build { ids, total, moves }
    }
}
