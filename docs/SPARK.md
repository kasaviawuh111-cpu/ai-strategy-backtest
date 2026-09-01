# SPARK：决策与问题台账

> 最后更新：2026-09-01
> 用途：记录已经确认的决定、Demo 边界、上线阻塞项和后续问答。未确认事项只记录问题，不在这里替用户做决定。

本文件统一使用以下证据层级：`decided`、`implemented`、`fixture-tested`、
`local-real-data-verified`、`Mock-verified`、`Live-unverified`、
`production-unavailable`。其中 `local-real-data-verified` 表示本地真实物理数据、
校验和与正式 loader/Pin 已核验，不等于浏览器 Live E2E，更不等于生产授权。

## 已确认决策

- 2026-09-01 发布收口：本版默认回测区间由近 5 年改为近 1 年，用户明确说出其他区间时仍以原话为准。事件策略暂不对外执行，回购、股东增减持和业绩发布均记为后续待办；早期“Demo 必须支持事件策略”的决策对当前发布版不再适用。
- 2026-09-01 财务/估值数据收口：当前发布版只使用东方财富《操盘必读》正式接口直接返回的字段；暂停之前 `RPT_F10_FINANCE_*` 财务链路。不自行计算单季度、TTM、同比、比率或估值，缺少直接字段时返回 `capability_unavailable` 或 `no_signal`，不填 0、不换字段。

