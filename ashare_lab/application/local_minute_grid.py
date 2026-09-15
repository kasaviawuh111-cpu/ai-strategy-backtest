"""Server-owned local minute data selection; never fall back silently to daily."""
import json
import re
from hashlib import sha256
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo
from ashare_lab.application.corporate_action_timeline import TimelineCorporateActionApplier
from ashare_lab.application.minute_price_rebase import MinutePriceRebase

from ashare_lab.adapters.market_data.eastmoney_minute_snapshot import (
    load_eastmoney_minute_snapshot, SNAPSHOT_SCHEMA_VERSION, EastmoneyMinuteSnapshotError,
)
from ashare_lab.adapters.market_data.mx_daily_history import MxDailyHistory
from ashare_lab.adapters.market_data.external_minute_parquet import ExternalMinuteParquet
from ashare_lab.application.minute_grid_plan import execute_minute_grid
from ashare_lab.application.minute_conditional_orders import execute_minute_conditions
from ashare_lab.application.mx_minute_replay import prepare_mx_minute_replay
from ashare_lab.application.minute_replay_input import MinuteReplayDataError
from ashare_lab.domain.execution.fees import AshareExchange
from ashare_lab.domain.strategy import StrategySpec, HybridExecutionPolicy, ComposedExecutionPolicy
from ashare_lab.domain.strategy.price_plans import GridPlan, ConditionalPlan, ScheduledPlan


@dataclass(frozen=True)
class PricePlanCorporateInputs:
    applier: TimelineCorporateActionApplier
    price_rebases: tuple[MinutePriceRebase, ...]
    evidence: dict


