# 本地开发与严格 research demo

> 当前本地和 Compose 路径只用于 research/demo。正式技术/事件联合演示只接受内容寻址的 `composite_snapshot`；仓库 `var/data` fixture 不能作为验收数据。

## 1. 前置条件

- Python 3.12、uv；
- Docker Engine 与 Compose v2（使用容器方案时）；
- Node.js 22+、pnpm 11（单独开发 H5 时）；
- 已按 [Choice Quant API](choice-quant-api.md) 生成包含双价格流、公司行动覆盖证明和 session 的 Choice 快照；
- 已按 [多源事件数据](multi-source-events.md) 生成秒级事件快照，并与 Choice 组成 composite；
- 工作树已经提交且干净。strict composite 不接受 `local-demo`、dirty revision 或临时内容指纹。

若使用本文件第 2 节的内部按需准备模式，还需当前 Python 环境能导入 `EmQuantAPI` 与 `baostock`，并能访问 BaoStock、东方财富公开公司行动/公告接口及 Choice。该模式可在 dirty worktree 上做本地数据链验证，但产生的快照只能标记 `local-real-data-verified`，不能作为最终 strict Live 验收。

快照目录至少应包含：

```text
daily_ohlcv.parquet
signal_daily_ohlcv.parquet
corporate_actions.parquet
instrument_sessions.parquet
events.parquet
event_observations.parquet
snapshot_manifest.json
```

`corporate_actions.parquet` 为 0 行时，manifest 仍必须证明公司行动查询成功并完整覆盖本次区间。不要创建空文件绕过门禁。

## 2. 内部 Demo：fresh submission 按需准备

内部 Demo 使用独立模板：

```bash
cp .env.internal-demo.example .env.local
uv sync --extra dev --extra demo
uv run uvicorn ashare_lab.main:create_app --factory --host 127.0.0.1 --port 8000
```

模板的关键配置为：

```dotenv
MARKET_DATA_PROFILE=on_demand_snapshot
EVENT_DATA_REQUIRED=true
ON_DEMAND_REFRESH_EACH_SUBMISSION=true
CHOICE_SNAPSHOT_ROOT=var/snapshots/internal-demo/choice
EVENT_SNAPSHOT_ROOT=var/snapshots/internal-demo/events
COMPOSITE_SNAPSHOT_ROOT=var/snapshots/internal-demo/composite
SNAPSHOT_PREPARATION_ROOT=var/snapshots/internal-demo/preparations
```

`ON_DEMAND_REFRESH_EACH_SUBMISSION=true` 的语义不是“边回测边搜”：fresh submission 先按本次股票、扩展区间和事件码重新采集，校验后发布内容寻址 producer snapshot，再把精确 ID/schema/checksum 固定进 RunManifest。并发的相同准备只共享同一次 single-flight 尝试。Worker 携带 expected snapshot identity 时只读本地 registry，不调用采集器；目标缺失、目录与内容 ID 不一致或校验和变化都会 fail closed。已发布目录作为不可变审计产物保留，不作为可变缓存回退。

准备链当前依次执行：

```text
BaoStock：证券身份 + 按年 preclose/tradestatus/isST
→ 东方财富：现金分红/送转/配股正向查询 + 拆股/缩股负证明
→ Choice：不复权执行日线 + 后复权信号日线 + 交易日历
→ 事件策略：东方财富秒级事件；技术策略：显式 no_event_required Event v2
→ 统一发布 Composite v2 内容寻址快照
```

技术策略的 `no_event_required` 表示本次策略根本不需要事件，因此没有调用事件供应商；它不是“查询成功且返回 0 条”，也不能满足后续事件请求。运行证据中 `dataSchemaVersion=local-parquet.market-data.v3` 是 loader 生成的本次 slice schema，`producerSnapshotSchemaVersion=ashare-lab.composite-research-snapshot.v2` 是上游不可变快照 schema，两者不得混用，Worker 会分别重验。

