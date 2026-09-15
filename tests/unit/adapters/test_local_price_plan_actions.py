import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter

from ashare_lab.adapters.market_data.local_price_plan_actions import (
    LocalPricePlanActions,
    PreparingPricePlanActions,
    price_plan_export,
    source_backed_price_rebases,
)
from ashare_lab.application.minute_price_rebase import MinutePriceRebase
from ashare_lab.application.minute_replay_input import MinuteReplayDataError
from ashare_lab.domain.market_data import CorporateAction, CorporateActionKind
from ashare_lab.settings import AppSettings
from tests.unit.portfolio.test_corporate_actions import _action, _clock, INSTRUMENT


def request():
    return (SimpleNamespace(instrument=SimpleNamespace(symbol="300059.SZ"),
            backtest=SimpleNamespace(start=date(2025, 1, 2), end=date(2025, 1, 6))),
            SimpleNamespace(instrument_id="300059.SZ"))


def payload():
    action = _action(CorporateActionKind.CASH_DIVIDEND, cash_per_share=Decimal(".5"), pay_date=date(2025, 1, 6))
    rebase = MinutePriceRebase(INSTRUMENT, date(2025, 1, 3), Decimal(".95"), _clock(date(2025, 1, 3), 9), "a" * 64)
    return dict(schemaVersion="price-plan-corporate-actions.v1", instrumentId="300059.SZ",
                source=dict(provider="fixture-not-real-vendor", responseSha256="a" * 64),
                coverage=["2025-01-02", "2025-01-06"],
                actions=TypeAdapter(list[CorporateAction]).dump_python([action], mode="json"),
                priceRebases=TypeAdapter(list[MinutePriceRebase]).dump_python([rebase], mode="json"))


def test_local_export_roundtrip_and_default_disabled(tmp_path):
    assert AppSettings(_env_file=None).price_plan_corporate_action_root is None
    data = payload()
    path = tmp_path / "300059.SZ.json"
    path.write_text(json.dumps(data))
    loaded = LocalPricePlanActions(tmp_path)(*request())
    assert loaded.applier.actions[0].gross_cash_per_share == Decimal(".5")
    assert loaded.price_rebases[0].factor == Decimal(".95")
    assert loaded.evidence["status"] == "source_export_loaded"
    assert len(loaded.evidence["fileSha256"]) == 64


def test_preparing_actions_reuses_valid_export_and_preserves_old_file_on_failure(tmp_path):
    calls = []
    def acquire(strategy, history, start):
        calls.append(start)
        return payload()
    loader = PreparingPricePlanActions(tmp_path, acquire=acquire)
    first = loader(*request())
    assert loader(*request()).evidence == first.evidence
    assert calls == [date(2025, 1, 2)]
    path = tmp_path / "intervals/300059.SZ/2025-01-02_2025-01-06/300059.SZ.json"
    original = path.read_bytes()
    # Acquisition that fails the requested warmup coverage cannot replace cache.
    with pytest.raises(MinuteReplayDataError, match="coverage_insufficient"):
        loader(*request(), required_start=date(2024, 12, 1))
    assert path.read_bytes() == original
    assert calls[-1] == date(2024, 12, 1)
    assert not list(tmp_path.rglob(".actions-*"))
    path.write_text("not json")
    with pytest.raises(MinuteReplayDataError, match="source_invalid"):
        loader(*request())
    assert len(calls) == 2


def test_preparing_actions_pins_interleaved_intervals_and_reuses_after_restart(tmp_path):
    calls = []
    def acquire(strategy, history, start):
        calls.append(start)
        data = payload()
        data["coverage"][0] = start.isoformat()
        data["source"]["responseSha256"] = ("b" if start.year == 2024 else "a") * 64
        return data
    loader = PreparingPricePlanActions(tmp_path, acquire=acquire)
    first = loader(*request())
    wider = loader(*request(), required_start=date(2024, 12, 1))
    assert first.evidence["fileSha256"] != wider.evidence["fileSha256"]
    assert loader(*request()).evidence == first.evidence
    restarted = PreparingPricePlanActions(tmp_path, acquire=acquire)
    assert restarted(*request()).evidence == first.evidence
    assert restarted(*request(), required_start=date(2024, 12, 1)).evidence == wider.evidence
    assert len(calls) == 2


def test_preparing_actions_does_not_turn_acquisition_failure_into_empty_actions(tmp_path):
    def unavailable(*args):
        raise RuntimeError("source unavailable")
    loader = PreparingPricePlanActions(tmp_path, acquire=unavailable)
    with pytest.raises(RuntimeError, match="source unavailable"):
        loader(*request())
    assert not (tmp_path / "300059.SZ.json").exists()


