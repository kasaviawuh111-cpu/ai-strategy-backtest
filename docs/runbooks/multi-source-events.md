# 多源事件数据与回放

事件链路固定为：

```text
东方财富公告 + 同花顺 iFinD + RQData + Tushare + 权威网页证据
  -> 原始观察与响应 hash
  -> 固定优先级融合、冲突隔离
  -> 事件内容寻址快照
  -> 与 Choice 技术快照组成 composite 快照
  -> 回测 Worker 只读 Parquet，不联网
```

## 来源职责

| 顺序 | 来源 | 采用的时间字段 | 当前接入状态 |
|---|---|---|---|
| 1 | 东方财富公告服务 | `display_time`、`eiTime` | 公共接口适配器已接通；`eiTime` 只视为供应商观测时间，不冒充交易所认证的首次发布时间 |
| 2 | 同花顺 iFinD | `ctime` | Mapping/可注入客户端适配器已完成；本机尚无账号实联权限证据 |
| 3 | RQData | `info_date`、`create_tm` | Mapping/可注入客户端适配器已完成；`create_tm` 可作为秒级供应商时间 |
| 4 | Tushare | `rec_time` | Mapping/可注入客户端适配器已完成；本机尚未用 Token 实联 |
| 独立事件通道 | `web_archive` | 前向 `collector_observed_at` | 仅接受真实首次抓取；旧网页落款不能回填 |

顺序是数据政策，不是运行时竞速：只要东方财富记录通过验证，它就是主记录；其余来源补属性、做交叉验证和降级。绝不在回测结束后挑四家中时间最早的一条。

## 可交易门禁

进入 Demo 的 canonical 事件必须同时满足：

- 时间归一为 `Asia/Shanghai`，保存到秒；供应商给毫秒时向上进位，不能截断回填；
- `validation_status=validated`；
- `time_quality=exact` 或 `vendor_observed`；
- 有 `source_event_id`、`provider`、`source_url`、`raw_response_sha256` 和真实 `ingested_at`；
- 同一事件跨源匹配无事件码、日期、事实属性或身份歧义。

只有日期、`00:00:00`、`12:00:00` 占位时间、异常回填时间以及冲突记录仍写入 `event_observations.parquet` 和隔离清单，但不会写入可交易 `events.parquet`。因此日期级 BaoStock/Choice 数据可以研究，不能通过本 Demo 验收。

`ingested_at` 是本系统实际采集时间。历史回放另存 `replay_available_at`，它只来自固定选中来源的历史发布/供应商首次可见时间；否则 2026 年下载的 2024 年公告会被错误地推迟到 2026 年才可见。

## 东方财富真实脱敏样例

探查时间：2026-08-29；接口：`np-anotice-stock` 公告列表；证券：`300059.SZ`。以下只保留字段映射所需内容：

```json
{
  "art_code": "AN202608271828551090",
  "title": "东方财富:东方财富信息股份有限公司关于完成工商变更登记的公告",
  "display_time": "2026-08-27 18:34:11:530",
  "eiTime": "2026-08-27 18:35:16:000",
  "notice_date": "2026-08-27 00:00:00",
  "columns": [{"column_code": "001002005001007", "column_name": "公司注册资本变更"}]
}
```

规范化结果：

| 统一字段 | 值/规则 |
|---|---|
| `source_event_id` | `AN202608271828551090` |
| `source_released_at` | `2026-08-27T18:34:12+08:00`，毫秒向上进位 |
| `vendor_first_available_at` | `2026-08-27T18:35:16+08:00` |
| `replay_available_at` | 两个历史时间取较晚值，即 `18:35:16` |
| `ingested_at` | 本次真实采集时钟，单独审计，不参与历史可得时间 |
| `time_quality` | `vendor_observed` |
| `validation_status` | `validated` |

这个样例属于“工商变更”，会保留在观察层；它不在当前 69 条可执行事件定义中，因此不会混入事件策略。

同日还用适配器重新读取了可执行年报 `AN202403141626765931`，脱敏规范化结果为：

```json
{
  "provider": "eastmoney",
  "source_event_id": "AN202403141626765931",
  "event_code": "event.financial_results.annual_report",
  "source_released_at": null,
  "vendor_first_available_at": "2024-03-14T20:57:33+08:00",
  "ingested_at": "2026-08-29T13:23:58+08:00",
  "time_quality": "vendor_observed",
  "validation_status": "validated",
  "raw_response_sha256": "5afd37736349f7adcf8104dc4e0c9e33339e680e371cef9dd2be2bb38cebcb43"
}
```

该记录的 `display_time` 为空，所以没有捏造来源发布时间；研究回放保守使用供应商观测字段 `eiTime` 的 `20:57:33`。这只能证明东方财富记录了这个时点，不能证明它是交易所或全市场最早可见时间。真实采集发生在 2026 年，仍单独保存在 `ingested_at`。

## 当前覆盖范围

当前能力必须分四层描述，不能混写成“69 类都有数据”：

- 69 条 registered/executable code path：事件定义可校验、可由统一运行时评估；
- 69 条自然语言入口：只接受明确生命周期阶段，泛称、否定、候选和澄清语义 fail closed；
- 东方财富公告适配器：5 类财报栏目映射，加 63 条确定性标题规则；分类仍依据栏目与标题，完整正文/PDF 只作为冻结的审计证据，并未做正文语义确认；
- 旧 v1 composite 的 1 类年报、1 条记录只保留为迁移/历史证据，不能外推为当前 strict v2 E2E；后续五类定期报告 Event v2 曾固定 27 条严格观察，但其 Composite 已因最新公司行动 coverage 门禁而失效。当前没有通过最新 loader 的事件 Composite，也没有 clean-SHA Live 双策略验收。

