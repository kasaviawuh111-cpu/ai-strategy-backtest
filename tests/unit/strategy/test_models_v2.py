from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from ashare_lab.domain.instruments.models import AssetType, Exchange, InstrumentRef
from ashare_lab.domain.strategy.models_v2 import (
    AllConditionV2,
    AnyConditionV2,
    BacktestConfigV2,
    CatalogRefV2,
    ConditionGrounding,
    EventAttributePredicate,
    EventConditionV2,
    FinancialCondition,
    InterpretationCoverage,
    NotConditionV2,
    SourceSpan,
    StrategySpecV2,
    TechnicalConditionV2,
    canonical_hash_v2,
    canonical_json_v2,
    iter_condition_leaf_paths,
)


def _instrument(asset_type: AssetType = AssetType.STOCK) -> InstrumentRef:
    if asset_type is AssetType.ETF:
        return InstrumentRef(
            symbol="510300.SH",
            name="沪深300ETF",
            exchange=Exchange.SH,
            asset_type=asset_type,
            currency="CNY",
            listing_date=date(2012, 5, 28),
            delisting_date=None,
            tradable=True,
            data_source="choice.security_master",
        )
    return InstrumentRef(
        symbol="600519.SH",
        name="贵州茅台",
        exchange=Exchange.SH,
        asset_type=asset_type,
        currency="CNY",
        listing_date=date(2001, 8, 27),
        delisting_date=None,
        tradable=True,
        data_source="choice.security_master",
    )


def _technical(*, params: dict[str, int] | None = None) -> TechnicalConditionV2:
    return TechnicalConditionV2(
        indicator_id="indicator.macd",
        definition_version="1.0.0",
        params=params or {"fast": 12, "slow": 26, "signal": 9},
        trigger="golden_cross",
    )


def _coverage() -> InterpretationCoverage:
    return InterpretationCoverage(
        groundings=(
            ConditionGrounding(
                dsl_path="$.entry",
                source_start=0,
                source_end=8,
                source_text="MACD金叉买入",
            ),
            ConditionGrounding(
                dsl_path="$.exit",
                source_start=9,
                source_end=17,
                source_text="MACD死叉卖出",
            ),
        ),
        unmapped_material_spans=(),
    )


def _spec(
    *,
    instrument: InstrumentRef | None = None,
    entry: object | None = None,
    exit: object | None = None,
) -> StrategySpecV2:
    return StrategySpecV2(
        catalog=CatalogRefV2(catalog_id="ashare.signal-catalog", release_version="2026.08"),
        instrument=instrument or _instrument(),
        entry=entry or _technical(),
        exit=exit
        or TechnicalConditionV2(
            indicator_id="indicator.macd",
            definition_version="1.0.0",
            params={"fast": 12, "slow": 26, "signal": 9},
            trigger="death_cross",
        ),
        interpretation_coverage=_coverage(),
        backtest=BacktestConfigV2(
            start=date(2021, 1, 4),
            end=date(2026, 8, 28),
            initial_cash_cny=1_000_000,
        ),
    )


def test_strategy_v2_accepts_confirmed_stock_and_etf_instruments() -> None:
    assert _spec().instrument.asset_type is AssetType.STOCK
    assert _spec(instrument=_instrument(AssetType.ETF)).instrument.asset_type is AssetType.ETF


def test_strategy_v2_cannot_accept_index_or_unknown_top_level_fields() -> None:
    payload = _spec().model_dump(mode="json")
    payload["instrument"]["asset_type"] = "INDEX"
    with pytest.raises(ValidationError):
        StrategySpecV2.model_validate(payload)

    for forbidden in ("executable", "status", "signal", "order"):
        with pytest.raises(ValidationError, match="Extra inputs"):
            StrategySpecV2.model_validate({**_spec().model_dump(mode="json"), forbidden: True})


