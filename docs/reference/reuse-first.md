# 开源复用与胶水代码边界

本文把“优先拼好码、少造轮子”固化为项目工程规则。它参考
[tradecatlabs/vibe-coding-cn](https://github.com/tradecatlabs/vibe-coding-cn) 的
[拼好码方法论](https://github.com/tradecatlabs/vibe-coding-cn/blob/develop/docs/concepts/glue-coding.md)：
成熟能力解决通用问题，胶水代码只做连接、转换、隔离和业务编排，自研只服务不可替代的业务差异。

这不是“发现一个大仓库就整体换引擎”。每项复用都必须先核对维护状态、许可证、商业限制、
口径差异、可替换性和双跑验收；第三方类型不得直接渗透进核心领域模型。

## 1. 当前已经在复用的成熟能力

| 能力 | 当前依赖 | 本项目只负责 |
|---|---|---|
| HTTP/API 与数据校验 | FastAPI、Pydantic、httpx | A 股策略与数据契约、错误语义 |
| 数据计算与不可变文件 | NumPy、Pandas、PyArrow、DuckDB | 点时校验、内容寻址、快照发布与读取 |
| 持久化与迁移 | SQLAlchemy、Alembic、PostgreSQL/SQLite | RunManifest、任务和结果映射 |
| 任务执行 | Redis、RQ、线程队列 | 运行身份、幂等、进度和取消语义 |
| H5 工程 | React、TanStack Query、Vite、Vitest、Playwright | 策略确认、因果轨迹与响应式产品交互 |

这些适配器应保持短、薄、可删、可替换。新增通用基础设施前，先证明现有成熟依赖不能满足。

## 2. 已落地与下一批复用候选

| 通用能力 | 优先候选 | 许可证/边界 | 接入方式与验收 | 当前状态 |
|---|---|---|---|---|
| 长尾自然语言候选 | [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) | MIT；只借需求提取工作流，不执行其生成的 Python，不允许模型替换宿主股票或绕过 Catalog | `VibeBoundedCandidateGenerator` 输出严格 JSON CandidateAst，确定性解析器仍为快路；正式 transport 接通前保持 fail closed | `implemented / fixture-tested / Live-unverified`；transport 未配置 |
| 受限自然语言 DSL | [Lark](https://github.com/lark-parser/lark) | MIT；只接管括号、比较、逻辑组合和参数结构，中文同义词、事件生命周期和缺参澄清仍由领域层处理 | 先建立 EBNF/LALR adapter，与现有规则解析器双跑并比较同一输入的 AST；未达成语义等价前不替换 | `candidate / unimplemented` |
| 标准技术指标 | [MOSS](https://github.com/moss-site/moss-trade-bot-skills) + [TA-Lib Python](https://github.com/TA-Lib/ta-lib-python) | MOSS MIT-0、TA-Lib BSD-2-Clause；国内 MACD 柱、KDJ 初始化、停牌与 NaN 口径仍需适配 | `IndicatorBackend` 与 MOSS/pandas adapter 已建立；EMA/MACD 通过显式容差和前缀门禁，RSI 因 seed/零损失语义不同排除；TA-Lib 仍作下一独立 oracle | `implemented / fixture-tested / Live-unverified`；尚非运行时默认 |
| A 股日线采集候选 | [AKShare `stock_zh_a_hist`](https://github.com/akfamily/akshare/blob/8e95744b79ae22326308ccd2b4e62650c5b53c55/akshare/stock_feature/stock_hist_em.py) | MIT；只固定东方财富 Push2 请求协议，AKShare 不作为运行依赖，公共未文档化接口不等于生产授权 | 薄 adapter 输出候选 batch，再经过本项目 A 股身份、raw hash、单位、session、公司行动与 coverage 门禁；运行期禁止联网 | `implemented / fixture-tested / Live-unverified`；直连被远端断开，无真实 batch/snapshot |
| 交易日历基线 | [exchange_calendars](https://github.com/gerrymanoim/exchange_calendars) | Apache-2.0；只能做公开日历基线，不能替代逐股停牌和供应商交易状态 | 新建 `CalendarBackend`；与交易所/Choice `SessionReference` 交叉校验，冲突 fail closed | `decided / unimplemented` |
| 通用收益统计 | [empyrical-reloaded](https://github.com/stefan-jansen/empyrical-reloaded) | Apache-2.0；年化天数、无风险利率、日期对齐和 NaN 口径必须固定 | 先只作 Sharpe、波动率、最大回撤的差分 oracle；逐项定义一致后才决定是否接管计算 | `candidate / unimplemented` |
| 完整离线报告 | [QuantStats](https://github.com/ranaroussi/quantstats) | Apache-2.0；依赖较重，且不得让它自行下载行情 | 仅在未来需要独立 HTML 报告时评估，不进入当前回测核心 | `candidate / out-of-current-scope` |
| Parquet 数据契约 | 已有 Pydantic、PyArrow、DuckDB | MIT/Apache-2.0；内容 hash、路径安全、PIT、冲突决议仍属本项目 | 不新增第二套 Schema 真相：Pydantic 统一 manifest，PyArrow 比较物理 Schema，DuckDB 做行数、空值、范围和重复键校验 | `decided / incremental-refactor` |
| 收益与回撤图 | [Apache ECharts](https://github.com/apache/echarts) | Apache-2.0；时区、点位身份、键盘与辅助列表必须由产品层锁定 | 用 `echarts/core` 按需导入，小范围与现有 SVG 做视觉和交互双跑；不引入额外 React wrapper | `candidate / unimplemented` |
| 异步任务 | 已有 [RQ](https://github.com/rq/rq) | BSD-2-Clause；PostgreSQL 仍是业务真相，重试不得造成重复运行 | 保留 RQ，不迁移 Celery；后续补稳定 `job_id`、基础设施错误的有界 Retry、registry 对账和取消映射 | `decided / incremental-hardening` |
| 研究型因子/批量实验 | [Microsoft Qlib](https://github.com/microsoft/qlib) | MIT；当前单股 Demo 不引入整个平台 | 未来把不可变 snapshot 适配成 Dataset，只复用研究能力 | `candidate / out-of-current-scope` |
| 未来实盘网关/事件总线 | [VeighNa/vn.py](https://github.com/vnpy/vnpy) | MIT；当前不做真实下单 | 未来把受控订单映射到网关，不自造券商连接、RPC 或 WebSocket 框架 | `candidate / out-of-current-scope` |

迁移顺序固定为：先把已实现的 Vibe 候选 adapter 接入正式 transport 与影子审计，并让 MOSS 指标 adapter
按单指标逐项扩白名单；再收敛现有 Pydantic/PyArrow/DuckDB 和 RQ 用法、验证 ECharts，随后做 Lark、
TA-Lib 与 empyrical 的独立双跑。先加端口和适配器，再固定差异，最后才允许下线旧实现。
禁止为了减少代码行数直接替换已经验收的公式或执行语义。

## 3. 只参考、不直接并入主链

- [RQAlpha](https://github.com/ricequant/rqalpha) 的 A 股撮合设计可作对照，但其仓库明确限定非商业使用；未经单独授权不并入商业主链。
- [vectorbt](https://github.com/polakowo/vectorbt) 适合参数扫描，但许可证含 Commons Clause，且其向量化成交语义不等于严格 A 股事件回放。
- Backtrader、Zipline、LEAN 等完整引擎可作差分测试和行业参考；不让它们接管本项目的订单、公司行动、事件可得时间或 RunManifest。
- Qlib、vn.py 只在对应里程碑出现时通过 adapter 接入，不为当前 Demo 全量安装。

## 4. 必须由本项目掌控的差异

以下是产品真正的核心，不交给通用开源引擎自由决定：

- `source_released_at / vendor_first_available_at / available_at` 的历史可得时间；
- 事件原始证据、分页覆盖、hash、隔离和不可变 Event/Composite snapshot；
- A 股 T+1、停牌、涨跌停、一字板、集合竞价、申报数量、费用和公司行动；
- 信号确认、处理延迟、委托、成交/未成交和资金变化的连续因果轨迹；
- 自然语言进入受限 DSL 后的白名单、歧义门禁和一次集中澄清；
- RunManifest、数据/算法/配置/Git 身份和结果重放。

这里仍应尽量调用成熟库完成通用计算，但业务规则、证据和时点由本项目的领域层裁决。

## 5. 每个新能力的复用门禁

编码前必须回答：

```text
能力：
候选官方 API / 开源项目：
维护状态：
许可证及商业限制：
可直接复用的部分：
需要的薄 adapter：
与 A 股/点时口径的差异：
黄金样本与双跑方法：
替换或回滚路径：
若仍需自研，为什么成熟实现无法满足：
```

没有完成这张决策表，不新增通用算法。依赖版本、许可证、adapter、黄金样本和退出路径都要进入工程记录；
“测试通过”只证明当前代码路径，不等于外部数据、Live E2E 或生产授权已完成。
