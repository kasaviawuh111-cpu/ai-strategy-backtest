# v1 目标与验收标准

> 版本目标：可运行、可解释、可重放的 A 股单股日线 research demo。分钟/Tick/L2、股票池、组合、实盘交易和生产放行不属于本次完成定义。

## 1. 证据状态

本文件只使用以下状态，且允许同一能力同时拥有多个状态：

| 状态 | 含义 |
|---|---|
| `decided` | 产品或工程语义已经冻结，未必已实现 |
| `implemented` | 代码与契约已落地 |
| `fixture-tested` | 确定性夹具或自动化测试已通过；不说明真实数据状态 |
| `local-real-data-verified` | 本地真实物理数据、内容身份与正式 loader 校验/Pin 已通过；不等于 Live E2E 或生产授权 |
| `Mock-verified` | Mock 交互已在浏览器验证，只证明交互，不证明真实回测 |
| `Live-unverified` | 仍缺最终 clean SHA 下的真实 API、真实快照和浏览器闭环 |
| `production-unavailable` | 生产授权、存储、安全或运维条件尚不具备 |

目录数量、旧 v1 run、Mock 数字或测试全绿，都不能单独把 `Live-unverified` 改成最终验收通过。

## 2. 用户结果与范围

用户在一只股票页面说一句规则，系统能够：

```text
中文规则
→ 可解释、可编辑的受限 Strategy DSL
→ 版本化数据与执行假设
→ A 股约束回测
→ 收益、风险、基准与逐笔因果轨迹
```

当前范围：A 股、单股、只做多、日线；22 个透明日线指标；目标事件范围为年报、半年报、季报、业绩预告和业绩快报，年度报告 Mock 作为主演示；初始完整定期报告正文确定性词频；首次真实买入成交后第 N 个 A 股交易日退出；一次集中澄清；T+1、停牌、涨跌停、整手、费用、滑点、PIT 容量、部分成交、订单过期、公司行动、同约束买入持有基准；异步 API 与 H5/WebView。当前没有通过最新 strict loader 的事件 Composite，所以该事件范围仍是本轮验收目标，不是当前 Live 能力。

`/capabilities` 必须把 69 个 Catalog 可编译事件与当前 pinned strict snapshot 的逐码运行可用性分开；当前没有已通过最新公司行动 coverage 门禁的事件 Composite，因此不能把历史五类工件暴露为 `pinned_snapshot` 可回测。Mock 主演示只开放 `event.financial_results.annual_report`。69 个事件代码路径不能写成 69 类真实历史覆盖或 69 类均可回测。

产品入口支持任意合法 A 股股票页上下文；东方财富 `300059.SZ` 只作为 standalone 默认和固定 golden fixture。当前仅信任通过 A 股代码/交易所规则校验的规范 symbol；URL 的 `name/instrument_name` 只是未校验文本，不能作为证券身份或回测路由依据。按需准备链当前支持沪深主板、创业板和科创板；北交所只完成领域数量/涨跌停规则，因历史挂牌场所归属和跨源逐日停牌/reference 事实尚不能证明而 fail closed。某个新标的只有在行情、session、公司行动与所需事件覆盖齐全时才能 Live 运行；“能传入代码”不等于“该股票已有真实数据”。

## 3. 已完成的代码、自动化与本地数据门禁

### R1 策略、数据与执行

