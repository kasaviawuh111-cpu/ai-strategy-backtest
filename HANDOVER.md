# A 股自然语言策略回测：工程交接

更新日：2026-09-01  
仓库：`/Users/mima0000/Documents/回测`  
状态口径：本文严格区分代码存在、本地测试、真实本地数据、Mock、公网可用与生产可用。测试绿或目录存在不等于线上能力。

## 1. 当前结论

- 产品目标：用户在 A 股股票或可交易 A 股 ETF 页面说一句交易想法，系统将其还原为受限策略，校验后回测，并解释信号、委托、成交、未成交和结果来源。
- 当前不是“任意一句话都能回测”。已实现的是受限自然语言到 DSL；不完整表达应集中澄清一次，观点类表达仍需先转为投资假设和候选策略。
- 当前 Git 工作区很脏，不能直接发布、合并或宣称可复现。HEAD 为 `b165bd3734c04d7ba68e60f76549da17aaedc870`，分支 `codex/public-live-integration`，无 remote/upstream。
- 当前正式技术目录在未提交工作区中有 36 个稳定指标、134 个触发器；两条旧测试仍硬编码 35，导致后端全量测试 2 失败。
- 当前真实本地复合快照只证明 `300059.SZ` 的日线与五类定期报告事件，不证明全 A 股、36 指标的所有语义、69 类事件或公网能力。
- 固定 WorkBuddy 链接仍能打开，但前端 bundle 含 `mock_demo`，因此只能称 Mock 演示。另一个 WorkBuddy 链接已失效。
- 公网 API 当前返回 `503 SERVICE_FORBIDDEN`，不能称 Live 可用。
- 不要部署、切流量、回滚、改环境变量或提交 Git，除非新负责人先完成本文门禁。

## 2. Git 与未提交改动

### 2.1 当前身份

```text
branch: codex/public-live-integration
HEAD: b165bd3734c04d7ba68e60f76549da17aaedc870
remote/upstream: 无
staged: 0
```

创建本文档前，工作区有 27 个已跟踪修改和 7 个未跟踪源码/测试文件；本文档及 Quickstart 也是未跟踪文件。原代码改动约 `+1359/-136`，不得把它们当作一个已验收功能整体提交。

主要改动范围：

- 自然语言：`ashare_lab/adapters/language/rule_based.py`、`vibe_candidates.py`
- 编译/运行：`compile_strategy.py`、`daily_backtest.py`、`technical_v2.py`、`signals/runtime.py`
- API/bootstrap/settings：`api/app.py`、`bootstrap.py`、`settings.py`
- 技术目录：`catalogs/signals/cn_a_technical.v1.manifest.json`、coverage 目录与生成器
- CloudBase：Dockerfile、环境样例、bundle 脚本
- 前端 Mock 与 same-origin 发布保护
- 新增未跟踪：`ashare_lab/api/web_hosting.py` 及其测试；前端 same-origin 配置、guard、检查脚本及测试

### 2.2 已知最近测试

```text
make check
ruff: pass
format check: 343 files pass
pyright: 0 errors
pytest: 2476 passed, 2 failed, 1 skipped, 1 warning
```

两个失败均是稳定指标数量从 35 变为 36 后，旧断言未更新：

- `test_stable_catalog_and_daily_evaluator_registry_are_identical`
- `test_projection_exactly_matches_current_35_indicator_and_69_event_release`

前端最近证据：

```text
Vitest: 15 files / 179 tests passed
same-origin target: 22 passed
typecheck, lint, build:same-origin: passed
backend web-hosting/bundle target: 22 passed
```

结论：当前整仓不绿，不能形成 release commit。

## 3. 实际部署拓扑

```text
用户浏览器
  ├─ 固定 WorkBuddy 前端（HTTP 200，但当前 bundle 为 Mock）
  └─ 预期调用 CloudBase HTTPS API
         ├─ FastAPI
         ├─ SQLite/线程队列（当前默认，不是生产持久化）
         └─ 本地/打包 Composite snapshot
                ├─ Choice 日线
                └─ 东方财富公告观察
```

