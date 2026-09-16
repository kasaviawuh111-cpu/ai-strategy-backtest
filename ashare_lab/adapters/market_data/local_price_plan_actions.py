"""Read a locally pinned, source-described corporate-action export.

This does not fetch or certify vendor data. The exporting adapter must supply
the covered interval and original response hash, including for empty results.
"""
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from threading import RLock
from typing import Callable
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter

from ashare_lab.application.corporate_action_timeline import TimelineCorporateActionApplier
from ashare_lab.application.local_minute_grid import PricePlanCorporateInputs
from ashare_lab.application.minute_price_rebase import MinutePriceRebase
from ashare_lab.application.minute_replay_input import MinuteReplayDataError
from ashare_lab.domain.market_data import CorporateAction
from ashare_lab.domain.shared import InstrumentId


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_PRICE_REBASE_FORMULA_VERSION = "cn-a-ex-right-reference.v1"


class CorporateActionBoundaryPriorCloseMissing(MinuteReplayDataError):
    """An ex-date is present as the first row, without its preceding session."""

    def __init__(self, ex_date: date):
        super().__init__("corporate_action_prior_close_missing")
        self.ex_date = ex_date


def source_backed_price_rebases(result, history):
    """Build ex-date factors from known action terms and the prior raw close.

    The economic formula is point-in-time: every action revision must already
    be knowable before the ex-date open and the preceding session close is the
    only market price input.  The ex-date ``raw_preclose`` is the market's
    actual reference price.  It normally equals the standard formula within a
    tick; for differential cash distributions it may use a smaller virtual
    cash dividend.  Such a deviation is accepted only when the published
    reference stays between the full-cash and zero-cash formula bounds.  Share
    or rights terms therefore cannot be hidden by a broad numeric tolerance.
    """
    symbol = result.instrument_id.value
    if history.instrument_id != symbol:
        raise MinuteReplayDataError("corporate_action_price_history_instrument_mismatch")
    rows = history.rows
    by_day = {row.session_date: (index, row) for index, row in enumerate(rows)}
    evidence = [item for item in history.query_evidence if item.purpose == "raw_prices"]
    if not evidence:
        raise MinuteReplayDataError("corporate_action_raw_price_evidence_missing")
    grouped = {}
    for action in result.corporate_actions:
        if result.start <= action.ex_date <= result.end:
            grouped.setdefault(action.ex_date, []).append(action)
    rebases = []
    for ex_date, actions in sorted(grouped.items()):
        located = by_day.get(ex_date)
        if located is None:
            raise MinuteReplayDataError("corporate_action_prior_close_missing")
        if located[0] == 0:
            raise CorporateActionBoundaryPriorCloseMissing(ex_date)
        index, ex_row = located
        previous = rows[index - 1]
        ex_open = datetime.combine(ex_date, time(9, 30), tzinfo=_SHANGHAI)
        available = []
        cash = Decimal(0)
        share_multiplier = Decimal(1)
        rights_ratio = Decimal(0)
        rights_value = Decimal(0)
        for action in actions:
            known_at = action.available_at
            if known_at is None or known_at > ex_open:
                raise MinuteReplayDataError("corporate_action_terms_not_known_before_ex_date")
            available.append(known_at)
            if action.gross_cash_per_share is not None:
                cash += action.gross_cash_per_share
            if action.share_multiplier is not None:
                share_multiplier *= action.share_multiplier
            if action.rights_ratio is not None:
                if action.rights_subscription_price is None:
                    raise MinuteReplayDataError("corporate_action_rights_terms_incomplete")
                rights_ratio += action.rights_ratio
                rights_value += action.rights_ratio * action.rights_subscription_price
        numerator = previous.raw_close - cash + rights_value
        denominator = share_multiplier + rights_ratio
        if numerator <= 0 or denominator <= 0:
            raise MinuteReplayDataError("corporate_action_price_rebase_invalid")
        standard_reference = numerator / denominator
        cashless_reference = (previous.raw_close + rights_value) / denominator
        provider_reference = ex_row.raw_preclose
        tick = Decimal("0.01")
        # A differential distribution can reduce the virtual cash dividend
        # used for the exchange reference price, but it cannot reverse the
        # event or exceed the no-cash/full-cash bounds without separate source
        # terms.  Keep that case closed instead of widening an arbitrary error
        # tolerance.
        if not (
            min(standard_reference, cashless_reference) - tick
            <= provider_reference
            <= max(standard_reference, cashless_reference) + tick
        ):
            raise MinuteReplayDataError("corporate_action_reference_price_mismatch")
        known_at = max((*available,
            datetime.combine(previous.session_date, time(15), tzinfo=_SHANGHAI)))
        source = {
            "formulaVersion": _PRICE_REBASE_FORMULA_VERSION,
            "instrumentId": symbol,
            "exDate": ex_date.isoformat(),
            "previousSession": previous.session_date.isoformat(),
            "previousRawClose": str(previous.raw_close),
            "standardFormulaReference": str(standard_reference),
            "providerReferencePreclose": str(provider_reference),
            "factorReferenceBasis": "provider_ex_date_raw_preclose",
            "actionResponseHashes": sorted(action.raw_response_sha256 for action in actions),
            "rawPriceEvidence": sorted(item.response_sha256 for item in evidence),
        }
        encoded = json.dumps(source, sort_keys=True, separators=(",", ":"))
        rebases.append(MinutePriceRebase(
            instrument_id=InstrumentId(symbol), ex_date=ex_date,
            factor=provider_reference / previous.raw_close, available_at=known_at,
            source_sha256=sha256(encoded.encode()).hexdigest(),
        ))
    return tuple(rebases)


