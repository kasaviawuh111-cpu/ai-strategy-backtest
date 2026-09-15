"""Skill-only catalog overlay for generic numeric comparison operators.

The operator is stable; the requested metric's data is not thereby certified.
No global manifest, OHLCV evaluator inventory, or other profile is changed.
"""

from __future__ import annotations

from datetime import date

from ashare_lab.domain.catalog import (
    CatalogManifest,
    CatalogSnapshot,
    CatalogTimeSemantics,
    CoverageCatalogRelease,
    CoverageCatalogSnapshot,
    DataRequirement,
    IndicatorDefinition,
    MetricDefinition,
    ParameterDefinition,
    TriggerDefinition,
)
from ashare_lab.domain.signals.skill_numeric import (
    SKILL_COMPARISON_IDS,
    SKILL_NUMERIC_ID,
    SKILL_NUMERIC_TRIGGERS,
    SKILL_NUMERIC_VERSION,
    SKILL_SERIES_COMPARE_ID,
)
from ashare_lab.domain.strategy.canonical import canonical_hash

SKILL_NUMERIC_DATASET = "market.skill.numeric"
SKILL_NUMERIC_FIELDS = frozenset({"instrument_id", "session_date", "value", "unit"})
SKILL_SERIES_COMPARE_FIELDS = frozenset({
    "instrument_id", "session_date", "left_value", "right_value", "unit",
})


