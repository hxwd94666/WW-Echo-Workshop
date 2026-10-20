// 共享预算的独占五件搜索，先改善达标再用保守冲突上界证明。
use crate::allocation_model::{Build, Input, Problem};
use crate::objective::Objective;
use crate::{ComputeError, ComputeResult, checkpoint};
use serde_json::{Value, json};
use std::sync::atomic::AtomicBool;
use std::time::{Duration, Instant};

struct Budget<'a> {
    cancel: &'a AtomicBool,
    started: Instant,
    duration: Duration,
    maximum: u64,
    nodes: u64,
    stop: &'static str,
}
impl Budget<'_> {
    fn alive(&mut self) -> ComputeResult<bool> {
        checkpoint(self.cancel)?;
        if !self.stop.is_empty() {
            return Ok(false);
        }
        if self.nodes >= self.maximum {
            self.stop = "node_limit";
        } else if self.started.elapsed() >= self.duration {
            self.stop = "time_limit";
        }
        Ok(self.stop.is_empty())
    }
    fn tick(&mut self) -> ComputeResult<bool> {
        if !self.alive()? {
            return Ok(false);
        }
        self.nodes += 1;
        Ok(true)
    }
}

struct Search<'p, 'b, 'c> {
    p: &'p Problem,
    budget: &'b mut Budget<'c>,
    group: Vec<usize>,
    order: Vec<usize>,
    rows: Vec<Build>,
    best: Vec<Build>,
    best_rank: Objective,
    used: Vec<bool>,
    base_used: Vec<bool>,
    // For each suffix and COST, each physical UUID occurs only once.
    rankings: Vec<[Vec<(usize, i64)>; 3]>,
    minimum_moves: Vec<[i64; 5]>,
    improvements: u64,
    first_complete_ms: Option<f64>,
    first_all_met_ms: Option<f64>,
    last_improvement_ms: f64,
}

fn rank(p: &Problem, group: &[usize], rows: &[Build]) -> Objective {
    let mut totals = [0; 8];
    let mut targets = [0; 8];
    let mut complete = 0;
    let mut moves = 0;
    for (i, &r) in group.iter().enumerate() {
        totals[i] = rows[r].total;
        targets[i] = p.targets[r];
        complete += usize::from(rows[r].ids.len() == 5);
        moves += rows[r].moves;
    }
    Objective::new(
        &totals[..group.len()],
        &targets[..group.len()],
        complete,
        moves,
    )
}

fn cost_index(cost: u8) -> usize {
    match cost {
        1 => 0,
        3 => 1,
        4 => 2,
        _ => unreachable!(),
    }
}

