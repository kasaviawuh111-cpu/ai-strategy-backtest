# 指标与数据源映射

本文用于确定自然语言策略可使用的数据、计算口径和历史可得时间。原则是：能由固定输入和公开公式稳定复算的指标自行计算；供应商模型只保存原始序列；无法说明公式与时间口径的包装能力不仿造。

## 1. 已接入的透明日线定义

当前发布 35 个 `stable` 定义。它们都由固定日线输入复算，固化参数、预热、初始化、缺失值和百分数单位；只使用当时已经获得的数据。信号在日线收盘后确认，最早在下一可交易日执行。A 股停牌或其他零成交量日统一视为非交易观察：不推进任何指标窗口、不产生信号，输出仍按原始日期序列对齐。

| ID | 用户能力 | 固定口径 |
| --- | --- | --- |
| `technical.ma` | 价格突破/跌破 N 日均线 | 包含当日的 N 日简单平均；交叉比较相邻两个完整交易日 |
| `technical.ma_cross` | 短期均线上穿/下穿或高于/低于长期均线 | 两条简单均线，且 `fast_period < slow_period`；交叉与持续位置是不同触发器 |
| `technical.ema` | 价格上穿/下穿 EMA | `α=2/(N+1)`，首值递归初始化，达到 N 根后才暴露 |
| `technical.ema_bias` | EMA 乖离率 | `100 × (price / EMA - 1)`，数值 `5` 表示 5% |
| `technical.macd` | MACD 金叉、死叉、零轴交叉 | 固定快/慢/信号参数、首值初始化和稳定预热；柱值为 `2 × (DIF-DEA)` |
| `technical.rsi` | RSI 阈值与交叉 | Wilder RSI；默认 14 日，平坦窗口为 50 |
| `technical.kdj` | KDJ 金叉、死叉、J 值阈值 | 默认 9/3/3；K/D 初值 50，零振幅窗口 RSV=50 |
| `technical.bollinger` | 价格穿越上/中/下轨 | 默认 20 日、2 倍总体标准差（`ddof=0`） |
| `technical.cci` | CCI 阈值与交叉 | 默认 14 日，典型价与总体平均绝对偏差，常数 0.015 |
| `technical.bbi` | 价格穿越 BBI | `mean(MA3, MA6, MA12, MA24)`；四个周期必须严格递增并进入策略身份 |
| `price.return_pct` | N 日涨跌幅 | `100 × (price_t / price_(t-N) - 1)`，默认 5 日 |
| `price.rolling_high` | 股价创 N 日新高 | 严格高于此前 N 个完整交易日最高价；当天不进入比较窗口，相等不触发 |
| `price.consecutive_up` | 连续上涨至少 N 天 | 收盘价逐日严格上涨；平盘或下跌中断，默认 3 天 |
| `price.amplitude` | 日振幅阈值与交叉 | `100 × (high-low) / previous_close` |
| `market.volume` | 成交量相对基准均量 | 当前成交量与包含当日在内的 N 日均量比较，倍数与周期均进入策略 |
| `market.amount` | 单日成交额阈值与交叉 | 直接使用快照中的不复权成交额，单位人民币元 |
| `amount.average` | N 日平均成交额阈值与交叉 | 包含当日的 N 日成交额简单平均，单位人民币元 |
| `volume.relative` | 放量、缩量、持续放量 | 当日量除以前 N 个正成交量交易日均量，基线排除当日；零量日跳过 |
| `volume.price_confirmation` | 放量上涨、放量下跌 | RVOL 与相对前一有效交易日的收盘涨跌幅同时达到阈值 |
| `technical.obv` | OBV 上升或下降 | 按有效交易日收盘涨跌累加/扣减成交量；零量日不触发 |
| `volume.price_divergence` | 量价顶背离、底背离 | 价格枢轴等待右侧日线确认，并要求 OBV 反向变化达到历史均量阈值；信号只记确认日 |
| `technical.trend_regime` | 阶段上涨、下跌、震荡 | SMA20/SMA60、短均线斜率、Wilder ADX/+DI/-DI 与连续确认共同判定 |
| `price.true_range` | 真实波幅阈值与交叉 | 首根因没有前收盘记为未知；其后为 `max(high-low, abs(high-prev_close), abs(low-prev_close))` |
| `technical.atr` | ATR 阈值与交叉 | TR 第 1..N 根算术均值作种子，之后按 Wilder `ATR_t=(ATR_(t-1)×(N-1)+TR_t)/N`；不是 `ewm(span=N)` |
| `technical.natr` | 归一化 ATR 阈值与交叉 | `100 × WilderATR / close`，数值 `5` 表示 5% |
| `technical.adx` | ADX 趋势强度阈值与交叉 | Wilder TR、+DM、-DM 平滑；首个 ADX 为连续 N 个 DX 的算术均值，之后按 `alpha=1/N` 递推 |
| `technical.dmi` | +DI 与 -DI 的交叉或高低关系 | 与 ADX 共用同一套 Wilder 平滑 TR/+DM/-DM；只有方向比较，不引入隐含 ADX 阈值 |
| `technical.bias` | N 日简单乖离率阈值与交叉 | `100 × (price / SMA_N - 1)`，默认收盘价、20 日 |
| `technical.roc` | N 日价格变动率阈值与交叉 | `100 × (price_t / price_(t-N) - 1)`，默认 12 日 |
| `technical.momentum` | N 日绝对动量阈值与交叉 | `price_t - price_(t-N)`，保留价格单位，不冒充百分比 |
| `technical.stochastic` | Stochastic K/D 交叉或 K 值阈值 | `%K=100×(close-LL_N)/(HH_N-LL_N)`；D 为 K 的 M 日简单均值；平坦窗口 K/D=50，K 与 D 都完成预热后才暴露 |
| `technical.williams_r` | Williams %R 阈值与交叉 | `-100×(HH_N-close)/(HH_N-LL_N)`，范围 `[-100,0]`；平坦窗口固定为 -50 |
| `technical.donchian` | 收盘突破/跌破唐奇安通道 | 上下轨只取此前 N 个完整交易日高低点，不含当前柱；中轨为上下轨均值，避免用当日高低点阻断或伪造突破 |
| `technical.return_stddev` | 日收益率标准差阈值与交叉 | 最近 N 个简单日收益率百分数的总体标准差，固定 `ddof=0` |
| `technical.historical_volatility` | 年化历史波动率阈值与交叉 | 最近 N 个对数日收益率的总体标准差 × `sqrt(annualization_sessions)` × 100；默认年化交易日 252 |

