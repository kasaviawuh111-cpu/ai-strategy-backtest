# Choice Quant API 本地接入

Choice SDK 只用于后端离线采集。H5 不直接请求 Choice，回测运行时也不临时联网取数：

```text
Choice Quant API -> 原始响应校验 -> 版本化 Parquet 快照 -> 回测引擎
```

这样可以固定复权口径、记录取数时间与校验结果，并保证同一策略版本能够重放。

## 本机安装

SDK 是官方本地库，不是 PyPI 包。将官网下载的 `EMQuantAPI_Python.zip` 解压到仓库忽略目录后，用项目 Python 注册：

```bash
mkdir -p .vendor/emquantapi
unzip -oq ~/Downloads/EMQuantAPI_Python.zip \
  'EMQuantAPI_Python/python3/*' \
  -d .vendor/emquantapi
.venv/bin/python .vendor/emquantapi/EMQuantAPI_Python/python3/installEmQuantAPI.py
```

注册脚本只在 `.venv` 中写入 `EmQuantAPI.pth`，指向本机 `.vendor`；SDK、令牌和数据都不会进入 Git。

## 首次激活

不在代码、命令行参数或 `.env` 中保存 Choice 密码。使用官方上行短信流程生成绑定本机的 `userInfo` 令牌：

1. 用已绑定 Choice API 账号的手机号发送 `SXDL` 到 `9535711`。
2. 十分钟内运行 `.venv/bin/python scripts/emquant_activate_sms.py`。
3. 在本地终端输入手机号；成功后令牌位于 SDK 的 `libs/mac` 目录。

修改 Choice 密码、切换设备或令牌过期后，需要重新激活。账号默认不支持跨 IP、多进程同时登录；采集任务必须串行持有一个 SDK 会话。

## 最小权限验证

```bash
.venv/bin/python scripts/emquant_probe.py \
  --code 300059.SZ \
  --start 2026-07-01 \
  --end 2026-08-01
```

探针只执行四件事：令牌登录、A 股交易日历、不复权 OHLCV/成交额、流量余额查询。它不会运行 SDK 自带的 `demo.py`，也不会调用实时订阅、资讯、选股或组合交易接口。

## 回测口径

- 模拟成交和账户记账使用 `AdjustFlag=1` 的不复权价格。
- MACD、均线、RSI 等相对技术信号可使用 `AdjustFlag=2` 的后复权连续价格；绝对价格条件仍使用不复权价格。任何复权价格都不能直接用于模拟成交。
- 开盘成交容量默认只使用上一已完成交易日不复权日线的成交量 × 参与率；当日全天量在 09:30 尚不可知，不得参与当日开盘撮合。
- “同期持有”是同初始资金、同费用/滑点/整手/涨跌停/容量/公司行动的资金账户，不使用后复权价格首尾比。
- 交易日历由 `tradedates` 提供，不能从有无 K 线反推停牌。长区间按自然年有界请求，每批显式使用 `RECVtimeout=30`；读取仅对 `10002004`、登录仅对 `10002002/10002004` 做最多 3 次有限重试，权限或口径错误不重试。
- 历史停牌、ST、每日涨跌停价、公司行动与公告首次可得时间需要逐项验证权限和字段语义；未通过对照样本前，不进入正式回测。公司行动条款不能从复权差异推导。
- 每次采集保存请求参数、SDK 版本、取数时间、行数、日期范围和文件校验和；空响应或缺失交易日必须失败，不能静默换源。

## 生成可重放 Choice 基础快照

当前个人账号只允许生成 research Demo 快照。命令必须显式承认尚未取得历史 ST 事实：

```bash
.venv/bin/python scripts/prepare_choice_snapshot.py \
  --corporate-actions-json /secure/path/300059-corporate-actions.json \
  --research-assume-never-st
```

`--corporate-actions-json` 是必填输入，结构分为：

```json
{
  "coverage": {
    "status": "complete",
    "querySucceeded": true,
    "provider": "verified-provider",
    "start": "2021-02-07",
    "end": "2026-08-20",
    "rawResponseSha256": "<64 lowercase hex>"
  },
  "actions": []
}
```