- 当前阶段先完成可演示、可复现的 Demo，不将个人账号的数据能力视为正式产品授权。
- 当前 Choice 账号是个人研究账号，只用于 Demo、数据探查和工程链路验证。
- 数据链路为 `fresh submission 重采 BaoStock/东方财富/Choice → 交叉校验 → 内容寻址不可变快照 → Worker 本地精确重放`。已发布快照是审计产物，不是可变运行缓存；Worker 禁止调用供应商采集路径，目标缺失或 identity/checksum 不符即 fail closed。
- 历史阶段曾要求 Demo 同时验收技术与事件策略；该要求已被 2026-09-01 发布收口覆盖。当前版本只发布技术与可证明时点的估值条件，事件编译和历史快照代码保留但运行能力关闭。
- 事件后续恢复时仍必须使用可证明的首次可得时间；精确到秒时按秒处理，只有日期或时间不可靠时才保守降级。当前发布版不因已有快照而对外开放事件回测。
- 事件采集保留“公开公告历史回放 + 网页前向采集”设计，不是回测运行时搜索；但本版不执行该路径。
- 数据快照范围必须覆盖用户回测区间之外的指标预热与结算扩展；系统按策略中最长参数动态计算交易日依赖，再用 `8/5` 的保守 A 股自然日换算并增加 14 天缓冲（总上限 3650 天），结算端另扩展 14 天，不再固定写死为 180 天。
- 指标扩展只发布能由固定输入、公开公式和黄金样例稳定复算的定义；截图中的彩带、四合一、主力介入强度、彼得林奇 0—100 等包装名，在没有可验证公式或供应商原样序列时不做同名仿制。
- 估值、财务、资金流、筹码和龙虎榜等供应商数据先通过 Choice 命令生成器/`cfc` 或正式 API 文档验证字段、单位、频率与历史可得时间，再进入版本快照；当前来源映射不等于已经接入回测运行时。
- 正式上线前必须换成公司授权账号，并重新执行数据范围、字段语义、权限、质量和回放一致性验收；不能直接沿用个人账号的验收结论。
- 账号授权、数据权限、字段语义、数据源切换和验收结论等同类事项，后续统一补充到本文件中共同处理。
- 当前单股策略 Demo 默认使用 100 万元标准模拟账户，以减少整手和最低佣金造成的尺度失真；主流程不询问资金，用户只在折叠的“资金与成交假设”中按需修改。资金充足不代表保证成交，停牌、涨跌停和 5% 成交量参与率仍可造成部分成交或不成交。
- 初始资金不是展示占位：买入默认使用 100% 可用资金，按板块的最低买入数量/递增单位、费用和成交能力计算数量；实际采用值必须进入策略版本与 run manifest。沪深主板/创业板为 100/100，科创板为 200/1；部分成交可形成非整手整数持仓，卖出可清掉任意整数余额。结果页以收益率、净值和回撤为主，期末资产明确标注模拟资金基准。
- 预开盘 BUY 数量禁止读取当天尚未可得的 `bar.open`。策略账户 09:15 委托与 funded benchmark 09:29 委托统一使用当日已知 `session.upper_limit`，按该最坏合法成交价连同费用预留资金；当天 `bar.open` 仅在 09:30 日线开盘价代理撮合时使用。没有有效涨停上限的无涨跌幅 session 无法用日线数据安全定量，必须以 `pre_open_buy_sizing_upper_limit_unavailable` fail closed 且不创建订单。
- 100 万元默认仅适用于 `ashare_lab` + H5 正式主链；`astock_backtest` legacy v1 继续只作迁移对照，不据此解释产品默认。历史 10 万元运行记录不改写，新默认产生新的策略哈希、运行指纹和结果证据。
- 日线开盘价代理默认使用“上一已完成交易日成交量 × 参与率”作为容量代理；必须保存来源交易日和 `known_at`，缺失时不成交。当日全天成交量不能用于当日开盘代理撮合。
- `unlimited` 只作为折叠高级设置中的显式研究选项；不得在容量数据缺失时自动启用，且选择结果必须进入策略版本、RunManifest 和结果执行假设。
- “同期持有”政策固定为 `funded_buy_and_hold.same_execution_ledger.v1`：同样 100 万起始资金、同费用/滑点/整手/涨跌停/停牌/容量/公司行动，不再使用后复权价格首尾比。
- 公司行动账本政策为 `cn.a_share.timeline_entitlement_receivable_settlement.date_only_cash_after_close_conservative.v2`：“登记日权益 → 除权日应收 → 派息/到账结算”。现金派息只有日期而没有日内到账时间时，派息日交易时段内继续作为应收，收盘后才转为可用现金，最早下一交易日可用于下单；不得声称有盘中到账证据。股份仍按明确到账/上市/可卖日期盘前处理。现金分红政策为 `gross_research_no_withholding.v1`，按税前总额入账且未模拟个人按持有期补税。账户股数始终为整数；不足 1 股权益尾数缺最终登记证据时 fail closed，不默认现金补偿；配股默认 `decline_no_external_cash.v1` 已实现，记录拒绝认购，不注入外部资金、不改变账户经济状态。
- 公开公司行动派息日补链不能把 `BonusFinancing.PageAjax` 当作完整历史：实测它只保留最近窗口。较早现金分红必须以 `RPT_SHAREBONUS_DET` 为动作锚点，完整查询并哈希同一 `NOTICE_DATE` 的公告列表，在下载正文前按东方财富官方“分配方案实施”栏目筛候选，再以证券代码、报告期、登记日、除权日、税前每 10 股金额和正文唯一明确划款日做严格 join。无候选、字段冲突或双候选一律拒绝，绝不把除权日当派息日猜测。
- Choice 日历与日线读取是幂等供应商查询；日历按自然年有界分为最多 366 天一批，并显式设置 `RECVtimeout=30`，避免五年窗口退化为 60 多次串行月请求。对外请求只在 transport 瞬态失败时最多重试 3 次；权限、字段、Schema、业务数据和非交通层异常不重试。POST 使用稳定幂等键，超时后重放不得创建第二条 draft/run；最终成功结果、实际区间、参数、尝试次数和请求身份进入 audit。
- 严格 Demo 必须从 `composite_snapshot` 启动，结果展示秒级事件 provenance、snapshot 身份和精确 40 位 clean Git SHA；`local-demo`、dirty revision、临时内容指纹和日期级事件都不能作为验收证据。
- 技术策略 fresh submission 也必须发布显式 `no_event_required` Event v2，再组成 Composite v2；这表示“本次策略不需要事件”，不是查询到了 0 条事件，也不赋予任何事件回测能力。Run evidence 分开保存 loader slice 的 `dataSchemaVersion=local-parquet.market-data.v3` 与 producer 的 `producerSnapshotSchemaVersion=ashare-lab.composite-research-snapshot.v2`，Worker 对两层身份分别重验。
- Registry 显式选择按 `producer_snapshot_id`/内容身份只校验目标快照：mixed v1/v2 root 中旧 v1 不得毒化无关 v2，目标自身损坏或不满足当前 coverage 仍拒绝；无显式目标的全扫描继续对坏条目 fail closed。旧快照不删除，但也不因“曾经 pin 过”而保持当前有效；当前通过最新公司行动 coverage 门禁的事件 Composite 是 `composite:1f26…`，loader pin 为 `snapshot:4bd9…`。
- MACD 盘中能力已冻结为两个独立产品：首版默认做“日线 MACD 盘中估算”，每根已完成 1 分钟 K 线都从上一交易日最终日线 EMA/DIF/DEA 状态重新估算当日值，分钟结束后产生临时信号并在下一可交易分钟开盘尝试成交；明确说“1 分钟/5 分钟 MACD”时才使用分钟周期 MACD。两者不得共用一个开关。默认容量使用上一已完成分钟成交量代理；午休顺延 13:00、尾盘顺延下一交易日；无 L2 时不模拟排队，一字涨停买入/一字跌停卖出不成交；同分钟收盘成交仅可作为未来明确标注的乐观研究选项。当前只有分钟数据模型、Choice `cmc` 解码/探针、双价格分钟快照发布器和本地读取器达到 `implemented / fixture-tested`；网络实测被 `10000017 overseas ip is restricted` 阻断，尚无真实分钟快照。完整分钟策略链仅为 `decided / Live-unverified`：DSL、盘中信号 runtime 和分钟撮合仍未实现，盘中选项必须显示“待接入”并禁止产生假结果。
- 视觉继续沿用当前新版东方财富橙色语义、规整对话和渐进卡片；不回退旧红白主题，也不因工程修复整体换皮。Spec 只约束可信度、信息层级、交易因果轨迹、响应式和可访问性，不写死像素、字体、圆角或卡片尺寸。
- Live 对缺股票、缺买入条件或缺卖出条件分别只集中澄清一次，并让用户编辑原话；缺入场不能误提示补退出。系统没有默认金叉买、死叉卖等交易策略。“MACD 收盘确认还是盘中触发”目前只完成 Mock 交互，盘中真实链路完成前不能泛化为 Live 能力。
- Demo 产品不固定东方财富：H5/WebView 接收任意合法 A 股规范 symbol；东方财富 `300059.SZ` 仅是独立打开默认值和 golden fixture。按需真实数据链当前覆盖沪深主板、创业板和科创板；北交所只有领域数量/涨跌停规则，历史挂牌场所归属和跨源逐日停牌/reference 事实未闭环，因此完整回测 fail closed。URL 的 `name/instrument_name` 未经证券主数据校验时不可信，不能决定证券身份；当前无可信名称时只展示规范 symbol。某标的缺真实 snapshot/session/公司行动/事件覆盖时明确拒绝，不能借用 fixture 数据。
- 自然语言分成两条受限路线。完整策略继续使用 `deterministic_fast_path_llm_bounded_candidate.v1`：常见明确表达走确定性快路，其余由模型只生成 1—3 个受限 CandidateAst，再由服务端 Schema/Catalog/时间/数据能力门禁和用户确认决定是否可执行；模型 transport/bootstrap 配置路径已存在，较早 provider 路径做过公网冒烟，但当前 revision 仍需 Live 复验。纯观点进入不可执行的 `idea-route.v1`：任意有意义输入先被理解，再形成待检验假设，只把当前权威 A 股页作为价格代理，从服务端固定模板给出 2—3 个候选，用户一次选择后重新走原编译器；当前仅 `implemented / fixture-tested`，不得写成 Live。
- 单笔订单始终 DAY。当前信号有效期按语义执行：简单日线边沿最多 3 个交易 session；持续状态每 session 重算且单次有效；复合条件无法安全逐日重建全部语义时单次有效；事件因尚无点时修订/撤回/quarantine 失效链也单次有效，不跨日重试。结果 API/UI 显示 edge=3、event=1、state=1、composite=1。分钟有效期仍待分钟链实现。
- “初始完整定期报告文本层词频 + 首次真实买入成交后第 N 个 A 股交易日退出”采用确定性契约：可提取文本必须冻结在 Event/Composite v2，并验证抽取器身份、版本、逐页 hash/字符数和总文本重建；NFKC 后，ASCII 词按 token 边界且默认忽略大小写，中文按 literal 子串，不扩同义词、不 OCR、不让 LLM 计数。只开放 `gt/gte`，`eq/lt/lte` fail closed。摘要、英文/外文版、更正、修订、取消/撤回/撤销、更新和补充版本排除；同一事件码与报告期出现两个候选初始完整版本时 fail closed。单一 PDF 文本层不能自证语义全文完整，生产仍 unavailable。
- 正文工件的严格校验必须只有一套共享 validator：runtime、Event→Composite 发布和 strict loader pin/readiness 都验证 source format、抽取器身份/版本、连续页码、逐页 hash/字符数、总字符数、源文档 hash 与逐页重建总文本。坏正文必须在发布或启动准备阶段 fail closed，不能等 Worker 执行策略时才暴露。
- 持有期从第一笔实际 BUY fill 起算，成交 session 不算第 1 天；目标是运行时 pinned A 股 session 中后续第 N 个交易日，并在该日用日线开盘价代理尝试卖出。停牌、跌停、容量和 DAY 订单规则继续生效，未成交不伪造成交；回测区间结束不强制平仓。
- 事件代码真实门槛冻结：工程 Demo 每类至少 30 个独立真实正例、30 个困难负例和 10 条完整因果 E2E；至少覆盖 10 个发行人、3 个年份以及更正/撤回/错股/候选/传闻。生产声明候选至少 60+60、独立复核，关键时点和错股零容忍。理由是先审真实输出与高损边界；按零失败 `rule of three`，30 个样本只能把 95% 失败率上界约束到约 10%，60 个约 5%，不是证明零错误。样本数不能替代 `instrument × event_code × interval` 的 100% acquisition coverage；策略效果另按有效独立样本 `n_eff` 做功效分析。
- 日线事件执行采用 1 秒处理延迟和 09:15 信息 cutoff；`decision_ready_at <= 09:15` 时可使用当日已发布的日线开盘价代理，否则顺延下一可交易 session。日线 OHLCV 不能证明集合竞价逐笔成交，`filledAt=09:30` 只是统一记录边界并标记时间非精确。未来分钟引擎使用提交后的下一完整分钟；1/3/30/60 秒完整压力档尚待实现。
- 数据对照冻结为依赖字段精确门禁：身份/session exact，OHLC 按有效 tick 同档，成交量统一为股后 exact，成交额用到时精确到分，复权因子相对误差不超过 `1e-8`，所有未解决冲突/关键缺失 fail closed。当前跨源全量 reconciliation 仅为部分实现。
- 修订政策冻结为不可变旧 snapshot/run + 新 snapshot 新 run：复权因子只要影响 warm-up/回测区间就重跑受影响结果，不静默覆盖；正式更正按真实可得时间新增 revision，数据商纠错才发布历史修订。普通历史 run 的影响索引、superseded 关系和差异页尚未实现。

