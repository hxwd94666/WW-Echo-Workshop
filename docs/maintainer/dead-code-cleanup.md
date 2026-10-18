# 死代码与遗留代码处置

> 定位：维护者文档，记录"已确认无运行时入口或仅测试使用的代码"及其处置方式。
> 不重复架构说明（见 [architecture.md](architecture.md)），不替代发布验收（见 [release.md](release.md)）。
> 状态图例：`☐ 待办`、`☑ 已删`、`⊘ 保留`、`⚠ 待决策`。

## 1. 判定标准

一条代码被判为"死代码"需同时满足：

1. **静态无引用**：在 `src/`、`plugin/`、`tests/`、`tools/` 内按符号名检索，除定义处外无调用；
2. **运行时无入口**：从 `plugin/okww/*` 出发的导入图之外，调用链在 UI 层断链（导入图可达 ≠ 用户能触达）；
3. **非框架回调**：排除 Qt 虚函数/槽与标准库回调（见附录 B）；
4. **非动态分发**：抽查无 `getattr`、字符串信号名或宿主按名称反射调用。

"遗留代码"另指仍在运行、但只为兼容旧数据或宿主行为而存在的分支。此类不删除，登记其"退出条件"。

## 2. 现状总览

- 模块导入图：215/215 全部被引用；唯一"仅测试引用"的模块是 `core/allocation/set_counts.py`。
- 但存在**成块运行时断链**：旧版工作区恢复/完整备份整条 UI 链无用户入口（见 §4）。
- 另有约 34 个单点符号无引用、无测试（§5），4 个生产 API 仅测试使用（§6），8 项遗留兼容仍在运行（§7）。

结论一句话：**导入图全绿不等于用户能触达**；清理必须以运行时入口为准。

## 3. 处置分级

| 级别 | 含义 | 处置 |
| --- | --- | --- |
| P0 | 整块功能断链，需产品决策 | 二选一：下线整块 / 接回入口 |
| P1 | 单点死代码，无引用无测试 | 直接删除（分批） |
| P2 | 生产 API 仅测试使用 | 迁入 `tests/` 或标注"测试辅助" |
| P3 | 仍在运行的旧数据/宿主兼容 | 保留并登记退出条件 |

## 4. P0 专项：旧版工作区恢复 / 完整备份 ✅ 已下线（路径 A）

### 4.1 断链证据

| 位置 | 现状 |
| --- | --- |
| `ui/shell.py:43 open_workspaces` | 唯一调用者是 `ui/inventory/page.py:266` 的同名方法 |
| `ui/inventory/page.py:266 open_workspaces` | 全仓无调用方（仅定义） |
| `ui/inventory/controller.py:601 open_workspaces` | 无调用方；`WorkspaceWindow` 只在此处实例化 |
| `ui/settings/controller.py:141 _toggle_recovery` | 无调用方；它才调用 `_populate_recovery` |
| `ui/settings/controller.py:146 _populate_recovery` | 仅被 `_toggle_recovery` 与 `open_settings_section('recovery')` 调用 |
| `ui/settings/controller.py:179 open_settings_section('recovery')` | 唯一来路是上面已死的 `shell.open_workspaces` |
| `adapters/okww/assistant_ui.py:143/170/202` | 宿主卡片只路由 `repair` / `shortcuts` / `weights`，无 `recovery` |
| `ui/settings/recovery.py`（407 行） | 仅被死链 `page.WorkspaceSettings`、`controller.open_workspaces` 引用 |
| `application/backup_jobs.py:80 BackupService.start` | 仅由 `recovery.py` 与已死的 `recover_full_backup` 调用 |

### 4.2 仍被使用的部分（不可整类删除）

`runtime.backups` 的状态查询仍在运行：

- `adapters/okww/capture_commands.py:62/76/87/113/241`（`restoring` 门禁）
- `adapters/okww/manual_capture.py:24`
- `ui/allocation/roles.py:68`（`status.busy`）
- `adapters/okww/settings_task.py:122/136`（注入 settings reader 与 preference recovery）

### 4.3 路径 A：下线（若产品确认不再提供"恢复旧工作区"）

删除清单：

