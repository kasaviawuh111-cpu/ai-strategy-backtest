# A 股单股自然语言策略回测

当前本地 MVP 使用真实 DeepSeek 生成和修改策略、东方财富选股/查数 Skill 获取数据，再用本地引擎回测。模糊输入先由模型引导，完整策略进入可编辑预览；回测后由真实模型给出简短分析及可重新执行的优化候选，不用规则模板或 Mock 代替模型。

```text
一句中文
→ 完整规则直接编译；纯观点先生成非执行候选并由用户选择
→ 策略卡
→ 异步回测
→ 收益 / 风险 / 净值
→ 信号 / 委托 / 成交 / 未成交原因
→ AI 分析与优化候选 → 修改或优化重跑
```

当前不是生产交易系统，也不构成投资建议。

## 当前本地入口（eastmoney_skill）

- 运行 `./scripts/start_local_fullstack_review.command`，在隐藏输入框提供 DeepSeek Key；凭证只注入本次进程，不写入前端或启动日志。
- 前端：`http://127.0.0.1:5184/`；后端：`http://127.0.0.1:8011/api/v1/health`。
- 已启动后端时，可在 `web` 目录运行 `npm run dev:live`；本地真实模式构建用 `npm run build:live`。两个命令都固定到 8011，并禁用 Mock。
- 持久缓存仅包含东方财富、贵州茅台、平安银行、中芯国际、比亚迪这 5 只股票；其余股票每次新回测走东方财富 Skill，历史回测结果仍单独保存。
- DeepSeek 使用独立联网搜索获取公开信息；交易价格和历史指标不从搜索摘要中生成。
- 此路径不接持仓复盘，不调用 BaoStock、Wind 或自己的公司行动账本；按东方财富声明的复权序列模拟收益，不声称是实盘股份账本。
- 对话草稿保留最近 20 轮上下文，限当前服务会话；启动脚本不启用自动重载，避免编辑文件时丢失草稿。运行结果保存在 SQLite。
- 本地联调和用户验收后再准备发布。`eastmoney_skill` 当前拒绝生产环境；不要直接用下方旧的 Compose/快照配置发布这一版。

## 历史严格快照分支说明

以下内容保留旧 Composite/事件研究分支的设计与运行记录，不是上方当前 MVP 的启动、数据源或能力承诺。

本轮交付是给老板直接在公网入口随机试用的 Live 回测，不是 Mock 演示。Mock 仅用于本地开发预览；正式入口必须使用真实 HTTPS API、固定真实快照和真实回测结果，断网或接口失败时明确报错，绝不静默回退或展示预设收益。

产品默认回看近 5 年。2017 年至今只有在来源、秒级时间质量和完整区间覆盖都合格时才条件开放；
不先承诺完整 10 年，2017 年以前尤其需要更权威来源交叉证明。

项目的产品、交互、数据、算法、实现状态、验收与路线图统一见 [Living Spec](docs/SPEC.md)。
通用能力采用“开源优先、薄适配、双跑后迁移”，边界与许可证清单见
[开源复用与胶水代码](docs/reference/reuse-first.md)、
[MOSS 与 Vibe-Trading 胶水化采用方案](docs/reference/oss-adoption-moss-vibe.md)、
[`THIRD_PARTY.yml`](THIRD_PARTY.yml) 和 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

文档统一区分 `decided`、`implemented`、`fixture-tested`、
`local-real-data-verified`、`Mock-verified`、`Live-unverified` 与
`production-unavailable`。测试通过、Mock 页面或本地真实快照校验都不能替代最终 clean-SHA Live E2E。

## 当前能力