日线预开盘 BUY 订单的数量也必须符合信息时点：策略账户 09:15 和同期持有 09:29 都按当日已知的 `upper_limit` 预留最坏成交价与费用，不得用 09:30 才由日线数据提供的 `bar.open` 定量。无有效涨停上限的 session 直接返回 `pre_open_buy_sizing_upper_limit_unavailable`，不创建订单；实际撮合仍可在 09:30 使用开盘价代理。

BaoStock `preclose/tradestatus` 必须与 Choice 同日期的 `PRECLOSE/TRADESTATUS` 精确一致；不一致时拒绝发布。Choice `tradedates` 长区间按自然年有界分块再合并去重，每批显式设置 `RECVtimeout=30`；后复权信号 `csd` 保持整段查询，避免改变复权锚点。session 文件使用 v2 的 `minimum_buy_quantity + buy_quantity_increment`：主板/创业板 100/100，科创板 200/1；北交所领域规则虽为 100/1，但历史挂牌场所与跨源逐日停牌/reference 尚未闭环，因此 `.BJ` 准备请求会明确失败。

启动后的 `/ready` 只证明准备命令、SDK、输出目录、registry、数据库和队列可用，不表示任意股票已经有快照。实际提交成功后再核对运行 manifest 的 producer snapshot identity；失败时保留结构化 `unsupported / preparation_failed / preparation_incomplete` 原因，不得改用 fixture。

当前对外按需事件准备面仍是年报、半年报、季报、业绩预告和业绩快报五类。`event.repurchase_capital.repurchase_change` 只有 implemented + fixture-tested 证据，尚未发布包含该码的新 live Event/Composite v2；运行手册不得把它当作已可提交事件。

本轮已验证样例为 `choice:a2eebf97b7c2ed4f2467ca6f91a3e95f48ed609c4026b440a16e7a11a4a23b4f`：`300059.SZ`、2021-08-06 至 2026-08-06，执行/信号/session 各 1,211 行，公司行动 7 条（现金 5、送转 2），配股 0 条、拆股/缩股候选 0，后复权前缀 969 行差异 0、`passed`。这是 dirty worktree 下的 `local-real-data-verified`，不是 clean-SHA Live E2E。

## 3. 固定 strict composite：本机单进程

复制模板后，把两个 `<sha256>` 替换为同一个 composite 内容 hash：

```bash
cp .env.local.example .env.local
```

关键配置应为：

```dotenv
APP_ENV=local
DATABASE_URL=sqlite+pysqlite:///./var/ashare.db
INITIALIZE_SCHEMA=true
QUEUE_BACKEND=thread
DATA_ROOT=var/snapshots/composite/<sha256>
MARKET_DATA_PROFILE=composite_snapshot
SESSION_REFERENCE_MODE=parquet
SESSION_REFERENCE_PATH=var/snapshots/composite/<sha256>/instrument_sessions.parquet
EVENT_DATA_REQUIRED=true
```

本机有 `.git` 时，服务自动读取当前 revision；不要在 `.env.local` 手填伪 SHA。先确认 `git status --porcelain` 没有输出，再启动：

```bash
uv sync --extra dev --extra demo
uv run uvicorn ashare_lab.main:create_app --factory --host 127.0.0.1 --port 8000
```

检查 `/ready` 的每一项，而不是只看 HTTP 200：

```bash
curl -fsS http://127.0.0.1:8000/api/v1/ready
curl -fsS http://127.0.0.1:8000/api/v1/capabilities
./scripts/smoke.sh --require-backtest --as-of-date 2026-08-06
```

严格实例至少应报告执行日线、信号日线、公司行动、事件、事件观察、snapshot manifest、session、run store、queue、event profile、strict event manifest 和 code revision 为 `ok`。`/capabilities` 必须同时允许技术与事件回测。

## 4. Compose research demo

复制模板并填写三个值：host 侧 composite 路径、同一快照在容器内的固定 `/app/var/data`，以及当前干净提交的 40 位 Git SHA。

```bash
cp .env.example .env
git status --porcelain
git rev-parse HEAD
```

`.env` 的数据段应保持：

