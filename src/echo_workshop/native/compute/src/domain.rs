// 校验当前库存观测并规范化业务名称，保留每个独立 UUID。
use crate::scoring::Stat;
use crate::{ComputeError, ComputeResult, constants};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;

#[derive(Clone, Deserialize, Serialize)]
pub struct Record {
    pub item_id: String,
    pub profile_id: String,
    pub cost: u8,
    pub main_stats: Vec<Stat>,
    #[serde(default)]
    pub sub_stats: Vec<Stat>,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub sonata: String,
    pub level: Option<u8>,
    #[serde(default)]
    pub review: String,
    #[serde(default)]
    pub issues: Vec<String>,
    #[serde(default)]
    pub locked: bool,
    #[serde(default)]
    pub owner: String,
    #[serde(default)]
    pub owner_state: String,
    #[serde(default)]
    pub game_equipped: Option<bool>,
    #[serde(default)]
    pub note: String,
    #[serde(default)]
    pub captured_at: String,
}

pub fn uuid(value: &str) -> bool {
    value.len() == 36
        && value.bytes().enumerate().all(|(i, c)| {
            if [8, 13, 18, 23].contains(&i) {
                c == b'-'
            } else {
                c.is_ascii_digit() || (b'a'..=b'f').contains(&c)
            }
        })
}

pub fn normalize(value: &str) -> String {
    let map = &constants()["normalization"];
    value
        .chars()
        .map(|c| {
            let c = if c == '撃' { '擊' } else { c };
            map[c.to_string()]
                .as_str()
                .and_then(|s| s.chars().next())
                .unwrap_or(c)
        })
        .collect::<String>()
        .trim()
        .to_owned()
}

pub fn canonical_stat(value: &str) -> String {
    let plain = normalize(value);
    constants()["stat_aliases"][&plain]
        .as_str()
        .unwrap_or(&plain)
        .to_owned()
}

impl Record {
    pub fn validate_identity(&self) -> ComputeResult<()> {
        if !uuid(&self.item_id)
            || !uuid(&self.profile_id)
            || ![1, 3, 4].contains(&self.cost)
            || self.level.is_some_and(|level| level > 25)
        {
            return Err(ComputeError::new(
                "invalid_record",
                "库存 UUID、COST 或等级错误",
            ));
        }
        Ok(())
    }

    pub fn validation_issues(&self) -> Vec<String> {
        let mut issues = self.issues.clone();
        if self.name.is_empty() {
            issues.push("missing:name".into());
        }
        if self.sonata.is_empty() {
            issues.push("missing:sonata".into());
        }
        if self.level.is_none() {
            issues.push("missing:level".into());
        }
        if self.main_stats.len() != 2 {
            issues.push("main:expected_two_slots".into());
        }
        for (i, stat) in self.main_stats.iter().enumerate() {
            let maximum =
                constants()["validation_main"][self.cost.to_string()][i][&stat.stat_name].as_f64();
            if !stat.value.is_finite()
                || maximum.is_none_or(|m| stat.value <= 0.0 || stat.value > m + 0.01)
            {
                issues.push(format!("main:invalid:{i}"));
            }
        }
        let names: HashSet<_> = self.sub_stats.iter().map(|s| &s.stat_name).collect();
        if self.sub_stats.len() > 5 || names.len() != self.sub_stats.len() {
            issues.push("sub:duplicate_or_excess".into());
        }
        for stat in &self.sub_stats {
            if constants()["tiers"][&stat.stat_name]
                .as_array()
                .is_none_or(|tiers| {
                    !tiers
                        .iter()
                        .any(|n| (stat.value - n.as_f64().unwrap()).abs() <= 0.011)
                })
            {
                issues.push(format!("sub:invalid_roll:{}", stat.stat_name));
            }
        }
        if self
            .level
            .is_some_and(|level| self.sub_stats.len() > (level / 5) as usize)
        {
            issues.push("sub:exceeds_level_capacity".into());
        }
        let mut seen = HashSet::new();
        issues.retain(|s| seen.insert(s.clone()));
        issues
    }

    pub fn usable(&self) -> bool {
        self.review == "confirmed" && self.validation_issues().is_empty()
    }
}

#[derive(Clone, Default, Deserialize, Serialize)]
pub struct Rule {
    #[serde(default)]
    pub element: String,
    #[serde(default)]
    pub sonatas: Vec<String>,
}

impl Rule {
    pub fn validate(&self) -> ComputeResult<()> {
        if !self.element.is_empty()
            && !constants()["elements"]
                .as_array()
                .unwrap()
                .iter()
                .any(|v| v.as_str() == Some(&self.element))
        {
            return Err(ComputeError::new("invalid_rule", "未知角色属性"));
        }
        let names: HashSet<_> = self.sonatas.iter().collect();
        if names.len() != self.sonatas.len() || self.sonatas.iter().any(|s| s.trim().is_empty()) {
            return Err(ComputeError::new("invalid_rule", "允许套装名称重复或为空"));
        }
        Ok(())
    }
}

pub struct PreparedRule {
    element: String,
    sonatas: HashSet<String>,
}

impl PreparedRule {
    pub fn new(rule: &Rule) -> ComputeResult<Self> {
        rule.validate()?;
        Ok(Self {
            element: rule.element.clone(),
            sonatas: rule.sonatas.iter().map(|s| normalize(s)).collect(),
        })
    }

    pub fn compatibility(&self, record: &Record) -> (&'static str, &'static str, &'static str) {
        let element = if self.element.is_empty() {
            "unknown"
        } else if record.main_stats.iter().any(|s| {
            s.stat_name.strip_suffix("伤害加成").is_some_and(|name| {
                constants()["elements"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|v| v.as_str() == Some(name))
                    && name != self.element
            })
        }) {
            "mismatch"
        } else {
            "match"
        };
        let sonata = normalize(&record.sonata);
        let sonata = if self.sonatas.is_empty() || sonata.is_empty() {
            "unknown"
        } else if self.sonatas.contains(&sonata) {
            "match"
        } else {
            "mismatch"
        };
        let overall = if element == "mismatch" || sonata == "mismatch" {
            "mismatch"
        } else if element == "match" && sonata == "match" {
            "match"
        } else {
            "unknown"
        };
        (element, sonata, overall)
    }
}