| 范围 | 当前状态 |
|---|---|
| 策略 | A 股、单股、只做多、日线 |
| 自然语言与观点引导 | 完整策略继续走“确定性快路 + 受限 CandidateAst + 现有严格编译”；模型 transport 已配置，只有较早版本的公网冒烟证据，当前 revision 仍为 `Live-unverified`。`idea-route.v1` 已达到 `implemented / fixture-tested`：任意有意义输入先被理解；纯观点只生成“观点 → 可检验假设 → 当前权威 A 股页价格代理 → 2—3 个固定模板候选”，用户一次选择后才重新编译，选择前不可回测 |
| 其他开源胶水 | `implemented / fixture-tested / Live-unverified`：MOSS/pandas 指标差分、Vibe/mootdx 日线接口和 AKShare-shaped 东方财富 Push2 日线请求协议已落地；指标未切默认，mootdx 商业与数据条款待审，Push2 直连未取得真实 batch 或 snapshot，公共未文档化接口不视为生产授权 |
| 技术信号 | 37 个透明日线定义：固定收盘价阈值、均线/趋势、MACD/RSI/KDJ/CCI/Stochastic/Williams %R、BIAS/ROC/Momentum、BOLL/Donchian、TR/ATR/NATR/波动率、ADX/DMI、价格、量价与换手率等；目录可执行不等于任意中文说法都已验收 |
| 事件信号 | 69 项只达到 Catalog/编译/Adapter/运行时代码路径；当前 strict Composite 只对年报、半年报、季报、业绩预告、业绩快报 5 码提供 pinned coverage，共 26 条东方财富秒级观察；不得写成 69 类真实历史覆盖 |
| 报告正文词频 | `implemented / fixture-tested / Mock-verified / Live-unverified`：可对“初始完整定期报告正文”做确定性词频，并与“首次实际买入成交后第 N 个 A 股交易日退出”组合；当前 strict Composite 的 26 条 observation 中 `document_text=0`，必须 opt-in 重采正文后才能 Live 运行 |
| A 股规则 | T+1、停牌、涨跌停、手数、费用、滑点、PIT 容量、部分成交、订单过期 |
| 资金账户 | 策略与同期持有同资金、同撮合；分红/送转按权益、应收、结算三阶段记账 |
| 结果 | 摘要、净值、因果活动、秒级事件来源、运行证据、版本化完整结果哈希、预设执行敏感性场景 |
| 交互 | React H5/WebView，支持编辑参数、区间、资金和成本 |
| 运行 | 本地 SQLite/thread；部署材料为 PostgreSQL/Redis/RQ/Docker |

云端持久 PostgreSQL/CFS、数据库不可变权限和跨容器重启恢复已登记为后续生产化 TODO；当前版本不得据本地 SQLite 或容器内文件声称生产级持久化。

覆盖目录还登记了 182 个 A 股单股指标/数据项和 160 个 A 股相关事件，但登记不等于可执行。其中技术、价格、量能 132 项中只有 37 个 `stable`；最终以
`GET /api/v1/capabilities` 的 `backtest_execution_available`、逐事件码
`backtest_available` 与 `preparation_available` 为准。固定 composite 只有在当前
pinned snapshot 已有合格区间覆盖时才返回 `backtest_available=true`；on-demand
运行时在还没有具体股票与区间快照时只能返回
`preparation_available=true + availability_scope=request_preparation`，不能把“能够按需采集”写成“当前快照已有数据”。

`resultHash` 只有在携带受支持的 `hashSchemaVersion`，且 API 读时对完整持久化结果重算一致时才是完整性证据。无版本且无 hash 的 legacy 结果只按“无完整性证据”兼容；legacy 带 hash、未知版本或重算不一致均拒绝返回。

宿主股票页只把通过 A 股代码/交易所规则校验的规范 symbol 作为身份。URL 中的 `name` 或
`instrument_name` 未经证券主数据校验时不可信，不能决定回测标的；无可信名称时只展示规范代码。

## 本机启动

要求 Python 3.12、uv、Node.js 22.12+ 和 pnpm 11。正式技术/事件联合 Demo 必须先生成审核过的 Choice + 事件 `composite_snapshot`；仓库不会用随机数据或日期级事件补齐它。

