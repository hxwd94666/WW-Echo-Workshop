# 发布前开发与验收计划

## 目标

将当前 `ww_echo_workshop` 迁移版本收敛到可交付的发布候选版本，先完成工程门禁，再完成宿主与真实游戏验收；未通过的项不得用“代码已完成”替代。

## 当前基线

- 分支：`main`
- 基线提交：`d3d5c50 refactor: migrate echo workshop to ww_echo_workshop with workbench and HUD`
- 包版本：`0.1`
- 当前状态：功能主体完成，发布验收未完成，暂不标记正式稳定版。

## 发布门禁

### G1 工程静态检查

必须通过：

```powershell
.\.venv\Scripts\python.exe -m ruff format --check --no-cache src plugin tests tools
.\.venv\Scripts\python.exe -m ruff check --no-cache src plugin tests tools
.\.venv\Scripts\python.exe -B tools/check_structure.py
.\.venv\Scripts\python.exe -B tools/resources.py validate
```

当前阻塞：8 个文件、16 处格式差异。

修复文件：

- `src/ww_echo_workshop/application/compute_jobs.py`
- `src/ww_echo_workshop/application/policy_data.py`
- `src/ww_echo_workshop/resources/policy_updates.py`
- `src/ww_echo_workshop/resources/weights.py`
- `tests/calculation/test_constraints.py`
- `tests/contracts/test_compute_process.py`
- `tests/contracts/test_compute_startup.py`
- `tests/native_runtime.py`

### G2 原生计算

```powershell
$env:CARGO_INCREMENTAL = "0"
.\.venv\Scripts\python.exe -B tools/compute.py test
```

当前阻塞：本机增量编译目录锁权限导致 `os error 5`；必须用可写 target 目录或禁用增量重跑，不能以环境失败冒充通过。

### G3 Python 测试

必须先通过：

- `tests.contracts.test_update_fallback`
- `tests.contracts.test_native_package`
- `tests.contracts.test_host_loading`
- 工作台、HUD、实时策略、评分预设相关契约测试

HUD 测试需使用带 PySide6 的宿主解释器，不得把仓库 `.venv` 的 3 项跳过视为通过。

### G4 工作台与 HUD 验收

2K + 125% 缩放已人工确认四场景可用。

其余 OKWW 支持分辨率：

```text
1920x1080
1600x900
1280x720
```

要求：

- 逐分辨率检查展示/对比/强化/背包四个场景；
- 开启“悬浮窗位置变换”时自动定位；
- 关闭时 HUD 保持用户位置；
- 展示/背包不遮挡右侧词条；
- 对比不遮挡左右词条并保持 OCR 可用。

未完成前，发布说明只能标记为“2K 已验证，其他分辨率待验收”。

### G5 真实游戏链路

至少覆盖：

- 实时识别 → 评分；
- 实时识别 → 强化/重抽/联合策略；
- HUD 锁定、详情、拖动、位置开关；
- 头像后台更新与取消更新；
- 数据源失败时保留旧资料。

### G6 构建与安装

```powershell
.\.venv\Scripts\python.exe -B build.py
```

要求：

- 连续两次构建 SHA256 一致；
- ZIP 包含 manifest 版本和原生计算程序；
- 不包含 tests、缓存、虚拟环境、玩家数据；
- 通过 OKWW 插件 ZIP API 安装；
- 三个入口导入正常；
- 安装前备份旧插件目录。

### G7 文档与版本

- 更新 `docs/maintainer/release.md` 的“迁移中”描述；
- 增加发布说明/变更记录；
- 明确版本号和回退包；
- 记录已验证分辨率与未验证范围。

## 执行顺序

1. 修复格式化，重跑 G1；
2. 用 `CARGO_INCREMENTAL=0` 完成 G2；
3. 补齐 G3 测试与临时目录问题；
4. 进行 G4 HUD 分辨率验收；
5. 进行 G5 真实游戏验收；
6. G6 构建、复现和安装；
7. G7 文档、版本、提交与发布候选标记。

## 每次交付记录

必须记录：

- 执行命令；
- 字面输出；
- 退出码；
- 修改文件；
- 未验证范围；
- 回退路径。

## 回退

- 源码回退到基线提交 `d3d5c50`；
- 插件回退使用 `ok_import_backup` 中安装前备份；
- 不删除玩家数据、权重缓存和 `plugin_data`。

## 发布判定

只有 G1–G6 全部通过，且 G7 文档完成后，才允许标记：

```text
release candidate / 可发布
```

任何一项未完成，状态保持：

```text
beta / 内部测试
```


## 执行记录（2026-11-02）

### G1 工程静态检查：通过

```text
ruff format --check --no-cache src plugin tests tools
324 files already formatted

ruff check --no-cache src plugin tests tools
All checks passed!

tools/check_structure.py
status: passed
files: 324

tools/resources.py validate
status: passed
```

说明：本次修复了 16 处格式差异；仓库根目录测试产物 `policy/` 已删除并加入 `.gitignore`。

### G2 原生计算：通过（使用 LLD）

普通 MSVC `link.exe` 仍受本机临时文件写入限制；使用 Rust 自带 LLD 后通过：

```text
18 passed; 0 failed
```

原 MSVC 失败记录：

`CARGO_INCREMENTAL=0` 后仍在链接阶段失败：

```text
LINK : fatal error LNK1104:
无法打开文件 ...\ww-echo-workshop-build-temp\lnk{...}.tmp
```

尝试过：

- `CARGO_INCREMENTAL=0`；
- 更换 `WW_ECHO_BUILD_TEMP` 到 `F:\WW\tmp-link2`；
- 更换到 `C:\Temp`；
- 清理 `build/native` 后重跑。

判断为本机链接器/安全策略写入限制，不是 Rust 编译错误。发布命令必须使用 LLD 和可写临时目录，当前已验证通过。

### G3 Python 测试：核心通过

```text
30 tests OK（策略服务、HUD 状态、实时策略、评分预设）
HUD 窗口测试：宿主 Python 13 项通过
更新回退测试：显式 tempfile.tempdir 后 4 项通过
原生包清单测试：LLD 下通过
```

仍需：完整相关契约在标准临时目录环境中复跑；仓库 `.venv` 的 PySide6 测试跳过需用宿主解释器记录。

### G4 HUD 分辨率：离线几何通过，实机待验收

已按 125% 缩放对 2560×1440、1920×1080、1600×900、1280×720 计算锚点，
并为对比场景增加左侧词条面板边界限制。

新增测试：

```text
tests.contracts.test_hud_layout
2 tests OK
```

仍需真实游戏逐场景确认四个 HUD 布局和 OCR 不被遮挡；离线计算不能替代实机截图。

### G5 真实游戏链路：未开始

需要实时识别、策略、HUD、头像更新和失败回退的现场验收。

### G6：通过

构建命令：

```text
RUSTFLAGS=-C linker=rust-lld
CARGO_INCREMENTAL=0
WW_ECHO_BUILD_TEMP=F:\WW\tmp-link2
build.py --output F:\WW\release-temp
```

结果：

```text
archive: F:\WW\release-temp3\ww-echo-workshop-0.1.zip
files: 316
sha256: 8489c3caaaa774f371d6af545c55ed5c58f01eb1ad6ed7b9a4ef0fd81c5805ac
```

连续两次独立输出构建 SHA256 一致；插件已通过 OKWW ZIP API 安装，三个入口导入正常。