- [x] `implemented`：中文规则只编译到受限 `StrategySpec`，不执行任意代码。
- [x] `implemented / fixture-tested`：只说指标/事件名称、只有买入条件或只有卖出条件时，分别只集中澄清一次缺失的买入/卖出条件，不生成默认交易策略。
- [x] `fixture-tested`：22 个透明日线指标通过 Catalog/Schema、黄金值、预热和前缀不变性测试。
- [x] `implemented`：技术与事件策略共用 Signal → Decision → Order → Fill → Ledger → Analytics。
- [x] `implemented`：日线收盘信号不按同一收盘价成交；默认在下一可交易 session 使用已发布的日线开盘价代理尝试。
- [x] `implemented / fixture-tested`：事件加固定 1 秒处理延迟；不晚于 09:15 完成决策时可使用当日已发布的日线开盘价代理，否则顺延至下一可交易 session。日线 OHLCV 无法证明集合竞价逐笔成交，活动中的 `filledAt=09:30` 只是统一记录边界，并非声称实际在 09:25 或 09:30 成交。
- [x] `implemented / fixture-tested`：策略账户 09:15 和 funded benchmark 09:29 的预开盘 BUY 数量不读取当天未来 `bar.open`，统一按有效 `upper_limit` 做含费最坏资金预算；只改变未来开盘价不会改变委托数量。无有效上限时不创建订单，并返回 `pre_open_buy_sizing_upper_limit_unavailable`。
- [x] `implemented / fixture-tested`：同一 session 的事件因果证据只使用最早可得时点当时已可见的观察，较晚观察不得倒灌进较早信号。
- [x] `implemented`：开盘容量使用上一已完成交易日成交量代理；缺失时不成交，不读取当日全天量。
- [x] `implemented`：T+1、停牌、涨跌停、整手、费用、滑点、部分成交和订单过期进入统一撮合。
- [x] `implemented`：策略与同期持有使用 `funded_buy_and_hold.same_execution_ledger.v1`。
- [x] `implemented`：公司行动使用 `cn.a_share.timeline_entitlement_receivable_settlement.date_only_cash_after_close_conservative.v2`；日期级现金派息收盘后才转可用。
- [x] `implemented`：税前现金分红使用 `gross_research_no_withholding.v1`；未实现个人持有期税制。
- [x] `implemented`：账户只保存整数股；不足 1 股权益尾数缺最终整数登记证据时拒绝，非整手整数余额可以完整卖出。
- [x] `implemented / fixture-tested`：配股默认 `decline_no_external_cash.v1`，记录拒绝认购，不注入外部资金、不改变账户经济状态；策略与同期持有使用同一政策。未来认购必须另设显式版本化政策。
- [x] `implemented / fixture-tested`：`instrument_sessions.parquet` 已升级到 v2 的 `minimum_buy_quantity + buy_quantity_increment`；沪深主板/创业板为 100/100，科创板为 200/1，北交所领域规则为 100/1。部分成交可形成非整手整数股，卖出可清掉任意整数余额。
- [x] `implemented / fixture-tested`：每张订单仍为 DAY；简单日线边沿信号最多 3 个交易 session，持续状态每个 session 重新计算且单次有效，复合条件因无法安全逐日重建完整语义而单次有效。事件因尚无点时修订/撤回失效链也单次有效，不跨日重试。分钟信号仍未实现。
- [x] `implemented / fixture-tested`：正文词频只接受冻结在 Event/Composite v2 中的初始完整主文档及逐页可重放文本证据。正文先做 NFKC；ASCII 词按 token 边界且默认不区分大小写，中文按 literal 子串匹配；只开放 `gt/gte`，`eq/lt/lte` fail closed；不展开同义词，不调用 OCR，不让 LLM 估算。摘要、英文/外文版、更正、修订、取消/撤回/撤销、更新和补充版本均排除；同一事件码和报告期出现两个候选初始完整版本时整批 fail closed。runtime、Composite 发布和 strict loader pin/readiness 复用同一 validator 校验 source format、抽取器身份/版本、连续页码、逐页 hash/字符数、源文档 hash 与正文重建，坏正文在 Worker 前拒绝。现有 `complete_text_layer` 是历史 wire value，只能证明每个 provider page 有非空嵌入文本并通过抽取器/逐页 hash 重放，不能证明语义全文完整；生产状态仍为 unavailable。
- [x] `implemented / fixture-tested`：持有期退出以首次真实买入 fill 为锚点，成交 session 不计数，按运行时 pinned A 股交易 session 固定第 N 日，并在目标 session 使用日线开盘价代理尝试卖出；受停牌、跌停或容量限制时继续服从统一撮合，不虚构成交，区间结束不强制平仓。
- [x] `implemented / fixture-tested / Live-unverified`：自然语言采用“确定性快路 + Vibe 风格受限 JSON CandidateAst + 服务端硬校验”；最多 3 个候选、置信度门槛、provider fail closed 与一次集中澄清已有夹具验证。
- [ ] `Live-unverified`：尚未配置正式模型 transport、bootstrap、完整 provenance 与真实 API/H5；当前不能宣称理解任意表达。
- [x] `implemented / fixture-tested / Live-unverified`：东方财富 Push2 日线 adapter 按固定 AKShare commit 的请求协议实现，AKShare 不作为运行依赖；A 股身份、raw hash、量纲、区间、session 和公司行动交叉校验已有夹具。
- [ ] `Live-unverified`：Push2 直连被远端断开，尚无真实 batch、不可变 snapshot 或生产授权；不得与只作研究的 Push2 主力资金序列混为一谈。
- [x] `implemented`：预设执行压力场景按完整配置 hash 去重；默认 base 与 5% 参与率相同时只保留一份结果，不挑最优场景。