```bash
cp .env.local.example .env.local
make install
# 按两个 runbook 生成并审核 composite_snapshot，再把内容 hash 写入 .env.local：
# docs/runbooks/choice-quant-api.md
# docs/runbooks/multi-source-events.md
make api
```

`.env.local` 必须指向同一个 composite 目录，并启用 `MARKET_DATA_PROFILE=composite_snapshot`、`EVENT_DATA_REQUIRED=true`。严格模式还要求干净工作树对应的精确 40 位 Git SHA；dirty 或手填伪 revision 不能通过 `/ready`。只需隔离调试 legacy fixture 时仍可运行 `make demo-data`，但其 `var/data`、日期级事件和历史结果不计入正式 Demo 验收。

后端启动后：

- API 文档：`http://127.0.0.1:8000/api/v1/docs`
- 存活：`http://127.0.0.1:8000/api/v1/health`
- 就绪：`http://127.0.0.1:8000/api/v1/ready`
- 能力：`http://127.0.0.1:8000/api/v1/capabilities`

另开终端验证真实 API：

```bash
./scripts/smoke.sh --require-backtest --as-of-date 2026-08-06
```

该 smoke 会识别 MACD 策略、提交任务、轮询到终态，并读取 summary、series 和 trades。

启动真实 H5：

```bash
cd web
pnpm install --frozen-lockfile
VITE_USE_MOCK=false VITE_API_BASE_URL= VITE_DATA_AS_OF_DATE=2026-08-06 pnpm dev
```

打开 `http://127.0.0.1:5173`。空的 `VITE_API_BASE_URL` 使用 Vite 同源代理，不依赖宽松 CORS。

浏览器自动验收：

```bash
.venv/bin/playwright install chromium
.venv/bin/python web/scripts/smoke_live.py --strategy technical
.venv/bin/python web/scripts/smoke_live.py --strategy event
```

Compose 路径见[本地运行手册](docs/runbooks/local-development.md)。

## 关键语义

- 任何有意义的输入都先被接住，但“理解了”不等于“可回测”。完整策略走原有快路；纯观点进入非执行的 `idea-route.v1`，只使用当前权威 A 股股票页作为价格代理，给出 2—3 个服务端固定模板候选。用户选择后仍要重新经过现有 Catalog、Schema、数据与时间门禁，并再次明确点击开始回测。
- 例如“我讨厌特朗普”可以被复述为负面观点并转成几个价格行为检验方向，但当前没有可靠的政治/主题事件快照时，不得生成“特朗普事件”或声称该观点导致当前股票涨跌；也不得由模型另猜一只股票。
- 技术指标按日线收盘确认；日线 MACD 金叉不能按同一收盘价成交。
- 只说“MACD”等裸指标、只有买入或只有卖出条件时，系统只集中澄清一次缺少的交易方向，不自动补成“金叉买、死叉卖”。
- 事件只能在 `available_at` 后触发。通用研究文件仍可把日期级记录按收盘后处理；
  Demo 严格模式只接受经验证的 `Asia/Shanghai` 秒级时间，日期级记录只审计、不交易。
