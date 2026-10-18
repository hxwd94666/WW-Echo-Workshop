# 定向验收与发布

## 工程检查

```powershell
.\.venv\Scripts\python.exe -m ruff format --check src plugin tests tools
.\.venv\Scripts\python.exe -m ruff check src plugin tests tools
.\.venv\Scripts\python.exe -B tools/check_structure.py
.\.venv\Scripts\python.exe -B tools/resources.py validate
.\.venv\Scripts\python.exe -B tools/run_checks.py tests.calculation.test_scoring
.\.venv\Scripts\python.exe -B tools/compute.py build
.\.venv\Scripts\python.exe -B tools/compute.py test
.\.venv\Scripts\python.exe -B build.py
```

普通改动只跑相关方法/模块。目录迁移收敛时可明确运行 `tools/run_checks.py --calculation`，不自动遍历历史UI套件。宿主加载检查需要可选宿主依赖并在隔离目录运行。

`ruff` 是pyproject描述的开发工具，核心/归档构建不要求安装它；发布前需执行format和lint。归档构建另需源码中锁定的 Rust 工具链和 Cargo.lock 对应依赖，当前工具默认离线构建，不修改宿主依赖。结构工具负责中文首注、800行、Python3.11语法、内部模块存在性、旧flat导入与纯计算/UI边界。

## 构建和安装

构建从唯一源码、任务入口和版本生成manifest，递归保留模块/资源相对路径；校验源码、生成目录、ZIP字节和资源来源。固定ZIP时间/权限与排序，重复构建应得到相同hash。

Rust 开发源码在唯一业务源码根内，但不直接随插件发行。构建器对其单独白名单校验，编译 release EXE，核对嵌入的源码摘要和唯一产品版本，再将 EXE 与带 SHA256 的原生清单加入归档；用户端不运行 Cargo。当前为迁移中，计算程序随包不等于所有生产入口已完成 Rust 接管。

产物不包含tests、缓存、旧ZIP、虚拟环境、玩家数据或开发流水账。ZIP外层保留安装ID `echo-workshop`；发行文件名使用新项目名。

完全关闭宿主后替换插件；开发安装前保存上一版，并提供能执行的回退。只替换安装目录，不覆盖宿主源码或 `plugin_data/`。安装后做少量相关加载检查，不重复全测试。无明确指令不commit/push/tag或创建远程仓库。

## 简短用户验收

- 三模式各自可加减角色，选择/改规则不自动计算；匹配单/多角色切换保持选择器。
- 声骸筛选同组多选并集，无重复套装绘制；选择确认/取消回到实际调用页。
- 快速评分显示分数/评级、颜色和明确标准；无额外悬浮解释。
- 配装点角色直达要求；未确认不能进入可生成结果；优先级只影响配装。
- 按角色/全部备份只写历史；保存原子更新当前配装、预留与装备者。单槽替换预览不写入，显式保存才生效；其他角色不受影响，UUID和冻结评级不变。
- 宽窄窗、明暗主题、常用缩放不截断重要词条和操作。
- 离线可用、自定义权重保留；取消与错误日志可区分。
- 实际游戏扫描需额外前台验收；offscreen截图不是该证据。

## 交付记录

给出版本、修改包、迁移/删除映射、字面验证记录、回退命令和明确未验证范围。验收覆盖完整需求才标记重构完成，不用一个绿色检查推断所有模块正确。
