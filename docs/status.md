# 交付状态

> 审计日期：2026-08-30
> 当前结论：后端达到 `implemented / fixture-tested`，前端达到 `implemented / fixture-tested / Mock-verified`。单独 `300059.SZ` Choice 用户区间快照为 `local-real-data-verified`，但不覆盖 expanded 区间且不包含事件。旧五类定期报告 Event/Composite v2 因公司行动 coverage 不满足最新 strict loader，已对当前验收失效。clean SHA 下的新 exact 采集已通过 BaoStock 会话参考、东方财富公司行动和 Choice 登录，但在不复权日线读取时被 `10002004 network connection closed when recv` 阻断，所以当前没有可用的 strict Event/Composite、双 run 或真实 H5，整体仍是 `Live-unverified`。生产条件为 `production-unavailable`。

状态含义统一见 [Living Spec 1.2](SPEC.md#12-状态词)。旧 v1 run、Mock 数字、目录数量或当前 dirty 工作树测试全绿，都不能改写上述结论。

## 1. 当前状态总览

| 能力 | 事实状态 | 已有证据 | 尚缺证据 |
|---|---|---|---|
| 自然语言胶水 | `implemented / fixture-tested / Live-unverified` | 确定性解析器仍为默认快路；Vibe 风格的严格 JSON CandidateAst adapter、最多 3 个候选、置信度门槛和 fail-closed 错误已有夹具 | 正式模型 transport、bootstrap、完整 provenance 与真实 API/H5；不能称任意表达已支持 |
| 开源指标与采集 adapter | `implemented / fixture-tested / Live-unverified` | MOSS/pandas `IndicatorBackend` 的 EMA/MACD 已对拍，RSI 明确不兼容；Vibe/mootdx 接口已用 fake client 验证；Push2 请求协议已按固定 AKShare commit 补齐 | 指标未成为 Live 默认；mootdx 商业与数据条款待审；2026-08-30 Push2 直连被远端断开，未发布新快照 |
| 日线策略与 A 股撮合 | `implemented / fixture-tested / Live-unverified` | 22 个透明指标；PIT 容量、T+1、停牌、涨跌停、费用、部分成交、过期均有代码和测试 | 最终 clean SHA 技术 run |
| 事件策略 | `implemented / fixture-tested / Live-unverified` | 五类定期报告的采集/覆盖门禁、秒级观察和统一执行主链已实现；历史本地事件切片仍可审计 | 新 strict Event/Composite、clean-SHA 事件 run 与 Live H5 |
| 定期报告正文词频 + 持有期退出 | `implemented / fixture-tested / Mock-verified / Live-unverified` | 初始完整正文门禁、确定性词频、首次实际成交后第 N 个 A 股交易日退出及 Mock 策略卡 | 历史五事件工件 `document_text=0`；需 opt-in 重采、发布新 v2 并跑 Live |
| 公司行动与基准 | `implemented / fixture-tested / local-real-data-verified / Live-unverified` | 三阶段账本、配股默认不认购、整数股/非整手余额、同资金同约束买入持有；`300059.SZ` fresh 五年公司行动/session 快照 | 最终 clean-SHA strict 双 run 对账；个人税制仍受限 |
| H5 | `implemented / Mock-verified / Live-unverified` | 唯一入口；规范 A 股 symbol 上下文；URL 展示名不作为可信身份；技术 Mock 四档，年报/unsupported/一次澄清为 390px | `VITE_USE_MOCK=false` 四档真实 API 旅程；证券主数据名称解析 |
| 分钟数据层 | `implemented / fixture-tested / Live-unverified` | 分钟数据模型、Choice `cmc` 解码/探针、双价格发布器/读取器 | 真实分钟快照及授权网络验证 |
| MACD 盘中策略链 | `decided / Live-unverified` | 产品语义和默认执行政策已冻结 | minute DSL、信号 runtime、matcher；前端仍禁用 |
| 生产 | `production-unavailable` | 只有研究 Demo 与部署材料 | 公司授权、不可变存储、安全、审计、运维与 A 股全标的覆盖 |

## 2. 当前真实数据证据

| 证据 | 当前值 | 事实层级 |
|---|---|---|
| 当前 strict Event/Composite | 无 | 旧 producer 组因公司行动 coverage 不完整被当前 loader 拒绝；`Live-unverified` |
| 历史 Event/Composite | `events:f22b9a…a1171fef` / `composite:1b3dbee…f5a6f053` 及旧 expanded/user pins | 仅迁移审计；不是当前有效身份 |
| 历史五类事件切片 | 27 条东方财富 `vendor_observed + validated` 秒级观察；9 页/862 条公告查询 hash | 数据切片可审计，不能替代新 strict coverage |
| 历史事件正文 | `document_text=0` | 正文词频仍为 `Live-unverified` |
| 当前单独 Choice 快照 | `choice:a2eebf97b7c2ed4f2467ca6f91a3e95f48ed609c4026b440a16e7a11a4a23b4f` | 用户区间 `local-real-data-verified` |
| 当前 Choice loader pin | `snapshot:d3f5d91ff0ddd688c0f6ef58c18500f9435f86433b572cffa21296277ef42d66` | 仅 2021-08-06 至 2026-08-06；不包含事件，扩到 2021-02-07 至 2026-08-20 会超出 coverage |
| fresh 五年 Choice 公司行动链 | `choice:a2eebf97b7c2ed4f2467ca6f91a3e95f48ed609c4026b440a16e7a11a4a23b4f`；`300059.SZ`，2021-08-06 至 2026-08-06；执行/信号/session 各 1,211 行；公司行动 7 条 | dirty worktree 下 `local-real-data-verified`，不是 clean-SHA Live E2E |
| 五年公司行动分类 | 现金分红 5、送转 2、配股 0；拆股/缩股候选均 0；后复权前缀 969 行差异 0、`passed` | 东方财富逐类正向覆盖 + 拆并股负向证明；仅此股票/区间 |
| 最新 exact API 尝试 | clean SHA `0e902577…`；BaoStock 与东方财富公司行动准备成功；Choice 单次登录返回成功码 0；不复权日线在有限重试后返回 `10002004 network connection closed when recv` | 未发布新 Choice/Event/Composite，未生成 run ID；不能算 E2E 成功 |

用户固定回测区间为 2021-08-06 至 2026-08-06；上述更宽区间用于指标预热和结算扩展。两种区间不能混写成同一概念。
历史记录中的 862 条是 expanded 区间扫描到的全部公告，不是 862 条策略事件；五类定期报告观察共 27 条。它们均不是当前 strict 运行身份。

产品普通默认是近 5 年。2017 年至今只在可信 `eiTime/display_time`、正文证据、秒级时间质量和完整区间覆盖都合格时条件开放；不先承诺完整 10 年。2017 年以前存在历史回填风险，2016 段需要更权威来源交叉证明。

69 个事件项当前只表示 Catalog、自然语言编译和目录代码路径存在。它们不表示 69 类都在本快照中，不表示都经过真实 runtime，更不表示完成 E2E。

`event.repurchase_capital.repurchase_change` 已完成东方财富细栏目映射、自然语言编译、查询覆盖/分页/0 结果夹具和 strict 代码门禁，状态仅为 `implemented / fixture-tested / Live-unverified`；当前没有通过最新 loader 的事件 Composite，也没有该码的新 live snapshot coverage，不能把它算入历史五类事件切片。

`/capabilities` 的 Catalog 发布状态、按需准备能力与当前快照运行能力是三个概念：只有当前 pinned strict snapshot 对某事件码具备 acquisition coverage 时，才能返回 `backtest_available=true + availability_scope=pinned_snapshot`。on-demand 在没有具体股票与区间快照时只能返回 `preparation_available=true + availability_scope=request_preparation`；它表示 fresh submission 将重新采集、校验、发布内容寻址快照并精确 pin，不表示当前已有数据，也不是复用可变缓存。携带 expected snapshot identity 的 Worker replay 不调用采集器，只读本地 registry，目标缺失或身份不符时 fail closed。五类只是历史本地事件切片事实，不是当前可用快照，也不是硬编码的永久白名单。

Registry 显式选择语义仍为 `implemented / fixture-tested`：只校验指定目标，旧 v1 不毒化无关 v2，但目标自身不满足最新 coverage 时必须拒绝。旧快照未删除，不代表仍然有效。

正文不是默认采集的隐式副作用。当前代码通过薄 adapter 实际复用 `pypdfium2` 抽取 PDF 内嵌文本，但没有 OCR；只有显式开启正文抽取、通过初始完整版本门禁并重新发布内容寻址 Event/Composite v2，才能让需要正文的策略进入 strict loader。历史五事件工件为 0 条 `document_text`，当前没有新 strict pins，不能用代码或 Mock 状态替代 Live 数据。

历史五类共 27 条本地真实观察只证明该股票、区间和数据切片，尚未达到逐事件码质量门禁。工程 Demo 每类至少需要 30 个独立真实正例、30 个困难负例（至少 10 个发行人、3 个年份，覆盖更正/撤回/错股/候选/传闻）和 10 条完整因果 E2E；生产声明至少 60+60，并要求独立复核、关键时点和错股零容忍。

## 3. 当前 fresh 工程证据

下表记录本次集成内容树可复核的 fresh 结果。更早的测试数只作历史阶段证据，不再作为当前门禁；最终提交后的同批重跑结果与 SHA 由交付报告记录，本文不预填。

| 门禁 | Fresh 结果 | 事实层级 |
|---|---|---|
| pytest | 当前工作树已有 fresh 通过记录；精确数字留待最终 integration SHA 同批刷新 | `fixture-tested / Live-unverified` |
| Ruff / Pyright / Catalog | Makefile 定义范围 `ashare_lab tests scripts alembic` 的 Ruff check/format fresh 通过；Pyright 0 errors/0 warnings；Catalog check 通过。对整个 checkout 运行 `ruff check .` 仍会命中旧 `astock_backtest`/examples 等不在 Makefile 门禁内的 184 项历史债务 | `fixture-tested / Live-unverified` |
| Alembic | 临时空 SQLite fresh upgrade 到 `20260828_0001 (head)`，current/check 通过且无新增 upgrade operations；最终 integration SHA 仍须重跑 | `fixture-tested / Live-unverified` |
| Web 类型、规范、测试与构建 | TypeScript、ESLint、Vitest、diff check 与 production build 已有 fresh 通过记录；精确数字留待最终 SHA 刷新 | `fixture-tested` |
| Mock 浏览器 | 技术旅程覆盖 320/390/768/1280；年报、unsupported、一次澄清覆盖 390；所有已执行旅程无横向溢出，console/page error 0 | `Mock-verified` |
| strict dirty gate | dirty/伪 revision 会 fail closed | `fixture-tested` |

`pnpm` 受管 wrapper 的本地 store 提示不作为质量结论；上述 Web 门禁使用当前已安装的直接 binaries 得到。构建和测试通过也不替代真实 API 浏览器验收。

## 4. 已冻结且已实现的执行口径

- 默认资金 1,000,000 元；资金、费用、参与率和容量模式进入策略与运行身份。
- 日线事件加入固定 1 秒处理延迟；不晚于 09:15 完成决策时可使用当日已发布的日线开盘价代理，否则顺延下一可交易 session。日线 OHLCV 不证明集合竞价逐笔成交；`filledAt=09:30` 只是统一记录边界，不是实际 09:25/09:30 成交声明。容量使用上一已完成交易日成交量代理，不读取当日全天成交量；缺观察时不成交，`unlimited` 只能显式选择。
- 策略账户 09:15 与 funded benchmark 09:29 的预开盘 BUY 数量均只按当日 `upper_limit` 做含费最坏资金预算，不读取未来 `bar.open`；`bar.open` 只供 09:30 开盘价代理撮合。无有效上限时两边都以 `pre_open_buy_sizing_upper_limit_unavailable` fail closed，且不会创建订单。
- `BonusFinancing.PageAjax` 只作为最近派息日补充，不能证明全历史；较早现金分红以完整分页的 `RPT_SHAREBONUS_DET` 动作行为锚，再从同一公告日官方“分配方案实施”栏目下载实施公告，严格绑定证券、报告期、登记日、除权日、税前每 10 股金额和正文唯一明确支付日。缺失、冲突或双候选拒绝，绝不以除权日推断派息日。
- 结果 UI 已展示 `timeQuality/timeSemantics` 并统一说明“日线开盘价代理（时间非精确）”；Mock 委托时间为 09:15，09:30 只作记录边界。执行假设用小白标签展示 edge=3、event=1、state=1、composite=1。
- 同期持有使用 `funded_buy_and_hold.same_execution_ledger.v1`，与策略同资金、同费用、同成交和同公司行动。
- 技术策略按需准备也固定为 `Event v2(no_event_required) → Composite v2`；该声明不伪装成供应商零结果，也不能满足事件请求。结果中的 `dataSchemaVersion` 表示本次 loader slice，`producerSnapshotSchemaVersion` 表示上游 Composite v2，Worker 分别核验。
- 公司行动使用 `cn.a_share.timeline_entitlement_receivable_settlement.date_only_cash_after_close_conservative.v2`；日期级现金派息在派息日收盘后才转可用。
- 现金分红使用 `gross_research_no_withholding.v1`，未模拟个人持有期补税。
- 账户只保存整数股；不足 1 股权益尾数缺最终登记分配证据时拒绝，不默认现金补偿；非整手整数余额可以完整卖出。配股默认 `decline_no_external_cash.v1` 已实现：保留拒绝认购审计，不注入外部现金、不改变账户经济状态。
- session schema 已升级为 `parquet-instrument-sessions.v2`：沪深主板/创业板使用最低 100 股且按 100 股递增，科创板使用最低 200 股后按 1 股递增；北交所领域规则为 100/1，但完整历史数据链仍 fail closed。部分成交可形成非整手整数持仓，卖出可清掉任意整数余额。
- 历史 ST/停牌/preclose 由 BaoStock 按年有界查询，并逐日与 Choice 的交易状态/前收精确交叉校验；Choice 交易日历长区间按自然年有界分块并显式设置 30 秒接收超时，后复权信号序列保持整段查询。
- 执行压力场景按完整 config hash 去重；默认 base 与 5% 参与率相同时不会重复输出。
- strict 事件只接受 `validated + exact/vendor_observed` 秒级记录；日期级、分钟级、冲突或未验证观察不能交易。
- 单笔订单为 DAY。简单日线边沿最多 3 个交易 session；持续状态每 session 重算且单次有效；复合条件无法安全逐日重建完整语义时单次有效；事件因缺少点时修订/撤回失效链也单次有效，不跨日重试。分钟有效期和分钟策略链仍未实现。
- 定期报告正文词频只对冻结的初始完整主文档及可重放逐页文本证据执行。正文做 NFKC；ASCII 词按 token 边界且默认不区分大小写，中文按 literal 子串；只开放 `gt/gte`，`eq/lt/lte` fail closed；无同义词扩展、无 OCR、无 LLM 计数。摘要、英文/外文版、更正、修订、取消/撤回/撤销、更新和补充版本排除；同一报告期两个候选初始完整版本 fail closed。单一 PDF 嵌入文本层不能证明语义全文完整，当前生产状态仍 unavailable。
- 正文 source format、抽取器身份/版本、连续页码、逐页 hash/字符数、源文档 hash 和逐页重建总文本由同一 validator 在 runtime、Composite 发布和 strict loader pin/readiness 复用；坏正文不会进入 Worker。
- 正文质量不完整与基础设施故障使用不同的结构化异常：只有明确需要 `document_text` 的策略遇到扫描页无文本、页数不符等质量失败时返回 `422 event_document_text_data_unavailable`；网络、SDK、解析器、子进程和超时继续返回可重试 503。技术策略和普通事件不会因同名异常被误分流。
- “首次实际买入成交后第 N 个 A 股交易日退出”从第一笔真实 BUY fill 起算，成交 session 不计，目标 session 由 pinned A 股日历固定；第 N 日使用日线开盘价代理尝试卖出，失败仍按停牌、跌停、容量和 DAY 订单规则处理，不假装成交，区间末不强平。
- 只有受支持 `hashSchemaVersion` 下对完整持久化 bundle 读时重算一致的 `resultHash` 才可信；四个结果读取接口和完成态幂等重放共用该门禁。legacy 无 schema 且无 hash 仅按无证据路径兼容并返回 `resultHash=null`；legacy 带 hash、未知版本、缺 hash 或篡改均拒绝。

## 5. 前端与澄清边界

- 正式入口只有 `web/src/main.tsx → web/src/App.tsx`；旧状态机已删除。
- 视觉沿用当前新版东方财富橙色语义、规整对话和渐进卡片；不回退旧红白，也不因工程修复整体换皮。
- Mock 永久显示“演示数据”，不得显示“身份已验证”或伪造完整运行身份。
- Live 前端每次编译、修订和提交前都读取 `/capabilities`。固定快照按逐事件码 `backtest_available` 开放；on-demand 只在后端明确返回 `preparation_available + request_preparation` 时允许进入按需生成并 pin 的提交链。两者都为 false 时 fail closed。Mock 主演示仍只开放年度报告并持续标注“演示数据”。
- 只说“MACD”等名称、只有买入条件或只有卖出条件时，分别只集中澄清一次缺失的方向并返回编辑原话；系统没有默认金叉买、死叉卖等交易策略，只有版本化执行默认。
- 股票页可通过 `instrument_context|symbol` 注入规范 A 股代码；非法、非 A 股或交易所后缀错配 fail closed。URL 的 `instrument_name|name` 不经证券主数据校验时不可信，不能决定证券身份；当前没有可信名称时展示规范 symbol。东方财富只保留为 standalone 默认和验收 fixture。
- Live 的缺股票澄清可写入 `instrument_context`；“MACD 收盘/盘中”目前只是 Mock 交互。分钟链路未闭环前，盘中选项保持禁用。

## 6. 仍阻止最终 Live 验收的事项

- 由主线程形成 clean integration commit；当前没有最终验收 SHA。
- 待 Choice 连接恢复后生成新 exact Event/Composite v2，由当前 loader 完成 expanded/user 区间校验。
- 在该 SHA 下用新 Composite v2 启动 strict API 并通过 `/ready`。
- 用同一 snapshot、同一 1,000,000 元和同一执行配置完成技术策略与五类快照中的事件策略双 run。
- 保存两条 run 的 run ID、受支持 hash schema 下读时重算一致的 result hash、snapshot/checksum、strategy/catalog/config/data/engine/Git identity；当前不得预填。
- 用 `VITE_USE_MOCK=false` 在 320/390/768/1280 完成技术与年报真实 H5，console/page error 为 0。
- 在最终 SHA 上重跑后端、前端、Catalog、Migration、diff 和 strict smoke 全部门禁。
- 要把正文词频策略升级为 Live，需显式 opt-in 重采完整报告、发布新的 Event/Composite v2，并用新 pins 完成 API/H5 因果链；历史五事件工件的 `document_text=0`，当前无可用 strict pins。

## 7. 仍阻止生产放行的事项

- 当前 Choice 为个人研究账号；需公司授权账号并重新验收字段、范围、SLA、许可和再分发。
- iFinD、RQData、Tushare 只有适配和测试，缺本机授权实网证据。
- 东方财富公告原始响应字节、完整公告/PDF 和结果产物尚无不可变对象存储与按 hash 找回能力。
- 按需准备代码不再固定 `300059.SZ`，并可覆盖沪深主板、创业板、科创板；但 fresh 五年真实证据仍只有 `300059.SZ`，缺全 A 股生产级逐日状态、独立数据对账和覆盖/SLA 证明。
- 北交所只有领域数量/涨跌停规则；可证明的历史挂牌场所归属和跨源逐日停牌/reference 事实未闭环，因此完整回测继续 fail closed。
- 送转不足 1 股尾数的最终登记证据、个人现金分红税制未完整闭环；配股默认不认购已实现，认购路径仍未提供。
- 草稿持久化、结果分页、认证、用户级 run ownership、审计、限流、监控、备份恢复与生产发布演练未完成。
- 当前分钟实网探针返回 `10000017 overseas ip is restricted`；没有真实分钟快照，也没有可执行分钟策略链。
- Tick、L2、股票池、组合和实盘交易不在 v1。

生产部署要求见 [生产发布准备](runbooks/production.md)。