impl<'p, 'b, 'c> Search<'p, 'b, 'c> {
    fn new(
        p: &'p Problem,
        budget: &'b mut Budget<'c>,
        group: Vec<usize>,
        base_used: Vec<bool>,
    ) -> Self {
        let mut order = group.clone();
        order.sort_by_key(|&r| {
            (
                p.pools[r].iter().map(Vec::len).min().unwrap(),
                p.pools[r].iter().map(Vec::len).sum::<usize>(),
                p.roles[r].key(),
            )
        });
        let rows = vec![Build::default(); p.roles.len()];
        let best_rank = rank(p, &group, &rows);
        let mut rankings = Vec::new();
        for index in 0..=order.len() {
            let mut lists: [Vec<(usize, i64)>; 3] = std::array::from_fn(|_| Vec::new());
            for item in 0..p.records.len() {
                let maximum = order[index..]
                    .iter()
                    .filter(|r| p.masks[**r][item] != 0)
                    .filter_map(|r| p.scores[*r][item])
                    .max();
                if let Some(score) = maximum {
                    lists[cost_index(p.records[item].cost)].push((item, score));
                }
            }
            for list in &mut lists {
                list.sort_by(|(a, av), (b, bv)| bv.cmp(av).then(a.cmp(b)));
            }
            rankings.push(lists);
        }
        let minimum_moves = p
            .pools
            .iter()
            .enumerate()
            .map(|(r, slots)| {
                std::array::from_fn(|s| slots[s].iter().map(|i| p.moves[r][*i]).min().unwrap_or(0))
            })
            .collect();
        Self {
            p,
            budget,
            group,
            order,
            rows: rows.clone(),
            best: rows,
            best_rank,
            used: base_used.clone(),
            base_used,
            rankings,
            minimum_moves,
            improvements: 0,
            first_complete_ms: None,
            first_all_met_ms: None,
            last_improvement_ms: 0.0,
        }
    }
    fn update(&mut self) {
        let current = rank(self.p, &self.group, &self.rows);
        if current > self.best_rank {
            self.best_rank = current;
            self.best.clone_from(&self.rows);
            self.improvements += 1;
            let now = self.budget.started.elapsed().as_secs_f64() * 1000.0;
            self.last_improvement_ms = now;
            if self.best_rank.complete == self.group.len() && self.first_complete_ms.is_none() {
                self.first_complete_ms = Some(now);
            }
            if self.best_rank.complete == self.group.len()
                && self.best_rank.met
                    == self
                        .group
                        .iter()
                        .filter(|r| self.p.targets[**r] > 0)
                        .count()
                && self.first_all_met_ms.is_none()
            {
                self.first_all_met_ms = Some(now);
            }
        }
    }
    fn reset(&mut self) {
        self.used.clone_from(&self.base_used);
        for &r in &self.group {
            self.rows[r] = Build::default();
        }
    }
    fn partial_legal(&self, role: usize, ids: &[usize]) -> bool {
        if self.p.roles[role].distinct_names
            && ids.iter().enumerate().any(|(i, a)| {
                ids[..i]
                    .iter()
                    .any(|b| self.p.names[*a] == self.p.names[*b])
            })
        {
            return false;
        }
        for &(set, minimum) in &self.p.requirements[role] {
            let mut names = [usize::MAX; 5];
            let mut count = 0;
            for &id in ids {
                if self.p.sets[id] == set && !names[..count].contains(&self.p.names[id]) {
                    names[count] = self.p.names[id];
                    count += 1;
                }
            }
            if count + 5 - ids.len() < minimum {
                return false;
            }
        }
        !self.p.energy_max[role].is_some_and(|maximum| self.p.energy_total(role, ids) > maximum)
    }
    fn symmetry(&self, role: usize, slot: usize, item: usize, ids: &[usize]) -> bool {
        let spec = &self.p.roles[role].slots[slot];
        spec.fixed_item_id.is_empty()
            && (0..slot)
                .rev()
                .find(|&j| self.p.roles[role].slots[j] == *spec)
                .is_some_and(|j| self.p.uuid_rank[item] <= self.p.uuid_rank[ids[j]])
    }
    fn seed(
        &mut self,
        role: usize,
        slot: usize,
        ids: &mut Vec<usize>,
        total: i64,
        goal: i64,
        limit: u64,
    ) -> ComputeResult<bool> {
        if self.budget.nodes >= limit || !self.budget.tick()? {
            return Ok(false);
        }
        if !self.partial_legal(role, ids) {
            return Ok(false);
        }
        if slot == 5 {
            return Ok(total >= goal && self.p.legal_names_sets(role, ids));
        }
        let upper: i64 = self.p.pools[role][slot..]
            .iter()
            .map(|pool| {
                pool.iter()
                    .find(|i| !self.used[**i])
                    .map_or(0, |i| self.p.scores[role][*i].unwrap())
            })
            .sum();
        if total + upper < goal {
            return Ok(false);
        }
        let mut candidates = self.p.pools[role][slot].clone();
        if goal > 0 {
            let desired = ((goal - total).max(0) + (4 - slot) as i64) / (5 - slot) as i64;
            candidates.sort_by_key(|i| {
                (
                    (self.p.scores[role][*i].unwrap() - desired).abs(),
                    self.p.uuid_rank[*i],
                )
            });
        }
        for (scanned, item) in candidates.into_iter().enumerate() {
            if scanned.is_multiple_of(64) && !self.budget.alive()? {
                return Ok(false);
            }
            if self.used[item] {
                continue;
            }
            ids.push(item);
            self.used[item] = true;
            if self.seed(
                role,
                slot + 1,
                ids,
                total + self.p.scores[role][item].unwrap(),
                goal,
                limit,
            )? {
                return Ok(true);
            }
            self.used[item] = false;
            ids.pop();
        }
        Ok(false)
    }
    fn bound(&self, index: usize, partial: Option<(usize, usize, i64, i64)>) -> Option<Objective> {
        let mut totals = [0; 8];
        let mut targets = [0; 8];
        let mut complete = 0;
        let mut fixed_total = 0;
        let mut moved = 0;
        let full_best = self.best_rank.complete == self.group.len()
            && self.best_rank.met
                == self
                    .group
                    .iter()
                    .filter(|r| self.p.targets[**r] > 0)
                    .count()
            && self.best_rank.numerator == self.best_rank.denominator;
        let mut needed = [0_usize; 3];
        for (j, &r) in self.order.iter().enumerate() {
            targets[j] = self.p.targets[r];
            if j < index {
                totals[j] = self.rows[r].total;
                fixed_total += totals[j];
                moved += self.rows[r].moves;
                complete += usize::from(self.rows[r].ids.len() == 5);
                continue;
            }
            let (start, total, moves) = partial
                .filter(|(role, _, _, _)| *role == r)
                .map_or((0, 0, 0), |(_, s, t, m)| (s, t, m));
            let mut upper = total;
            let mut possible = true;
            let mut minimum_moves = moves;
            for slot in start..5 {
                needed[cost_index(self.p.roles[r].slots[slot].cost)] += 1;
                if let Some(item) = self.p.pools[r][slot].iter().find(|i| !self.used[**i]) {
                    upper += self.p.scores[r][*item].unwrap();
                } else {
                    possible = false;
                }
                minimum_moves += self.minimum_moves[r][slot];
            }
            if !possible && (full_best || start > 0) {
                return None;
            }
            totals[j] = if possible { upper } else { 0 };
            complete += usize::from(possible);
            if full_best {
                moved += minimum_moves;
            } else {
                moved += moves;
            }
        }
        let partial_score = partial.map_or(0, |(_, _, score, _)| score);
        let mut total_bound = fixed_total + partial_score;
        for (cost, &need) in needed.iter().enumerate() {
            total_bound += self.rankings[index][cost]
                .iter()
                .filter(|(i, _)| !self.used[*i])
                .take(need)
                .map(|(_, s)| s)
                .sum::<i64>();
        }
        let mut value = Objective::new(
            &totals[..self.group.len()],
            &targets[..self.group.len()],
            complete,
            moved,
        );
        value.total = value.total.min(total_bound);
        value.utility = value.utility.min(total_bound);
        Some(value)
    }
    fn assign_subset(
        &mut self,
        role: usize,
        subset: &[usize],
        slot: usize,
        mask: u8,
        chosen: &mut Vec<usize>,
    ) -> ComputeResult<bool> {
        if !self.budget.tick()? {
            return Ok(false);
        }
        if !self.partial_legal(role, chosen) {
            return Ok(false);
        }
        if slot == 5 {
            return Ok(self.p.legal_names_sets(role, chosen));
        }
        for (j, &item) in subset.iter().enumerate() {
            if mask & (1 << j) != 0
                || self.p.masks[role][item] & (1 << slot) == 0
                || self.symmetry(role, slot, item, chosen)
            {
                continue;
            }
            chosen.push(item);
            if self.assign_subset(role, subset, slot + 1, mask | (1 << j), chosen)? {
                return Ok(true);
            }
            chosen.pop();
        }
        Ok(false)
    }
    fn rebalance(&mut self) -> ComputeResult<()> {
        let mut pairs = Vec::new();
        for (i, &a) in self.group.iter().enumerate() {
            for &b in &self.group[i + 1..] {
                let deficient = i64::from(self.p.targets[a] > self.best[a].total)
                    + i64::from(self.p.targets[b] > self.best[b].total);
                let excess = (self.best[a].total - self.p.targets[a]).max(0)
                    + (self.best[b].total - self.p.targets[b]).max(0);
                pairs.push((deficient, excess, a, b));
            }
        }
        pairs.sort_by(|a, b| b.cmp(a));
        for (_, _, a, b) in pairs {
            if !self.budget.alive()? {
                break;
            }
            if self.best[a].ids.len() != 5 || self.best[b].ids.len() != 5 {
                continue;
            }
            let union: Vec<_> = self.best[a]
                .ids
                .iter()
                .chain(&self.best[b].ids)
                .copied()
                .collect();
            for mask in 0_u16..1024 {
                if mask.count_ones() != 5 {
                    continue;
                }
                if !self.budget.alive()? {
                    break;
                }
                let mut left = Vec::with_capacity(5);
                let mut right = Vec::with_capacity(5);
                for (j, &id) in union.iter().enumerate() {
                    if mask & (1 << j) != 0 {
                        left.push(id);
                    } else {
                        right.push(id);
                    }
                }
                let mut first = Vec::with_capacity(5);
                let mut second = Vec::with_capacity(5);
                if !self.assign_subset(a, &left, 0, 0, &mut first)?
                    || !self.assign_subset(b, &right, 0, 0, &mut second)?
                {
                    continue;
                }
                self.rows.clone_from(&self.best);
                self.rows[a] = self.p.build(a, first);
                self.rows[b] = self.p.build(b, second);
                self.update();
                if self
                    .bound(0, None)
                    .is_none_or(|upper| upper <= self.best_rank)
                {
                    self.reset();
                    return Ok(());
                }
            }
        }
        self.reset();
        Ok(())
    }
    fn visit(&mut self, index: usize) -> ComputeResult<()> {
        if !self.budget.tick()? {
            return Ok(());
        }
        self.update();
        if index == self.order.len()
            || self
                .bound(index, None)
                .is_none_or(|upper| upper <= self.best_rank)
        {
            return Ok(());
        }
        let role = self.order[index];
        self.slots(index, role, 0, &mut Vec::with_capacity(5), 0, 0)?;
        if self.budget.alive()? {
            self.visit(index + 1)?;
        }
        Ok(())
    }
    fn slots(
        &mut self,
        role_index: usize,
        role: usize,
        slot: usize,
        ids: &mut Vec<usize>,
        total: i64,
        moves: i64,
    ) -> ComputeResult<()> {
        if !self.budget.tick()? || !self.partial_legal(role, ids) {
            return Ok(());
        }
        if self
            .bound(role_index, Some((role, slot, total, moves)))
            .is_none_or(|upper| upper <= self.best_rank)
        {
            return Ok(());
        }
        if slot == 5 {
            if self.p.legal_names_sets(role, ids) {
                self.rows[role] = self.p.build(role, ids.clone());
                self.visit(role_index + 1)?;
                self.rows[role] = Build::default();
            }
            return Ok(());
        }
        for position in 0..self.p.pools[role][slot].len() {
            if position.is_multiple_of(64) && !self.budget.alive()? {
                return Ok(());
            }
            let item = self.p.pools[role][slot][position];
            if self.used[item] || self.symmetry(role, slot, item, ids) {
                continue;
            }
            ids.push(item);
            self.used[item] = true;
            self.slots(
                role_index,
                role,
                slot + 1,
                ids,
                total + self.p.scores[role][item].unwrap(),
                moves + self.p.moves[role][item],
            )?;
            self.used[item] = false;
            ids.pop();
            if !self.budget.stop.is_empty() {
                break;
            }
        }
        Ok(())
    }
    fn solve(&mut self) -> ComputeResult<()> {
        let mut orders = Vec::new();
        for mut direction in [
            self.order.clone(),
            self.order.iter().rev().copied().collect(),
        ] {
            for _ in 0..direction.len() {
                if !orders.contains(&direction) {
                    orders.push(direction.clone());
                }
                direction.rotate_left(1);
            }
        }
        for balanced in [false, true] {
            for (iteration, order) in orders.iter().enumerate() {
                self.reset();
                for &r in order {
                    let mut ids = Vec::with_capacity(5);
                    let limit = (self.budget.nodes + 2000).min(self.budget.maximum);
                    let goal = if balanced { self.p.targets[r] } else { 0 };
                    if self.seed(r, 0, &mut ids, 0, goal, limit)? {
                        for first in 0..5 {
                            if !self.p.roles[r].slots[first].fixed_item_id.is_empty()
                                || (0..first).any(|j| {
                                    self.p.roles[r].slots[j] == self.p.roles[r].slots[first]
                                })
                            {
                                continue;
                            }
                            let positions: Vec<_> = (first..5)
                                .filter(|&j| {
                                    self.p.roles[r].slots[j] == self.p.roles[r].slots[first]
                                })
                                .collect();
                            let mut canonical: Vec<_> = positions.iter().map(|j| ids[*j]).collect();
                            canonical.sort_by_key(|i| self.p.uuid_rank[*i]);
                            for (position, item) in positions.into_iter().zip(canonical) {
                                ids[position] = item;
                            }
                        }
                        self.rows[r] = self.p.build(r, ids);
                    }
                    self.update();
                    if !self.budget.alive()? {
                        break;
                    }
                }
                self.reset();
                if self
                    .bound(0, None)
                    .is_none_or(|upper| upper <= self.best_rank)
                {
                    return Ok(());
                }
                if !balanced && iteration == 0 {
                    self.rebalance()?;
                }
                if self
                    .bound(0, None)
                    .is_none_or(|upper| upper <= self.best_rank)
                {
                    return Ok(());
                }
                if !self.budget.alive()? {
                    return Ok(());
                }
            }
        }
        self.reset();
        self.visit(0)
    }
}