def price_plan_export(result, price_rebases=()):
    """Bridge a prepared supplier result to the existing execution loader.

    Factors must come from a separately sourced input, never from a default
    one or an inferred cash/price ratio. This does not widen supplier coverage.
    """
    symbol = result.instrument_id.value
    coverage = result.coverage
    if (coverage.get("querySucceeded") is not True
            or coverage.get("status") != "complete_mixed_mode"
            or coverage.get("strictBlockers")
            or coverage.get("instrumentId") != symbol
            or coverage.get("start") != result.start.isoformat()
            or coverage.get("end") != result.end.isoformat()):
        raise MinuteReplayDataError("corporate_action_coverage_insufficient")
    rebases = TypeAdapter(tuple[MinutePriceRebase, ...]).validate_python(price_rebases)
    actions = result.corporate_actions
    if (any(action.instrument_id.value != symbol for action in actions)
            or any(item.instrument_id.value != symbol for item in rebases)
            or len({item.ex_date for item in rebases}) != len(rebases)):
        raise MinuteReplayDataError("corporate_action_instrument_or_factor_invalid")
    required = {action.ex_date for action in actions
                if result.start <= action.ex_date <= result.end}
    if required - {item.ex_date for item in rebases}:
        raise MinuteReplayDataError("corporate_action_ex_date_factor_missing")
    evidence = {"coverage": dict(coverage),
                "queryCollections": [item.as_dict() for item in result.query_collections]}
    encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return dict(
        schemaVersion="price-plan-corporate-actions.v1", instrumentId=symbol,
        coverage=[result.start.isoformat(), result.end.isoformat()],
        source=dict(provider=coverage["provider"], responseSha256=sha256(encoded.encode()).hexdigest(),
                    capturedAt=result.captured_at.isoformat(), evidence=evidence),
        actions=TypeAdapter(tuple[CorporateAction, ...]).dump_python(actions, mode="json"),
        priceRebases=TypeAdapter(tuple[MinutePriceRebase, ...]).dump_python(rebases, mode="json"),
    )


