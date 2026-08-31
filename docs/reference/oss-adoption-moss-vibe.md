# MOSS 与 Vibe-Trading 胶水化采用方案

## 1. 决策

本项目不合并三套完整引擎。`astock-backtest-engine` 继续作为唯一 A 股可信内核；
[HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 提供可复用的数据采集、
自然语言工作流和稳健性统计模块；
[MOSS](https://github.com/moss-site/moss-trade-bot-skills) 提供指标公式候选、固定数据指纹和
回放一致性方法。接入只发生在端口与 adapter 层，第三方对象不得成为领域模型或运行清单的真相。

本轮已经有五个窄 adapter；代码、夹具、旧 provider 公网冒烟、当前 Live 和生产状态必须分别描述：

- `ashare_lab/adapters/language/vibe_candidates.py`：受限 JSON 策略候选与确定性规则快路；模型
  transport/bootstrap 配置路径已存在，旧 provider 路径有公网冒烟记录，当前 revision
  的完整 provenance 与 API/H5 仍为 `Live-unverified`；
- `ashare_lab/adapters/language/vibe_ideas.py`：借鉴 Vibe 的“模糊需求给 2—3 个方向”与假设工作流，产出
  不可执行的 `idea-route.v1`。它只把当前权威 A 股页当价格代理，只能选择服务端固定模板，用户选择后再回到
  原编译器；当前仅 `implemented / fixture-tested`，没有 Live 证据；
- `ashare_lab/adapters/signals/moss_pandas_backend.py`：真正调用 MOSS 同款 pandas 路径的差分 provider；
  EMA、MACD 已在显式 `1e-12` 容差内通过 A 股口径对拍，RSI 因 seed/零损失语义不同被排除；
- `ashare_lab/adapters/market_data/vibe_mootdx.py`：严格日线查询形状、单位转换和 coverage 证据；只用 fake
  client 做过夹具验证。`mootdx` 本身的 MIT 文件与 README“不得用于商业目的”声明互相冲突，因此依赖
  暂不安装，等待法务和底层行情使用条款确认；也没有联网取数或发布不可变快照。
- `ashare_lab/adapters/market_data/eastmoney_daily.py`：按固定 AKShare commit 适配完整 Push2 日线请求协议；
  AKShare 不作为运行依赖。本地增加 A 股身份、raw hash、量纲、区间、session 与公司行动门禁；直连被
  远端断开，尚无真实 batch、不可变 snapshot 或生产授权。它与研究用 Push2 主力资金不是同一数据集。

这些 adapter 均有各自的定向夹具；历史 `85 passed` 只对应 2026-08-30 当时的四个 adapter，不能冒充新增
`idea-route.v1` 或当前 clean-SHA Live E2E。上游包和源码树没有被整体导入；本地 adapter 只固定引用来源
commit 和窄接口，真实数据、前端与生产授权仍需独立验收。

机器可读清单见仓库根目录的 [`THIRD_PARTY.yml`](../../THIRD_PARTY.yml)，许可证要求摘要见
[`THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md)。

## 2. 固定版本

| 项目 | 固定版本 | 许可证 | 当前角色 |
|---|---|---|---|
| `astock-backtest-engine` | clean base `7a3a9cc1861f4b3006d0f000e0674355229da02d`；审计工作树另有未提交改动 | MIT | 第一方 A 股领域契约和可信执行内核 |
| [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading/tree/e90b6c6cd9fea23067a85667e7fbf74f9d73ea48) | `0.1.14` / `e90b6c6cd9fea23067a85667e7fbf74f9d73ea48` | MIT | 数据采集、候选生成工作流、稳健性统计候选 |
| [MOSS](https://github.com/moss-site/moss-trade-bot-skills/tree/1fce09b03151a01ba1dee230466ec64cdd12fda8) | `v1.0.28` / `1fce09b03151a01ba1dee230466ec64cdd12fda8` | MIT-0 | 指标与回放一致性候选，不是 A 股引擎 |
| [bukosabino/ta](https://pypi.org/project/ta/0.11.0/) | 2023 PyPI sdist `0.11.0` / SHA-256 `de86af…493fd`；另审 2026 master `a8904107`，两者不等同 | MIT | 仅作指标差分 oracle 候选；未安装、未执行、非运行依赖 |
| [AKShare](https://github.com/akfamily/akshare/tree/8e95744b79ae22326308ccd2b4e62650c5b53c55) | `8e95744b79ae22326308ccd2b4e62650c5b53c55` | MIT | 仅固定 `stock_zh_a_hist` 的 Push2 请求协议；不作为运行时依赖 |

“固定版本”表示原四个窄 adapter 的审计来源，不表示两套上游产品已整体采用。升级必须重新做
许可证、差异和门禁审计。

新增 `vibe_ideas.py` 使用的 hypothesis/autopilot pattern 已固定到
`1ee7df16af6eed8831014fa16ec0a9cb2d35f4e7`；该 revision、精确上游路径和采用边界已补入
`THIRD_PARTY.yml` 与 notices。两次审计 revision 的 MIT 许可证字节 SHA-256 相同，继续复用仓库中已归档的
精确许可证文本。完成 provenance 登记不等于 Live 证据，`idea-route.v1` 仍只标
`implemented / fixture-tested`。

## 3. 目标结构

```text
用户自然语言
  -> 已是完整策略：CandidateGenerator（规则快路 + 已配置但需当前版本复验的 VibeBounded provider）
  -> 纯观点：IdeaRouter（理解 -> 待检验假设 -> 当前 A 股页价格代理 -> 2—3 个固定模板候选）
  -> 用户选择候选后重新进入 CandidateGenerator
  -> CandidateAst / StrategySpec（本项目唯一 DSL）
  -> IndicatorBackend（MOSS/pandas adapter 已夹具双跑；仅 EMA/MACD 进入采用白名单）
  -> SnapshotAcquirer（VibeMootdx 与 Push2 日线 adapter 已夹具验证，仅采集期允许联网）
  -> 不可变 Event v2 + Composite v2（运行期只读）
  -> A 股订单、账本、公司行动、因果轨迹（本项目唯一裁决）
  -> ValidationProvider（现有压力场景 + Vibe 统计插件）
```

第三方复用的意义是删掉通用算法和连接器的重复建设，不是绕过 A 股点时证据和成交规则。

## 4. 精确替换矩阵

| 当前责任/位置 | 上游候选 | 采用方式 | 胶水层输出 | 明确不采用 | 替换状态 |
|---|---|---|---|---|---|
| 中文完整策略：`rule_based.py` + `vibe_candidates.py` | Vibe `agent/src/skills/strategy-generate/SKILL.md` 的需求提取流程 | 已实现 `HybridCandidateGenerator` 和严格 JSON Schema；规则解析器仍作快路，只有 allowlist miss 才调用 bounded provider | 一个或多个 `CandidateAst`，随后仍经 Catalog、Schema、股票和时间门禁 | Vibe 生成并执行 `signal_engine.py`；任意 Python/SQL；缺参时模型静默默认 | `implemented / fixture-tested / Live-unverified`；transport/bootstrap 路径已配置，旧公网冒烟不证明当前 revision |
| 纯观点：`vibe_ideas.py` + `idea_routing.py` | Vibe 的需求模糊时给 2—3 个方向，以及 hypothesis/research workflow | 模型只复述观点、写待检验假设并选择 2—3 个服务端固定模板 ID；资产只取当前权威 A 股页 symbol | 非执行 `idea-route.v1`；用户选中后生成服务端模板原话并重新走原编译器 | 模型另选股票、自由写规则/参数、生成政治主题事件、自动选择收益最好候选、直接创建 StrategySpec/run | `implemented / fixture-tested`；本轮没有 Live 证据 |
| 长尾表达的结构化安全 | Vibe `agent/src/shadow_account/codegen.py`、`agent/backtest/runner.py` | 只借 literal-safe、AST 负例和 shape-check 测试方法；主方案仍是受限 JSON DSL | 安全负例语料和 provider contract tests | 把 AST denylist 当完整沙箱；把生成 Python 当正式策略表示 | `reference_only` |
| 采集源选择 | Vibe `agent/backtest/loaders/registry.py` | 复用 registry/fallback 设计，改成“采集任务内选择并记录来源” | 一个显式 source result，进入本项目 snapshot publisher | 回测执行时联网 fallback；某源失败后不留证据地换源 | `planned` |
| 通达信日线采集：`vibe_mootdx.py` | Vibe `agent/backtest/loaders/mootdx_loader.py` | 已按固定 commit 重写最小 `get_k_data` Protocol；要求权威 session 列表，lots 转 shares，记录 canonical-frame/raw-byte hash 语义 | 规范 SH/SZ 股票、日线 OHLCV、查询身份和精确 coverage evidence；仍不是已发布 snapshot | 直接把 DataFrame 当正式快照；北交所默默返回空；把 canonical-frame hash 冒充 wire hash | `implemented / fixture-tested / Live-unverified`；真实 client 未配置且法务阻断 |
| 腾讯/东财/BaoStock/Tushare 日线 fallback | Vibe 对应 loader | 当前本地 adapter 与固定上游实现做差分；只有缺失的通用分页/重试逻辑才带出处移植 | 同一 canonical acquisition batch | 复制整套跨市场 registry；把供应商复权价直接记到账户 | `planned_differential` |
| 东方财富 Push2 日线：`eastmoney_daily.py` | AKShare `stock_zh_a_hist` | 按固定 commit 适配完整请求参数；本地增加 A 股身份、原始响应 hash、单位/区间门禁和独立 session/公司行动交叉校验 | 采集 batch，发布后运行期离线 | 把公共未文档化接口当生产授权；漏掉 `ut/f116`；把返回 K 线当完整交易状态 | `implemented / fixture-tested / Live-unverified`；当前网络探针被远端断开 |
| 标准指标：`indicators.py` + `moss_pandas_backend.py` | MOSS `scripts/core/indicators.py` | adapter 直接调用同款 pandas `ewm`，A 股薄层只增加 warm-up 和 MACD 双倍柱；EMA/MACD 在 `1e-12` 容差内对拍，RSI 保留为差异 oracle 并禁止接管 | 带上游 commit 的序列和逐指标采用白名单 | 再手写一套递推；整体替换；把 MOSS RSI 的不同 seed 当作等价；MOSS `ichimoku` 的负向 shift | `implemented / fixture-tested / EMA+MACD-adoptable / RSI-incompatible / runtime-default-unmodified` |
| 指标/数据一致性 | MOSS `replay_baseline.py`、`data_cache_archive.py` 和 fingerprint 测试 | 复用固定数据、参数和算法身份一起对账的方法；适配为本项目 manifest 测试 | provider revision、input fingerprint、output fingerprint 和差异报告 | 用 MOSS Hyperliquid 数据身份替代 Composite snapshot | `planned_test_method` |
| 稳健性报告：`ashare_lab/application/robustness.py` | Vibe `agent/backtest/validation.py` | 作为独立 `ValidationProvider` 接 Monte Carlo、bootstrap、walk-forward；固定 seed 和定义版本 | 附属研究报告，不反写成交和主结果 | 自动挑最好参数；把重排交易顺序的 p-value 包装成策略有效性结论 | `planned` |
| A 股撮合与账本 | 本项目 `domain/execution`、`orders`、`portfolio` 和 `daily_backtest.py` | 保留第一方权威；第三方只能做黄金样例对照 | 可审计订单、成交、未成交、现金和持仓轨迹 | Vibe `china_a.py`、MOSS `engine.py/backtest.py` 接管正式结果 | `retain_first_party` |
| 事件、公司行动、运行身份 | 本项目 Event/Composite snapshot、company-action timeline、RunManifest/result hash | 全部保留；开源只可提供底层解析库或独立 oracle | 点时证据、覆盖、revision/quarantine、公司行动账本、可重放身份 | 供应商“当前值”回填历史；运行中搜索；第三方结果 hash 替代本项目 identity | `retain_first_party` |
| legacy `astock_backtest/` | 本仓库旧引擎 | 只作 characterization/differential oracle，仍禁止新主链直接 import | 独立差异报告 | 为减少代码行数把两套领域模型重新耦合 | `oracle_only` |

## 5. 为什么不能整体替换

### 5.1 Vibe-Trading

Vibe 的 A 股 loader registry 很适合采集层，但它的 `ChinaAEngine` 通过代码前缀估计板块涨跌停，
源码也明确无法只凭代码识别 ST；当历史涨跌停基准不可得时，其 helper 会返回“未阻断”。这与本项目
“未知就 fail closed”的正式口径冲突。因此 loader 和统计模块可以复用，撮合结果不能直接继承。

Vibe 的自然语言流程会生成 `signal_engine.py`。其 runner 有较完整的 AST 防御，但源码也把它描述为
defense-in-depth 而非内核级保证。本项目面向普通用户，正式链必须只接收 `CandidateAst -> StrategySpec`，
不能让模型生成的任意 Python 在 API/worker 进程中运行。

### 5.2 MOSS

MOSS 的主要运行语义是 Hyperliquid、15 分钟固定数据、双向永续、杠杆、保证金、资金费和爆仓；
这些都不属于当前 A 股单股、只做多、日线研究回测。可移植部分限于纯指标、固定数据指纹和回放一致性
方法。`decision.py` 的综合信号权重也不能成为用户未说出的默认策略。

MOSS `indicators.py` 的 `ichimoku()` 含 `close.shift(-kijun)`。该输出若进入历史交易信号会读取未来值，
因此不在首批指标 allowlist 中；任何其他指标也必须先过前缀不变性测试，不能因“开源已有”而豁免。
同一文件的 ATR/ADX 使用 `ewm(span=period)`，与本项目冻结的 Wilder `alpha=1/period`、算术种子和递推口径
不同；Supertrend 的初值和上下带收缩也没有独立黄金向量。因此这些实现不接管运行时。Stochastic、
Williams %R、Donchian 仅作公式候选；尤其 Donchian 的当前柱是否进入通道会直接改变突破信号，必须按本项目
“只看此前 N 根”口径单独验证。

`bukosabino/ta` 的审计对象是 2023 PyPI `ta==0.11.0` sdist（固定 SHA-256），另行查看的
`a8904107` 是 2026 master 源码，只能说明当前公式形状，不能冒充该旧包的 source commit。当前没有安装或
执行任一版本。其 ATR 在
`window-1` 位置用包含首行的 true range 初始化，而本项目把首根 TR 记为未知并从第 1..N 根种下 Wilder
ATR；其 Donchian 默认包含当前柱。未来若增加差分测试，必须显式做时间对齐和前一柱 shift，关闭 `fillna`，
并处理 pandas 的 NaN/float 与本项目 Decimal、停牌零量过滤差异，不能把库默认输出直接当作信号。

## 6. 双跑门禁

### G0：来源与许可证

- `THIRD_PARTY.yml` 固定项目、commit、路径、采用方式和禁止范围；禁止只写浮动 branch/tag。
- 分发保留的 Vibe 派生/复制代码前保存其完整 MIT license artifact，并让打包流程带上；记录本地修改。
- MOSS 虽为 MIT-0，仍保存精确 license、commit 和修改历史。
- 每个新增传递依赖也要单独登记和审计。`mootdx 0.11.7` 及 PyPI artifact hash 已登记，但其标准 MIT
  文件允许商业使用，README 又写“不得用于任何商业目的”；在法务与底层 TDX 数据使用条款确认前，
  `VibeMootdxDailyResearchSource` 只能保留为未联网 adapter，不能加入运行依赖。
- `formula_reference`、`adapted_pattern`、`vendored_source` 和 `runtime_dependency` 必须分开标记；夹具通过
  不能改写成已经 Live。

### G1：架构隔离

- 第三方只能实现 `CandidateGenerator`、现有 `IndicatorBackend`、采集 provider 或
  `ValidationProvider`；不得从 `ashare_lab/domain` import 第三方类型。
- 适配器失败必须返回结构化错误；禁止静默切回另一套定义。
- 每个 provider 有独立开关和旧实现回滚路径；新 provider 不得改变既有 run。

### G2：自然语言双跑

- 用固定中文 corpus 同时运行当前 parser 和候选 provider。
- 已支持表达必须得到语义等价的 `CandidateAst`：标的、买入、卖出、逻辑组合、参数、时间区间均比较。
- 有意义但不构成策略的观点必须进入 `idea-route.v1`，得到理解、待检验假设与 2—3 个固定模板候选；选择前不得产生 StrategySpec、hash 或 run。
- 观点候选只使用当前权威 A 股页 symbol；缺股票时澄清，不能由模型猜股票。没有可靠政治/主题事件快照时只允许价格行为代理，不得生成事件因果回测。
- 用户一次选择后必须重新运行当前 parser/candidate provider，并重新通过全部硬门禁；引导对象不能直接转成正式策略。
- 矛盾、缺关键退出条件和非 A 股输入必须在理解后 fail closed 或给出诚实改写；只允许一次集中澄清。空输入和协议错误保持 invalid。
- 模型输出先做 Schema/Catalog/股票/时间校验；模型置信度不能绕过硬门禁。
- 模型 transport 暂不可用是可重试服务故障，不得降级成“用户的话无法理解”。
- 正式 worker 不读取、不生成、不 import `signal_engine.py`。

### G3：指标黄金样本

- 每个指标单独固定 `definition_version`、公式、warm-up、NaN、舍入与容差；没有全局模糊容差。
- 覆盖普通行情、长停牌/零成交量、复权跳点、常数序列、极短序列和参数边界。
- 对每个历史前缀重算，既有位置结果不得随未来 bars 改变。
- MOSS/Vibe、当前实现及独立成熟库至少两方对拍；差异必须解释并固化为版本，不得取“看起来最好”的实现。
- 只有某一指标通过，才允许只迁移该指标；其余继续走当前透明实现。

### G4：行情采集与快照

- 同一规范 A 股、区间、价格口径下对拍交易日键、OHLC、成交量/额、停牌和前收盘。
- 明确供应商原始 volume 单位；进入本项目前统一为 shares，并保存原始单位与转换版本。
- 请求结束日超过来源实际覆盖时不得宣称完整 coverage；分页不足、北交所不支持或代码/后缀冲突均 fail closed。
- fallback 只发生在 snapshot acquisition：记录每次尝试、失败原因、最终源和 raw/query hash。
- 发布后回测断网运行；相同 Composite snapshot、算法和配置必须重放为相同结果 hash。

### G5：稳健性统计

- 固定 seed、样本生成方式、年化因子、交易定义和结果 Schema。
- 与 Vibe 固定 commit 对拍 Monte Carlo、bootstrap 和 walk-forward 输出；差异进入算法版本。
- 统计结果是附属研究证据，不得改变订单或权益曲线，也不得自动选参；它是否进入结果 bundle 和
  `resultHash` 必须由显式 Schema/算法版本决定，不能静默漂移。

### G6：A 股执行不回归

- T+1、历史 ST/板块涨跌停、停牌、一字板、整手买入、非整手全部卖出、费用和公司行动黄金样例全绿。
- 事件首次可得时间、revision、撤回、quarantine 与 acquisition coverage 不因第三方接入而弱化。
- 技术策略和事件策略都在固定不可变快照上重放；不能用目录数量、Mock 或第三方演示代替 Live 证据。

### G7：替换与回滚

- manifest 写入 provider id/version、上游 commit、本地 adapter version 和输入 snapshot identity。
- 先影子双跑，差异为零或属于已批准的算法版本变化后，再切默认 provider。
- 至少保留一个发布周期的旧 provider 和结果读取能力；回滚不改写历史 run。
- 删除旧实现前，代码搜索证明无调用方，characterization tests 已迁移，许可证和 provenance 仍保留。

## 7. 首批实施顺序

1. 在当前 clean revision 复验已配置的 `VibeBoundedCandidateGenerator` transport/bootstrap，补齐超时、
   持久化 provenance 与真实 API/H5 证据；仍只产出 `CandidateAst`，旧 provider 公网冒烟不作为当前结论。
2. 对 `idea-route.v1` 验收“任意有意义输入先被理解 → 观点/假设 → 当前股票价格代理 → 2—3 个固定候选
   → 用户一次选择 → 原编译器重编译”。以“我讨厌特朗普”为负例证明不会生成政治事件、换股或因果结论。
3. 先解决 `mootdx` 许可证/README 与底层行情条款冲突；只有获得允许后才注入真实 client。真实取数仍须
   进入本项目 snapshot publisher、通过 session/公司行动/双价格门禁，不把 fallback 带入运行期。若不能
   获得允许，adapter 保留为研究对照，正式链继续使用 Choice/东方财富等已批准来源。
4. 当前第一方运行时已新增 ATR/NATR、ADX/DMI、BIAS、ROC、Momentum、Stochastic、Williams %R、
   Donchian、收益率标准差、历史波动率和 True Range；黄金向量与前缀不变性已由本项目测试固定。MOSS 与
   `bukosabino/ta` 仍只是差分候选，尚未形成已执行的第三方 oracle 证据。KDJ 也不能与 Stochastic K/D
   视为同一定义；任何单项替换仍需先通过对齐、停牌、NaN 和参数边界门禁。
5. 把 Vibe validation 作为可关闭的附属报告 provider 接入，先固定 seed 和算法版本。
6. 在 clean integration revision 上重新执行技术策略、事件策略、真实 H5 和结果身份门禁，再决定删除哪些
   重复实现。

这份顺序刻意先接窄端口、后删旧代码：复用失败时可以退回当前实现，且不会把第三方执行语义带进已经
固定的 A 股结果。
