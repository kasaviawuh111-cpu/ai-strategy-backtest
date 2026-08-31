# A 股单股自然语言策略回测

一个可运行的 research demo：用户说一句 A 股交易规则，系统把它转换成受限、可编辑的 Strategy DSL，再用统一的 A 股日线引擎回测。

```text
一句中文
→ 策略卡
→ 异步回测
→ 收益 / 风险 / 净值
→ 信号 / 委托 / 成交 / 未成交原因
```

当前不是生产交易系统，也不构成投资建议。

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
| 开源胶水 | `implemented / fixture-tested / Live-unverified`：Vibe 受限候选、MOSS/pandas 指标差分、Vibe/mootdx 日线接口和 AKShare-shaped 东方财富 Push2 日线请求协议已落地；正式模型/client 未配置，指标未切默认，mootdx 商业与数据条款待审，Push2 直连未取得真实 batch 或 snapshot，公共未文档化接口不视为生产授权 |
| 技术信号 | 22 个日线定义：MA/EMA、双均线、MACD、RSI、KDJ、CCI、BOLL、BBI、EMA 乖离、区间涨跌幅、阶段新高、连涨、振幅、成交量/成交额、相对成交量、量价确认、OBV、确认式量价背离、阶段趋势 |
| 事件信号 | 69 项只达到 Catalog/编译/Adapter/运行时代码路径；`/capabilities` 再按当前 pinned strict snapshot 逐码给出运行可用性。历史工件曾包含五类定期报告的 27 条东方财富秒级观察，但已不满足当前公司行动 coverage 门禁；当前没有可用于 strict 重放的新 Event/Composite |
| 报告正文词频 | `implemented / fixture-tested / Mock-verified / Live-unverified`：可对“初始完整定期报告正文”做确定性词频，并与“首次实际买入成交后第 N 个 A 股交易日退出”组合；历史五事件工件的 `document_text` 为 0，且当前无新 strict Composite，尚不能 Live 运行 |
| A 股规则 | T+1、停牌、涨跌停、手数、费用、滑点、PIT 容量、部分成交、订单过期 |
| 资金账户 | 策略与同期持有同资金、同撮合；分红/送转按权益、应收、结算三阶段记账 |
| 结果 | 摘要、净值、因果活动、秒级事件来源、运行证据、版本化完整结果哈希、预设执行敏感性场景 |
| 交互 | React H5/WebView，支持编辑参数、区间、资金和成本 |
| 运行 | 本地 SQLite/thread；部署材料为 PostgreSQL/Redis/RQ/Docker |

覆盖目录还登记了 180 个 A 股单股指标/数据项和 160 个 A 股相关事件，但登记不等于可执行。最终以
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

- v1 技术指标按日线收盘确认；日线 MACD 金叉不能按同一收盘价成交。
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

公司行动按 `cn.a_share.timeline_entitlement_receivable_settlement.date_only_cash_after_close_conservative.v2` 的“登记日权益 → 除权日应收 → 派息/到账结算”进入策略和同期持有账户。现金派息来源只有日期而没有日内到账时间时，派息日交易时段内仍保持应收，收盘后才转为可用现金，最早下一交易日可用于下单；不得把日期解释成开盘前或盘中到账。股份仍按来源明确的到账日、上市日和可卖日盘前处理。现金分红按 `gross_research_no_withholding.v1` 税前总额记账，尚未模拟个人按持有期补税。账户股数始终为整数；送转计算出现不足 1 股权益尾数但没有最终整数登记证据时 fail closed，不默认现金补偿。配股当前代码仍 fail closed，产品默认已决定为“不认购、不注入外部资金”，尚待账本实现。同期持有政策为 `funded_buy_and_hold.same_execution_ledger.v1`。后复权价格只用于相对技术信号，不能直接成交或替代公司行动。完整字段和拒绝条件见[公司行动契约](docs/reference/corporate-actions-v1.md)。

当前只有一份单独 Choice 用户区间快照还能通过正式 loader：
`choice:a2eebf97b7c2ed4f2467ca6f91a3e95f48ed609c4026b440a16e7a11a4a23b4f`。它覆盖
`300059.SZ` 2021-08-06 至 2026-08-06，在当前 loader 下 pin 为
`snapshot:d3f5d91ff0ddd688c0f6ef58c18500f9435f86433b572cffa21296277ef42d66`；该证据仅为
`local-real-data-verified`。它不覆盖 2021-02-07 至 2026-08-20 的预热/结算 expanded 区间，
也没有事件数据，不能替代 Event v2 + Composite v2 或用于最终双策略 Live 验收。

历史工件曾固定五类定期报告的 27 条秒级观察和 9 页/862 条公告查询证据，
但其 Composite 的公司行动 coverage 仅是旧口径。当前 strict loader 会以
`strict corporate-action coverage must prove all categories` 拒绝该组快照和旧 pins；它们只保留为历史本地真实工件，
不是当前可用身份。历史 Event 中 `document_text` 观察为 0。正文词频链路已在代码、夹具和 Mock 中落地，
PDF 内嵌文本抽取实际复用 `pypdfium2`，但要做 Live 仍必须显式开启正文抽取，重新采集并发布新的内容寻址
Event v2 + Composite v2。clean SHA `0e9025779791d988f8451ef0ce68b4e2e10f008f` 下的最新精确采集已通过
BaoStock 会话参考、东方财富公司行动和 Choice 登录，但不复权日线在有限重试后返回
`10002004 network connection closed when recv`。因此尚无新 exact Choice/Event/Composite、run ID 或 Live 结果。

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
