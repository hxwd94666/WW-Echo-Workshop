// 按原库存分类组并集、跨组交集求值，不改变源行序或推断游戏状态。
use crate::domain::normalize;
use crate::inventory_snapshot::Row;
use crate::{ComputeError, ComputeResult};
use serde::Deserialize;

#[derive(Deserialize)]
pub struct View {
    pub text: String,
    pub compact: String,
    statuses: Vec<String>,
    cost: Option<u8>,
    costs: Vec<u8>,
    main_stat: String,
    main_stats: Vec<String>,
    sub_stats: Vec<String>,
    sonata: String,
    sonatas: Vec<String>,
}

fn stat(name: &str) -> String {
    let name = normalize(name);
    match name.as_str() {
        "攻击百分比" => "攻击%".into(),
        "生命百分比" => "生命%".into(),
        "防御百分比" => "防御%".into(),
        "普攻伤害加成" => "普攻".into(),
        "重击伤害加成" => "重击".into(),
        "共鸣技能伤害加成" => "共鸣技能".into(),
        "共鸣解放伤害加成" => "共鸣解放".into(),
        _ => name,
    }
}

impl View {
    pub fn validate(&self) -> ComputeResult<()> {
        if self
            .statuses
            .iter()
            .any(|s| !["equipped", "locked", "pending", "other"].contains(&s.as_str()))
            || self.cost.is_some_and(|c| ![1, 3, 4].contains(&c))
            || self.costs.iter().any(|c| ![1, 3, 4].contains(c))
            || self.sub_stats.len() > 5
            || self.statuses.len() > 4
            || self.main_stats.len() > 128
            || self.sonatas.len() > 20000
        {
            return Err(ComputeError::new("invalid_filter", "库存视图筛选结构错误"));
        }
        Ok(())
    }

    pub fn accepts(&self, row: &Row, usable: bool) -> bool {
        let equipped =
            row.record.game_equipped == Some(true) || row.record.owner_state == "equipped";
        let locked = row.record.locked || !row.reservation.is_empty();
        let pending = row.kind != "item" || !usable;
        if !self.statuses.is_empty()
            && !self.statuses.iter().any(|s| match s.as_str() {
                "equipped" => equipped,
                "locked" => locked,
                "pending" => pending,
                "other" => !equipped && !locked && !pending,
                _ => false,
            })
        {
            return false;
        }
        if (self.cost.is_some() || !self.costs.is_empty())
            && self.cost != Some(row.record.cost)
            && !self.costs.contains(&row.record.cost)
        {
            return false;
        }
        if !self.main_stat.is_empty() || !self.main_stats.is_empty() {
            let Some(first) = row.record.main_stats.first() else {
                return false;
            };
            let name = stat(&first.stat_name);
            if name != self.main_stat && !self.main_stats.contains(&name) {
                return false;
            }
        }
        if !self.sub_stats.iter().all(|required| {
            row.record
                .sub_stats
                .iter()
                .any(|s| stat(&s.stat_name) == *required)
        }) {
            return false;
        }
        if !self.sonata.is_empty() || !self.sonatas.is_empty() {
            let name = normalize(&row.record.sonata);
            if name != self.sonata && !self.sonatas.contains(&name) {
                return false;
            }
        }
        self.text.is_empty()
            || row.search.literal.contains(&self.text)
            || row.search.plain.contains(&self.text)
            || (!self.compact.is_empty()
                && self.compact.bytes().all(|c| c.is_ascii_alphabetic())
                && row.search.keys.contains(&self.compact))
    }
}