| 层 | 当前地址/身份 | 当前事实 | localhost/VPN |
|---|---|---|---|
| 前端 | `https://59ac3319a9594be59fa3034fcae82a8f.app.workbuddy.link/` | HTTP 200；资源 `index-DZcV7yf9.js`、`index-C4VtYJdx.css`；JS 含 `draft_mock_demo_001`/`mock_demo` | 访问不依赖 localhost；发布控制可能受 VPN/代理影响 |
| 旧前端 | `https://8ae96e06a2b3404ea1b9470810cccd7c.app.workbuddy.link/` | HTTP 404，链接失效 | 否 |
| API | `https://ashare-backtest-api-305722-11-1330091763.sh.run.tcloudbase.com` | health/ready/version/capabilities 当前均为 HTTP 503，网关 `SERVICE_FORBIDDEN` | 不依赖 localhost；控制面状态待确认 |
| 本地前端 | `127.0.0.1:5188` | 先前观察到 Vite listener/HTTP 200；是否仍运行待确认 | 是 |
| 本地 API | `127.0.0.1:8011` | 先前 health/version 200，ready 因 dirty revision 503；当前是否仍运行待确认 | 是 |
| 数据库 | 默认 SQLite `var/ashare.db` | Alembic DB 版本 `20260830_0002`，仓库 head `20260831_0005`；0 run | 本地文件；云端持久 PostgreSQL 待办 |

线上版本 009/010/011 的当前流量、stable/gray 标记、镜像 digest 与真实 Git SHA 均 `待确认`。历史操作记录不能代替当前控制面查询。

## 4. API

正式路由：

```text
GET  /api/v1/health
GET  /api/v1/ready
GET  /api/v1/version
GET  /api/v1/capabilities
POST /api/v1/strategy-drafts
POST /api/v1/strategy-drafts/{draft_id}/revisions
POST /api/v1/backtest-runs
GET  /api/v1/backtest-runs/{run_id}
POST /api/v1/backtest-runs/{run_id}/cancel
GET  /api/v1/backtest-runs/{run_id}/summary
GET  /api/v1/backtest-runs/{run_id}/series
GET  /api/v1/backtest-runs/{run_id}/trades
POST /api/v2/strategy-drafts
GET  /api/v2/strategy-drafts/{draft_id}/revisions/{revision}
POST /api/v2/strategy-validations
POST /api/v2/backtest-runs
GET  /api/v2/backtest-runs/{run_id}
```

- OpenAPI：`/api/v1/openapi.json`；Swagger：`/api/v1/docs`。
- 当前没有认证/授权实现；不得公开到可写公网后当作生产服务。
- v1 创建类请求支持可选 `Idempotency-Key`；请求体上限 16 KiB。
- 标准错误：`{"error":{"code":"...","message":"...","details":[]},"request_id":"..."}`。
- HTTP 202 只代表 queued，不代表回测成功；必须轮询到 `succeeded` 并核验 result hash、活动与来源。
- v1 草稿仍是进程内存态，服务重启会丢失。
- v2 路由始终注册，但服务未启用时返回 503；现有前端没有 v2 client。
- v2 的可信字段由服务端签发；客户端不能自报 executable、receipt、source_refs、snapshot、provider 或 Git SHA。

## 5. DSL 与自然语言

### 5.1 契约位置

- v1 JSON Schema：`contracts/strategy.v1.schema.json`
- v1 模型：`ashare_lab/domain/strategy/models.py`
- v2 模型：`ashare_lab/domain/strategy/models_v2.py`
- 规则解析：`ashare_lab/adapters/language/rule_based.py`
- 观点候选：`ashare_lab/adapters/language/vibe_candidates.py`
- 编译器：`ashare_lab/application/compile_strategy.py`

### 5.2 执行边界

- LLM/DeepSeek 只能生成候选 DSL，不能直接生成买卖信号、行情事实或 executable 标记。
- 候选必须经过 Schema、标的、能力、PIT、覆盖和 ConditionGrounding 校验。
- 缺买入/卖出关键条件、遗漏用户条件、偷换指标、未知单位/周期、指数标的均不得 READY。
- A 股 STOCK 与有真实主数据/行情覆盖的 ETF 才能进入验证；INDEX 不支持，不能自动映射 ETF。
- 默认回测区间当前为近一年，以可信快照末日为基准，不以系统“今天”硬算。

v1 条件对应的执行声明必须精确：

| 条件 | data_policy | signal_clock |
|---|---|---|
| 纯技术 | `daily_ohlcv` | `1d_close` |
| 事件 | `daily_ohlcv_events` | `event_available_plus_1d_close` |
| 财务 | `daily_ohlcv_financials` | `financial_available_plus_1d_close` |
| 事件+财务 | `daily_ohlcv_events_financials` | `event_financial_available_plus_1d_close` |

