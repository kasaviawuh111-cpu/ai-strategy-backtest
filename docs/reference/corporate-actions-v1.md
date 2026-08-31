# A 股公司行动与资金账户契约 v1

公司行动文件是资金账户回放的必需事实，不是复权价格的附属说明。技术信号可以使用后复权连续价格；模拟成交、现金、持仓、应收和同期持有账户必须使用不复权价格与本契约中的经济条款。当前账本政策 ID 为 `cn.a_share.timeline_entitlement_receivable_settlement.date_only_cash_after_close_conservative.v2`。

## 1. 覆盖证明

严格 Choice/composite 快照必须包含 `corporate_actions.parquet` 及 manifest 中的覆盖证明。公司行动只在 fresh submission 的准备阶段联网采集，随后发布内容寻址、不可变的 producer snapshot；Worker 只按 RunManifest 中的精确 snapshot identity 本地重放，缺文件、身份不符或校验和变化即失败，不在运行时补抓。覆盖证明至少记录：

- 供应商和查询成功状态；
- 覆盖起止日，必须包住快照区间；
- 原始响应 SHA-256；
- 文件 SHA-256、行数和 schema 版本。

当前内部 Demo 的严格混合覆盖口径为：

- 东方财富 `RPT_SHAREBONUS_DET` 对现金分红、送股/转增提供按证券过滤且完整分页的动作主表；`BonusFinancing.PageAjax` 只作为最近记录的派息日补充，不能被表述为全历史完整来源；
- 当 `BonusFinancing.PageAjax` 已把较早记录挤出返回窗口时，准备阶段必须先完整分页并哈希动作 `NOTICE_DATE` 当天的公告列表，再只下载官方栏目 `001002002001005`（分配方案实施）的实施公告。候选必须同时绑定证券代码、报告期、登记日、除权日、税前每 10 股金额，并从冻结正文中解析出唯一明确的现金划入/发放日期；缺字段、不一致、两个候选或只有除权日时全部 fail closed，绝不以除权日猜派息日；
- 东方财富 `RPT_IPO_ALLOTMENT` 对配股提供正向完整查询，0 条也要保存成功查询与响应证据；
- 东方财富 `RPT_F10_EH_EQUITY` 全证券历史对拆股/缩股提供负向证明；区间内每个股本变动原因都必须能确定为非拆并股，未知原因或拆并股候选一律 fail closed；
- manifest 必须逐类对账动作数、正向 0 结果和拆并股候选数，不能用一份总行数替代分类覆盖。

区间内没有公司行动时，仍需成功的完整范围查询及其响应 hash。缺文件、查询失败、覆盖范围不足、拆并股负证明不完整或只提供空文件时 fail closed。

当前内部 Demo 会固定公告列表页 response hash、content/document hash、抽取后的规范正文和逐页证据，并把它们纳入公司行动 action/coverage hash；但尚未把所有 list/content 原始 wire bytes 与 PDF 放入可按 hash 找回的不可变对象存储，因此只能称为 hash-evidenced 内部回放，不能声称生产级离线原始证据归档。

## 2. 一条公司行动事实

主键为 `action_id + revision_no`。核心字段分为四组：

| 组 | 字段 | 作用 |
|---|---|---|
| 身份 | `stock_code`、`action_id`、`source_action_id`、`action_type`、`revision_no` | 固定动作与修订 |
| 时间 | `record_date`、`ex_date`、派息/到账/可卖日期 | 驱动权益、应收和结算 |
| 可得性 | `source_released_at`、`vendor_first_available_at`、`replay_available_at`、`ingested_at`、`time_quality` | 防止事后数据进入历史回放 |
| 来源 | `provider`、`source_url`、`raw_response_sha256`、`validation_status` | 审计和重放 |

经济条款按类型互斥：