技术计算可用 [TA-Lib Python](https://github.com/TA-Lib/ta-lib-python) 作为交叉校验实现，但项目自身仍保存公式版本和黄金样例，不能只保存库名。BBI 尤其不能假设行业参数统一：开源实现中 [stock-pandas](https://github.com/kaelzhang/stock-pandas) 使用 3/6/12/24，而 [MyTT](https://github.com/mpquant/Python-Financial-Technical-Indicators-Pandas/blob/main/MyTT.py) 使用 3/6/12/20。

本批波动、动量和通道指标另外审阅了 2023 PyPI `ta==0.11.0` sdist（SHA-256 `de86af43418420bd6b088a2ea9b95483071bf453c522a8441bc2f12bcf8493fd`）、另行查看了 2026 master [`a8904107`](https://github.com/bukosabino/ta/tree/a890410710a6e483c9ba08da7f3dd5089e4b9dff)，并审阅 [MOSS@1fce09b0](https://github.com/moss-site/moss-trade-bot-skills/tree/1fce09b03151a01ba1dee230466ec64cdd12fda8)（MIT-0）。`a8904107` 不是 `ta==0.11.0` 的 source commit，两份证据不能合并声称。它们都不是运行依赖或权威结果：`ta` 的 ATR 首值、Donchian 当前柱和 pandas NaN/fill 语义需要显式对齐；MOSS ATR/ADX 的 `ewm(span=N)` 不是本项目 Wilder 口径，Ichimoku 的负向 shift 会泄漏未来 close，Supertrend 初值也未通过独立黄金向量。本项目以 Decimal 公式、Catalog 版本、黄金数值、动态预热、参数边界、停牌过滤和逐前缀不变性测试共同定义指标身份。

股价区间位置、连跌天数、盘中“量比”、换手率等虽可透明计算或取得供应商原始序列，但本轮尚未发布成可执行 ID；盘中量比需要分钟级同期成交量，换手率还需要点时可流通股本。`volume.relative` 是日线 RVOL，不冒充盘中量比。笼统的“K 线形态”总开关也不接入，后续必须把每个形态拆成独立、可测试的定义。

## 2. 供应商原样序列

下列数据不是仅靠 OHLCV 就能还原。Demo 可优先接 Tushare；使用 Choice 时以账号内“命令生成器”返回的正式指标代码为准。所有供应商序列都保存请求参数、原始响应、供应商、口径版本、抓取时间和内容哈希。

| 数据能力 | Tushare 接口与关键字段 | 回测中的最早可用时间 |
| --- | --- | --- |
| 市值、股本、估值、股息率、日换手 | [`daily_basic`](https://tushare.pro/document/2?doc_id=32)：`turnover_rate`、`volume_ratio`、`pe_ttm`、`pb`、`ps_ttm`、`dv_ttm`、`total_share`、`float_share`、`total_mv`、`circ_mv` | 官方为盘后更新；无可靠到秒记录时按下一交易日可用 |
| 利润、收入、费用 | [`income`](https://tushare.pro/document/2?doc_id=33)：`ann_date`、`f_ann_date`、`end_date`、`total_revenue`、`revenue`、`sell_exp`、`n_income_attr_p`、`rd_exp`、`update_flag` | 以 `ann_date/f_ann_date` 控制可得性；只有日期时按当日收盘后处理 |
| 财务比率与增长 | [`fina_indicator`](https://tushare.pro/document/2?doc_id=79)：`eps`、`bps`、`ocfps`、`profit_dedt`、`grossprofit_margin`、`netprofit_margin`、`saleexp_to_gr`、`salescash_to_or`、`debt_to_assets`、`roe`、`netprofit_yoy`、`dt_netprofit_yoy`、`or_yoy`、`rd_exp`、`update_flag` | 同上；不得用报告期末 `end_date` 提前暴露结果 |
| 经营现金流与销售回款 | [`cashflow`](https://tushare.pro/document/2?doc_id=44)：`ann_date`、`f_ann_date`、`end_date`、`n_cashflow_act`、`c_fr_sale_sg`、`update_flag` | 同上；派生“销售收现率”等指标时同时冻结分子分母口径 |
| 业绩预告 | [`forecast`](https://tushare.pro/document/2?doc_id=45)：`ann_date`、`end_date`、`type`、`p_change_min/max`、`net_profit_min/max`、`summary`、`change_reason` | 以公告首次可得时间为准；只有日期时按收盘后处理 |
| 东方财富资金流口径 | [`moneyflow_dc`](https://tushare.pro/document/2?doc_id=349)：`net_amount`、`net_amount_rate`、`buy_elg/lg/md/sm_amount` 及相应占比；历史自 2023-09-11 起 | 盘后序列，无可靠到秒记录时下一交易日可用 |
| Tushare L2 主动买卖口径 | [`moneyflow`](https://tushare.pro/document/2?doc_id=170)：`buy/sell_sm/md/lg/elg_vol/amount`、`net_mf_vol`、`net_mf_amount` | 盘后序列，无可靠到秒记录时下一交易日可用 |
| 筹码分布摘要 | [`cyq_perf`](https://tushare.pro/document/2?doc_id=293)：`cost_5pct/15pct/50pct/85pct/95pct`、`weight_avg`、`winner_rate` | 官方说明约 18:00–19:00 更新；下一交易日可用 |
| 筹码价格分布 | [`cyq_chips`](https://tushare.pro/document/2?doc_id=294)：`price`、`percent` | 同上；只作为供应商模型结果使用 |
| 龙虎榜股票汇总 | [`top_list`](https://tushare.pro/document/2?doc_id=106)：`l_buy`、`l_sell`、`l_amount`、`net_amount`、`net_rate`、`amount_rate`、`float_values`、`reason` | 官方说明约 20:00 更新；下一交易日可用 |
| 龙虎榜席位明细 | [`top_inst`](https://tushare.pro/document/2?doc_id=107)：`exalter`、`side`、`buy`、`buy_rate`、`sell`、`sell_rate`、`net_buy`、`reason` | 同上；“机构”称谓只按原始席位标识，不自行扩义 |
| 股东及高管增减持 | [`stk_holdertrade`](https://tushare.pro/document/2?doc_id=175)：`ann_date`、`holder_name`、`holder_type`、`trade_type`、变动股数和比例 | 以 `ann_date` 或更精确的公告可得时间为准 |
| 公募基金持仓 | [`fund_portfolio`](https://tushare.pro/document/2?doc_id=121)：`ann_date`、`end_date`、`symbol`、`mkv`、`amount`、`stk_mkv_ratio`、`stk_float_ratio` | 以披露日 `ann_date` 为准，不以季度末日期回填 |
| 券商盈利预测、评级和目标价 | [`report_rc`](https://tushare.pro/document/2?doc_id=292)：`report_date`、`org_name`、`quarter`、`np`、`eps`、`pe`、`rd`、`roe`、`rating`、`max_price`、`min_price`、`create_time` | 优先使用可信的 `create_time`；否则按 `report_date` 收盘后处理 |
| 机构调研 | [`stk_surv`](https://tushare.pro/document/2?doc_id=275)：`surv_date`、`fund_visitors`、`rece_org`、`org_type`、`content` | 以公告/记录首次可得时间为准，不以调研发生日自动回填 |

Tushare 也提供技术因子序列：[`stk_factor`](https://tushare.pro/document/2?doc_id=296) 和 [`stk_factor_pro`](https://tushare.pro/document/2?doc_id=328)。这些可用于供应商对照测试；正式信号仍优先使用第 1 节的透明实现，避免复权、初始化和 BBI 参数差异悄悄改变结果。

Choice 的 Python 接口以 [`css`、`csd`、`ctr`、`cses`、`cfc`](https://quantapi.eastmoney.com/Upload/EMQuantAPI_Python.pdf) 为主。公开文档不能替代账号内指标手册，因此接入流程必须是：命令生成器选择指标 → `cfc` 校验代码、指标和参数 → 保存生成命令及原始响应 → 用固定样例核对。不得凭字段中文名猜 Choice 指标代码。

东方财富网页数据可用于发现和人工核对，但隐藏的 `push2` 字段不是稳定、公开的数据合同；没有授权合同和快照审计时，不作为正式回测输入。

## 3. 不能仿造的包装项

以下名称从截图本身无法还原唯一算法，且不同产品很可能包含私有打分、样本筛选或人工规则。当前不做同名仿制：

- 策略精选、估值擒龙、趋势策略、价值策略、业绩超预期（成品评分）。
- 资金流入榜单、各路资金抢筹、主力介入强度、主力流入等未标明供应商口径的包装项。
- 扫雷、事件驱动、消息观测台等未给出事件全集、分类规则和可得时间的组合能力。
- 蓝粉彩带、中期彩带、紧贴彩带、四合一、底部出击、操盘提醒。
- 彼得林奇成长指标 0–100、机构新进/连续增持等无法由截图确定样本范围和归一化方式的评分。
- 筹码集中度、平均持仓成本、获利盘比例等若没有供应商序列，只凭日线自行拟合的“同名结果”。

若供应商后续能提供精确公式、参数、源字段、历史可得时间、修订规则和使用授权，可把它作为“供应商原样序列”接入；仍不声称项目自行复算出了相同算法。

可另建透明、不同名的能力，例如把“业绩超预期”拆成“实际净利润相对前一日可得一致预期的偏离率”。前提是存在可回放的一致预期历史快照，不能拿今天的预测覆盖历史。

## 4. 数据口径门禁

1. **资金流两套口径绝不拼接。** `moneyflow_dc` 与 `moneyflow` 的大中小单定义、计算方法和覆盖起点不同。一个策略运行必须固定 `vendor + dataset + methodology_version`；不得用前者覆盖 2023-09-11 以后、后者补更早历史，再把结果叫成同一条“主力净额”。
2. **财务数据只按当时披露时间进入。** `end_date` 是报告期，不是投资者可得时间。使用 `ann_date/f_ann_date` 或更精确的公告首次可得时间；日期级信息保守按当日收盘后、下一可交易日执行。修订版本连同 `update_flag`、抓取时间和原始哈希一并保留，禁止把最新版回填到旧日期。
3. **筹码是供应商模型，不是真实账户持仓。** `cyq_perf/cyq_chips` 可保存并使用，但界面和报告必须标注模型来源、版本和覆盖期；不得解释为真实股东成本，也不得宣称本地算法与供应商完全一致。
4. **BBI 必须参数化。** 策略、缓存键、结果页和活动轨迹均显示 `BBI(M1,M2,M3,M4)`；参数不同就是不同信号，禁止只存“BBI”。
5. **盘后数据不在当日收盘前可见。** 若供应商只有“每日更新”或一个日期，没有可信的首次可得时分秒，统一按当日收盘后处理，最早下一交易日成交。
6. **Choice 字段先验证再入库。** 每个字段用账号内命令生成器和 `cfc` 校验，固定函数、代码、参数、单位和频率；校验失败就标为不可用，不用相似字段顶替。
7. **机构标签不能扩大含义。** `fund_portfolio` 仅代表已披露的公募基金持仓；龙虎榜“机构席位”、高管增持、基金持仓不能合并包装成全市场“机构持仓”。
8. **一致预期必须是点时快照。** `report_rc` 等研究数据只有在保存当时可见的机构样本、发布时间和聚合版本后，才可计算历史一致预期；今天重算出的共识不能用于过去信号。
9. **供应商技术值不覆盖本地真值。** MACD、RSI、KDJ、BOLL、CCI、EMA 等以透明实现为主；供应商序列只做对照。出现差异时先检查复权、参数、初始化、预热和缺失值规则，不能静默替换。
10. **数据身份进入回测快照。** 每次运行保存数据源、接口、参数、可得时间规则、原始响应哈希、清洗版本和内容地址；无法重放的序列不能进入正式报告。
11. **用户明说的方向和参数不能被默认值覆盖。** `DIF/DEA`、K/D、零轴、涨幅/跌幅、交叉/持续高低关系分别解析；`MACD(6,13,5)`、`RSI6`、`KDJ(14,3,3)`进入策略身份。否定或不完整表达若无法精确表示，直接返回不支持，不反向猜测。
12. **预热不能静默截断。** 动态预热按实际参数和触发器计算；若保守换算后超过 3650 个自然日快照上限，提交时明确拒绝并要求缩短参数，而不是缺少历史仍继续回测。
13. **停牌日不能改变技术窗口。** 35 个 stable 指标在统一运行时入口排除零成交量观察，再把事实映射回原日期；停牌槽位固定为无信号。不得让旧指标把停牌价计入均线、动量或成交额窗口，而新指标却跳过。
14. **组合条件使用可审计的三值逻辑。** `任一满足` 中已有条件为真时立即为真，不被另一个仍在预热的条件压成未知；`全部满足` 中已有条件为假时立即为假。其余含未知的组合继续等待，避免多退出条件漏卖出。
