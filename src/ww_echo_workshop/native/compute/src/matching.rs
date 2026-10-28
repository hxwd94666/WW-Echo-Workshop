// 在一个冻结请求内批量评分并给出元素、套装适配和排除原因。
use crate::domain::{PreparedRule, Record, Rule};
use crate::scoring::{Prepared, Score, ScoreInput, prepare, score};
use crate::{ComputeError, ComputeResult, checkpoint};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::sync::atomic::AtomicBool;

#[derive(Deserialize)]
pub struct Selection {
    pub character_id: String,
    pub modal_id: String,
    pub template: Value,
    pub rule: Rule,
    #[serde(default)]
    pub slots: Option<Slots>,
}

/// 五槽要求：COST、可选主词条候选、可选固定 UUID。
#[derive(Deserialize)]
pub struct Slots {
    pub costs: Vec<u8>,
    #[serde(default)]
    pub main_names: Vec<Vec<String>>,
    #[serde(default)]
    pub fixed_item_ids: Vec<String>,
    #[serde(default)]
    pub allowed_sonatas: Vec<String>,
    #[serde(default)]
    pub set_requirements: Vec<(String, usize)>,
}

fn slot_matches(slots: &Slots, record: &Record) -> bool {
    if slots.costs.len() != 5 {
        return false;
    }
    let pinned: Vec<&str> = (0..5)
        .map(|index| {
            slots
                .fixed_item_ids
                .get(index)
                .map(String::as_str)
                .unwrap_or("")
        })
        .collect();
    (0..5).any(|index| {
        if record.cost != slots.costs[index] {
            return false;
        }
        let main = record
            .main_stats
            .first()
            .map(|stat| stat.stat_name.as_str())
            .unwrap_or("");
        let names = slots.main_names.get(index);
        if names.is_some_and(|names| !names.is_empty() && !names.iter().any(|n| n == main)) {
            return false;
        }
        let pin = pinned[index];
        if pin.is_empty() {
            // 已固定到其它槽位的 UUID 不能挪到本槽。
            !pinned
                .iter()
                .any(|other| !other.is_empty() && *other == record.item_id)
        } else {
            pin == record.item_id
        }
    })
}

#[derive(Serialize)]
pub struct Cell {
    pub item_id: String,
    pub character_id: String,
    pub modal_id: String,
    pub score: Option<Score>,
    pub compatibility: String,
    pub sonata_compatibility: &'static str,
    pub recommendation: &'static str,
    /// 可见性结论：invalid / compatibility / slot / 空串（可见）。
    pub reason: &'static str,
    pub issues: Vec<String>,
    pub explicit_rule: Rule,
}

pub struct Evaluator<'a> {
    selection: &'a Selection,
    rule: PreparedRule,
    prepared: HashMap<u8, Option<Prepared>>,
}

impl<'a> Evaluator<'a> {
    pub fn new(selection: &'a Selection) -> ComputeResult<Self> {
        if selection.character_id.is_empty()
            || selection.modal_id.is_empty()
            || !selection.template.is_object()
        {
            return Err(ComputeError::new(
                "invalid_request",
                "角色、玩法和模板须明确",
            ));
        }
        let rule = PreparedRule::new(&selection.rule)?;
        if let Some(slots) = &selection.slots {
            let empty = vec![Vec::new(); 5];
            let main = if slots.main_names.is_empty() {
                &empty
            } else {
                &slots.main_names
            };
            crate::requirement_checks::layout(&slots.costs, main)
                .and_then(|_| {
                    crate::requirement_checks::sets(&slots.allowed_sonatas, &slots.set_requirements)
                })
                .map_err(|message| ComputeError::new("invalid_request", message))?;
        }
        Ok(Self {
            selection,
            rule,
            prepared: [1, 3, 4]
                .into_iter()
                .map(|cost| (cost, prepare(&selection.template, cost)))
                .collect(),
        })
    }

