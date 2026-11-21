# 声骸工坊重构与 OKWW 3.x 适配方案

2026-11-19 · 实施提案

**建议先恢复新版 OKWW 的加载、页面和退出链路，再按业务链路完成重构。** 首轮以本地 OKWW `03632d02`（其变更日志标记 `v3.8.5-beta.2`）及 `ok-script[ocr,qt]==3.0.6` 为参考。业务代码仍从 `src/ww_echo_workshop/` 一处构建。页面、运行时与采集重新划分职责，评分、评级、配装目标与数据兼容按现行合同验收。

## 1. 失效排查与宿主变化

宿主源码读自 `F:\WW\ok-wuthering-waves`。工坊对照 `1c8e695` 及当前工作区，已有未提交的 OCR、搜索预算和界面改动，实施时先核对并纳入基线。宿主锁文件声明新版组合，但宿主现有 `.venv` 的分发元数据显示 `ok-script 2.0.17`。直接用该环境启动不能作为 3.0.6 适配证据。初步结论来自源码、锁文件、官方加载文档和发布元数据。2.x 的定向运行结果见第 6 节，实际 3.0.6 的首发错误仍待复现。

| 接入点 | 已确认的变化或现状 | 适配动作 |
| --- | --- | --- |
| Qt 依赖 | OKWW 从 `qfluentwidgets` 迁到 `ok.ui.qt`。宿主锁定 `ok-pyside-minimal==6.12.0.post2`，工坊声明完整 PySide6 6.11.1 和 Fluent Widgets，设置入口还会间接导入 Fluent Widgets。 | **优先验证导入链。** 在只装新版宿主依赖的隔离进程中逐个导入任务，记录第一个导入错误及模块来源。工坊界面采用精简包提供的 Qt Widgets，宿主专用导航、主题接入留在适配层。两套 Qt 分发共用 `PySide6` 命名空间，分开安装和验证。 |
| 插件发现 | OKWW 的 `config.py` 使用 Qt，设置了 `custom_tasks=True`、`plugin=False`。官方文档说明 `plugin` 控制管理页，`ok_import/` 中已有插件仍会加载。框架支持 manifest 显式声明任务，以及现有的根目录发现方式。 | 按宿主发布版的实际工作目录放置 ZIP 解包后的单一插件目录。验证三任务均注册一次。推荐切换到 manifest `tasks` 显式声明，保留现有模块名、类名、安装 ID；用目标版本的真实加载器验证。 |
| 页面挂载 | 工坊已有 `create_import_page(parent)`。官方文档将它列为 Workshop 类插件的接入方式，同时提供独立的 `custom_tabs`。当前工坊还存在任务卡和旧的导航桥接代码。 | 首版使用任务拥有的 `create_import_page(parent)`，通过工坊同一个页面工厂装配界面，Qt 导入延迟到工厂。保留已有的任务卡打开入口，验证它能打开同一业务界面。确认生产调用、回退与关闭语义后收拢旧桥接。 |
| 任务卸载 | 现有 `lifecycle.py` 针对旧宿主全局替换 `TaskManager.unload_task`。官方文档声明导入任务卸载会收到 `on_destroy()`。现有 `test_host_loading.py` 直接调用旧加载辅助方法并断言补丁行为。 | 对 3.0.6 核对 `load_config`、`on_create`、页面工厂、`on_destroy` 与应用退出的调用顺序和线程；较早的 2.x 保留缺失卸载通知的适配；具备插件注册器的宿主走自身卸载入口。测试断言改为任务和页面收口后资源释放与幂等。 |
| OCR | OKWW 锁定 `onnxocr-ppocrv6==0.0.1`，配置了 OpenVINO 和可用时的 NPU。工坊已有私有 CPU OCR，通过 `ONNXPaddleOcr` 和宿主 `Box` 辅助方法解析结果。上游声明 v5、v6 API 和返回格式一致，识别模型与字典已更换。 | 准备同一批脱敏帧，核对文本、置信度、框坐标、裁剪回退与最终记录。保留插件专用 CPU 引擎归属，将宿主 `Box` 转换封在适配边界。以画面解析结果评估模型变更，v5 与 v6 分发各用独立环境。 |
| 截图与输入 | 本地 `config.py` 的 Windows 原生路径列出 `WGC`、`WGC_HDR`、`BitBlt_RenderFull`，输入列出 `PostMessage`、`Pynput`。现有 `capture_device.py` 用宿主内部类和窗口身份判断后台能力。 | 逐项核对具体实现、`get_frame()` 返回、窗口族和捕获源身份，确认坐标、黑边、HDR 及前台切换。后台自动扫描首批仅对验证过的窗口捕获与 `PostMessage` 组合开放，其他组合按各自能力显示手动收录或实时评分可用性。 |

第一轮复现按“发现包 → 导入任务 → 实例化 → 构建页面 → 收到游戏帧”排查，记录每步版本、错误类型与首个异常栈。任务消失时先看安装路径和入口导入，任务可见但页面打不开时看 Qt 初始化，页面正常后再检查 OCR 与捕获。现有依赖差异给出了优先方向，具体首发故障以复现日志确认。

