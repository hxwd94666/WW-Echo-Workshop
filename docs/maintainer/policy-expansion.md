# 评分体系、联合策略与策略数据扩展方案

> 状态：设计稿，P5 部分实现。本文承接 [声骸强化策略与工作台](policy-workbench.md)，记录五套评分体系、Joint、候选比较、锁法列表、资源明细、SolverLimits 和国内更新的完整方案。
>
> P5 进展（2026-10-21）：Rust 五体系 scorer、Python 请求字段、数据包校验/预设模型和对应测试已完成；数据包随包/UI 尚未接入，等待 Wuwa/MC 许可和镜像决定。
>
> P7 进展（2026-10-22）：五体系、Joint、候选、锁法、资源明细、角色/变体预设、实时评分体系已接入。五套评分起始数据已随包；不再依赖项目镜像，玩家通过代理更新或手动导入替换。实时预设自动匹配尚未接入。
>
> 目标不是把五套评分混进现有 XWUID 库存评分，而是在策略侧提供可验证、可切换的评分口径，并保留工坊默认 XWUID 行为。

## 1. 结论

五种评分体系可以全部接入。上游 `data/echo-data.json`（schema 1）已经包含：

| 体系 | 预设 | 变体 | 默认参数 |
|---|---:|---:|---|
| linear 线性数值 | 1 个默认 | 1 | 权重 + main/max + target 70 |
| fixed 固定词条 | 0 | 0 | 13 个整数权重 + 整数目标 |
| wuwa_echo_tool | 60 角色 | 284 | 权重 + target 70 / main 0 / max 100 |
| mc_boost_assistant | 15 角色 | 15 | 权重 + target 95 / max 120 |
| qq_bot（XWUID） | 61 角色 | 278 | 权重 + mainBuffScore / cost / mainStats / sourceScoreMax |

但必须区分两件事：

- **算法**：五套 scorer 需要一次插件版本更新；
- **数据**：角色、变体、权重、目标/主词条参数可以后续通过 JSON 数据包更新，但前提是我们自己维护镜像和生成链，不能依赖已停更的上游合并包。

## 2. 五套评分的实现

### 2.1 Rust 评分适配

新增 `PolicyScoring` 请求字段，保留 `workshop_xwuid` 作为默认：

```json
{
  "scoring": "workshop_xwuid | linear | fixed | wuwa_echo_tool | mc_boost_assistant | qq_bot",
  "weights": [13 numbers],
  "main_score": 0.0,
  "max_score": 100.0,
  "fixed_weights": [13 integers],
  "target": 70.0
}
```

在 [policy.rs](../src/ww_echo_workshop/native/compute/src/policy.rs) 增加：

| 体系 | 构造 |
|---|---|
| workshop_xwuid | 现有 `XwuidScorer` + template，不改变库存评分 |
| linear | `LinearScorer::new(weights, main, max)` |
| fixed | `FixedScorer::new([u16;13])` |
| wuwa_echo_tool | `LinearScorer::new(weights, 0, 100)` |
| mc_boost_assistant | `LinearScorer::mc_boost_assistant_scorer(weights)` |
| qq_bot | `LinearScorer::qq_bot_scorer(weights, main_score)` |

目标统一使用内部 u16：Linear 用 `target_from_display`，Fixed 直接目标整数，XWUID 沿用现有主词条扣减。

**口径警告**：上游 `qq_bot` 的 50 分归一化与工坊现有逐项截断 XWUID 评分可能有细微差异。策略界面必须显示当前评分体系，不能把策略分和库存分当成同一个数。

### 2.2 数据加载

新增：

- `src/ww_echo_workshop/resources/policy/echo-data.json`：内置数据包；
- `src/ww_echo_workshop/resources/policy/sources.json`：URL、版本、commit、SHA256；
- `application/policy_data.py`：加载、校验、按体系索引；
- `core/policy_presets.py`：体系枚举、字段映射、默认 target/main/max。

校验：

- `schema_version == 1`；
- 只接受已知五体系和 13 个词条 ID；
- 权重有限非负；fixed 为整数；
- 角色/变体/ID 唯一；
- 文件 ≤ 8 MiB；
- 坏数据保留上一份有效缓存。

### 2.3 界面

工作台实时策略卡增加：

