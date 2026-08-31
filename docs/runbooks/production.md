# 生产发布准备

> 代码已经提供严格的 Parquet 证券状态 Adapter，并在生产环境拒绝 research fallback；但仓库 demo 数据不是授权生产数据，本机也没有完成 Docker/生产恢复演练。因此当前发布级别仍是 research demo。

## 1. 上线前硬门槛

生产数据必须同时满足：

- 授权的不复权执行日线、后复权信号日线、公司行动、秒级事件/观察和逐日证券状态数据；
- 公司行动查询成功且完整覆盖快照区间；事件来源时间、修订和原始响应可追溯；
- 生产税务政策已经业务/法务确认；当前 `gross_research_no_withholding.v1` 未模拟个人按持有期补税，只能用于 research，不能冒充完整税后账户；
- 每次发布固定文件 SHA-256、Catalog hash、代码 revision 与镜像 digest；
- `instrument_sessions.parquet` 满足[逐日证券状态契约](../reference/session-reference-v1.md)；
- 原始文件可按 checksum 从不可变存储找回；
- PostgreSQL 迁移、Redis/RQ Worker、备份恢复和 E2E 均在隔离环境演练通过；
- API 认证、用户级 run ownership、审计与限流已补齐。

仓库 `var/data` 来自 BaoStock，报告时间为 date-only、缺公司行动覆盖证明、历史 ST 状态为研究假设，只能用于隔离调试，不能挂到生产或正式 Demo。

## 2. 必填配置

```dotenv
APP_ENV=production
DATABASE_URL=postgresql+psycopg://USER:URL_ENCODED_PASSWORD@postgres:5432/ashare
INITIALIZE_SCHEMA=false
REDIS_URL=redis://:URL_ENCODED_PASSWORD@redis:6379/0
QUEUE_BACKEND=rq
QUEUE_NAME=backtests

MARKET_DATA_PATH=/srv/ashare/snapshots/composite/<sha256>
DATA_ROOT=/app/var/data
MARKET_DATA_PROFILE=composite_snapshot
SESSION_REFERENCE_MODE=parquet
SESSION_REFERENCE_PATH=/app/var/data/instrument_sessions.parquet
EVENT_DATA_REQUIRED=true
ARTIFACT_ROOT=/app/var/artifacts
CATALOG_ROOT=/app/catalogs

CODE_REVISION=<40-character-clean-git-sha>
ENGINE_VERSION=release-version
```

Compose 把明确的 host `MARKET_DATA_PATH` 只读挂载到容器 `/app/var/data`；变量缺失时拒绝启动。生产装配若不是 strict `composite_snapshot`、使用 `research_300059`、缺公司行动/事件观察文件，或 `CODE_REVISION` 不是构建镜像对应的 40 位 SHA，都必须 fail-fast。密码应由 Secret 管理；不要提交 `.env`，也不要复用 Compose 默认凭据。

## 3. 发布顺序

1. 备份 PostgreSQL，确认 composite、公司行动覆盖、事件观察与 Catalog 哈希；
2. 拉取按 digest 固定的 API/Worker/Web 镜像；
3. 启动 PostgreSQL 与 Redis；
4. 单独执行一次 `alembic upgrade head`；
5. 启动一个 API 和一个 canary Worker；
6. 等待 `/api/v1/ready`，再运行固定日期 E2E；
7. 核对 run id、数据 manifest、Git SHA、执行假设、结果 hash、队列和错误率；
8. 扩容 Worker，最后切换 Web/Ingress 流量。

Compose 演练示例：

```bash
docker compose pull
docker compose up -d postgres redis
docker compose run --rm migrate
docker compose up -d --no-deps api worker
curl -fsS http://127.0.0.1:8000/api/v1/ready
./scripts/smoke.sh --require-backtest --as-of-date 2026-08-06
```

固定日期只适用于仓库 demo。正式环境应从审核过的数据 manifest 选择截止日。

## 4. 就绪与健康

| 检查 | 当前含义 | 仍需额外验证 |
|---|---|---|
| `/api/v1/health` | HTTP 进程可响应 | 不代表依赖可用 |
| `/api/v1/ready` | 执行/信号行情、公司行动、事件/观察、严格 manifest、session 和 clean code revision 存在；数据库和队列可连接 | 不检查 Alembic head、RQ Worker 数量、全文件逐行经济一致性或真实策略成功 |
| `/api/v1/capabilities` | 分开声明技术回测与事件回测当前是否可用 | Catalog 已登记不代表对应数据权限已具备 |
| `alembic current` | 数据库 revision | 必须等于唯一 head |
| RQ Worker/queue | 有消费者、队列可投递 | 仍需 canary run |
| E2E smoke | 编译、提交、执行和结果读取成功；结果含 snapshot/Git/执行假设 | 只覆盖指定数据和策略，少量事件不证明策略有效 |

API 容器健康检查使用 `/ready`；Worker 容器目前验证 Redis 连通性，不能代替 Worker 消费监控。

## 5. 扩缩容与重试

- PostgreSQL 是任务状态事实源，Redis/RQ 只投递 `run_id`；
- Worker 通过原子 `QUEUED → RUNNING_DATA` 抢占，同一 run 的重复投递不会重复计算；
- 提交重放遇到仍为 `QUEUED` 的 run 会重新入队，用于修复“已入库、未入队”；
- 缩容时停止接收新任务并等待 Worker 优雅退出，不强杀正在写结果的任务；
- 当前没有自动扫描孤儿任务、lease 超时恢复和运维补投命令，仍需队列告警与人工审核流程。

## 6. 安全与观测

- API/Worker 使用非 root、只读根文件系统和 `no-new-privileges`；
- 行情目录只读挂载，产物使用独立卷；
- 生产入口必须经 TLS，PostgreSQL/Redis 不暴露公网；
- 日志不得输出数据库 URL、Redis URL、凭据或完整敏感原话；
- 告警至少覆盖 5xx、任务失败码、queued 时长、Worker 数量、磁盘、数据库连接和备份新鲜度。

备份与回退见[备份恢复](backup-restore.md)和[数据库迁移](migrations.md)。