fn reasons(p: &Problem, r: usize, build: &Build, stop: &str, computed: bool) -> Vec<String> {
    let mut result = Vec::new();
    if !computed {
        result.push("预算已用尽，尚未计算本组；不表示无解".into());
    }
    result.push(format!(
        "候选槽位数量 {:?}；排除计数 {:?}",
        p.pools[r].each_ref().map(Vec::len),
        p.diagnostics[r]
    ));
    if build.ids.is_empty() {
        result.push(if stop.is_empty() {
            "最优公平方案中该角色未配齐五件；候选或组合/独占约束不足".into()
        } else {
            "未配齐五件：候选/固定件/主词条/套装/同名/独占约束；限时结果不证明无解".into()
        });
    } else if build.total < p.targets[r] {
        result.push(format!(
            "距插件目标分差 {:.2}；公平/独占及约束限制{}",
            (p.targets[r] - build.total) as f64 / 100.0,
            if stop.is_empty() {
                ""
            } else {
                "；搜索未穷尽"
            }
        ));
    }
    let upper: i64 = p.pools[r]
        .iter()
        .map(|pool| {
            pool.iter()
                .filter_map(|i| p.scores[r][*i])
                .max()
                .unwrap_or(0)
        })
        .sum();
    if upper < p.targets[r] {
        result.push("即使忽略独占/同名/套装数量，单角色槽位分数上界仍低于目标".into());
    }
    result
}