历史 PE+MACD 422 的直接原因是人工拼装 JSON 的 execution 声明不匹配，不是已证明的回测引擎错误。正确 smoke 应先调用编译 API，原样提交服务端返回策略，并等待最终 succeeded。

## 6. 当前能力矩阵

### 6.1 技术指标

未提交工作区中的执行目录为 36 个稳定指标、134 个触发器：

```text
MACD, MA, RSI, volume, EMA, MA cross, Bollinger, KDJ, CCI, BBI,
EMA bias, close price, return %, rolling high, consecutive up, amplitude,
amount, average amount, relative volume, price-volume confirmation, OBV,
price-volume divergence, trend regime, true range, ATR, NATR, ADX, DMI,
BIAS, ROC, momentum, stochastic, Williams %R, Donchian,
return standard deviation, historical volatility
```

机器可读精确参数和触发器以 `catalogs/signals/cn_a_technical.v1.manifest.json` 为准。目录注册不等于每种口语、所有股票和公网数据均已真实验证。

### 6.2 财务/估值

领域模型登记 27 个财务/估值指标，但当前可执行证据只覆盖东方财富估值趋势接口中的：

```text
valuation.pe
valuation.pb
valuation.ps
valuation.pcf
```

使用接口直接 `INDICATOR_VALUE`，不自行推导 TTM、MRQ、同比、单季度、ROE、利润率或估值。以下仍 fail closed：

- 报表类指标的完整修订历史
- `pe_ttm`、`pb_mrq`、`dividend_yield_ttm`
- API 未直接给出所需口径的任何值

当前 provider 的公网 Live 可用性未验证。

### 6.3 事件

- 产品目录有 69 条 stable 事件代码路径，但不能称 69 类真实历史覆盖。
- 当前真实本地严格快照只覆盖 5 类：业绩预告、业绩快报、年度报告、半年度报告、季度报告。
- 当前产品决定：事件能力暂不作为本轮可发布承诺；股份回购、股东增减持、业绩发布等后续再接正式公告源。
- 事件需按 `first_available_at` 判断；秒级可信则按秒，分钟/小时可信则按相应粒度，只有日期或时间不可靠才按收盘后保守处理。
- `retrieved_at` 仅是抓取审计时间，不能冒充历史可得时间。
- 当前快照 `document_text=0`，不支持公告全文词频回测。

### 6.4 标的

- `300059.SZ`：本地快照有真实日线与事件数据。
- `510300.SH`：证券主数据中标为 ETF，但当前本地 composite 没有其行情；不能宣称本地可执行。
- `000300.SH`：INDEX、不可交易；必须返回 capability_unavailable，严禁自动改成 510300.SH。
- “全量 A 股按需拉取”有代码方向但没有完成的生产数据覆盖证明。

## 7. 回测语义

- 单股/单 ETF、只做多、单持仓、不加仓，日线为当前可靠执行粒度。
- 日线收盘确认后，下一可交易日开盘代理价 09:30 尝试成交；日线 OHLCV 不能证明 09:25 集合竞价成交。
- A 股 T+1；买入后当日不能卖。
- 一字涨停买入、一字跌停卖出默认不假装成交；默认等待开板。
- 默认容量为前一交易日成交量的 5% PIT 代理，避免使用当天收盘后成交量撮合 09:30 订单。
- 默认滑点 5 bps、佣金率 0.03%、最低 5 元、最大仓位 100%；印花税/过户费按历史规则。
- 基准必须用同样初始资金、费用、公司行动与可成交规则运行买入持有账户；超额收益 = 策略账户收益 - 同口径买入持有收益。
- 0 笔成交不是自动成功：需解释是条件未触发、数据不足、不可成交还是解析错误；正式 smoke 要求有可审计活动。
- 公司行动仍有缺口：配股认购/缴款、复杂税务等未生产闭环。
- 分钟模型/Choice 探针存在，但没有真实分钟快照、minute DSL、minute signal runtime 或 minute matcher；不得宣称分钟回测。

## 8. 数据与快照

当前主要本地复合快照：

```text
Composite: composite:1f26afb8b1223b656397abd5c99ce5a0ac9ed6593c3dbf7bdc46fb5a61bb9b8b
schema: ashare-lab.composite-research-snapshot.v2
Choice child: choice:93941d...
Event child: events:399ba57...
instrument: 300059.SZ
daily range: 2021-02-08 .. 2026-08-20
bars/sessions/signals: 1340 / 1340 / 1340
corporate actions: 9
event observations/canonical: 26 / 26
```