`actions` 中每条记录按[公司行动与资金账户契约](../reference/corporate-actions-v1.md)提供 PIT/provenance 和经济条款。空数组只有在 `coverage` 证明完整查询成功且确实无动作时才合法。脚本会把该 JSON 纳入原始审计与 hash，并拒绝疑似凭证字段。若个人 Choice 权限不能返回完整公司行动条款，可以保留 Choice 失败响应审计，再使用经验证的官方公告或东方财富数据中心结果生成 provider JSON；不能从后复权价差猜分红送转。

默认快照日期为 `2021-02-07` 至 `2026-08-20`。其中用户可见回测区间仍是
`2021-08-06` 至 `2026-08-06`；额外的前置 180 天用于技术指标预热，后置 14 天用于
T+1、未成交重试和结算。不能只下载用户界面显示的日期。

每个版本位于 `var/snapshots/choice/<sha256>/`，包含：

- `raw/provider_response.json`：不含凭证的供应商响应审计副本；
- `raw/requests.json`：函数、指标、日期和复权参数；
- `daily_ohlcv.parquet`：`AdjustFlag=1` 不复权成交与账本价格；
- `signal_daily_ohlcv.parquet`：`AdjustFlag=2` 后复权技术信号价格；
- `corporate_actions.parquet`：独立公司行动事实、修订、来源、时间和经济条款；
- `instrument_sessions.parquet`：Demo 逐日交易规则事实；
- `snapshot_manifest.json`：文件 hash、数据能力、假设和限制。

快照发布器要求公司行动覆盖证明：查询必须成功、覆盖完整起止日并保存原始响应 hash。区间内 0 条动作也是一个可验证的查询结果；缺文件、失败查询或手工空文件都会拒绝发布。账本政策为 `cn.a_share.timeline_entitlement_receivable_settlement.date_only_cash_after_close_conservative.v2`：登记日锁权、除权日应收；现金派息只有日期时，派息日盘中仍保持应收，收盘后才转为可用现金，最早下一交易日可用于下单；股份按明确到账/上市/可卖日期盘前处理。现金分红使用 `gross_research_no_withholding.v1` 税前研究口径，未模拟个人按持有期补税。账户只保存整数股；送转计算出现不足 1 股权益尾数但缺最终整数登记证据时拒绝，不默认现金补偿。配股当前实现继续 fail closed，产品默认不认购政策尚待账本落地。

脚本还会在同一次采集中用较早截止日重复拉取后复权序列。重叠历史值只要超过容差，就拒绝
发布快照，以验证接口结果不依赖本次请求的结束日。这不能证明未来公司行动发生后供应商永不
修订历史复权值；已发布快照会固定旧值，新快照还需与上一个版本做跨快照差异审核。回测引擎
会同时固定不复权文件、复权文件和生产者 manifest；任一已固定文件变化都会导致重放失败。

运行技术 Demo 时使用脚本输出的路径：

```dotenv
DATA_ROOT=var/snapshots/choice/<sha256>
MARKET_DATA_PROFILE=choice_snapshot
SESSION_REFERENCE_MODE=parquet
SESSION_REFERENCE_PATH=var/snapshots/choice/<sha256>/instrument_sessions.parquet
EVENT_DATA_REQUIRED=false
```

`MARKET_DATA_PROFILE=choice_snapshot` 会强制校验信号文件、公司行动、manifest、内容寻址目录和 manifest
登记的全部文件 hash；缺少或篡改任何一个文件时服务不会静默退回不复权信号。该模式的
`/ready` 会单独显示 `signal_data`、`corporate_actions` 与 `snapshot_manifest`。

`EVENT_DATA_REQUIRED=false` 只表示该服务实例是“技术策略 Demo”。`/capabilities` 会返回
`event_backtest_available=false`；提交事件策略会明确返回 `422 event_data_unavailable`，不会
用空数据假装成功，也不会退化为通用 500。

## 一分钟数据接入与验证

Choice Python SDK V2.7.5.0 提供历史分钟 K 线函数 `c.cmc`。仓库已接入以下数据层能力：

