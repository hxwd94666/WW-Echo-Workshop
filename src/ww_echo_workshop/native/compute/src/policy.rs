// 将 Echo Studio 策略内核适配到现有 XWUID 评分，提供强化与重抽决策。
use echo_policy::{
    CostModel, CostModelError, FixedScorer, InternalScorer, JointDecision, JointPolicySolver,
    JointPolicySolverError, LinearScorer, LockChoice, RerollPolicySolver, RerollPolicySolverError,
    ScorerError, SolverLimits, UpgradePolicySolver, UpgradePolicySolverError,
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::sync::atomic::AtomicBool;
use std::time::Instant;

use crate::numeric::entry_cents;
use crate::scoring::{self, ScoreInput, Stat};
use crate::{ComputeError, ComputeResult, checkpoint};

const STAT_NAMES: [&str; 13] = [
    "暴击",
    "暴击伤害",
    "攻击%",
    "防御%",
    "生命%",
    "攻击",
    "防御",
    "生命",
    "共鸣效率",
    "普攻",
    "重击",
    "共鸣技能",
    "共鸣解放",
];
const PERCENT: [bool; 13] = [
    true, true, true, true, true, false, false, false, true, true, true, true, true,
];
const MAX_INTERNAL: [u16; 13] = [
    105, 210, 116, 147, 116, 60, 70, 580, 124, 116, 116, 116, 116,
];
const DEFAULT_COSTS: [f64; 4] = [0.0, 1.0, 0.0, 1000.0];

#[derive(Deserialize)]
struct LimitsInput {
    #[serde(default)]
    max_states: Option<usize>,
    #[serde(default)]
    max_transitions: Option<usize>,
}

#[derive(Deserialize)]
struct PolicyInput {
    #[serde(default)]
    template: Option<Value>,
    #[serde(default = "default_scoring")]
    scoring: String,
    #[serde(default)]
    weights: Vec<f64>,
    #[serde(default)]
    fixed_weights: Vec<u16>,
    #[serde(default)]
    main_score: Option<f64>,
    #[serde(default)]
    max_score: Option<f64>,
    #[serde(default)]
    target_internal: Option<u16>,
    cost: u8,
    #[serde(default)]
    main: Vec<Stat>,
    #[serde(default)]
    sub: Vec<Stat>,
    mode: String,
    target: f64,
    #[serde(default)]
    costs: Vec<f64>,
    #[serde(default)]
    limits: Option<LimitsInput>,
    #[serde(default)]
    candidate: Option<Vec<Stat>>,
}

fn default_scoring() -> String {
    "workshop_xwuid".to_owned()
}

struct XwuidScorer {
    score_max: f64,
    weights: [f64; 13],
}

impl XwuidScorer {
    fn new(template: &Value, cost: u8) -> ComputeResult<Self> {
        let position = match cost {
            1 => 0usize,
            3 => 1,
            4 => 2,
            _ => return Err(invalid("COST 必须是 1/3/4")),
        };
        let maxima = template
            .get("score_max")
            .and_then(Value::as_array)
            .ok_or_else(|| invalid("评分模板缺少 score_max"))?;
        let score_max = maxima
            .get(position)
            .and_then(Value::as_f64)
            .ok_or_else(|| invalid("评分模板 score_max 不完整"))?;
        if !score_max.is_finite() || score_max <= 0.0 {
            return Err(invalid("评分模板 score_max 无效"));
        }
        let mut weights = [0.0f64; 13];
        let mut any_positive = false;
        for (index, name) in STAT_NAMES.iter().enumerate() {
            let weight = scoring::weight(template, cost, name, false);
            if !weight.is_finite() || weight < 0.0 {
                return Err(invalid("评分模板副词条权重无效"));
            }
            any_positive |= weight > 0.0;
            weights[index] = weight;
        }
        if !any_positive {
            return Err(invalid("评分模板副词条权重全为 0"));
        }
        Ok(Self { score_max, weights })
    }
}

impl InternalScorer for XwuidScorer {
    fn buff_score_internal(&self, index: usize, value: u16) -> Result<u16, ScorerError> {
        if index >= STAT_NAMES.len() {
            return Err(ScorerError::InvalidBuffIndex {
                buff_index: index,
                buff_value: value,
            });
        }
        if value > MAX_INTERNAL[index] {
            return Err(ScorerError::InvalidBuffValue {
                buff_index: index,
                buff_value: value,
            });
        }
        let display = if PERCENT[index] {
            f64::from(value) / 10.0
        } else {
            f64::from(value)
        };
        let cents = entry_cents(display, self.weights[index], self.score_max).map_err(|_| {
            ScorerError::InvalidBuffValue {
                buff_index: index,
                buff_value: value,
            }
        })?;
        u16::try_from(cents).map_err(|_| ScorerError::InvalidBuffValue {
            buff_index: index,
            buff_value: value,
        })
    }

    fn echo_score_internal(&self, echo: &[(usize, u16)]) -> Result<u16, ScorerError> {
        if echo.len() > 5 {
            return Err(ScorerError::InvalidEcho);
        }
        let mut mask = 0u16;
        let mut total = 0u32;
        for &(index, value) in echo {
            let bit = 1u16 << index;
            if mask & bit != 0 {
                return Err(ScorerError::InvalidEcho);
            }
            mask |= bit;
            total = total
                .checked_add(u32::from(self.buff_score_internal(index, value)?))
                .ok_or(ScorerError::InvalidEcho)?;
        }
        u16::try_from(total).map_err(|_| ScorerError::InvalidEcho)
    }
}

enum Scorer {
    Xwuid(XwuidScorer),
    Linear(LinearScorer),
    Fixed(FixedScorer),
}

impl Scorer {
    fn new(input: &PolicyInput) -> ComputeResult<Self> {
        match input.scoring.as_str() {
            "workshop_xwuid" => {
                let template = input
                    .template
                    .as_ref()
                    .ok_or_else(|| invalid("XWUID 策略需要评分模板"))?;
                Ok(Self::Xwuid(XwuidScorer::new(template, input.cost)?))
            }
            "linear" => {
                let weights = weights_array(&input.weights)?;
                let main = input.main_score.unwrap_or(0.0);
                let maximum = input.max_score.unwrap_or(100.0);
                LinearScorer::new(weights, main, maximum)
                    .map(Self::Linear)
                    .map_err(|error| invalid(format!("线性评分无效：{error:?}")))
            }
            "wuwa_echo_tool" => {
                let weights = weights_array(&input.weights)?;
                LinearScorer::new(weights, 0.0, 100.0)
                    .map(Self::Linear)
                    .map_err(|error| invalid(format!("Wuwa 评分无效：{error:?}")))
            }
            "mc_boost_assistant" => {
                let weights = weights_array(&input.weights)?;
                LinearScorer::mc_boost_assistant_scorer(weights)
                    .map(Self::Linear)
                    .map_err(|error| invalid(format!("漂泊者评分无效：{error:?}")))
            }
            "qq_bot" => {
                let weights = weights_array(&input.weights)?;
                let main = input.main_score.unwrap_or(0.0);
                LinearScorer::qq_bot_scorer(weights, main)
                    .map(Self::Linear)
                    .map_err(|error| invalid(format!("XWUID 评分无效：{error:?}")))
            }
            "fixed" => {
                let mut weights = [0u16; 13];
                for (index, value) in weights.iter_mut().enumerate() {
                    *value = input.fixed_weights[index];
                }
                FixedScorer::new(weights)
                    .map(Self::Fixed)
                    .map_err(|error| invalid(format!("固定评分无效：{error:?}")))
            }
            _ => Err(invalid("未知的评分体系")),
        }
    }
}

impl InternalScorer for Scorer {
    fn buff_score_internal(&self, index: usize, value: u16) -> Result<u16, ScorerError> {
        match self {
            Self::Xwuid(scorer) => scorer.buff_score_internal(index, value),
            Self::Linear(scorer) => scorer.buff_score_internal(index, value),
            Self::Fixed(scorer) => scorer.buff_score_internal(index, value),
        }
    }

    fn echo_score_internal(&self, echo: &[(usize, u16)]) -> Result<u16, ScorerError> {
        match self {
            Self::Xwuid(scorer) => scorer.echo_score_internal(echo),
            Self::Linear(scorer) => scorer.echo_score_internal(echo),
            Self::Fixed(scorer) => scorer.echo_score_internal(echo),
        }
    }
}

fn weights_array(values: &[f64]) -> ComputeResult<[f64; 13]> {
    if values.len() != 13 {
        return Err(invalid("评分权重需要 13 项"));
    }
    let mut weights = [0.0f64; 13];
    for (index, value) in values.iter().enumerate() {
        if !value.is_finite() || *value < 0.0 {
            return Err(invalid("评分权重必须是非负有限数"));
        }
        weights[index] = *value;
    }
    Ok(weights)
}

fn invalid(message: impl Into<String>) -> ComputeError {
    ComputeError::new("invalid_request", message)
}

fn stat_index(name: &str) -> Option<usize> {
    match name {
        "暴击" => Some(0),
        "暴击伤害" => Some(1),
        "攻击%" => Some(2),
        "防御%" => Some(3),
        "生命%" => Some(4),
        "攻击" => Some(5),
        "防御" => Some(6),
        "生命" => Some(7),
        "共鸣效率" => Some(8),
        "普攻" | "普攻伤害加成" => Some(9),
        "重击" | "重击伤害加成" => Some(10),
        "共鸣技能" | "共鸣技能伤害加成" => Some(11),
        "共鸣解放" | "共鸣解放伤害加成" => Some(12),
        _ => None,
    }
}

fn to_internal(index: usize, value: f64) -> ComputeResult<u16> {
    if !value.is_finite() || value < 0.0 {
        return Err(invalid("副词条数值无效"));
    }
    let scaled = if PERCENT[index] { value * 10.0 } else { value };
    let rounded = scaled.round();
    if !rounded.is_finite() || rounded > f64::from(MAX_INTERNAL[index]) {
        return Err(invalid(format!(
            "副词条数值超出上限：{}={}",
            STAT_NAMES[index], value
        )));
    }
    Ok(rounded as u16)
}

fn build_echo(rows: &[Stat], require_full: bool) -> ComputeResult<Vec<(usize, u16)>> {
    if rows.len() > 5 || (require_full && rows.len() != 5) {
        return Err(invalid(if require_full {
            "重抽需要完整的五条副词条"
        } else {
            "副词条最多五条"
        }));
    }
    let mut echo = Vec::with_capacity(rows.len());
    let mut mask = 0u16;
    for row in rows {
        let index = stat_index(&row.stat_name)
            .ok_or_else(|| invalid(format!("未知副词条：{}", row.stat_name)))?;
        let bit = 1u16 << index;
        if mask & bit != 0 {
            return Err(invalid("副词条类型重复"));
        }
        mask |= bit;
        echo.push((index, to_internal(index, row.value)?));
    }
    Ok(echo)
}

fn solver_limits(input: &Option<LimitsInput>) -> ComputeResult<SolverLimits> {
    let max_states = input
        .as_ref()
        .and_then(|value| value.max_states)
        .unwrap_or(2_000_000);
    let max_transitions = input
        .as_ref()
        .and_then(|value| value.max_transitions)
        .unwrap_or(64_000_000);
    if max_states == 0 || max_states > 5_000_000 {
        return Err(invalid("max_states 超出允许范围"));
    }
    if max_transitions == 0 || max_transitions > 160_000_000 {
        return Err(invalid("max_transitions 超出允许范围"));
    }
    Ok(SolverLimits {
        max_states,
        max_transitions,
    })
}

fn costs(input: &[f64]) -> [f64; 4] {
    let mut result = DEFAULT_COSTS;
    for (index, value) in input.iter().take(4).enumerate() {
        result[index] = *value;
    }
    result
}

fn validate(input: &PolicyInput) -> ComputeResult<()> {
    if !matches!(input.cost, 1 | 3 | 4) {
        return Err(invalid("COST 必须是 1/3/4"));
    }
    if !matches!(
        input.scoring.as_str(),
        "workshop_xwuid" | "linear" | "fixed" | "wuwa_echo_tool" | "mc_boost_assistant" | "qq_bot"
    ) {
        return Err(invalid("未知的评分体系"));
    }
    if input.scoring == "workshop_xwuid" && input.template.is_none() {
        return Err(invalid("XWUID 策略需要评分模板"));
    }
    if input.scoring != "workshop_xwuid" {
        if input.scoring == "fixed" {
            if input.fixed_weights.len() != 13 {
                return Err(invalid("固定评分需要 13 个整数权重"));
            }
        } else {
            if input.weights.len() != 13
                || input
                    .weights
                    .iter()
                    .any(|value| !value.is_finite() || *value < 0.0)
            {
                return Err(invalid("评分权重需要 13 个非负有限数"));
            }
        }
        if let Some(main) = input.main_score {
            if !main.is_finite() || main < 0.0 {
                return Err(invalid("主词条分必须是非负有限数"));
            }
        }
        if let Some(maximum) = input.max_score {
            if !maximum.is_finite() || maximum <= 0.0 {
                return Err(invalid("归一满分必须是正有限数"));
            }
        }
    }
    if !matches!(input.mode.as_str(), "upgrade" | "reroll" | "joint") {
        return Err(invalid("只支持 upgrade / reroll / joint"));
    }
    if input.main.len() != 2 {
        return Err(invalid("策略需要两条主词条"));
    }
    if input.sub.len() > 5 {
        return Err(invalid("副词条最多五条"));
    }
    if input.mode == "reroll" && input.sub.len() != 5 {
        return Err(invalid("重抽需要完整的五条副词条"));
    }
    if !input.target.is_finite() || input.target < 0.0 {
        return Err(invalid("目标分必须是非负有限数"));
    }
    if input.costs.len() > 4
        || input
            .costs
            .iter()
            .any(|value| !value.is_finite() || *value < 0.0)
    {
        return Err(invalid("资源权重必须是非负有限数"));
    }
    if input.candidate.is_some() && input.mode != "reroll" {
        return Err(invalid("只有重抽支持候选比较"));
    }
    Ok(())
}

fn classify(detail: &str, fallback: &'static str) -> (&'static str, &'static str) {
    if detail.contains("TooLarge") || detail.contains("Overflow") {
        ("state_limit", "策略状态空间超过限制")
    } else if detail.contains("Impossible") {
        ("impossible_target", "当前评分规则无法达到目标分")
    } else {
        ("solver_error", fallback)
    }
}

fn upgrade_error(error: UpgradePolicySolverError) -> ComputeError {
    let detail = format!("{error:?}");
    let (kind, message) = classify(&detail, "强化策略求解失败");
    ComputeError::new(kind, format!("{message}（{detail}）"))
}

fn reroll_error(error: RerollPolicySolverError) -> ComputeError {
    let detail = format!("{error:?}");
    let (kind, message) = classify(&detail, "重抽策略求解失败");
    ComputeError::new(kind, format!("{message}（{detail}）"))
}

fn joint_error(error: JointPolicySolverError) -> ComputeError {
    let detail = format!("{error:?}");
    let (kind, message) = classify(&detail, "联合策略求解失败");
    ComputeError::new(kind, format!("{message}（{detail}）"))
}

fn cost_error(error: CostModelError) -> ComputeError {
    ComputeError::new("invalid_request", format!("资源权重无效（{error:?}）"))
}

fn mask_types(mask: u16) -> Vec<String> {
    STAT_NAMES
        .iter()
        .enumerate()
        .filter(|(index, _)| mask & (1u16 << index) != 0)
        .map(|(_, name)| (*name).to_owned())
        .collect()
}

fn lock_cost(mask: u16) -> u8 {
    match mask.count_ones() {
        0..=2 => 1,
        3 => 2,
        _ => 3,
    }
}

fn lock_json(choice: &LockChoice) -> Value {
    json!({
        "mask": choice.lock_mask,
        "types": mask_types(choice.lock_mask),
        "cost": lock_cost(choice.lock_mask),
        "expected": choice.expected_cost,
        "probability": choice.success_probability,
        "regret": choice.regret,
    })
}

fn finite(value: f64) -> bool {
    value.is_finite()
}

fn numerical_error() -> ComputeError {
    ComputeError::new("numerical_failure", "策略结果超出有限数值范围")
}

pub fn evaluate(payload: Value, cancelled: &AtomicBool) -> ComputeResult<Value> {
    checkpoint(cancelled)?;
    let started = Instant::now();
    let input: PolicyInput = serde_json::from_value(payload)
        .map_err(|error| invalid(format!("策略请求字段无效：{error}")))?;
    validate(&input)?;
    let echo = build_echo(&input.sub, input.mode == "reroll")?;
    let scorer = Scorer::new(&input)?;
    let sub_score = scorer
        .echo_score_internal(&echo)
        .map_err(|error| invalid(format!("副词条评分失败：{error:?}")))?;
    let (main_score, current_score) = if input.scoring == "workshop_xwuid" {
        let template = input
            .template
            .as_ref()
            .ok_or_else(|| invalid("XWUID 策略需要评分模板"))?;
        let prepared = scoring::prepare(template, input.cost)
            .ok_or_else(|| invalid("评分模板无法用于策略计算"))?;
        let base = scoring::score(
            ScoreInput {
                template_key: String::new(),
                cost: input.cost,
                cost_key: input.cost.to_string(),
                main_rows: input.main.clone(),
                sub_rows: input.sub.clone(),
            },
            template,
            &prepared,
        )?;
        (base.row_scores.iter().take(2).sum(), base.current_score)
    } else {
        let main = input.main_score.unwrap_or(0.0);
        (main, main + f64::from(sub_score) / 100.0)
    };
    if !finite(main_score) || !finite(current_score) {
        return Err(numerical_error());
    }
    let target_internal = if let Some(target) = input.target_internal {
        target
    } else {
        let target_sub_display = (input.target - main_score).max(0.0);
        let target_internal_f = (target_sub_display * 100.0).round();
        if !target_internal_f.is_finite()
            || target_internal_f < 0.0
            || target_internal_f > f64::from(u16::MAX)
        {
            return Err(ComputeError::new("numeric_limit", "目标分超出策略支持范围"));
        }
        target_internal_f as u16
    };
    if target_internal == 0 {
        let decision = if input.mode == "reroll" || input.sub.len() == 5 {
            "keep"
        } else {
            "upgrade"
        };
        return Ok(json!({
            "mode": input.mode,
            "scoring": input.scoring,
            "current_score": current_score,
            "main_score": main_score,
            "sub_score": f64::from(sub_score) / 100.0,
            "target_internal": target_internal,
            "decision": decision,
            "lock_mask": Value::Null,
            "lock_types": Vec::<String>::new(),
            "resources": Value::Null,
            "current_probability": 1.0,
            "locks": Vec::<Value>::new(),
            "comparison": Value::Null,
            "cached": false,
            "allocated_bytes": 0,
            "note": "目标分为 0 或已由主词条满足",
            "elapsed_ms": started.elapsed().as_millis(),
        }));
    }
    match input.mode.as_str() {
        "upgrade" => upgrade(
            &input,
            &scorer,
            &echo,
            sub_score,
            target_internal,
            current_score,
            main_score,
            started,
        ),
        "reroll" => reroll(
            &input,
            &scorer,
            &echo,
            sub_score,
            target_internal,
            current_score,
            main_score,
            started,
        ),
        "joint" => joint(
            &input,
            &scorer,
            &echo,
            sub_score,
            target_internal,
            current_score,
            main_score,
            started,
        ),
        _ => unreachable!("validated mode"),
    }
}

fn upgrade<S: InternalScorer>(
    input: &PolicyInput,
    scorer: &S,
    echo: &[(usize, u16)],
    sub_score: u16,
    target_internal: u16,
    current_score: f64,
    main_score: f64,
    started: Instant,
) -> ComputeResult<Value> {
    let values = costs(&input.costs);
    let cost_model = CostModel::new(values[0], values[1], values[2]).map_err(cost_error)?;
    let mut solver =
        UpgradePolicySolver::new(scorer, target_internal, cost_model).map_err(upgrade_error)?;
    solver.lambda_search(1e-9, 100).map_err(upgrade_error)?;
    let resources = solver
        .calculate_expected_resources()
        .map_err(upgrade_error)?;
    let decision = if input.sub.len() == 5 {
        if sub_score >= target_internal {
            "keep"
        } else {
            "abandon"
        }
    } else if solver.get_decision_for_echo(echo).map_err(upgrade_error)? {
        "upgrade"
    } else {
        "abandon"
    };
    let probability = if input.sub.len() == 5 {
        if sub_score >= target_internal {
            1.0
        } else {
            0.0
        }
    } else {
        solver
            .get_success_probability_for_echo(echo)
            .map_err(upgrade_error)?
    };
    let weighted = solver.weighted_expected_cost().map_err(upgrade_error)?;
    let numbers = [
        resources.echo_per_success(),
        resources.tuner_per_success(),
        resources.exp_per_success(),
        weighted,
        probability,
    ];
    if numbers.iter().any(|value| !finite(*value)) {
        return Err(numerical_error());
    }
    Ok(json!({
        "mode": "upgrade",
        "scoring": input.scoring,
        "current_score": current_score,
        "main_score": main_score,
        "sub_score": f64::from(sub_score) / 100.0,
        "target_internal": target_internal,
        "target_display": f64::from(target_internal) / 100.0,
        "decision": decision,
        "lock_mask": Value::Null,
        "lock_types": Vec::<String>::new(),
        "resources": {
            "echoes": resources.echo_per_success(),
            "tuners": resources.tuner_per_success(),
            "tubes": resources.exp_per_success(),
            "frequencies": 0.0,
            "weighted": weighted,
            "attempt_probability": resources.success_probability(),
        },
        "current_probability": probability,
        "locks": Vec::<Value>::new(),
        "comparison": Value::Null,
        "cached": false,
        "allocated_bytes": solver.allocated_bytes(),
        "elapsed_ms": started.elapsed().as_millis(),
    }))
}

fn reroll<S: InternalScorer>(
    input: &PolicyInput,
    scorer: &S,
    echo: &[(usize, u16)],
    sub_score: u16,
    target_internal: u16,
    current_score: f64,
    main_score: f64,
    started: Instant,
) -> ComputeResult<Value> {
    if sub_score >= target_internal {
        return Ok(json!({
            "mode": "reroll",
            "scoring": input.scoring,
            "current_score": current_score,
            "main_score": main_score,
            "sub_score": f64::from(sub_score) / 100.0,
            "target_internal": target_internal,
            "decision": "keep",
            "lock_mask": Value::Null,
            "lock_types": Vec::<String>::new(),
            "resources": Value::Null,
            "current_probability": 1.0,
            "locks": Vec::<Value>::new(),
            "comparison": Value::Null,
            "cached": false,
            "allocated_bytes": 0,
            "elapsed_ms": started.elapsed().as_millis(),
        }));
    }
    let limits = solver_limits(&input.limits)?;
    let mut solver =
        RerollPolicySolver::with_scorer_and_limits(scorer, limits).map_err(reroll_error)?;
    solver.set_target(target_internal).map_err(reroll_error)?;
    solver.solve().map_err(reroll_error)?;
    let current_cost = solver
        .expected_lock_cost_for_echo(echo)
        .map_err(reroll_error)?;
    let best_lock = solver
        .best_lock_choices_for_echo(echo)
        .map_err(reroll_error)?;
    let locks = solver
        .lock_choices_for_echo(echo, 5)
        .map_err(reroll_error)?;
    let probability = solver
        .best_lock_success_probability_for_echo(echo)
        .map_err(reroll_error)?;
    let mut lock_values = Vec::with_capacity(locks.len());
    for choice in &locks {
        let numbers = [
            choice.expected_cost,
            choice.success_probability,
            choice.regret,
        ];
        if numbers.iter().any(|value| !finite(*value)) {
            return Err(numerical_error());
        }
        lock_values.push(lock_json(choice));
    }
    if !finite(current_cost) {
        return Err(numerical_error());
    }
    let comparison = match &input.candidate {
        Some(rows) => {
            let candidate = build_echo(rows, true)?;
            let candidate_score = scorer
                .echo_score_internal(&candidate)
                .map_err(|error| invalid(format!("候选副词条评分失败：{error:?}")))?;
            let accept = solver
                .should_accept_echo(echo, &candidate)
                .map_err(reroll_error)?;
            let candidate_cost = solver
                .expected_lock_cost_for_echo(&candidate)
                .map_err(reroll_error)?;
            if !finite(candidate_cost) {
                return Err(numerical_error());
            }
            Some(json!({
                "accept": accept,
                "current_cost": current_cost,
                "candidate_cost": candidate_cost,
                "candidate_score": f64::from(candidate_score) / 100.0,
            }))
        }
        None => None,
    };
    Ok(json!({
        "mode": "reroll",
        "scoring": input.scoring,
        "current_score": current_score,
        "main_score": main_score,
        "sub_score": f64::from(sub_score) / 100.0,
        "target_internal": target_internal,
        "decision": "reroll",
        "lock_mask": best_lock,
        "lock_types": best_lock.map(mask_types).unwrap_or_default(),
        "resources": {
            "echoes": 0.0,
            "tuners": 0.0,
            "tubes": 0.0,
            "frequencies": current_cost,
            "weighted": current_cost,
            "attempt_probability": probability.unwrap_or(0.0),
        },
        "current_probability": probability,
        "locks": lock_values,
        "comparison": comparison,
        "cached": false,
        "allocated_bytes": solver.allocated_bytes(),
        "elapsed_ms": started.elapsed().as_millis(),
    }))
}

fn joint<S: InternalScorer>(
    input: &PolicyInput,
    scorer: &S,
    echo: &[(usize, u16)],
    sub_score: u16,
    target_internal: u16,
    current_score: f64,
    main_score: f64,
    started: Instant,
) -> ComputeResult<Value> {
    let values = costs(&input.costs);
    let cost_model = CostModel::new(values[0], values[1], values[2]).map_err(cost_error)?;
    let limits = solver_limits(&input.limits)?;
    let mut solver =
        JointPolicySolver::with_limits(scorer, cost_model, values[3], target_internal, limits)
            .map_err(joint_error)?;
    solver.derive_policy(1e-9, 100).map_err(joint_error)?;
    let decision = solver.get_decision(echo).map_err(joint_error)?;
    let probability = solver.get_success_probability(echo).map_err(joint_error)?;
    let resources = solver.calculate_expected_resources().map_err(joint_error)?;
    let weighted = solver.weighted_expected_cost().map_err(joint_error)?;
    let (decision_text, lock_mask) = match decision {
        JointDecision::Upgrade => ("upgrade", None),
        JointDecision::Abandon => ("abandon", None),
        JointDecision::Keep => ("keep", None),
        JointDecision::Reroll { lock_mask } => ("reroll", Some(lock_mask)),
    };
    let locks = if lock_mask.is_some() {
        solver
            .reroll_solver()
            .lock_choices_for_echo(echo, 5)
            .map_err(reroll_error)?
            .iter()
            .map(lock_json)
            .collect::<Vec<_>>()
    } else {
        Vec::new()
    };
    let numbers = [
        resources.echo_per_success(),
        resources.tuner_per_success(),
        resources.exp_per_success(),
        resources.frequency_per_success(),
        weighted,
        probability,
    ];
    if numbers.iter().any(|value| !finite(*value)) {
        return Err(numerical_error());
    }
    Ok(json!({
        "mode": "joint",
        "scoring": input.scoring,
        "current_score": current_score,
        "main_score": main_score,
        "sub_score": f64::from(sub_score) / 100.0,
        "target_internal": target_internal,
        "decision": decision_text,
        "lock_mask": lock_mask,
        "lock_types": lock_mask.map(mask_types).unwrap_or_default(),
        "resources": {
            "echoes": resources.echo_per_success(),
            "tuners": resources.tuner_per_success(),
            "tubes": resources.exp_per_success(),
            "frequencies": resources.frequency_per_success(),
            "weighted": weighted,
            "attempt_probability": resources.success_probability(),
        },
        "current_probability": probability,
        "locks": locks,
        "comparison": Value::Null,
        "cached": false,
        "allocated_bytes": solver.allocated_bytes(),
        "elapsed_ms": started.elapsed().as_millis(),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::scoring::{self, ScoreInput, Stat};

    fn template() -> Value {
        json!({
            "score_max": [67.96, 71.509, 75.509],
            "sub_props": {
                "暴击": 1.0,
                "暴击伤害": 1.0,
                "攻击%": 1.1,
                "攻击": 2.0,
                "共鸣效率": 0.4,
                "技能伤害加成": 1.0
            },
            "skill_weight": [1.0, 1.0, 1.0, 1.0],
            "main_props": {
                "1": {"攻击%": 0.4, "生命%": 0.2, "防御%": 0.2},
                "3": {"攻击%": 0.4, "生命%": 0.3, "防御%": 0.3},
                "4": {"暴击": 0.5, "暴击伤害": 0.25}
            }
        })
    }

    fn small_template() -> Value {
        json!({
            "score_max": [67.96, 71.509, 75.509],
            "sub_props": {
                "暴击": 1.0,
                "暴击伤害": 1.0,
                "攻击%": 1.0
            },
            "skill_weight": [0.0, 0.0, 0.0, 0.0],
            "main_props": {
                "1": {"攻击%": 0.4},
                "3": {"攻击%": 0.4},
                "4": {"暴击": 0.5, "暴击伤害": 0.25}
            }
        })
    }

    fn small_reroll_payload() -> Value {
        let mut value = payload("reroll");
        value["template"] = small_template();
        value["target"] = json!(28.0);
        value
    }

    fn stat(name: &str, value: f64) -> Stat {
        Stat {
            stat_name: name.to_owned(),
            value,
        }
    }

    fn payload(mode: &str) -> Value {
        json!({
            "template": template(),
            "cost": 4,
            "main": [stat("暴击", 22.0), stat("攻击", 150.0)],
            "sub": if mode == "reroll" {
                json!([stat("暴击", 6.3), stat("暴击伤害", 12.6), stat("攻击%", 6.4), stat("共鸣效率", 6.8), stat("攻击", 30.0)])
            } else {
                json!([stat("暴击", 6.3), stat("暴击伤害", 12.6)])
            },
            "mode": mode,
            "target": 35.0,
            "costs": [0.0, 1.0, 0.0, 1000.0]
        })
    }

    fn scoring_payload(scoring: &str, target_internal: u16) -> Value {
        let mut value = payload("upgrade");
        value.as_object_mut().unwrap().remove("template");
        value["scoring"] = json!(scoring);
        value["weights"] = json!(vec![1.0f64; 13]);
        value["fixed_weights"] = json!(vec![1u16; 13]);
        value["target_internal"] = json!(target_internal);
        value["main_score"] = json!(0.0);
        value["max_score"] = json!(100.0);
        value
    }

    #[test]
    fn all_scoring_systems_build_and_solve() {
        for (scoring, target) in [
            ("linear", 100u16),
            ("fixed", 1),
            ("wuwa_echo_tool", 100),
            ("mc_boost_assistant", 100),
            ("qq_bot", 100),
        ] {
            let result = evaluate(scoring_payload(scoring, target), &AtomicBool::new(false))
                .unwrap_or_else(|error| panic!("{scoring}: {error:?}"));
            assert_eq!(result["scoring"], scoring);
            assert!(result["decision"].as_str().is_some());
        }
    }

    #[test]
    fn joint_mode_returns_a_decision() {
        let mut value = payload("upgrade");
        value["template"] = small_template();
        value["mode"] = json!("joint");
        value["target"] = json!(20.0);
        let result = evaluate(value, &AtomicBool::new(false)).unwrap();
        assert_eq!(result["mode"], "joint");
        assert!(result["decision"].as_str().is_some());
    }

    #[test]
    fn scorer_matches_existing_entry_cents() {
        let template = template();
        let scorer = XwuidScorer::new(&template, 4).unwrap();
        let prepared = scoring::prepare(&template, 4).unwrap();
        let base = scoring::score(
            ScoreInput {
                template_key: String::new(),
                cost: 4,
                cost_key: "4".into(),
                main_rows: vec![stat("暴击", 22.0), stat("攻击", 150.0)],
                sub_rows: vec![stat("暴击", 6.3), stat("暴击伤害", 12.6)],
            },
            &template,
            &prepared,
        )
        .unwrap();
        let echo = vec![(0usize, 63u16), (1, 126)];
        let internal = scorer.echo_score_internal(&echo).unwrap();
        let expected: f64 = base.row_scores.iter().skip(2).sum();
        assert!((f64::from(internal) / 100.0 - expected).abs() < 1e-9);
    }

    #[test]
    fn upgrade_returns_a_decision() {
        let result = evaluate(payload("upgrade"), &AtomicBool::new(false)).unwrap();
        assert!(matches!(
            result["decision"].as_str(),
            Some("upgrade" | "abandon" | "keep")
        ));
        assert!(result["current_score"].as_f64().is_some());
    }

    #[test]
    fn reroll_returns_lock_choices() {
        let result = evaluate(small_reroll_payload(), &AtomicBool::new(false)).unwrap();
        assert_eq!(result["decision"].as_str(), Some("reroll"));
        assert!(result["locks"].as_array().is_some());
    }
}