def extend_skill_numeric_catalogs(
    catalog: CatalogSnapshot,
    coverage: CoverageCatalogSnapshot,
) -> tuple[CatalogSnapshot, CoverageCatalogSnapshot]:
    """Copy immutable catalogs and add the operator only to the Skill composition.

    Coverage releases require at least one event. Extend a copied existing
    release instead of inventing an event or duplicating its ownership across
    releases. Its changed version and content hash identify this private overlay.
    """
    if any(catalog.resolve_indicator(key) or coverage.resolve_metric(key)
           for key in SKILL_COMPARISON_IDS):
        raise ValueError("Skill numeric catalog overlay is already present or inconsistent")
    definition = IndicatorDefinition(
        id=SKILL_NUMERIC_ID,
        version=SKILL_NUMERIC_VERSION,
        status="stable",
        warmup_bars=1,
        timeframes=("1d",),
        evaluation_modes=("bar_close_confirmed",),
        parameters=tuple(
            ParameterDefinition(name=name, value_type="string", required=True)
            for name in ("metric_query", "unit")
        ),
        triggers=tuple(
            TriggerDefinition(id=trigger, value_requirement="required")
            for trigger in SKILL_NUMERIC_TRIGGERS
        ),
    )
    metric = MetricDefinition(
        id=SKILL_NUMERIC_ID,
        kind="indicator",
        family="technical",
        name_zh="Skill 通用数值比较",
        description=(
            "稳定的是数值比较操作，不代表所有指标数据已取得。metric_query 必填、"
            "非空且不超过 200 字符；unit 必填并保留用户原始比较单位。"
            "模型只能描述查询、单位、比较方式和阈值，不能提交 fieldCode 等数据证据。"
            "服务端查询真实日线序列并检查股票、字段、单位和覆盖后执行研究回放。"
        ),
        status="stable",
        formula_summary=(
            "将 Skill 实际返回并经服务端绑定的日线数值 value 与有限阈值比较："
            "above/below/at_least/at_most 对应 >/< />=/<=；"
            "crosses_above/crosses_below 使用前一观测和当前观测判定穿越。"
            "metric_query 是原始查询描述，unit 是原始比较单位；不做猜测换算。"
        ),
        parameters=("metric_query", "unit"),
        triggers=SKILL_NUMERIC_TRIGGERS,
        warmup_bars=1,
        data_requirements=(DataRequirement(
            dataset_id=SKILL_NUMERIC_DATASET,
            fields=tuple(sorted(SKILL_NUMERIC_FIELDS)),
            frequency="1d",
            point_in_time_required=False,
            required_time_fields=("fact_at",),
            license_status="internal_only",
        ),),
        time_semantics=CatalogTimeSemantics(
            observation_basis="bar_close",
            required_quality="date_only",
            date_only_policy="conservative_after_close",
            revision_policy="as_known_at_cutoff",
            default_order_time="next_tradable_session_open",
            daily_bar_fallback="next_tradable_session_open",
            notes=(
                "真实返回的首次可得时间存在时遵守该时间；缺失时以观测日收盘作为"
                "研究回放假设，依据只保留在后台审计，不视为已通过 PIT 验证。"
                "存在历史修订但缺少首次版本时不能宣称还原当时已知信息。"
                "实际股票、字段口径、比较单位、数值有限性及日期覆盖由数据层检查。"
            ),
        ),
        implementation_ref="ashare_lab.domain.signals.skill_numeric:skill_numeric_binding",
    )
    compare_parameters = ("left_metric_query", "right_metric_query", "unit")
    compare_definition = definition.model_copy(update={
        "id": SKILL_SERIES_COMPARE_ID,
        "parameters": tuple(
            ParameterDefinition(name=name, value_type="string", required=True)
            for name in compare_parameters
        ),
        "triggers": tuple(
            TriggerDefinition(id=trigger, value_requirement="forbidden")
            for trigger in SKILL_NUMERIC_TRIGGERS
        ),
    })
    compare_metric = metric.model_copy(update={
        "id": SKILL_SERIES_COMPARE_ID,
        "name_zh": "Skill 两条历史数值比较",
        "description": (
            "分别以 left_metric_query、right_metric_query 查询同一股票的两条真实日线历史，"
            "每条查询非空且不超过 200 字符；unit 是两者共同比较单位。"
            "实际字段、单位及股票由数据层核验，按相同交易日配对，不填补缺失值。"
            "只支持数值序列比较，不代表支持任意表达式或盘中事件。禁止填写阈值 value。"
        ),
        "formula_summary": (
            "将两条经 Skill 历史数据绑定且统一单位的 left_value 与 right_value 比较："
            "above/below/at_least/at_most 对应 >/< />=/<=；crosses_above/crosses_below "
            "使用前一交易日和当前交易日的两侧数值判定穿越。缺任一侧不生成当日信号，"
            "信号可得时间不早于任一依赖数据的实际可得时间。"
        ),
        "parameters": compare_parameters,
        "data_requirements": (metric.data_requirements[0].model_copy(update={
            "fields": tuple(sorted(SKILL_SERIES_COMPARE_FIELDS)),
        }),),
        "implementation_ref": (
            "ashare_lab.domain.signals.skill_numeric:skill_series_compare_binding"
        ),
    })
    manifest = CatalogManifest(
        catalog_id="cn_a.skill.numeric",
        release_version=SKILL_NUMERIC_VERSION,
        state="active",
        published_on=date(2026, 9, 7),
        indicators=(definition, compare_definition),
    )
    manifests = tuple(sorted((*catalog.manifests, manifest), key=lambda item: item.catalog_id))
    skill_catalog = CatalogSnapshot(
        manifests=manifests,
        indicators=tuple(item for manifest in manifests for item in manifest.indicators),
        content_hash=canonical_hash({
            "manifests": [item.model_dump(mode="json") for item in manifests],
        }),
    )
    base_release = coverage.releases[0]
    release = CoverageCatalogRelease.model_validate({
        **base_release.model_dump(),
        "release_version": f"{base_release.release_version}.skill-numeric.v2",
        "published_on": date(2026, 9, 7),
        "metrics": (*base_release.metrics, metric, compare_metric),
    })
    releases = (release, *coverage.releases[1:])
    skill_coverage = CoverageCatalogSnapshot(
        releases=releases,
        metrics=tuple(item for release in releases for item in release.metrics),
        events=coverage.events,
        content_hash=canonical_hash({
            "releases": [item.model_dump(mode="json") for item in releases],
        }),
    )
    return skill_catalog, skill_coverage