fn run(problem: Problem, cancel: &AtomicBool, started: Instant) -> ComputeResult<Value> {
    let mut budget = Budget {
        cancel,
        started,
        duration: Duration::from_secs_f64(problem.seconds),
        maximum: problem.max_nodes,
        nodes: 0,
        stop: "",
    };
    let mut final_rows = vec![Build::default(); problem.roles.len()];
    let mut used = vec![false; problem.records.len()];
    let mut statistics = Vec::new();
    let mut computed = vec![false; problem.roles.len()];
    for group in &problem.groups {
        if !budget.alive()? {
            break;
        }
        let mut search = Search::new(&problem, &mut budget, group.clone(), used.clone());
        search.solve()?;
        statistics.push(json!({"improvements":search.improvements,"first_complete_ms":search.first_complete_ms,
            "first_all_met_ms":search.first_all_met_ms,"last_improvement_ms":search.last_improvement_ms}));
        for &r in group {
            computed[r] = true;
            final_rows[r] = search.best[r].clone();
            for &id in &final_rows[r].ids {
                used[id] = true;
            }
        }
    }
    let rows: Vec<_> = final_rows
        .iter()
        .enumerate()
        .map(|(r, build)| {
            let scores: Vec<i64> = build
                .ids
                .iter()
                .map(|i| problem.scores[r][*i].unwrap())
                .collect();
            let template = problem.templates.get(&format!(
                "{}/{}",
                problem.roles[r].character_id, problem.roles[r].modal_id
            ));
            // 评级与分值同源冻结：这里只求值一次，界面与方案载荷直接消费。
            let item_ratings: Vec<_> = build
                .ids
                .iter()
                .zip(&scores)
                .map(|(index, score)| {
                    template
                        .and_then(|value| {
                            crate::rating::standard(value, Some(problem.records[*index].cost))
                        })
                        .and_then(|standard| crate::rating::rate(&standard, *score as f64 / 100.0))
                })
                .collect();
            let rating = template.and_then(|value| crate::rating::loadout(value, &scores));
            json!({
                "character_id":problem.roles[r].character_id,"modal_id":problem.roles[r].modal_id,"label":problem.labels[r],
                "item_ids":build.ids.iter().map(|i|&problem.records[*i].item_id).collect::<Vec<_>>(),
                "scores":scores,
                "total_cents":build.total,"target_cents":problem.targets[r],"moves":build.moves,
                "energy_regen":problem.energy_total(r, &build.ids) as f64 / 10.0,
                "energy_regen_min":problem.roles[r].energy_regen_min,
                "energy_regen_max":problem.roles[r].energy_regen_max,
                "rating":rating,"item_ratings":item_ratings,
                "reasons":reasons(&problem, r, build, budget.stop, computed[r])
            })
        })
        .collect();
    let value = rank(
        &problem,
        &(0..problem.roles.len()).collect::<Vec<_>>(),
        &final_rows,
    );
    Ok(
        json!({"roles":rows,"objective":value.as_json(),"minimum_ratio":[value.numerator,value.denominator],
        "policy_version":crate::objective::policy_version(problem.explicit_groups),
        "exhaustive":budget.stop.is_empty(),"stop_reason":budget.stop,"nodes":budget.nodes,
        "elapsed":budget.started.elapsed().as_secs_f64(),"statistics":statistics}),
    )
}

pub fn batch(payload: Value, cancel: &AtomicBool) -> ComputeResult<Value> {
    let started = Instant::now();
    let input: Input = serde_json::from_value(payload)
        .map_err(|e| ComputeError::new("invalid_request", e.to_string()))?;
    run(
        Problem::compile(input, None, cancel, false)?,
        cancel,
        started,
    )
}

pub fn matrix_batch(payload: Value, cancel: &AtomicBool) -> ComputeResult<Value> {
    let started = Instant::now();
    let matrix = serde_json::from_value(payload["matrix_cents"].clone())
        .map_err(|e| ComputeError::new("invalid_request", e.to_string()))?;
    let input: Input = serde_json::from_value(payload)
        .map_err(|e| ComputeError::new("invalid_request", e.to_string()))?;
    run(
        Problem::compile(input, Some(matrix), cancel, false)?,
        cancel,
        started,
    )
}