[OKWW 基准提交](https://github.com/ok-oldking/ok-wuthering-waves/commit/03632d02d5cf77fac1712cb391e4e530b4e7848a) · [OKWW Qt 迁移提交](https://github.com/ok-oldking/ok-wuthering-waves/commit/9dff2c92b99b6cc5c6e1e2a76d40cfea41586335) · [官方导入插件合同](https://ok-script.com/en/docs/plugins/) · [ok-script 3.0.6](https://pypi.org/project/ok-script/3.0.6/) · [精简 Qt 分发](https://pypi.org/project/ok-pyside-minimal/6.12.0.post2/) · [PP-OCRv6 0.0.1](https://pypi.org/project/onnxocr-ppocrv6/0.0.1/)

## 2. 目标结构

`settings_task.py` 同时承担宿主任务、数据初始化、窗体生命周期与资料更新。`ui/shell.py` 用 `_PageTask.__getattr__` 把业务页连接到宿主任务。`adapters/okww/` 也混合了实时和扫描业务与宿主截图、输入。保留现有一级目录，按调用方与状态所有者重组内部文件。

| 目录 | 重构后的职责 | 迁移落点 |
| --- | --- | --- |
| `plugin/okww/` | manifest 模板与三个薄任务入口。 | 保留 `WWEchoWorkshopSettingsTask`、`WWEchoWorkshopOverlayTask`、`WWEchoWorkshopScanTask`；构建时按唯一版本生成包。 |
| `adapters/okww/` | 把 OKWW 的任务、配置、捕获、输入、生命周期和主题转换为工坊可消费的输入与事件。 | 收口 `og`、`Box`、设备实现、宿主 UI 接入与关闭逻辑；任务负责转发，使用同一个运行时。 |
| `ui/` | 一套 Qt 工作台，组织声骸库、匹配、配装与设置；页面渲染状态、分派用户动作。 | 从外壳和设置入口开始迁离 Fluent Widgets，再逐页迁移。页面接收明确的应用服务、模式状态与导航回调，逐步替换 `_PageTask`、任务字段访问和重复窗体组装。 |
| `application/` | 按用户用例组织收录、库存查询、匹配、配装、设置及备份；管理作业、取消和结果发布。 | 将 `PluginRuntime` 收口为组合与生命周期所有者。按实际用例整理散落的作业和状态，保留已验证的队列及服务，页面仅接触所需用例。 |
| `capture/` | 帧裁剪、布局与文本解析、冻结观测、扫描状态机。 | 从 `overlay_task.py`、`scanning.py`、`scan_observation.py` 抽出与宿主无关的稳定性、重试和停止条件，复用现有收录/解析逻辑。输入和截图仍由适配层执行。 |
| `core/` 与 `native/compute/` | `core/` 提供标准库领域模型、约束和共享合同，Rust 承担当前生产评分、匹配与配装计算。 | 先复用现有 Rust 程序与 v2 协议。搬迁用例时用冻结结果证明输出等价，算法变化另行版本化。 |
| `storage/` 与 `resources/` | 库存、方案、档案、证据与恢复由存储层负责；权重和游戏资料的离线快照、校验、原子更新由资源层负责。 | 把 `config_file` 到数据根的解析放在组合边界，应用层接收明确的数据位置。沿用现有数据布局和持久化格式。 |

## 3. 运行路径与业务合同

- **启动与退出。** 宿主创建任务后，由设置任务协调唯一工坊运行时。工作台懒加载模式页；OCR 和计算进程按需使用。启动异常以阶段和具体原因反馈。关闭先停止接收新作业并广播取消，再断开订阅、停止扫描与其他后台作业，确认退出后关闭资料、计算进程和存储。重复关闭和部分初始化失败都走同一清理路径。窗口的 Qt 操作由 GUI 线程执行，后台等待有界，迟到结果按 generation 丢弃。
- **采集。** 实时评分读取当前帧并投影，库存写入由手动收录或自动扫描的显式会话提交。覆盖单件展示、背包、左右对比、强化/调谐四种实时场景。扫描每次确认页面回执、帧稳定性与捕获设备身份；失焦、窗口/档案切换、异常或取消时停止输入，未提交项保留原状态，已成功提交项保留成功结果。
- **模式。** 声骸库、匹配、配装各自持有候选范围、角色选择和草稿。选择角色、编辑要求、更新资料可刷新界面状态，评分或生成由显式动作提交。作业冻结角色/模版 ID、UUID、库存与资料修订、要求和候选范围，发布前核对当前上下文。
- **工作台与设置。** 把实时 HUD、强化策略开关和目标、资料预设、快捷键及档案管理接入工作台和设置的用例。强化候选与重抽继续按既有确认条件执行，策略输入和快照保留现有口径。资料更新继续校验 schema、大小、来源与完整性，校验通过后才激活新快照。
- **计算与保存。** 保留主词条口径、精度、阈值、单件和五件评级、排序及配装目标。配装遵守五件、COST≤12、主词条、套装、固定件、独占及优先级；配装结果携带预算与完成状态。保存按显式角色集合，通过现有事务提交当前配装、预留、库存装备者及修订号。备份追加独立历史，查看历史使用冻结快照。已保存方案的单槽替换固定其他四件，确认保存时才提交到工坊数据。
- **退化与日志。** 游戏窗口、捕获或 OCR 不可用时给出对应的功能状态，库存与离线资料仍可访问；原生计算缺失时标明受影响的计算操作。作业记录 `operation_id`、`kind`、`stage`、`state`、耗时、计数、版本和 `error_type`。取消记为 `cancelled`，真实失败保留栈。诊断使用有界、脱敏输出。

既有合同见[架构与模块边界](architecture.md)、[实时评分与收录链路](capture.md)、[数据身份、兼容与恢复](data-and-migration.md)和[Rust 计算方案](rust-compute-plan.md)。

## 4. 实施顺序与完成条件

| 顺序 | 实施范围 | 阶段完成条件 |
| --- | --- | --- |
| 0 · 固定基线并复现 | 以当前工坊工作区和宿主提交为输入，使用干净的 Python 3.12 环境按 OKWW 的锁文件准备 `ok-script 3.0.6` 等依赖。记录 PySide6 模块来源、OCR 分发、插件目录和首次错误。复用现有黄金样本、脱敏画面及数据兼容夹具。 | 拿到可重复的首次失效阶段，或者成功的版本矩阵与原失效环境差异。确定三任务、产品 ID、数据位置、既有功能及回归样本。 |
| 1 · 恢复宿主入口 | 适配 manifest、Qt 页面工厂、任务生命周期与工作台最小可用入口。验证实际运行的导入链，将必要的 UI 依赖迁到宿主精简 Qt 可用范围。 | 干净的新版桌面宿主中三任务注册一次，声骸工坊页面可打开，从任务卡可进入已有界面；重复打开或卸载、重启后无重复监听或残留作业。通过该阶段即可产生可给开发者验证的恢复版本。 |
| 2 · 重构实时评分和收录 | 收口截图、窗口/输入能力与私有 OCR，抽出解析及扫描状态。迁移实时评分、手动收录、自动扫描的业务链。 | 四种实时场景通过脱敏帧回放并完成实机验证；背包扫描和手动收录覆盖取消、切换和回退。未验证的输入或捕获组合受到能力限制。 |
| 3 · 重构工作台与业务用例 | 按声骸库 → 匹配 → 配装迁移页面与用例，再收口设置与资料入口。每次替换一条完整用户路径，沿用 Rust 与数据合同。 | 无宿主对象透传到模式页；三模式独立，基线样本的评分/评级/排序和配装输出一致，保存/历史/单槽替换通过定向检查。正式路径集中于迁移后的实现。 |
| 4 · 构建与交付验收 | 从唯一源码生成发布候选 ZIP，更新实际安装路径、支持范围和回退说明。在隔离宿主中验证安装及更换。 | 包的文件、资源和原生程序校验通过；真实宿主完成无游戏/接游戏/退出验证。按发布清单核对宽窄窗、明暗主题、DPI；发布说明列明实测组合和剩余缺口。 |

阶段 1 与阶段 2 负责恢复接入，阶段 3 负责内部重构。每条链路切换前核对生产与外部调用、存量数据关联和回退路径，切换后收口旧入口。新版插件同时适配 ok-script 2.x 和 3.x，旧宿主从根目录发现相同任务，有原生页面能力的宿主读取 manifest 并挂载同一工作台。旧宿主缺少挂载能力时从任务卡打开。Web、云游戏及 Pynput 自动扫描列为待验证组合，支持声明以实测的 Windows 桌面 Qt、原生游戏窗口及设备为准。

## 5. 数据与安装回退

- 保留安装 ID `ww-echo-workshop`、Python 包名、任务类名及配置身份。以宿主实际 `config_file` 校验 `config_file.parent/plugin_data/ww-echo-workshop/`，确保与旧版读取的是同一受控数据根。路径变化时先报告旧根、新根和差异；复制或导入前在合成副本演练，避免默认空库掩盖旧库存。
- 首版以当前库存 schema4、当前计划及档案格式为目标。验收声骸与档案 UUID、匹配/配装配置、保护与预留、截图证据和冻结历史。现行拒绝的旧格式继续显式拒绝并保留原件；往返保留兼容字段。
- 发布 ZIP 由构建器校验源文件、生成目录及归档对应关系，包含按当前规则打包的 Rust 程序、内置资料及必要许可。升级前关闭 OKWW，备份旧安装和受控数据。将旧安装移出扫描目录后再就位新目录；重新启动核对任务唯一、数据根及库存/档案身份。
- 回退先关闭宿主，换回外置旧包，并检查其宿主版本与数据格式兼容性。数据已变更且兼容性未证实时，保留现数据，使用备份副本验证回退或使用已经验证的档案导入。故障发生在 3.x 宿主接入时，旧包与原可用宿主作为一对回退。

## 6. 验证记录

| 改动范围 | 首选验证 |
| --- | --- |
| 宿主入口、页面与退出 | 以 `tests/contracts/test_host_loading.py` 为迁移起点，覆盖实际加载器发现、配置身份、页面挂载和幂等关闭，分别检查无游戏和真实游戏宿主。 |
| OCR、采集与 HUD | 复用 `test_private_ocr.py`、`test_live_snapshot.py`、`test_live_stability.py` 和当前工作区 OCR 定向用例。以精选脱敏帧核对字段与坐标，再验证实机切页、DPI、换窗、失焦和停止收录。 |
| 业务搬迁 | 按实际受影响的评分、匹配、配装模块运行 `tests/calculation/` 下对应测试，复用黄金样本与小型穷举基线。计算规则和搜索预算结果按现行合同核对。 |
| 数据与交付 | 用 `test_data_compatibility.py` 和合成副本覆盖往返、取消、失败及回退。发布候选按[定向验收与发布](release.md)和[真实游戏验收清单](release-g5-checklist.md)走一次对应检查。 |

### 首轮实现

同一份插件保留三个根任务入口，并在 manifest 显式声明。工作台和任务卡弹窗共用 Qt Widgets。具备插件页面注册器的宿主使用原生挂载和卸载，早期 2.x 继续用任务卡适配及卸载兜底。所有卸载路径都关闭当前工坊运行时，应用退出也会触发清理。

验证使用 Python 3.12.13、PySide6 6.11.1、离屏 Qt 和临时合成档案。宿主根目录来自测试临时目录，工坊业务代码从源码导入。测试覆盖三任务注册与重复扫描、任务卡打开、入库/匹配/配装页面和设置构建、卸载，以及角色选择和 HUD 窗口。在此源码测试环境中，Rust 计算二进制未就位，后台库存计算返回缺少引擎，界面仍可打开。实机捕获、计算产物与发行 ZIP 属于后续验收。

定向用例通过各现场解释器以 `-B -m unittest tests.contracts.test_host_loading tests.contracts.test_role_picker tests.contracts.test_hud_window` 选中。2.0.8 使用缓存 wheel 的隔离导入目录；2.0.17 复用用户提供的宿主环境，并用导入过滤器限制 Qt 模块和 Fluent Widgets。

| 环境与输入 | 字面输出 | 退出码 |
| --- | --- | --- |
| ok-script 2.0.8，根目录任务扫描 | `HOST_WORKBENCH_OK: manifest, three tasks, shared Qt pages and runtime shutdown`，`Ran 10 tests in 8.199s`，`OK` | 0 |
| ok-script 2.0.17，manifest 声明，限制 Qt 导入 | `QT_IMPORTS: only QtCore, QtGui, QtWidgets, QtSvg; no Fluent Widgets`，`Ran 10 tests in 6.989s`，`OK` | 0 |
| `.venv/Scripts/python.exe -B tools/check_structure.py` | `"errors": []`，`"status": "passed"` | 0 |

### 新版桌面挂载回归

使用宿主 `03632d02` 的 `requirements.txt` 在隔离 Python 3.12.13 环境执行 `uv pip sync --python <3.x解释器> ../ok-wuthering-waves/requirements.txt`（退出码 0）。实际从此环境加载 ok-script 3.0.6、ok-pyside-minimal 6.12.0.post2（提供 `PySide6`）和 onnxocr-ppocrv6 0.0.1，未混装完整 PySide6 或 Fluent Widgets。

离屏桌面复现使用该提交的配置，禁用游戏启动、内置游戏任务和模板加载，保留真实 Qt 主窗口、任务管理器与插件页面注册器。在临时 `ok_import/ww-echo-workshop` 放入三个源码入口及版本化 manifest；在临时配置中令 `CaptureHotkeysJSON` 的值为 `{`。修复前，任务注册成功，但 `ImportedPluginPage → create_import_page → _configure_shortcuts` 抛出 `JSONDecodeError: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)`，侧栏工坊降级为任务列表。修复后，首次挂载、任务卡及弹窗使用同一快捷键恢复逻辑：损坏值保持原样，快捷键设置显示提示，工坊仍可打开，显式保存可清除提示。安装 ID、任务名和数据路径未变。

| 验证命令 | 输入和字面结果 | 退出码 |
| --- | --- | --- |
| `<3.x解释器> -B %TEMP%/ww-echo-workshop-desktop-smoke.py --invalid-shortcut` | 离屏桌面、临时插件与损坏快捷键；`DESKTOP_ENTRY ['WWEchoWorkshopSettingsTask', 'WWEchoWorkshopOverlayTask', 'WWEchoWorkshopScanTask'] [] {}`、`DESKTOP_MOUNT_OK`、`DESKTOP_RELOAD_OK`、`DESKTOP_QUIT_OK`。卸载后页面消失且运行时关闭；重新加载只有一个侧栏页，合成档案 UUID 保持不变。 | 0 |
| `<3.x解释器> -B -m unittest tests.contracts.test_host_loading tests.contracts.test_role_picker tests.contracts.test_hud_window` | 同一 manifest 与损坏快捷键夹具；`HOST_WORKBENCH_OK: manifest, three tasks, shared Qt pages and runtime shutdown`、`Ran 10 tests in 2.455s`、`OK`。 | 0 |
| `<2.0.17解释器> -B -m unittest tests.contracts.test_host_loading` | 完整 PySide6 6.11.1、原生页面容器；`HOST_WORKBENCH_OK: manifest, three tasks, shared Qt pages and runtime shutdown`、`Ran 3 tests in 2.804s`、`OK`。 | 0 |
| `<旧宿主解释器> -B -m unittest tests.contracts.test_host_loading` | 以 `PYTHONPATH` 优先加载 2.0.8 的独立 wheel 目录；根目录发现并点击真实旧宿主任务卡进入工坊；`HOST_WORKBENCH_OK: manifest, three tasks, shared Qt pages and runtime shutdown`、`Ran 3 tests in 3.935s`、`OK`。 | 0 |
| 分别用 3.0.6 / 2.0.17 解释器执行 `-B %TEMP%/ww-echo-workshop-ocr-smoke.py` | OpenCV 生成同一张 610×160 的合成字图；真实私有 OCR 的宿主 Box 偏移均为 (37, 23)。v6：`OCR_BOX_OK 3.0.6 [('CRIT RATE 6.3%', 0.9991, 45, 54, 444, 53)]`；v5：`OCR_BOX_OK 2.0.17 [('CRIT RATE 6.3%', 0.9739, 40, 43, 453, 70)]`。 | 0 / 0 |

这些结果覆盖新版 Windows Qt 启动、挂载、重新加载和退出，以及两种旧宿主的进入方式。源码测试未部署随包的 Rust 程序，离线库存后台查询仍报 `engine_missing`；上述 OCR 比较只证明合成字图的 API 与坐标转换，不代表实际游戏帧识别。真实游戏捕获、自动扫描、宽窄窗/主题/DPI 和发行 ZIP 留待后续对应阶段。

### 阶段 2：脱敏帧回放

用代码生成四张 1600×900 合成画面，仅包含匿名场景、词条及等级文字；使用真实私有 CPU OCR 和现有 `build/native/release/ww-echo-workshop-compute.exe`，没有读取实际玩家截图或库存。回放发现从背包快速切入对比页时，旧的裁剪布局提示会把已识别的右栏误标为背包。顶部结构变化现在绕过全图刷新冷却，同帧重判布局；评分、手动和自动扫描共享只读像素的 OCR 边界。

手动收录冻结最后一帧，只接受当前请求；停止、重新开启或更换档案/来源，旧任务不得写入。扫描需要目标点击回执、两张新鲜稳定详情帧以及滚轮后的列表变化，且在原生质量检查后、写入前再次校验会话与前台。背包加速裁剪未识别时，在同一冻结帧回退到原始详情；证据不是拼接图。不启用任何后台扫描组合；仅为原生 SDR WGC/BitBlt 保留前台原生鼠标路径，未读取或改写宿主的鼠标后端设置。

在仓库根设置 `QT_QPA_PLATFORM=offscreen`、`PYTHONPATH=<仓库>/src`、`PYTHONIOENCODING=utf-8`。2.0.8 在 `PYTHONPATH` 中优先加载缓存 wheel 的独立目录；2.0.17 与 3.0.6 沿用第 6 节的独立 Qt/OCR 环境。

| 验证命令 | 输入和字面结果 | 退出码 |
| --- | --- | --- |
| 3.0.6：`python -B -m unittest tests.calculation.test_native_observations tests.contracts.test_capture_replay tests.contracts.test_private_ocr tests.contracts.test_scan_grade_ocr tests.contracts.test_live_snapshot tests.contracts.test_live_stability tests.contracts.test_host_loading` | 原生评分黄金值、四场景合成图、临时档案、捕获替换和取消；`CAPTURE_LIVE_REPLAY_OK 3.0.6: [('display', 17.02), ('bag', 17.02), ('comparison', 17.73), ('enhancement', 17.02)]`、`CAPTURE_INTAKE_REPLAY_OK 3.0.6: frozen, cancelled, safe fallback, detail PNG`、`Ran 26 tests in 6.000s`、`OK`。计算模块放在 Qt 宿主测试前，避免后者创建的 QApplication 触发主线程计算保护。 | 0 |
| 2.0.17：`python -B -m unittest tests.contracts.test_capture_replay tests.contracts.test_host_loading` | PP-OCRv5 与原生页面容器；`CAPTURE_LIVE_REPLAY_OK 2.0.17: [('display', 17.02), ('bag', 17.02), ('comparison', 17.73), ('enhancement', 17.02)]`、`CAPTURE_INTAKE_REPLAY_OK 2.0.17: frozen, cancelled, safe fallback, detail PNG`、`Ran 8 tests in 9.677s`、`OK`。 | 0 |
| 2.0.8：`python -B -m unittest tests.contracts.test_capture_replay tests.contracts.test_host_loading` | PP-OCRv5 与真实旧任务卡；`CAPTURE_LIVE_REPLAY_OK 2.0.8: [('display', 17.02), ('bag', 17.02), ('comparison', 17.73), ('enhancement', 17.02)]`、`CAPTURE_INTAKE_REPLAY_OK 2.0.8: frozen, cancelled, safe fallback, detail PNG`、`Ran 8 tests in 8.996s`、`OK`。 | 0 |
| `./.venv/Scripts/python.exe -B tools/check_structure.py` | `"files": 338`、`"errors": []`、`"status": "passed"`。 | 0 |

上述回放没有真实游戏进程、Windows 输入或实机 DPI 数据。阶段 2 的现场检查统一使用 [G5 真实游戏验收清单](release-g5-checklist.md)：分别记录评分、手动收录、前台扫描及各捕获／输入组合。未测项保留为未测，后台扫描继续关闭。

#### 离线补充：滚动行位回执

合成列表验证了旧判定的一个缺口：跳过整页时，像素确实变化，但前后没有一行重叠，不能据此推定固定三行位移。前台扫描现逐次滚动，只有两张新鲜画面共同确认滚动条方向和唯一重叠行时才按实测位移推进。无重叠、重复行歧义或尚未稳定时暂停；恢复先观察未确认的滚动，不额外发送滚轮。短页只裁剪、点击实测格子，已扫描的重叠格不重扫。

复用本节三套解释器和环境，分别运行 `python -B -m unittest -v tests.contracts.test_scan_scroll tests.contracts.test_capture_replay tests.contracts.test_host_loading`。合成输入覆盖整页跳过、重复行、原地未滚动、短页底部和暂停后确认原滚轮回执，没有调用真实鼠标输入。

| ok-script | 字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 | `test_production_scroll_dispatch_holds_after_ambiguous_frame (...) ... ok`；`Ran 12 tests in 7.229s`；`OK` | 0 |
| 2.0.17 | `Ran 12 tests in 7.652s`；`OK` | 0 |
| 2.0.8 | `Ran 12 tests in 7.144s`；`OK` | 0 |

本轮按用户要求只做离线验证。尚未验收真实游戏的滚动步长和窗口捕获，不为任何组合开放后台扫描。

#### 离线补充：完整扫描任务的取消

用三个合成卡片回放真实 `WWEchoWorkshopScanTask.run()`：执行背包预检、独立等级 OCR、前台选格回执、冻结详情和临时 SQLite 写入，捕获输入及点击由夹具代替；没有读取玩家截图、触发鼠标或运行游戏。第一件入库后，在最终排空阶段停止扫描：第二件正在解析，第三件还在队列。三个宿主只保留了第一件的记录及一张原始裁剪 PNG；停止后未发送输入，诊断为 `state=cancelled stage=draining`、`added=1`、`pending=0`、`failed=0`、`backlog=0`。2.x 的 PP-OCRv5 回放还经过了一次单件等级补读。

补上两项取消回归：修复前队列取消后仍报告 `pending=2`（断言 `0 != 2`）；现在清空计数并丢弃迟到结果。packed 解析收到取消信号后不再回退到原始详情 OCR，也不会编码或提交证据。等级 OCR 在持有它的工作线程结束时清理。

| ok-script | 命令与字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 | `python -B -m unittest tests.calculation.test_native_observations.NativeObservations.test_scan_pipeline_cancel_reaches_live_child_without_error_diagnostics_or_partial_result tests.contracts.test_capture_replay tests.contracts.test_scan_scroll`；`CAPTURE_SCAN_CHAIN_OK 3.0.6: committed=1, cancelled_ocr=1, png=1`；`Ran 13 tests in 8.847s`；`OK` | 0 |
| 2.0.17 | `python -B -m unittest tests.contracts.test_capture_replay tests.contracts.test_scan_scroll`；`CAPTURE_SCAN_CHAIN_OK 2.0.17: committed=1, cancelled_ocr=1, png=1`；`Ran 12 tests in 7.594s`；`OK` | 0 |
| 2.0.8 | 同 2.0.17 命令，以缓存 wheel 优先加载 2.0.8；`CAPTURE_SCAN_CHAIN_OK 2.0.8: committed=1, cancelled_ocr=1, png=1`；`Ran 12 tests in 7.449s`；`OK` | 0 |

本轮继续遵循仅离线验证的范围；主任务实机验证和后台组合许可保持原状态。

#### 离线补充：三条采集链路共用解析

详情读取、词条解析与记录构建移入 `capture`。实时评分、手动收录和自动扫描各自装配同一 `PanelReader`；自动扫描的详情和等级 OCR 不再复制实时宿主任务。宿主适配层只复制必要的文字修正规则，将宿主 `Box` 转成普通像素坐标与置信度；等级仍使用独立的私有 CPU OCR 引擎。黄金样本的数值与投影预期保持不变。

使用原有脱敏帧和临时档案，分别运行 `python -B -m unittest tests.contracts.test_capture_replay tests.contracts.test_scan_grade_ocr tests.contracts.test_host_loading`；3.0.6 在前面加 `tests.calculation.test_native_observations` 检查黄金样本（计算必须在 Qt 宿主契约之前执行）。坐标夹具验证 OCR 框从裁剪原点正确映射为 `(12, 23, 10, 6, 0.91)`、宿主修正字典后续变化不影响已冻结读取器，且后者不保留宿主执行器引用。

| ok-script | 字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 | `CAPTURE_SCAN_CHAIN_OK 3.0.6: committed=1, cancelled_ocr=1, png=1`；`Ran 23 tests in 10.961s`；`OK` | 0 |
| 2.0.17 | `CAPTURE_SCAN_CHAIN_OK 2.0.17: committed=1, cancelled_ocr=1, png=1`；`Ran 14 tests in 10.135s`；`OK` | 0 |
| 2.0.8 | `CAPTURE_SCAN_CHAIN_OK 2.0.8: committed=1, cancelled_ocr=1, png=1`；`Ran 14 tests in 9.598s`；`OK` | 0 |

仓库解释器在独立进程导入 `PanelReader` 和 `analyze_echo_stats`：`PANEL_IMPORT_OK: no OK Script or Qt imported`（退出码 0）。`tools/check_structure.py` 返回 `"files": 342`、`"errors": []`、`"status": "passed"`（退出码 0）。本轮只做离线回放，未开放任何后台扫描组合。

#### 离线补充：捕获格式与画面坐标

捕获目标的绑定校验现在包含宿主的窗口目标签名；即使 HWND 不变，子窗或裁剪边界变化时也丢弃旧帧。采集层只向 OCR 交付有效的 uint8 BGR 帧，宿主捕获错误视为本次未取得画面；HDR 只接受宿主已经完成色调映射的 BGR，不开启自动扫描。超宽画面按实际右边界定位详情，缩放及黑边场景用原图像素保存证据。HUD 稳定化仅沿用分数与显示文字，不沿用坐标；从上一帧借用的 COST 只用于估算，不授予确定评级。

沿用上述离屏环境与版本隔离。旧版命令为 `python -B -m unittest -v tests.contracts.test_live_stability tests.contracts.test_capture_replay`，3.0.6 在模块列表开头加 `tests.calculation.test_native_observations`。共用夹具包括同句柄重绑定、格式错误及宿主捕获异常、1280×720 合成详情加不等宽黑边，以及连续帧的坐标与 COST 证据变化。原生黄金值不变。

| ok-script | 字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 | `CAPTURE_SCALED_EVIDENCE_OK 3.0.6: viewport=(80,32,1280,720), crop=(931, 90, 423, 612)`；`Ran 26 tests in 8.077s`；`OK` | 0 |
| 2.0.17 | `CAPTURE_SCALED_EVIDENCE_OK 2.0.17: viewport=(80,32,1280,720), crop=(931, 90, 423, 612)`；`Ran 16 tests in 6.554s`；`OK` | 0 |
| 2.0.8 | `CAPTURE_SCALED_EVIDENCE_OK 2.0.8: viewport=(80,32,1280,720), crop=(931, 90, 423, 612)`；`Ran 16 tests in 6.488s`；`OK (skipped=1)`（该版本没有 WGC HDR 捕获类） | 0 |

证据 PNG 解码后逐像素等于输入帧的 `frame[90:702,931:1354]`。3.0.6 与 2.0.17 通过合成 FP16 像素验证宿主 HDR 转换与只读限制；未经过真实 Windows 捕获、显示器 DPI 或游戏输入验证。本轮继续保持后台扫描关闭。

#### 离线补充：冻结证明与提交前切换

核对生产调用、插件入口和维护文档后，删除没有生产调用方的连续收录旧入口及其去重/稳定状态。手动仅通过冻结帧证明提交，扫描仅通过点击后取得的稳定详情证明提交；实时评分仍不提交库存。两条收录链共用原有质量判断、查重及升级待确认逻辑，记录和证据格式不变。

查重可能晚于捕获源切换或窗口失焦。修复前，用已有声骸指纹回放手动收录，在真实 SQLite 查重之后模拟切换捕获源；错误结果是 `已提交=True`、`pending=1`，新回归得到 `AssertionError: False is not true`、`FAILED (failures=1)`（退出 1）。现于查重、判定升级之后且写入 PNG 或库存之前再核对会话；扫描提交使用相同边界。离线回放验证手动切源、扫描失去前台均只保留先前成功提交的一条记录及一张 PNG，数据库待确认区为空。

| ok-script | 验证命令及字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 基线 | `python -B -m unittest -v tests.contracts.test_capture_replay.FrameReplayContract.test_manual_cancellation_and_scan_detail_replay`；`Ran 1 test in 2.351s`；`FAILED (failures=1)` | 1 |
| 3.0.6 | `python -B -m unittest -v tests.contracts.test_capture_replay tests.contracts.test_scan_scroll`；`CAPTURE_LATE_GUARD_OK 3.0.6: saved=1, pending=0, png=1`；`Ran 15 tests in 8.356s`；`OK` | 0 |
| 2.0.17 | 同一命令，使用旧宿主解释器；`CAPTURE_LATE_GUARD_OK 2.0.17: saved=1, pending=0, png=1`；`Ran 15 tests in 7.495s`；`OK` | 0 |
| 2.0.8 | 同一命令，缓存 wheel 优先加载；`CAPTURE_LATE_GUARD_OK 2.0.8: saved=1, pending=0, png=1`；`Ran 15 tests in 7.469s`；`OK (skipped=1)`（无 WGC HDR 捕获类） | 0 |

沿用本节离屏环境与合成帧，只使用临时档案。Ruff 定向检查 `All checks passed!`、`4 files already formatted`；结构检查 `"files": 342`、`"errors": []`、`"status": "passed"`（退出 0）。真实 Windows 捕获及输入仍未验收，后台扫描保持关闭。

#### 离线补充：暂停恢复先核对背包

修复前，从暂停点继续会把缓存网格直接交给遍历状态机，再回到主循环取新帧；基线回归的五类输入均未调用取帧（`Expected 'read_task_sample' to have been called once. Called 0 times.`，`FAILED (failures=5)`）。现在取新帧后才从暂停恢复：同页核对卡片布局、内容和滚动条；缺帧、重复帧或未识别为背包时继续等待，确认列表位置或内容变化时停止。暂停前的选格点击不再充当恢复后的回执。若暂停时已有滚轮待确认，保留原滚动记录，恢复后必须由两张新帧确认，先不发送下一次滚轮，也不消费待写入结果。

沿用三套宿主的离屏环境。新增回放覆盖缺帧、重复帧、同页、滚动条变化但内容相同、同滚动位置更换列表，以及待确认滚轮。另复用已有回放核对完整扫描的取消，以及被禁止时不触发输入。

| ok-script | 验证命令及字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 基线 | `python -B -m unittest -v tests.contracts.test_scan_scroll.ScrollReplay.test_resume_waits_for_new_frame_and_rejects_changed_list`；`Ran 1 test in 0.246s`；`FAILED (failures=5)` | 1 |
| 3.0.6 | `python -B -m unittest -v tests.contracts.test_scan_scroll tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr tests.contracts.test_capture_replay.CaptureDeviceContract.test_production_click_and_scroll_never_reach_native_input_when_blocked`；`Ran 7 tests in 4.220s`；`OK` | 0 |
| 2.0.17 | 同一命令；`Ran 7 tests in 3.789s`；`OK` | 0 |
| 2.0.8 | 同一命令，缓存 wheel 优先加载；`Ran 7 tests in 3.809s`；`OK` | 0 |

Ruff 定向检查 `All checks passed!`、`4 files already formatted`；结构检查 `"files": 342`、`"errors": []`、`"status": "passed"`（退出 0）。本轮仍只做离线验证；游戏 DPI、滚动位移及捕获与输入组合未实测，后台扫描保持关闭。

#### 离线补充：词条小幅变化即时刷新

32 像素间距的取样现在只用于安排全量 OCR 刷新。两次全量刷新之间，用当前 OCR 区域的原始像素指纹判断是否复用结果；稳定背包/展示页中，详情区不变的中央模型动画不触发详情 OCR，切页判定、定期全量刷新与回退保留。

基线回放将一件声骸的副词条从 13.8% 改成 15.0%，保持全部粗取样点一致。修复前真实 OCR 没有重读，沿用 17.02，断言报告 `17.73 != 17.02`；同一批次还复现了仅中央模型变化触发额外 OCR。修复后，三版宿主均在全量刷新冷却内通过真实私有 OCR 和现有原生引擎更新为 17.73；四类画面的原有评分结果不变。

| ok-script | 命令及字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 基线 | `python -B -m unittest -v tests.contracts.test_live_stability.LiveAnalysisStabilityContract.test_right_detail_fingerprint_ignores_model_motion_but_detects_small_glyph tests.contracts.test_capture_replay.FrameReplayContract.test_real_ocr_and_native_score_for_four_scenes`；`Ran 2 tests in 2.107s`；`FAILED (failures=2)` | 1 |
| 3.0.6 | `python -B -m unittest -v tests.contracts.test_live_stability tests.contracts.test_capture_replay.FrameReplayContract.test_real_ocr_and_native_score_for_four_scenes`；`CAPTURE_DIGIT_REFRESH_OK 3.0.6: 17.73`；`Ran 7 tests in 3.125s`；`OK` | 0 |
| 2.0.17 | 同一命令；`CAPTURE_DIGIT_REFRESH_OK 2.0.17: 17.73`；`Ran 7 tests in 2.519s`；`OK` | 0 |
| 2.0.8 | 同一命令，缓存 wheel 优先加载；`CAPTURE_DIGIT_REFRESH_OK 2.0.8: 17.73`；`Ran 7 tests in 2.500s`；`OK` | 0 |

仅离屏合成回放，未测实机画面、DPI 或输入，后台扫描保持关闭。Ruff 定向检查 `All checks passed!`、`3 files already formatted`；结构检查 `"files": 342`、`"errors": []`、`"status": "passed"`（退出 0）。

#### 离线补充：卸载时切断在途 OCR 的结果

私有 OCR 仍串行推理；关闭不再等待原生推理锁，而是立即使旧一代 OCR 失效。已经进入原生库的调用正常返回时释放旧引擎，不强杀线程；旧结果不做坐标转换或发布。实时评分发布前再次确认任务未卸载。手动收录卸载时撤销当前请求、丢弃排队快照，最多等待解析线程 0.25 秒；线程随后退出，卸载后拒绝入队。

基线以事件暂停合成 OCR，`python -B -m unittest -v tests.contracts.test_private_ocr.PrivateOcrContract.test_close_detaches_blocked_engine_and_discards_its_result` 得到 `OCR close waited for blocked inference`、`Ran 1 test in 1.035s`、`FAILED (failures=1)`（退出 1）。修复后，关闭先于原生调用返回；真实宿主任务对象的卸载夹具验证丢弃 1 个排队快照、0 个迟到结果，并在解开夹具后确认线程退出。既有场景回放直接使用每版宿主的私有 CPU OCR。

离屏命令：`python -B -m unittest -v tests.contracts.test_private_ocr tests.contracts.test_scan_grade_ocr tests.contracts.test_capture_replay.FrameReplayContract.test_real_ocr_and_native_score_for_four_scenes tests.contracts.test_capture_replay.FrameReplayContract.test_manual_cancellation_and_scan_detail_replay tests.contracts.test_host_loading`。2.0.8 沿用缓存 wheel 优先加载。

| ok-script | 字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 | `CAPTURE_MANUAL_UNLOAD_OK 3.0.6: drained=1, late_results=0`；`Ran 12 tests in 6.463s`；`OK` | 0 |
| 2.0.17 | `CAPTURE_MANUAL_UNLOAD_OK 2.0.17: drained=1, late_results=0`；`Ran 12 tests in 7.053s`；`OK` | 0 |
| 2.0.8 | `CAPTURE_MANUAL_UNLOAD_OK 2.0.8: drained=1, late_results=0`；`Ran 12 tests in 6.660s`；`OK` | 0 |

Ruff 定向检查 `All checks passed!`、`4 files already formatted`；结构检查 `"files": 342`、`"errors": []`、`"status": "passed"`（退出 0）。本轮只做离线验证，未触发真实鼠标或打开游戏；后台扫描保持关闭。

#### 离线补充：窗口与捕获器绑定一致后再取帧

宿主切换窗口时，设备管理器和捕获器可能短暂指向不同的窗口对象，即使 HWND 的数值恰好相同。离线夹具用宿主实际 BitBlt 类构造这段过渡：基线的被动取帧返回旧捕获器的 `uint8` BGR 缓冲，回归报 `FAILED (failures=1)`。现在，声明窗口绑定的捕获器只在绑定与管理器的有效窗口完全一致时报告就绪；被动和执行器取帧均在错绑时不调用取帧方法，重新绑定后恢复。被动取帧也从实际将要读取的对象构造身份，并在读取前后复核。没有新增自动扫描许可。

设备身份、格式和输入许可契约移到 `tests/contracts/test_capture_device.py`，与多场景 OCR 和 SQLite 回放分开，原有断言全部保留。环境沿用本节三套隔离解释器，无真实设备调用。

| ok-script | 命令及字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 基线 | `python -B -m unittest -v tests.contracts.test_capture_replay.CaptureDeviceContract.test_capture_waits_for_native_capture_binding_to_follow_host_window`；`Ran 1 test in 0.226s`；`FAILED (failures=1)` | 1 |
| 3.0.6 | `python -B -m unittest -v tests.contracts.test_capture_device tests.contracts.test_capture_replay.FrameReplayContract.test_real_ocr_and_native_score_for_four_scenes tests.contracts.test_capture_replay.FrameReplayContract.test_manual_cancellation_and_scan_detail_replay tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr`；`Ran 9 tests in 5.845s`；`OK` | 0 |
| 2.0.17 | 同一命令；`Ran 9 tests in 5.309s`；`OK` | 0 |
| 2.0.8 | 同一命令，缓存 wheel 优先加载；`Ran 9 tests in 5.179s`；`OK (skipped=1)`（无 WGC HDR 捕获类） | 0 |

Ruff 定向检查与结构检查均通过。三版同时回放四种评分场景、手动冻结与扫描取消。未使用真实游戏或输入，后台扫描保持关闭。

#### 离线补充：扫描双帧同时核对词条名、等级和 COST

扫描详情的稳定判定原先比较标题、装备者和数值区，未覆盖词条名、等级及 COST。回放保持同一个选格与相同数值，只更改其中一项，三类输入都错误地产生了 `tokens=(21, 22)` 的冻结证明。现在按名/值分别比较每行，并独立核对等级、COST 的文字区域。两帧不同会重新累积；仅中央模型变化时，仍可用同一详情的 `tokens=(22, 23)` 通过。冻结签名也覆盖扩展后的 COST 区域，不修改 OCR 输入的原始像素、库存格式或评分规则。

| ok-script | 命令及字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 基线 | `python -B -m unittest -v tests.contracts.test_capture_replay.ScanDetailProofContract`；`Ran 1 test in 0.057s`；`FAILED (failures=3)` | 1 |
| 3.0.6 | `python -B -m unittest -v tests.contracts.test_capture_replay.ScanDetailProofContract tests.contracts.test_capture_replay.FrameReplayContract.test_manual_cancellation_and_scan_detail_replay tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr`；`Ran 3 tests in 4.304s`；`OK` | 0 |
| 2.0.17 | 同一命令；`Ran 3 tests in 3.947s`；`OK` | 0 |
| 2.0.8 | 同一命令，缓存 wheel 优先加载；`Ran 3 tests in 3.871s`；`OK` | 0 |

沿用离屏合成画面和临时档案；三版均保留手动详情回退与整条扫描取消结果（`committed=1, cancelled_ocr=1, png=1`）。Ruff 定向检查 `All checks passed!`、`2 files already formatted`；结构检查 `"files": 343`、`"errors": []`、`"status": "passed"`（退出 0）。未开启实机或真实输入，后台扫描保持关闭。

#### 离线补充：手动收录识别采样间消失的词条

手动收录仍只解析最后一帧。原先稳定检查只覆盖最终 OCR 找到的文字框；如果首帧多一条副词条、最后一帧消失，缺失位置没有 OCR 框可检。合成回放在首帧加入一条 `防御力 40`，其余字段不变；基线仍报告 `已提交=True`，回归失败（退出 1）。现在原始详情裁剪范围还会按行高分带比较文字像素，不依赖最终帧是否识别到该行。回放确认取消时库存和 PNG 均无新增；仅中央模型变化或详情区出现单点亮斑时仍可收录。

| ok-script | 命令及字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 基线 | `python -B -m unittest -v tests.contracts.test_capture_replay.FrameReplayContract.test_manual_cancellation_and_scan_detail_replay`；`Ran 1 test in 1.766s`；`FAILED (failures=1)` | 1 |
| 3.0.6 | `python -B -m unittest -v tests.contracts.test_capture_replay.FrameReplayContract.test_manual_cancellation_and_scan_detail_replay tests.contracts.test_capture_replay.FrameReplayContract.test_scaled_letterbox_has_original_pixel_evidence`；`Ran 2 tests in 7.390s`；`OK` | 0 |
| 2.0.17 | 同一命令；`Ran 2 tests in 6.279s`；`OK` | 0 |
| 2.0.8 | 同一命令，缓存 wheel 优先加载；`Ran 2 tests in 6.416s`；`OK` | 0 |

三版输出均含 `手动收录未提交：采样词条或名称发生变化`；黑边缩放夹具确认稳定证明、原图裁剪坐标与 PNG 像素未改变。Ruff 定向检查 `All checks passed!`、`2 files already formatted`；结构检查 `"files": 343`、`"errors": []`、`"status": "passed"`（退出 0）。未启动游戏或触发真实输入，后台扫描保持关闭。

#### 离线补充：证据落盘期间切换会话

收录共享的发布检查原先停在 PNG 落盘之前，写盘和读回校验期间切换捕获源仍可能新增待确认记录。合成回放在新 PNG 已写出、读回校验时替换捕获源；基线仍报告 `已提交=True`，回归发现 `source switched during PNG write`。现在在临时文件刷盘后以及最终文件读回后各核对一次会话；取消时清理尚未发布的临时文件，或只撤回本次新生成的 PNG。已有同哈希证据不删除，未参与收录的归档调用继续使用原有无守卫接口。

离屏环境沿用本节，另设 `OK_DISABLE_FILE_LOG=1`。只操作合成档案。回放同时核对取消时没有待确认项、无临时文件残留、旧 PNG 字节不变，以及正常扫描已入库记录仍保留。

| ok-script | 命令及字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 基线 | `python -B -m unittest -v tests.contracts.test_capture_replay.FrameReplayContract.test_manual_cancellation_and_scan_detail_replay`；`Ran 1 test in 4.094s`；`FAILED (failures=1)` | 1 |
| 3.0.6 | `python -B -m unittest -v tests.contracts.test_capture_replay.FrameReplayContract.test_manual_cancellation_and_scan_detail_replay tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr`；`Ran 2 tests in 8.745s`；`OK` 后测试解释器未退出，已单独终止 | -1 |
| 3.0.6 串行复验 | 在同一命令的 `-B` 后加 `-u -X faulthandler`；`Ran 2 tests in 5.899s`；`OK`，进程正常结束 | 0 |
| 2.0.17 | 同一命令；`Ran 2 tests in 7.471s`；`OK` | 0 |
| 2.0.8 | 同一命令，缓存 wheel 优先加载；`Ran 2 tests in 7.619s`；`OK` | 0 |

Ruff 定向检查 `All checks passed!`、`3 files already formatted`；结构检查 `"files": 343`、`"errors": []`、`"status": "passed"`（退出 0）。未启动游戏或触发真实输入，后台扫描保持关闭。

#### 离线补充：等级低于阈值先复核选中详情

卡片等级是快速筛选提示，不再单独触发提前结束。低于阈值或未读清时，先确认选中回执和两张稳定详情帧，再由独立等级线程补读详情头部。补读返回时，如当前详情已经变化，丢弃结果并重新取证。补读仍未读清则记录单件失败并继续；只有经稳定详情验证的低等级才按降序结束，该件不入库。阈值本身不变。

真实任务回放将第一页首格的卡片 OCR 从 +5 模拟为 +0，其他 OCR 保持原样。基线直接结束，输出 `grid misread stopped the scan`，没有执行详情补读。修复后详情读回 +5，第一件仍可入库，继续扫描并保留原有取消行为。另一夹具验证迟到的 +0 不结束扫描，随后在新稳定画面确认 +3 才结束、跳过 1 件且无待写入。

| ok-script | 串行离屏命令及字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 基线 | `python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr`；`Ran 1 test in 2.836s`；`FAILED (failures=1)` | 1 |
| 3.0.6 | `python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_grade_ocr tests.contracts.test_capture_replay.ScanDetailProofContract tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr`；`Ran 6 tests in 3.747s`；`OK` | 0 |
| 2.0.17 | 同一命令；`Ran 6 tests in 3.015s`；`OK` | 0 |
| 2.0.8 | 同一命令，缓存 wheel 优先加载；`Ran 6 tests in 3.029s`；`OK` | 0 |

沿用离屏环境及 `OK_DISABLE_FILE_LOG=1`；Ruff 定向检查 `All checks passed!`、`6 files already formatted`；结构检查 `"files": 343`、`"errors": []`、`"status": "passed"`（退出 0）。未使用实机输入，未新增任何扫描许可。

#### 离线补充：运行中的一两格短列表重新实测

常规列表保护至少比较出三格变化才判定移动，因此一两格短列表无论怎么更换都无法触发。只采样旧格中心时，旧格不动但旁边新增一格也看不出来。运行中的短列表现在重新测量卡片数、几何、内容和滚动信息，不依赖旧坐标。首张不符的帧不给输入或提交使用；两张新帧确认同一变化后停止，若恢复原列表则继续。常规页的滚动回执、暂停恢复规则不变。

| ok-script | 串行离屏命令及字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 基线 | `python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_scroll.ScrollReplay.test_running_short_page_remeasures_replaced_or_added_cards`；旧帧被接纳：`unconfirmed grid must not authorize input`；`Ran 1 test in 0.123s`；`FAILED (failures=3)` | 1 |
| 3.0.6 | `python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_scroll tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr`；`Ran 7 tests in 5.378s`；`OK` | 0 |
| 2.0.17 | 同一命令；`Ran 7 tests in 4.297s`；`OK` | 0 |
| 2.0.8 | 同一命令，缓存 wheel 优先加载；`Ran 7 tests in 3.668s`；`OK` | 0 |

修复后的合成回放覆盖换件、新增、减少和单帧后恢复；三版也复核了整条扫描取消、滚动歧义及暂停继续。沿用离屏环境及 `OK_DISABLE_FILE_LOG=1`，未调用真实设备。Ruff 定向检查 `All checks passed!`、`2 files already formatted`；结构检查 `"files": 343`、`"errors": []`、`"status": "passed"`（退出 0）。后台扫描保持关闭。

#### 离线补充：排序准备只使用新测格子

自动扫描开始前先确认在列表顶部；该条件与遍历启动共用。选择等级排序后等待新的列表画面。若菜单自动关闭，不多点声骸；若菜单仍开，只用排序后实测且位于菜单上方的首格来关闭，随后再次确认菜单已关、等级排序生效和选格回执。没有新网格或离开顶部时停止准备，不用排序前缓存坐标。

基线合成夹具记录到旧首格 `(208, 197)` 被点击；修复后的同一场景只用新首格 `(251, 227)`。还验证了菜单自动关闭、菜单按钮文案延迟更新、排序无回执、开始时已滚动和排序后离开顶部。所有点击由夹具捕获，未调用真实鼠标。

| ok-script | 串行离屏命令及字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 基线 | `python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_scroll.ScrollReplay.test_sort_preflight_waits_for_new_grid_before_any_item_click`；`Ran 1 test in 0.006s`；`FAILED (failures=5)` | 1 |
| 3.0.6 | `python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_scroll tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr`；`Ran 8 tests in 6.038s`；`OK` | 0 |
| 2.0.17 | 同一命令；`Ran 8 tests in 4.876s`；`OK` | 0 |
| 2.0.8 | 同一命令，缓存 wheel 优先加载；`Ran 8 tests in 5.148s`；`OK` | 0 |

Ruff 定向检查 `All checks passed!`、`4 files already formatted`；结构检查 `"files": 343`、`"errors": []`、`"status": "passed"`（退出 0）。真实游戏的排序行为、坐标及输入仍待实机验证，后台扫描保持关闭。

#### 离线补充：断线恢复不抢占其他任务的捕获

新版宿主无帧时可能跳过触发任务，故保留既有的轻量恢复监视器，但只在实时评分已启用、没有收录或恢复库存作业、宿主未暂停且没有其他任务占用或排队时允许恢复原生 Windows 捕获。宿主仍选择 `pc/pc_<hwnd>` 但重启后旧条目已消失时，保留正常恢复。查询连接后再核对任务、停止状态、首选设备和窗口绑定；不为浏览器或其他后端关闭共享捕获，不改变扫描输入许可。

合成宿主夹具使用真实 BitBlt 类的无设备实例，并替换关闭和刷新调用。基线在其他任务运行、浏览器目标、探测期间启动任务及探测期间停止四种情况下均错误调用恢复，得到 `FAILED (failures=4)`。修复后这几种情况及待执行任务、探测期间改选设备均不关闭或刷新；空闲 Windows 断线或重启只恢复一次，仍受重试间隔限制。

| ok-script | 串行离屏命令及字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 基线 | `python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_device.CaptureDeviceContract.test_capture_recovery_does_not_close_capture_during_other_host_work`；`Ran 1 test in 0.352s`；`FAILED (failures=4)` | 1 |
| 3.0.6 | `python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_device tests.contracts.test_capture_replay.FrameReplayContract.test_real_ocr_and_native_score_for_four_scenes tests.contracts.test_host_loading`；`Ran 12 tests in 5.306s`；`OK` | 0 |
| 2.0.17 | 同一命令；`Ran 12 tests in 4.368s`；`OK` | 0 |
| 2.0.8 | 同一命令，缓存 wheel 优先加载；`Ran 12 tests in 4.099s`；`OK (skipped=1)`（无 WGC HDR 捕获类） | 0 |

另复核四种实时评分场景及三任务加载和卸载。Ruff 定向检查 `All checks passed!`、`3 files already formatted`；结构检查 `"files": 343`、`"errors": []`、`"status": "passed"`（退出 0）。真实游戏重启与输入未验收，后台扫描保持关闭。

#### 离线补充：滚动回执保留已确认的前进位置

同批次滚动仍以起始页计算全局行号，但后续回执还要符合已经确认的最远位置。只比较起始页时，滚动条退回中间位置，或滚动条没变、同一行却明显下移，旧逻辑都会把两张回退帧当成有效回执。现在保留本批次已确认的滚动条上界和同一行的最靠上位置，暂停或重新等待不清除这些界限；容许约一个滚动条像素和小幅边框测量抖动，不用容差逐步下调确认边界。回退帧不授权下一次滚轮；随后两张有效前进帧仍可确认。

- 基线：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_scroll.ScrollReplay.test_scroll_receipt_rejects_rollback_after_a_confirmed_partial_step`；分别在滚动条回退和同一行下移时错误返回回执，输出 `a rollback must not authorize another wheel step`、`Ran 1 test in 0.350s`、`FAILED (failures=2)`，退出 1。
- 修复后：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_scroll.ScrollReplay.test_scroll_receipt_rejects_rollback_after_a_confirmed_partial_step tests.contracts.test_scan_scroll.ScrollReplay.test_measured_short_page_and_real_traversal_skip_only_overlap tests.contracts.test_scan_scroll.ScrollReplay.test_repeated_or_missing_rows_do_not_acknowledge_displacement tests.contracts.test_scan_scroll.ScrollReplay.test_stationary_receipt_allows_retry_without_advancing_page tests.contracts.test_scan_scroll.ScrollReplay.test_production_scroll_dispatch_holds_after_ambiguous_frame`；合成帧覆盖中间回退、测量抖动、继续前进、静止及重叠歧义，输出 `Ran 5 tests in 0.569s`、`OK`，退出 0。复用上一轮宿主/OCR 兼容证据，不重跑三版；此处 3.0.6 解释器仅用于加载离线测试所需宿主模块。
- Ruff 定向检查 `All checks passed!`、`2 files already formatted`；结构检查 `"files": 343`、`"errors": []`、`"status": "passed"`（退出 0）。未启动游戏或触发 Windows 输入，后台扫描继续关闭。

#### 离线补充：下一次滚轮使用当前卡片间隙

同一次换页中的每格滚轮都从最新已确认的网格选择落点。以两张相邻卡片的实测间隙为候选，可用同行横向间隙或同列纵向间隙；落点必须位于画面内并与每张卡片边界留出余量。若无合适间隙，暂停并等待重新观察，不使用按屏幕比例猜出的坐标。滚动回执和原生鼠标守卫不变。

- 基线命令：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_scroll.ScrollReplay.test_scroll_dispatch_recomputes_hover_after_partial_step tests.contracts.test_scan_scroll.ScrollReplay.test_scroll_dispatch_pauses_when_no_measured_gap`。移动后两次滚轮仍使用 `(282, 286)`，且无间隙时仍发出一次滚轮；`Ran 2 tests in 0.178s`、`FAILED (failures=2)`，退出 1。
- 修复后命令：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_scroll.ScrollReplay.test_scroll_dispatch_recomputes_hover_after_partial_step tests.contracts.test_scan_scroll.ScrollReplay.test_scroll_dispatch_pauses_when_no_measured_gap tests.contracts.test_scan_scroll.ScrollReplay.test_wheel_hover_can_use_a_measured_vertical_gap tests.contracts.test_scan_scroll.ScrollReplay.test_production_scroll_dispatch_holds_after_ambiguous_frame tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr`。回放验证两次落点各处于对应画面的实测间隙，纵向间隙可用、过窄时拒绝、无间隙时发出 0 次滚轮；同时核对未确认位移不会发下一次滚轮、扫描取消正常。输出 `Ran 5 tests in 3.363s`、`OK`，退出 0。
- 本次只改坐标选择和扫描内部状态，宿主捕获与输入接口、OCR 均未改，复用已有三版兼容结果。Ruff 输出 `All checks passed!`、`4 files already formatted`；结构检查 `"files": 343`、`"errors": []`、`"status": "passed"`（退出 0）。未启动游戏或调用真实输入，后台扫描保持关闭。

#### 离线补充：扫描只使用经校验的自有快照

核对源码、任务入口和定向测试后，删除没有调用方的旧三帧采样门与旧证明分支。扫描仅保留当前的「点击回执后两张稳定详情帧＋等级来源」证明，不变更持久化格式。冻结时先复制宿主像素，再校验实际交给 OCR 和截图编码的副本；校验通过才标为只读。采集后的宿主缓冲变化不影响该副本。

- 基线：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_replay.ScanDetailProofContract.test_snapshot_checks_the_owned_copy_not_the_borrowed_frame`。模拟在复制时宿主复用缓冲，将副词条从 13.8% 改为 15.0%；旧路径接受了不同的副本。输出 `ValueError not raised`、`Ran 1 test in 0.043s`、`FAILED (failures=1)`，退出 1。
- 修复后：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_replay.ScanDetailProofContract tests.contracts.test_capture_replay.FrameReplayContract.test_manual_cancellation_and_scan_detail_replay tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr tests.contracts.test_scan_grade_ocr.ScanGradeOcrContract.test_cutoff_grade_requires_stable_selected_detail_and_rejects_late_result`。拒绝复制时变化的画面；正常冻结后，再修改原缓冲不改变只读副本。覆盖缺失/跨批次等级来源、双帧一致性、低等级复核、同帧裁剪回退和扫描取消。输出 `Ran 5 tests in 6.035s`、`OK`，退出 0。
- 本轮仅改进程内证明与像素交接，宿主接口和 OCR 未变，复用已有三版兼容结果。Ruff 输出 `All checks passed!`、`3 files already formatted`；结构检查 `"files": 343`、`"errors": []`、`"status": "passed"`（退出 0）。未启动游戏或调用真实输入，后台扫描保持关闭。

#### 离线补充：扫描只保留一套行位遍历

删除旧的金边定位、分段暂停、单帧重叠和未进入生产状态的网格探测分支。任务启动即使用实测网格，不再另行切换遍历模式；普通帧只能确认当前已测页，不能改变全局行偏移。滚动阶段只有属于当前会话、原页和页面上下文的双帧回执才能换页。继续扫描仍由适配层先确认新画面，不会把未核验的新网格传入遍历。核对源码、插件入口和测试后，旧入口引用为零；没有涉及持久化档案。

- 基线：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_scroll.ScrollReplay.test_measured_short_page_and_real_traversal_skip_only_overlap`。以普通启动路径输入一张未接受回执的新页，旧实现直接返回选格动作，`AssertionError: 'scroll' != 'click'`、`Ran 1 test in 0.167s`、`FAILED (failures=1)`，退出 1。
- 修复后：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_scroll tests.contracts.test_scan_grade_ocr.ScanGradeOcrContract.test_cutoff_grade_requires_stable_selected_detail_and_rejects_late_result tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr`。未提交回执、过期会话或不同页面上下文不推进；有效回执只跳过已经处理的重叠格，继续扫描第一个新格。定向回放覆盖排序、暂停、滚动、低等级边界和取消；`Ran 13 tests in 3.604s`、`OK`，退出 0。
- `TRAVERSAL_LINES=707->343`、`LEGACY_SCAN_PATH_REFERENCES=0`。Ruff 输出 `All checks passed!`、`10 files already formatted`；结构检查 `"files": 342`、`"errors": []`、`"status": "passed"`（退出 0）。没有改变宿主或 OCR 接口，复用已有兼容证据；未启动游戏或调用真实输入，后台扫描保持关闭。

#### 离线补充：前台探测和清理绑定扫描会话

前台/捕获就绪探测可能晚于用户停止并重新排队。现在探测后再次核对扫描代次、捕获会话和页面阶段；过期探测不再授权输入或提交。扫描任务发出的停止、暂停、恢复及排空也绑定自身代次，防止旧回调操作新会话。用户主动停止仍面向当前会话。

- 基线：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_device.CaptureDeviceContract.test_late_foreground_probe_does_not_authorize_a_replaced_scan`。在前台探测返回前停止旧扫描并排队新扫描，旧守卫仍返回 True；`old scan accepted a late foreground result`、`Ran 1 test in 0.007s`、`FAILED (failures=1)`，退出 1。
- 修复后：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_device.CaptureDeviceContract.test_late_foreground_probe_does_not_authorize_a_replaced_scan tests.contracts.test_capture_device.CaptureDeviceContract.test_production_click_and_scroll_never_reach_native_input_when_blocked tests.contracts.test_scan_scroll.ScrollReplay.test_running_short_page_remeasures_replaced_or_added_cards tests.contracts.test_scan_scroll.ScrollReplay.test_resume_waits_for_new_frame_and_rejects_changed_list tests.contracts.test_scan_scroll.ScrollReplay.test_production_scroll_dispatch_holds_after_ambiguous_frame tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr`。迟到探测被拒绝，旧停止/暂停不改变新扫描或已开启的手动收录；定向用例同时验证滚动、短页、恢复和原生输入门禁，整个扫描取消仍只保留已提交的一件。输出 `Ran 6 tests in 3.239s`、`OK`，退出 0。
- 只改内部会话判定，复用宿主/OCR 兼容证据。Ruff 输出 `All checks passed!`、`7 files already formatted`；结构检查 `"files": 342`、`"errors": []`、`"status": "passed"`（退出 0）。未启动游戏或使用真实输入，后台扫描保持关闭。

#### 离线补充：实时评分只发布仍属于空闲收录状态的帧

实时评分在取帧前记录当时的收录状态。OCR 返回、对比参考件读完及浮层发布前，检查运行时、捕获源、权重、设置和收录状态仍属于该帧；一次收录即使在 OCR 期间开始又结束，也会使旧帧失效。被丢弃的帧不更新工作台快照、角色缓存、策略文本或浮层；此前已验证的最近快照仍保留。评分算法与 OCR 读法不变。

- 基线：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_replay.FrameReplayContract.test_live_result_is_not_published_after_intake_takes_ownership`。在 OCR 时开始收录、开始后又立即结束，以及策略处理时开始收录，旧实现均发布了 `current_score=17.02`；`Ran 1 test in 1.907s`、`FAILED (failures=3)`，退出 1。
- 修复后：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_replay.FrameReplayContract.test_live_result_is_not_published_after_intake_takes_ownership tests.contracts.test_capture_replay.FrameReplayContract.test_real_ocr_and_native_score_for_four_scenes`。三种迟到结果均不更新评分快照或 HUD；原有四种场景通过，输出 `CAPTURE_LIVE_REPLAY_OK 3.0.6: [('display', 17.02), ('bag', 17.02), ('comparison', 17.73), ('enhancement', 17.02)]`、`Ran 2 tests in 2.563s`、`OK`，退出 0。
- 只改发布时序，复用宿主/OCR 兼容证据。Ruff 输出 `All checks passed!`、`2 files already formatted`；结构检查 `"files": 342`、`"errors": []`、`"status": "passed"`（退出 0）。未启动游戏或调用真实输入，后台扫描继续关闭。


#### 离线补充：结束报告绑定原扫描会话

扫描结束时固定该轮的等级门槛、计数和失败项。旧任务排空后仍记录自身的取消结果，不读取新任务计数；如果已有新扫描排队，旧任务既不会发布报告，也不会在临时文件刷盘后覆盖报告。报告 JSON 字段和版本不变。

- 基线：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_report`。在结束前排队或在报告刷盘时排队，旧任务均覆盖了已有报告；`Ran 1 test in 0.059s`、`FAILED (failures=2)`，退出 1。
- 修复后：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_report tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr`。合成状态验证旧任务保留 2 格处理、1 件收录、1 格失败的独立计数，两种排队时机均不覆盖报告；脱敏帧回放输出 `CAPTURE_SCAN_CHAIN_OK 3.0.6: committed=1, cancelled_ocr=1, png=1`、`Ran 2 tests in 2.310s`、`OK`，退出 0。
- Ruff 输出 `All checks passed!`、`4 files already formatted`；结构检查 `"files": 343`、`"errors": []`、`"status": "passed"`，退出 0。未改宿主或 OCR 接口，复用既有三版兼容结果。未启动游戏或发送真实输入，后台扫描保持关闭。


#### 离线补充：取消的手动截图不占用重试名额

旧 OCR 仍在收尾时，新手动收录可冻结一张截图等待。若用户再取消并重试，已取消的等待截图让出唯一等待位；仍有效的等待截图不被替换。正在执行的 OCR 仍自然返回，取消后的结果不进入库存。

- 基线：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_private_ocr.PrivateOcrContract.test_manual_worker_replaces_cancelled_waiting_snapshot`。第一次解析被合成阻塞，第二次请求入队后取消；第三次请求被错误拒绝，输出 `False is not true : cancelled snapshot occupied the slot`、`Ran 1 test in 0.801s`、`FAILED (failures=1)`，退出 1。
- 修复后：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_private_ocr.PrivateOcrContract.test_manual_worker_replaces_cancelled_waiting_snapshot tests.contracts.test_private_ocr.PrivateOcrContract.test_overlay_unload_releases_waiting_manual_snapshot_during_inflight_ocr tests.contracts.test_capture_replay.FrameReplayContract.test_manual_cancellation_and_scan_detail_replay`。只有最初在途项与最后有效项进入解析，等待位最多一项且任务计数归零；卸载和脱敏帧回放分别输出 `CAPTURE_MANUAL_UNLOAD_OK 3.0.6: drained=1, late_results=0`、`CAPTURE_LATE_GUARD_OK 3.0.6: saved=1, pending=0, png=1`，合计 `Ran 3 tests in 3.517s`、`OK`，退出 0。
- Ruff 输出 `All checks passed!`、`2 files already formatted`；结构检查 `"files": 343`、`"errors": []`、`"status": "passed"`，退出 0。宿主和 OCR 接口不变，复用原有兼容结果。未启动游戏或发送真实输入，后台扫描仍关闭。


#### 离线补充：逐格扫描同时核对滚动条位置

普通长列表帧沿用已确认的网格和缩略图签名，但也复用网格测量中的滚动滑块检测。不重新拟合卡片：只读滚动条窄带。即使多行缩略图完全一样，滑块明确移位也会停止扫描并保留已收录项；原先可见的滑块暂时读不到时，不按旧坐标继续，恢复后可重新取帧。保留小幅测量容差；没有滚动条的单页、短列表重新实测和主动换页的双帧回执仍走原有路径。

- 基线：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_scroll.ScrollReplay.test_running_full_page_checks_scrollbar_even_when_thumbnails_match`。滑块移位或消失都仍返回可用于继续扫描的帧；`Ran 1 test in 0.149s`、`FAILED (failures=2)`，退出 1。
- 修复后：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_scroll tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr`。重复缩略图加滑块移位时停止；缺失时等待并接受恢复后的原位置；1 像素抖动不影响扫描。完整回放输出 `CAPTURE_SCAN_CHAIN_OK 3.0.6: committed=1, cancelled_ocr=1, png=1`、`Ran 13 tests in 4.016s`、`OK`。已保存完整 unittest 结果，执行会话已失效，未取得外层 shell 退出码。
- Ruff 输出 `All checks passed!`、`3 files already formatted`；结构检查 `"files": 343`、`"errors": []`、`"status": "passed"`，退出 0。未改宿主或 OCR 接口，复用原有兼容证据。未启动游戏或发送真实输入，后台扫描仍关闭。


#### 离线补充：低等级补读先核对实际副本

触发低等级补读的两张帧稳定，不代表宿主缓冲在复制期间保持不变。现在先确认实际复制的详情与稳定证明一致，再从该副本裁出只读等级头部。副本不匹配时不向 OCR 入队，也不使用旧的稳定证明；重新取得两张新鲜稳定帧后才重试。保留原来的等级阈值、裁剪坐标和迟到结果复核。

- 基线：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_grade_ocr.ScanGradeOcrContract.test_cutoff_grade_discards_snapshot_changed_during_capture_copy`。合成宿主仅在复制时改变等级像素、随后还原；旧实现将变更的像素送入补读，输出 `0 != 1 : changed crop reached OCR`、`Ran 1 test in 0.143s`、`FAILED (failures=1)`，退出 1。
- 修复后：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_grade_ocr.ScanGradeOcrContract.test_cutoff_grade_discards_snapshot_changed_during_capture_copy tests.contracts.test_scan_grade_ocr.ScanGradeOcrContract.test_cutoff_grade_requires_stable_selected_detail_and_rejects_late_result tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr`。坏副本提交 0 次；重新稳定后仅把包含原等级像素的只读裁剪块送去补读。完整扫描取消回放输出 `CAPTURE_SCAN_CHAIN_OK 3.0.6: committed=1, cancelled_ocr=1, png=1`，合计 `Ran 3 tests in 2.719s`、`OK`，退出 0。
- Ruff 输出 `All checks passed!`、`3 files already formatted`；结构检查 `"files": 343`、`"errors": []`、`"status": "passed"`，退出 0。未改宿主、OCR 接口或输入许可，复用先前的新旧宿主兼容证据。仅离线回放，未启动游戏或发送真实输入，后台扫描继续关闭。


#### 离线补充：扫描停止通知准备阶段的识别和评分

准备阶段的详情解析现在接收原扫描会话的取消信号，不必等整个工坊关闭。停止或重新排队后，已开始的原生 OCR 可以正常返回，但不会进入下一 OCR 区域；已发起的评分走既有的协作取消，旧任务不干扰新任务。后台解析和真实输入许可不变。

- 基线：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_scroll.ScrollReplay.test_stop_cancels_preflight_between_ocr_regions_and_inside_compute`。在首个 OCR 区域、评分检查点分别停止，并分别有/无替换任务；四种情况仍允许继续，输出 `cancelled preflight entered another OCR region`、`cancelled preflight still permitted native compute`、`Ran 1 test in 0.119s`、`FAILED (failures=4)`，退出 1。
- 修复后：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_scroll.ScrollReplay.test_stop_cancels_preflight_between_ocr_regions_and_inside_compute tests.contracts.test_scan_scroll.ScrollReplay.test_sort_preflight_waits_for_new_grid_before_any_item_click tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr`。四种情况均触发正常取消，替换任务仍在准备；排序确认和完整取消回放通过，输出 `CAPTURE_SCAN_CHAIN_OK 3.0.6: committed=1, cancelled_ocr=1, png=1`、`Ran 3 tests in 2.247s`、`OK`，退出 0。
- Ruff 输出 `All checks passed!`、`2 files already formatted`；结构检查 `"files": 343`、`"errors": []`、`"status": "passed"`，退出 0。只改进程内取消条件，复用已有新旧宿主兼容验证。未启动游戏或发送真实输入，后台扫描保持关闭。


#### 离线补充：手动收录先冻结像素再确定画面边界

手动收录的有效画面范围现在从冻结副本测量。后续样本的黑边范围、完整帧尺寸都必须与首个样本一致；复制后是黑帧或尺寸变化时，在送入 OCR 前结束该次采样。合法黑边及缩放仍按原始像素裁剪，不缩放证据 PNG。

- 基线：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_device.CaptureDeviceContract.test_manual_geometry_is_measured_from_the_owned_sample`。宿主复制时返回新增黑边、全黑或只扩大黑边外的截图尺寸，旧采样三种都接受；输出 `ValueError not raised`、`Ran 1 test in 0.018s`、`FAILED (failures=3)`，退出 1。
- 修复后：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_device.CaptureDeviceContract.test_manual_geometry_is_measured_from_the_owned_sample tests.contracts.test_capture_replay.FrameReplayContract.test_scaled_letterbox_has_original_pixel_evidence tests.contracts.test_capture_replay.FrameReplayContract.test_manual_cancellation_and_scan_detail_replay`。三种坏副本未入采样，正常副本只读且独立；原像素回放输出 `CAPTURE_SCALED_EVIDENCE_OK 3.0.6: viewport=(80,32,1280,720), crop=(931, 90, 423, 612)`、`CAPTURE_LATE_GUARD_OK 3.0.6: saved=1, pending=0, png=1`、`Ran 3 tests in 3.543s`、`OK`，退出 0。
- Ruff 输出 `All checks passed!`、`2 files already formatted`；结构检查 `"files": 343`、`"errors": []`、`"status": "passed"`，退出 0。本轮只改宿主无关的快照校验，复用已有的新旧宿主兼容证据；未启动游戏或发送真实输入，后台扫描保持关闭。


#### 离线补充：收录接管前清除旧评分浮层

手动收录在采样前清除实时评分的绘制内容、Qt HUD 状态和缓存；自动扫描在等待切回背包前走同一清理路径，不要求存在原生浮层。最近一次已验证的评分记录仍可在工作台查看；收录结束后由新帧恢复实时显示。

- 基线（3.0.6）：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_device.CaptureDeviceContract.test_manual_intake_hides_live_scores_before_sampling tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr`。有/无原生浮层的手动采样和自动扫描均看到旧 HUD 状态；`old score survived into intake`、`old score survived into scan`、`Ran 2 tests in 0.186s`、`FAILED (failures=3)`，退出 1。
- 修复后串行离屏命令：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_device.CaptureDeviceContract.test_manual_intake_hides_live_scores_before_sampling tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr tests.contracts.test_capture_replay.FrameReplayContract.test_real_ocr_and_native_score_for_four_scenes tests.contracts.test_scan_report`。三版均在取样前清掉旧 HUD，四场景原有评分和取消报告不变；2.0.8 使用已有 wheel 导入目录。

| ok-script | 字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 | `Ran 4 tests in 3.463s`；`OK` | 0 |
| 2.0.17 | `Ran 4 tests in 3.350s`；`OK` | 0 |
| 2.0.8 | `Ran 4 tests in 2.663s`；`OK` | 0 |

Ruff 输出 `All checks passed!`、`5 files already formatted`；结构检查 `"files": 343`、`"errors": []`、`"status": "passed"`，退出 0。验证的是 HUD 状态和原生绘制调用，不代替实际窗口绘制/捕获验收。未启动游戏或发送真实输入，后台扫描仍关闭。


#### 离线补充：库存事务失败时撤回新截图

手动收录与自动扫描共用写入边界：在已有截图目录锁内发布并校验 PNG，再写入库存或待确认区。库存事务抛错时，只删除本次新建的 PNG；已存在的同内容文件和历史证据保留，收录计数不变。重试成功后仍按原来的库存／待确认规则保存。此检查针对正常异常回退，不宣称覆盖进程在两次写入间被终止的情形。

- 基线：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_persistence`。在临时 SQLite 事务最后一步注入 `OperationalError`，新件和疑似重复件的数据库都回滚，但各留下 1 张本次截图；`Ran 1 test in 0.563s`、`FAILED (failures=2)`，退出 1。
- 修复后：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_persistence tests.contracts.test_capture_replay.FrameReplayContract.test_manual_cancellation_and_scan_detail_replay tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr`。新件、待确认件和目标文件预先存在三种情况通过，核对数据库版本、收录计数、PNG 及可重试行为；脱敏回放分别输出 `CAPTURE_LATE_GUARD_OK 3.0.6: saved=1, pending=0, png=1`、`CAPTURE_SCAN_CHAIN_OK 3.0.6: committed=1, cancelled_ocr=1, png=1`。合计 `Ran 3 tests in 4.420s`、`OK`，退出 0。
- Ruff 输出 `All checks passed!`、`3 files already formatted`；结构检查 `"files": 344`、`"errors": []`、`"status": "passed"`，退出 0。未改变宿主或 OCR 接口，不重复三版宿主验证。仅使用临时合成档案，未启动游戏或发送真实输入，后台扫描仍关闭。


#### 离线补充：前台发出点击和滚轮前复核鼠标

点击和滚轮共用最终检查：通过原有前台与会话守卫后，重新确认用户未按住鼠标、光标仍在目标位置的容差内。确认失败时不发出点击或滚轮，并按该扫描会话暂停；已发出的按下仍由原来的 finally 释放。没有改变坐标映射、宿主鼠标后端或捕获许可。

- 基线：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_device.CaptureDeviceContract.test_scan_rechecks_button_and_hover_before_foreground_input`。初次位置检查后光标偏移时，旧路径仍点击或滚动；此时用户按下鼠标，旧路径仍滚动。输出 `stale hover authorized native input`、`Ran 1 test in 0.004s`、`FAILED (failures=3)`，退出 1。
- 修复后：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_device.CaptureDeviceContract.test_scan_rechecks_button_and_hover_before_foreground_input tests.contracts.test_capture_device.CaptureDeviceContract.test_production_click_and_scroll_never_reach_native_input_when_blocked tests.contracts.test_capture_device.CaptureDeviceContract.test_late_foreground_probe_does_not_authorize_a_replaced_scan tests.contracts.test_scan_scroll.ScrollReplay.test_production_scroll_dispatch_holds_after_ambiguous_frame`。模拟鼠标确认异常时不发输入、稳定时各发一次，并保留焦点、切换会话和滚动回执门禁；`Ran 4 tests in 0.160s`、`OK`，退出 0。
- Ruff 输出 `All checks passed!`、`2 files already formatted`；结构检查 `"files": 344`、`"errors": []`、`"status": "passed"`，退出 0。只改本地门禁判定，宿主兼容沿用此前证据。未启动游戏或发送真实输入，后台扫描继续关闭。


#### 离线补充：手动取帧和入队前复核原请求

手动采样的前台和捕获探测可能晚于用户停止、重新请求或切换捕获源。返回后重新核对该请求与捕获源，在发出取帧调用前拒绝过期探测。最后一帧复制完成后再核对一次，确认通过才标为已冻结并送解析队列；冻结后的普通失焦仍不影响有效截图。

- 基线：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_device.CaptureDeviceContract.test_manual_sampling_rejects_replaced_context_before_read_or_enqueue`。前台探测期间替换请求、连接探测期间换源时均额外取帧一次；复制最后一帧时换源仍入队。输出 `Ran 1 test in 0.025s`、`FAILED (failures=3)`，退出 1。
- 修复后：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_device.CaptureDeviceContract.test_manual_sampling_rejects_replaced_context_before_read_or_enqueue tests.contracts.test_capture_replay.FrameReplayContract.test_manual_cancellation_and_scan_detail_replay tests.contracts.test_private_ocr.PrivateOcrContract.test_manual_worker_replaces_cancelled_waiting_snapshot`。前两种情况取帧 0 次，最后一种已取得两帧但入队 0 次，新请求保持有效；回放仍输出 `CAPTURE_LATE_GUARD_OK 3.0.6: saved=1, pending=0, png=1`。合计 `Ran 3 tests in 2.679s`、`OK`，退出 0。
- Ruff 输出 `All checks passed!`、`2 files already formatted`；结构检查 `"files": 344`、`"errors": []`、`"status": "passed"`，退出 0。宿主和 OCR 接口不变，复用已有兼容证据。仅离线回放，未启动游戏或发送真实输入，后台扫描保持关闭。


#### 离线补充：实时评分停止处理已过期的帧

实时评分的发布条件也用于 OCR 区域与原生评分的协作取消：收录接管、截图来源或权重已变化时，不读取下一 OCR 区域，也不继续等待旧评分。已进入原生 OCR 的调用可自然返回；丢弃结果后清除对应浮层状态，但保留工作台最近一次有效评分。

- 基线：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_device.CaptureDeviceContract.test_live_score_cancels_obsolete_ocr_and_compute`。在首个区域开始收录旧帧仍进入下一区域；评分期间换权重旧请求未收到取消。输出 `obsolete frame entered another OCR region`、`old weights still permitted native compute`、`Ran 1 test in 0.208s`、`FAILED (failures=2)`，退出 1。
- 修复后：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_device.CaptureDeviceContract.test_live_score_cancels_obsolete_ocr_and_compute tests.contracts.test_capture_replay.FrameReplayContract.test_live_result_is_not_published_after_intake_takes_ownership tests.contracts.test_capture_replay.FrameReplayContract.test_real_ocr_and_native_score_for_four_scenes`。过期结果不发布，Qt HUD 状态被清除；四种评分场景保持 `CAPTURE_LIVE_REPLAY_OK 3.0.6: [('display', 17.02), ('bag', 17.02), ('comparison', 17.73), ('enhancement', 17.02)]`，合计 `Ran 3 tests in 2.575s`、`OK`，退出 0。
- Ruff 输出 `All checks passed!`、`2 files already formatted`；结构检查 `"files": 344`、`"errors": []`、`"status": "passed"`，退出 0。此次只改协作取消条件，未改变宿主或 OCR 接口。未启动游戏或发送真实输入，后台扫描继续关闭。


#### 离线补充：扫描排队和卸载绑定同一次请求

排队时固定运行时、扫描代次和捕获身份。任务只启动该项请求，过期的宿主回调不接管后来排队的扫描。排队失败或任务卸载只撤回自己那一轮；未启动的任务同样能释放准备中的收录状态，旧任务不取消后来进入的扫描或手动收录。

- 基线（3.0.6）：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_report.ScanReportContract.test_unload_cancels_only_the_scan_owned_by_that_task`。卸载未执行的任务后仍为 `preparing`；清理旧扫描也误停了新扫描和手动收录。输出 `Ran 1 test in 0.005s`、`FAILED (failures=2, errors=1)`，退出 1。
- 修复后串行离屏命令：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_report tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr tests.contracts.test_host_loading`。验证排队和执行中卸载、旧回调晚到、替换会话保留、结束报告、脱敏扫描及三任务宿主加载；2.0.8 使用已有 wheel 导入目录。

| ok-script | 字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 | `Ran 6 tests in 5.030s`；`OK` | 0 |
| 2.0.17 | `Ran 6 tests in 5.948s`；`OK` | 0 |
| 2.0.8 | `Ran 6 tests in 4.196s`；`OK` | 0 |

三版均输出 `committed=1, cancelled_ocr=1, png=1` 和 `HOST_WORKBENCH_OK: manifest, three tasks, shared Qt pages and runtime shutdown`。Ruff 输出 `All checks passed!`、`4 files already formatted`；结构检查 `"files": 344`、`"errors": []`、`"status": "passed"`，退出 0。未启动游戏或发送真实输入，后台扫描保持关闭。


#### 离线补充：手动对比页始终收录右栏

未明确识别为背包的冻结画面先确认双栏布局。确认后，词条、名称、等级、元数据复读和截图都使用同一右栏边界；同帧裁剪回退不再因看不到左侧标题而丢失双栏身份。自动扫描仍使用原来的背包裁剪。

- 基线（3.0.6）：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_device.CaptureDeviceContract.test_manual_comparison_keeps_the_right_panel_through_same_frame_retry`。左侧与右侧提供不同名称、等级和副词条，两种路径均未确认双栏，输出 `False is not true`、`Ran 1 test in 0.041s`、`FAILED (failures=2)`，退出 1。
- 修复后串行离屏命令：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_device.CaptureDeviceContract.test_manual_comparison_keeps_the_right_panel_through_same_frame_retry tests.contracts.test_capture_replay.FrameReplayContract.test_real_ocr_and_native_score_for_four_scenes tests.contracts.test_capture_replay.FrameReplayContract.test_manual_cancellation_and_scan_detail_replay tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr`。双栏坐标夹具确认只保留右侧名称、等级 +5、COST 4 和 15.0% 副词条；PNG 逐像素等于右栏原图裁剪，初读及强制回退均通过。原有四场景评分、收录取消和扫描链路通过；2.0.8 使用已有 wheel 导入目录。

| ok-script | 字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 | `Ran 4 tests in 5.681s`；`OK` | 0 |
| 2.0.17 | `Ran 4 tests in 4.296s`；`OK` | 0 |
| 2.0.8 | `Ran 4 tests in 4.108s`；`OK` | 0 |

Ruff 输出 `All checks passed!`、`7 files already formatted`；结构检查 `"files": 344`、`"errors": []`、`"status": "passed"`，退出 0。仅离线验证，未启动游戏或发送真实输入，后台扫描保持关闭。


#### 离线补充：扫描换黑边不沿用旧网格

逐格读取、等待滚动回执和暂停恢复都核对实际内容边界。即使帧的宽高不变，只要有效区域改变，就结束该会话并保留已收录项。纯黑瞬时帧只等待，不冒充分辨率变化。无黑边时先验证四条外边均有有效像素，避免为正常帧做两次全图归约；有黑边时仍按原规则计算。

- 基线：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_scroll.ScrollReplay.test_locked_grid_rejects_new_content_bounds_at_same_capture_size`。在不改变其余画面的 1600×900 帧左侧加入 8 像素黑边；逐格与滚动时旧网格仍可用，恢复时仍停在暂停态。输出 `Ran 1 test in 0.296s`、`FAILED (failures=3)`，退出 1。
- 修复后：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_scroll tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr tests.contracts.test_capture_replay.FrameReplayContract.test_scaled_letterbox_has_original_pixel_evidence tests.contracts.test_capture_device.CaptureDeviceContract.test_manual_geometry_is_measured_from_the_owned_sample`。三种扫描阶段均不接受新边界，瞬时黑帧后恢复原范围可继续取帧；`CAPTURE_SCAN_CHAIN_OK 3.0.6: committed=1, cancelled_ocr=1, png=1`、`CAPTURE_SCALED_EVIDENCE_OK 3.0.6: viewport=(80,32,1280,720), crop=(931, 90, 423, 612)`、`Ran 17 tests in 6.418s`、`OK`，退出 0。
- Ruff 输出 `All checks passed!`、`3 files already formatted`；结构检查 `"files": 344`、`"errors": []`、`"status": "passed"`，退出 0。未改宿主或 OCR 接口，仅使用离线回放；未启动游戏或发送真实输入，后台扫描仍关闭。


#### 离线补充：核对宿主执行器实际取帧的捕获器

本地宿主源码显示，2.0.8 的 `TaskExecutor.method` 读取其设备管理器，2.0.17 和 3.0.6 则还可使用作业临时捕获器。取帧前后同时核对设备管理器、窗口绑定和执行器实际捕获器；即便返回有效 BGR 且帧号更新，来自不同捕获器的像素也不进入识别。连接状态不把不同捕获器视为已就绪；实时评分的直接被动取帧仍走独立的同源校验。

- 基线（3.0.6）：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_device.CaptureDeviceContract.test_capture_waits_for_native_capture_binding_to_follow_host_window`。执行器管理器不同、捕获器被覆盖、取帧时替换，三种情况均错误返回帧；`executor pixels bypassed source binding`、`Ran 1 test in 0.002s`、`FAILED (failures=3)`，退出 1。
- 修复后串行离屏命令：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_device.CaptureDeviceContract.test_capture_waits_for_native_capture_binding_to_follow_host_window tests.contracts.test_capture_device.CaptureDeviceContract.test_capture_rejects_format_and_same_handle_binding_changes tests.contracts.test_capture_device.CaptureDeviceContract.test_fresh_samples_and_native_foreground_capability tests.contracts.test_capture_device.CaptureDeviceContract.test_host_tonemapped_hdr_pixels_are_read_only_and_not_automatic tests.contracts.test_capture_replay.FrameReplayContract.test_manual_cancellation_and_scan_detail_replay tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr`。取帧前不匹配时宿主读取 0 次，取帧中切换只丢弃返回像素；正常冻结和取消回放保持不变。

| ok-script | 字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 | `Ran 6 tests in 3.958s`；`OK` | 0 |
| 2.0.17 | `Ran 6 tests in 2.803s`；`OK` | 0 |
| 2.0.8 | `Ran 6 tests in 2.971s`；`OK (skipped=1)`（没有 WGC HDR 捕获类） | 0 |

Ruff 输出 `All checks passed!`、`5 files already formatted`；结构检查 `"files": 344`、`"errors": []`、`"status": "passed"`，退出 0。未启动游戏或发送真实输入，后台扫描继续关闭。


#### 离线补充：断流后实时评分浮层自动到期

宿主拿不到新帧时可能跳过触发任务，该轮评分便没有机会显式隐藏浮层。Qt HUD 状态和 GDI 自定义绘制共用 6 秒有效期，高于正常的 2 秒全帧刷新间隔。无有效刷新时，Qt 轮询得到隐藏状态，GDI 交给宿主原有的绘制到期机制；新评分刷新期限。工作台的最近一次有效评分记录不受影响。

- 基线（3.0.6）：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_hud_state.HudStateContract.test_live_snapshot_expires_without_another_host_frame_and_recovers tests.contracts.test_capture_replay.FrameReplayContract.test_real_ocr_and_native_score_for_four_scenes`。模拟 6 秒无帧后 Qt 状态仍可见，GDI 绘制也没有 `duration`；`Ran 2 tests in 1.742s`、`FAILED (failures=2)`，退出 1。
- 修复后串行离屏命令：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_hud_state tests.contracts.test_capture_replay.FrameReplayContract.test_real_ocr_and_native_score_for_four_scenes tests.contracts.test_hud_window`。模拟时钟验证到期前仍可见、到期清除显示状态和新帧恢复；GDI 回放断言调用带 6 秒期限，四场景评分不变。另对三版实际 `Win32GdiOverlay.draw` 反射，均输出 `GDI_DRAW (self, key, callback, duration=None)`，退出 0。

| ok-script | 字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 | `Ran 7 tests in 2.145s`；`OK` | 0 |
| 2.0.17 | `Ran 7 tests in 1.783s`；`OK` | 0 |
| 2.0.8 | `Ran 7 tests in 1.486s`；`OK` | 0 |

Ruff 输出 `All checks passed!`、`4 files already formatted`；结构检查 `"files": 344`、`"errors": []`、`"status": "passed"`，退出 0。未验证实机原生绘制或断流，未启动游戏或发送真实输入，后台扫描保持关闭。


#### 离线补充：浮层交互期间也不显示过期分数

Qt 浮层展开详情或拖动时仍保持窗口打开。没有有效评分时把旧行替换为“等待新的评分画面…”，保留详情面板与拖动状态；解锁重建窗口也从当前有效状态读取，不复制旧窗口的行。新评分到达后替换等待提示，普通浮层隐藏时序不变。

- 基线：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_hud_window.HudWindowContract.test_expiry_keeps_hud_interaction_without_showing_old_score`。在过期状态展开详情、拖动或解锁仍看到 `实时评分 17.02`；`Ran 1 test in 0.125s`、`FAILED (failures=3)`，退出 1。
- 修复后离屏 100%：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_hud_window`；`Ran 5 tests in 0.180s`、`OK`，退出 0。125%（`QT_SCALE_FACTOR=1.25`）执行基线命令；`Ran 1 test in 0.110s`、`OK`，退出 0。固定模拟过期前后的界面文本、详情/拖动和解锁状态，确认新结果 `实时评分 17.73` 能恢复显示。
- Ruff 输出 `All checks passed!`、`2 files already formatted`；结构检查 `"files": 344`、`"errors": []`、`"status": "passed"`，退出 0。只改工坊 Qt 窗口状态，不改宿主接口，不将离屏缩放当作 Windows 实机验收。未启动游戏或发送真实输入，后台扫描保持关闭。


#### 离线补充：扫描暂停不会漏计已提交记录

暂停可能落在数据库提交成功与扫描计数更新之间。现在同一会话已取出的解析结果可以在暂停态完成记账；这不会开放新输入、解析提交或库存写入。已提交的位置从待处理集合中移除，恢复后不重复写，同一页处理完可正常结束。

- 基线：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_report.ScanReportContract.test_pause_keeps_accounting_for_an_already_committed_snapshot`。临时档案中新件及待确认件均已保存，但暂停后扫描计数仍为 0；`committed result was not counted`、`Ran 1 test in 0.284s`、`FAILED (failures=2)`，退出 1。
- 修复后：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_report tests.contracts.test_scan_scroll.ScrollReplay.test_resume_waits_for_new_frame_and_rejects_changed_list tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr`。两种记录都只提交、记账一次；重复回调不写入，恢复后待处理数为 0。输出 `CAPTURE_SCAN_CHAIN_OK 3.0.6: committed=1, cancelled_ocr=1, png=1`、`Ran 5 tests in 2.888s`、`OK`，退出 0。
- Ruff 输出 `All checks passed!`、`2 files already formatted`；结构检查 `"files": 344`、`"errors": []`、`"status": "passed"`，退出 0。未改宿主或 OCR 接口，复用已有兼容证据。未启动游戏或发送真实输入，后台扫描仍关闭。


#### 离线补充：停止与重排也保留原扫描的提交计数

库存提交直接返回“入库”或“待确认”，不再对照可能已经切换的状态计数。停止瞬间已经写入、尚未记账的结果可补到原扫描的报告，每个位置只接受一次；新扫描的计数和报告不受影响。取消后未写入的任务不计作已收录。

- 基线：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_report.ScanReportContract.test_control_transition_keeps_accounting_for_an_already_committed_snapshot`。暂停用例通过；两种库存结果各自在停止／立即重排时未计入旧报告，输出 `committed result was not counted`、`Ran 1 test in 0.895s`、`FAILED (failures=4)`，退出 1。
- 修复后：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_report tests.contracts.test_capture_persistence tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr tests.contracts.test_capture_replay.FrameReplayContract.test_manual_cancellation_and_scan_detail_replay`。已入库和待确认在三种时序下均只计一次，新会话从 0 开始；数据回退与脱敏回放不变。输出 `CAPTURE_SCAN_CHAIN_OK 3.0.6: committed=1, cancelled_ocr=1, png=1`、`CAPTURE_LATE_GUARD_OK 3.0.6: saved=1, pending=0, png=1`、`Ran 6 tests in 6.055s`、`OK`，退出 0。
- Ruff 输出 `All checks passed!`、`4 files already formatted`；结构检查 `"files": 344`、`"errors": []`、`"status": "passed"`，退出 0。未改宿主、OCR 或报告格式；未启动游戏或发送真实输入，后台扫描保持关闭。


#### 离线补充：扫描调度不让宿主抢前台

本地 2.0.8、2.0.17 和 3.0.6 的一次性任务调度会在调用插件前执行 `interaction.on_run()`，部分后端会主动激活游戏。扫描改由同一宿主线程的触发任务调度：仅用户明确点击后临时启用，游戏进入前台才可接手，运行内仍逐次校验前台和捕获身份。没有新截图、宿主暂停或未切回游戏时，由现有进度轮询在 20 秒后释放准备状态；结束、停止或卸载时撤销临时启用，不恢复上次扫描。未改宿主输入配置或释放后台扫描许可。

- 基线（3.0.6）：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_report.ScanReportContract.test_explicit_scan_uses_host_dispatch_without_forced_window_activation`。真实宿主调度方法选中一次性任务，对合成输入后端调用 `on_run` 1 次；`Ran 1 test in 0.129s`、`FAILED (failures=1)`，退出 1。
- 修复后串行离屏命令：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_scan_report tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr tests.contracts.test_host_loading`。真实宿主调度代码在模拟失焦时不派发，前台时按触发任务派发且 `on_run` 调用 0 次；模拟准入后宿主未能取帧并暂停，进度轮询仍会释放状态。三版均输出 `HOST_WORKBENCH_OK: manifest, three tasks, shared Qt pages and runtime shutdown`，脱敏扫描均保留 `committed=1, cancelled_ocr=1, png=1`。

| ok-script | 字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 | `Ran 8 tests in 5.474s`；`OK` | 0 |
| 2.0.17 | `Ran 8 tests in 5.803s`；`OK` | 0 |
| 2.0.8 | `Ran 8 tests in 5.336s`；`OK` | 0 |

Ruff 输出 `All checks passed!`、`4 files already formatted`；结构检查 `"files": 344`、`"errors": []`、`"status": "passed"`，退出 0。未启动游戏、切换真实前台或发送真实输入，后台扫描保持关闭。


#### 离线补充：快捷键只作用于按下时的收录会话

快捷键从读取线程排到 Qt 时，携带按下时的工作区、档案、捕获源和收录会话。换档案、换源、重新开启后忽略旧消息。收录还绑定按键时的等待状态：如果其间另一请求已经开始并结束，旧按键不会再补收一件。停止同一会话不受识别进度或扫描暂停影响；更换为新会话后，旧停止键不会停止新任务。

- 基线（3.0.6）：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_shortcut`。合成按键经延迟消息触发旧动作；换会话、档案、捕获源以及已完成另一请求共出现七次错误，输出 `stale shortcut reached intake`、`Ran 1 test in 0.009s`、`FAILED (failures=7)`，退出 1。
- 修复后串行离屏命令：`python -B -u -X faulthandler -m unittest -v tests.contracts.test_capture_shortcut tests.contracts.test_capture_replay.FrameReplayContract.test_manual_cancellation_and_scan_detail_replay tests.contracts.test_host_loading`。模拟正常及过期的收录／停止按键，覆盖迟到的捕获探测和暂停后同一／不同扫描；三版均通过工作台挂载／卸载与手动脱敏回放。

| ok-script | 字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 | `Ran 5 tests in 4.596s`；`OK` | 0 |
| 2.0.17 | `Ran 5 tests in 4.432s`；`OK` | 0 |
| 2.0.8 | `Ran 5 tests in 4.312s`；`OK` | 0 |

Ruff 输出 `All checks passed!`、`5 files already formatted`；结构检查 `"files": 345`、`"errors": []`、`"status": "passed"`，退出 0。夹具不读真实键盘、不启动游戏、不发送输入，后台扫描保持关闭。

#### 离线补充：派发后失焦或暂停时让回宿主线程

宿主选中扫描触发任务后，可能先取帧才进入 `run()`。如果这期间游戏切出或宿主暂停，扫描现在立即让回线程，不启动识别、诊断或输入。保留同一请求与原 20 秒截止时间：条件恢复时由宿主重新派发，否则由进度轮询撤销；进入扫描后仍由现有前台校验限制观察和输入。

定向命令：`python -B -u -X faulthandler -m unittest -v` 加下列用例；以本节既有的三个解释器配置、离屏环境、合成时钟和脱敏帧运行：

- `tests.contracts.test_scan_report.ScanReportContract.test_late_focus_loss_or_pause_yields_without_disarming_scan`
- `tests.contracts.test_scan_report.ScanReportContract.test_explicit_scan_uses_host_dispatch_without_forced_window_activation`
- `tests.contracts.test_scan_report.ScanReportContract.test_unload_cancels_only_the_scan_owned_by_that_task`
- `tests.contracts.test_scan_report.ScanReportContract.test_old_finalizer_uses_own_counts_and_cannot_replace_newer_report`
- `tests.contracts.test_capture_replay.FrameReplayContract.test_scan_task_cancel_with_committed_row_and_inflight_ocr`

3.0.6 基线只运行第一条，两种情形均出现 `Calls: [call(0.1)].`；`Ran 1 test in 0.013s`、`FAILED (failures=2)`，退出 1。修复后两种情形均不等待、保持原代次与截止时间；脱敏扫描链均输出 `committed=1, cancelled_ocr=1, png=1`。

| ok-script | 字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 | `Ran 5 tests in 2.598s`；`OK` | 0 |
| 2.0.17 | `Ran 5 tests in 1.942s`；`OK` | 0 |
| 2.0.8 | `Ran 5 tests in 2.091s`；`OK` | 0 |

Ruff 输出 `All checks passed!`、`2 files already formatted`；结构检查 `"files": 345`、`"errors": []`、`"status": "passed"`，退出 0。按本轮仅做离线验证的范围结束；未启动游戏、未发送真实输入、未安装插件，G5 保留未测，后台扫描保持关闭。

### 阶段 3：声骸库页面与用例迁移

声骸库先迁移档案切换、候选选择与扫描失败后返回库存的导航接线，以及单件／批量保护和删除。导航只走外壳注入的回调，不再从任务读取 `_panel`。保护和删除由库存用例冻结档案、UUID、动作、库存／配装修订及受影响记录；确认后提交同一预览，保留存储层的事务、历史和修订检查。未改计算、SQLite schema、宿主入口或界面布局。页面的库存读取、收录与核对、快速评分仍有旧的任务或运行时依赖，后续继续迁移。

离线验证用临时档案、同名不同 UUID、重复待判定记录及模拟的候选选择草稿。命令为 `python -B -u -X faulthandler -m unittest -v` 加下列用例；沿用此前三版 `ok-script` 的离屏环境。

- `tests.contracts.test_host_loading.HostLoadingContract.test_manifest_loads_and_opens_shared_workbench`：基线和三版迁移后均执行；迁移后额外验证工作台回到同一声骸库页面，以及候选选择草稿拒绝切换档案并保留原档案。
- `tests.calculation.test_matching.TestMatchInventorySelection`：3.0.6 基线及迁移后执行；用户取消恢复原状态，筛选外已选 UUID 保留，显式空集合不扩大范围。
- `tests.contracts.test_inventory_selection_edit`：3.0.6 迁移后执行；预览不写入，档案／库存／配装修订变化不提交，新预览可提交，局部保护不覆盖游戏侧字段。

| 环境 | 字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 原件副本 | `Ran 5 tests in 3.994s`；`OK` | 0 |
| 3.0.6 迁移后 | `Ran 7 tests in 2.668s`；`OK` | 0 |
| 2.0.17 迁移后 | `Ran 1 test in 7.495s`；`OK` | 0 |
| 2.0.8 迁移后 | `Ran 1 test in 2.843s`；`OK` | 0 |

宿主测试均输出 `HOST_WORKBENCH_OK: manifest, three tasks, shared Qt pages and runtime shutdown`；只证明挂载、导航、草稿守卫和卸载，源码夹具未部署随包引擎，后台查询出现既有的 `engine_missing`。Ruff 输出 `All checks passed!`、`6 files already formatted`；结构检查 `"files": 346`、`"errors": []`、`"status": "passed"`。未运行全量、安装插件或执行 G5 实机验收。

#### 声骸库：显式服务取代宿主任务透传

声骸库主页面、收录控制、核对编辑和快速评分只接收所需的应用服务、只读状态或导航回调。库存读取由工坊运行时创建并绑定取消，筛选仍由原有 Rust 查询处理。收录按钮通过 OKWW 适配层调用同一手动／扫描命令，快捷键、捕获条件和后台许可保持不变。核对仍以原生质量校验为准，取消撤销校验，迟到的保存信号只能作用于仍打开的同一草稿；截图通过后台证据服务读取，删除已无调用的独立弹窗和同步读图分支。快速评分只在明确动作后调用共享 `ScoreService`；修复了评分标签调用 Qt 控件构造参数时的离屏崩溃。未改库存 schema、备份格式、评分规则、候选排序或现有宿主入口。

- 基线与迁移后：`python -B -u -X faulthandler -m unittest -v tests.calculation.test_quick_quality tests.calculation.test_native_inventory_query.NativeInventoryQuery.test_reader_maps_snapshot_identity_counts_and_latest_request_only tests.contracts.test_data_compatibility.DataCompatibilityContract.test_current_inventory_schema_roundtrips_and_old_schemas_are_rejected`，比较冻结质量、29.96 分黄金值、库存最新代次及旧格式拒绝。原件基线 `Ran 5 tests in 0.836s`、`OK`，退出 0。
- 三版在各自的离屏环境执行 `python -B -u -X faulthandler -m unittest -v tests.contracts.test_host_loading.HostLoadingContract.test_manifest_loads_and_opens_shared_workbench`。夹具显式注入开发构建的 Rust 引擎，不使用发布安装路径；用临时库合成声骸和 PNG，验证真实后台库存读取、取消核对后不写入、明确评分后仍为 29.96、库存修订不变和原图后台读取。收录无快捷键或画面断开时未调用启动命令。各环境均输出 `HOST_INVENTORY_NATIVE_OK: review=1, cancelled_draft, quick_score=29.96, evidence=1` 与 `HOST_WORKBENCH_OK: manifest, three tasks, shared Qt pages and runtime shutdown`。

| ok-script | 字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6（含 5 项基线复测） | `Ran 6 tests in 4.534s`；`OK` | 0 |
| 2.0.17 | `Ran 1 test in 4.721s`；`OK` | 0 |
| 2.0.8 | `Ran 1 test in 2.838s`；`OK` | 0 |

Ruff 输出 `All checks passed!`、`19 files already formatted`；结构检查 `"files": 348`、`"errors": []`、`"status": "passed"`。声骸库迁移完成；匹配、配装、设置和资料入口另按阶段 3 顺序处理。没有启动游戏、安装插件或做全量验证；真实窗口／主题／DPI 与 G5 仍留待验收，后台扫描保持关闭。

#### 匹配：冻结作业和配置用例取代宿主任务透传

匹配页只接收显式应用服务、资料快照和路由回调，不再持有 `_PageTask`、宿主配置或整套运行时。`MatchingService` 冻结档案、角色模版、候选 UUID、筛选、角色要求和权重；候选刷新只读目录与反馈，只有显式动作才执行评分。完整结果绑定原请求及库存修订，复用既有原生评分、评级、投影和排序。按档案写回原有匹配配置结构，不改变配装角色或候选；配装的取消信号改从共用应用入口接线，不改变配装用例。

以同一份合成词条和三枚 UUID 对照旧后台函数与迁移后应用作业：原件输出 `BASELINE_WORKER ['3', '1'] [(29.96, 'B'), (29.96, 'B')] ['1', '3']`，新作业输出 `MATCH_JOB_FROZEN_OK: 29.96/B, source rows preserved, ties ordered by UUID`；候选保持源次序，同分展示按原 UUID 顺序，未选第三件不进入矩阵。原件定向基线 `Ran 11 tests in 0.248s`、`OK`，退出 0。

迁移后通过以下命令验证（`python -B -u -X faulthandler -m unittest -v` 加用例；3.0.6 执行全部，2.0.17 和 2.0.8 只执行真实宿主的挂载与匹配用例）：

- `tests.calculation.test_matching_scope_query`、`tests.calculation.test_match_projection`：14 组原生候选范围、5 组投影，加冻结输入、取消及 29.96/B 同分条件。
- `tests.calculation.test_matching.TestComparison.test_element_and_set_rules_are_separate_from_identical_base_score`、`tests.calculation.test_matching.TestComparison.test_cancellation_does_not_return_partial_matrix`：分值与适配独立，取消不发布部分矩阵。
- `tests.contracts.test_matching_rule_save`：最新角色草稿合并、取消、失效、提交失败与真实原生校验；精简 Qt 环境使用事件循环轮询，不依赖 `QtTest`。
- `tests.contracts.test_host_loading.HostLoadingContract.test_manifest_loads_and_opens_shared_workbench`：临时档案及开发版 Rust；从声骸库勾选并返回、显式计算、原配置范围写回、配装设置不变。三版均输出 `HOST_MATCH_NATIVE_OK: explicit candidate, explicit calculation, 29.96/B` 和 `HOST_WORKBENCH_OK: manifest, three tasks, shared Qt pages and runtime shutdown`。

| ok-script | 字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6 | `Ran 20 tests in 3.780s`；`OK` | 0 |
| 2.0.17 | `Ran 1 test in 3.610s`；`OK` | 0 |
| 2.0.8 | `Ran 1 test in 3.609s`；`OK` | 0 |

Ruff 输出 `All checks passed!`、`15 files already formatted`；结构检查 `"files": 351`、`"errors": []`、`"status": "passed"`。匹配目录没有宿主任务、运行时、原始配置、适配器或存储引用。只执行离线定向验证；未安装插件、启动游戏、执行 G5 或开放后台扫描。阶段 3 下一项是配装页面与用例。

#### 配装：显式作业、工作草稿和历史详情脱离宿主任务

配装页、角色编辑、当前配装和历史详情只使用显式应用服务、快照与导航回调。`AllocationJobs` 冻结候选 UUID、筛选、当前配装上下文和搜索预算；预检与搜索共用候选准备，沿用现有 Rust 五件约束、目标与排序，不调整算法、阈值或数据格式。事务提交仍由原有方案和库存用例负责；备份不改装备者，保存按原角色范围写入。已保存配装的单槽预览保持另外四件不变，不写入库存；明确保存后才更新当前配装，历史快照保留原记录与权重。工作草稿按同一格式保存，局部调整保留未参与角色、优先级与搜索预算。

以 `python -B -u -X faulthandler -m unittest -v` 加下列直接相关用例运行。原件与迁移后均执行以下八项：

- `tests.calculation.test_native_allocation_service.TestNativeAllocationService` 的 `test_real_production_score_to_five_peer_rebalance_and_native_revalidation`、`test_native_deadline_node_limit_and_cancel_are_honest`、`test_plan_precheck_uses_injected_native_service_without_database_writes`。
- `tests.calculation.test_native_allocation.TestNativeAllocation.test_mixed_cost_pins_main_stats_and_sets_match_independent_enumeration`。
- `tests.calculation.test_native_storage.TestNativeStorage` 的 `test_save_load_summary_and_progress_use_native_without_python_evaluators`、`test_archive_roundtrip_propagates_same_native_service_and_keeps_source_immutable`。
- `tests.calculation.test_native_replacement.TestNativeReplacement.test_seven_frozen_option_and_preview_outputs_without_python_business_evaluation`、`tests.contracts.test_loadout_actions.LoadoutActionsContract.test_backup_subset_is_independent_history_and_new_save_keeps_full_source`。

原件基线输出 `Ran 8 tests in 4.018s`、`FAILED (failures=4)`，退出 1。四个失败均为旧替换夹具未声明既有角色序列化字段 `weapon_id`、`energy_regen_min`、`energy_regen_max`、`base_energy_regen`。逐字段对照只见这四个缺项；测试补上精确默认值断言，未改原夹具、分数、评级、顺序或约束预期。七个实际序列化输出前后 SHA256 一致：`837F8EE27D5E32A3880832F4FA2E9E071BF820D448686667A465DC5D8DF0DAA9`。原件与迁移后五角色达标样本均输出 `new_cents=[15800, 15000, 15000, 15000, 15200] validated=True`。

迁移后额外执行 `TestNativeAllocationService.test_page_jobs_keep_scope_and_save_history_and_replacement_contracts` 与 `TestNativeAllocationService.test_subset_draft_preserves_other_roles_priorities_and_budget`：六取五、取消、预览与显式提交分离、历史不变、独立草稿范围。三版均以临时档案和开发版 Rust 执行 `tests.contracts.test_host_loading.HostLoadingContract.test_manifest_loads_and_opens_shared_workbench`，输出 `HOST_ALLOCATION_NATIVE_OK: 5 items, saved, history, one slot preview only`。

| ok-script | 字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6（全部定向） | `Ran 11 tests in 8.710s`；`OK` | 0 |
| 2.0.17（宿主链路） | `Ran 1 test in 4.392s`；`OK` | 0 |
| 2.0.8（宿主链路） | `Ran 1 test in 4.184s`；`OK` | 0 |

Ruff 输出 `All checks passed!`、`19 files already formatted`；结构检查 `"files": 352`、`"errors": []`、`"status": "passed"`。配装目录没有宿主任务、运行时、原始配置、适配器或存储引用。未启动游戏、安装插件或执行 G5，后台扫描保持关闭；后续继续设置与资料入口。

#### 设置与资料：先迁移资料检查与自定义权重

资料更新控件与自定义权重编辑器通过 `ResourceSettings` 使用同一运行时的资料协调器和原生权重作业，不直接读取宿主设置任务。旧版独立入口和工作台用同一实现：工作台展开资料卡才挂载更新控件，打开设置不检查网络；显式检查、取消、自动检查开关和本机重读保持原入口。自定义权重仍按角色、模版和原快照版本提交，资料更新仍保留本地自定义值。策略数据导入、代理 JSON 更新和 Wuwa 静态提取走 `PolicyResourceJobs`，沿用原校验和原子写入；资料变化经回调通知实时卡和测试卡。匹配与配装的“编辑模版权重”均走同一设置导航，不再从角色编辑器父控件寻找宿主任务；配装角色编辑所需面板资料也改为显式传入。

基线与迁移后的定向命令为 `python -B -u -X faulthandler -m unittest -v` 加以下用例：

- `tests.calculation.test_native_custom_weights.NativeCustomWeights` 的 `test_all_frozen_projection_build_and_validation_are_exact_without_python_math`、`test_provider_atomic_publication_and_rejection_preserve_current_files`、`test_update_worker_builds_only_after_explicit_request_and_publishes_once`、`test_actual_editor_methods_never_wait_on_pending_future_or_build_on_gui`。
- `tests.contracts.test_policy_updates` 基线已有两项和 `tests.contracts.test_wuwa_source.WuwaSourceContract.test_updater_fetches_and_merges_without_executing`。基线 `Ran 7 tests in 0.602s`、`OK`，退出 0。
- 新增 `tests.contracts.test_policy_updates.PolicyUpdateContract.test_workbench_jobs_use_validated_import_and_leave_last_good_data_on_failure`，本地伪造响应验证显式更新、原子导入和坏数据保留活动包。输出 `Ran 1 test in 0.067s`、`OK`，退出 0。
- 三版均执行 `tests.contracts.test_host_loading.HostLoadingContract.test_manifest_loads_and_opens_shared_workbench`，以临时设置和开发版 Rust 核对单一控件、显式检查／取消／重读、指定角色模版的原生投影与版本化保存请求。所有检查／取消调度用夹具截获，三版均输出 `HOST_RESOURCE_SETTINGS_OK: shared controls, explicit check, cancel, reload, custom projection`。

| ok-script | 字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6（原有定向用例与宿主链路） | `Ran 8 tests in 6.166s`；`OK` | 0 |
| 2.0.17（宿主链路） | `Ran 1 test in 5.364s`；`OK` | 0 |
| 2.0.8（宿主链路） | `Ran 1 test in 4.916s`；`OK` | 0 |

Ruff、格式及结构检查通过（354 个 Python 文件）；未安装插件、启动游戏或实际联网更新，G5 保留。档案、收录快捷键、截图清理和故障修复仍有旧接线，继续迁移后再结束设置与资料目标。

#### 设置：档案、快捷键、截图与故障修复统一接线

设置页接收显式服务与操作回调，不再持有宿主任务、整套运行时或原始宿主配置。档案继续使用同一库存修订、备份作业和设置发布/回执服务；管理选择与当前档案保持独立。嵌入与旧宿主快捷键、启动故障修复弹窗共用表单和适配层接线。截图清理仍需明确确认，确认后先停止收录，再排队清理。修复预览不写入，备份作业忙碌时不允许提交；修复前仍保存损坏原件，故障启动时也可在无运行时的情况下打开独立修复窗口。

离屏宿主用例 `python -B -u -X faulthandler -m unittest -v tests.contracts.test_host_loading.HostLoadingContract.test_manifest_loads_and_opens_shared_workbench` 使用临时档案和配置，实际导出档案包、预览后取消导入、修复合成损坏记录，并核对原件、库存修订与当前设置。快捷键监听与截图清理调度由夹具截获，确认没有隐式启用收录或删除截图。3.0.6 另带 `tests.contracts.test_capture_shortcut.CaptureShortcutContract.test_queued_shortcut_stays_with_its_original_intake_and_source`；三版宿主均输出 `HOST_SETTINGS_OK: rename, export, cancelled import, explicit shortcuts, cleanup and repair`。

| ok-script | 字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6（改动前宿主基线） | `Ran 1 test in 4.545s`；`OK` | 0 |
| 3.0.6（迁移后） | `Ran 2 tests in 4.628s`；`OK` | 0 |
| 2.0.17（迁移后） | `Ran 1 test in 5.454s`；`OK` | 0 |
| 2.0.8（迁移后） | `Ran 1 test in 5.281s`；`OK` | 0 |

本轮未安装插件、启动游戏或联网。工作台实时评分、策略测试和 HUD 的设置接线仍需收口；G5 真实游戏验收按约定暂缓。

#### 阶段 3 收尾：工作台实时设置与宿主边界

实时评分卡、强化测试卡和 Qt HUD 共用 `LivePreferences`，沿用原角色/模版 ID、目标分、资源权重、预设和 HUD 位置/字号的配置键与序列化口径。测试卡仍仅在明确发起计算后调用原生策略求解；候选来自同一实时快照，资料更新与自定义权重继续使用同一运行时服务。外壳通过显式路由切换档案与模式，移除任务透传代理；宿主配置、挂载和独立窗口所有权集中在 [OKWW 装配层](../../src/ww_echo_workshop/adapters/okww/page_bindings.py)。旧版任务卡、独立窗口返回和新版内嵌页都使用同一工作台实现。

离线按 `python -B -u -X faulthandler -m unittest -v` 执行以下相关用例：

- 改动前及 3.0.6 迁移后执行 `tests.contracts.test_hud_window`、`tests.contracts.test_hud_state`、`tests.contracts.test_policy_ui_state`，覆盖锁定与解锁重建、过期分数清除、新帧恢复和测试草稿格式。
- 三版执行 `tests.contracts.test_host_loading.HostLoadingContract.test_manifest_loads_and_opens_shared_workbench`。在临时宿主中核对档案切换守卫、旧版任务卡返回、角色与预设双向同步、显式 Rust 策略请求及 HUD 字号/位置和卸载停表，输出 `HOST_LIVE_SETTINGS_OK: role, shared preset, explicit policy, HUD position and font`。同一用例保留三模式、档案与资料更新/自定义权重的定向回归。

| ok-script | 字面输出 | 退出码 |
| --- | --- | --- |
| 3.0.6（改动前 HUD 与测试草稿基线） | `Ran 9 tests in 0.312s`；`OK` | 0 |
| 3.0.6（迁移后） | `Ran 10 tests in 6.944s`；`OK` | 0 |
| 2.0.17（迁移后） | `Ran 1 test in 6.384s`；`OK` | 0 |
| 2.0.8（迁移后） | `Ran 1 test in 5.914s`；`OK` | 0 |

Ruff 输出 `All checks passed!`、`17 files already formatted`；结构检查 `"files": 355`、`"errors": []`、`"status": "passed"`。阶段 3 的源码迁移及离线定向验证完成。阶段 2 的 G5 实机缺口、阶段 4 发布包与安装/升级/DPI 验收均未由本轮离屏结果替代。