- 技术日线信号在 15:00 收盘确认，下一可交易 session 使用已发布的日线开盘价代理尝试成交；日线 OHLCV 不能证明集合竞价逐笔成交。
- 秒级事件先加 1 秒处理延迟；`decision_ready_at <= 09:15` 时可使用当日已发布的日线开盘价代理，否则顺延下一交易 session。活动中的 `filledAt=09:30` 只是统一记录边界，不代表真实成交发生在 09:25 或 09:30。
- 日线开盘价代理的容量默认使用“上一已完成交易日成交量 × 参与率”；缺少该 PIT 观察时不成交，绝不使用当天收盘后才知道的全天量。
- `unlimited` 只在高级设置中显式开启，并写入运行证据；它不是缺数据时的自动兜底。
- 日线无法证明涨停队列位置；一字涨停买入和一字跌停卖出默认不假装成交。
- 其他涨跌停模式可以显式设置，但不会产生越过当日限制的价格。
- “完整年报正文中 AI 出现超过 5 次”当前只对冻结的可提取文本层做字面计数：先做 NFKC，ASCII 词用 token 边界且默认忽略大小写，中文按 literal 子串匹配；不扩同义词、不用 OCR，也不让 LLM 猜次数。只开放“超过/至少”这类下限条件；“少于/不超过/等于”会 fail closed，避免文本层漏字反而制造买入。摘要、英文/外文版、更正、修订、取消/撤回/撤销、更新和补充版本不进入该口径；同一报告期出现两个候选初始完整版本时 fail closed。抽取器与逐页 hash 可以证明冻结文本可重放，但单一 PDF 文本层不能自证语义全文完整，因此该能力仍不可用于生产。
- “首次买入成交后第 N 个 A 股交易日退出”以首次真实买入 fill 为锚点，成交日不计数，按 pinned A 股 session 固定目标日，并在第 N 个 session 使用已发布的日线开盘价代理尝试卖出；受停牌、跌停或容量限制时不假装成交，回测区间结束也不强制平仓。
- “大跌反弹”尚未发布选股模板时直接返回不支持，不向用户追问内部阈值。

## 数据与重放

正式 Demo 使用内容寻址 composite 目录：

```text
var/snapshots/composite/<sha256>/daily_ohlcv.parquet
var/snapshots/composite/<sha256>/signal_daily_ohlcv.parquet
var/snapshots/composite/<sha256>/corporate_actions.parquet
var/snapshots/composite/<sha256>/instrument_sessions.parquet
var/snapshots/composite/<sha256>/events.parquet
var/snapshots/composite/<sha256>/event_observations.parquet
var/snapshots/composite/<sha256>/snapshot_manifest.json
```

每次运行固定数据 SHA-256、Catalog、市场规则、策略、配置、引擎和精确 Git SHA。Worker 会重算身份；文件、配置或工作树身份被改动时拒绝执行。结果中的运行证据来自该不可变 RunManifest，不从页面草稿反推。

Choice 技术 Demo 使用独立的严格快照模式，强制同时提供不复权执行价格、后复权信号价格、
session 和内容寻址 manifest；配置与验收见 [Choice Quant API 本地接入](docs/runbooks/choice-quant-api.md)。

事件 Demo 使用东方财富公告为固定主源；同花顺 iFinD、RQData、Tushare 已有固定顺序的可插拔适配器，
但当前本机没有后三者的真实授权联网证据。最终中标和许可获批另外支持授权新闻记录或前向网页归档，
但搜索结果本身不能触发交易。所有来源
先生成独立事件快照，再与 Choice 技术快照组成 `composite_snapshot`。回测运行时不访问任何
供应商 API，详见[多源事件数据与回放](docs/runbooks/multi-source-events.md)和
[网页发现事件](docs/runbooks/web-discovered-events.md)。

公司行动按 `cn.a_share.timeline_entitlement_receivable_settlement.date_only_cash_after_close_conservative.v2` 的“登记日权益 → 除权日应收 → 派息/到账结算”进入策略和同期持有账户。现金派息来源只有日期而没有日内到账时间时，派息日交易时段内仍保持应收，收盘后才转为可用现金，最早下一交易日可用于下单；不得把日期解释成开盘前或盘中到账。股份仍按来源明确的到账日、上市日和可卖日盘前处理。现金分红按 `gross_research_no_withholding.v1` 税前总额记账，尚未模拟个人按持有期补税。账户股数始终为整数；送转计算出现不足 1 股权益尾数但没有最终整数登记证据时 fail closed，不默认现金补偿。配股默认 `decline_no_external_cash.v1` 已进入策略与 funded benchmark 账本：保留拒绝认购审计，不注入外部资金；配股认购、缴款、退款和上市可卖链仍未实现。同期持有政策为 `funded_buy_and_hold.same_execution_ledger.v1`。后复权价格只用于相对技术信号，不能直接成交或替代公司行动。完整字段和拒绝条件见[公司行动契约](docs/reference/corporate-actions-v1.md)。