def test_strategy_v2_supports_strict_discriminated_condition_shapes() -> None:
    financial = FinancialCondition(
        metric_id="financial.net_profit",
        definition_version="1.0.0",
        report_type="annual",
        period_basis="full_year",
        statement_scope="consolidated",
        revision_policy="as_known_at_signal",
        comparator="gte",
        value=100_000_000,
        unit="CNY",
    )
    event = EventConditionV2(
        event_code="event.financial_results.annual_report",
        definition_version="1.0.0",
        trigger="became_available",
        occurrence_scope="initial_validated_publication",
        availability_mode="validated_first_available_at",
        attributes=(
            EventAttributePredicate(
                attribute_id="report_type",
                comparator="eq",
                value="annual",
                value_type="text",
            ),
        ),
    )
    entry = AllConditionV2(
        children=(
            _technical(),
            financial,
            NotConditionV2(
                child=AnyConditionV2(
                    children=(
                        event,
                        TechnicalConditionV2(
                            indicator_id="indicator.rsi",
                            definition_version="1.0.0",
                            params={"period": 14},
                            trigger="above",
                            value=70,
                        ),
                    )
                )
            ),
        )
    )

    spec = _spec(entry=entry)

    assert spec.entry.type == "all"
    assert [child.type for child in spec.entry.children] == ["technical", "financial", "not"]
    assert spec.entry.children[2].child.type == "any"


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    [
        ("report_type", "monthly"),
        ("period_basis", "latest"),
        ("statement_scope", "unknown"),
        ("revision_policy", "latest_revision"),
        ("comparator", "approximately"),
        ("unit", "RMB"),
    ],
)
def test_financial_condition_rejects_unknown_period_unit_and_semantics(
    field_name: str,
    bad_value: str,
) -> None:
    payload = FinancialCondition(
        metric_id="financial.net_profit",
        definition_version="1.0.0",
        report_type="annual",
        period_basis="full_year",
        statement_scope="consolidated",
        revision_policy="as_known_at_signal",
        comparator="gte",
        value=100_000_000,
        unit="CNY",
    ).model_dump(mode="json")
    payload[field_name] = bad_value

    with pytest.raises(ValidationError):
        FinancialCondition.model_validate(payload)


def test_financial_and_technical_conditions_reject_unknown_fields_and_non_finite_values() -> None:
    financial = FinancialCondition(
        metric_id="financial.roe",
        definition_version="1.0.0",
        report_type="annual",
        period_basis="full_year",
        statement_scope="consolidated",
        revision_policy="as_known_at_signal",
        comparator="gte",
        value=15,
        unit="PERCENT",
    )
    with pytest.raises(ValidationError, match="Extra inputs"):
        FinancialCondition.model_validate({**financial.model_dump(), "period": "2025"})
    with pytest.raises(ValidationError):
        FinancialCondition.model_validate({**financial.model_dump(), "value": float("nan")})
    with pytest.raises(ValidationError):
        FinancialCondition.model_validate({**financial.model_dump(), "value": "15"})
    with pytest.raises(ValidationError):
        TechnicalConditionV2.model_validate({**_technical().model_dump(), "value": float("inf")})


def test_event_condition_uses_typed_bounded_attribute_predicates() -> None:
    event = EventConditionV2(
        event_code="event.financial_results.annual_report",
        definition_version="1.0.0",
        trigger="became_available",
        occurrence_scope="initial_validated_publication",
        availability_mode="validated_first_available_at",
        attributes=(
            EventAttributePredicate(
                attribute_id="report_type",
                comparator="eq",
                value="annual",
                value_type="text",
            ),
        ),
    )
    assert event.attributes[0].attribute_id == "report_type"

    with pytest.raises(ValidationError):
        EventConditionV2.model_validate(
            {
                **event.model_dump(mode="json"),
                "attributes": [
                    {
                        "attribute_id": "report_type",
                        "comparator": "contains_sql",
                        "value": "annual",
                        "value_type": "text",
                    }
                ],
            }
        )
    with pytest.raises(ValidationError):
        EventConditionV2.model_validate(
            {**event.model_dump(mode="json"), "attributes": event.model_dump()["attributes"] * 9}
        )