- `cash_dividend`：每股税前现金、派息日；
- `share_distribution`：股份乘数、到账日、可卖日；
- `stock_split` / `reverse_split`：股份乘数、到账日、可卖日；
- `rights_issue`：配股比例、认购价、缴款截止日、上市日。

条款不能从后复权价差反推。严格账户只使用 `validation_status=validated` 且在登记日收盘前已可知的版本；迟到或冲突修订不得倒改已经发生的权益。

## 3. 账户状态转换

```text
登记日收盘持仓
→ entitlement：锁定应享股数
→ 除权日前一瞬 accrual：形成现金/股份应收
→ settlement：日期级现金派息在当日收盘后转可用；股份按明确到账/可卖日期盘前处理
```

- 现金应收计入净值，但结算前不能用于下单。来源只有派息日期、没有日内到账时间时，派息日交易时段内继续作为应收，15:00 收盘后才转为可用现金，最早下一交易日可用于下单；不得声称具备开盘前或盘中到账证据；
- 股份应收计入经济持仓估值；股份仍按来源明确的到账日、上市日和可卖日盘前处理，达到可卖日之前不能卖出；
- 支持的动作在账户中幂等，每个动作修订的每个阶段只能应用一次；
- 账户股数始终为整数；送转/拆并股保持账户总成本守恒。计算出现不足 1 股权益尾数时，只有最终整数登记结果或可重放的登记分配证据才能入账，否则拒绝；不默认现金补偿；
- 现金分红政策为 `gross_research_no_withholding.v1`：按来源税前总额形成应收并到账，**不模拟个人投资者按持有期补税/扣税**；涉及分红的收益只能解读为研究税前口径；
- 配股默认政策为 `decline_no_external_cash.v1`，已经进入策略账户和同期持有账户：记录 `rights_issue_declined_no_external_cash`，不认购、不注入外部现金，也不改变持仓经济状态。只有未来用户显式选择认购且条款、现金、缴款期和上市可卖日均完整时，才可发布另一条版本化政策；不得静默从默认“不认购”切换为认购。

## 4. 与同期持有的关系

策略账户和同期持有账户读取同一份公司行动文件并经过相同状态转换。净值均包含：

```text
可用现金 + 现金应收 + (已到账股份 + 股份应收) × 当日不复权收盘价
```

因此“跑赢/跑输同期持有”比较的是同资金、同市场约束下的两条账户曲线，不是策略账户与一条理想化复权价格曲线的比较。

## 5. 拒绝条件

以下任一情况不得静默忽略：

- 公司行动覆盖证明缺失或范围不完整；
- 经济条款在登记日收盘后才首次可得；
- 同一动作修订冲突或同一除权日存在未归一化的多条送转；
- 送转计算出现不足 1 股权益尾数，但没有最终整数登记结果或登记分配证据；
- RunManifest 的配股政策不是已实现的 `decline_no_external_cash.v1`，或配股事实缺少审计所需的比例、价格、缴款截止日和上市日；
- 对外声称现金分红已经覆盖个人持有期税制；当前只支持明确标注的税前研究口径；
- 账户动作无法按 `action_id + revision_no + phase` 幂等重放。

## 6. 当前真实数据证据边界

2026-08-30 在 dirty worktree 上 fresh 采集并发布了 `300059.SZ`、2021-08-06 至 2026-08-06 的 Choice 快照 `choice:a2eebf97b7c2ed4f2467ca6f91a3e95f48ed609c4026b440a16e7a11a4a23b4f`。它包含 1,211 行执行日线、1,211 行信号日线、1,211 行 session 和 7 条公司行动（5 条现金分红、2 条送转）；配股为 0 条正向结果，拆股/缩股均为 0 个候选且通过负向证明。后复权前缀检查覆盖 969 行，差异 0，状态为 `passed`。

该证据仅为 `local-real-data-verified`：证明这一只股票、这一段区间和本地严格发布链，不证明全 A 股、clean-SHA Live E2E、生产授权或不可变对象存储已经完成。