东方财富 `eiTime` 只设置了 2017 年后的时间合理性门禁；2017 年以前发现过历史回填，适配器直接降级/拒绝。这个门禁不等于已经证明 2017 年以来任意股票、任意区间都具有完整秒级公告覆盖。

iFinD、RQData 和 Tushare 的字段适配与离线契约测试已完成，但“账号可访问的历史起点、调用量、修订历史、许可范围”取决于实际账号。未提供账号/导出时会明确显示 `not_configured`，不会假装已覆盖。

因此当前稳定承诺只到：**69 个已注册、可编译、能进入统一运行时的代码路径；某条历史记录只有通过固定来源、完整文档、秒级供应商时间和冲突门禁后才可交易。** 目前尚未生成能证明指定股票、完整回测区间和目标事件集合均已查询成功的新 acquisition coverage；在该证明进入 snapshot manifest 并由提交端校验前，不能把“快照里没有记录”解释成“区间内确实没有事件”。两类网页事件只接受授权供应商的已验口径历史，或采集器上线后的前向记录。不会把旧网页日期、搜索收录时间或当前抓取时间倒填成历史信号。

## 生成快照

默认在线采东方财富；其余三家可先从各自 SDK/API 导出 JSON 数组后接入，不把密钥写入文件：

```bash
.venv/bin/python scripts/prepare_event_snapshot.py \
  --symbol 300059.SZ \
  --start 2017-01-01 \
  --end 2026-08-29 \
  --ifind-json /secure/path/ifind_rows.json \
  --rqdata-json /secure/path/rqdata_rows.json \
  --tushare-json /secure/path/tushare_rows.json \
  --web-evidence-json /secure/path/validated_web_evidence.json \
  --choice-snapshot var/snapshots/choice/<sha256>
```

供应商 JSON 每行要么带 `event_code`，要么命令只传一个 `--event-code`。脚本会拒绝包含 `token`、`password`、`userInfo`、手机号等疑似凭证字段的导出文件。

只有日期或证据待审的数据可用 `--research-snapshot` 固化到观察层和隔离清单；该模式不能与 `--choice-snapshot` 组合，也不能进入 Demo 回测。

输出分两级：

- `var/snapshots/events/<sha256>/`：`events.parquet`、`event_observations.parquet`、隔离清单和事件 manifest；
- `var/snapshots/composite/<sha256>/`：Choice 双价格流、公司行动、session、事件/观察及各来源 manifest。

运行组合 Demo：

```dotenv
DATA_ROOT=var/snapshots/composite/<sha256>
MARKET_DATA_PROFILE=composite_snapshot
SESSION_REFERENCE_MODE=parquet
SESSION_REFERENCE_PATH=var/snapshots/composite/<sha256>/instrument_sessions.parquet
EVENT_DATA_REQUIRED=true
```

`composite_snapshot` 会校验内容寻址目录、所有登记文件 hash、公司行动覆盖、秒级事件政策和事件观察文件。strict 启动还要求当前工作树对应的精确 40 位 Git SHA；dirty、伪 revision 或回退到 generic/Choice-only profile 时 `/ready` 不通过。任一文件变化，提交和 Worker 重新固定出的 data snapshot hash 都会不同，已排队 run 会拒绝继续。

技术与事件 run 均使用 `funded_buy_and_hold.same_execution_ledger.v1` 和 `cn.a_share.timeline_entitlement_receivable_settlement.date_only_cash_after_close_conservative.v2`；事件策略不能因为事件来自另一条数据通道而改用价格首尾基准、假定现金盘中到账或绕过公司行动账本。

结果活动必须沿同一个 `chain_id/decision_id/order_id/fill_id` 展示因果链。事件信号节点保留完整到秒的 `available_at`，并显示 provider、time quality、source event ID/URL、原始响应 hash 和 snapshot 身份；前端截断到分钟或只写“年报发布”不满足验收。

## 尚缺权限/证据

- 同花顺 iFinD、RQData、Tushare 真实账号的历史范围、配额和许可尚未实联；
- 东方财富公共接口没有生产 SLA 或正式授权承诺，正式产品需商务/法务确认；
- 东方财富单页公告固定原始正文，多页公告固定完整 PDF；其他机构源仍需逐一确认正文归档与 hash 能力；
- 事件 acquisition coverage 尚需固化请求股票、起止日期、事件集合、来源状态、分页总数与零结果证据，并在提交时按策略范围 fail closed；
- 同花顺 iFinD、RQData、Tushare 的真实账号联调完成后，还要对同一批公告做跨源差异报告；当前真实 E2E 只使用东方财富主源。

## 2026-08-29 历史链路证据

- 真实年报公告：`AN202403141626765931`；
- 事件快照：`events:9dfae6b8…d6bff`，1 条 canonical 事件；
- Choice + 事件组合快照：`composite:69a25fae…f5e7`；
- API 编译“年报发布后买入，MACD 死叉卖出”并提交 Worker 成功；
- run：`run:b6edeb65765c47a28b69299403390c03`，当时状态 `succeeded`；
- 返回 1,212 个净值点、6 条活动、1 笔完整交易，总收益约 `-6.90%`。

这条记录证明过事件采集、快照、信号、撮合和结果接口能够贯通，但它早于公司行动覆盖、funded benchmark、PIT 容量和 clean Git evidence 门禁，且当前运行库未必仍保存该 run，因此不能继续作为本轮最终验收。新链路必须用新的 strict composite 重跑并持久化。

即使新运行成功，一条 canonical 年报事件和一笔完整交易也只能证明工程链路，不能评价策略有效性。前台必须标明当前单一东方财富真实样例、其他三家尚未实网验证和事件样本不足。