    pub fn evaluate(&self, record: &Record) -> ComputeResult<Cell> {
        self.evaluate_checked(record, &matching_issues(record))
    }

    fn evaluate_checked(&self, record: &Record, issues: &[String]) -> ComputeResult<Cell> {
        let (element, sonata, mut recommendation) = self.rule.compatibility(record);
        if !issues.is_empty() {
            recommendation = "excluded";
        }
        let value = if !issues.is_empty() {
            None
        } else if let Some(prepared) = self.prepared.get(&record.cost).and_then(Option::as_ref) {
            Some(score(
                ScoreInput {
                    template_key: String::new(),
                    cost: record.cost,
                    cost_key: record.cost.to_string(),
                    main_rows: record.main_stats.clone(),
                    sub_rows: record.sub_stats.clone(),
                },
                &self.selection.template,
                prepared,
            )?)
        } else {
            None
        };
        // 排除原因与评分同源：界面只读这里，不再重算槽位或兼容性。
        let reason = if !issues.is_empty() || value.is_none() {
            "invalid"
        } else if recommendation == "mismatch" {
            "compatibility"
        } else if self
            .selection
            .slots
            .as_ref()
            .is_some_and(|slots| !slot_matches(slots, record))
        {
            "slot"
        } else {
            ""
        };
        Ok(Cell {
            item_id: record.item_id.clone(),
            character_id: self.selection.character_id.clone(),
            modal_id: self.selection.modal_id.clone(),
            score: value,
            compatibility: if element == "unknown" {
                "unknown".into()
            } else {
                format!("element_{element}")
            },
            sonata_compatibility: sonata,
            recommendation,
            reason,
            issues: issues.to_vec(),
            explicit_rule: self.selection.rule.clone(),
        })
    }
}

fn matching_issues(record: &Record) -> Vec<String> {
    let mut issues = record.validation_issues();
    if (record.review != "confirmed" || !issues.is_empty())
        && !issues.iter().any(|s| s == "inventory:not_confirmed")
    {
        issues.push("inventory:not_confirmed".into());
    }
    issues
}

/// 列式单元：`[score, compatibility, sonata, recommendation, reason, issues]`。
/// 评级标准表与记录 UUID 按批外置，界面按位置重建，避免每格重复键名。
fn compact_entry(cell: Cell, standard: Option<usize>) -> Value {
    let compatibility = match cell.compatibility.as_str() {
        "element_match" => 0,
        "element_mismatch" => 1,
        _ => 2,
    };
    let sonata = match cell.sonata_compatibility {
        "match" => 0,
        "mismatch" => 1,
        _ => 2,
    };
    let recommendation = match cell.recommendation {
        "match" => 0,
        "mismatch" => 1,
        "excluded" => 3,
        _ => 2,
    };
    let reason = match cell.reason {
        "" => 0,
        "invalid" => 1,
        "compatibility" => 2,
        "slot" => 3,
        _ => 4,
    };
    let score = match cell.score {
        Some(score) => {
            let rating = match score.rating.as_ref() {
                Some(value) => json!([label_index(value.label), value.score, standard]),
                None => Value::Null,
            };
            // 分数值与评分入口保持同一结构（含逐条贡献），界面可直接复用。
            json!([
                score.cost,
                score.cost_key,
                score.row_scores,
                score.current_score,
                score.potential_score,
                rating
            ])
        }
        None => Value::Null,
    };
    json!([
        score,
        compatibility,
        sonata,
        recommendation,
        reason,
        cell.issues
    ])
}

fn label_index(label: &str) -> u8 {
    match label {
        "C" => 0,
        "B" => 1,
        "A" => 2,
        "S" => 3,
        "SS" => 4,
        "SSS" => 5,
        _ => 255,
    }
}

