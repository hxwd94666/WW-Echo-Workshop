<div align="center">

<img src="src/echo_workshop/ui/widgets/assets/workshop.png" alt="Echo Workshop 声骸工坊兽面标志" width="108">

# 声骸工坊

**Echo Workshop · 鸣潮声骸收录、评分与配装**

![平台](https://img.shields.io/badge/平台-Windows-0078d4)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab)
![状态](https://img.shields.io/badge/状态-开发预发布-e6a23c)
[![许可](https://img.shields.io/badge/许可-GPLv3-238636)](LICENSE)

[功能](#features) · [界面预览](#preview) · [安装升级](#install) · [快速开始](#quick-start) · [参与开发](#development)

</div>

## 简介

声骸工坊把声骸操作拆成三个独立模式：**声骸库**管库存，**匹配**看单件，**配装**分整套。角色、模版、固定件与候选范围都是明确输入，计算由你主动触发；评分是角色模版权重下的参考指标，不是游戏官方评级，也不等于伤害模拟。

<a id="features"></a>

## 功能

- **收录**：手动收录当前声骸或扫描背包；识别存疑时先核对再入库。
- **库存**：搜索、多条件筛选（主词条、套装、COST）、待核对、保护与档案管理。
- **匹配**：多角色对比同一批声骸，查看单件分数、评级与适配原因。
- **配装**：按角色要求、固定件、套装与优先级生成五件，跨角色不重复占用；支持保存、备份、历史与单槽替换。
- **计算**：随包 Rust 引擎执行评分、匹配与配装搜索，界面与计算分离，大库存批量计算在后台完成。

<a id="preview"></a>

## 🔥界面预览

以下为界面实例截图；评分是角色模版权重下的参考指标，不代表游戏官方评级或伤害模拟。

<table>
  <tr>
    <td align="center" width="50%">
      <strong>声骸库</strong><br>
      <sub>收录后的库存，支持搜索、筛选、待核对与保护</sub><br><br>
      <img src="docs/assets/inventory.png" alt="声骸库">
    </td>
    <td align="center" width="50%">
      <strong>多角色匹配</strong><br>
      <sub>同一批声骸对比多个角色模版下的分数与适配</sub><br><br>
      <img src="docs/assets/matching-live.png" alt="多角色匹配">
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <strong>按角色配装</strong><br>
      <sub>查看五件配装、总分评级与配装变动</sub><br><br>
      <img src="docs/assets/allocation-live.png" alt="按角色配装">
    </td>
    <td align="center" width="50%">
      <strong>自定义评分权重</strong><br>
      <sub>按角色调整主词条与副词条权重</sub><br><br>
      <img src="docs/assets/weights.png" alt="自定义评分权重">
    </td>
  </tr>
</table>

<a id="install"></a>

## 安装与升级

获取维护者提供的 `echo-workshop-<version>.zip`（GitHub 源码 ZIP 不是可安装插件包）。

1. **完全退出 OKWW**。
2. 解压发行包，把 `echo-workshop` 文件夹放入 OKWW 的 `ok_import/`。
3. 升级时整包替换旧文件夹，不混合新旧文件，不保留两份插件。
4. 重启 OKWW，打开左侧 **声骸工坊**。

```text
OKWW 运行目录/
└─ ok_import/echo-workshop/
   ├─ manifest.json
   ├─ echo_workshop_settings.py
   ├─ echo_workshop_overlay.py
   └─ echo_workshop_scan.py
```

**数据位置**：玩家数据在宿主配置目录的 `plugin_data/echo-workshop/`，不在插件安装文件夹。替换或重装插件不会覆盖库存与配装；重装宿主、更换运行目录或迁移电脑前，请在 **设置 → 数据** 导出档案。

### Windows 安全提示

插件包含未签名的本地 Rust 计算程序（`native/bin/echo-workshop-compute.exe`），Windows 或 Defender 可能提示“打开这些文件可能会对你的计算机有害”或“Windows 已保护你的电脑”。它只在本机计算，不下载或执行远程代码。

- 右键 ZIP → **属性** → 勾选 **解除锁定** → 确定；
- 或解压后在 PowerShell 执行：`Get-ChildItem '<OKWW>\ok_import\echo-workshop' -Recurse | Unblock-File`；
- 若 Defender 实际隔离了该 exe，确认来源后把 `ok_import\echo-workshop` 加入排除项，不要关闭 Defender。

<a id="quick-start"></a>

## 快速开始

1. **建立库存**：游戏打开声骸背包或展示页，手动收录或扫描。
2. **核对**：检查识别存疑的词条、数值与套装，确认后再使用。
3. **看单件**：声骸库右键「快速评分」；比较多个角色时到「匹配」添加角色并开始计算。
4. **分整套**：到「配装」设置角色要求与固定件，选择候选后直接配装。
5. **保存**：查看每个角色的总分与变动，按角色或全部保存；备份只新增冻结历史，不改变当前配装。

[使用指南](docs/user/guide.md) · [故障排查](docs/user/troubleshooting.md)

<a id="development"></a>

## 参与开发

源码要求 **Python 3.11+**。界面验收组合为 Python 3.12、ok-script 2.0.8、PySide6 6.11.1、Fluent Widgets 1.11.3。Rust 计算引擎使用锁定的 1.98.0 工具链，插件用户无需安装 Rust。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[ui,okww,dev]"

.\.venv\Scripts\python.exe -m ruff check src tests tools
.\.venv\Scripts\python.exe -m ruff format --check src tests tools
.\.venv\Scripts\python.exe -B tools/check_structure.py
.\.venv\Scripts\python.exe -B tools/run_checks.py --calculation   # 计算测试
.\.venv\Scripts\python.exe -B tools/run_checks.py --smoke         # 强路径契约（逐模块隔离）
.\.venv\Scripts\python.exe -B tools/compute.py build              # Rust 引擎
.\.venv\Scripts\python.exe -B build.py                      # 发行 ZIP（输出到仓库根目录）
```

| 目录 | 职责 |
| --- | --- |
| `src/echo_workshop/native/compute/` | Rust 计算引擎 |
| `src/echo_workshop/application/` | 用例、异步作业、运行时组合 |
| `src/echo_workshop/storage/`、`resources/`、`capture/` | 本地数据、资料更新、收录识别 |
| `src/echo_workshop/ui/` | 声骸库、匹配、配装、设置界面 |
| `plugin/okww/` | 三个薄宿主入口与 manifest |
| `tests/calculation/`、`tests/contracts/` | 计算验证、强路径契约 |

版本唯一来源是 `src/echo_workshop/__init__.py`；不要编辑 `dist/` 中的生成代码。更多细节见[维护文档](docs/README.md)与[贡献指南](CONTRIBUTING.md)。

## 许可与来源

项目遵循 [GPLv3](LICENSE)。本项目从原声骸评分插件独立整理与维护；最初实现及第三方资料来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。游戏图像属于其权利人，本项目不是游戏官方产品。