1. `ui/settings/recovery.py`（`WorkspaceContent`、`WorkspaceWindow`）；
2. `ui/settings/page.py` 的 `WorkspaceSettings`（`:76`）及其导入；
3. `ui/settings/controller.py` 的 `_toggle_recovery`、`_populate_recovery`、`open_settings_section` 中 `'recovery'` 分支；
4. `ui/shell.py:43 open_workspaces`、`ui/inventory/page.py:266`、`ui/inventory/controller.py:601`；
5. `ui/inventory/controller.py` 的 `restore_backup:550`、`export_full_backup:576`、`recover_full_backup:589`；
6. `application/backup_jobs.py` 的 bundle 分支（`:344`/`:356`/`:391`）与 `BackupService.start`；保留 `status`/`restoring`/`cancel`/`close`；
7. `storage/workspace_bundle.py:300 export_bundle`、`:447 recover_bundle` 及仅其使用的内部函数；
8. `storage/recovery_staging.py`（242 行）——若 `recover_bundle` 一并删除。

同步要求：

- `tests/contracts/test_screenshot_cleanup.py:147` 目前用 `workspace_bundle.export_bundle` + `_stage_bundle` 验证"完整备份有效"；下线后该断言须改为当前库存备份（`EchoRepository.export_backup`）或直接删除并说明理由；
- 删除后 `runtime.backups` 的 `restoring/busy` 语义仍需覆盖：保留 `BackupService.status` 与适配器断言。

### 4.4 路径 B：接回入口（若仍要提供恢复能力）

- 在数据设置区恢复一个可见卡片，调用 `open_settings_section('recovery')`；
- 或在宿主助手卡片中显式增加 `recovery` 路由（`assistant_ui._route_settings('recovery')`）；
- 必须补一条 UI 可达性验证：从卡片点击到 `WorkspaceWindow` 出现的路径（契约测试或手工验收记录）。

> 2026-10-18 决策：采用**路径 A（下线）**并已执行。删除清单：
>
> - `ui/settings/recovery.py`（407 行）、`ui/settings/page.py` 的 `WorkspaceSettings`、`ui/settings/controller.py` 的 `_toggle_recovery`/`_populate_recovery` 与 `'recovery'` 分支；
> - `ui/shell.py::open_workspaces`、`ui/inventory/page.py::open_workspaces`、`ui/inventory/controller.py::export_full_backup/recover_full_backup/open_workspaces`；
> - `application/backup_jobs.py` 的 `export-bundle`/`recover-bundle` 分支、导入、`restoring`/`before_restore` 条目与未再使用的 `weights` 参数；
> - `storage/workspace_bundle.py` 的 `export_bundle`/`_export_bundle`/`recover_bundle`/`_stage_bundle`；
> - `ui/shell.py` 状态标签中的两项、`tests/native_runtime.py` 的 bundle 别名、`tests/contracts/test_screenshot_cleanup.py` 的 bundle 段（改为库存/方案断言）。
>
> 保留：`BackupService` 的状态/`restoring`/取消、档案导入导出与库存归档分支、`storage/recovery_staging.py`（仍被 `inspect/schedule/staging` 种类使用）。
>
> 二次清理（2026-10-18 已完成）：上述 7 类作业（副本预览/切换预约/设置预览/设置预约/暂存列表·预览·隔离）连同 `backup_jobs` 分支、校验与 `preference_recovery`/`preference_config` 装配一并删除；`storage/recovery_staging.py` 整文件删除；`WorkspaceManager.schedule()` 删除（`inspect()` 保留给启动配置修复）；`PreferenceRecovery` 裁成只读台账（`state`/`_read`），删除预览/预约/应用/撤销方法。

## 5. P1 清单：单点死代码（无引用、无测试）

证据统一为：全仓按符号名检索仅命中定义处（`grep -rn --include=*.py "<name>" src plugin tests tools`）。