- 评分体系：XWUID（工坊默认）/ 线性 / 固定 / Wuwa / 漂泊者强化助手；数据包内部的 `qq_bot` 就是 XWUID，界面不再单独展示。
- 角色预设和变体；linear/fixed 显示“手动配置”；
- 目标分、主词条分、归一满分（按体系显示）；
- 13 项权重编辑；
- 四项资源相对价值；
- 说明“此评分只用于策略，不改变声骸库/匹配/配装的 XWUID 分数”。

强化测试卡增加：

- 同一套评分体系选择；
- 候选声骸（5 条完整副词条）；
- 锁法备选表（锁法、期望、单次成功率、regret、频整器）；
- 完整资源（胚子、调谐器、密音筒、频整器、加权期望）；
- SolverLimits 高级输入。

## 3. Joint、候选与锁法

- Rust 开放 `JointPolicySolver`，`mode=joint`，频整器权重生效；
- Python `mode_for_record` 允许 joint；
- 请求支持 `candidate`，重抽/联合显示候选比较；
- 结果已有 `locks`，测试卡完整展示，实时浮层仍只显示最优锁法；
- Joint 走独立 `policy` lane，设置超时、状态/转移上限和错误提示；
- 首期实时浮层不默认开启 Joint，工作台显式选择。

## 4. 实时浮层

- 浮层使用实时策略卡选定的评分体系/预设；
- `qq_bot` 可用 `sourceTemplate.characterId` 自动匹配；Wuwa/MC 只能按名称匹配，匹配不到回退固定预设并提示；
- 非工坊 XWUID 体系时，浮层标题标注“策略分（体系名）”；
- 浮层最多两行，候选/锁法明细只进工作台。

## 5. 国内更新：现实情况

### 5.1 没有五套独立的资源仓库

上游没有一个“五套评分统一更新 API”，也没有五套各自可拉取的数据仓库。现实是：

| 来源 | 是否有独立更新源 | 说明 |
|---|---|---|
| linear | 否 | 应用内默认/手动权重 |
| fixed | 否 | 应用内手动整数权重 |
| XWUID / qq_bot | 有源仓库 | `IceHe/okww-xwuid-echo-score`（GPL-3.0）与 XWUID 资源目录；需要上游转换脚本重新生成 |
| Wuwa Echo Tool | 有源仓库 | `GQin404/WuwaEchoTool`；权重内嵌 `js/base.js`，需运行提取脚本生成预设，没有独立数据仓库/发布物 |
| MC Boost Assistant | 否 | 微信小程序，没有公开数据仓库；只有上游已转换快照 |
| 五套合并包 | 有，但只有一个 | `ww-echo-policy-calculator/data/echo-data.json`；上游桌面端默认从仓库 `main` 更新，服务端可用 `/api/data-pack` |

此外，上游本身也没有自动更新链：

- 桌面端只有在用户点击“角色数据 → 在线更新”时才下载数据包，默认地址是仓库 `main/data/echo-data.json`；不会启动时自动检查，也不会定时更新；
- 也支持手动导入 `echo-data.json`；
- 服务端只热加载本地 `ECHO_DATA_FILE`，由维护者原子替换文件；没有从 XWUID/Wuwa/MC 拉取并重新生成的代码；
- 数据包由 `scripts/build-data-pack.py` 手工生成，仓库没有 CI 自动重建或定时发布。

因此不能按体系分别“直接更新”。

### 5.2 停更现实与可达性（2026-10-21）

- 上游维护者已表示 `ww-echo-policy-calculator/data/echo-data.json` 不再更新，不能再把它当作持续更新源。
- Wuwa Echo Tool 源仓库在本机测试可直接访问：GitHub 仓库与 raw 均返回 200；jsDelivr 连接被重置，`raw.gitmirror.com` DNS 失败，`ghproxy.net` 可通但约 13.8 秒。这只是当前网络结果，不能保证所有国内网络。
- WuwaEchoTool 仓库没有 license 字段/许可文件；即使能下载源码，也不能默认再分发其衍生权重。
- 漂泊者强化助手是微信小程序，没有公开仓库、数据 API 或可订阅的发布物；上游只有转存快照，无法自动获取更新。

因此更新能力必须改为：

1. 由我们维护一份自建数据包；
2. XWUID 使用现有权重更新链生成 qq_bot 预设；
3. Wuwa 在取得许可后由构建期提取器生成；
4. MC 冻结在当前 15 个快照，或由用户导入；
5. 不能运行时执行上游提取代码，也不能把停更包当在线源。