@dataclass(frozen=True)
class LocalMinuteGrid:
    snapshot_root: Path
    calendar_path: Path
    corporate_loader: Callable[[StrategySpec, MxDailyHistory], PricePlanCorporateInputs] | None = None
    minute_acquirer: Callable[[str, tuple[date, ...]], tuple[Path, ...]] | None = None
    external_minute_root: Path | None = None

    def load_calendar(self):
        if not self.calendar_path.is_file():
            raise MinuteReplayDataError("market_calendar_source_missing")
        try:
            calendar_bytes = self.calendar_path.read_bytes()
            calendar = json.loads(calendar_bytes)
        except (OSError, ValueError) as exc:
            raise MinuteReplayDataError("market_calendar_unreadable") from exc
        if (not isinstance(calendar, dict) or calendar.get("schemaVersion") != "market-calendar.v1"
                or not isinstance(calendar.get("provider"), str) or not calendar["provider"].strip()
                or not isinstance(calendar.get("sourceSha256"), str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", calendar["sourceSha256"])):
            raise MinuteReplayDataError("market_calendar_provenance_missing")
        try:
            if not isinstance(calendar["sessions"], list):
                raise ValueError("sessions must be a list")
            dates = tuple(date.fromisoformat(day) for day in calendar["sessions"])
            if not dates or any(a >= b for a, b in zip(dates, dates[1:])):
                raise ValueError("sessions must be nonempty and strictly ordered")
        except (ValueError, TypeError, KeyError) as exc:
            raise MinuteReplayDataError("invalid_market_calendar") from exc
        return dates, calendar, calendar_bytes

    def execute(self, strategy: StrategySpec, history: MxDailyHistory, *, signal_input=None, config=None,
                corporate_inputs=None):
        plan = strategy.trading_plan
        independent = getattr(strategy, 'independent_plans', None)
        hybrid = isinstance(getattr(strategy, "execution", None), HybridExecutionPolicy)
        composed = isinstance(getattr(strategy, 'execution', None), ComposedExecutionPolicy) and independent is None
        if composed:
            from ashare_lab.application.composed_execution import validate_composed_route
            validate_composed_route(strategy)
        if (hybrid or composed) and (signal_input is None or config is None or corporate_inputs is None):
            raise MinuteReplayDataError("hybrid_signal_inputs_missing")
        if not hybrid and independent is None and not isinstance(plan, (GridPlan, ConditionalPlan, ScheduledPlan)):
            raise MinuteReplayDataError("minute_strategy_adapter_missing")
        dates, calendar, calendar_bytes = self.load_calendar()
        start, end = strategy.backtest.start, strategy.backtest.end
        corporate = (corporate_inputs if corporate_inputs is not None else
                     self.corporate_loader(strategy, history) if self.corporate_loader else None)
        if independent is not None and (corporate is None or config is None):
            raise MinuteReplayDataError('independent_plan_corporate_inputs_missing')
        action_options = dict(corporate_actions=corporate.applier, price_rebases=corporate.price_rebases) if corporate else {}
        if isinstance(plan, ScheduledPlan) and not plan.parameters.exit_rules and not composed:
            from ashare_lab.application.daily_scheduled_plan import execute_sourced_daily_schedule
            return execute_sourced_daily_schedule(
                strategy, history, calendar_input=(dates, calendar, calendar_bytes),
                corporate=corporate, config=config)
        from ashare_lab.domain.market_data import TradingStatus
        requested_start = start
        if independent is not None or composed or isinstance(plan, ScheduledPlan) and plan.parameters.exit_rules:
            # Load and reconcile a real completed session solely for first-bar
            # capacity. It must not become part of the user's trading period.
            previous = [row.session_date for row in history.rows if row.session_date < start
                        and row.trading_status is TradingStatus.TRADING]
            if previous:
                start = max(previous)
        range_rows = [row for row in history.rows if start <= row.session_date <= end]
        all_nontrading = bool(range_rows) and all(row.trading_status is not TradingStatus.TRADING for row in range_rows)
        minute_sources = []
        if all_nontrading:
            # A complete, source-marked suspension needs no minute snapshot.
            # The common preparation below still checks every calendar date.
            controls_json = json.dumps([asdict(row) for row in range_rows], default=str, sort_keys=True, separators=(",", ":"))
            snapshot_id = "mx-nontrading:" + sha256(controls_json.encode()).hexdigest()
            minutes = ()
        else:
            candidates = []
            for manifest_path in self.snapshot_root.glob("*/snapshot_manifest.json"):
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if (manifest.get("schemaVersion") != SNAPSHOT_SCHEMA_VERSION
                            or manifest.get("symbol") != history.instrument_id):
                        continue
                    lo, hi = map(date.fromisoformat, manifest["requestedRange"])
                    if lo <= end and start <= hi:
                        candidates.append((-datetime.fromisoformat(manifest["capturedAt"]).timestamp(), (hi - lo).days,
                                           manifest["snapshotId"], manifest_path.parent))
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    continue
            # Select whole sessions from the newest immutable snapshot. Never
            # splice individual bars from different revisions of the same day.
            selected_days = {}
            if self.external_minute_root is not None:
                try:
                    external = ExternalMinuteParquet(self.external_minute_root).load(
                        history.instrument_id, start, end,
                        confirmed_opening_prices={row.session_date: row.raw_open for row in range_rows})
                except (OSError, ValueError, ArithmeticError, EastmoneyMinuteSnapshotError) as exc:
                    raise MinuteReplayDataError('external_minute_file_invalid') from exc
                if external is not None:
                    for bar in external.bars:
                        day = bar.bar_end_at.date()
                        selected_days.setdefault(day, []).append(bar)
                    minute_sources.append(external.evidence)
            def add_snapshot(source_id, path):
                try:
                    source_bars = load_eastmoney_minute_snapshot(path)
                except (EastmoneyMinuteSnapshotError, OSError) as exc:
                    raise MinuteReplayDataError("minute_snapshot_unreadable_or_invalid") from exc
                grouped = {}
                for bar in source_bars:
                    day = bar.bar_end_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
                    if start <= day <= end:
                        grouped.setdefault(day, []).append(bar)
                used_days = sorted(set(grouped) - set(selected_days))
                for day in used_days:
                    selected_days[day] = grouped[day]
                if used_days:
                    minute_sources.append(dict(snapshotId=source_id, sessions=[day.isoformat() for day in used_days]))
            for _, _, source_id, path in sorted(candidates):
                add_snapshot(source_id, path)
            needed = tuple(sorted({row.session_date for row in range_rows
                if row.trading_status is TradingStatus.TRADING} - set(selected_days)))
            if needed and self.minute_acquirer:
                for path in self.minute_acquirer(history.instrument_id, needed):
                    manifest = json.loads((path / "snapshot_manifest.json").read_text(encoding="utf-8"))
                    if manifest.get("symbol") != history.instrument_id:
                        raise MinuteReplayDataError("market_data_instrument_mismatch")
                    add_snapshot(manifest["snapshotId"], path)
            minutes = tuple(bar for day in sorted(selected_days) for bar in selected_days[day])
            if not minute_sources:
                raise MinuteReplayDataError("minute_snapshot_range_unavailable")
            snapshot_id = (minute_sources[0]["snapshotId"] if len(minute_sources) == 1 else
                ("minute-source-set:" if any(s.get('provider') == 'user_supplied_stock_1min_parquet'
                                             for s in minute_sources) else "eastmoney-minute-set:")
                + sha256(json.dumps(minute_sources, sort_keys=True,
                    separators=(",", ":")).encode()).hexdigest())
        prepared = prepare_mx_minute_replay(history=history, minutes=minutes, start=start, end=end,
                                            market_calendar=dates, eastmoney_whole_unit_research=True,
                                            external_stock_1min_research=any(s.get('provider') ==
                                                'user_supplied_stock_1min_parquet' for s in minute_sources))
        if start < requested_start:
            prior = [bar for bar in prepared.bars if bar.session.session_date < requested_start]
            prepared = replace(prepared,
                bars=tuple(bar for bar in prepared.bars if bar.session.session_date >= requested_start),
                nontrading_closes=tuple(item for item in prepared.nontrading_closes
                                       if item[0].session_date >= requested_start),
                preceding_bar=prior[-1] if prior else None)
            start = requested_start
        signal_options = {}
        if composed:
            from ashare_lab.application.daily_signal_execution import prepare_daily_signal_intents
            from ashare_lab.application.composed_execution import composed_protection, composed_holding_sessions, composed_daily_position_risk
            signal_options = dict(daily_signals=prepare_daily_signal_intents(
                strategy=strategy, signal_input=signal_input, market_sessions=prepared.market_sessions,
                cash_fraction=config.allocation_ratio), protection=composed_protection(strategy),
                holding_sessions=composed_holding_sessions(strategy), holding_anchor="first_entry_fill",
                daily_position_risk=composed_daily_position_risk(strategy, history, prepared.market_sessions))
        if independent is not None:
            from ashare_lab.application.independent_plan_execution import execute_independent_plans
            result = execute_independent_plans(independent, prepared=prepared,
                exchange=AshareExchange(history.instrument_id.rsplit('.', 1)[1]),
                start=start, end=end, execution_config=config, **action_options)
        elif hybrid:
            from ashare_lab.application.daily_signal_execution import execute_hybrid_signals
            result = execute_hybrid_signals(strategy=strategy, signal_input=signal_input,
                prepared=prepared, config=config,
                exchange=AshareExchange(history.instrument_id.rsplit(".", 1)[1]), **action_options)
        elif isinstance(plan, ScheduledPlan):
            from ashare_lab.application.minute_scheduled_plan import execute_minute_schedule
            result = execute_minute_schedule(plan.parameters, prepared=prepared,
                exchange=AshareExchange(history.instrument_id.rsplit(".", 1)[1]), start=start, end=end,
                exit_rules=plan.parameters.exit_rules, execution_config=config,
                **signal_options, **action_options)
        else:
            execute = execute_minute_grid if isinstance(plan, GridPlan) else execute_minute_conditions
            if composed and isinstance(plan, GridPlan):
                signal_options["active_sides"] = ("sell",) if strategy.entry is not None else ("buy",)
            result = execute(plan.parameters, prepared=prepared,
                             exchange=AshareExchange(history.instrument_id.rsplit(".", 1)[1]),
                             execution_config=config, **signal_options, **action_options)
        # Save the actual controls/calendar used by this run, not just a mutable
        # local pathname or a provider hash supplied without its session list.
        controls = [asdict(row) for row in history.rows if start <= row.session_date <= end]
        controls_json = json.dumps(controls, default=str, sort_keys=True, separators=(",", ":"))
        evidence = dict(
            capacityPredecessor=asdict(prepared.preceding_bar) if prepared.preceding_bar else None,
            corporateActions=corporate.evidence if corporate else dict(status="not_connected"),
            minute=dict(snapshotId=snapshot_id,
                        snapshots=minute_sources,
                        source=("mx_daily_nontrading_sessions" if all_nontrading else
                                "external_parquet_and_optional_snapshots" if any(
                                    s.get('provider') == 'user_supplied_stock_1min_parquet'
                                    for s in minute_sources) else "eastmoney_minute_snapshot"),
                        schemaVersion=(None if all_nontrading else 'external-stock-1min.v1' if any(
                            s.get('provider') == 'user_supplied_stock_1min_parquet'
                            for s in minute_sources) else SNAPSHOT_SCHEMA_VERSION),
                        status="not_required_no_trading_sessions" if all_nontrading else "loaded"),
            calendar=dict(provider=calendar["provider"], sourceSha256=calendar["sourceSha256"],
                          fileSha256=sha256(calendar_bytes).hexdigest(), sessions=calendar["sessions"]),
            dailyControls=dict(provider=history.provider, retrievedAt=history.retrieved_at,
                               cacheStatus=history.cache_status or "unknown", instrumentId=history.instrument_id,
                               sha256=sha256(controls_json.encode()).hexdigest(), rows=controls,
                               queries=[item.query for item in history.query_evidence]),
        )
        if config is not None and not hybrid:
            evidence["executionConfig"] = asdict(config)
        if independent is not None:
            evidence['independentPlans'] = independent.model_dump(mode='json')
        if hybrid or composed:
            evidence["dailySignals"] = dict(
                entry=[asdict(fact) if fact else None for fact in signal_input.entry_timeline],
                exit=[asdict(fact) if fact else None for fact in signal_input.exit_timeline],
                adjustmentSource=signal_input.signal_adjustment_source,
                executionConfig=asdict(config))
        return result, snapshot_id, prepared.reconciliation, evidence
