# 东方财富日线资金流研究快照

本模块只采集东方财富公开 Push2 历史日线资金流，用于研究和接口口径观察。它没有接入回测运行时，也不能包装成“主力真实账户买卖”。

## 数据边界

- 接口：`https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get`
- 数据集标识：`stock_fflow_daykline`
- 频率：仅日线 `klt=101`
- 接口配置请求上限：本采集器最多请求 120 行；实际返回可少于 120 行
- 覆盖承诺：没有形成历史覆盖 SLA，也没有“稳定返回约 120 个交易日”的结论
- 口径：东方财富供应商定义未知，快照固定标记 `methodologyUnknown=true`
- 历史分钟：未获得，也不推导或伪造

2026-08-29 的最近一次真实冒烟在收到正文前被服务端断开，因此没有生成真实快照。MockTransport 契约测试只能证明解析与保护逻辑，不能证明公开端点当前可用。

`f51-f63` 按公开响应顺序保存：

| 字段 | 快照字段 | 单位/含义 |
|---|---|---|
| `f51` | `tradeDate` | 交易日期 |
| `f52` | `mainNetInflowCny` | 主力净流入，供应商标签，元 |
| `f53` | `smallNetInflowCny` | 小单净流入，元 |
| `f54` | `mediumNetInflowCny` | 中单净流入，元 |
| `f55` | `largeNetInflowCny` | 大单净流入，元 |
| `f56` | `extraLargeNetInflowCny` | 超大单净流入，元 |
| `f57-f61` | 各档净流入占比 | 百分比 |
| `f62` | `closeCny` | 收盘价，元/股 |
| `f63` | `changePct` | 涨跌幅，百分比 |

采集器校验 `f52 ≈ f55 + f56`，允许最多 1 元或十亿分之一的相对舍入误差。该恒等式只检查字段完整性，不能证明大单划分方法正确。

## 时间政策

公开响应没有可信的历史首次可得时分秒，所以不得给每个历史交易日拼接一个 16:05 并当成 `known_at` 或 `available_at`。快照采用以下保守语义：

- `requestStartedAt` 和 `responseReceivedAt` 都由 adapter 内部时钟生成，调用者不能传入或倒填；
- `retrievedAt=responseReceivedAt`；
- 所有返回行的 `historicalKnownAt=retrievedAt`，只表示本次采集后才知道这些值；
- `eligibleForHistoricalBacktest=false`；
- `earliestExecution=not_applicable`。

`Asia/Shanghai 16:05:00` 只保留为 `collectionSessionPolicy.stabilityMarkerLocalTime`。它仅用于当前响应包含当日交易行时，阻止 16:05 前收藏可能尚未稳定的当日值；它不是历史可得时间，也不适用于此前交易日。快照固定 `isHistoricalAvailabilityTimestamp=false`。

## 生成快照

```bash
.venv/bin/python scripts/prepare_fund_flow_snapshot.py \
  --symbol 300059.SZ \
  --limit 120 \
  --output-root var/snapshots/fund_flow_research
```

输出是内容寻址的单个 JSON 文件。发布先写同目录临时文件、`fsync`，再通过 `os.replace` 原子替换。文件内固定：

- 完整请求 URL 和参数；
- adapter 生成的 `requestStartedAt`、`responseReceivedAt` 和 `retrievedAt`；
- HTTP 正文字节的 `rawWireSha256`，并明确其 wire-byte 语义；
- 可由 loader 重算的 `rawPayloadCanonicalSha256` 及规范化规则；
- 原始解码响应和逐行原文；
- provider、dataset、字段映射；
- `researchOnly=true`、`methodologyUnknown=true`；
- `networkFetchAllowedDuringBacktest=false`；
- 不可用于历史回测的标记、当前会话 16:05 稳定标记和请求上限。

回测不得调用在线采集器；当前这类事后采集的历史行连离线历史回测也不得进入。只有未来取得可验证的逐时点历史版本，并另行建立 point-in-time 契约后，才可讨论回测接入；届时仍只能读取固定快照，并标注“东方财富供应商口径，算法未知”。

loader 除了验证内容哈希，还会验证文件名 digest、原始 payload 规范化哈希、非空且日期升序唯一的 rows、采集时钟先后顺序，以及 `researchOnly`、`methodologyUnknown`、禁止运行时联网、不可用于历史回测等语义。即使有人重算外层哈希，改变这些保护字段也会失败关闭。

## 失败关闭

以下情况不写快照：HTTP/JSON 失败、`rc != 0`、空响应、证券身份不符、非 13 列、缺失或非数值字段、重复日期、超过请求行数、`f52` 分解不一致、响应时钟倒退、未来交易日期，以及包含当日行但尚未到 16:05。接口字段变化必须先更新契约与 MockTransport 测试，不能静默兼容。