### R2 v2 本地真实数据与校验

- [x] `implemented / fixture-tested`：技术策略按需准备也发布显式 `no_event_required` Event v2 并组成 Composite v2；未查询事件供应商时不得记为“查询 0 条”，且该快照不能满足事件策略。
- [x] `implemented / fixture-tested`：运行证据同时保留 `dataSchemaVersion=local-parquet.market-data.v3` 与 `producerSnapshotSchemaVersion=ashare-lab.composite-research-snapshot.v2`，Worker 对两层身份分别 fail-closed 重验。

- [ ] `Live-unverified`：当前没有可被最新 strict loader 接受的 Event v2 + Composite v2。历史 `events:f22b9a…a1171fef` / `composite:1b3dbee…f5a6f053` 及其 expanded/user pins 因公司行动 coverage 不完整而被当前 loader 拒绝，只作迁移审计，不属于当前验收身份。
- [x] 历史数据记录（不属于当前状态）：上述旧事件工件曾包含五类定期报告的 27 条 `vendor_observed + validated` 秒级观察（3/2/6/5/11），并保留 2021-02-07 至 2026-08-20 的 9 页/862 条公告查询 hash；这些数据切片事实不能替代新 strict Composite。
- [x] `local-real-data-verified`：单独 Choice 用户区间快照 `choice:a2eebf97b7c2ed4f2467ca6f91a3e95f48ed609c4026b440a16e7a11a4a23b4f` 覆盖 `300059.SZ` 2021-08-06 至 2026-08-06，当前 loader pin 为 `snapshot:d3f5d91ff0ddd688c0f6ef58c18500f9435f86433b572cffa21296277ef42d66`。它不覆盖 expanded 区间且不包含事件，不能替代 Composite 或 Live E2E。
- [ ] `Live-unverified`：clean SHA 下的新 exact 采集已完成 BaoStock 会话参考、东方财富公司行动并成功登录 Choice，但不复权日线在有限重试后返回 `10002004 network connection closed when recv`；未生成新 Choice/Event/Composite、strict pin 或 run ID。
- [x] `implemented / fixture-tested`：mixed registry 可用 `producer_snapshot_id` 显式选择目标 Composite v2，只校验该目标；旧 v1 不再毒化无关 v2，目标自身不满足当前 coverage 时仍必须拒绝。
- [x] `implemented / fixture-tested`：on-demand fresh submission 默认按请求重采并发布内容寻址 producer snapshot；携带 expected snapshot identity 的 Worker replay 只从本地 registry 精确 pin，不调用采集器，目标缺失或 identity/checksum 不一致时 fail closed。
- [x] `implemented / fixture-tested`：历史 session 通过 BaoStock 年度有界查询取得 `preclose/tradestatus/isST`，并逐日与 Choice `PRECLOSE/TRADESTATUS` 精确交叉校验；Choice 交易日历长区间按自然年有界分块，每批显式设置 30 秒接收超时，后复权信号序列不分块。
- [x] `implemented / fixture-tested`：公司行动严格混合覆盖以东方财富对现金分红/送转/配股的逐类正向查询，加上完整股本变动历史对拆股/缩股的 0 候选负向证明组成；未知变动原因、正向拆并股候选、分页/0 结果证据不全或逐类计数不一致均拒绝发布。
- [x] `implemented / fixture-tested`：历史现金派息日不依赖 `BonusFinancing.PageAjax` 的最近窗口；缺失时以动作主表行和同日官方实施公告做证券、报告期、登记日、除权日、税前每 10 股金额及唯一明确支付日的严格 join。只完整分页后筛 provider column 再取正文；无候选、冲突、双候选或只有除权日均拒绝。
- [x] `implemented / fixture-tested`：Choice 幂等 `tradedates/csd` 读取只对明确瞬态网络码 `10002004` 做最多 3 次有限重试；登录只对明确网络码 `10002002/10002004` 做相同上限的有限重试；成功所需 attempts 写入 request audit。权限码、字段/数据错误、非 provider 返回和 SDK 异常均不重试。
- [x] `local-real-data-verified`：dirty worktree 下 fresh 五年 Choice 快照 `choice:a2eebf97b7c2ed4f2467ca6f91a3e95f48ed609c4026b440a16e7a11a4a23b4f` 覆盖 `300059.SZ` 2021-08-06 至 2026-08-06，执行/信号/session 各 1,211 行，公司行动 7 条（现金 5、送转 2，配股 0，拆股/缩股候选均 0）；后复权前缀 969 行差异 0、状态 `passed`。该项不是 clean-SHA Live E2E 或全 A 股证明。
- [x] `implemented / fixture-tested / Live-unverified`：`event.repurchase_capital.repurchase_change` 已有东方财富细栏目映射、编译和覆盖夹具，但尚未生成并验证包含该码的新 live Event/Composite v2，不能返回 pinned-snapshot 可回测。
- [ ] `Live-unverified`：历史五事件工件的 `document_text` 观察数为 0，当前又没有有效 strict Composite。正文策略必须使用显式 opt-in 正文抽取重新采集并发布新的内容寻址 Event v2 + Composite v2；在此之前 loader 对需要正文的策略必须 fail closed。
- [ ] `production-unavailable`：东方财富公告页原始响应字节尚未归档；当前只保存逐页 hash，不能声称不可变原始存储已完成。
- [ ] `Live-unverified`：历史五类工件的 27 条本地真实观察只是数据切片，尚未达到每个事件码的 Demo 门禁：至少 30 个独立真实正例、30 个困难负例（至少 10 个发行人、3 个年份，覆盖更正/撤回/错股/候选/传闻）和 10 条完整因果 E2E。生产声明至少 60+60，并要求独立复核与关键时点/错股零容忍。

