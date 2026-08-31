# A 股指标与事件覆盖清单

版本：`cn_a.coverage@2026.08.30`

## 1. 这份目录解决什么问题

产品需要“尽量多覆盖”，但目录里出现一个名字，不等于系统已经能可靠回测。为避免把规划能力冒充正式能力，项目把两个目录分开：

- 可执行目录：`catalogs/signals/*.manifest.json`，只允许 Strategy DSL 使用已经接入权威运行时的 `stable` 指标；
- 覆盖目录：`catalogs/coverage/*.catalog.json`，记录产品准备支持的指标、数据序列和 A 股事件，以及缺失的数据、实现和时间质量。

覆盖目录不会被 Strategy DSL 自动当成可执行能力。每个非 `stable` 条目必须写明 blocker；每个 `stable` 条目必须有公式摘要和实现引用。

## 2. 当前覆盖

### 2.1 指标与数据序列

| 家族 | 数量 | 范围 |
|---|---:|---|
| 技术指标 | 63 | 均线、动量、摆动、通道、波动、趋势、量价技术指标 |
| 价格行为 | 38 | 收益、新高新低、真实波幅、涨跌停、连续涨跌和 K 线形态 |
| 量能与流动性 | 30 | 成交量额、换手、量价、价差、盘口、排队和冲击成本 |
| 基本面与估值 | 50 | 估值、成长、盈利、费用、偿债、现金流、周转和质量分数 |
| 合计 | **181** | 只登记可绑定到当前 A 股单只标的的指标或数据序列 |

状态分布：`stable=35`、`research_only=74`、`unavailable=72`。

当前可执行的 35 项包括：

- 均线和摆动类：MA、EMA、双均线、MACD、RSI、KDJ、CCI、BBI、布林带、EMA 乖离率；
- 价格、动量与波动：区间涨跌幅、真实波幅、ATR/NATR、BIAS、ROC、Momentum、收益率标准差、历史波动率、阶段新高、连续上涨、振幅；
- 摆动、方向与通道：ADX/DMI、Stochastic、Williams %R、Donchian；
- 成交额：成交额、平均成交额；
- 量价与趋势：兼容成交量、相对成交量、量价确认、OBV、确认式量价背离、阶段趋势。

项目没有为了凑“80 个 stable”而复用旧代码冒充新主链实现。G2 的 80 个 stable 验收仍未完成；当前 35 项均已补齐权威公式、参数边界、动态 warmup、前缀不变性测试和 SignalRuntime 接线。后续条目也必须满足同一门禁才能升级状态。

### 2.2 旧信号原子的迁移边界

旧注册表当前保留 83 个产品范围内的原子，并全部映射到参数化的规范定义；映射关系保存在各指标的 `legacy_atom_ids`：

| 旧原子类别 | 数量 | 规范化方向 |
|---|---:|---|
| 技术指标 | 40 | MA、MACD、RSI、KDJ、Bollinger |
| 价格行为 | 29 | 滚动高低点、涨跌停、收益、连续方向、K 线形态 |
| 量能 | 12 | 成交量倍数、新高、量价确认/背离、换手率 |
| 基本面 | 2 | PE 历史分位 |
| 合计 | **83** | 仅统计当前产品范围内的迁移映射 |

这只是迁移溯源，不表示旧 `compute_fn` 已成为新权威实现。旧原子和新定义的公式口径不同或尚无边界测试时，新定义保持 `research_only` 或 `unavailable`。

## 3. A 股事件 Taxonomy

事件共 16 个家族，每个家族 10 个代码，合计 **160** 个：