### 5.3 建议更新源顺序

> 当前没有项目镜像，实际入口只有“代理更新”和“手动导入”；直连按钮已移除。

1. 项目自建 CNB/Gitee 镜像（未来可选）；
2. 项目 GitHub raw（未来可选）；
3. `ghproxy.net` 等代理（本机可通但延迟高）；
4. jsDelivr 备用域名（本机当前被重置，仅作候选）；
5. 用户自定义 HTTPS 地址（VPS）；
6. 离线导入 JSON。

下载约束：

- 仅 HTTPS，禁止跨域重定向，总超时 30 秒，最大 8 MiB；
- 校验 schema、版本、ID、权重范围、SHA256；
- 原子替换；失败保留上一份；
- 记录来源、耗时、hash、失败原因；
- 不执行远程代码。

### 5.4 镜像与许可

- XWUID 快照为 GPL-3.0，与本项目兼容，保留 `LICENSE-XWUID`；
- WuwaEchoTool 快照未看到独立许可文件；
- MC Boost Assistant 来自微信小程序，没有公开仓库和明确再分发许可；
- 上游仓库 MIT 不等于第三方权重数据全部可再分发。

落地策略：

1. 先随包 XWUID + linear/fixed；
2. Wuwa/MC 先做“导入/用户主动下载”，或取得授权后再随包；
3. 五套全部随包/镜像前，必须完成许可确认。

### 5.5 三层更新模型（建议）

不要只做“直连/代理”二元切换。建议固定三层，保证任何网络状态下都有可用功能。

#### 5.5.1 内置基线

- 随包 `echo-data.json`，首次启动、断网、镜像故障时使用；
- **离线也必须能完整使用当前已支持的功能**；
- 内置基线不依赖直连更新成功，不能被更新流程清空；
- 版本、来源、hash 写入缓存；
- “恢复内置数据”始终可用。

#### 5.5.2 直连更新

- 只从项目自己控制的国内镜像/Release 拉取，不经过用户代理；
- 目标：更新全部可合法分发的五体系数据；
- 手动“检查更新”为主，可选每日低频检查；不阻塞启动；
- 优先 CNB/Gitee；其次项目 GitHub raw/ghproxy；jsDelivr 仅候选；
- 包必须带版本、schema、内容 hash，建议 Ed25519 签名；
- 更新失败、超时、包损坏时保留上一份有效数据；
- 用户自定义权重/预设不被覆盖。

#### 5.5.3 代理/导入更新

这里的“代理”建议定义为**用户配置的 HTTPS 镜像地址**，不是通用系统 HTTP 代理：

- 高级设置填写“策略数据代理地址”，只接受 HTTPS；
- 只拉取 JSON，不执行远程代码，不携带账号/库存；
- 支持代理前缀或完整 URL，记录实际来源；
- 未签名/第三方包必须显式确认，并标注“非内置来源”；
- 仅手动触发，不自动检查；
- 提供“导入数据包”和“恢复内置数据”；
- 代理/导入失败不影响直连和内置数据。

#### 5.5.4 “完整使用所有功能”的边界

如果要求“只靠直连更新就能完整使用五套评分”，直连包必须包含 Wuwa/MC 的合法数据。当前许可状态做不到这一点。

可选口径：

1. **取得授权**：五套数据全部进入直连包和镜像；
2. **不取得授权**：算法五套全做，内置/直连只含 XWUID + linear/fixed；Wuwa/MC 走代理/导入，界面明确标注“需自行提供数据”，不能宣称直连包含五套；
3. **MC 单独冻结**：内置当前 MC 快照但不更新；直连包不含新 MC 数据。

建议选 2 或 1，不要静默把未授权数据放进直连镜像。

#### 5.5.5 状态与回退

- 数据状态：`builtin / direct / proxy / imported`；
- 每个状态记录版本、来源、时间、签名/未签名、hash；
- 只保留一个当前包 + 上一份有效包；
- 任意失败都回退到上一份，不存在半更新；
- 用户可随时恢复内置基线；
- 数据更新与 XWUID 权重更新分开，互不覆盖。

### 5.6 起始数据随包（2026-10-22）

用户确认不维护镜像仓库；采用：

