"""Connect pinned MX inputs to the existing daily share ledger and report."""
from dataclasses import asdict, fields
from datetime import datetime, time
from decimal import Decimal
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

from ashare_lab.api.result_schemas import BacktestResultBundle
from ashare_lab.application.daily_backtest import (
    ENTRY_SIGNAL_VALIDITY_POLICY, DailyBacktestConfig, DailyBacktestInput, run_daily_backtest,
)
from ashare_lab.application.funded_benchmark import FundedBenchmarkInput, FundedBuyAndHoldConfig, run_funded_buy_and_hold
from ashare_lab.application.minute_replay_input import MinuteReplayDataError
from ashare_lab.application.result_views import build_result_bundle, calculate_result_bundle_hash
from ashare_lab.application.robustness import run_execution_robustness
from ashare_lab.domain.execution import AshareExchange, FeeCalculator, FeePolicy, TradingCalendar
from ashare_lab.domain.market_data import DailyBar, InstrumentSession, standard_buy_quantity_rule
from ashare_lab.domain.shared import InstrumentId, Money, Price, Quantity
from ashare_lab.domain.strategy import canonical_hash


def replay_mx_daily_shares(*, run_id, strategy, prepared, config, calendar_input, corporate, runtime_evidence=None):
    history = prepared.history
    dates, calendar_source, calendar_bytes = calendar_input
    required = [day for day in dates if strategy.backtest.start <= day <= strategy.backtest.end]
    row_days = {row.session_date for row in history.rows}
    if not required or any(day not in row_days for day in required):
        raise MinuteReplayDataError("security_session_missing")
    if dates[-1] <= required[-1]:
        raise MinuteReplayDataError("market_calendar_missing_next_settlement_session")
    instrument = InstrumentId(history.instrument_id)
    if history.instrument_id != strategy.instrument.symbol:
        raise MinuteReplayDataError("market_data_instrument_mismatch")
    calendar = TradingCalendar(sha256(calendar_bytes).hexdigest(), dates)
    minimum, increment = standard_buy_quantity_rule(history.board)
    bars, sessions = [], []
    for row in history.rows:
        at = datetime.combine(row.session_date, time(15), ZoneInfo("Asia/Shanghai"))
        bars.append(DailyBar(instrument, row.session_date,
            *(Price(getattr(row, f"raw_{name}")) for name in ("open", "high", "low", "close")),
            Quantity(row.volume), row.amount, at))
        sessions.append(InstrumentSession(instrument, row.session_date, history.board, row.trading_status,
            Price(row.raw_preclose), Price(row.upper_limit) if row.upper_limit is not None else None,
            Price(row.lower_limit) if row.lower_limit is not None else None, minimum, increment, is_st=row.is_st))
    fees = FeeCalculator(FeePolicy(AshareExchange(history.instrument_id.rsplit(".", 1)[1]),
                                  config.commission_rate, Money(config.minimum_commission_cny)))
    execution = DailyBacktestConfig(**{field.name: getattr(config, field.name)
        for field in fields(DailyBacktestConfig) if hasattr(config, field.name)})
    benchmark = run_funded_buy_and_hold(FundedBenchmarkInput(
        run_key=f"{run_id}:benchmark", period_start=strategy.backtest.start, period_end=strategy.backtest.end,
        bars=tuple(bars), sessions=tuple(sessions), calendar=calendar, fee_calculator=fees,
        corporate_actions=corporate.applier,
        config=FundedBuyAndHoldConfig(initial_cash=Money(Decimal(strategy.backtest.initial_cash_cny)),
            **{key: getattr(execution, key) for key in ("participation_rate", "slippage_bps", "slippage_cny",
                "limit_handling", "allocation_ratio", "capacity_mode")})))
    request = DailyBacktestInput(run_key=str(run_id), strategy=strategy, bars=tuple(bars),
        sessions=tuple(sessions), calendar=calendar, fee_calculator=fees,
        provider_entry_timeline=prepared.entry_timeline, provider_exit_timeline=prepared.exit_timeline,
        corporate_actions=corporate.applier.actions, risk_price_rebases=corporate.price_rebases, config=execution,
        benchmark_equity=benchmark.funded_equity_path, benchmark_initial_equity=benchmark.initial_cash.amount,
        benchmark_entry_filled=benchmark.entry_fill is not None)
    result = run_daily_backtest(request)
    evidence = dict(history=[asdict(row) for row in history.rows], calendar=calendar_source,
                    calendarSha256=f"sha256:{sha256(calendar_bytes).hexdigest()}",
                    calendarSessions=[day.isoformat() for day in dates],
                    corporateActions=corporate.evidence, signalAdjustmentSource=prepared.signal_adjustment_source,
                    indicatorSources=[dict(provider=series.provider, indicatorId=series.indicator_id,
                        responseSha256=series.response_sha256,
                        fields=[asdict(value) for value in series.points[0].values])
                        for series in prepared.indicator_series if series.points],
                    entrySignals=[asdict(fact) if fact else None for fact in prepared.entry_timeline],
                    exitSignals=[asdict(fact) if fact else None for fact in prepared.exit_timeline])
    source_json = json.dumps(evidence, default=str, sort_keys=True)
    source_hash = sha256(source_json.encode()).hexdigest()
    assumptions = {field.name: str(getattr(execution, field.name)) for field in fields(execution)}
    assumptions["entry_signal_validity_policy"] = ENTRY_SIGNAL_VALIDITY_POLICY
    if runtime_evidence is not None:
        assumptions.update({
            key: runtime_evidence[key]
            for key in (
                "opening_auction_policy", "benchmark_policy", "corporate_action_policy",
                "dividend_tax_policy", "rights_issue_policy",
            )
            if key in runtime_evidence
        })
    manifest = dict(strategy_hash=canonical_hash(strategy.model_dump(mode="json")),
        catalog_hash="not_pinned_in_mx_profile", code_revision="local_worktree_unreleased",
        engine_version="daily_share_ledger", assumptions=assumptions,
        data_snapshot=dict(snapshot_id=f"mx-share:{source_hash}", checksum=f"sha256:{source_hash}", schema_version="mx-share-input.v1"))
    if runtime_evidence is not None:
        manifest.update({key: runtime_evidence[key] for key in ("catalog_hash", "code_revision", "engine_version")})
    report = build_result_bundle(result, run_id=str(run_id), period_start=strategy.backtest.start,
        period_end=strategy.backtest.end, manifest=manifest,
        robustness=run_execution_robustness(request, base_result=result).as_dict() if config.run_robustness else None)
    # This legacy MX caller does not carry a pinned catalog/release manifest.
    # Retain the actual source/ledger below, without claiming release evidence.
    if runtime_evidence is None:
        report["summary"].pop("runEvidence")
    report["summary"]["warnings"].append("本地股份账本接线：仓位风控使用原始价格及来源除权因子重算锚点；未完成真实供应商与前端验收。")
    if any("announcement_date_next_trading_session.v1" in (value.source_parameters or "")
           for series in prepared.indicator_series if series.points for value in series.points[0].values):
        report["summary"]["warnings"].append(
            "报告期财务指标从公告后的交易日起使用；采用当前取得的报告版本，"
            "较晚更新可能包含修订，不能视为严格未修订的历史版本。")
    report["audit"]["pricePlanLedger"] = json.dumps(dict(sourceId=f"mx-share:{source_hash}", sourceEvidence=evidence,
        portfolio=asdict(result.final_portfolio)), default=str)
    from ashare_lab.application.execution_feedback import untriggered_combination_note
    note = untriggered_combination_note(evidence["entrySignals"],
        [day for day in dates if strategy.backtest.start <= day <= strategy.backtest.end], report["activities"])
    if note is not None:
        report["summary"]["executionNote"] = note
    bundle = BacktestResultBundle.model_validate(report)
    bundle.audit.result_hash = calculate_result_bundle_hash(bundle.model_dump(mode="json", by_alias=True))
    return bundle