| # | 位置 | 符号 | 批次 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | `ui/allocation/plans.py:219` | `save_plan` | B1 | ☑ |
| 2 | `ui/allocation/plans.py:329` | `protect_plan` | B1 | ☑ |
| 3 | `ui/allocation/plans.py:368` | `open_current_plan` | B1 | ☑ |
| 4 | `ui/allocation/plans.py:377` | `adjust_current` | B1 | ☑ |
| 5 | `ui/allocation/plans.py:471` | `load_plan` | B1 | ☑ |
| 6 | `ui/allocation/requirements.py:175` | `set_main_names` | B1 | ☑ |
| 7 | `ui/allocation/requirements.py:309` | `preserved_requirements` | B1 | ☑ |
| 8 | `ui/allocation/history.py:638` | `_operate` | B1 | ☑ |
| 9 | `ui/allocation/constraints.py:69` | `class SetConstraints` | B1 | ☑ |
| 10 | `ui/inventory/controller.py:526` | `lock_selected` | B1 | ☑ |
| 11 | `ui/inventory/page.py:639` | `_open_detail` | B1 | ☑ |
| 12 | `ui/inventory/intake_feedback.py:16` | `pending_review_count` | B1 | ☑ |
| 13 | `ui/inventory/model.py:64` | `is_reserved` | B1 | ☑ |
| 14 | `ui/inventory/render_cache.py:17` | `card_cache_info` | B1 | ☑ |
| 15 | `ui/inventory/render_cache.py:21` | `clear_card_cache` | B1 | ☑ |
| 16 | `ui/inventory/filter_proxy.py:113` | `set_inventory_filters` | B1 | ☑ |
| 17 | `ui/settings/controller.py:276` | `apply_scan_level` | B1 | ☑ |
| 18 | `ui/shell.py:205` | `show_task_help` + `ui/task_help.py` 整模块 | B1 | ☑ |
| 19 | `ui/shell.py:508` | `_guarded_data_backup` | B1 | ☑ |
| 20 | `ui/widgets/inputs.py:161` | `_showComboMenu` | B1 | ☑ |
| 21 | `ui/widgets/rating.py:209` | `clear_avatar_pixmaps` | B1 | ☑ |
| 22 | `ui/widgets/rating.py:216` | `avatar_pixmap_cache_info` | B1 | ☑ |
| 23 | `ui/widgets/workspace.py:82` | `class EditSidebar` | B1 | ☑ |
| 24 | `core/allocation/models.py:263` | `reservation_policy` | B2 | ☑ |
| 25 | `core/rating.py:18` | `RATING_POLICY` | B2 | ☑ |
| 26 | `resources/custom_weights.py:17` | `SKILL_NAMES` | B2 | ☑ |
| 27 | `resources/weights.py:761` | `select_modal` | B2 | ☑ |
| 28 | `capture/scan/traversal.py:557` | `prepare_anchor_scroll` | B2 | ☑ |
| 29 | `capture/scan/traversal.py:574` | `position_anchor` | B2 | ☑ |
| 30 | `capture/scan/traversal.py:621` | `track_scroll` | B2 | ☑ |

说明：

- `ui/allocation/plans.py` 的计划操作现统一走 `runtime.plan_commands`（同文件 `:38`），上表 1–5 是迁移后的残留方法；
- `show_task_help` 是 `ui/task_help.py` 的唯一引用，删除后整模块不可达；
- `filter_proxy.setFilterFixedString`（`:110`）是 Qt 槽覆盖，**不在删除清单**。

## 6. P2 清单：生产 API 仅测试使用

| # | 位置 | 符号 | 使用方 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 1 | `core/allocation/set_counts.py` | 整模块（18 行） | `tests/calculation/test_requirement_policies.py` | 迁入 `tests/` 或注明"测试辅助" | ☑ 已删（测试内实现） |
| 2 | `core/costs.py:34` | `with_cost_layout` | `tests/calculation/test_constraints.py` | 同上 | ☑ 已删（测试内实现） |
| 3 | `core/requirements.py:156` | `with_role_requirements` | `tests/calculation/test_constraints.py` | 同上 | ☑ 已删（测试内实现） |
| 4 | `resources/weights.py:558` | `set_custom_template` | `test_native_custom_weights.py`、`test_rating.py` | 生产已用 `set_custom_values`，评估合并 | ☑ 已删（测试内实现） |

> 取舍标准：若这些函数承载"不得回退的计算契约"，保留在 `src/` 并在注释中写明"仅测试调用"；否则迁入测试，避免生产代码为测试而存在。

## 7. P3 清单：仍在运行的遗留兼容（保留并登记退出条件）