事件分布：年度报告 6、半年度报告 5、季度报告 10、业绩预告 3、业绩快报 2；查询覆盖证据为 9 页、862 条公告。

限制：

- Choice 原始响应随快照归档；东方财富公告保存逐页 response hash，但原始页字节未完整归档。
- 数据许可仅能称研究/Demo；生产授权和再分发权待确认。
- iFinD/RQData/Tushare 适配器存在，但本机无授权或导出，不能称真实联网验证。
- Choice SDK 本机版本与文档版本存在差异，激活/授权及云端可用性待确认。
- 所有运行必须只读固定 content-addressed snapshot；禁止边搜边跑和事后选择“最早”来源。

## 9. 开源复用

以 `THIRD_PARTY.yml` 为准：

| 项目 | 许可证/状态 | 当前使用方式 |
|---|---|---|
| astock-backtest-engine 基础 | MIT | 作为基础结构；大量 A 股 PIT/DSL/审计能力仍为本仓适配 |
| HKUDS Vibe Trading | MIT | 借鉴/适配候选想法、观点到假设路径；无运行时直接依赖 |
| MOSS | MIT-0 | EMA/MACD 公式参考；不是当前运行引擎 |
| AKShare | MIT | 协议/数据源路由参考；不是当前正式运行依赖 |
| `ta` | MIT | 候选指标库，未安装 |
| pypdfium2 | 见归档许可树 | 可选 PDF 文本处理依赖 |
| BaoStock | 许可/数据条款未澄清 | 发布阻塞，未采用为正式源 |
| mootdx | 许可冲突 | 已阻止接入 |
| Choice | 专有 SDK/数据 | 正式研究数据候选，需账号激活、权限与再分发确认 |

原则：优先薄适配成熟库，不复制整仓；但开源项目不能替代 A 股数据权限、PIT 时间、涨跌停/T+1、证券主数据、审计与执行闸门。

## 10. 本地启动

要求：Python 3.12、uv、Node 22、pnpm。最近本机版本：Python 3.12.12、uv 0.11.7、Node 22.23.2、pnpm 11.19。

```bash
cd /Users/mima0000/Documents/回测
git status --short --branch
uv sync --frozen
pnpm --dir web install --frozen-lockfile
```

后端本地开发所需环境值应从 `deploy/cloudbase/env.example` 复制到不入 Git 的本地文件；不要把密钥写入命令历史、日志或文档。

常用验证：

```bash
make check
pnpm --dir web test
pnpm --dir web typecheck
pnpm --dir web lint
pnpm --dir web build:same-origin
```

启动命令以仓库 Makefile/README 为准；在当前 dirty revision 下 `/ready` 预期 fail closed，不能通过伪造 Git SHA 绕过。

## 11. 发布与验收

### 11.1 禁止的捷径

- 不发布 Mock 目录冒充 Live。
- 不把本地 Live 联调当公网全链路。
- 不把 queued/HTTP 202 当成功。
- 不让发布工具修改 strategy JSON、阈值、execution、源码、环境变量或 smoke。
- 不因 CloudBase promote/gray 状态异常反复切流量。
- 不以单只股票样例冒充全市场覆盖。

### 11.2 正确门禁

1. 整仓测试绿、worktree clean、形成可回滚 commit。
2. 数据快照、schema、content hash、代码 SHA、费用配置全部固定。
3. 后端候选版本先 0% 流量，构建正常后验 `/ready`。
4. 执行仓库内固定 smoke：至少技术策略、财务策略、指数拒绝、不支持策略。
5. 每条策略必须轮询到最终 succeeded/rejected；成功返回 run ID、result hash、code revision、数据身份和非空审计活动。
6. 用真实浏览器核验 Network 请求确实命中公网 API，CORS、响应与后端日志一致。
7. 前端以 `VITE_USE_MOCK=false` 和真实 API/same-origin 构建；bundle guard 确认无 Mock。
8. 任一核心门禁失败停止切流量；记录原始日志，由代码负责人修复，不让发布执行器现场设计测试。

## 12. 故障速查