## 事件能力与数据权限现状

- 权限状态（2026-08-29）：用户当前暂时无法取得 RQData、iFinD 或 Wind 的新闻/资讯历史权限；本轮 Demo 不再要求用户继续申请、购买或提供这些账号。
- 事件目录中的 16 个家族、160 个事件代码只是统一分类词典，用来避免以后重复命名和接口返工；它不是“160 个事件已经支持”，也不是本轮必须全部建设的产品承诺。
- 当前 11 个家族、69 个事件项只达到 Catalog、自然语言编译、Adapter/统一事件运行时代码路径的 `implemented` 口径；它不表示 69 项逐项测试通过，也不表示 69 类都有真实历史样本、真实快照或 strict E2E。
- `/capabilities` 必须把这 69 个 Catalog 可编译项、按需准备能力与当前 pinned strict snapshot 的逐码运行可用性分开；只有 acquisition coverage 合格的事件码才可返回 `backtest_available=true`。on-demand 尚无具体股票与区间快照时只能返回 `preparation_available=true + request_preparation`，随后按请求采集、校验并 pin；不得冒充当前快照已有数据。前端 Live 编译、修订和提交均动态读取该合同并 fail closed。当前 `composite:1f26…` 对五类定期报告暴露 `pinned_snapshot`；其余 64 码不得冒充已有真实数据。
- 当前本地真实工件覆盖年报、半年报、季报、业绩预告和业绩快报五类：东方财富 `300059.SZ` 在 2021-02-07 至 2026-08-20 共形成 26 条 `vendor_observed + validated` 秒级 canonical/observation，依次为 6、5、10、3、2 条；producer 为 `composite:1f26…`，loader pin 为 `snapshot:4bd9…`，状态为 `local-real-data-verified`。
- `event_snapshot` / `composite_snapshot` 已升级到 v2，并按 event code 保存 acquisition coverage。东方财富公告类和重大中标必须证明指定股票、区间与事件代码查询成功；网页许可事件必须有 `web_archive` 区间覆盖。成功查询 0 条可以发布，缺少覆盖则 snapshot、composite 和 runtime 全部 fail closed。当前无法证明区间覆盖的 `license_approval` 已从默认 strict codes 移除。
- 五类财务事件的当前本地工件保留了 2021-02-07 至 2026-08-20 的 9 页、862 条公告查询 hash；原始响应 bytes 尚未进入不可变归档，只有 hash 还不能独立重放页面内容。
- 当前五事件 Event 中 `document_text` 观察数为 0。正文词频虽达到 `implemented / fixture-tested / Mock-verified`，但真实策略仍是 `Live-unverified`；必须显式 opt-in 开启正文抽取、重新采集并发布新的内容寻址 Event v2 + Composite v2，不能用标题/摘要代替正文。
- 历史事件优先使用能够证明秒级首次公开时间的东方财富公开公告及其原始公告证据。最终重大中标或业务许可获批只有在上市公司公告链满足事件边界、实体映射和秒级时间门禁时，才可进入历史回测。
- 不属于公告链的网页事件采用 `web_archive` 前向采集：只从采集器上线后使用真实 `collector_observed_at`，不得把今天抓到的旧网页倒填成历史首次可得时间。
- RQData、iFinD、Wind 后续若获得授权，只作为数据覆盖增强和独立交叉验证来源；接入前仍需重新验证时间语义、历史修订、缓存和产品使用授权，不得事后在多源中挑最早时间。
- 上述免费替代仅解决事件来源；日线行情和交易日历在本轮 Demo 中仍使用现有 Choice 个人研究快照，不能把整个 Demo 描述为完全公开数据驱动。
- 本轮按“现有可验证数据优先”推进三条能力：东方财富公告事实进入事件快照；基于冻结 OHLCV 的量价/阶段趋势由本地确定性算法计算；东方财富 Push2 主力资金只建设独立研究快照，不进入五年正式回测主链。
- 东方财富公告不能无条件承诺“全历史都稳定到秒”。对 2017 年及以后，只有通过可信 `eiTime/display_time`、正文和区间覆盖门禁的记录才可进入 strict；2017 年以前存在 `eiTime` 历史回填风险，2016 段需更权威来源交叉证明。历史方案曾按近 5 年设计，但 2026-09-01 已改为默认近 1 年；事件能力本版关闭，后续恢复时仍须通过来源、时间质量和完整区间覆盖门禁。合格记录使用 `max(ceil(display_time), credible_eiTime)` 作为保守供应商观测时间，并保留原始字段和 response hash；它不是交易所认证发布时间，也不能承诺到毫秒。
- 公告分类采用“细栏目代码 + 标题确定性规则 + 正文证据 + 歧义隔离”。一级栏目、标题大模型自由判断或当前抓取时间都不能直接生成历史可交易事件；回购、增减持、重组等必须区分计划、进展、完成、终止等阶段。
- `event.repurchase_capital.repurchase_change` 已完成“回购方案修订”细栏目映射、自然语言编译、查询覆盖/分页/0 结果夹具和 strict 代码门禁，状态仅为 `implemented / fixture-tested / Live-unverified`；当前 `1f26…/4bd9…` 不含该码的 live coverage，不得算入五类定期报告切片。
- 成交量和趋势标签不是交易所原始事实，必须公开公式并固定版本。正式首版采用前 N 个有效交易日、排除当日的量基准；背离只能在右侧 K 线完成后确认，禁止倒填到历史高低点。
- “主力/超大/大/中/小单资金流”是东方财富 Choice 供应商计算结果，公开接口未披露完整算法、L2 依赖或版本，无法由 OHLCV 复算；任何展示必须标记 `research_only + methodology_unknown`，不得称为真实机构账户流向。