| # | 位置 | 机制 | 退出条件 | 状态 |
| --- | --- | --- | --- | --- |
| 1 | `adapters/okww/assistant_ui.py:258-314` | `_echo_legacy_buttons` 隐藏/恢复宿主旧按钮 | 宿主助手卡片改版、旧按钮不再存在 | ⊘ |
| 2 | `adapters/okww/scan_observation.py:289-311` | `packed_legacy_fallbacks` 旧打包布局回退 | 新布局覆盖全部机型且回退计数长期为 0 | ⊘ |
| 3 | `core/filters.py:75` | 丢弃退休视图筛选名 | 旧调用方全部移除 | ⊘ |
| 4 | `storage/preferences.py:183` | 接受五字段旧档案形状 | 恢复包不再含旧形状 | ⊘ |
| 5 | `resources/weight_control.py:84-100` | 旧 v2 权重指针迁移 | 线上安装全部完成一次迁移 | ⊘ |
| 6 | `ui/allocation/costs.py:98`、`editor.py:254` | `allow_legacy` 读旧 COST 草稿 | 草稿格式统一为当前结构 | ⊘ |
| 7 | `ui/allocation/constraints.py:2` | 旧文本约束适配 | 约束输入全部结构化 | ⊘ |
| 8 | `resources/weights.py:382`（`assistant_ui.py:411`、`settings_task.py:147`） | 标签→稳定 ID 解析（已改名 `resolve_selection`） | 这是**现行**首次启动/下拉选择路径，不是旧版本兼容；恢复包的标签反查回退已随 `normalize_selection` 删除 | ⊘ |

## 8. 执行批次与验收

| 批次 | 内容 | 前置 | 定向验证 |
| --- | --- | --- | --- |
| B0 | 快照：把待删文件复制到 `build/cleanup-backup/<批次>/` | 无 | 目录存在、文件数一致 |
| B1 | P1 的 UI 层（第 1–23 项） | B0 | `tests.contracts.test_main_page_actions`、`test_inventory_selection`、`test_saved_modes`（分模块运行）；`ruff check` / `format --check` |
| B2 | P1 的核心/资源/抓取（第 24–30 项） | B0 | `tests.calculation.test_matching`、`test_requirement_policies`、`test_scoring`；`ruff` |
| B3 | P2 决策与迁移 | B1/B2 完成 | 迁移后 `tests.calculation.test_constraints`、`test_requirement_policies`、`test_native_custom_weights` |
| B4 | P0 决策后执行 | 产品确认 A 或 B | 路径 A：库存/设置契约测试 + 包内无缺失文件；路径 B：新增可达性验证 |

规则：

- 每批只跑**直接相关**的模块；不因到批次出口自动跑全量；
- 记录命令、输入、字面输出与退出码；静态检查不冒称运行验收；
- 不以删除失败测试、放宽断言或改预期来让清理通过；删除测试必须写明理由；
- 不改计算规则、不改玩家数据、不动 `plugin/okww` 的宿主入口契约。

验收记录表（每批一行）：

| 批次 | 删除项数 | 定向测试 | ruff | 构建 | 备注 |
| --- | --- | --- | --- | --- | --- |
| B1 | 23 + `task_help.py` 模块 | `test_main_page_actions` 2、`test_inventory_selection` 6、`test_saved_modes` 12、`test_capture_shortcuts` 6、`test_evidence_viewer` 4 全通过 | 通过 | 未重建 | 2026-10-18 执行 |
| B2 | 7 | `test_rating`/`test_scoring`/`test_matching`/`test_native_matching`/`test_native_allocation` 90 项、`test_compute_chunks` 11 项通过 | 通过 | 未安装 | 同时修复既有回归：匹配单元恢复 `row_scores` |
| B3 | 4（P2 从生产删除，测试内实现） | `test_constraints` 15、`test_native_custom_weights`/`test_rating`/`test_native_storage`/`test_native_replacement` 34 项通过 | 通过 | 未重建 | 2026-10-18 执行；辅助迁移到 `tests/calculation/helpers.py` |
| B4 | 9（P0 路径 A） | `test_screenshot_cleanup` 7、`test_saved_modes` 12、`test_profile_repair_services` 2、`test_inventory_selection` 6、`test_data_compatibility` 4 通过；全模块导入 212/212 | 通过 | 未安装 | 2026-10-18 执行 |
| B5 | 7 类作业 + 2 个模块/类 | `profile_repair_services` 2、`saved_modes` 12、`data_compatibility` 4、`loadout_actions` 1、`screenshot_cleanup` 7、`inventory_selection` 6、`main_page_actions` 2 通过；导入 211/211 | 通过 | dev29 已安装 | 2026-10-18 执行；`protected_item_ids` 回退经夹具验证属当前语义，撤回 |

