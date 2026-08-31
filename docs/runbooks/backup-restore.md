# 备份与恢复

## 1. 保护对象与建议目标

| 对象 | 作用 | 建议 RPO | 建议方式 |
|---|---|---:|---|
| PostgreSQL | 任务、Manifest、状态、结果事实源 | ≤ 15 分钟 | 托管 PITR + 每日逻辑备份 |
| Redis AOF | 尚未消费的 `run_id` | ≤ 5 分钟 | AOF `everysec` + 卷快照 |
| composite 数据 | 执行/信号行情、公司行动、事件/观察、session 与 manifest | 每次发布/更新 | 只读对象存储、版本和 SHA-256 |
| Catalog | 指标定义版本 | 每次发布 | 随镜像 + 内容哈希 |
| artifacts 卷 | 导出文件与扩展产物 | ≤ 24 小时 | 卷快照或对象存储同步 |

具体 RPO/RTO 必须由业务负责人确认；表中数值只是首版运维目标。

## 2. 一致性备份

最稳妥的全量快照窗口：暂停 API 写入，优雅停止 Worker，确认没有 `running:*` 任务，再备份 PostgreSQL、Redis AOF、artifacts 和行情清单。在线 PostgreSQL 备份可以保证数据库自身一致，但无法自动保证与外部卷在同一时点。

创建逻辑备份目录并导出数据库：

```bash
backup_dir="backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_dir"
docker compose exec -T postgres pg_dump \
  --username "${POSTGRES_USER:-ashare}" \
  --dbname "${POSTGRES_DB:-ashare}" \
  --format custom > "$backup_dir/postgres.dump"
sha256sum "$backup_dir/postgres.dump" > "$backup_dir/SHA256SUMS"
```

请求 Redis 落盘并记录卷快照：

```bash
docker compose exec -T redis sh -ec '
  if [ -n "${REDIS_PASSWORD:-}" ]; then
    redis-cli --no-auth-warning -a "$REDIS_PASSWORD" BGSAVE
  else
    redis-cli BGSAVE
  fi
'
```

`BGSAVE` 是异步命令。卷快照前应通过 `INFO persistence` 确认
`rdb_bgsave_in_progress:0` 且 `rdb_last_bgsave_status:ok`；同时保存整个 Redis 数据卷，不能只复制单个 AOF 片段。

Redis 不是事实源，但丢失 AOF 会留下已入库、未被 Worker 消费的 queued/running 任务。重复提交相同指纹会把仍为 `queued` 的 run 重新入队，但当前没有自动扫描孤儿任务的运维命令；恢复时仍应恢复 Redis，或由负责人核对后通过受控重放补投，不能直接修改业务状态表。

composite 清单至少记录路径、大小、修改时间和 SHA-256。`MARKET_DATA_PATH` 必须显式指向本次发布的内容寻址目录，不能默认回退到 `./var/data`：

```bash
find "${MARKET_DATA_PATH}" -type f -print0 \
  | sort -z \
  | xargs -0 sha256sum > "$backup_dir/market-data.SHA256SUMS"
```

将备份加密后复制到独立故障域，并定期做恢复演练。只验证“备份任务成功”不等于可恢复。

## 3. 恢复到隔离环境

不要先覆盖生产。使用新数据库、新卷和新 Compose project 验证；若恢复环境与生产同机，还必须换用不冲突的主机端口：

1. 固定与备份匹配的镜像、Catalog 与行情快照；
2. 启动隔离 PostgreSQL/Redis；
3. 恢复 PostgreSQL；
4. 恢复 Redis AOF 与 artifacts；
5. 执行 `alembic upgrade head`；
6. 校验行情 SHA-256；
7. 先启动一个 API 和一个 Worker；
8. 运行 smoke，并抽查 succeeded/failed/cancelled 任务；
9. 确认 queued/running 任务与 Redis 队列一致后再切流量。

示例中的 `--clean` 会删除目标数据库已有对象，只能对明确的隔离恢复库执行：

```bash
docker compose -p ashare-restore up -d postgres redis
docker compose -p ashare-restore exec -T postgres pg_restore \
  --username ashare \
  --dbname ashare \
  --clean --if-exists < backups/TIMESTAMP/postgres.dump
docker compose -p ashare-restore run --rm migrate
./scripts/smoke.sh --base-url http://RESTORE_HOST:8000 --require-backtest --as-of-date 2026-08-06
```

## 4. 校验清单

- `alembic current` 等于唯一 head；
- Catalog hash、composite/公司行动/事件 checksum、`ENGINE_VERSION` 和精确 40 位 `CODE_REVISION` 可追溯；
- PostgreSQL 中 succeeded 任务都有结果 JSON，failed 任务都有错误码；
- API、Redis、Worker 健康，队列可消费；
- smoke 能编译策略、完成一次回测，并读取 summary、series 和 trades；
- 恢复报告记录耗时、数据缺口、人工动作和审批人。
