# 内置资料与自动更新

## 来源分工

| 资料 | 主源 | 备用或离线 |
| --- | --- | --- |
| 角色名称、属性、头像地址 | Nanoka character.json | Encore character；内置目录 |
| 声骸名称、图标地址、套装归属 | Nanoka echo.json | Encore echo；内置目录 |
| 套装名称、门槛、效果 | Nanoka zh/sonata.json | Encore echo 的 FetterGroups；内置目录 |
| 套装图标与角色头像 | static.nanoka.cc/assets/ww | api-v2.encore.moe/resource/Data/Game/Aki/UI；图片缓存 |
| 评分模版、权重、评级 | XWUID map/character | ww3 → ww1 → ww2；用户自定义优先 |
| 角色默认套装建议 | XWUID map/1.json | 国内镜像与内置 role_sonatas.json |
| 主副词条数值与算法 | 已验证的本地计算规则 | 不通过效果文本或远程代码热更新 |

游戏源为 https://static.nanoka.cc 和 https://api-v2.encore.moe 。权重和默认套装继续使用 https://ww3.loping151.cn/XutheringWavesUID/resource/ 、ww1.loping151.top、ww2.loping151.top。默认套装来自配装样例，不是官方强制要求。

## 自动更新合同

- 基础目录接入已有单作业协调器；启动宽限约45秒，成功24小时检查，失败30分钟退避；自动开关、手动检查与取消共用。`catalog` 先完成，`weights`/`sonata`/`panel`/`role-defaults` 随后有界并行，`effects` 等 `sonata` 后再检查。
- Nanoka 读取 manifest 的 ww.live，不采用测试服 latest。完整主源失败才读取完整备用目录，不拼接两个来源的半份资料。
- 两站声骸分别保留 nanoka:/encore: 命名空间，不替换玩家 UUID。Encore 列表包含额外历史条目，条目数不等同于当前可装备种类。
- 验证目录数量、重复 ID、角色属性、套装门槛、引用和图片来源；拒绝已有角色目录缩减及套装 ID 名称错位。Encore 详情 FetterGroupDetails 有关联错位样本，不使用该字段。
- JSON 单文件4 MiB、缓存16 MiB；每来源最多20秒、总40秒、单请求5秒。只向相同 URL 发送已验证缓存的条件头；无同源缓存的304失败。
- 原始资料保存URL、SHA-256和条件头，规范化资料有内容版本；临时文件同步后原子切换。坏资料、取消、写入失败保留旧快照；离线读取内置或有效缓存。
- 游戏源使用每连接 IPv4 HTTPS 直连：本机默认 IPv6 TLS 超时，IPv4 正常。保持证书验证、不改全局 socket、不使用系统代理、不跟随跳转。
- 图片独立主备与缓存。单张失败保留原图；新套装缺图用透明占位，不冒充其他套装。图标失败不阻塞基础目录，暂缓后30分钟重试。校验 PNG/WebP 格式、尺寸、大小再规范化。
- 头像按需获取，绘制不发网络 IO；声骸目录保存图片地址但不全量下载声骸图片，卡片继续以词条展示为主。
- 套装图片最多四路并发，共用60秒阶段预算；单图片预算与主备验证不变。权重仍完整读取与二次复查，不为提速跳过一致性检查。
- 更新状态提供当前通道、已完成步骤与总步骤；数据页独立维持可见时轮询，即使权重编辑页隐藏也持续显示进度。技术诊断只保留显式展开入口。
- 不覆盖玩家库存、装备者、保护、配置及历史，不自动计算。角色属性从游戏目录投影至评分快照；权重数值仍由 XWUID/用户提供。

## 角色面板资料

- `builtin/panel_catalog.json` 保存 64 个角色、124 把武器的 90 级基础数据、默认推荐武器和 Forte 属性；内置文件是离线快照。
- 在线检查先比对 Nanoka release；版本未变不抓取详情，变化后才从 Nanoka 主源、XWUID 双镜像、Encore 备用抓取并原子更新本地快照。
- 静态面板只合并角色基础、技能树被动、武器基础/副词条、无条件武器被动、简单 2 件套和声骸词条；条件型效果不进入总值。
- 面板卡按固定顺序显示角色主要属性与伤害词条，点击卡片查看完整明细；已保存方案和当前结果共用同一生成逻辑。

## 词条数值核查

2026-10-16检查 Nanoka 声骸列表、中文详情、页面引用数据及 Encore 声骸列表和详情。已观察到强度分类、品质、技能和套装；Encore 的 MainProp 仅是 RandGroupId/RandNum 引用，未提供完整主词条成长及副词条档位。分类不是明确 COST，技能倍率不是词条值，不据此推算。

声骸 COST 提示及其静态资料已移除；编辑器继续由用户手动选择 COST 1/3/4，不从目录推断。

也检查了 XWUID 的 score.py、calculate.py、calc/__init__.py、damage/constants.py、damage/utils.py、ascension/constant.py 与 utils/constants.py。该提交中的评分/计算入口部分转发至下载的 waves_build 模块，未找到可直接替换完整主副词条表的声明式来源；不下载执行二进制提取规则。

因此保留 core/scoring.py 的数值表及当前独立校验上限，计算测试验证口径不变。后续找到完整来源后须比对全部档位再单独升级；本次没有把这部分规则变为在线更新。

## 内置快照与验证

builtin/game_catalog.json 是统一目录；game_sources.json 记录原始URL、版本、hash与规范化图片hash；sources.json 为逐文件清单。

```powershell
.\.venv\Scripts\python.exe -B tools/resources.py validate
.\.venv\Scripts\python.exe -B tools/run_checks.py tests.contracts.test_game_resources tests.contracts.test_role_defaults tests.calculation.test_scoring tests.calculation.test_constraints
```

适配器为独立实现，资料出处见 THIRD_PARTY_NOTICES.md。公开可访问不等于素材授权；新机制与未发布版本仍需维护者核实。
