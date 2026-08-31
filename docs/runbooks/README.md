# 运行手册

本目录只描述如何运行、发布和恢复系统。架构取舍与业务规则仍以 `docs/architecture/` 为准。

| 文档 | 用途 |
|---|---|
| [本地开发](local-development.md) | 本机进程、Compose、可选 H5 与 smoke |
| [生产发布准备](production.md) | 生产数据门禁、镜像、配置、发布顺序与健康检查 |
| [数据库迁移](migrations.md) | Alembic 升级、检查与回退边界 |
| [备份与恢复](backup-restore.md) | PostgreSQL、Redis、产物和行情快照的灾备流程 |
| [Choice Quant API](choice-quant-api.md) | 本机 SDK 注册、无密码激活与最小权限探针 |
| [多源事件数据](multi-source-events.md) | 东方财富、iFinD、RQData、Tushare 融合与组合快照 |
| [网页发现事件](web-discovered-events.md) | 搜索线索、权威原文、新闻时钟、修订链与回放门禁 |

## 运行时边界

- PostgreSQL 是回测任务状态、不可变输入和结果 JSON 的事实源。
- Redis/RQ 只负责投递 `run_id`，不是业务状态事实源。
- `DATA_ROOT` 是只读行情快照；API 与 Worker 必须挂载同一份内容。
- 正式技术/事件联合 Demo 的 `DATA_ROOT` 必须是内容寻址的 `composite_snapshot`，其中同时包含不复权执行价、后复权信号价、公司行动、session、秒级事件、观察层和 manifest；`var/data` 仅可用于隔离的 legacy/research fixture。
- `ARTIFACT_ROOT` 是可持久化产物目录，不能写入容器临时层。
- API 只提交任务；CPU 回测只在 Worker 中执行。

当前 Compose 是 **research/demo 编排**，但要求显式提供 host 侧 `MARKET_DATA_PATH`，并把该 composite 目录只读挂载到容器 `/app/var/data`；缺少路径时不启动。仓库生成的 session 文件仍是 `300059.SZ` 研究数据，不是全 A 股生产事实；生产环境还必须使用授权、可追溯的逐证券逐日文件，且 `APP_ENV=production` 会拒绝 research fallback。

容器使用非 root 用户、只读根文件系统和独立持久卷。Compose 中的默认密码只供本机演示，不能用于生产。