| 事件家族 | 数量 | 示例 |
|---|---:|---|
| 业绩与财报 | 10 | 预增、预减、快报、定期报告、预告修正、审计意见 |
| 分红与公司行动 | 10 | 分红预案、除权除息、送转、配股、拆并股 |
| 回购与资本变动 | 10 | 回购预案、实施、完成、注销、终止、减资 |
| 股东增减持 | 10 | 大股东与董监高增减持、持股比例、实控人变化 |
| 解禁、质押与冻结 | 10 | 解禁、质押、解除质押、冻结、司法拍卖 |
| 交易状态 | 10 | 停复牌、临停、ST/*ST、退市整理、上市、转板 |
| 监管与信披风险 | 10 | 问询、立案、处罚、谴责、差错更正、报表重述 |
| 市场交易事件 | 10 | 龙虎榜、大宗交易、异常波动、价格偏离、换手异常 |
| 融资融券 | 10 | 标的调整、融资余额、融券余额、转融通与规则变化 |
| 指数与被动资金 | 10 | 纳入剔除、权重调整、调仓生效、股通与 ETF 清单 |
| 合同与订单 | 10 | 中标、重大合同、订单、履约、客户认证、供应商资格 |
| 产能与经营 | 10 | 开工、试产、量产、爬坡、达产、扩产、延期、停产 |
| 并购重组 | 10 | 收购、出售、重组审批、完成、终止、分拆上市 |
| 治理与人员 | 10 | 董事长/高管变更、辞任、异议、股权激励、员工持股 |
| 诉讼与信用 | 10 | 诉讼、仲裁、查封、违约、评级、担保、破产重整 |
| 宏观、政策与行业 | 10 | 支持/限制政策、产品与原料价格、关税、补助、许可 |

状态分布：`stable=69`、`research_only=29`、`unavailable=62`。69 个 `stable` 事件表示已有确定性分类、完整文档固化、秒级可得时间门禁和通用事件运行时；其中 62 个由东方财富公告的明确生命周期标题规则发布，另含原有财务披露、最终中标和许可获批纵切。`stable` 只证明代码可执行，不证明每个事件都已有真实历史样本；提交时仍必须提供满足 point-in-time 契约且实际包含对应事件的 `events.parquet`。其余事件继续保持研究或不可用状态。

## 4. 时间与成交默认语义

### 4.1 四个时间字段

事件数据必须同时保留：

| 字段 | 含义 | 禁止的替代 |
|---|---|---|
| `fact_at` | 事件在现实中发生或对应的业务时点 | 不能当成投资者已经知道的时间 |
| `available_at` | 信息首次公开、策略最早可能知道的时间 | 不能用公告所属日期零点代替 |
| `ingested_at` | 系统第一次收到该版本的时间 | 不能覆盖原值来“修历史” |
| `revision_id` | 公告修订或供应商版本 | 不能只保留最新版本 |

### 4.2 事件信号

统一默认：

1. 信号最早在 `available_at` 产生，不能倒填到 `fact_at`；
2. 通用研究观察可以把 date-only 归一为当日 15:00 供审计，但 strict Demo 只接受经验证的秒级 `exact/vendor_observed`；date-only 不触发交易；
3. 日线回测最早在下一可交易日开盘尝试下单；
4. 停牌时继续顺延到下一可交易日；涨跌停能否成交仍由统一撮合器判断；
5. 修订公告按 `first_seen_only` 保留首次可见版本，后续修订形成新版本，不重写旧信号；
6. 将来接入分钟数据后，可以显式选择“`available_at` 后下一次连续竞价”，但不能用日线 OHLC 模拟盘中即时成交。

例子：公司在周二 20:30 发布中标公告，信号时间为周二 20:30，最早周三开盘尝试成交；若来源只给“周二”而没有时间，该记录进入观察/隔离层，不能用于 strict Demo。公司周二 10:30 发布公告时，当前日线引擎仍等到周三开盘；未来分钟引擎才可以在 10:30 之后按独立执行假设回测。

### 4.3 日线技术指标

当前 35 个 stable 指标均为 `bar_close_confirmed`。MACD 等盘中值当然可以变化，但使用日 K 线定义的“当日金叉/突破”只有在该根日线收盘后才是最终值；若要回测盘中信号，必须另建分钟定义、分钟数据依赖和分钟成交模型，不能混用日线定义。

成交量口径明确分为两版：`market.volume@1.0.0` 保留旧的“含当日均量”兼容语义；新自然语言中的“放量、缩量、持续放量”使用 `volume.relative@1.0.0`，基线为此前 N 个正成交量交易日，不含当日。停牌或成交量为零的日线不进入基线，也不产生量价或趋势信号。

量价顶底背离使用价格枢轴和 OBV，默认左右各 3 根确认。枢轴只有在右侧日线全部收盘后才成立，信号记在确认日，绝不回填到枢轴日；动态 warmup 同时覆盖平均量窗口、左右确认窗口和最大枢轴间隔。阶段趋势由 SMA20/SMA60、短均线斜率、Wilder ADX/+DI/-DI 共同判断，默认还需连续两个有效交易日确认。

## 5. 数据授权与状态规则

| 状态 | 可以做什么 | 必须具备 |
|---|---|---|
| `stable` | 编译并提交正式回测 | 权威实现、公式、数据、时间语义、边界与前缀测试 |
| `research_only` | 展示、设计研究或离线验证 | 明确 blocker，不进入正式 Strategy DSL |
| `unavailable` | 仅展示覆盖计划 | 明确缺失的数据、授权或实现，不做静默替代 |

目录中的每项数据依赖都声明频率、字段、point-in-time 要求、四类时间字段和授权状态。L2、盘口、事件、财务与外部情绪数据目前不会用日线数据“猜”出来。

## 6. 文件与校验

### 6.1 可执行事件 Parquet 契约

通用 profile 中，纯技术策略可以不要求 `events.parquet`；只要 Strategy DSL 含 `EventCondition`，提交服务就会把行情与事件一起固定并纳入快照哈希。正式技术/事件联合 Demo 则始终使用 strict `composite_snapshot`，同时固定执行/信号行情、公司行动、session、事件和观察层，避免重启后回退到另一套数据口径。

| 列 | 类型 | 规则 |
|---|---|---|
| `stock_code` | string | 六位 A 股代码或规范化代码 |
| `event_id` | string | 同一事件的所有修订保持不变 |
| `source_event_id` | string，组合快照必填 | 固定选中来源的原始事件 ID |
| `external_fact_id` | string，网页事件必填 | 项目号、批件号等跨页面事实 ID |
| `event_code` | string | Catalog 中的完整事件代码 |
| `occurred_at` | timestamp/date/string，可空 | 事实发生时间，不作为默认信号时间 |
| `source_released_at` | timestamp/date/string，可空 | 来源首次发布时间 |
| `vendor_first_available_at` | timestamp/string，可空 | 供应商首次可用时间 |
| `ingested_at` | timestamp/string | 系统首次取得该版本的时间 |
| `replay_available_at` | timestamp/string，组合快照必填 | 经验证的历史首次可得时间，不使用离线下载日 |
| `revision_no` | non-negative integer | 同一 `event_id` 的修订序号 |
| `provider` | string，组合快照必填 | `eastmoney`、`ifind`、`rqdata`、`tushare` 或前向采集的 `web_archive` |
| `source_url` | string，组合快照必填 | 可审计的公告/来源链接 |
| `raw_response_sha256` | hex string，组合快照必填 | 原始供应商返回的 SHA-256 |
| `time_quality` | enum | `exact`、`vendor_observed`、`date_only_conservative` 或 `estimated_research_only` |
| `validation_status` | enum，组合快照必填 | `validated`、`unverified`、`blocked_time_quality` 或 `rejected` |
| `attributes_json` | JSON object string | 只允许标量属性，不允许可执行表达式 |

为避免 DuckDB 在缺少 `pytz` 时解码 `TIMESTAMPTZ`，Adapter 在 SQL 投影阶段把时间列转为文本，再用 Python 标准库解析。无时区时间戳会被拒绝；DATE 或 `YYYY-MM-DD` 只允许搭配 `date_only_conservative`，并统一解释为上海时间 15:00 供研究审计。旧的通用研究文件仍兼容精简列；`composite_snapshot` 必须使用上述完整 provenance 契约，并且只允许 `validated + exact/vendor_observed` 的秒级记录。

组合快照另外固定 `event_observations.parquet`，逐条保存各来源的原始 ID、外部事实 ID、真实采集时间、URL、响应 hash、时间质量和验证状态。`replay_available_at` 与 `ingested_at` 分离：前者控制历史信号，后者只做双时态审计。字段映射与生成命令见[多源事件数据](../runbooks/multi-source-events.md)和[网页发现事件](../runbooks/web-discovered-events.md)。

同一个 `event_id` 只在首个匹配的可见修订触发一次。后续修订不会重写已经发生的历史信号；若早期版本不满足属性过滤、后续修订首次满足，则只能在后续修订的 `available_at` 触发。

### 6.2 目录文件

- 覆盖清单：`catalogs/coverage/cn_a.v1.catalog.json`
- JSON Schema：`contracts/catalog-coverage.v1.schema.json`
- 权威模型：`ashare_lab/domain/catalog/coverage_models.py`
- 加载器：`ashare_lab/domain/catalog/coverage_loader.py`
- 确定性生成器：`catalogs/tools/build_coverage.py`
- 覆盖测试：`tests/unit/catalog/test_coverage.py`

校验命令：

```bash
.venv/bin/python catalogs/tools/build_coverage.py --check
.venv/bin/pytest -q tests/unit/catalog
```

加载器会拒绝重复 release、同一 catalog 的多个 active release、重复指标/事件 ID、重复 legacy 映射，以及缺少时间或 blocker 的虚假能力声明。快照和每个 release 都有规范化 SHA-256 内容哈希。