### R3 API、工程与 Mock H5

- [x] `implemented`：草稿创建/修订、回测提交/取消/状态/结果接口齐全；任务与运行指纹具备幂等和并发保护。
- [x] `implemented`：`/health`、`/ready`、`/version`、`/capabilities` 已发布；strict 运行对 dirty/伪 revision fail closed。
- [x] `implemented / fixture-tested`：只有携带受支持 `hashSchemaVersion`、且状态/摘要/曲线/交易读取及完成态幂等重放对完整持久化 bundle 重算一致的 `resultHash` 才构成完整性证据。legacy 无 schema 且无 hash 可按无证据路径读取并返回 `resultHash=null`；legacy 带 hash、未知 schema、缺失 modern hash 或内容不一致均 fail closed。
- [x] `implemented / fixture-tested`：需要 `document_text` 的策略若遇到扫描页无可提取文本、页数不符等结构化正文质量失败，返回可操作的 `422 event_document_text_data_unavailable`；技术策略或普通事件的 incomplete 仍为 503，正文供应商网络、SDK、解析器、子进程和超时故障也保持 503，不按异常文案猜测类型。
- [x] `fixture-tested`：后端自动化、Ruff/format、Pyright、Catalog、schema export、Alembic 与 diff check 已有本轮 fresh 记录；精确测试数不在本文档中固化，最终以 clean integration SHA 下同批重跑的证据包为准。
- [x] `fixture-tested`：前端唯一入口仍为 `src/main.tsx → src/App.tsx`；TypeScript、ESLint、Vitest、diff check 与 production build 已有本轮 fresh 记录，精确数字留待最终 clean integration SHA 同批刷新。
- [x] `Mock-verified`：技术旅程覆盖 320/390/768/1280；年度报告、unsupported、一次澄清覆盖 390；所有已执行旅程无横向溢出，console/page error 为 0。
- [x] `Mock-verified`：Mock 始终显示“演示数据”，不会显示“身份已验证”，事件演示不伪造正式 snapshot 身份。
- [x] `Mock-verified`：年度报告正文词频 + 首次实际成交后 3 个 A 股交易日退出可以走完策略确认卡；Mock 只证明交互与文案，不证明已获取真实正文、算出真实次数或完成真实成交。
- [x] `implemented / fixture-tested`：结果 UI 展示 `timeQuality/timeSemantics`，统一说明“日线开盘价代理（时间非精确）”；Mock 委托为 09:15，09:30 只作记录边界；执行假设以小白标签展示 edge=3、event=1、state=1、composite=1。
- [x] `implemented`：前端禁止盘中策略运行；“MACD 收盘/盘中”澄清目前只属于 Mock 交互，Live v2 不作泛化承诺。