def test_condition_tree_is_bounded_by_depth_and_node_count() -> None:
    deep: object = _technical()
    for _ in range(8):
        deep = NotConditionV2(child=deep)
    with pytest.raises(ValidationError, match="depth 8"):
        _spec(entry=deep)

    leaves = tuple(
        TechnicalConditionV2(
            indicator_id=f"indicator.test_{index}",
            definition_version="1.0.0",
            params={},
            trigger="above",
            value=float(index),
        )
        for index in range(16)
    )
    branch = AllConditionV2(children=leaves)
    oversized = AllConditionV2(children=(branch, branch, branch, branch))
    with pytest.raises(ValidationError, match="64 nodes"):
        _spec(entry=oversized)


def test_condition_grounding_is_strict_and_spans_are_ordered() -> None:
    grounding = ConditionGrounding(
        dsl_path="$.entry.children[0]",
        source_start=3,
        source_end=12,
        source_text="净利润超过1亿",
    )
    assert grounding.dsl_path == "$.entry.children[0]"

    with pytest.raises(ValidationError, match="source_end"):
        ConditionGrounding(
            dsl_path="$.entry",
            source_start=8,
            source_end=8,
            source_text="空",
        )
    with pytest.raises(ValidationError):
        ConditionGrounding(
            dsl_path="entry",
            source_start=0,
            source_end=2,
            source_text="买入",
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        ConditionGrounding.model_validate({**grounding.model_dump(), "verified": True})

    exact_text = ConditionGrounding(
        dsl_path="$.entry",
        source_start=0,
        source_end=10,
        source_text=" MACD 金叉 ",
    )
    assert exact_text.source_text == " MACD 金叉 "


def test_interpretation_coverage_tracks_unmapped_material_source_spans() -> None:
    coverage = InterpretationCoverage(
        groundings=(),
        unmapped_material_spans=(
            SourceSpan(source_start=10, source_end=16, source_text="且且涨停不买"),
        ),
    )
    assert coverage.unmapped_material_spans[0].source_text == "且且涨停不买"


def test_strategy_v2_has_deterministic_canonical_serialization_and_hash() -> None:
    first = _spec(entry=_technical(params={"fast": 12, "slow": 26, "signal": 9}))
    second = _spec(entry=_technical(params={"signal": 9, "slow": 26, "fast": 12}))

    assert canonical_json_v2(first) == canonical_json_v2(second)
    assert canonical_hash_v2(first) == canonical_hash_v2(second)
    assert canonical_hash_v2(first).startswith("sha256:")


def test_backtest_window_is_strict_and_ordered() -> None:
    with pytest.raises(ValidationError, match="on or before"):
        BacktestConfigV2(
            start=date(2026, 1, 2),
            end=date(2025, 1, 2),
            initial_cash_cny=1_000_000,
        )
    with pytest.raises(ValidationError):
        BacktestConfigV2.model_validate(
            {
                "start": "2021-01-04",
                "end": "2026-08-28",
                "initial_cash_cny": 0,
            }
        )


def test_leaf_paths_are_deterministic_and_match_grounding_paths() -> None:
    nested = AllConditionV2(
        children=(
            _technical(),
            NotConditionV2(
                child=FinancialCondition(
                    metric_id="financial.roe",
                    definition_version="1.0.0",
                    report_type="annual",
                    period_basis="full_year",
                    statement_scope="consolidated",
                    revision_policy="as_known_at_signal",
                    comparator="gte",
                    value=15,
                    unit="PERCENT",
                )
            ),
        )
    )
    spec = _spec(entry=nested)

    assert [path for path, _condition in iter_condition_leaf_paths(spec)] == [
        "$.entry.children[0]",
        "$.entry.children[1].child",
        "$.exit",
    ]


def test_strategy_v2_is_frozen_and_round_trips_without_shape_changes() -> None:
    spec = _spec(instrument=_instrument(AssetType.ETF))
    restored = StrategySpecV2.model_validate_json(spec.model_dump_json())

    assert restored == spec
    assert restored.model_dump(mode="json") == spec.model_dump(mode="json")
    with pytest.raises(ValidationError, match="frozen"):
        spec.instrument = _instrument()  # type: ignore[misc]