/// 一段记录 × 全部角色；顺序与输入记录顺序一致。
fn evaluate_chunk(
    records: &[Record],
    evaluators: &[Evaluator<'_>],
    standard_indices: &[[Option<usize>; 3]],
    cancelled: &AtomicBool,
) -> ComputeResult<Vec<Value>> {
    let mut cells = Vec::with_capacity(records.len() * evaluators.len());
    for record in records {
        checkpoint(cancelled)?;
        let issues = matching_issues(record);
        let position = [1u8, 3, 4].iter().position(|cost| *cost == record.cost);
        for (column, evaluator) in evaluators.iter().enumerate() {
            checkpoint(cancelled)?;
            let standard = position.and_then(|index| standard_indices[column][index]);
            cells.push(compact_entry(
                evaluator.evaluate_checked(record, &issues)?,
                standard,
            ));
        }
    }
    Ok(cells)
}

#[derive(Deserialize)]
struct MatchInput {
    records: Vec<Record>,
    selections: Vec<Selection>,
}

pub fn batch(payload: Value, cancelled: &AtomicBool) -> ComputeResult<Value> {
    let input: MatchInput = serde_json::from_value(payload)
        .map_err(|e| ComputeError::new("invalid_request", e.to_string()))?;
    if input.records.len() > 20_000 || input.selections.len() > 128 {
        return Err(ComputeError::new("input_limit", "匹配输入超过限额"));
    }
    let mut keys = HashSet::new();
    // 先整体校验身份与唯一性，再并行分片求值，避免分片间漏检。
    let mut ids = HashSet::new();
    for record in &input.records {
        record.validate_identity()?;
        if !ids.insert(&record.item_id) {
            return Err(ComputeError::new("invalid_request", "匹配输入含重复 UUID"));
        }
    }
    // 评级标准按"角色 × COST"去重，避免每格重复下发阈值与来源。
    let mut standards: Vec<Value> = Vec::new();
    let mut standard_indices: Vec<[Option<usize>; 3]> = Vec::new();
    let evaluators: Vec<_> = input
        .selections
        .iter()
        .map(|s| {
            if !keys.insert((&s.character_id, &s.modal_id)) {
                return Err(ComputeError::new("invalid_request", "匹配角色玩法重复"));
            }
            let mut row = [None, None, None];
            for (position, cost) in [1u8, 3, 4].into_iter().enumerate() {
                if let Some(standard) = crate::rating::standard(&s.template, Some(cost)) {
                    row[position] = Some(standards.len());
                    standards.push(json!(standard));
                }
            }
            standard_indices.push(row);
            Evaluator::new(s)
        })
        .collect::<ComputeResult<_>>()?;
    checkpoint(cancelled)?;
    // 大矩阵按记录分片并行；分片按顺序回收，保持输出记录顺序。
    let cells = if input.records.len() >= 256 {
        let threads = std::thread::available_parallelism()
            .map(|value| value.get())
            .unwrap_or(1)
            .clamp(1, 8)
            .min(input.records.len());
        let chunk = input.records.len().div_ceil(threads);
        std::thread::scope(|scope| -> ComputeResult<Vec<Value>> {
            let handles: Vec<_> = input
                .records
                .chunks(chunk)
                .map(|slice| {
                    let evaluators = &evaluators;
                    let standard_indices = &standard_indices;
                    scope.spawn(move || {
                        evaluate_chunk(slice, evaluators, standard_indices, cancelled)
                    })
                })
                .collect();
            let mut out = Vec::with_capacity(input.records.len() * evaluators.len());
            for handle in handles {
                let local = handle
                    .join()
                    .map_err(|_| ComputeError::new("panic", "匹配并行求值失败"))??;
                out.extend(local);
            }
            Ok(out)
        })?
    } else {
        evaluate_chunk(&input.records, &evaluators, &standard_indices, cancelled)?
    };
    // 记录 UUID 每批只下发一次；单元按位置与 ids 对齐。
    let ids_out: Vec<&str> = input
        .records
        .iter()
        .map(|record| record.item_id.as_str())
        .collect();
    Ok(json!({"ids": ids_out, "cells": cells, "standards": standards}))
}