## Demo 当前允许

- 使用当前 Choice 个人研究账号验证日线、交易日历等已通过探查的数据能力。
- 使用当前账号开发采集、校验、快照和回测引擎之间的接口与流程。
- 使用经过校验并固定版本的数据快照运行研究 Demo。
- 建设过程中，对尚未验证或当前账号无权限的数据明确显示为“不可用”或“待验证”；但事件数据必须在 Demo 验收前接通并通过真实回测。
- 无机构新闻权限时，使用已通过秒级门禁的公开公告事件完成事件策略验收；不得用日期级网页、搜索摘要或当前抓取时间伪造历史事件。

## Demo 验收前必须解决

- [x] 接入东方财富公告秒级时间适配器，并完成同花顺 iFinD、RQData、Tushare 可插拔行适配；机构源适配保留为可选扩展，不阻塞当前 Demo。
- [x] 完成事件观察、固定优先级融合、冲突隔离、事件快照及 Choice + 事件组合快照基础设施；回测运行时不联网。
- [x] 默认容量改为上一已完成交易日量代理；缺失不成交，`unlimited` 仅显式配置。
- [x] 公司行动领域契约、三阶段账本、整数股强约束、不足 1 股尾数 fail-closed 与非整手整数余额完整卖出已落地。
- [x] 配股默认 `decline_no_external_cash.v1` 已进入策略与同期持有账本；不认购、不注入外部现金，并保留审计原因。
- [x] 历史 session 改为 BaoStock `preclose/tradestatus/isST` 年度有界查询，并逐日与 Choice 前收/交易状态精确交叉校验；`instrument_sessions.parquet` 升级为 v2 数量规则。沪深主板、创业板、科创板进入按需准备链；北交所完整历史数据仍 fail closed。
- [x] dirty worktree 上 fresh 发布 `300059.SZ` 五年 Choice 公司行动快照 `choice:a2eebf97b7c2ed4f2467ca6f91a3e95f48ed609c4026b440a16e7a11a4a23b4f`：2021-08-06 至 2026-08-06，执行/信号/session 各 1,211 行，公司行动 7 条（现金 5、送转 2），配股 0 条正向结果、拆股/缩股 0 个候选；前缀 969 行差异 0、`passed`。该项只为 `local-real-data-verified`，不是 clean-SHA Live E2E。
- [x] `implemented / fixture-tested`：现金派息日的旧记录补链已接入东方财富实施公告，先完成列表分页证据再做 provider-column 正文筛选；报告期/登记日/除权日/现金额/明确支付日不一致、无候选和双候选均有 fail-closed 回归。真实五年 exact API 链仍必须以最终 fresh snapshot/run 证据单独验收。
- [x] `local-real-data-verified`：五类定期报告 Event/Composite v2 已生成并由当前 loader pin；producer `composite:1f26…`，slice `snapshot:4bd9…`，26 条秒级观察、9 页/862 条查询证据。本地 `.env` 已指向该 Composite。
- [x] 正文词频 DSL、初始完整版本门禁、确定性计数和首次实际成交后 N 个 A 股交易日退出达到 `implemented / fixture-tested`；年度报告词频 + 3 个交易日退出的策略卡达到 `Mock-verified`，Mock 不证明真实正文或成交。
- [ ] 为正文策略显式 opt-in 重采完整报告，发布 `document_text > 0` 的新 Event/Composite v2，并用新 pins 跑 strict API/H5；当前 `1f26…` 的 26 条 observation 全部 `document_text=0`，必须 fail closed。
- [ ] 在 final clean SHA 下用同一 100 万资金、同一 funded benchmark 重跑至少一条技术策略和一条秒级事件策略，并持久化新 run。较早 `4dc55904…` 的公网双 run 已再次通过，但不替代本项。
- [x] 前台代码展示秒级 available_at、provider、time quality、snapshot/Git 身份和 signal → order → fill/unfilled 因果链；Mock 技术旅程覆盖 320/390/768/1280px，年报/不支持/一次澄清覆盖 390px，所有已执行旅程均无横向溢出且 console/page error 为 0。该项是 `Mock-verified`，不是 Live。
- [x] 公告分类的否定、候选、传闻、错股票/错市场和生命周期误判已进入 `fixture-tested` 门禁；当前真实 snapshot 另证明 `300059.SZ` 五类财务事件数据，不据此声称其他事件或全 A 股已有真实覆盖。
- [x] 为新量价信号完成前缀不变性、停牌零量、末端未确认背离和参数版本迁移测试。
- [x] 冻结 Push2 资金流为独立研究增强；正式事件/技术 Demo 不依赖其在线可用性，也不把供应商口径包装成可复算机构资金事实。
- [ ] 从干净 Git 提交重启，确认 strict `/ready`、技术/定期报告 API 双 run 和 320/390/768/1280px Live H5 均通过，且 console/page error 为 0。