```dotenv
MARKET_DATA_PATH=./var/snapshots/composite/<sha256>
DATA_ROOT=/app/var/data
MARKET_DATA_PROFILE=composite_snapshot
SESSION_REFERENCE_MODE=parquet
SESSION_REFERENCE_PATH=/app/var/data/instrument_sessions.parquet
EVENT_DATA_REQUIRED=true
CODE_REVISION=<40-character-clean-git-sha>
```

Compose 会把明确的 host composite 目录只读挂载到 API/Worker 的 `/app/var/data`。`MARKET_DATA_PATH` 缺失时直接拒绝启动，不会退回 `./var/data`。

```bash
mkdir -p var/artifacts
docker compose --profile web up --build -d
docker compose ps
docker compose logs --tail=100 api worker migrate
curl -fsS http://127.0.0.1:8000/api/v1/ready
./scripts/smoke.sh --require-backtest --as-of-date 2026-08-06
```

API 默认绑定 `127.0.0.1:8000`，OpenAPI 位于 `http://127.0.0.1:8000/api/v1/docs`。H5 默认监听 `8080`，同源反向代理 `/api/` 到 API。

停止进程但保留数据：

```bash
docker compose down
```

不要在有用数据上运行 `docker compose down -v`；它会删除 PostgreSQL、Redis 和产物卷。

## 5. PostgreSQL + RQ 本机进程

若要分别运行 API 与 Worker，保留同一 `.env.local` composite 配置，只替换基础设施：

```dotenv
DATABASE_URL=postgresql+psycopg://ashare:ashare@localhost:5432/ashare
INITIALIZE_SCHEMA=false
REDIS_URL=redis://localhost:6379/0
QUEUE_BACKEND=rq
QUEUE_NAME=backtests
```

启动顺序：

```bash
docker compose up -d postgres redis
DATABASE_URL=postgresql+psycopg://ashare:ashare@localhost:5432/ashare uv run alembic upgrade head
uv run uvicorn ashare_lab.main:create_app --factory --host 127.0.0.1 --port 8000
uv run rq worker backtests --url redis://localhost:6379/0
```

API 与 Worker 必须看到同一份只读 composite；不能让 API pin 一个 hash、Worker 读取另一个目录。

## 6. H5 单独开发

```bash
cd web
pnpm install
VITE_USE_MOCK=false VITE_API_BASE_URL= VITE_DATA_AS_OF_DATE=2026-08-06 pnpm dev
```

空的 `VITE_API_BASE_URL` 会使用 Vite 的同源 `/api` 代理。首次执行浏览器 smoke 还需安装 Chromium：

```bash
.venv/bin/playwright install chromium
.venv/bin/python web/scripts/smoke_live.py --strategy technical
.venv/bin/python web/scripts/smoke_live.py --strategy event
```

浏览器验收必须核对：事件时间显示到秒，provider/time quality/source/snapshot/Git 可见，策略与同期持有都显示同一资金基准，活动按 signal → order → fill/partial/unfilled/expired 串成因果轨迹。运行证据还应显示 `funded_buy_and_hold.same_execution_ledger.v1`、`cn.a_share.timeline_entitlement_receivable_settlement.date_only_cash_after_close_conservative.v2`、`gross_research_no_withholding.v1` 和 `decline_no_external_cash.v1`。日期级现金派息必须标明收盘后才转可用，避免把它误读成盘中到账；配股默认明确记录不认购、不注入外部现金；税前分红不能被误读成完整税后口径。

## 7. legacy fixture 的边界

`make demo-data` / `scripts/prepare_demo_data.py` 仍可生成 BaoStock `var/data` fixture，用于隔离的 schema、迁移和错误路径调试。它包含日期级报告时间和研究推导 session，不满足 strict event、公司行动覆盖或 clean-run 证据要求。由该目录产生的收益和 E2E 不能写入 `docs/status.md` 的正式验收栏。

## 8. 提交前检查

```bash
make check
uv run alembic heads
cd web && pnpm typecheck && pnpm lint && pnpm test && pnpm build
```

若 Docker 不可用，至少执行 Alembic SQLite 往返和 `compose.yaml` 静态解析；这不能替代真实镜像构建。最终 strict E2E 必须在提交后重新运行，确保 RunManifest 与 `/ready` 使用精确 clean Git SHA。