def test_prepared_supplier_export_requires_factors_then_loads_without_field_loss(tmp_path):
    from datetime import datetime, UTC
    data = payload()
    actions = TypeAdapter(tuple[CorporateAction, ...]).validate_python(data["actions"])
    result = SimpleNamespace(instrument_id=INSTRUMENT, start=date(2025, 1, 2),
        end=date(2025, 1, 6), captured_at=datetime.now(UTC), corporate_actions=actions,
        query_collections=(), coverage={"querySucceeded": True, "status": "complete_mixed_mode",
            "instrumentId": "300059.SZ", "start": "2025-01-02", "end": "2025-01-06",
            "provider": "fixture-not-real-vendor", "strictBlockers": []})
    with pytest.raises(MinuteReplayDataError, match="ex_date_factor_missing"):
        price_plan_export(result)
    exported = price_plan_export(result, data["priceRebases"])
    (tmp_path / "300059.SZ.json").write_text(json.dumps(exported))
    loaded = LocalPricePlanActions(tmp_path)(*request())
    assert loaded.applier.actions == actions
    assert loaded.price_rebases[0].factor == Decimal(".95")
    result.coverage["querySucceeded"] = False
    with pytest.raises(MinuteReplayDataError, match="coverage_insufficient"):
        price_plan_export(result, data["priceRebases"])


def test_source_backed_factor_uses_market_reference_with_structural_formula_bounds():
    from datetime import datetime, UTC
    action = _action(CorporateActionKind.CASH_DIVIDEND,
                     cash_per_share=Decimal(".5"), pay_date=date(2025, 1, 6))
    result = SimpleNamespace(
        instrument_id=INSTRUMENT, start=date(2025, 1, 2), end=date(2025, 1, 6),
        corporate_actions=(action,),
    )
    history = SimpleNamespace(
        instrument_id="300059.SZ",
        rows=(
            SimpleNamespace(session_date=date(2025, 1, 2), raw_close=Decimal("10"),
                            raw_preclose=Decimal("9.9")),
            SimpleNamespace(session_date=date(2025, 1, 3), raw_close=Decimal("9.6"),
                            raw_preclose=Decimal("9.5")),
        ),
        query_evidence=(SimpleNamespace(
            purpose="raw_prices", response_sha256="sha256:" + "b" * 64,
            retrieved_at=datetime.now(UTC)),),
    )
    rebases = source_backed_price_rebases(result, history)
    assert rebases[0].factor == Decimal(".95")
    assert rebases[0].available_at <= _clock(date(2025, 1, 3), 9, 30)
    # A smaller virtual cash dividend is valid for a differential
    # distribution; the actual market reference remains authoritative.
    history.rows[1].raw_preclose = Decimal("9.7")
    assert source_backed_price_rebases(result, history)[0].factor == Decimal(".97")
    # Outside both full-cash and zero-cash bounds indicates missing or
    # incompatible event terms and still fails closed.
    history.rows[1].raw_preclose = Decimal("10.2")
    with pytest.raises(MinuteReplayDataError, match="reference_price_mismatch"):
        source_backed_price_rebases(result, history)


def test_signal_warmup_requires_source_coverage_before_backtest_start(tmp_path):
    data = payload()
    path = tmp_path / "300059.SZ.json"
    path.write_text(json.dumps(data))
    loader = LocalPricePlanActions(tmp_path)
    with pytest.raises(MinuteReplayDataError, match="coverage_insufficient"):
        loader(*request(), required_start=date(2024, 12, 1))
    data["coverage"][0] = "2024-12-01"
    path.write_text(json.dumps(data))
    assert loader(*request(), required_start=date(2024, 12, 1)).price_rebases


def test_missing_file_is_not_an_empty_event_result(tmp_path):
    with pytest.raises(MinuteReplayDataError, match="source_missing"):
        LocalPricePlanActions(tmp_path)(*request())


@pytest.mark.parametrize("change", ["coverage", "factor", "identity"])
def test_invalid_export_is_not_executed(tmp_path, change):
    data = payload()
    if change == "coverage":
        data["coverage"][1] = "2025-01-03"
    elif change == "factor":
        data["priceRebases"] = []
    else:
        data["instrumentId"] = "600519.SH"
    (tmp_path / "300059.SZ.json").write_text(json.dumps(data))
    with pytest.raises(MinuteReplayDataError):
        LocalPricePlanActions(tmp_path)(*request())
