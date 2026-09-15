"""Derive Skill data routes from the existing Catalog and engine, not probe failures.

This describes integration, not point-in-time data availability or investment validity.
Provider field contracts remain strict; local formulas never masquerade as those fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from ashare_lab.domain.catalog import (
    CatalogSnapshot,
    CoverageCatalogSnapshot,
    load_catalog_directory,
    load_coverage_catalog_directory,
)
from ashare_lab.domain.signals.provider_catalog import PROVIDER_INDICATOR_BUILDERS
from ashare_lab.domain.signals.runtime import STABLE_INDICATOR_EVALUATOR_IDS
from ashare_lab.domain.signals.skill_numeric import SKILL_COMPARISON_IDS, SKILL_SERIES_COMPARE_ID
from ashare_lab.ports.provider_indicator_data import LEGACY_SKILL_PROVIDER_INDICATORS

from .skill_numeric_catalog import (
    SKILL_NUMERIC_DATASET,
    SKILL_NUMERIC_FIELDS,
    SKILL_SERIES_COMPARE_FIELDS,
)

# These describe an available input adapter, not a list of admitted indicators.
_SKILL_DAILY_FIELDS = frozenset(
    {"instrument_id", "session_date", "open", "high", "low", "close", "volume", "amount"}
)
# Preserve the already-shipped formula/source choice. Other verified provider
# contracts retain provider-first behavior.
_EXISTING_LOCAL_ROUTES = frozenset(
    {
        "technical.ma", "technical.macd", "technical.donchian",
        "price.rolling_high", "volume.relative",
    }
)
# Recover only Catalog-defined formulas on independently validated daily data.
# RSI recovery uses the Catalog's explicit Wilder definition and original period;
# it is labelled local computation, not claimed to reproduce a vendor's seed.
_VERIFIED_LEGACY_FORMULA_RECOVERY = frozenset({
    "price.consecutive_up", "price.close", "technical.rsi", "technical.ma_cross",
})


@dataclass(frozen=True)
class SkillIndicatorRoute:
    source: Literal[
        "provider_indicator", "skill_ohlcv_python", "skill_numeric_history", "unavailable"
    ]
    formula_summary: str
    implementation_ref: str | None
    required_fields: tuple[str, ...]
    reason: str | None = None
    fallback_source: Literal["skill_ohlcv_python"] | None = None

    @property
    def source_note(self) -> str:
        if self.source == "provider_indicator":
            return (
                "优先使用东方财富 Skill 历史指标序列，按实际请求校验字段、参数和日期。"
                + ("成品指标不可用时，复用 Skill 历史行情和目录内现有公式计算，记录实际来源。"
                   if self.fallback_source else "")
            )
        if self.source == "skill_ohlcv_python":
            return (
                "东方财富 Skill 提供历史日线，现有 Python 引擎按目录公式计算；"
                "不要求供应商返回成品指标。实际股票、区间及预热覆盖在运行时检查。"
            )
        if self.source == "skill_numeric_history":
            return (
                "服务端按条件中的指标查询描述取得 Skill 真实日线数值，检查股票、字段、比较单位"
                "和日期覆盖后绑定；不是 OHLCV 公式，也不表示所有指标已取得数据。"
                "按日线收盘条件进行研究回放，下一可交易日尝试成交。"
            )
        return self.reason or "所需历史数据尚未接入。"


def build_skill_indicator_routes(
    catalog: CatalogSnapshot, coverage: CoverageCatalogSnapshot,
    *, provider_first: bool = True,
) -> dict[str, SkillIndicatorRoute]:
    """Only actual evaluators with connected input dependencies can be admitted."""
    metrics = {metric.id: metric for metric in coverage.metrics}
    routes: dict[str, SkillIndicatorRoute] = {}
    for definition in catalog.indicators:
        if definition.status != "stable":
            continue
        metric = metrics.get(definition.id)
        if metric is None or metric.status != "stable":
            raise ValueError(f"Skill route missing stable coverage: {definition.id}")
        required = tuple(sorted({
            field for item in metric.data_requirements for field in item.fields
        }))
        source: Literal[
            "provider_indicator", "skill_ohlcv_python", "skill_numeric_history", "unavailable"
        ]
        reason = None
        provider_fields: frozenset[str] = _SKILL_DAILY_FIELDS | (
            {"turnover_rate_pct", "turnover_rate_provider", "turnover_rate_methodology"}
            if definition.id == "market.turnover_rate" else set[str]()
        )
        provider_dependencies_connected = bool(metric.data_requirements) and all(
            item.dataset_id == "market.ohlcv.daily"
            and item.frequency == "1d"
            and set(item.fields) <= provider_fields
            for item in metric.data_requirements
        )
        formula_inputs_connected = (
            definition.id in STABLE_INDICATOR_EVALUATOR_IDS
            and bool(metric.implementation_ref and metric.formula_summary)
            and bool(metric.data_requirements)
            and all(item.dataset_id == "market.ohlcv.daily"
                    and item.frequency == "1d" and set(item.fields) <= _SKILL_DAILY_FIELDS
                    for item in metric.data_requirements)
        )
        if (
            definition.id in SKILL_COMPARISON_IDS
            and metric.implementation_ref
            and metric.formula_summary
            and len(metric.data_requirements) == 1
            and metric.data_requirements[0].dataset_id == SKILL_NUMERIC_DATASET
            and metric.data_requirements[0].frequency == "1d"
            and frozenset(metric.data_requirements[0].fields) == (
                SKILL_SERIES_COMPARE_FIELDS if definition.id == SKILL_SERIES_COMPARE_ID
                else SKILL_NUMERIC_FIELDS
            )
            and not metric.data_requirements[0].point_in_time_required
        ):
            source = "skill_numeric_history"
        elif (
            provider_first
            and definition.id in PROVIDER_INDICATOR_BUILDERS
            and provider_dependencies_connected
        ) or (
            definition.id in LEGACY_SKILL_PROVIDER_INDICATORS
            and definition.id not in _EXISTING_LOCAL_ROUTES
            and provider_dependencies_connected
        ):
            source = "provider_indicator"
        elif (
            not provider_first
            and definition.id in STABLE_INDICATOR_EVALUATOR_IDS
            and metric.implementation_ref
            and metric.formula_summary
            and metric.data_requirements
            and all(
                item.dataset_id == "market.ohlcv.daily"
                and item.frequency == "1d"
                and set(item.fields) <= _SKILL_DAILY_FIELDS
                for item in metric.data_requirements
            )
        ):
            source = "skill_ohlcv_python"
        else:
            source = "unavailable"
            reason = (
                "尚无对应的确定性计算实现。"
                if definition.id not in STABLE_INDICATOR_EVALUATOR_IDS
                else "所需历史数据或计算口径尚未接入：" + ", ".join(required)
            )
        routes[definition.id] = SkillIndicatorRoute(
            source, metric.formula_summary or "", metric.implementation_ref, required, reason,
            fallback_source=(
                "skill_ohlcv_python" if source == "provider_indicator"
                and formula_inputs_connected
                and (definition.id not in LEGACY_SKILL_PROVIDER_INDICATORS
                     or definition.id in _EXISTING_LOCAL_ROUTES
                     or definition.id in _VERIFIED_LEGACY_FORMULA_RECOVERY) else None
            ),
        )
    return routes


@lru_cache(maxsize=1)
def default_skill_indicator_routes() -> dict[str, SkillIndicatorRoute]:
    """Legacy formula profile for direct callers; live Skill composition supplies routes."""
    root = Path(__file__).resolve().parents[2] / "catalogs"
    return build_skill_indicator_routes(
        load_catalog_directory(root), load_coverage_catalog_directory(root / "coverage"),
        provider_first=False,
    )
