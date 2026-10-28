# echo_policy

《鸣潮》声骸培养策略求解库。

给定评分规则、目标分数和各类资源的相对价值，`echo_policy` 计算出让你**以最低期望成本得到一件达标声骸**的策略：每开一条副词条后该继续强化还是放弃，满词条后该怎么锁、要不要接受重抽结果。

纯 Rust 实现，无第三方依赖。

## 三个求解器

| 场景 | 求解器 | 回答的问题 |
|---|---|---|
| 只强化，不重抽 | `UpgradePolicySolver` | 这件声骸还要不要继续强化？平均要花多少资源？ |
| 已有满词条声骸，只重抽 | `RerollPolicySolver` | 该锁哪几条？新结果要不要接受？平均要多少频整器？ |
| 强化和重抽都考虑 | `JointPolicySolver` | 下一步是强化、放弃、保留还是重抽？平均要花多少资源？ |

联合求解会把“之后还能重抽”纳入强化决策：允许使用频整器后，一些纯强化策略中原本会放弃的声骸，可能值得继续强化到满词条再重抽。

## 安装

需要支持 Rust 2024 edition 的工具链。以本地路径依赖引入：

```toml
[dependencies]
echo_policy = { path = "../echo-policy" }
```

## 快速开始

目标：只看调谐器消耗，求一件双暴声骸。暴击率、暴击伤害各计 1 分，目标 2 分。

```rust
use echo_policy::{CostModel, FixedScorer, UpgradePolicySolver};

// 13 个权重按词条类型索引排列：0 = 暴击率，1 = 暴击伤害。
let scorer = FixedScorer::new([1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]).unwrap();
let mut solver = UpgradePolicySolver::new(&scorer, 2, CostModel::tuner_only()).unwrap();
solver.lambda_search(1e-9, 100).unwrap();

// 已开出暴击率 6.3% 和攻击 6.4%：要不要继续？
let echo = [(0, 63), (2, 64)];
let keep_going = solver.get_decision_for_echo(&echo).unwrap();
println!("{}", if keep_going { "继续强化" } else { "放弃" });

// 按此策略从头刷，平均每件成品的消耗（已计入失败的尝试）。
let cost = solver.calculate_expected_resources().unwrap();
println!("胚子 {:.1} 个，调谐器 {:.0} 个", cost.echo_per_success(), cost.tuner_per_success());
```

## 使用前须知

**词条的写法。** 一条副词条写作 `(类型索引, 数值)`。百分比属性以 0.1% 为单位（6.3% 写 `63`），固定值属性直接写整数。类型索引表见[使用指南](docs/guide.md#词条与类型索引)。

**目标分数的单位。** 三个求解器的目标都是**内部副词条分**（`u16`）：

- `FixedScorer`：权重就是分数，目标直接写，如上例的 `2`。
- `LinearScorer`：用 `target_from_display` 把面板上的显示总分换算过来，它会扣掉主词条分：

```rust
use echo_policy::{CostModel, LinearScorer, UpgradePolicySolver};

let scorer = LinearScorer::default([1., 1., 0., 0., 0., 0., 0., 0., 0., 0., 0., 0., 0.]).unwrap();
let target = scorer.target_from_display(80.0).unwrap(); // 显示 80 分 → 内部 8000
let solver = UpgradePolicySolver::new(&scorer, target, CostModel::tuner_only()).unwrap();
```

**资源的相对价值。** `CostModel` 设定胚子、调谐器、经验三者的权重；联合求解另外传入频整器权重。例如调谐器权重 `1`、频整器权重 `1000`，表示一个频整器值一千个调谐器。求解器最小化的是加权后的期望总消耗。

## 文档

- [使用指南](docs/guide.md)：数据约定、三个求解器的完整用法、算法原理与模型限制。
- API 参考：运行 `cargo doc --no-deps --open`。

## 开发

```sh
cargo test
cargo clippy --all-targets -- -D warnings
```

本 README 同时是 crate 的根文档，其中的代码示例会作为 doctest 运行。

## 许可证

[MIT](./LICENSE)
