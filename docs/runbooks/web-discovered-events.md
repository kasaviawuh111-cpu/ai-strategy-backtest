# 网页发现事件与历史回放

状态：`research/demo`，2026-08-29。

## 1. 边界

“能在网上搜到”只表示发现了线索，不表示策略当时已经知道，也不表示网页中的说法为真。
搜索结果、摘要、收录时间、HTTP `Last-Modified` 和模型记忆都不能直接生成交易信号。

统一链路是：

```text
搜索命中
  -> 原始发布者页面或授权新闻记录
  -> 固化 URL、原始响应 hash 与版本
  -> 事件分类和证券实体映射
  -> 事实 ID、时间和状态校验
  -> EventObservation
  -> 固定来源政策、冲突隔离
  -> 不可变 event/composite snapshot
  -> 回测 Worker 离线重放
```

这沿用 LEAN 的“只在现实可获得时间进入策略”、Zipline 的版本化数据 bundle，以及 LSEG
新闻的原始、更新、更正、删除版本链思路。参考：

- [LEAN 时间片与 Time Frontier](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/time-modeling/timeslices)
- [LEAN 自定义数据的可得时间警告](https://www.quantconnect.com/docs/v2/writing-algorithms/live-trading/reconciliation)
- [Zipline 数据 Bundle](https://zipline.ml4trading.io/bundles.html)
- [LSEG Machine Readable News 修订模型](https://developers.lseg.com/en/article-catalog/article/machine-readable-news-mrn-n2_ubms-comparison-and-migration-guide)

## 2. 第一批能力

现有 160 个事件代码已经包含新闻和网页中的典型事件，不再新建一套同义 taxonomy。

| 状态 | 事件 | 放行边界 |
|---|---|---|
| 可执行 | `event.contracts_orders.major_contract_won` | 必须是最终中标，并有 `materiality_status=material` 及可审计判断依据；中标候选、小额普通采购、预中标、入围、框架意向不算 |
| 可执行 | `event.macro_policy_industry.license_approval` | 必须是监管机构明确核准或批准；受理、拟批准、征求意见、待批不算 |
| 研究态 | 量产、产品发布、客户认证、停产与事故 | 术语和事实边界容易混淆，需要项目 ID、里程碑规则和更多权威证据 |
| 研究态 | 政策支持、产品涨价、行业事故 | 行业事实到单只股票的暴露映射带主观性，不能只凭搜索命中下单 |
| 结构化数据 | 龙虎榜、大宗交易、指数调整、两融、停复牌 | 应读取交易所、指数商或授权结构化数据，不走网页 NLP |
| 行情规则 | 涨停、放量、创新高、大跌反弹 | 从冻结行情确定性计算，不包装成新闻事件 |

事件属性首版仍只支持精确相等，金额大于某阈值等范围条件尚未进入 Strategy DSL。

## 3. 发现源、事实源与交易时钟

三个角色必须分开：

- **发现源**：搜索引擎、Tushare 新闻、媒体检索等，只负责找到候选；
- **事实源**：监管机构、法定招采平台、交易所互动平台、上市公司官网等原始发布者；
- **交易时钟**：经验证的供应商首次可得时间，或本系统前向采集器第一次实际抓到的时间。

首版来源政策：

| 事件通道 | 预先固定的使用方式 |
|---|---|
| 公司公告 | 延续东方财富 → iFinD → RQData → Tushare，不按事后最早时间选源 |
| 新闻/网页事件 | RQData 新闻作为候选主时钟，Tushare 补内容和发现；字段语义未验权前不得冒充正式首次可得时间 |
| 自建网页采集 | `web_archive` 只使用真实 `collector_observed_at`；历史网页落款不得回填 |
| 中标事实 | 法定招采平台或公司正式页面作为原始证据，必须有项目/中标编号 |
| 许可事实 | 发布许可的监管机构页面作为原始证据，必须有批件号 |

[RQData](https://www.ricequant.com/doc/rqdata/python/alternative-data) 的新闻记录具有
新闻 ID、秒级时间、原始时间、URL、来源及证券相关度，最接近可回放契约；但在米筐明确
`datetime` 是否代表首次入库/首次可得，以及修订方式之前，只能标记为待验证。
[Tushare news](https://tushare.pro/document/2?doc_id=143) 与
[major_news](https://tushare.pro/document/2?doc_id=195) 可补多媒体历史内容，但公开字段缺少
稳定文章 ID、修订号和原文 URL，不能单独担任正式历史时钟。

## 4. 网页证据契约

在统一事件字段外，网页事件必须保留：

```text
external_fact_id          项目编号、批件号等跨页面事实 ID
source_event_id           当前来源内稳定的记录 ID
canonical_url             原始发布者固定链接
publisher_name/type       发布者名称及类型
publisher_authority       authoritative / secondary
content_sha256            固化页面或授权正文 hash
source_published_at       页面标注的发布时间，可空
vendor_first_available_at 供应商首次可得或前向采集时间
first_seen_basis          licensed_vendor / prospective_collector / page_metadata_only
timestamp_precision       second / minute / date
revision_id/type/no       original、update、correction、delete 及序号
extractor_id/version      字段抽取器及版本
classifier_id/version     事件分类器及版本
entity_match_method       统一社会信用代码、精确法定名称等映射方式
entity_mapping_key/status 证券映射键及 exact 状态
mapping_confidence        实体映射置信度
review_status             accepted / pending / rejected
evidence_span             支撑判断的短证据片段或其 hash
materiality_status/basis  重大中标的重大性结论及判断依据
```

不同媒体转载同一事实时，通过 `instrument_id + event_code + external_fact_id` 聚类；各来源观察
仍全部保留。更新、更正和删除生成新 revision，不能覆盖旧版本。

## 5. 时间门禁

- 授权供应商：只有字段语义经过确认的秒级 `vendor_first_available_at` 才能进入历史回放；
- 自建采集器：使用第一次真实抓取的 `collector_observed_at`，可以从上线后积累历史；
- 页面只到分钟：可用于研究，不能假装补成 `:00` 秒进入当前 Demo；
- 页面只有日期：历史记录隔离；前向采集时只能从实际抓取时刻开始可用；
- 搜索结果的“几小时前”、收录时间和网页修改时间：一律不是历史首次可得时间；
- 日线回测：无论盘前、盘中还是盘后发现，统一到下一可交易日开盘尝试成交；
- 分钟回测：未来必须另接分钟行情和处理延迟，不能拿日线 OHLC 模拟新闻后的即时成交。

## 6. 真实页面核验样例

[中国证监会关于核准东方财富证券上市证券做市交易业务资格的批复](https://www.csrc.gov.cn/csrc/c101857/c7552337/content.shtml)
具备稳定页面 URL 和批件号 `证监许可〔2025〕689号`，能够确定事件类型与实体；但公开页面
只显示 `2025-04-02`，没有时分秒。因此历史导入必须隔离，不能在 2025-04-02 生成交易信号。

若系统在以后某一秒第一次抓到该页面，可以把那一秒作为 `prospective_collector` 的安全上界，
但只能从该抓取时刻向后使用，不能回填到 2025 年。

中国政府采购网页面通常有稳定数字 URL、项目编号和分钟级发布时间，适合建立前向采集；当前
Demo 仍要求秒级时钟，因此只有分钟的旧页面同样先隔离。

仓库内保留了这条真实页面的脱敏证据样例：
[`csrc-license-date-only.web-evidence.json`](../examples/csrc-license-date-only.web-evidence.json)。
它可以生成研究快照：

```bash
.venv/bin/python scripts/prepare_event_snapshot.py \
  --symbol 300059.SZ \
  --start 2025-01-01 \
  --end 2026-08-29 \
  --event-code event.macro_policy_industry.license_approval \
  --skip-eastmoney \
  --research-snapshot \
  --web-evidence-json docs/examples/csrc-license-date-only.web-evidence.json
```

2026-08-29 验证结果为：`observations=1`、`canonicalEvents=0`、
`quarantinedEvents=1`、`validation_status=blocked_time_quality`。去掉
`--research-snapshot` 后严格 Demo 会拒绝生成，而不是忽略或补造时间。

## 7. 当前可稳定返回的边界

- 事件类型：具备重大性依据的最终中标、业务许可明确获批；
- 历史供应商：只有授权账号能返回经确认的秒级首次可得时间、稳定记录 ID 和原文 URL 时放行；
- 自建网页源：只承诺采集器上线后的前向数据，使用真实首次抓取秒数；
- 实体：当前单股输入必须有 exact 映射；子公司事件还依赖 point-in-time 控股关系主数据；
- 执行：日线策略统一在下一可交易日开盘尝试成交；
- 降级：日期、分钟、搜索收录时间、页面修改时间和普通媒体转载只保留为研究证据。

因此，目前不能承诺“把过去十年网上所有新闻补成秒级事件”。能承诺的是：输入满足上述证据契约时稳定生成可重放事件；否则稳定返回明确的隔离原因。

## 8. 后续事件顺序

在不放宽门禁的前提下，下一批依次验证：量产/投产里程碑、客户认证、召回/停产/事故、重大产品发布。政策刺激、行业涨价和产业链传闻要先建立 point-in-time 暴露映射，暂不直接映射成单股买入信号。

## 9. 尚缺验证

- RQData 新闻真实账号返回、`datetime/original_time` 字段口径、修订行为和内部缓存授权；
- Tushare 新闻权限，以及原文 URL/稳定 ID 的补全方式；
- iFinD、Wind 新闻真实返回中的首次可得、故事 ID 与修订字段；
- 各法定招采和监管网站的 robots、使用条款、限流及长期归档方案；
- 批量证券实体别名、子公司关系的 point-in-time 主数据。

这些缺项会明确显示为 `pending`、`unverified` 或 `blocked_time_quality`，不会静默降级成可交易事件。

## 9. 结果展示门禁

进入回测的网页/新闻事件不能在结果页退化成一行“某事件发生”。事件信号节点必须展示完整到秒的 `available_at`，并保留 provider、source event ID、external fact ID、time quality、validation status、source URL、revision、原始响应/正文 hash、抽取与分类版本，以及本次 composite snapshot 和 Git SHA。signal、order、fill/partial/unfilled/expired 通过稳定关系 ID 组成同一条因果轨迹。

搜索摘要、当前抓取时间或只有日期的权威页面即使适合人工研究，也必须在前台标记为不可回测；不能为了让卡片完整而补造秒级时间。