## 日线 Demo 之后的分钟里程碑

- [ ] 完成 MACD 盘中策略真实链路。该项不阻塞当前日线 Demo：产品语义和默认撮合政策已冻结；分钟模型、Choice `cmc` 采集/严格校验、内容寻址双价格快照和仓储读取代码已完成。后续仍需在中国大陆合规网络实测个人账号权限与历史深度、发布真实分钟快照，再接入 DSL、信号运行时和分钟撮合，并通过午休、尾盘、缺失/零量分钟、反复穿越、停牌、T+1、涨跌停和公司行动用例。日线结果或 Mock 不能代替该验收。

## 正式上线前必须解决

- [ ] 后续生产化 TODO：接通云端持久 PostgreSQL/CFS，以专用低权限账号实施 append-only/不可变保护，并完成容器重启后的 draft、receipt、manifest、run 恢复及备份/回滚演练。本轮明确延期，不作为当前功能收口门禁。
- [ ] 换用公司授权账号，确认后台采集、缓存、加工和产品展示等使用范围。
- [ ] 使用公司账号重新跑完整的数据与权限验收，重新生成正式快照，并把账号、权限与供应商版本纳入生产验收。
- [ ] 若生产方案选择 Choice `regularreport`，再解决其权限不足问题；当前 Demo 已改走公开公告快照，不等待该权限。
- [ ] 用公司授权源重新验证 BaoStock `isST/tradestatus/preclose` 与 Choice 逐日交叉校验口径，并扩展到全 A 股；当前 `300059.SZ` 五年本地真实链不等于生产覆盖。
- [ ] 用公司授权源验证全区间公司行动数据完整性、修订链、到账/可卖日期，并完成独立账户对账。
- [ ] 若后续支持配股认购，另行实现显式、版本化的全额/最大可负担认购政策以及缴款、上市可卖账本；默认 `decline_no_external_cash.v1` 已实现且不得静默改变。
- [ ] 建立跨快照复权修订检测与接受/拒绝规则；当前“较早截止日”探针只证明同次采集不受查询结束日影响，不能证明未来公司行动后历史复权值永不变化。
- [ ] 获得并验证逐日精确涨跌停价格；当前可用标记不能替代精确价格。
- [ ] 验证事件精确到秒的“首次可获得时间”，确保回测只使用当时真实可知的信息，避免未来数据泄漏。
- [ ] 获取或匹配上交所、深交所、巨潮原始公告链接并验证与东方财富镜像内容一致；当前公开正文接口只证明东方财富镜像可读。
- [ ] 完成东方财富公开接口的产品使用授权、缓存、再分发和限流评估；当前结论仅适用于低频内部研究 Demo。
- [ ] 建立公告增量修订监控，验证 `art_code`、正文和分类栏目在未来是否发生静默修改，并规定快照修订/回滚政策。
- [ ] 支持“一份公告拆成多个事件”；当前适配器只输出一个最高优先级事件，不能同时表达一份公告中的分红、转增、回购注销等多个事实。
- [ ] 将公告正文失败从“整段采集失败”升级为可审计的逐条重试与隔离；在此之前继续 fail closed，不允许退化为只有标题也可交易。
- [ ] 建立并持续保留原始公告响应 bytes 与多页完整 PDF 的不可变归档。当前只有逐页 response hash；`np-cnotice` 的第一页 `notice_content` 不能代替年报等多页完整文件。
- [ ] 为重大合同/中标补金额、收入占比、主体角色和重大性正文抽取；当前只有标题明确阶段时才分类，不能据此声称已经验证合同重大性。
- [ ] 若产品正式提供“主力资金”策略，取得有 SLA、历史范围和算法版本说明的授权数据源；公开 Push2 最近约 120 个交易日的实测范围不能外推为五年覆盖。
- [ ] 为增发/定增建立独立事件家族和阶段代码；当前 160 项词典没有完整表达预案、审核、注册、发行、上市和终止全生命周期。
- [x] 任意沪深股票的停牌处理以 `InstrumentSession.status` 为权威，BaoStock `tradestatus` 已映射并与 Choice 交叉校验；矛盾数据 fail closed，不按前一日容量假成交。北交所仍缺可证明的同等历史链。
- [x] session 买入数量规则已从单一 `lot_size` 升级为 `minimum_buy_quantity + buy_quantity_increment`：主板/创业板 100/100、科创板 200/1、北交所领域规则 100/1；北交所数据链未闭环不因规则存在而放行。
- [ ] RunManifest 增加版本化信号会话政策（候选：`cn_a.trading_sessions_only.v1`），回测前按有效交易 session 数校验预热；当前自然日保守换算在长停牌标的上仍可能不足。

## 待逐项问答

