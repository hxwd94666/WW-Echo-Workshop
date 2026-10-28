// 从完整冻结矩阵生成各角色及汇总视图，排序和业务统计不进入 Qt。
use crate::{ComputeError, ComputeResult, checkpoint};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::HashSet;
use std::sync::atomic::AtomicBool;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Input {
    identity: String,
    ids: Vec<String>,
    roles: Vec<[String; 2]>,
    // 当前分、排除原因、推荐状态，按记录 × 角色排列；不再次评分。
    cells: Vec<(Option<f64>, String, String)>,
}

#[derive(Serialize)]
struct View {
    role: Option<usize>,
    entries: Vec<[usize; 2]>,
    excluded: usize,
    unknown: usize,
}

pub fn evaluate(payload: Value, cancelled: &AtomicBool) -> ComputeResult<Value> {
    let input: Input = serde_json::from_value(payload)
        .map_err(|e| ComputeError::new("invalid_request", e.to_string()))?;
    let width = input.roles.len();
    if input.ids.len() > 20_000 || width > 128 || input.cells.len() > 160_000 {
        return Err(ComputeError::new("input_limit", "匹配展示矩阵超过限额"));
    }
    if input.identity.is_empty()
        || input.identity.len() > 128
        || width == 0
        || input.cells.len() != input.ids.len() * width
        || input.ids.iter().any(|id| id.is_empty() || id.len() > 128)
        || input
            .roles
            .iter()
            .flatten()
            .any(|id| id.is_empty() || id.len() > 128)
        || input.ids.iter().collect::<HashSet<_>>().len() != input.ids.len()
        || input.roles.iter().collect::<HashSet<_>>().len() != width
    {
        return Err(ComputeError::new(
            "invalid_request",
            "匹配展示身份或矩阵尺寸错误",
        ));
    }
    // 0 可用、1 可用但条件未知、2 不可评分、3 条件不符、4 槽位不符。
    let mut flags = Vec::with_capacity(input.cells.len());
    for (index, (current, reason, recommendation)) in input.cells.iter().enumerate() {
        if index % 256 == 0 {
            checkpoint(cancelled)?;
        }
        if current.is_some_and(|value| !value.is_finite())
            || !matches!(
                recommendation.as_str(),
                "match" | "unknown" | "mismatch" | "excluded"
            )
        {
            return Err(ComputeError::new(
                "invalid_request",
                "匹配展示单元数值或状态错误",
            ));
        }
        let flag = match reason.as_str() {
            "" if current.is_some() => u8::from(recommendation == "unknown"),
            "invalid" => 2,
            "compatibility" => 3,
            "slot" => 4,
            _ => return Err(ComputeError::new("invalid_request", "匹配展示排除原因错误")),
        };
        flags.push(flag);
    }
    let mut views = Vec::with_capacity(width + 1);
    for role in std::iter::once(None).chain((0..width).map(Some)) {
        checkpoint(cancelled)?;
        let mut entries = Vec::with_capacity(input.ids.len());
        let mut unknown = 0;
        for row in 0..input.ids.len() {
            if row % 256 == 0 {
                checkpoint(cancelled)?;
            }
            let mut best: Option<usize> = None;
            let mut unknown_fit = false;
            let start = role.unwrap_or(0);
            let end = role.map_or(width, |column| column + 1);
            for column in start..end {
                let index = row * width + column;
                if flags[index] >= 2 {
                    continue;
                }
                unknown_fit |= flags[index] == 1;
                let current = input.cells[index].0.unwrap();
                // 严格更大才替换：同分保留用户原有角色顺序。
                if best
                    .is_none_or(|previous| current > input.cells[row * width + previous].0.unwrap())
                {
                    best = Some(column);
                }
            }
            if let Some(column) = best {
                entries.push([row, column]);
                unknown += usize::from(unknown_fit);
            }
        }
        entries.sort_by(|[left_row, left_col], [right_row, right_col]| {
            input.cells[right_row * width + right_col]
                .0
                .unwrap()
                .partial_cmp(&input.cells[left_row * width + left_col].0.unwrap())
                .unwrap()
                .then_with(|| input.ids[*left_row].cmp(&input.ids[*right_row]))
        });
        checkpoint(cancelled)?;
        views.push(View {
            role,
            excluded: input.ids.len() - entries.len(),
            entries,
            unknown,
        });
    }
    Ok(json!({"identity":input.identity, "flags":flags, "views":views}))
}