上表是当前工作树的阶段证据；完整工程门禁尚未在最终 integration SHA 同批重跑，因此不能据此勾选 Live 验收。

## 4. 最终 Live 验收：当前全部未完成

- [ ] `Live-unverified`：形成 clean integration commit，并记录精确 40 位 Git SHA。
- [ ] `Live-unverified`：在该 SHA 下以 Composite v2 启动 strict API，`/ready` 全部通过。
- [ ] `Live-unverified`：固定用户区间 2021-08-06 至 2026-08-06；快照额外覆盖 2021-02-07 至 2026-08-20 的预热与结算范围，两者不得混写。
- [ ] `Live-unverified`：以同一 snapshot、1,000,000 元、同一执行配置完成 MACD 技术策略 run。
- [ ] `Live-unverified`：以同一 snapshot、1,000,000 元、年度报告秒级事件完成事件买入 + MACD 死叉卖出 run。
- [ ] `Live-unverified`：两条 run 均保存 strategy/catalog/config/data/engine/Git identity、summary、series、trades、受支持 `hashSchemaVersion` 与经四个读取接口重算一致的 result hash，以及去重后的压力场景。
- [ ] `Live-unverified`：H5 显式 `VITE_USE_MOCK=false`，在 320/390/768/1280 完成技术与年报双旅程，控制台和 page error 为 0。
- [ ] `Live-unverified`：Live 缺股票澄清能写入 `instrument_context`；不得把 Mock 的 MACD 时间澄清冒充 Live 能力。
- [ ] `Live-unverified`：若把正文词频策略纳入 Live 演示，必须先 opt-in 重采正文、发布新 Event/Composite v2，并保存正文 hash、抽取元数据、策略/运行身份和真实因果轨迹；当前没有可满足该项的 strict pins。
- [ ] `Live-unverified`：在最终 SHA 上复跑后端、前端、Catalog、Migration、diff 和 strict smoke 全部门禁。

在这些项目全部完成前，文档不得填写新的 run ID、收益、回撤或“最终验收通过”。旧 v1 run 只作迁移证据。

## 5. 分钟与生产边界

- [x] `implemented`：分钟领域模型、Choice `cmc` 解码/探针、双价格分钟快照发布器和读取器已存在。
- [x] `fixture-tested`：分钟数据层有契约、校验和篡改拒绝测试。
- [ ] `Live-unverified`：当前实网返回 `10000017 overseas ip is restricted`，没有可验收的真实分钟快照。
- [ ] `Live-unverified`：没有 minute DSL、盘中信号 runtime、minute matcher，前端继续禁用分钟回测。
- [ ] `production-unavailable`：公司授权数据账号、全 A 股逐日状态（尤其北交所历史链）、原始数据/结果不可变存储、认证、用户归属、审计、限流、结果分页、备份恢复、生产部署演练未完成。

这些条件完成前，项目只能称 research demo，不能用于实盘决策或描述为生产系统。