以下事项按“一次问一个，并说明为什么要问”的方式确认：

- [x] Demo 第一阶段必须同时演示技术指标策略与事件策略；具体事件样例待确认。
- [ ] 正式账号需要覆盖哪些 Choice 数据集、历史区间和调用量？
- [x] 已取得东方财富公开公告 API 真实脱敏返回，确认 `art_code`、`display_time`、`eiTime`、`notice_date`、栏目和分页字段；修订/增量仍需生产任务长期验证。
- [x] 只有日期、没有精确发布时间的事件不进入 Demo 验收回测，不使用日期级保守兜底替代时分秒。
- [x] 复权价格、真实成交价格与公司行动分别采用什么处理口径？后复权只算相对技术信号；不复权用于成交/估值；公司行动用独立版本化事实和三阶段账户账本。
- [ ] 涨跌停、停牌、T+1、部分成交等执行规则中，哪些固定，哪些允许用户设置？
- [ ] 数据正确性用哪些独立来源对照，允许的误差和缺失阈值是多少？
- [ ] 快照保存在哪里，保留多久，谁可以生成、发布和回滚正式版本？
- [ ] 新快照改写既往复权价格时，由谁审核、如何展示差异、旧回测是否自动重跑？

## 完成记录

- 2026-08-29：确认 Choice 当前为个人研究账号，仅用于 Demo。
- 2026-08-29：确认开始建设 `Choice → 数据校验 → 版本快照 → 回测引擎` 链路。
- 2026-08-29：记录基础版 `regularreport` 权限不足，以及历史 ST、公司行动、精确涨跌停价、事件首次可得时间等未验证项。
- 2026-08-29：建立本 SPARK 台账，后续持续追加确认结果和完成证据。
- 2026-08-29：真实联调发现仅采集用户可见日期会缺少 MACD 预热和 T+1 结算日，已固定为快照级数据要求。
- 2026-08-29：曾发布旧 Choice 技术快照 `choice:9865254d…764c8`，三套核心数据各 1,340 行，复权前缀 1,098 行差异为 0；该快照早于公司行动覆盖门禁，只保留迁移审计。
- 2026-08-29：旧 Choice 快照真实 API MACD 回测曾得到 1,212 个净值点、237 条活动和 39 笔完整交易；因容量/基准/公司行动口径已升级，该结果不再作为当前验收。
- 2026-08-29：启用 `choice_snapshot` 严格模式；缺少或篡改信号文件、manifest 或其登记文件时拒绝运行，不再静默降级。
- 2026-08-29：事件数据未接通期间，运行时会诚实返回不可用并拒绝事件回测；这只是建设期保护，不满足最终 Demo 验收。
- 2026-08-29：确认 Demo 必须同时支持技术指标策略和事件策略，事件链路升级为当前交付阻塞项。
- 2026-08-29：确认 Demo 事件首次可得时间必须精确到秒；该问题由事件数据协作线程同步解决。
- 2026-08-29：上游交付暂按“API + 搜索”记录，等待真实样例确认；回测侧确定先快照、后运行，不直接依赖实时搜索结果。
- 2026-08-29：已与事件数据协作线程直接对齐；只让带 Asia/Shanghai 秒级时间且验证通过的事件进入可交易快照，日期级记录仅保留在原始观察层，并将事件快照与 Choice 快照一同固定进 run manifest。
- 2026-08-29：实现四源统一 `EventObservation`、固定 `eastmoney → ifind → rqdata → tushare` 融合、歧义隔离和真实采集时间审计；禁止按事后最早时间选源。
- 2026-08-29：实现 `event_snapshot` 与 `composite_snapshot`。旧组合快照固定了 Choice 不复权成交价、后复权信号价、session、canonical 事件、四源观察和两份来源 manifest；新版本另强制加入公司行动与覆盖证明。
- 2026-08-29：东方财富真实脱敏样例确认 `display_time=2026-08-27 18:34:11:530`、`eiTime=2026-08-27 18:35:16:000`；毫秒时间向上进位到下一秒，`ingested_at` 不参与历史首次可得时间。
- 2026-08-29：确认单股策略 Demo 默认按 100 万元标准模拟账户运行；资金入口收进折叠高级设置，但实际值继续参与整手、费用、成交数量计算并固化到运行快照。
- 2026-08-29：结果摘要新增运行时初始资金字段；新结果直接从不可变净值起点返回，旧结果缺失时明确显示“资金基准未记录”，不使用当前页面草稿反推。
- 2026-08-29：审计确认旧开盘撮合读取当日全天成交量存在未来信息；默认改为上一已完成交易日量代理 × 参与率，观察缺失返回 `point_in_time_capacity_unknown`，显式 `unlimited` 除外。
- 2026-08-29：废止后复权价格首尾比“同期持有”；正式基准改为同初始资金、同撮合/成本/公司行动的 buy-and-hold 账户。
- 2026-08-29：冻结公司行动三阶段契约：登记日锁权、除权日应收、派息/到账结算；不足 1 股权益尾数无最终整数登记证据时 fail closed，账户不保存小数股。
- 2026-08-29：冻结 `funded_buy_and_hold.same_execution_ledger.v1`、`cn.a_share.timeline_entitlement_receivable_settlement.date_only_cash_after_close_conservative.v2`、`gross_research_no_withholding.v1`；日期级现金派息按收盘后可用，当前不能声称有盘中到账证据或支持个人现金分红持有期税制。2026-08-30 已实现配股默认 `decline_no_external_cash.v1`，不认购、不注入外部现金，并在因果活动中保留拒绝原因。
- 2026-08-29：结果 UI 已展示秒级事件来源、time quality、snapshot/Git 运行身份和 signal → order → fill/unfilled 因果链；Mock 技术旅程覆盖 320/390/768/1280px，年报/不支持/一次澄清覆盖 390px，所有已执行旅程均无横向溢出且 console/page error 为 0；真实 API 双策略仍待最终 strict E2E。
- 2026-08-29：旧技术/事件 run 仅保留迁移审计；新口径必须在当前 strict v2、精确 clean Git SHA 下重新验收。当前 snapshot ID 可以记录，但最终 run ID、result hash 与 Git SHA 不得预填。
- 2026-08-29：确认用户暂时无法取得 RQData、iFinD、Wind 新闻/资讯历史权限；本轮 Demo 不再要求用户继续申请或提供账号，机构数据源降级为未来可选增强。
- 2026-08-29：澄清 16 个事件家族、160 个事件代码仅为分类词典；11 个家族、69 项只代表 Catalog/编译/Adapter/统一运行时代码路径。当时 `local-real-data-verified` 的 strict v2 只有年报 1 类、6 条秒级真实观察，不能写成 69 类真实历史覆盖。
- 2026-08-29 至 2026-08-30：年报单码和五类定期报告曾生成多批 v2 中间快照，现只保留迁移审计，不再把旧 ID 写成当前运行身份。
- 2026-08-30：历史工件 `events:f22b9a444b73297f640b63043602d6bb7dc8aee7b7fc96ba18c1db41a1171fef` / `composite:1b3dbee47973ed0bc3573da460f1075a2b8d235daba3ba2d96f0f82bf5a6f053` 曾覆盖 `300059.SZ` 2021-02-07 至 2026-08-20，并在 9 页、862 条唯一公告中筛得 27 条严格秒级财务事件：年报 6、半年报 5、季报 11、业绩预告 3、业绩快报 2。随后公司行动 coverage 门禁升级，当前 strict loader 已拒绝该 Composite 及旧 pins；这批 ID 现只是迁移审计记录，不得写成当前有效身份。
- 2026-08-30：自然语言不再把只说“MACD”等名称自动补成金叉买、死叉卖；缺完整买卖规则或缺关键退出条件都只集中澄清一次。没有统一默认交易策略，只有版本化、可见且可配置的执行默认。
- 2026-08-30：采纳“拼好码”工程原则：通用指标、统计、日历、解析和图表优先复用成熟开源并以薄 adapter 隔离；A 股点时证据、交易规则、快照身份和因果轨迹保留领域自研。具体候选、许可证和双跑门禁见 `docs/reference/reuse-first.md`。
- 2026-08-30：东方财富 Push2 日线采集按 AKShare 固定 commit `8e95744b…b53c55` 的 `stock_zh_a_hist` 请求协议补齐 `ut` 与 `f116`，AKShare 不进入运行依赖；本地仍增加 A 股身份、raw hash、量纲、session/公司行动与不可变快照门禁。当前直连在远端无响应后断开，只能称 `implemented / fixture-tested / Live-unverified`，不能用夹具冒充已取得真实 Push2 数据。
- 2026-08-30：定期报告 PDF 内嵌文本抽取已实际复用 `pypdfium2`，项目只保留薄 adapter 和 A 股版本/点时门禁，不自行重写 PDF 引擎；当前不支持 OCR。Vibe-Trading、MOSS 与 AKShare 协议参考均已固定 commit/许可证并完成窄胶水 adapter：受限策略候选、观点假设引导、MOSS/pandas 指标差分、Vibe/mootdx 日线接口、AKShare-shaped 东方财富 Push2 日线请求协议。它们没有整体导入上游运行时；模型 transport/bootstrap 配置路径已存在，但旧 provider 冒烟不证明当前 `idea-route.v1`；指标未切默认，mootdx 因商业/数据条款待审而未安装或联网，Push2 直连被远端断开且无真实 batch/snapshot，不能把夹具写成 Live。
- 2026-08-30：正文词频和成交后持有期链路完成代码、夹具与 Mock 收口；当前 `composite:1f26…` 虽已通过 loader，但 26 条 observation 的 `document_text` 为 0，因此正文策略保持 `Live-unverified`，待 opt-in 重采并发布新内容身份后再验收。
- 2026-08-30：按需数据链完成联网准备与 Worker 重放隔离：fresh submission 默认重采并发布内容寻址 snapshot；Worker 只按 manifest expected identity 本地 pin，禁止调用采集器。历史 session 使用 BaoStock 年度有界 `preclose/tradestatus/isST` 并与 Choice 逐日交叉校验；Choice `tradedates` 长区间按自然年有界分块。
- 2026-08-30：fresh 发布 `choice:a2eebf97b7c2ed4f2467ca6f91a3e95f48ed609c4026b440a16e7a11a4a23b4f`，覆盖 `300059.SZ` 2021-08-06 至 2026-08-06：执行/信号/session 各 1,211 行，公司行动 7 条，前缀 969 行差异 0。东方财富对现金/送转/配股做正向覆盖，对拆股/缩股做完整股本历史负证明。证据来自 dirty worktree，只为 `local-real-data-verified`。
- 2026-08-30：`instrument_sessions.parquet` 升级为 v2 的最低买入数量/递增单位；主板/创业板 100/100、科创板 200/1、北交所领域规则 100/1。按需真实链放行沪深主板、创业板、科创板；北交所仍因历史挂牌场所和跨源逐日 reference 未闭环而 fail closed。
- 2026-08-30：`event.repurchase_capital.repurchase_change` 完成东方财富细栏目、自然语言和覆盖夹具，保持 `implemented / fixture-tested / Live-unverified`；尚未发布包含该码的新 live Event/Composite v2。
- 2026-08-30：确认 `BonusFinancing.PageAjax` 只覆盖 `300059.SZ` 2022—2026 最近记录，2021 现金派息日缺失；改由官方栏目 `001002002001005` 的 2020 年度权益分派实施公告补链。公告正文明确登记日 2021-05-26、除权日 2021-05-27、现金红利 2021-05-27 划入，代码按通用严格字段 join 实现，未硬编码该股票或日期。
- 2026-08-30：正文 strict validator 前移到 Composite 发布与 loader pin/readiness；缺 source format、抽取器身份、页码/页 hash/字符数、源 hash 或重建不一致均在 Worker 前拒绝。
- 2026-08-30：正文按需准备的失败完成结构化分流：扫描页无文本、页数不符等 `DocumentTextIncompleteError` 经专用 exit code 进入 `SnapshotPreparationDocumentTextIncompleteError`，且仅在策略明确需要 `document_text` 时返回 `422 event_document_text_data_unavailable`；缺 SDK、解析器异常、网络、子进程与超时仍为 503。分流不依赖异常文案。
- 2026-08-30：结果完整性只信任受支持 `hashSchemaVersion` 下对完整持久化 bundle 的读时重算；状态、摘要、曲线、交易接口与完成态幂等重放共用门禁。legacy 无 schema 且无 hash 只作无证据兼容并返回 `resultHash=null`，legacy 带 hash、未知版本、缺失 modern hash 或篡改均 fail closed，不猜旧 hash 算法。
- 2026-08-30：fresh exact 准备曾在 Choice `tradedates` 2021-03 月批次遇到 `10002004 network connection closed when recv`；后续 fresh 五年准备因 67 个串行月请求累计耗尽 600 秒。最小 probe 又证明 2022-11 第一次返回 `10002004`、第二次成功，而一轮年度 probe 的三次登录均返回 `10002002 network connect failure`。因此日历改为 6 个左右的自然年批次并设置 30 秒接收超时，读取只重试 `10002004`，登录只重试 `10002002/10002004`；`10000017` 等权限错误仍一次即拒绝。该修复已 fixture-tested。随后在 clean SHA `0e902577…` 下重新采集：BaoStock 会话参考、东方财富公司行动和 Choice 登录成功，但不复权日线在有限重试后再次返回 `10002004 network connection closed when recv`，未发布新的 exact Choice/Event/Composite，也未运行 API/Worker/H5 Live；不能写成 Live 成功。
- 2026-08-29：事件数据改为公开可落地双路径：历史回测仅接收通过秒级门禁的公开公告；普通网页事件只使用 `web_archive` 上线后的真实首次抓取时间，禁止历史倒填。
- 2026-08-29：技术指标目录扩展至 22 项稳定定义；新增相对成交量、量价确认、OBV、确认后量价背离和阶段趋势，均完成黄金值、动态预热、前缀不变性、停牌零量与版本拒绝测试。日线相对成交量不冒充盘中“量比”；彩带、四合一、主力介入强度、彼得林奇 0—100 等黑盒包装在无原始序列或可验证公式时不仿制。
- 2026-08-29：自然语言语义门禁补齐 DIF/DEA、K/D、MACD 零轴、涨幅/跌幅、均线交叉/持续位置和显式参数；否定、不完整参数与无法精确表示的比较 fail closed。RSI/MA 交叉预热边界修正，超过 3650 个自然日的数据依赖不再静默截断。
- 2026-08-29：核查确认“盘中实时触发”之前只落在前端 Mock 选项，未进入策略契约、数据快照或执行引擎；已取消首屏的旧固定时点文案，并在真实分钟链路接通前禁止盘中选项运行假结果。
- 2026-08-29：接入 Choice `c.cmc` 分钟数据层：不复权 OHLCV/成交额用于执行，后复权分钟收盘用于信号；发布前强制 240 根完整会话、时间标签校准、日线聚合对账、严格首会话前缀不变性、内容寻址与篡改拒绝。本机真实单日探针登录成功但返回 `10000017 overseas ip is restricted`，因此当前没有真实分钟快照，也未声称个人账号已有五年分钟权限。
- 2026-08-29：22 个 stable 指标统一在信号运行时排除零成交量非交易观察，再按原日期回填 `None`；参数化测试验证插入停牌极端价格不会改变其余有效交易日的指标结果。现有固定东方财富 Choice/composite 快照实测没有零量或非交易行，因此该修复不改写当前 Demo 数据结果。
- 2026-08-29：修正组合条件三值逻辑：`Any` 有一个真值即为真，`All` 有一个假值即为假，其余含未知时继续等待；新增 `FirstOfExit` 中另一条件尚在预热也不会压掉真实卖出信号的整链回归。
- 2026-08-30：本次集成内容树已有后端自动化、Ruff/format、Pyright、Catalog、schema export、fresh SQLite Alembic 和 diff check 的 fresh 通过记录；前端 TypeScript、ESLint、Vitest 与 production build 也已通过本轮阶段门禁。精确测试数不在本文档固化；最终提交后的同批重跑结果与 clean SHA 由交付报告记录。
- 2026-08-31：当前真实五年 producer 为 `composite:1f26afb8b1223b656397abd5c99ce5a0ac9ed6593c3dbf7bdc46fb5a61bb9b8b`，loader pin 为 `snapshot:4bd9ae887af1cac159bf2d831313c73d839a313b11a746c015ea19fa3d5728cd`；五类定期报告共 26 条秒级观察，本地 `.env` 已指向该 Composite。
- 2026-08-31：较早公网 revision `4dc55904…` 的技术 `run:b2778dc334cc4eb3b39ac9eab2efd54f` 与事件 `run:12fe45b6624249d5a6bba3458751cc98` 已再次通过；这是 prior public API E2E，不是 final clean-SHA H5 验收。
- 2026-08-31：裸指标、缺买入或缺卖出的方向澄清已补 P0 回归；完成态幂等 replay 必须重算完整 result bundle；对外请求只对 transport 瞬态失败最多重试 3 次，POST 使用稳定幂等键。加入观点引导后，前端本轮 fresh Vitest 为 154 个；最终 clean SHA 仍需同批重跑。
- 2026-08-31：最终交付对象是老板在公网入口使用常见口语化问句随机试用，不是 Mock 展示。Mock 只保留为本地“界面预览”；交付构建必须 `VITE_USE_MOCK=false`、命中真实 HTTPS API/固定真实快照，接口失败不得静默回退，且需补齐浏览器 Network、API 响应与后端日志证据。
- 2026-08-31：冻结 Vibe 风格的观点前置层 `idea-route.v1`。任何有意义的输入先被接住；纯观点按“观点 → 待检验假设 → 当前权威 A 股页价格代理 → 2—3 个服务端固定模板候选 → 用户一次选择 → 重新严格编译”推进。用户选择前不得产生 StrategySpec、hash 或 run；缺可信页面标的不猜股票。没有可靠政治/主题事件快照时只允许价格代理，不创建主题事件、不宣称观点与股价的因果关系。当前状态仅为 `implemented / fixture-tested`，等待当前 clean revision 的真实 API/H5 复验。
