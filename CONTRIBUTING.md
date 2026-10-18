# 贡献指南

## 开发入口

先读 `AGENTS.md` 和 `docs/maintainer/architecture.md`。Python使用本地虚拟环境，核心不需要Qt/宿主。使用标准库unittest进行定向验证，不启动游戏、不遍历历史大套件。

`requirements-dev.lock` 固定本次验收的直接UI/宿主依赖，不宣称锁定整个OKWW运行环境。真实宿主的截图/OCR后端由宿主管理。

## 改动规则

- 文件首行中文职责说明、最多800物理行；按职责拆合，不为每个类建立目录。
- 计算层只接收显式输入；改公式、精度、目标或数据格式须单独说明，不混在布局重构中。
- 测试优先计算；非计算保留极少量数据/宿主关键契约，普通UI用发布清单验收。
- 修复失败测试先确定真实行为，不通过删断言掩盖回归。
- 迁移在副本验证；任何示例、日志、测试fixtures都不含真实玩家数据。
- 删除模块前核查生产调用、格式兼容和替代路径。

开发格式与lint使用固定 Ruff 0.17.0；可选开发依赖不进入插件运行依赖。执行 `python -m ruff format src plugin tests tools` 和 `python -m ruff check src plugin tests tools`。配置依据 [Ruff官方格式说明](https://docs.astral.sh/ruff/formatter/)。

## 提交内容

说明用户问题、改动范围、运行过的具体检查、未验证范围和兼容影响。文档以现行事实为准，不追加dev长流水账。修改相应CHANGELOG条目即可。

不提交缓存、ZIP、虚拟环境、数据库、个人路径或用户截图。正式发布须维护者验收；自动化开发不自行commit/push/tag。