- 当前上游 `echo-data.json` 作为随包起始快照：`resources/policy/echo-data.json`；
- 版本 `2026.10.19.1`，SHA-256 `8c809bdab2077d394724df44f508eccfccf4c44ef7738c50945ab7af88130bcd`；
- 不提供项目直连镜像，不依赖上游自动更新；
- 玩家需要更新时，用 HTTPS 代理/镜像地址或手动导入；
- 代理地址默认为空；玩家填入可信的已生成 `echo-data.json` 地址或 ghproxy 代理地址；
- 导入/代理包覆盖随包快照，坏包保留上一份；
- 来源和许可风险写入 `THIRD_PARTY_NOTICES.md`。

因此“完整使用所有功能”不再依赖直连：内置快照即可启动五套评分；在线更新是可选增强。

### 5.7 更新范围与真实来源（2026-10-22）

不存在“按评分体系分别订阅上游数据”的更新链。实际可更新范围如下：

| 范围 | 方式 | 来源/说明 |
|---|---|---|
| XWUID 权重/角色/套装 | **直连** | 现有 `ww3/ww1/ww2.loping151` 镜像；由 `WeightContent` 的检查更新执行 |
| Wuwa Echo Tool | **代理下载 base.js + 静态提取** | `GQin404/WuwaEchoTool` 权重内嵌 `js/base.js`；无独立数据仓库、release 或 submodule；插件只解析字面量，不执行远程 JS |
| 漂泊者强化助手 | **冻结/手动导入** | 微信小程序，无公开仓库/API |
| linear / fixed | **无在线更新** | 应用内默认/手动权重 |
| 五体系合并包 | **代理/导入** | 上游合并包已停更；只接收可信的已生成 `echo-data.json` |

因此界面不做“按体系选择后自动拉取”的假选项；只标记每个范围是直连、代理/导入还是冻结。代理地址默认留空，避免指向已停更的合并包。

## 6. 配置键

新增/扩展：

| 键 | 说明 |
|---|---|
| `策略评分体系` | workshop_xwuid / linear / fixed / wuwa_echo_tool / mc_boost_assistant / qq_bot |
| `策略角色预设` | 数据包角色 ID |
| `策略变体` | 数据包 variant ID |
| `策略手动权重` | linear/fixed 的 13 项 JSON |
| `策略主词条分` | linear/qq_bot 使用 |
| `策略归一满分` | linear/wuwa 使用 |
| `强化策略资源权重` | 四项相对价值 JSON |
| `策略求解上限` | max_states / max_transitions JSON |
| `策略数据自动检查` | bool，默认开启，仅检查项目直连镜像 |
| `策略数据代理地址` | 用户自定义 HTTPS 镜像，默认空 |
| `策略数据允许未签名` | bool，默认关闭；第三方包需显式允许 |

旧 `强化策略资源预设` 仅作回退。

## 7. 分阶段

**P5 五体系数据与 scorer**
- 数据包解析/校验、Rust 五分支、parity 测试；UI 暂不改评分选择。

**P6 工作台评分体系**
- 体系/预设/变体/手动权重/目标字段；候选、锁法、资源明细、limits。

**P7 Joint**
- 开放联合、频整器权重、候选比较；测量耗时/内存和上限提示。

**P8 实时浮层**
- 策略评分体系选择、自动匹配、口径提示。

**P9 数据更新**
- 内置基线、直连镜像、代理/导入三层；schema/hash/签名校验、缓存、恢复内置、失败回退；更新不阻塞启动，不覆盖本地自定义。

## 8. 验收

- 每套体系与上游 `echo_app::Service` 用固定样例对照 decision/resources/locks；
- 目标单位、主词条扣减、fixed 整数、qq 归一化分别验证；
- Joint 状态/转移上限、耗时、内存；
- 断网时内置基线仍可完整使用已支持功能；
- 直连/代理/导入失败均回退上一份有效数据，恢复内置可用；
- 代理/导入的未签名包必须显式确认并标注来源；
- 数据包坏版本、截断、超大、hash 不符均保留旧数据；
- 工作台离屏宽窄/明暗，真实宿主 smoke；
- 不跑全量测试，只跑相关契约与计算模块。

## 9. 待确认

1. Wuwa/MC 数据许可：先导入，还是取得授权后随包？
2. 国内镜像：是否提供 CNB/Gitee 项目镜像；没有则只能 jsDelivr/ghproxy 多源兜底。
3. 默认评分体系：保持 XWUID；其余四套可选。`qq_bot` 是数据包内部的 XWUID 标识，不作为独立 UI 项。
