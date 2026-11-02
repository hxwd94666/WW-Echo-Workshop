# 文档索引

## 使用

- [使用指南](user/guide.md)：实时评分、收录、匹配、配装与档案。
- [常见问题](user/troubleshooting.md)：加载、评分、计算、数据与资料更新。

## 维护

- [架构与模块边界](maintainer/architecture.md)：目录、依赖、计算与宿主入口。
- [实时评分与收录链路](maintainer/capture.md)：四种实时评分场景、手动/自动收录区域和回退。
- [声骸强化策略与工作台](maintainer/policy-workbench.md)：工作台、实时策略、评分适配与分阶段计划。
- [评分体系与策略数据扩展](maintainer/policy-expansion.md)：五套评分、Joint、候选比较、锁法列表与国内更新。
- [OKWW 宿主优势整合方案](maintainer/policy-host-integration.md)：候选自动捕获、接受候选、Joint 转重抽、自定义预设与宿主取舍。
- [插件独立 OCR 引擎方案](maintainer/ocr-isolation.md)：与宿主 OCR 解耦、CPU 私有引擎、实时评分关闭行为和回滚。
- [实时评分透明悬浮窗 PoC](maintainer/policy-hud-poc.md)：透明置顶、鼠标穿透、编辑模式、拖拽和窗口内输入验证。
- [实时评分 HUD 改造方案](maintainer/policy-hud-refinement.md)：外观、颜色、字号、自动关闭、锁定缩放和设置搬迁。
- [原生计算设计](maintainer/rust-compute-plan.md)：评分、匹配、配装和库存查询的 Rust 迁移约束与历史背景。
- [界面性能](maintainer/performance.md)：历史测量方法与缓存边界。
- [数据与兼容](maintainer/data-and-migration.md)：格式、身份、事务和回退。
- [资料更新](maintainer/resources.md)：内置生成、自动检查、冻结与来源。
- [发布与验收](maintainer/release.md)：定向检查、构建、安装及用户流程。
- [发布前开发计划](maintainer/release-plan.md)：门禁、阻塞项、执行顺序与发布判定。
- [0.1 发布候选说明](maintainer/release-notes-0.1.md)：当前包、已验证范围、未验证项和回退。

安装前请先确认宿主：[OKWW 官方仓库](https://github.com/ok-oldking/ok-wuthering-waves)、[OKWW Releases](https://github.com/ok-oldking/ok-wuthering-waves/releases)、[OKWW 中文文档](https://ok-oldking.github.io/ok-wuthering-waves/zh-CN/)。
