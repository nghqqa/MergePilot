# MergePilot 复赛材料

当前正式材料基于 `v0.1.0-preview.4` / `5bb2635` 整理。同机黑盒验收为 `SAME_MACHINE_ACCEPTED`，独立物理机验收仍为 `EXTERNAL_BLOCKED`；不代表 production ready。

## 目录

- [材料约束](00-材料约束.md)
- [正式整理版 finals-v1](finals-v1/)
- [历史项目方案](01-更新版项目方案/)
- [历史代码包说明](02-代码包说明/)
- [历史 Demo](03-Demo/)
- [历史声明矩阵](04-声明证据矩阵/)

正式 Release：[v0.1.0-preview.4](https://github.com/nghqqa/MergePilot/releases/tag/v0.1.0-preview.4)。

## 冻结口径

9 个离线镜像，OCI tar 约 847MB；门禁 `2471 passed / 0 failed / 20 skipped`。传输为 `wsl-user-relay`，`direct_routing_verified=false`。所有材料必须保留以下真实性边界：`application_integration_verified=false`、`database_verified=false`、`production_verified=false`、`revision_producer_contract=NOT_VERIFIED`、`audit_producer_contract=NOT_VERIFIED`。
