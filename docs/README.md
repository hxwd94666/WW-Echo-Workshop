# 文档索引

## 使用

- [使用指南](user/guide.md)：三模式、收录、评分、配装与档案。
- [常见问题](user/troubleshooting.md)：加载、计算、数据与资料更新。

## 维护

- [全业务计算 Rust 化开发方案](maintainer/rust-compute-plan.md)：全计算迁移、算法优化、旧代码删除与宿主隔离门槛；实施中，全量接管和真实宿主验收尚未完成。
- [验收后续开发计划](maintainer/rust-compute-follow-up.md)：当前进度、剩余工作量、后续工作包与最小验证安排。
- [架构](maintainer/architecture.md)：目录、模块依赖和宿主入口。
- [界面性能](maintainer/performance.md)：热点、缓存边界和大库存测量方法。
- [数据与兼容](maintainer/data-and-migration.md)：现有格式、身份、事务和回退。
- [资料更新](maintainer/resources.md)：内置生成、自动检查、冻结与来源。
- [发布与验收](maintainer/release.md)：定向检查、构建、安装及用户流程。

现行文档只有一套；旧版本交付流水账不随项目维护或分发。阶段重构记录保存在开发交付记录中，不作为另一份用户手册。