| 现象 | 已知根因/检查顺序 |
|---|---|
| `SERVICE_FORBIDDEN` / 503 | CloudBase 服务隔离、流量或权限控制面；先查服务状态，不改策略代码 |
| WorkBuddy “链接已失效” | 发布未覆盖固定绑定目录或应用被取消；确认绑定关系和最新链接 |
| 页面能开但全是演示 | bundle 含 `mock_demo` 或 `VITE_USE_MOCK=true`；检查构建产物而非 UI 文案 |
| 点击回测无反应 | 浏览器 Network、API base、CORS、ready、请求响应依次检查 |
| 422 execution mismatch | 人工 strategy JSON 与条件类型不一致；必须用编译 API 返回的 strategy 原样提交 |
| 0 笔成交 | 条件未触发、数据区间、可成交限制、退出条件逐一看活动；不能自动判通过 |
| `/ready` 503 本地 | dirty revision、snapshot、session reference、queue/store 任一 fail closed |
| 指标口语解析失败 | 规则目录注册不等于自然语言覆盖；看 grounding/unsupported 原因，不能删条件强行执行 |
| 财务值缺失 | 返回 null/no_signal/capability_unavailable；禁止填 0 或自行推导 |

## 13. 优先级

### P0

1. 把 36 指标相关脏改拆分审查，修复两条固定 35 的测试，跑全量后形成独立 commit。
2. 查询 CloudBase 当前 009/010/011 控制面事实；恢复一个可访问、可回滚、带真实持久存储的候选 API。
3. 完成前端真实 API 构建，证明 bundle 无 Mock，并做公网技术策略端到端。
4. 补认证/授权；未完成前不得把写 API 暴露为生产。
5. 统一 v1/v2 持久化与 RunManifest：原话、DSL、receipt、Composite 及子快照、Hash、provider、完整 Git SHA。

### P1

1. 建立全 A 股 STOCK/ETF 权威证券解析与按需不可变快照，不再只靠 300059。
2. 为 510300.SH 补真实、可追溯 ETF 日线与规则验证。
3. 扩大自然语言：价格阈值、当日涨跌幅、持有交易日等常见条件单表达；每项都要 grounding 和语义测试。
4. 财务只接接口直接口径，补修订历史和 PIT 覆盖后再开放。
5. 事件待正式公告时间、修订/撤回、覆盖证明和授权齐备后再开放。

### P2

1. 分钟行情、分钟信号和分钟撮合完整链路。
2. 更丰富观点到投资假设引导，但不可把情绪直接当可执行策略。
3. 公司行动复杂规则、生产级容量模型和更多券商执行假设。

## 14. 新负责人第一天

前三条命令：

```bash
cd /Users/mima0000/Documents/回测
git status --short --branch
git diff --stat && git ls-files --others --exclude-standard
```

随后先读：`HANDOVER_QUICKSTART.md`、`docs/SPEC.md`、`docs/00-acceptance.md`、`THIRD_PARTY.yml`、`catalogs/signals/cn_a_technical.v1.manifest.json`。

绝对不要：清理/重置用户改动、提交当前整包、部署 Mock、改线上流量、把密钥贴进对话或把目录数量写成真实能力。

## 15. 待确认

1. CloudBase 009/010/011 当前 stable/gray/traffic、镜像 digest、Git SHA、环境变量和日志。
2. 公网 API 为何进入 `SERVICE_FORBIDDEN`，服务是否被隔离、欠费、权限禁用或流量解绑。
3. 固定 WorkBuddy 链接绑定的发布目录、能否安全覆盖且保持 URL 不变。
4. 本地 5188/8011 进程是否仍运行、由谁启动、日志位置和退出方式。
5. 27 个修改文件与 7 个未跟踪文件分别属于哪个阶段，是否全部保留。
6. 云端持久 PostgreSQL、不可变 ACL/trigger、备份与重启恢复方案。
7. Choice 账号的证券范围、ETF、历史区间、分钟权限、调用量、SDK 版本和商业再分发许可。
8. 东方财富/交易所/巨潮公告原始字节归档权和生产使用许可。
9. iFinD、RQData、Tushare 的实际授权及是否允许生产使用。
10. `300059.SZ` 以外的全 A 股/ETF 证券主数据与行情覆盖。
11. 事件本轮到底继续关闭，还是先开放哪几类及所需真实正负样本标准。
12. 36 个稳定指标新增项是否经产品确认，以及目录 182/160 项的正式分层。
13. 本地数据库落后三个 Alembic revision 的迁移、备份和回滚验证。
14. 线上是否曾保存真实 run；当前本地数据库为 0 run，历史 run ID 不能证明现状。
15. 生产认证、用户隔离、限流、审计留存和隐私策略。
16. 前端最终视觉/文案细节是否以当前本地 dist 为准；该 dist 未提交、未发布。

