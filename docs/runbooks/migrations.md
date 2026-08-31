# 数据库迁移

## 原则

- 生产只通过 Alembic 改结构；`INITIALIZE_SCHEMA` 必须为 `false`。
- API 和 Worker 启动前必须到达同一个 migration head。
- Alembic 复用运行存储的 ORM metadata 做漂移检查；迁移 revision 仍需显式编写并评审。
- 降级可能丢数据。执行 downgrade 前必须有可验证备份，并在隔离环境演练。

## 常用检查

```bash
uv run alembic heads
uv run alembic history --verbose
DATABASE_URL=... uv run alembic current
DATABASE_URL=... uv run alembic upgrade head
DATABASE_URL=... uv run alembic check
```

Compose 中：

```bash
docker compose run --rm migrate
docker compose exec api alembic current
```

首个 revision `20260828_0001` 创建 `backtest_runs`，包括：

- `run_id` 主键与唯一 `fingerprint`；
- Strategy、Manifest、执行配置和结果 JSON；
- 状态、进度、错误码、时间与乐观锁版本；
- 指纹、状态、更新时间索引；
- 进度和版本检查约束。

## 发布前演练

在临时数据库依次验证：

```bash
DATABASE_URL=sqlite+pysqlite:////tmp/ashare-migration-check.db uv run alembic upgrade head
DATABASE_URL=sqlite+pysqlite:////tmp/ashare-migration-check.db uv run alembic current
DATABASE_URL=sqlite+pysqlite:////tmp/ashare-migration-check.db uv run alembic check
DATABASE_URL=sqlite+pysqlite:////tmp/ashare-migration-check.db uv run alembic downgrade base
DATABASE_URL=sqlite+pysqlite:////tmp/ashare-migration-check.db uv run alembic upgrade head
```

SQLite 只能验证迁移链基本可执行；上线前仍需在与生产同大版本的 PostgreSQL 上验证锁时间、索引创建和回滚方案。

## 失败处理

1. 保持 API/Worker 停止，不要让新旧代码同时写不同结构；
2. 保存 Alembic 和 PostgreSQL 完整错误；
3. 判断迁移事务是否已回滚，使用 `alembic current` 与数据库目录核对；
4. 优先修复 forward migration；不要直接手改 `alembic_version`；
5. 只有在已验证备份且 downgrade 明确可逆时才回退。
