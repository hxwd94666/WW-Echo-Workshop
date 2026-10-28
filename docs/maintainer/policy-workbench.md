# 声骸强化策略与工作台开发文档

> 状态：设计稿，待实现。本文是实施前的合同，不是已完成功能的说明。
>
> 关联项目：[Echo Studio / echo-policy](https://github.com/2-07665/echo-policy)（MIT），核心 crate `echo_policy`；策略内核使用专用仓库，不再跟随计算器应用的 vendored 副本。五套评分与 Joint 的扩展方案见 [评分体系扩展](policy-expansion.md)。
>
> 适用版本：WW-Echo-Workshop 0.1 开发线。
>
> 当前范围：Upgrade + Reroll + Joint（实验）；五套评分可选，Joint 默认不启用。
>
> P0 状态（2026-10-21）：vendored crate、`XwuidScorer`、Upgrade/Reroll、`policy_solve`、默认 limits 与实测已完成。
>
> P1 状态（2026-10-21）：`policy` lane、`PolicyService`、请求构建、结果解析、缓存/去重、`runtime.policy` 生命周期与契约测试已完成。
>
> P2 状态（2026-10-21）：工作台导航、实时策略配置卡、强化测试卡和数据状态卡已实现；设置页移除“常用”分组及重复的资料更新入口。
>
> P3 状态（2026-10-21）：实时评分浮层已接入策略摘要，包含 300 ms 防抖、单请求在途、旧状态失效和最多两行展示。
>
> P4 状态（2026-10-21）：工作台在 900×700 与 500×400、明暗主题下完成离屏构造，强化测试计算动作与摘要契约通过，归档构建通过；真实 OKWW 宿主加载与 11 个强路径契约冒烟通过，真实游戏画面验收未完成。

## 1. 背景与目标

当前实时评分只回答“这件声骸现在多少分”，不回答“下一步继续强化还是放弃、满词条后锁哪几条、要不要重抽”。Echo Studio 的 `echo_policy` 用 DP 精确求解这三类问题，输出决策与期望资源，正好补上这一段。

本开发的目标：

1. 现有三模式之外新增**工作台**页面，聚合实时评分开关、声骸强化测试和数据更新状态。
2. 实时评分增加**可选的强化策略**：在强化页面逐条揭示词条时，异步展示继续/放弃、锁法、期望资源，做到“边强化边展示”。
3. 目标分由用户在工作台输入，不猜测、不自动套用。
4. 只整合策略求解内核；五套评分数据作为起始快照随包，不引入对方的 Web、Tauri、HTTP 服务或 OCR。

非目标：

- 不自动操作游戏，不自动强化、不自动重抽、不写库存。
- 不改变现有评分公式、评级阈值和主词条口径。
- 不把策略结果当作保底或库存上限。
- 不新增 Python 计算实现；所有策略求值仍由 Rust 计算进程执行。

## 2. 术语：三种策略模式

| 模式 | 起点 | 回答的问题 | 消耗资源 |
| --- | --- | --- | --- |
| Upgrade（强化） | 新胚子，逐条揭示 | 继续强化还是放弃重开 | 胚子、调谐器、经验 |
| Reroll（重抽） | 已满五词条 | 锁哪几条重抽、候选接不接受 | 频整器 |
| Joint（联合，实验） | 新胚子，可强化后重抽 | 强化/放弃/保留/重抽四选一 | 上述四种 |

**Joint 是什么**：它把“强化到满词条再决定要不要重抽”合并成一个策略。它不只回答“现在继续还是放弃”，还会回答“满词条后是否值得花频整器重抽”。因此它比 Upgrade 多一层状态和四种资源的期望，求解最慢，结果也最难在浮层里解释清楚。

**Joint 已作为实验模式接入。** 默认不主动推荐，工作台和实时卡需显式选择；求解慢时受 SolverLimits 和超时约束。

## 3. 产品设计

### 3.1 导航与页面

现有导航：声骸库、匹配、配装。新增第四项：**工作台**。

- 工作台是日常操作与状态聚合页，不替代右上角“设置”。
- 设置页继续保留完整配置；工作台只读写同一份配置键，不复制业务状态。
- 工作台采用纵向滚动卡片，宽窄窗都不截断主要操作。

### 3.2 工作台内容

#### 3.2.1 实时评分与策略

- 实时评分总开关（复用现有 `启用声骸评分`）。
- 角色/模态选择（复用 `角色评分模板` 与 `自动匹配评分模板`）。
- **启用强化策略**开关，默认关闭。
- **目标分**输入框（0–50，允许一位小数），用户手动输入；旁边提供“从评级阈值填入”的辅助按钮，但不自动计算。
- 策略模式：自动 / 仅强化 / 仅重抽；自动规则见 3.3。
- 资源相对价值：声骸胚子、调谐器、特级密音筒、频整器四个非负输入，默认 `0/1/0/1000`；0 表示不优化该资源。频整器只在联合模式使用。
- 状态行：当前权重版本、概率表版本、成本表版本、最近一次策略结果摘要。

#### 3.2.2 声骸强化测试

用于离线验证策略和调参，不读写库存：

- 选择库存声骸，或手动构造：COST、两条主词条、0–5 条副词条（类型 + 实际档位）。
- 选择角色/模态、目标分、模式、四项资源相对价值。
- 点击“计算策略”后显示：决策、当前分/目标差、锁法、单次成功率、期望资源、锁法备选。
- 支持“复制参数”和“重置”，不自动保存玩家数据。

#### 3.2.3 数据更新

- **权重资料**：复用现有权重更新与版本显示；提供“检查更新”“导入”入口和当前版本/来源。
- **策略数据**：概率表与成本表随插件编译，不提供在线更新。显示 `Echo Studio / commit / MIT` 来源与版本。
- 更新失败保留旧数据；不执行远程代码。

### 3.3 实时评分与“边强化”

适用页面由现有 `analysis.layout` / `page_kind` 判断：

- `layout == 'enhancement'`（强化页）且副词条 0–4 条：Upgrade。
- 五词条且页面为强化/展示/背包/对比：Reroll。
- Joint 需显式选择；默认自动流程仍只走 Upgrade（0–4 条）和 Reroll（5 条）。

流程：

1. 实时评分照常 OCR、评分、绘制。
2. 若“启用强化策略”开启且目标分已设置，`PolicyService.observe(analysis, settings)` 根据当前词条、角色/模态、目标分、资源相对价值生成缓存键。
3. 键变化且距上次提交超过 300 ms，提交 `policy_solve` 到独立 `policy` lane；同一时间只保留一个在途请求，新的键覆盖待提交键。
4. 浮层先显示“策略计算中…”，完成后替换为结果；旧结果不得冒充当前词条状态。
5. 词条变化、目标变化、角色/模态变化、权重版本变化都会使键失效。

浮层只显示两行策略摘要：

```text
策略：继续强化 · 达标率 38%
期望：42 调谐器 / 3.1 密音筒
```

满五词条时：

```text
策略：重抽 · 锁 暴击/暴伤
单次达标 21% · 期望 6.4 频整器
```

错误/未设置时只显示一行原因，不显示猜测结果：

```text
策略：请先在工作台设置目标分
```

详细结果（锁法备选、资源明细、模式）在工作台“声骸强化测试”或详情对话框查看，不塞进浮层。

### 3.4 配置键

在 `settings_task.default_config` 增加：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `启用强化策略` | `False` | 实时评分是否计算策略 |
| `强化策略目标分` | `0.0` | 0 表示未设置；单位与现有评分一致（总分） |
| `强化策略模式` | `'auto'` | auto / upgrade / reroll；读到 `joint` 或其他未知值时按 `auto` 处理并提示 |
| `强化策略资源预设` | `'tuner'` | 旧配置回退预设；新界面写 `强化策略资源权重` |
| `强化策略资源权重` | `'[0.0,1.0,0.0,1000.0]'` | JSON 四项相对价值：胚子/调谐器/密音筒/频整器 |

旧配置缺键时使用默认值，不阻塞加载，不做破坏性迁移。

## 4. 三个产品口径

### 4.1 Joint 实验范围

Joint 已通过 `mode=joint` 接入工作台、测试卡和实时卡，但默认不启用。

边界：

- 模式选择包含 `auto / upgrade / reroll / joint`；未知值仍回退 `auto`。
- Joint 使用频整器权重和四资源期望，状态空间最大；受 `SolverLimits`、超时和错误提示约束。
- 实时浮层不默认开启 Joint；需要用户显式选择，避免强化过程中加载过慢。
- Joint 需要小模板/真实配置单独验收，当前只有计算契约和小模板测试。

### 4.2 角色/模态入口

- 工作台统一选择，实时评分沿用现有 `角色评分模板` 与 `自动匹配评分模板`。
- 策略与评分共用同一个角色/模态，不允许“评分用 A、策略用 B”。
- 自动匹配开启时，策略使用当帧识别出的角色；识别不确定或回退固定模板时，工作台状态行提示“当前使用固定模板”。
- 声骸库单件没有角色上下文，因此详情页不直接猜角色；需要策略时打开工作台或对话框显式选择。

### 4.3 目标分

- 用户在工作台手动输入，单位是现有评分显示的总分（含主词条），范围 0–50，一位小数。
- 后台计算时扣除主词条分，得到副词条目标；用户不需要理解“内部整数分”。
- 0 或空表示未设置：策略开关可开，但实时评分只显示“请先设置目标分”，不计算。
- 目标分按全局保存；首期不做按角色/模态多套目标。后续如需要，再加可选的“按模板覆盖”，不改变全局语义。
- 不自动取评级阈值；只提供“从评级阈值填入”按钮，点击后仍需用户确认。

## 5. 技术设计

### 5.1 架构

```text
UI 工作台 / 实时评分浮层
  └─ application/policy.py  PolicyService（防抖、缓存、在途请求）
       └─ ComputeJobs lane='policy'
            └─ policy_solve op（Rust）
                 ├─ XwuidScorer（impl echo_policy::InternalScorer）
                 │    └─ scoring::entry_cents / weight（现有评分）
                 ├─ UpgradePolicySolver / RerollPolicySolver / JointPolicySolver（Joint 实验）
                 └─ 返回 decision / resources / locks / comparison

native/compute/vendor/echo_policy/   vendored MIT crate（固定 commit）
```

### 5.2 评分适配

不使用对方的 `LinearScorer`；新增 `XwuidScorer`：

- `buff_score_internal(index, value)`：内部单位转显示值（百分比 /10，固定值原样），按词条名取 `sub_props` 权重（技能伤害走 `sub_props["技能伤害加成"] * skill_weight[i]`），调用现有 `entry_cents(value, weight, score_max)`，返回 `u16`。
- `score_max` 取模板的 `score_max[cost]`（COST 1/3/4 对应 67.96/71.509/75.509 一类值，不是 50）。
- 目标：`target_internal = round((target_total - main_score) * 100)`；`UpgradePolicySolver` 传 `target_total - main_score`，`RerollPolicySolver` 传 `target_internal`。
- 必须写一致性测试：同一词条集合下，`XwuidScorer` 的单条分和总分与 `score_batch` 完全一致。
- 单条 cents 超过 `u16::MAX` 时明确报错，不静默截断。

### 5.3 词条与数值

13 类固定映射：

`暴击、暴击伤害、攻击%、防御%、生命%、攻击、防御、生命、共鸣效率、普攻、重击、共鸣技能、共鸣解放`

- 百分比类显示值 ×10 转内部值；固定值直接取整。
- 用现有 `SUBSTAT_TIERS` 吸附到最近合法档位，转换后必须落在 `echo_policy::data` 的合法档位内。
- 词条数 0–5，类型不重复；Reroll 必须恰好 5 条。
- 重复/非法档位/超出上限返回明确错误，不交给求解器 panic。

### 5.4 概率与成本

- 概率使用 `echo_policy::data` 的内置直方图；我方没有档位概率数据。P0 写测试核对所有档位与 `SUBSTAT_TIERS` 一致。
- 成本使用 `CostModel` 固定表：调谐器、经验返还、频整器。UI 显示“期望值模型，不是保底”，并标注随插件版本的策略数据版本。
- 五套评分预设来自随包 `resources/policy/echo-data.json`；工坊 XWUID 仍使用我方 `WeightSnapshot`。数据更新不覆盖本地自定义。

### 5.5 求解规模

P0 实测（2026-10-21，release，512 MB `search` lane）：

| 模板 | 模式 | 状态/转移 | 耗时 | 保留内存 |
| --- | --- | --- | --- | --- |
| 通用模板（3 个正权重） | Upgrade | — | 约 60 ms | 约 24 MB |
| 通用模板（3 个正权重） | Reroll | 约 0.5 MB 表 | 约 2 ms | 约 0.5 MB |
| 角色 1601（9 个正权重） | Upgrade | — | 约 75 ms | 约 14 MB |
| 角色 1601（9 个正权重） | Reroll | 1,501,533 states / 42,742,009 transitions | 约 3.7 s | 约 245 MB |

- 默认 `SolverLimits`: `max_states=2_000_000`, `max_transitions=64_000_000`；这是 P0 实测后在 512 MB lane 内的保守值。
- 超过默认上限返回 `state_limit`，UI 提示“当前评分/目标组合状态过多，无法精确求解；可降低目标或改用仅强化”。
- 求解不可取消；超时/放弃等待时终止该 lane 进程，UI 明确说明。
- Upgrade 的状态空间远小于 Reroll；实时边强化优先走 Upgrade。

### 5.6 协议

新增 op `policy_solve`：

```json
{
  "template": { "score_max": [67.96, 71.509, 75.509], "sub_props": {}, "skill_weight": [], "main_props": {} },
  "cost": 4,
  "main": [{ "stat_name": "暴击", "value": 22.0 }],
  "sub": [{ "stat_name": "暴击", "value": 6.3 }],
  "mode": "upgrade",
  "target": 35.0,
  "costs": [0, 1, 0, 1000],
  "limits": { "max_states": 250000, "max_transitions": 8000000 }
}
```

响应字段：

`current_score`、`main_score`、`target_internal`、`decision`、`lock_types`、`lock_mask`、`resources`、`current_probability`、`locks`、`comparison`、`elapsed_ms`、`allocated_bytes`、`cached`。

### 5.7 计算 lane 与缓存

- `ComputeJobs` 新增 `policy` lane；`ComputeWorker` 的 lane 白名单同步扩展。
- 内存 512 MB，超时 60 s，队列 1–2；不与 interactive/search 共用进程。
- `PolicyService` 维护 LRU（8–16 条）：键为 `(template_key, cost, mode, 词条元组, target, costs, weight_version)`；命中直接返回，不发计算。
- 在途只保留一个请求；新键只更新“待提交”，当前请求完成后再提交最新键，避免排队爆炸。
- 结果回调按 key 校验；过期结果丢弃，不覆盖新状态。

### 5.8 实时评分集成点

- `runtime` 新增 `policy`（`PolicyService`），随 runtime 关闭；`overlay_task` 只读取缓存/触发 observe。
- `live_summary` 增加可选策略行参数；不在浮层线程里同步求解。
- 浮层最多两行策略；超出时截断为“详情见工作台”。
- 强化页 `layout == 'enhancement'`、`page_kind == 'tuning'`；满词条在展示/背包页走 Reroll。
- 目标分未设置、词条非法、计算中、超时、超限分别有明确短文案。

### 5.9 文件清单

新增/修改：

- `src/ww_echo_workshop/native/compute/vendor/echo_policy/`：vendored crate + LICENSE。
- `src/ww_echo_workshop/native/compute/Cargo.toml`：path 依赖。
- `src/ww_echo_workshop/native/compute/src/policy.rs`：`XwuidScorer`、求解、响应。
- `src/ww_echo_workshop/native/compute/src/lib.rs`：注册 `policy_solve`。
- `src/ww_echo_workshop/application/policy.py`：`PolicyService`、请求构建、缓存/去重。
- `src/ww_echo_workshop/application/runtime.py`：持有并关闭 `PolicyService`。
- `src/ww_echo_workshop/core/policy.py`：DTO、输入边界与模式解析。
- `tests/contracts/test_policy_service.py`：请求、结果、缓存与 lane 契约。
- `src/ww_echo_workshop/ui/workspace/page.py`：工作台。
- `src/ww_echo_workshop/ui/workspace/live_card.py`、`test_card.py`、`data_card.py`。
- `src/ww_echo_workshop/ui/shell.py`：导航第四项。
- `src/ww_echo_workshop/adapters/okww/overlay_task.py`、`live_presentation.py`：策略行。
- `src/ww_echo_workshop/adapters/okww/live_policy.py`：实时策略请求、防抖与旧状态失效。
- `tests/contracts/test_live_policy.py`：实时摘要与防抖契约。
- `src/ww_echo_workshop/adapters/okww/settings_task.py`：配置键与隐藏项。
- `src/ww_echo_workshop/ui/inventory/page.py`：详情入口（可选，打开工作台策略测试）。
- `src/ww_echo_workshop/application/compute_jobs.py`、`compute_protocol.py`：policy lane。
- `build.py`：vendor 白名单与 source_id。
- `THIRD_PARTY_NOTICES.md`：MIT 声明。
- `docs/README.md`：索引。

### 5.10 构建与许可

- vendored crate 保留上游 `LICENSE` 与来源 commit；`build.py` 的 `native_members` 白名单允许 vendor 下 `.rs`/`Cargo.toml`/`LICENSE`，并把 vendor 源码纳入 `source_id`。
- ZIP 仍只打包编译后的 EXE 与 manifest，不打包 vendor 源码。
- 五体系起始数据随包；概率/成本表来自 `echo_policy` 代码，随插件版本分发。玩家可用代理或导入替换数据包。

## 6. 分阶段计划

**P0 PoC（只改 Rust 与构建）**
- vendor crate、五体系、Upgrade/Reroll/Joint 可运行；Joint 仅小模板测试，真实配置性能未验收；
- 一致性测试、典型模板耗时/内存、limits 实测；
- 验收：分数与 `score_batch` 一致；Upgrade/Reroll 返回合理结果；记录耗时/内存。

**P1 计算接口**
- `policy_solve`、`policy` lane、`PolicyService`、缓存/防抖；
- 验收：目标/词条/模式/超限/超时/取消路径；不阻塞评分。

**P2 工作台**
- 导航、三张卡片、配置键、目标分/开关/测试/数据状态；
- 验收：宽窄窗、明暗主题、配置持久化、旧配置缺键。

**P3 实时评分**
- 浮层策略行、强化页边强化、满词条重抽、错误文案；
- 验收：词条变化触发/不重复；旧结果不冒充新状态；计算中可读。

**P4 验收与文档**
- 对照测试、性能记录、用户文档、第三方声明；
- 验收：发布清单+真实游戏手动检查；未验证项明确记录。

## 7. 测试与验收

- Rust 单元：`XwuidScorer` 与 `score_batch` 一致；目标/主词条转换；非法档位；`u16` 上限。
- 对照：用固定模板/词条，与 Echo Studio `echo_app::Service` 的 decision/resources/locks 对比（容差 1e-9）。
- Python contract：payload 构建、结果映射、目标未设置、超限、超时、缓存键失效。
- UI 手动：工作台开关/目标分/测试卡片；强化页边强化；满词条 Reroll；计算中/错误/超时；宽窄窗与明暗主题。
- 性能：典型 XWUID 模板 Upgrade/Reroll 的耗时/峰值内存；只跑相关用例，不跑全量。
- 未验证：不同游戏版本的概率偏差、真实 OKWW 长时间运行；Joint 真实配置性能未验收。

## 8. 风险与未决

| 风险 | 应对 |
| --- | --- |
| 浮层空间不足 | 最多两行；详细结果进工作台 |
| 求解延迟与强化节奏不匹配 | 防抖、单在途、显示计算中；不显示旧状态 |
| 概率/成本表过时 | 标注来源与版本；随插件版本更新 |
| 目标分语义混淆 | UI 明确“总分（含主词条）”；后台扣主词条 |
| 角色识别不确定 | 回退固定模板并提示；不猜 |
| Joint 实验 | 已接入；不默认启用，受状态/转移上限和超时约束 |
| 上游停更 | vendored 固定 commit，保留 MIT；自行维护 |
| 许可 | 保留 MIT/XWUID 声明；五体系起始数据来源和许可风险见 `THIRD_PARTY_NOTICES.md` |

## 9. 验证记录（2026-10-21）

- 宿主环境冒烟：`QT_QPA_PLATFORM=offscreen <OKWW venv python> -B tools/run_checks.py --smoke` → `SMOKE OK: 11 contract modules`。
- 工作台离屏构造：900×700 明色、500×400 暗色均通过；强化测试卡点击后显示“建议”和“调谐器”。
- 真实引擎摘要：`LivePolicyHint` + policy lane 输出两行“重抽”摘要，低概率显示 `<0.1%`。
- 未完成：真实 OKWW 窗口内的浮层遮挡/字号、游戏前台背包/展示/对比/强化页的实际识别与策略刷新，需要真实宿主与游戏画面验收；offscreen 构造和契约测试不能替代该证据。

## 10. 许可与来源

- `echo_policy`：MIT，Copyright (c) 2025 2-07665；随包保留 LICENSE 文本与来源。
- 概率表/成本表：来自 `echo_policy` 源码，MIT；在工作台与第三方声明中标注。
- 角色权重：我方 XWUID 快照，继续遵循现有来源与许可说明。
- 不复制对方 UI、品牌、预设商店或远程服务。