@dataclass(frozen=True)
class LocalPricePlanActions:
    root: Path

    def __call__(self, strategy, history, *, required_start: date | None = None):
        symbol = strategy.instrument.symbol
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol) or history.instrument_id != symbol:
            raise MinuteReplayDataError("corporate_action_instrument_mismatch")
        path = self.root / f"{symbol}.json"
        try:
            raw = path.read_bytes()
            data = json.loads(raw)
            if data["schemaVersion"] != "price-plan-corporate-actions.v1" or data["instrumentId"] != symbol:
                raise ValueError("schema or instrument mismatch")
            source = data["source"]
            if not isinstance(source["provider"], str) or not source["provider"].strip():
                raise ValueError("provider missing")
            if not re.fullmatch(r"[0-9a-f]{64}", source["responseSha256"]):
                raise ValueError("source hash missing")
            lo, hi = (date.fromisoformat(value) for value in data["coverage"])
            start = min(strategy.backtest.start, required_start) if required_start is not None else strategy.backtest.start
            if not lo <= start <= strategy.backtest.end <= hi:
                raise MinuteReplayDataError("corporate_action_coverage_insufficient")
            actions = TypeAdapter(tuple[CorporateAction, ...]).validate_python(data["actions"])
            rebases = TypeAdapter(tuple[MinutePriceRebase, ...]).validate_python(data["priceRebases"])
            if any(action.instrument_id.value != symbol for action in actions) or any(item.instrument_id.value != symbol for item in rebases):
                raise ValueError("mixed instruments")
            if len({item.ex_date for item in rebases}) != len(rebases):
                raise ValueError("duplicate rebase dates")
            required = {action.ex_date for action in actions if start <= action.ex_date <= strategy.backtest.end}
            if required - {item.ex_date for item in rebases}:
                raise ValueError("missing ex-date factor")
            applier = TimelineCorporateActionApplier(actions)
        except FileNotFoundError as exc:
            raise MinuteReplayDataError("corporate_action_source_missing") from exc
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            if isinstance(exc, MinuteReplayDataError):
                raise
            raise MinuteReplayDataError("corporate_action_source_invalid") from exc
        return PricePlanCorporateInputs(applier, rebases, dict(
            status="source_export_loaded" if actions else "source_export_empty",
            source=source, coverage=data["coverage"], fileSha256=sha256(raw).hexdigest(),
            actions=data["actions"], priceRebases=data["priceRebases"],
        ))


def acquire_price_plan_actions(strategy, history, start):
    """Reuse the supplier adapter and raw-price factor verification, not a new feed."""
    from ashare_lab.adapters.market_data.eastmoney_corporate_actions import (
        EastmoneyCorporateActionReferenceAdapter, EastmoneyCorporateActionError,
        EastmoneyCorporateActionValidationError,
    )
    try:
        with EastmoneyCorporateActionReferenceAdapter() as adapter:
            result = adapter.prepare(symbol=strategy.instrument.symbol,
                                     start=start, end=strategy.backtest.end)
    except EastmoneyCorporateActionValidationError as exc:
        raise MinuteReplayDataError("corporate_action_source_invalid") from exc
    except EastmoneyCorporateActionError as exc:
        raise MinuteReplayDataError("corporate_action_acquisition_unavailable") from exc
    rebases = source_backed_price_rebases(result, history) if result.corporate_actions else ()
    return price_plan_export(result, rebases)


@dataclass
class PreparingPricePlanActions:
    """Fill missing coverage atomically; never overwrite a valid cache on failure.

    Call from a worker thread: supplier acquisition is synchronous. Invalid
    existing evidence is not silently replaced or treated as an empty result.
    """
    root: Path
    acquire: Callable = acquire_price_plan_actions
    _lock: object = field(default_factory=RLock, init=False, repr=False)

    def __call__(self, strategy, history, *, required_start: date | None = None):
        symbol = strategy.instrument.symbol
        if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", symbol) or history.instrument_id != symbol:
            raise MinuteReplayDataError("corporate_action_instrument_mismatch")
        start = min(strategy.backtest.start, required_start) if required_start else strategy.backtest.start
        interval_root = self.root / "intervals" / symbol / f"{start}_{strategy.backtest.end}"
        loader = LocalPricePlanActions(interval_root)
        with self._lock:
            try:
                return loader(strategy, history, required_start=required_start)
            except MinuteReplayDataError as exc:
                if str(exc) != "corporate_action_source_missing":
                    raise
            try:
                LocalPricePlanActions(self.root)(strategy, history, required_start=required_start)
                encoded = (self.root / f"{symbol}.json").read_bytes()
            except MinuteReplayDataError as exc:
                if str(exc) not in {"corporate_action_source_missing", "corporate_action_coverage_insufficient"}:
                    raise
                data = self.acquire(strategy, history, start)
                encoded = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
            interval_root.mkdir(parents=True, exist_ok=True)
            # Validate the newly acquired export before making it visible.
            # Different requested intervals never replace one another's input.
            with tempfile.TemporaryDirectory(prefix=".actions-", dir=interval_root) as staging:
                staged = Path(staging) / f"{symbol}.json"
                staged.write_bytes(encoded)
                prepared = LocalPricePlanActions(Path(staging))(
                    strategy, history, required_start=required_start,
                )
                os.replace(staged, interval_root / f"{symbol}.json")
            return prepared