当前真实五年运行身份为 `composite:1f26afb8b1223b656397abd5c99ce5a0ac9ed6593c3dbf7bdc46fb5a61bb9b8b`，当前代码的 strict loader pin 为 `snapshot:4bd9ae887af1cac159bf2d831313c73d839a313b11a746c015ea19fa3d5728cd`。它覆盖 `300059.SZ` 2021-02-07 至 2026-08-20，执行/信号/session 各 1,340 行、公司行动 9 条，并对五类定期报告固定 26 条 `vendor_observed + validated` 秒级观察（年报 6、半年报 5、季报 10、业绩预告 3、业绩快报 2）与 9 页/862 条公告查询证据。本地 `.env` 已指向该 Composite，运行时只读固定快照并禁止访问供应商网络；该层级为 `local-real-data-verified`，不等于最终 clean-SHA H5 验收。

该 Composite 的 26 条 observation 中 `document_text=0`。正文词频链路已在代码、夹具和 Mock 中落地，PDF 内嵌文本抽取实际复用 `pypdfium2`，但要做 Live 仍必须显式开启正文抽取、重新采集并发布新的内容寻址 Event v2 + Composite v2。

公网 API 已在较早 revision `4dc55904f38d4d26b6ed3e29fe8a729f529da66c` 上再次跑通技术策略 `run:b2778dc334cc4eb3b39ac9eab2efd54f` 与年报事件策略 `run:12fe45b6624249d5a6bba3458751cc98`，两者都引用上述 producer/slice 身份。它们是先前公网 API E2E 证据，不是当前工作树的 final clean SHA，也不替代公网 H5 Network/后端日志验收。

## 项目结构

```text
ashare_lab/
├── api/             HTTP 契约
├── application/     编译、提交、执行和报告用例
├── domain/          策略、信号、撮合、账户和绩效规则
├── ports/           外部能力边界
├── adapters/        Parquet、SQL、队列和中文解析
└── worker/          Worker 入口

web/                 H5
catalogs/            可执行目录与覆盖清单
contracts/           JSON Schema
tests/               单元、契约、架构、集成与 legacy 对照
docs/                验收、架构、数据契约和运行手册
astock_backtest/     legacy v1，仅用于迁移对照
```

依赖方向由测试固定为 `API / Worker / Adapters → Application → Domain + Ports`。新主链不直接调用 legacy 引擎。

## 质量检查

```bash
make check
cd web
pnpm typecheck
pnpm lint
pnpm test
pnpm build
```

GitHub Actions 还会检查迁移和后端/Web 容器构建。

## 生产边界

生产环境必须使用 `SESSION_REFERENCE_MODE=parquet` 和严格 composite；代码会在数据库连接前拒绝 research fallback。但“有严格 Adapter/账本”不等于“已有生产数据”。正式上线仍缺授权且经完整性验证的全 A 股历史状态、公司行动和多供应商事件覆盖，以及不可变数据归档、权限/审计、结果分页与 Docker/恢复实机演练。

详见：

- [Living Spec](docs/SPEC.md)
- [v1 验收](docs/00-acceptance.md)
- [技术方案](docs/architecture/01-technical-plan.md)
- [交付状态](docs/status.md)
- [运行手册](docs/runbooks/README.md)
- [Catalog 覆盖](docs/reference/catalog-coverage.md)
- [逐日证券状态契约](docs/reference/session-reference-v1.md)
- [公司行动与资金账户契约](docs/reference/corporate-actions-v1.md)
- [开源复用与胶水代码](docs/reference/reuse-first.md)

## License

MIT。历史回测结果不代表未来表现。
