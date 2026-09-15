"""Build the checked-in A-share capability coverage Catalog.

The lists below are the reviewable source.  The generated JSON is validated by
the domain model before it is written.  ``--check`` is suitable for CI.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ashare_lab.domain.catalog.coverage_models import CoverageCatalogRelease
from ashare_lab.domain.events.catalog import EXECUTABLE_EVENT_DEFINITIONS

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "catalogs/coverage/cn_a.v1.catalog.json"
SCHEMA_OUTPUT = ROOT / "contracts/catalog-coverage.v1.schema.json"


TECHNICAL: tuple[tuple[str, str], ...] = (
    ("technical.ma", "移动平均线"),
    ("technical.ema", "指数移动平均线"),
    ("technical.ma_cross", "双均线交叉"),
    ("technical.bbi", "多空指标 BBI"),
    ("technical.wma", "加权移动平均线"),
    ("technical.vwma", "成交量加权移动平均线"),
    ("technical.dema", "双重指数移动平均线"),
    ("technical.tema", "三重指数移动平均线"),
    ("technical.hma", "Hull 移动平均线"),
    ("technical.kama", "考夫曼自适应移动平均线"),
    ("technical.alma", "Arnaud Legoux 移动平均线"),
    ("technical.trima", "三角移动平均线"),
    ("technical.macd", "MACD"),
    ("technical.ppo", "价格百分比振荡器"),
    ("technical.pvo", "成交量百分比振荡器"),
    ("technical.trix", "三重指数平滑平均线"),
    ("technical.roc", "变动率 ROC"),
    ("technical.momentum", "动量 MOM"),
    ("technical.rsi", "相对强弱指标 RSI"),
    ("technical.stochastic", "随机指标 Stochastic"),
    ("technical.stoch_rsi", "随机 RSI"),
    ("technical.cci", "顺势指标 CCI"),
    ("technical.williams_r", "威廉指标 %R"),
    ("technical.ultimate_oscillator", "终极振荡器"),
    ("technical.awesome_oscillator", "动量震荡指标 AO"),
    ("technical.cmo", "钱德动量摆动指标"),
    ("technical.dpo", "去趋势价格振荡器"),
    ("technical.fisher_transform", "Fisher 变换"),
    ("technical.connors_rsi", "Connors RSI"),
    ("technical.kdj", "KDJ 随机指标"),
    ("technical.bias", "乖离率 BIAS"),
    ("technical.ema_bias", "EMA 乖离率"),
    ("technical.arbr", "人气意愿指标 ARBR"),
    ("technical.bollinger", "布林带"),
    ("technical.keltner", "肯特纳通道"),
    ("technical.donchian", "唐奇安通道"),
    ("technical.atr", "平均真实波幅 ATR"),
    ("technical.natr", "归一化 ATR"),
    ("technical.return_stddev", "收益率标准差"),
    ("technical.historical_volatility", "历史波动率"),
    ("technical.parkinson_volatility", "Parkinson 波动率"),
    ("technical.garman_klass_volatility", "Garman-Klass 波动率"),
    ("technical.adx", "平均趋向指数 ADX"),
    ("technical.dmi", "趋向指标 DMI"),
    ("technical.trend_regime", "阶段趋势"),
    ("technical.aroon", "Aroon 趋势指标"),
    ("technical.parabolic_sar", "抛物线转向 SAR"),
    ("technical.ichimoku", "一目均衡表"),
    ("technical.supertrend", "超级趋势"),
    ("technical.vortex", "Vortex 指标"),
    ("technical.mass_index", "梅斯线 Mass Index"),
    ("technical.choppiness", "震荡指数 Choppiness"),
    ("technical.fractal_dimension", "分形维数"),
    ("technical.obv", "能量潮 OBV"),
    ("technical.ad_line", "累积派发线 ADL"),
    ("technical.chaikin_money_flow", "蔡金资金流量 CMF"),
    ("technical.money_flow_index", "资金流量指标 MFI"),
    ("technical.force_index", "强力指数 Force Index"),
    ("technical.ease_of_movement", "简易波动指标 EMV"),
    ("technical.negative_volume_index", "负成交量指数 NVI"),
    ("technical.positive_volume_index", "正成交量指数 PVI"),
    ("technical.chaikin_oscillator", "蔡金振荡器"),
    ("technical.volume_profile", "成交量分布 Volume Profile"),
)

PRICE_ACTION: tuple[tuple[str, str], ...] = (
    ("price.close", "收盘价阈值"),
    ("price.return_pct", "区间涨跌幅"),
    ("price.log_return", "对数收益率"),
    ("price.amplitude", "振幅"),
    ("price.true_range", "真实波幅"),
    ("price.intraday_return", "日内收益"),
    ("price.close_location_value", "收盘位置值"),
    ("price.rolling_high", "滚动新高"),
    ("price.rolling_low", "滚动新低"),
    ("price.all_time_high", "历史新高"),
    ("price.all_time_low", "历史新低"),
    ("price.limit_state", "涨跌停状态"),
    ("price.one_price_limit", "一字涨跌停"),
    ("price.opening_gap", "开盘跳空缺口"),
    ("price.consecutive_up", "连续上涨"),
    ("price.consecutive_down", "连续下跌"),
    ("price.doji", "十字星"),
    ("price.hammer", "锤头线"),
    ("price.inverted_hammer", "倒锤头线"),
    ("price.shooting_star", "射击之星"),
    ("price.hanging_man", "上吊线"),
    ("price.bullish_engulfing", "看涨吞没"),
    ("price.bearish_engulfing", "看跌吞没"),
    ("price.bullish_harami", "看涨孕线"),
    ("price.bearish_harami", "看跌孕线"),
    ("price.morning_star", "早晨之星"),
    ("price.evening_star", "黄昏之星"),
    ("price.bullish_marubozu", "光头光脚阳线"),
    ("price.bearish_marubozu", "光头光脚阴线"),
    ("price.spinning_top", "纺锤线"),
    ("price.piercing_pattern", "刺透形态"),
    ("price.dark_cloud_cover", "乌云盖顶"),
    ("price.three_white_soldiers", "红三兵"),
    ("price.three_black_crows", "三只乌鸦"),
    ("price.inside_bar", "内包线"),
    ("price.outside_bar", "外包线"),
    ("price.nr4", "四日最窄波幅"),
    ("price.nr7", "七日最窄波幅"),
    ("price.wide_range_bar", "宽幅 K 线"),
)

VOLUME_LIQUIDITY: tuple[tuple[str, str], ...] = (
    ("market.volume", "成交量"),
    ("market.amount", "成交额"),
    ("market.turnover_rate", "换手率"),
    ("market.vwap", "成交量加权均价"),
    ("volume.average", "平均成交量"),
    ("amount.average", "平均成交额"),
    ("volume.relative", "相对成交量"),
    ("volume.zscore", "成交量 Z 分数"),
    ("volume.percentile", "成交量历史分位"),
    ("amount.percentile", "成交额历史分位"),
    ("volume.roc", "成交量变动率"),
    ("volume.rolling_high", "成交量滚动新高"),
    ("volume.dry_up", "成交量枯竭"),
    ("volume.price_confirmation", "量价同向确认"),
    ("volume.price_divergence", "量价背离"),
    ("liquidity.amihud", "Amihud 非流动性"),
    ("liquidity.zero_return_ratio", "零收益交易日比例"),
    ("liquidity.corwin_schultz_spread", "Corwin-Schultz 价差"),
    ("liquidity.roll_spread", "Roll 隐含价差"),
    ("liquidity.turnover_velocity", "换手速度"),
    ("liquidity.free_float_turnover", "自由流通股换手率"),
    ("liquidity.order_imbalance", "委托不平衡"),
    ("liquidity.bid_ask_spread", "买卖价差"),
    ("liquidity.depth", "盘口深度"),
    ("liquidity.trade_count", "成交笔数"),
    ("liquidity.average_trade_size", "平均单笔成交额"),
    ("liquidity.large_order_ratio", "大单成交占比"),
    ("liquidity.cancel_ratio", "撤单率"),
    ("liquidity.limit_queue", "涨跌停排队量"),
    ("liquidity.impact_cost", "冲击成本"),
)

FUNDAMENTAL_VALUATION: tuple[tuple[str, str], ...] = (
    ("valuation.pe_ttm", "滚动市盈率 PE-TTM"),
    ("valuation.pe_static", "静态市盈率"),
    ("valuation.pe_history_percentile", "市盈率历史分位"),
    ("valuation.pb", "市净率 PB"),
    ("valuation.ps_ttm", "滚动市销率 PS-TTM"),
    ("valuation.pcf_ttm", "滚动市现率 PCF-TTM"),
    ("valuation.ev_ebitda", "企业价值倍数 EV/EBITDA"),
    ("valuation.ev_ebit", "企业价值倍数 EV/EBIT"),
    ("valuation.peg", "PEG"),
    ("valuation.dividend_yield", "股息率"),
    ("valuation.market_cap", "总市值"),
    ("valuation.float_market_cap", "流通市值"),
    ("fundamental.revenue", "营业收入"),
    ("fundamental.operating_profit", "营业利润"),
    ("fundamental.net_profit", "净利润"),
    ("fundamental.net_profit_parent", "归母净利润"),
    ("fundamental.eps_basic", "基本每股收益"),
    ("fundamental.revenue_yoy", "营业收入同比增长"),
    ("fundamental.revenue_qoq", "营业收入环比增长"),
    ("fundamental.net_profit_yoy", "净利润同比增长"),
    ("fundamental.net_profit_qoq", "净利润环比增长"),
    ("fundamental.roe", "净资产收益率 ROE"),
    ("fundamental.roa", "总资产收益率 ROA"),
    ("fundamental.roic", "投入资本回报率 ROIC"),
    ("fundamental.gross_margin", "毛利率"),
    ("fundamental.operating_margin", "营业利润率"),
    ("fundamental.net_margin", "净利率"),
    ("fundamental.selling_expense_ratio", "销售费用率"),
    ("fundamental.admin_expense_ratio", "管理费用率"),
    ("fundamental.rd_expense_ratio", "研发费用率"),
    ("fundamental.financial_expense_ratio", "财务费用率"),
    ("fundamental.debt_asset_ratio", "资产负债率"),
    ("fundamental.current_ratio", "流动比率"),
    ("fundamental.quick_ratio", "速动比率"),
    ("fundamental.cash_ratio", "现金比率"),
    ("fundamental.interest_coverage", "利息保障倍数"),
    ("fundamental.operating_cashflow", "经营活动现金流"),
    ("fundamental.free_cashflow", "自由现金流"),
    ("fundamental.ocf_to_net_profit", "经营现金流/净利润"),
    ("fundamental.ocf_to_revenue", "经营现金流/营业收入"),
    ("fundamental.capex_to_revenue", "资本开支/营业收入"),
    ("fundamental.asset_turnover", "总资产周转率"),
    ("fundamental.inventory_days", "存货周转天数"),
    ("fundamental.receivable_days", "应收账款周转天数"),
    ("fundamental.payable_days", "应付账款周转天数"),
    ("fundamental.cash_conversion_cycle", "现金转换周期"),
    ("fundamental.accrual_ratio", "应计利润率"),
    ("fundamental.altman_z_score", "Altman Z 分数"),
    ("fundamental.piotroski_f_score", "Piotroski F 分数"),
    ("fundamental.goodwill_ratio", "商誉/总资产"),
)

EVENT_GROUPS: dict[str, tuple[tuple[str, str], ...]] = {
    "financial_results": (
        ("earnings_forecast_published", "业绩预告发布"),
        ("earnings_forecast_decrease", "业绩预告预减"),
        ("earnings_forecast_loss", "业绩预告首亏"),
        ("earnings_forecast_turnaround", "业绩预告扭亏"),
        ("earnings_flash_report", "业绩快报"),
        ("annual_report", "年度报告"),
        ("semiannual_report", "半年度报告"),
        ("quarterly_report", "季度报告"),
        ("earnings_revision", "业绩预告修正"),
        ("audit_opinion_change", "审计意见变化"),
    ),
    "dividends_corporate_actions": (
        ("cash_dividend_proposal", "现金分红预案"),
        ("cash_dividend_approved", "现金分红获批"),
        ("cash_dividend_ex_date", "现金分红除息"),
        ("stock_dividend_proposal", "送股预案"),
        ("stock_dividend_ex_date", "送股除权"),
        ("capitalization_issue", "资本公积转增"),
        ("rights_issue", "配股"),
        ("share_split", "股份拆细"),
        ("share_consolidation", "股份合并"),
        ("special_dividend", "特别股息"),
    ),
    "repurchase_capital": (
        ("repurchase_proposal", "回购预案"),
        ("repurchase_approved", "回购获批"),
        ("repurchase_first_execution", "首次实施回购"),
        ("repurchase_progress", "回购进展"),
        ("repurchase_completion", "回购完成"),
        ("repurchase_cancellation", "回购股份注销"),
        ("repurchase_change", "回购方案变更"),
        ("repurchase_termination", "回购终止"),
        ("capital_reduction", "减少注册资本"),
        ("treasury_share_disposal", "库存股处置"),
    ),
    "shareholder_holdings": (
        ("major_holder_increase_plan", "重要股东增持计划"),
        ("major_holder_increase_progress", "重要股东增持进展"),
        ("major_holder_decrease_plan", "重要股东减持计划"),
        ("major_holder_decrease_progress", "重要股东减持进展"),
        ("executive_increase", "董监高增持"),
        ("executive_decrease", "董监高减持"),
        ("ownership_reaches_five_percent", "持股达到百分之五"),
        ("ownership_below_five_percent", "持股降至百分之五以下"),
        ("actual_controller_change", "实际控制人变更"),
        ("employee_stock_plan_holding", "员工持股计划买卖"),
    ),
    "restricted_shares_pledges": (
        ("restricted_shares_unlock", "限售股解禁"),
        ("unlock_schedule_change", "解禁安排变更"),
        ("share_pledge", "股份质押"),
        ("share_pledge_release", "股份解除质押"),
        ("pledge_ratio_high", "高比例质押"),
        ("pledge_default_risk", "质押违约风险"),
        ("judicial_freeze", "股份司法冻结"),
        ("judicial_unfreeze", "股份解除冻结"),
        ("auction_of_shares", "股权司法拍卖"),
        ("nontradable_share_reform", "股权分置相关安排"),
    ),
    "trading_status": (
        ("suspension", "停牌"),
        ("resumption", "复牌"),
        ("temporary_suspension", "盘中临时停牌"),
        ("risk_warning_st", "实施 ST"),
        ("risk_warning_star_st", "实施 *ST"),
        ("risk_warning_removed", "撤销风险警示"),
        ("delisting_risk", "退市风险提示"),
        ("delisting_period_start", "进入退市整理期"),
        ("listing", "首发上市"),
        ("transfer_board", "转板或板块变更"),
    ),
    "regulation_risk": (
        ("inquiry_letter", "交易所问询函"),
        ("regulatory_letter", "监管工作函"),
        ("investigation_opened", "监管立案调查"),
        ("administrative_penalty", "行政处罚"),
        ("disciplinary_action", "纪律处分"),
        ("public_censure", "公开谴责"),
        ("rectification_order", "责令整改"),
        ("accounting_error_correction", "会计差错更正"),
        ("financial_restated", "财务报表重述"),
        ("information_disclosure_violation", "信息披露违规"),
    ),
    "market_activity": (
        ("dragon_tiger_buy", "龙虎榜买入"),
        ("dragon_tiger_sell", "龙虎榜卖出"),
        ("dragon_tiger_institution_buy", "机构席位龙虎榜买入"),
        ("dragon_tiger_institution_sell", "机构席位龙虎榜卖出"),
        ("block_trade_premium", "大宗交易溢价"),
        ("block_trade_discount", "大宗交易折价"),
        ("abnormal_trading_notice", "异常交易公告"),
        ("price_deviation_notice", "价格偏离值公告"),
        ("turnover_abnormal_notice", "换手率异常公告"),
        ("serious_abnormal_fluctuation", "严重异常波动"),
    ),
    "financing_securities": (
        ("margin_eligible_added", "调入融资融券标的"),
        ("margin_eligible_removed", "调出融资融券标的"),
        ("margin_balance_jump", "融资余额突增"),
        ("margin_balance_drop", "融资余额突降"),
        ("margin_buy_jump", "融资买入额突增"),
        ("securities_lending_jump", "融券余额突增"),
        ("short_sell_jump", "融券卖出量突增"),
        ("margin_warning", "融资融券风险提示"),
        ("refinancing_supply_change", "转融通供给变化"),
        ("short_sale_restriction_change", "融券卖出规则变化"),
    ),
    "index_passive": (
        ("index_inclusion_announced", "指数纳入公告"),
        ("index_exclusion_announced", "指数剔除公告"),
        ("index_weight_increase", "指数权重上调"),
        ("index_weight_decrease", "指数权重下调"),
        ("index_rebalance_effective", "指数调仓生效"),
        ("connect_northbound_added", "调入沪深股通"),
        ("connect_northbound_removed", "调出沪深股通"),
        ("etf_creation_redemption_change", "ETF 申赎清单变化"),
        ("sample_universe_change", "样本空间规则变化"),
        ("free_float_factor_change", "自由流通量因子调整"),
    ),
    "contracts_orders": (
        ("major_contract_signed", "重大合同签署"),
        ("major_contract_won", "重大中标"),
        ("framework_agreement", "框架协议"),
        ("purchase_order_received", "重大订单取得"),
        ("contract_progress", "合同履约进展"),
        ("contract_completed", "合同完成"),
        ("contract_changed", "合同变更"),
        ("contract_terminated", "合同终止"),
        ("customer_certification", "客户认证通过"),
        ("supplier_qualification", "供应商资格取得"),
    ),
    "capacity_operations": (
        ("capacity_project_announced", "产能项目公告"),
        ("capacity_construction_started", "产能开工"),
        ("capacity_trial_production", "试生产"),
        ("capacity_mass_production", "量产"),
        ("capacity_ramp_up", "产能爬坡"),
        ("capacity_full_production", "达产"),
        ("capacity_expansion", "扩产"),
        ("capacity_delay", "投产延期"),
        ("capacity_shutdown", "产线停产"),
        ("capacity_disposal", "产能出售或退出"),
    ),
    "m_and_a_restructuring": (
        ("acquisition_plan", "收购预案"),
        ("asset_sale_plan", "重大资产出售"),
        ("restructuring_suspension", "重组停牌"),
        ("restructuring_resumption", "重组复牌"),
        ("restructuring_approved_board", "重组获董事会通过"),
        ("restructuring_approved_shareholders", "重组获股东大会通过"),
        ("restructuring_regulatory_approval", "重组获监管批准"),
        ("restructuring_completed", "重组完成"),
        ("restructuring_terminated", "重组终止"),
        ("spin_off_listing", "分拆上市"),
    ),
    "governance_personnel": (
        ("chairman_change", "董事长变更"),
        ("ceo_change", "总经理变更"),
        ("cfo_change", "财务负责人变更"),
        ("board_secretary_change", "董事会秘书变更"),
        ("director_resignation", "董事辞任"),
        ("supervisor_resignation", "监事辞任"),
        ("independent_director_objection", "独立董事异议"),
        ("equity_incentive_plan", "股权激励计划"),
        ("equity_incentive_grant", "股权激励授予"),
        ("employee_stock_plan", "员工持股计划"),
    ),
    "litigation_credit": (
        ("major_litigation", "重大诉讼"),
        ("litigation_progress", "诉讼进展"),
        ("arbitration", "重大仲裁"),
        ("asset_seizure", "资产查封扣押"),
        ("debt_default", "债务违约"),
        ("credit_rating_upgrade", "信用评级上调"),
        ("credit_rating_downgrade", "信用评级下调"),
        ("bond_early_redemption", "债券提前赎回"),
        ("guarantee_risk", "对外担保风险"),
        ("bankruptcy_reorganization", "破产重整"),
    ),
    "macro_policy_industry": (
        ("industry_policy_support", "行业支持政策"),
        ("industry_policy_restriction", "行业限制政策"),
        ("product_price_increase", "主要产品涨价"),
        ("product_price_decrease", "主要产品降价"),
        ("raw_material_price_increase", "原材料涨价"),
        ("raw_material_price_decrease", "原材料降价"),
        ("tariff_change", "关税变化"),
        ("subsidy_awarded", "政府补助"),
        ("license_approval", "业务许可获批"),
        ("industry_accident", "行业重大事故"),
    ),
}

EXTERNAL_WEB_EVENT_CODES = frozenset(
    {
        "event.contracts_orders.major_contract_won",
        "event.macro_policy_industry.license_approval",
    }
)


STABLE_IMPLEMENTATIONS = {
    "technical.ma": "ashare_lab.domain.signals.runtime:SignalRuntime[technical.ma]",
    "technical.ema": "ashare_lab.domain.signals.indicators:exponential_moving_average",
    "technical.ma_cross": "ashare_lab.domain.signals.indicators:moving_average_cross",
    "technical.bbi": "ashare_lab.domain.signals.indicators:bull_and_bear_index",
    "technical.macd": "ashare_lab.domain.signals.indicators:macd",
    "technical.rsi": "ashare_lab.domain.signals.indicators:rsi",
    "technical.bollinger": "ashare_lab.domain.signals.indicators:bollinger_bands",
    "technical.kdj": "ashare_lab.domain.signals.indicators:kdj",
    "technical.cci": "ashare_lab.domain.signals.indicators:cci",
    "technical.ema_bias": "ashare_lab.domain.signals.indicators:ema_bias",
    "technical.obv": "ashare_lab.domain.signals.indicators:on_balance_volume",
    "technical.trend_regime": "ashare_lab.domain.signals.indicators:stage_trend",
    "technical.atr": "ashare_lab.domain.signals.indicators:average_true_range",
    "technical.natr": "ashare_lab.domain.signals.indicators:normalized_average_true_range",
    "technical.adx": "ashare_lab.domain.signals.indicators:directional_movement",
    "technical.dmi": "ashare_lab.domain.signals.indicators:directional_movement",
    "technical.bias": "ashare_lab.domain.signals.indicators:simple_bias",
    "technical.roc": "ashare_lab.domain.signals.indicators:rate_of_change",
    "technical.momentum": "ashare_lab.domain.signals.indicators:momentum",
    "technical.stochastic": "ashare_lab.domain.signals.indicators:stochastic_oscillator",
    "technical.williams_r": "ashare_lab.domain.signals.indicators:williams_r",
    "technical.donchian": "ashare_lab.domain.signals.indicators:donchian_channel",
    "technical.return_stddev": "ashare_lab.domain.signals.indicators:return_stddev",
    "technical.historical_volatility": "ashare_lab.domain.signals.indicators:historical_volatility",
    "price.close": "ashare_lab.domain.signals.runtime:SignalRuntime[price.close]",
    "price.return_pct": "ashare_lab.domain.signals.indicators:period_return_pct",
    "price.rolling_high": "ashare_lab.domain.signals.indicators:rolling_high",
    "price.consecutive_up": "ashare_lab.domain.signals.indicators:consecutive_up",
    "price.amplitude": "ashare_lab.domain.signals.indicators:amplitude_pct",
    "price.true_range": "ashare_lab.domain.signals.indicators:true_range",
    "market.volume": "ashare_lab.domain.signals.indicators:volume_average",
    "market.amount": "ashare_lab.domain.signals.indicators:market_amount",
    "market.turnover_rate": "ashare_lab.domain.signals.runtime:SignalRuntime[market.turnover_rate]",
    "amount.average": "ashare_lab.domain.signals.indicators:amount_average",
    "volume.relative": "ashare_lab.domain.signals.indicators:relative_volume",
    "volume.price_confirmation": "ashare_lab.domain.signals.indicators:volume_price_points",
    "volume.price_divergence": "ashare_lab.domain.signals.indicators:confirmed_obv_divergence",
}

STABLE_FORMULAS = {
    "technical.ma": (
        "SMA_t(n)=sum(price_field[t-n+1:t])/n，price_field 默认 close；"
        "触发器比较同一价格字段与同周期均线。"
    ),
    "technical.ema": "EMA 使用首值播种递推，alpha=2/(period+1)；满 period 根后才允许产生值。",
    "technical.ma_cross": (
        "分别计算 fast_period 与 slow_period 的简单移动平均线，并判断快线上穿或下穿慢线。"
    ),
    "technical.bbi": (
        "BBI=(MA(period_1)+MA(period_2)+MA(period_3)+MA(period_4))/4，默认周期为 3、6、12、24。"
    ),
    "technical.macd": "DIF=EMA(close,fast)-EMA(close,slow)，DEA=EMA(DIF,signal)，柱=2*(DIF-DEA)。",
    "technical.rsi": "Wilder RSI=100-100/(1+平滑平均涨幅/平滑平均跌幅)。",
    "technical.bollinger": (
        "中轨为 period 日总体均值，上下轨为中轨加减 stddev_multiplier 倍总体标准差。"
    ),
    "technical.kdj": (
        "RSV=100*(close-LLV)/(HHV-LLV)，零振幅时 RSV=50；K、D 以 50 播种并按各自 "
        "smoothing 参数递推，J=3*K-2*D。"
    ),
    "technical.cci": "CCI=(典型价-period 日均典型价)/(constant*period 日平均绝对偏差)。",
    "technical.ema_bias": "EMA 乖离率=100*(price/EMA(period)-1)，默认 period=28。",
    "price.close": "直接比较规范日线的不复权收盘价与人民币固定阈值；信号收盘确认。",
    "price.return_pct": "区间涨跌幅=100*(price_t/price_(t-period)-1)，数值单位为百分点。",
    "price.rolling_high": (
        "new_high 判断当日 price_field 严格高于此前 period 根日线同一 price_field 的最大值，"
        "窗口排除当日，相等不触发；不要求上一日未触发。"
        "price_field=close（默认）比较当日收盘价与此前最高收盘价；"
        "price_field=high 比较当日最高价与此前最高价，不能表达当日收盘价与此前 high 的比较。"
    ),
    "price.consecutive_up": "连续比较相邻收盘价；收盘价严格上涨累计一天，平盘或下跌归零。",
    "price.opening_gap": (
        "跳空高开为 open_t > high_(t-1)；跳空低开为 open_t < low_(t-1)。"
        "当前仅映射旧原子语义，未升级为新引擎 stable 信号。"
    ),
    "price.amplitude": "振幅=100*(当日最高价-当日最低价)/前一交易日收盘价。",
    "market.volume": "兼容口径：当日成交量与含当日的 N 日简单平均成交量比较。",
    "market.amount": "直接使用规范日线中的当日成交额，单位为人民币元。",
    "market.turnover_rate": (
        "直接比较供应商保存的原始换手率百分点；不由成交量、成交额或流通股本推导。"
        "缺失原始值、provider 或 methodology 时拒绝运行。"
    ),
    "amount.average": "含当日在内的 period 日成交额简单平均值，单位为人民币元。",
    "volume.relative": (
        "RVOL=当日成交量/此前 baseline_period 个正成交量交易日均量；基线排除当日，"
        "停牌或零成交量日不进入样本且不触发。"
        "gt_multiple/gte_multiple/lte_multiple 分别判断当日 RVOL > / >= / <= value，"
        "不使用 consecutive_days；该共享参数即使保留目录默认值3，也不增加连续多日条件。"
        "仅 consecutive_gte_multiple 使用 consecutive_days，要求最近 consecutive_days "
        "个有效 RVOL 观测均 >= value。各触发器都使用 baseline_period 计算均量基线。"
    ),
    "volume.price_confirmation": (
        "放量大涨/大跌要求 RVOL 达到 volume_multiple，且相对前一有效交易日收盘涨跌幅"
        "达到 return_threshold_pct。"
    ),
    "technical.obv": "OBV 按收盘涨跌累加或扣减成交量；零成交量日保持状态但不产生信号。",
    "volume.price_divergence": (
        "价格枢轴需等待 right_bars 根有效日线确认；相邻高低点满足价格阈值且 OBV 反向变化"
        "达到历史均量阈值时，信号仅在确认日产生，不回填枢轴日。"
    ),
    "technical.trend_regime": (
        "阶段上涨/下跌由收盘价、SMA20/SMA60 结构、短均线斜率及 Wilder ADX/+DI/-DI "
        "共同确认；默认连续两日确认，否则为震荡。"
    ),
    "price.true_range": "TR=max(high-low, abs(high-prev_close), abs(low-prev_close))；首根未知。",
    "technical.atr": ("Wilder ATR 以最初 period 个 TR 算术均值播种，之后 alpha=1/period 递推。"),
    "technical.natr": "NATR=100*WilderATR(period)/close，单位为百分点。",
    "technical.adx": "Wilder +DM/-DM/TR 平滑后计算 DX，再以 alpha=1/period 平滑为 ADX。",
    "technical.dmi": "DMI 比较同一 Wilder period 的 +DI 与 -DI，并支持交叉或持续关系。",
    "technical.bias": "SMA 乖离率=100*(price/SMA(period)-1)。",
    "technical.roc": "ROC=100*(price_t/price_(t-period)-1)，单位为百分点。",
    "technical.momentum": "MOM=price_t-price_(t-period)，单位与价格一致。",
    "technical.stochastic": (
        "%K=100*(close-LL)/(HH-LL)，零振幅为50；%D 为最近 d_period 个 %K 的简单均值。"
    ),
    "technical.williams_r": (
        "Williams %R=-100*(HH-close)/(HH-LL)，零振幅为-50，输出范围[-100,0]。"
    ),
    "technical.donchian": (
        "唐奇安上/下轨为此前 period 根日线 high 最大值/low 最小值，窗口排除当日；"
        "被比较的当日价格始终为 close，不支持 price_field 参数。"
        "price_above_upper/price_below_lower 分别判断当日 close > 上轨/close < 下轨。"
        "price_crosses_above_upper 还要求前一日 close <= 前一日上轨；"
        "price_crosses_below_lower 还要求前一日 close >= 前一日下轨。"
        "前一日轨道按前一日此前 period 根日线计算，不复用当日轨道。"
    ),
    "technical.return_stddev": ("最近 period 个简单日收益率百分点的总体标准差(ddof=0)。"),
    "technical.historical_volatility": (
        "最近 period 个对数日收益率的总体标准差乘 sqrt(annualization_sessions)*100。"
    ),
}


def _bar_requirement(*, l2: bool = False) -> dict[str, Any]:
    if l2:
        return {
            "dataset_id": "market.l2.orderbook",
            "fields": ["instrument_id", "exchange_sequence", "bid", "ask", "depth", "action"],
            "frequency": "tick",
            "point_in_time_required": True,
            "required_time_fields": ["fact_at", "available_at", "ingested_at", "revision_id"],
            "license_status": "not_acquired",
        }
    return {
        "dataset_id": "market.ohlcv.daily",
        "fields": [
            "instrument_id",
            "session_date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
        ],
        "frequency": "1d",
        "point_in_time_required": True,
        "required_time_fields": ["fact_at", "available_at", "ingested_at", "revision_id"],
        "license_status": "cleared",
    }


def _daily_time() -> dict[str, Any]:
    return {
        "observation_basis": "bar_close",
        "required_quality": "exchange_session",
        "date_only_policy": "not_applicable",
        "revision_policy": "as_known_at_cutoff",
        "default_order_time": "next_tradable_session_open",
        "daily_bar_fallback": "next_tradable_session_open",
        "notes": "日线指标在收盘后确认；不得以同一根日线收盘价成交。",
    }


def _legacy_mapping() -> dict[str, str]:
    from astock_backtest.signal_atoms import ATOM_REGISTRY

    mapping: dict[str, str] = {}
    shape_ids = {
        "doji": "price.doji",
        "hammer": "price.hammer",
        "inverted_hammer": "price.inverted_hammer",
        "long_upper_shadow": "price.shooting_star",
        "long_lower_shadow": "price.hammer",
    }
    for atom_id in ATOM_REGISTRY:
        if atom_id.startswith("ma_") or atom_id.startswith("close_"):
            target = "technical.ma"
        elif atom_id.startswith("macd_"):
            target = "technical.macd"
        elif atom_id.startswith("rsi_"):
            target = "technical.rsi"
        elif atom_id.startswith("kdj_"):
            target = "technical.kdj"
        elif atom_id.startswith("bollinger_"):
            target = "technical.bollinger"
        elif atom_id.startswith("new_high_"):
            target = "price.all_time_high" if atom_id.endswith("alltime") else "price.rolling_high"
        elif atom_id.startswith("new_low_"):
            target = "price.rolling_low"
        elif atom_id in {"limit_up", "limit_down"}:
            target = "price.limit_state"
        elif atom_id == "one_word_limit":
            target = "price.one_price_limit"
        elif atom_id in {"gap_up", "gap_down"}:
            target = "price.opening_gap"
        elif atom_id.startswith("single_day_"):
            target = "price.return_pct"
        elif atom_id.startswith("consecutive_up_"):
            target = "price.consecutive_up"
        elif atom_id.startswith("consecutive_down_"):
            target = "price.consecutive_down"
        elif atom_id in shape_ids:
            target = shape_ids[atom_id]
        elif atom_id.startswith("volume_") and "ma20" in atom_id:
            target = "market.volume"
        elif atom_id == "volume_max_60d":
            target = "volume.rolling_high"
        elif atom_id in {"vol_up_price_up", "vol_down_price_down"}:
            target = "volume.price_confirmation"
        elif atom_id in {"vol_up_price_down", "vol_down_price_up"}:
            target = "volume.price_divergence"
        elif atom_id.startswith("turnover_"):
            target = "market.turnover_rate"
        elif atom_id.startswith("pe_below_history_"):
            target = "valuation.pe_history_percentile"
        else:
            raise AssertionError(f"legacy atom is not mapped: {atom_id}")
        mapping[atom_id] = target
    if len(mapping) != 85:
        raise AssertionError(f"expected 85 in-scope legacy atoms, got {len(mapping)}")
    return mapping


def _metric_payloads() -> list[dict[str, Any]]:
    legacy = _legacy_mapping()
    payloads: list[dict[str, Any]] = []
    groups: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
        ("technical", TECHNICAL),
        ("price_action", PRICE_ACTION),
        ("volume_liquidity", VOLUME_LIQUIDITY),
        ("fundamental_valuation", FUNDAMENTAL_VALUATION),
    )
    research_volume_ids = {item[0] for item in VOLUME_LIQUIDITY[1:5]}
    for family, definitions in groups:
        for metric_id, name_zh in definitions:
            if metric_id in STABLE_IMPLEMENTATIONS:
                status = "stable"
            elif family in {"technical", "price_action"} or metric_id in research_volume_ids:
                status = "research_only"
            else:
                status = "unavailable"

            l2 = metric_id in {
                "liquidity.order_imbalance",
                "liquidity.bid_ask_spread",
                "liquidity.depth",
                "liquidity.trade_count",
                "liquidity.average_trade_size",
                "liquidity.large_order_ratio",
                "liquidity.cancel_ratio",
                "liquidity.limit_queue",
                "liquidity.impact_cost",
            }
            if metric_id == "market.turnover_rate":
                requirement = {
                    "dataset_id": "market.ohlcv.daily",
                    "fields": [
                        "instrument_id",
                        "session_date",
                        "turnover_rate_pct",
                        "turnover_rate_provider",
                        "turnover_rate_methodology",
                    ],
                    "frequency": "1d",
                    "point_in_time_required": True,
                    "required_time_fields": [
                        "fact_at",
                        "available_at",
                        "ingested_at",
                        "revision_id",
                    ],
                    "license_status": "cleared",
                }
                time_semantics = {
                    "observation_basis": "bar_close",
                    "required_quality": "exchange_session",
                    "date_only_policy": "not_applicable",
                    "revision_policy": "as_known_at_cutoff",
                    "default_order_time": "next_tradable_session_open",
                    "daily_bar_fallback": "next_tradable_session_open",
                    "notes": (
                        "只使用供应商原始换手率百分点；不得由成交量、成交额或流通股本推导。"
                        "原始值、provider 或 methodology 缺一即拒绝。"
                    ),
                }
            elif family == "fundamental_valuation":
                requirement = {
                    "dataset_id": "issuer.financials.point_in_time",
                    "fields": [
                        "instrument_id",
                        "report_period",
                        "field_code",
                        "value",
                        "revision_id",
                    ],
                    "frequency": "quarterly",
                    "point_in_time_required": True,
                    "required_time_fields": [
                        "fact_at",
                        "available_at",
                        "ingested_at",
                        "revision_id",
                    ],
                    "license_status": "not_acquired",
                }
                time_semantics = {
                    "observation_basis": "report_period",
                    "required_quality": "report_period_with_publication_time",
                    "date_only_policy": "conservative_after_close",
                    "revision_policy": "as_known_at_cutoff",
                    "default_order_time": "next_tradable_session_open",
                    "daily_bar_fallback": "next_tradable_session_open",
                    "notes": "财务期末日不是可用日；必须按当时公开时间和当时已知修订版本回放。",
                }
            else:
                requirement = _bar_requirement(l2=l2)
                time_semantics = _daily_time()

            stable = status == "stable"
            blockers: list[str] = []
            if status == "research_only":
                blockers.append("尚未接入新权威 SignalRuntime 的公式、边界与前缀不变性测试")
            elif status == "unavailable":
                blockers.append("当前数据快照或授权不满足 point-in-time 回测要求")
                blockers.append("尚无权威运行时实现与边界测试")

            payloads.append(
                {
                    "id": metric_id,
                    "kind": "data_series" if family == "fundamental_valuation" else "indicator",
                    "family": family,
                    "name_zh": name_zh,
                    "description": (
                        f"A 股单股回测目录中的{name_zh}；"
                        "状态仅表示当前产品可用性，不代表投资有效性。"
                    ),
                    "status": status,
                    "formula_summary": STABLE_FORMULAS.get(metric_id)
                    or (
                        f"按版本化定义计算{name_zh}，参数、复权口径和缺失值规则须在实现前冻结。"
                        if status == "research_only"
                        else None
                    ),
                    "parameters": _parameters(metric_id),
                    "triggers": _triggers(metric_id),
                    "warmup_bars": _warmup(metric_id),
                    "data_requirements": [requirement],
                    "time_semantics": time_semantics,
                    "implementation_ref": STABLE_IMPLEMENTATIONS.get(metric_id),
                    "legacy_atom_ids": sorted(
                        key for key, value in legacy.items() if value == metric_id
                    ),
                    "blockers": blockers if not stable else [],
                }
            )
    if len(payloads) != 182:
        raise AssertionError(f"expected 182 metrics, got {len(payloads)}")
    return payloads


def _parameters(metric_id: str) -> list[str]:
    if metric_id == "technical.macd":
        return ["fast", "slow", "signal"]
    if metric_id in {"technical.ema", "technical.ema_bias"}:
        return ["period", "price_field"]
    if metric_id == "technical.ma_cross":
        return ["fast_period", "slow_period", "price_field"]
    if metric_id == "technical.bollinger":
        return ["period", "stddev_multiplier", "price_field"]
    if metric_id == "technical.kdj":
        return ["period", "k_smoothing", "d_smoothing"]
    if metric_id == "technical.cci":
        return ["period", "constant"]
    if metric_id == "technical.bbi":
        return ["period_1", "period_2", "period_3", "period_4", "price_field"]
    if metric_id == "technical.trend_regime":
        return [
            "short_period",
            "long_period",
            "slope_lookback",
            "adx_period",
            "adx_threshold",
            "confirmation_days",
            "stability_bars",
        ]
    if metric_id == "technical.obv":
        return []
    if metric_id in {"technical.atr", "technical.natr", "technical.adx", "technical.dmi"}:
        return ["period"]
    if metric_id in {"technical.bias", "technical.roc", "technical.momentum"}:
        return ["period", "price_field"]
    if metric_id == "technical.stochastic":
        return ["k_period", "d_period"]
    if metric_id == "technical.williams_r":
        return ["period"]
    if metric_id == "technical.donchian":
        return ["period"]
    if metric_id == "technical.return_stddev":
        return ["period", "price_field"]
    if metric_id == "technical.historical_volatility":
        return ["period", "annualization_sessions", "price_field"]
    if metric_id == "price.close":
        return []
    if metric_id in {"price.return_pct", "price.rolling_high"}:
        return ["period", "price_field"]
    if metric_id == "price.consecutive_up":
        return ["days"]
    if metric_id == "price.opening_gap":
        return []
    if metric_id in {
        "price.amplitude",
        "price.true_range",
        "market.amount",
        "market.turnover_rate",
    }:
        return []
    if metric_id == "amount.average":
        return ["period"]
    if metric_id == "volume.relative":
        return ["baseline_period", "consecutive_days"]
    if metric_id == "volume.price_confirmation":
        return ["baseline_period", "volume_multiple", "return_threshold_pct"]
    if metric_id == "volume.price_divergence":
        return [
            "left_bars",
            "right_bars",
            "min_separation",
            "max_separation",
            "price_threshold_pct",
            "obv_threshold_adv",
            "average_volume_period",
        ]
    if metric_id == "market.turnover_rate":
        return []
    if metric_id == "technical.ma":
        return ["period", "price_field"]
    if metric_id == "technical.rsi":
        return ["period"]
    if metric_id == "market.volume":
        return ["baseline_period"]
    if metric_id.startswith("price."):
        return ["lookback", "threshold"]
    return ["lookback"]


def _triggers(metric_id: str) -> list[str]:
    if metric_id == "technical.macd":
        return ["golden_cross", "death_cross", "crosses_above_zero", "crosses_below_zero"]
    if metric_id == "technical.ma":
        return ["price_crosses_above", "price_crosses_below", "price_above", "price_below"]
    if metric_id in {"technical.ema", "technical.bbi"}:
        return ["price_crosses_above", "price_crosses_below", "price_above", "price_below"]
    if metric_id == "technical.ma_cross":
        return ["golden_cross", "death_cross", "fast_above_slow", "fast_below_slow"]
    if metric_id == "technical.bollinger":
        return [
            "price_crosses_above_upper",
            "price_crosses_below_upper",
            "price_crosses_above_middle",
            "price_crosses_below_middle",
            "price_crosses_above_lower",
            "price_crosses_below_lower",
        ]
    if metric_id == "technical.kdj":
        return ["golden_cross", "death_cross", "j_above", "j_below"]
    if metric_id in {"technical.cci", "technical.ema_bias"}:
        return ["crosses_above", "crosses_below", "above", "below"]
    if metric_id == "technical.obv":
        return ["rising", "falling"]
    if metric_id == "technical.trend_regime":
        return ["uptrend", "downtrend", "range"]
    if metric_id == "technical.dmi":
        return [
            "plus_crosses_above_minus",
            "plus_crosses_below_minus",
            "plus_above_minus",
            "plus_below_minus",
        ]
    if metric_id == "technical.stochastic":
        return ["k_crosses_above_d", "k_crosses_below_d", "k_above", "k_below"]
    if metric_id == "technical.donchian":
        return [
            "price_crosses_above_upper",
            "price_crosses_below_lower",
            "price_above_upper",
            "price_below_lower",
        ]
    if metric_id in {"price.close", "price.return_pct"}:
        return [
            "crosses_above",
            "crosses_below",
            "above",
            "below",
            "at_least",
            "at_most",
        ]
    if metric_id in {
        "price.amplitude",
        "price.true_range",
        "market.amount",
        "market.turnover_rate",
        "amount.average",
    }:
        return ["crosses_above", "crosses_below", "above", "below"]
    if metric_id == "price.rolling_high":
        return ["new_high"]
    if metric_id == "price.consecutive_up":
        return ["at_least"]
    if metric_id == "price.opening_gap":
        return ["gap_up", "gap_down"]
    if metric_id == "technical.rsi":
        return ["crosses_above", "crosses_below", "above", "below"]
    if metric_id == "market.volume":
        return ["gte_multiple", "lte_multiple"]
    if metric_id == "volume.relative":
        return ["gte_multiple", "lte_multiple", "consecutive_gte_multiple", "gt_multiple"]
    if metric_id == "volume.price_confirmation":
        return ["surge_up", "surge_down"]
    if metric_id == "volume.price_divergence":
        return ["bearish", "bullish"]
    return ["above", "below", "crosses_above", "crosses_below"]


def _warmup(metric_id: str) -> int:
    return {
        "technical.macd": 35,
        "technical.ma": 21,
        "technical.rsi": 16,
        "technical.ema": 29,
        "technical.ma_cross": 21,
        "technical.bollinger": 21,
        "technical.kdj": 10,
        "technical.cci": 15,
        "technical.bbi": 25,
        "technical.ema_bias": 29,
        "price.close": 2,
        "price.return_pct": 7,
        "price.rolling_high": 21,
        "price.consecutive_up": 4,
        "price.opening_gap": 2,
        "price.amplitude": 3,
        "market.amount": 2,
        "market.turnover_rate": 2,
        "amount.average": 21,
        "volume.relative": 23,
        "volume.price_confirmation": 21,
        "technical.obv": 2,
        "volume.price_divergence": 84,
        "technical.trend_regime": 121,
        "price.true_range": 3,
        "technical.atr": 16,
        "technical.natr": 16,
        "technical.adx": 29,
        "technical.dmi": 29,
        "technical.bias": 21,
        "technical.roc": 14,
        "technical.momentum": 12,
        "technical.stochastic": 17,
        "technical.williams_r": 15,
        "technical.donchian": 22,
        "technical.return_stddev": 22,
        "technical.historical_volatility": 22,
    }.get(metric_id, 20)


def _event_data_requirements(event_code: str, status: str) -> list[dict[str, Any]]:
    if event_code in EXTERNAL_WEB_EVENT_CODES:
        return [
            {
                "dataset_id": "web.documents.point_in_time",
                "fields": [
                    "document_id",
                    "canonical_url",
                    "publisher_name",
                    "publisher_type",
                    "published_at",
                    "content_sha256",
                    "revision_id",
                ],
                "frequency": "event",
                "point_in_time_required": True,
                "required_time_fields": [
                    "fact_at",
                    "available_at",
                    "ingested_at",
                    "revision_id",
                ],
                "license_status": "internal_only",
            },
            {
                "dataset_id": "external.events.point_in_time",
                "fields": [
                    "instrument_id",
                    "event_code",
                    "source_event_id",
                    "external_fact_id",
                    "publisher_authority",
                    "entity_match_method",
                    "attributes",
                    "revision_id",
                ],
                "frequency": "event",
                "point_in_time_required": True,
                "required_time_fields": [
                    "fact_at",
                    "available_at",
                    "ingested_at",
                    "revision_id",
                ],
                "license_status": "internal_only",
            },
        ]
    return [
        {
            "dataset_id": "issuer.events.point_in_time",
            "fields": [
                "instrument_id",
                "event_code",
                "source_event_id",
                "payload",
                "revision_id",
            ],
            "frequency": "event",
            "point_in_time_required": True,
            "required_time_fields": [
                "fact_at",
                "available_at",
                "ingested_at",
                "revision_id",
            ],
            "license_status": (
                "internal_only"
                if status == "stable"
                else "unknown"
                if status == "research_only"
                else "not_acquired"
            ),
        }
    ]


def _event_payloads() -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    research_categories = set(tuple(EVENT_GROUPS)[:6])
    for category, definitions in EVENT_GROUPS.items():
        for code, name_zh in definitions:
            event_code = f"event.{category}.{code}"
            if event_code in EXECUTABLE_EVENT_DEFINITIONS:
                status = "stable"
            elif category in research_categories:
                status = "research_only"
            else:
                status = "unavailable"
            is_external_web_event = event_code in EXTERNAL_WEB_EVENT_CODES
            payloads.append(
                {
                    "id": event_code,
                    "category": category,
                    "name_zh": name_zh,
                    "description": (
                        f"A 股事件分类：{name_zh}。仅接受有明确事实标识、权威发布者"
                        "和精确首次可得时间的原始网页；"
                        "搜索摘要与任意新闻不能直接触发交易。"
                        if is_external_web_event
                        else f"A 股事件分类：{name_zh}。信号时间取首次可获得时间，不倒填公告日期。"
                    ),
                    "status": status,
                    "lifecycle_states": [
                        "announced",
                        "amended",
                        "effective",
                        "completed",
                        "cancelled",
                    ],
                    "dedupe_keys": (
                        [
                            "source_provider",
                            "source_event_id",
                            "external_fact_id",
                            "available_at",
                        ]
                        if is_external_web_event
                        else ["source_provider", "source_event_id", "available_at"]
                    ),
                    "data_requirements": _event_data_requirements(event_code, status),
                    "time_semantics": {
                        "observation_basis": "available_at",
                        "required_quality": "exact_timestamp",
                        "date_only_policy": (
                            "reject" if status == "stable" else "conservative_after_close"
                        ),
                        "revision_policy": "first_seen_only",
                        "default_order_time": "next_tradable_session_open",
                        "daily_bar_fallback": "next_tradable_session_open",
                        "notes": (
                            "搜索只用于发现；必须固化权威原文、事实标识、实体匹配和精确首次可得时间。"
                            "有精确时间也不得早于 available_at，最早下一可交易日开盘。"
                            if is_external_web_event
                            else (
                                "必须有秒级 available_at；仅有日期的记录不得进入正式回测，"
                                "最早下一可交易日开盘。分钟引擎可另建可用后的连续竞价定义。"
                                if status == "stable"
                                else "仅有日期时只保留研究观察，不得自动升级成 stable 信号；"
                                "若未来形成已审批的保守定义，最早仍为下一可交易日开盘。"
                            )
                        ),
                    },
                    "implementation_ref": (
                        "ashare_lab.domain.events.runtime:evaluate_event_condition_aligned"
                        if status == "stable"
                        else None
                    ),
                    "blockers": (
                        []
                        if status == "stable"
                        else [
                            "尚未接入保留首次公开时间、修订链和授权证明的事件数据 Adapter",
                            "事件 SignalRuntime 尚未实现，因此不能提交交易回测",
                        ]
                    ),
                }
            )
    if len(payloads) != 160:
        raise AssertionError(f"expected 160 events, got {len(payloads)}")
    return payloads


def build_release() -> CoverageCatalogRelease:
    return CoverageCatalogRelease.model_validate(
        {
            "schema_version": "catalog-coverage.v1",
            "catalog_id": "cn_a.coverage",
            "release_version": "2026.09.01",
            "state": "active",
            "published_on": "2026-09-01",
            "metrics": _metric_payloads(),
            "events": _event_payloads(),
        }
    )


def _json_payload(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def expected_artifacts() -> dict[Path, str]:
    release = build_release()
    schema = CoverageCatalogRelease.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://schemas.ashare-lab.local/catalog-coverage.v1.schema.json"
    return {
        OUTPUT: _json_payload(release.model_dump(mode="json")),
        SCHEMA_OUTPUT: _json_payload(schema),
    }


def _check(paths: Iterable[tuple[Path, str]]) -> int:
    stale = [
        str(path.relative_to(ROOT))
        for path, content in paths
        if not path.exists() or path.read_text(encoding="utf-8") != content
    ]
    if stale:
        print("stale coverage artifacts: " + ", ".join(stale))
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    artifacts = expected_artifacts()
    if args.check:
        return _check(artifacts.items())
    for path, content in artifacts.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