注意：删除源码会改变发行包 sha，**不要**把"sha 不变"当作验收标准；构建验收看"构建成功、成员数符合预期、安装探针通过"。

## 9. 回滚

仓库当前没有 git 提交（文件均为未跟踪状态），回滚依赖批次快照：

1. B0 把待删文件按原相对路径复制到 `build/cleanup-backup/<批次>/`（该目录不随包分发，已在 `.gitignore` 的 `build/` 规则内）；
2. 回滚时按原路径拷回并重新运行该批定向测试；
3. 若后续引入 git，则以批次提交替代快照，并在本表登记提交号。

## 10. 未决问题

1. P0 走 A（下线）还是 B（接回入口）——需要产品确认；
2. 宿主是否会按名称反射调用 `BaseTask.open_*`（仓库内未发现声明，需实机确认）；
3. P2 四项是"迁入测试"还是"保留并标注"——取决于是否被视为计算契约；
4. ~~`ui/settings/page.py:77` 注释~~：已随 `WorkspaceSettings` 一并删除。
5. 恢复 UI 下线后，`inspect-workspace`/`schedule-workspace`/`preview-settings`/`schedule-settings` 与暂存种类已无入口，列入下一次清理。

## 附录 A：判定方法与可复现命令

```bash
# 1) 符号级引用计数（示例：计划方法）
grep -rn --include=*.py "save_plan" src plugin tests tools

# 2) 导入图与可达性（本次审查用脚本，按需复制执行，不落盘）
python -B - <<'PY'
import ast, pathlib, collections
root = pathlib.Path('src/echo_workshop')
mods = {}
for p in root.rglob('*.py'):
    parts = list(p.relative_to(root).with_suffix('').parts)
    is_init = parts[-1] == '__init__'
    if is_init: parts = parts[:-1]
    mods['echo_workshop' + ''.join('.' + x for x in parts)] = (p, is_init)
def pkg(name, is_init): return name if is_init else name.rsplit('.', 1)[0]
def imports(path, is_init, name):
    out, base_pkg = set(), pkg(name, is_init)
    for n in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(n, ast.ImportFrom):
            if n.level:
                base = base_pkg.split('.')
                if n.level > 1: base = base[: len(base) - (n.level - 1)]
                target = '.'.join(base) + ('.' + n.module if n.module else '')
            elif n.module and n.module.startswith('echo_workshop'):
                target = n.module
            else:
                continue
            out.add(target)
            out.update(target + '.' + a.name for a in n.names)
        elif isinstance(n, ast.Import):
            out.update(a.name for a in n.names if a.name.startswith('echo_workshop'))
    return out
edges = {m: imports(p, i, m) for m, (p, i) in mods.items()}
roots = set()
for p in pathlib.Path('plugin').rglob('*.py'):
    roots |= imports(p, False, 'echo_workshop')
seen, stack = set(), list(roots)
while stack:
    cur = stack.pop()
    if cur in mods and cur not in seen:
        seen.add(cur); stack.extend(edges.get(cur, ()))
        parent = cur.rsplit('.', 1)[0]
        if parent in mods: stack.append(parent)
print('modules', len(mods), 'reachable', len(seen))
print(sorted(set(mods) - seen))
PY
```

审查结论以"定义处唯一命中 + 运行时链核对"为准，不以单一脚本输出直接删除。

## 附录 B：框架回调白名单（不得误删）

| 位置 | 回调来源 |
| --- | --- |
| `ui/inventory/cards.py:349 helpEvent` | Qt 委托虚函数 |
| `ui/inventory/filters.py:57 editorEvent` | Qt 委托虚函数 |
| `ui/widgets/inputs.py:49 pathFromIndex` | `QCompleter` 虚函数 |
| `resources/http.py:172 handle_starttag` | `HTMLParser` 回调 |
| `resources/download.py:10 redirect_request` | `urllib` 重定向回调 |
| `ui/inventory/filter_proxy.py:110 setFilterFixedString` | `QSortFilterProxyModel` 槽 |