- `scripts/emquant_minute_probe.py`：单股、单交易日只读权限与数据质量探针；
- `scripts/prepare_choice_minute_snapshot.py`：分段采集并生成内容寻址快照；
- `minute_ohlcv.parquet`：不复权 OHLCV/成交额，只用于执行、容量和账本；
- `signal_minute_close.parquet`：后复权分钟收盘价，只用于盘中技术信号估算；
- `MARKET_DATA_PROFILE=choice_minute_snapshot`：运行前校验 manifest、覆盖、行数、原始响应审计和全部文件 hash。

发布器只接受每个普通 A 股连续竞价交易日精确 240 根的完整分钟轴，时间标签必须由真实完整会话校准为 `bar_start` 或 `bar_end`。每根 K 线只有在 `bar_end_at` 后可见；触发分钟不能用自己的收盘价成交。分钟聚合必须与 Choice 日线对账，较早截止日的 240 根完整首会话必须是长区间的严格前缀且数值不变。缺行、重复、错日期、价格口径混用、未来采集时间、篡改快照或畸形 SDK 维度均拒绝发布。

先运行单日探针，不要直接消耗多年额度：

```bash
.venv/bin/python scripts/emquant_minute_probe.py \
  --code 300059.SZ \
  --trade-date 2026-08-27
```

2026-08-29 在当前网络的真实结果为：登录成功，但 `c.cmc` 返回 `10000017 overseas ip is restricted`。因此目前只能确认 SDK 有该函数和本地接入代码可运行，不能确认个人基础账号实际具有分钟权限、可查历史深度或五年连续覆盖，也没有发布真实分钟快照。

切换到合规的中国大陆网络后，先重跑上述探针；只有 `ready_for_snapshot=true` 才允许用两个以上交易日的小区间生成快照：

```bash
.venv/bin/python scripts/prepare_choice_minute_snapshot.py \
  --symbol 300059.SZ \
  --start 2026-08-26 \
  --end 2026-08-27
```

当前接通的是分钟数据层，不是完整盘中回测：Strategy DSL、日线 MACD 盘中估算 runtime、下一可交易分钟撮合、分钟涨跌停/交易状态和公司行动日验收仍未接入。前端继续把盘中回测显示为“待接入”，不得用日线结果替代。

## 当前基础版权限实测

实测日期：2026-08-29；证券：东方财富 `300059.SZ`。

- 令牌登录、A 股交易日历、流量查询可用。
- `AdjustFlag=1` 不复权与 `AdjustFlag=2` 后复权序列均可用。
- 2021-08-06 至 2026-08-06 返回 1,211 个市场交易日和 1,211 条日线，无缺行、重复或空值。
- `HIGHLIMIT`、`LOWLIMIT`、`TRADESTATUS` 可用；前两者是“当天是否涨停/跌停”标记，不是涨跌停价格。
- 样本识别到 3 个涨停日，但没有一字涨停；不能把普通涨停直接判为无法成交。
- `regularreport` 返回 `10001012 insufficient user access`，当前基础版不能承担正式定期报告事件回测。
- 独立复权因子和历史 ST 字段尚未通过指标校验；在得到准确字段或替代权威来源前保持不可用。
- Choice 基础版权限尚未证明能一次返回公司行动的登记日、除权日、派息/到账/可卖日和全部经济条款；新快照必须由上述真实 provider JSON 补齐并独立核验，不能沿用旧快照。

## 2026-08-29 旧链路记录

- 已生成版本 `choice:9865254d1c752f69953fdd62a2aabbf495f4902a7081ea09694e0def18f764c8`；
- 不复权、后复权与 session 各 1,340 行；
- 后复权前缀稳定性覆盖 1,098 行，最大差异为 0；
- 真实 API MACD 回测成功：1,212 个净值点、237 条活动、39 笔完整交易；
- 严格模式回放 run：`run:6d45a57831db4fc186d26f9016dde895`；
- 当时总收益约 `4.52%` 只证明双价格工程链路曾经可运行，不代表策略有效；
- 该快照早于公司行动覆盖、PIT 容量和 funded benchmark 门禁，不能继续作为本轮验收结果。公司行动账本代码已落地，但必须生成新 Choice/composite 并重跑；历史 ST 权威事实和正式数据授权仍未解决。
