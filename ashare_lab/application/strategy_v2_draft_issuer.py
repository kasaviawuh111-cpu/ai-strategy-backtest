"""Provider-only, restart-safe issuance of grounded Strategy v2 drafts.

The bounded language provider may propose candidate intent, but this boundary
accepts only exact technical leaves, resolves the instrument against a trusted
security master, persists the canonical candidate, and later reruns every v2
validation gate from server-owned snapshots.  A restart never replays the
language model to reconstruct a draft.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal, cast

from ashare_lab.domain.catalog import CatalogManifest, CatalogSnapshot
from ashare_lab.domain.instruments import InstrumentResolutionError, InstrumentResolver
from ashare_lab.domain.runs import DraftRevisionV2
from ashare_lab.domain.shared import InstrumentId
from ashare_lab.domain.strategy import (
    AllConditionV2,
    AnyConditionV2,
    BacktestConfigV2,
    CatalogRefV2,
    ConditionGrounding,
    InterpretationCoverage,
    StrategyCandidateV2,
    StrategySpecV2,
    StrategyV2ValidationContext,
    StrategyV2ValidationError,
    TechnicalConditionV2,
    canonical_hash_v2,
    iter_condition_leaf_paths,
    validate_strategy_candidate_v2,
)
from ashare_lab.domain.strategy.canonical import canonical_json
from ashare_lab.domain.strategy.validation_v2 import condition_semantics_hash
from ashare_lab.ports.candidate_generation import (
    BoundedCandidateGenerator,
    CandidateAst,
    CompileInput,
    IndicatorIntent,
)
from ashare_lab.ports.market_data import DateRange
from ashare_lab.ports.trusted_snapshots import (
    TrustedInstrumentResolver,
    TrustedSecurityMasterSnapshot,
    TrustedSecurityMasterSnapshotLoader,
    TrustedSnapshotError,
    TrustedSnapshotProviderUnavailableError,
    TrustedStrategyV2SnapshotResolver,
    TrustedTechnicalSnapshotLoader,
    build_trusted_v2_snapshot_contracts,
)

from .strategy_v2_http import (
    StrategyV2DraftCommand,
    StrategyV2DraftStatus,
    StrategyV2DraftView,
    StrategyV2HttpServiceError,
)
from .validation_receipts_v2 import (
    PersistentPlanRecoveryError,
    ValidationReceiptServiceV2,
    snapshot_bindings_hash,
)

_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_PROVIDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SENSITIVE_REQUEST = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{16,}\b|authorization\s*:\s*bearer\s+\S+)",
    re.IGNORECASE,
)
_CLARIFICATION_CODES = frozenset(
    {
        "ambiguous_boolean_expression",
        "ambiguous_macd_trigger",
        "candidate_provider_low_confidence",
        "entry_rule_not_recognized",
        "exit_rule_not_recognized",
        "incomplete_rule",
        "instrument_missing",
        "missing_entry_rule",
        "missing_exit_rule",
    }
)


@dataclass(frozen=True, slots=True)
class StrategyV2ProducerPin:
    snapshot_id: str
    coverage_end: date

    def __post_init__(self) -> None:
        if re.fullmatch(r"composite:[0-9a-f]{64}", self.snapshot_id) is None:
            raise ValueError("Strategy v2 execution pin must be a Composite content id")
        if type(self.coverage_end) is not date:
            raise ValueError("Strategy v2 execution pin coverage_end must be a date")


class _DraftRejection(ValueError):
    def __init__(
        self,
        *,
        status: StrategyV2DraftStatus,
        code: str,
        message: str,
        requested_instrument: str | None = None,
    ) -> None:
        self.status: StrategyV2DraftStatus = status
        self.code: str = code
        self.requested_instrument: str | None = requested_instrument
        super().__init__(message)


class GroundedStrategyV2Issuer:
    """Turn one bounded provider candidate into a persisted v2 draft."""

    def __init__(
        self,
        *,
        generator: BoundedCandidateGenerator,
        provider: str = "bounded-provider",
        catalog: CatalogSnapshot,
        receipts: ValidationReceiptServiceV2,
        security_master_loader: TrustedSecurityMasterSnapshotLoader,
        security_master_snapshot_id: str,
        technical_snapshot_loader: TrustedTechnicalSnapshotLoader,
        producer_snapshot_id: str | None,
        backtest_anchor_date: date | None,
        producer_pins: Mapping[str, StrategyV2ProducerPin] | None = None,
        instrument_resolver: TrustedInstrumentResolver | None = None,
        snapshot_resolver: TrustedStrategyV2SnapshotResolver | None = None,
        code_revision: str,
        initial_cash_cny: int = 1_000_000,
        default_lookback_years: int = 1,
        draft_id_factory: Callable[[], str] = lambda: f"draft:{uuid.uuid4().hex}",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        # Accepting a generic CandidateGenerator here would silently reopen the
        # local-rule or arbitrary-model path. P0-D intentionally uses only the
        # schema-constrained, exact-span Vibe adapter.
        if getattr(generator, "boundary", None) != "schema_bounded_candidate.v1":
            raise TypeError("GroundedStrategyV2Issuer requires VibeBoundedCandidateGenerator")
        if _PROVIDER.fullmatch(provider) is None:
            raise ValueError("provider identity is invalid")
        if _GIT_SHA.fullmatch(code_revision) is None:
            raise ValueError("code_revision must be a complete Git SHA")
        if type(initial_cash_cny) is not int or initial_cash_cny <= 0:
            raise ValueError("initial_cash_cny must be a positive integer")
        if type(default_lookback_years) is not int or not 1 <= default_lookback_years <= 20:
            raise ValueError("default_lookback_years must be between 1 and 20")
        default_pin: StrategyV2ProducerPin | None = None
        if producer_snapshot_id is not None:
            if backtest_anchor_date is None:
                raise ValueError(
                    "default producer id and backtest anchor must be configured together"
                )
            default_pin = StrategyV2ProducerPin(producer_snapshot_id, backtest_anchor_date)
        selected_pins = dict(producer_pins or {})
        if default_pin is None and not selected_pins and snapshot_resolver is None:
            raise ValueError("a trusted producer pin or server-owned snapshot resolver is required")
        for symbol, pin in selected_pins.items():
            if re.fullmatch(r"[0-9]{6}\.(?:SH|SZ|BJ)", symbol) is None:
                raise ValueError("Strategy v2 producer pin symbol is invalid")
            if type(pin) is not StrategyV2ProducerPin:
                raise ValueError("Strategy v2 producer pin value is invalid")
        self._generator = generator
        self._provider = provider
        self._catalog = catalog
        self._receipts = receipts
        self._security_master_loader = security_master_loader
        self._security_master_snapshot_id = security_master_snapshot_id
        self._technical_snapshot_loader = technical_snapshot_loader
        self._default_producer_pin = default_pin
        self._producer_pins = selected_pins
        self._instrument_resolver = instrument_resolver
        self._snapshot_resolver = snapshot_resolver
        self._code_revision = code_revision
        self._initial_cash_cny = initial_cash_cny
        self._default_lookback_years = default_lookback_years
        self._draft_id_factory = draft_id_factory
        self._clock = clock

    @property
    def generator(self) -> BoundedCandidateGenerator:
        """Expose the fixed generator identity for composition and tests only."""

        return self._generator

    async def create_draft(self, command: StrategyV2DraftCommand) -> StrategyV2DraftView:
        if _SENSITIVE_REQUEST.search(command.utterance) or (
            command.instrument_context is not None
            and _SENSITIVE_REQUEST.search(command.instrument_context)
        ):
            # Reject before the text can reach a remote provider or durable
            # storage. The public message deliberately does not echo input.
            raise StrategyV2HttpServiceError(
                "sensitive_input_rejected",
                "request contains credential-like text",
            )
        draft_id = self._draft_id_factory()
        created_at = self._aware_now()
        request = CompileInput(
            utterance=command.utterance,
            instrument_context=command.instrument_context,
            as_of_date=self._provider_anchor_date(),
        )
        candidates = await self._generator.generate(request)
        ready: dict[str, tuple[StrategySpecV2, str, str, str]] = {}
        rejections: list[_DraftRejection] = []
        for candidate in candidates:
            if candidate.unsupported_code is not None:
                rejections.append(self._unsupported_candidate(candidate, command))
                continue
            try:
                strategy, requested, producer_id, security_master_id = self._ground_candidate(
                    candidate,
                    command,
                )
            except _DraftRejection as error:
                rejections.append(error)
                continue
            ready[canonical_hash_v2(strategy)] = (
                strategy,
                requested,
                producer_id,
                security_master_id,
            )

        if len(ready) == 1:
            strategy_hash, (strategy, requested, producer_id, security_master_id) = next(
                iter(ready.items())
            )
            record = DraftRevisionV2(
                draft_id=draft_id,
                revision=1,
                original_input=command.utterance,
                provider=self._provider,
                draft_status="ready",
                requested_instrument=requested,
                as_of_date=strategy.backtest.end,
                candidate_strategy_json=canonical_json(strategy),
                candidate_strategy_hash=strategy_hash,
                producer_snapshot_id=producer_id,
                security_master_snapshot_id=security_master_id,
                created_at=created_at,
            )
            self._receipts.save_draft(record)
            return self._view(record)

        if len(ready) > 1:
            rejection = _DraftRejection(
                status="needs_clarification",
                code="multiple_strategy_candidates",
                message="识别出多个不同策略，请集中确认一次买入和卖出条件",
                requested_instrument=command.instrument_context,
            )
        elif rejections:
            rejection = rejections[0]
        else:
            rejection = _DraftRejection(
                status="unsupported",
                code="no_supported_signal_recognized",
                message="未识别到可执行的纯技术买入和卖出条件",
                requested_instrument=command.instrument_context,
            )
        record = DraftRevisionV2(
            draft_id=draft_id,
            revision=1,
            original_input=command.utterance,
            provider=self._provider,
            draft_status=rejection.status,
            requested_instrument=rejection.requested_instrument,
            as_of_date=self._provider_anchor_date(),
            diagnostic_code=rejection.code,
            clarification=str(rejection),
            created_at=created_at,
        )
        self._receipts.save_draft(record)
        return self._view(record)

    def get_draft(self, draft_id: str, revision: int) -> StrategyV2DraftView:
        record = self._receipts.store.get_draft_revision(draft_id, revision)
        if record is None:
            raise StrategyV2HttpServiceError(
                "strategy_draft_not_found",
                "draft revision was not found in server storage",
            )
        if record.draft_status == "legacy":
            raise StrategyV2HttpServiceError(
                "draft_revision_not_recoverable",
                "legacy draft revision does not contain a persisted Strategy v2 candidate",
            )
        return self._view(record)

    def validate(self, draft_id: str, revision: int) -> None:
        record = self._receipts.store.get_draft_revision(draft_id, revision)
        if record is None:
            raise StrategyV2HttpServiceError(
                "strategy_draft_not_found",
                "draft revision was not found in server storage",
            )
        if record.draft_status != "ready" or record.candidate_strategy is None:
            raise StrategyV2HttpServiceError(
                "draft_not_ready",
                "draft is not ready for Strategy v2 validation",
            )
        requested = record.requested_instrument
        if requested is None:
            raise StrategyV2HttpServiceError(
                "draft_not_ready",
                "persisted draft is missing its requested instrument",
            )
        strategy = record.candidate_strategy
        producer_id = record.producer_snapshot_id
        if producer_id is None:
            # Backward-compatible read of pre-P0-D ready rows. New drafts always
            # persist the exact server-selected producer above.
            producer_id = self._producer_pin(strategy.instrument.symbol).snapshot_id
        period = DateRange(strategy.backtest.start, strategy.backtest.end)
        try:
            security_master = self._security_master_loader.load(
                record.security_master_snapshot_id or self._security_master_snapshot_id
            )
            technical = self._technical_snapshot_loader.load(
                producer_snapshot_id=producer_id,
                instrument_id=InstrumentId(strategy.instrument.symbol),
                period=period,
                security_master=security_master,
            )
            contracts = build_trusted_v2_snapshot_contracts(
                security_master=security_master,
                technical_snapshot=technical,
                instrument_id=InstrumentId(strategy.instrument.symbol),
                period=period,
            )
        except TrustedSnapshotProviderUnavailableError as error:
            raise StrategyV2HttpServiceError("provider_unavailable", str(error)) from error
        except TrustedSnapshotError as error:
            raise StrategyV2HttpServiceError("data_unavailable", str(error)) from error

        candidate = StrategyCandidateV2(
            original_input=record.original_input,
            draft_id=record.draft_id,
            revision=record.revision,
            provider=record.provider,
            strategy=strategy,
        )
        current_bindings = (
            contracts.security_master,
            contracts.trading_calendar,
            contracts.market_data,
            *contracts.producer_children,
            contracts.composite_snapshot,
        )
        context = StrategyV2ValidationContext(
            security_master=security_master.snapshot,
            original_input=record.original_input,
            draft_id=record.draft_id,
            revision=record.revision,
            provider=record.provider,
            requested_instrument=requested,
            expected_backtest=strategy.backtest,
            catalog=self._catalog,
            dataset_coverage=(contracts.dataset_coverage,),
            grounding_expectations=tuple(
                self._grounding_expectation(
                    strategy,
                    path,
                    cast(TechnicalConditionV2, condition),
                )
                for path, condition in iter_condition_leaf_paths(strategy)
            ),
            code_revision=self._code_revision,
            trading_calendar_snapshot_id=contracts.trading_calendar.snapshot_id,
            composite_snapshot_id=contracts.composite_snapshot.snapshot_id,
            snapshot_bindings_hash=snapshot_bindings_hash(current_bindings),
        )
        try:
            plan = validate_strategy_candidate_v2(candidate, context)
        except StrategyV2ValidationError as error:
            raise StrategyV2HttpServiceError(error.issue.code, str(error)) from error
        latest = self._receipts.store.get_validation_receipt_for_draft_revision(
            draft_id,
            revision,
        )
        if latest is not None:
            try:
                self._receipts.restore_executable_plan(
                    latest.receipt_id,
                    current_context=context,
                    current_snapshot_bindings=current_bindings,
                )
            except PersistentPlanRecoveryError:
                # Expiry, signature failure, or any changed trusted binding
                # requires a complete validator pass and a newly appended
                # receipt. Existing receipts and runs remain immutable.
                pass
            else:
                return
        self._receipts.persist_validated_plan(
            plan,
            snapshot_bindings=current_bindings,
        )

    def _ground_candidate(
        self,
        candidate: CandidateAst,
        command: StrategyV2DraftCommand,
    ) -> tuple[StrategySpecV2, str, str, str]:
        provenance = candidate.provenance
        if provenance is None or provenance.source != "bounded_provider":
            raise _DraftRejection(
                status="invalid",
                code="candidate_provenance_missing",
                message="候选策略缺少受限模型来源证明",
                requested_instrument=command.instrument_context,
            )
        if provenance.provider != self._provider:
            raise _DraftRejection(
                status="invalid",
                code="candidate_provider_mismatch",
                message="候选策略来源与服务端配置不一致",
                requested_instrument=command.instrument_context,
            )
        if not candidate.entry or not candidate.exit:
            raise _DraftRejection(
                status="needs_clarification",
                code="incomplete_rule",
                message="请一次补充完整的买入和卖出条件",
                requested_instrument=command.instrument_context,
            )
        if any(type(item) is not IndicatorIntent for item in (*candidate.entry, *candidate.exit)):
            raise _DraftRejection(
                status="unsupported",
                code="capability_unavailable",
                message="当前 Strategy v2 HTTP 运行时只开放纯技术条件",
                requested_instrument=command.instrument_context or candidate.instrument_symbol,
            )

        requested = command.instrument_context or candidate.instrument_symbol
        if requested is None:
            raise _DraftRejection(
                status="needs_clarification",
                code="instrument_missing",
                message="请提供具体的 A 股股票或 ETF 代码",
            )
        try:
            trusted_master = self._security_master_loader.load(self._security_master_snapshot_id)
            if self._instrument_resolver is not None:
                selection = self._instrument_resolver.resolve_or_prepare(
                    requested,
                    as_of=trusted_master.metadata.coverage.end,
                )
                trusted_master = selection.security_master
                resolved_identity = selection.instrument
            else:
                resolved_identity = InstrumentResolver(trusted_master.snapshot).resolve(
                    requested,
                    as_of=trusted_master.metadata.coverage.end,
                )
            resolver = InstrumentResolver(trusted_master.snapshot)
            producer_pin = self._resolve_producer(
                candidate,
                instrument_id=InstrumentId(resolved_identity.symbol),
                listing_date=resolved_identity.listing_date,
                security_master=trusted_master,
            )
            backtest = self._backtest(
                candidate,
                producer_pin.coverage_end,
                listing_date=resolved_identity.listing_date,
            )
            instrument = resolver.resolve(requested, as_of=backtest.start)
            if instrument.symbol != resolved_identity.symbol:
                raise InstrumentResolutionError(
                    identifier=requested,
                    message="instrument identity changed within the requested period",
                )
            instrument.require_tradable_on(backtest.end)
            # Validate the final anchored period before persisting the draft;
            # later validation/execution only reopen this exact producer.
            self._technical_snapshot_loader.load(
                producer_snapshot_id=producer_pin.snapshot_id,
                instrument_id=InstrumentId(instrument.symbol),
                period=DateRange(backtest.start, backtest.end),
                security_master=trusted_master,
            )
        except InstrumentResolutionError as error:
            status: StrategyV2DraftStatus = (
                "unsupported" if error.code == "capability_unavailable" else "invalid"
            )
            message = (
                "当前暂不支持指数回测；请提供希望交易的具体 A 股 ETF 代码"
                if error.code == "capability_unavailable"
                else "证券主数据无法确认该标的，请提供带交易所后缀的股票或 ETF 代码"
            )
            raise _DraftRejection(
                status=status,
                code=error.code,
                message=message,
                requested_instrument=requested,
            ) from error
        except TrustedSnapshotProviderUnavailableError as error:
            raise _DraftRejection(
                status="invalid",
                code="provider_unavailable",
                message="证券搜索或主数据服务暂不可用，请稍后重试",
                requested_instrument=requested,
            ) from error
        except TrustedSnapshotError as error:
            raise _DraftRejection(
                status="invalid",
                code="snapshot_unavailable",
                message="可信证券主数据暂不可用",
                requested_instrument=requested,
            ) from error

        entry_leaves = tuple(
            self._technical_condition(cast(IndicatorIntent, item)) for item in candidate.entry
        )
        exit_leaves = tuple(
            self._technical_condition(cast(IndicatorIntent, item)) for item in candidate.exit
        )
        entry = self._condition_tree(entry_leaves, candidate.entry_join)
        exit_condition = self._condition_tree(exit_leaves, candidate.exit_join)
        paths = tuple(
            [
                "$.entry" if len(entry_leaves) == 1 else f"$.entry.children[{index}]"
                for index in range(len(entry_leaves))
            ]
            + [
                "$.exit" if len(exit_leaves) == 1 else f"$.exit.children[{index}]"
                for index in range(len(exit_leaves))
            ]
        )
        source_paths = tuple(
            [f"/entry/{index}" for index in range(len(entry_leaves))]
            + [f"/exit/{index}" for index in range(len(exit_leaves))]
        )
        evidence_by_path = {item.path: item for item in candidate.grounding_evidence}
        if len(evidence_by_path) != len(candidate.grounding_evidence):
            raise _DraftRejection(
                status="invalid",
                code="condition_grounding_duplicate",
                message="同一个策略条件出现了重复原话证据",
                requested_instrument=requested,
            )
        groundings: list[ConditionGrounding] = []
        for source_path, dsl_path in zip(source_paths, paths, strict=True):
            evidence = evidence_by_path.get(source_path)
            if evidence is None or (
                command.utterance[evidence.start : evidence.end] != evidence.text
            ):
                raise _DraftRejection(
                    status="invalid",
                    code="condition_grounding_incomplete",
                    message="每个技术条件都必须精确对应用户原话",
                    requested_instrument=requested,
                )
            groundings.append(
                ConditionGrounding(
                    dsl_path=dsl_path,
                    source_start=evidence.start,
                    source_end=evidence.end,
                    source_text=evidence.text,
                )
            )
        release = self._catalog_release((*candidate.entry, *candidate.exit))
        strategy = StrategySpecV2(
            catalog=CatalogRefV2(
                catalog_id=release.catalog_id,
                release_version=release.release_version,
            ),
            instrument=instrument,
            entry=entry,
            exit=exit_condition,
            interpretation_coverage=InterpretationCoverage(
                groundings=tuple(groundings),
            ),
            backtest=backtest,
        )
        return (
            strategy,
            requested,
            producer_pin.snapshot_id,
            trusted_master.metadata.snapshot_id,
        )

    def _catalog_release(self, intents: tuple[object, ...]) -> CatalogManifest:
        required = {
            (item.indicator_id, item.definition_version)
            for item in intents
            if isinstance(item, IndicatorIntent)
        }
        matches = tuple(
            manifest
            for manifest in self._catalog.manifests
            if manifest.state == "active"
            and required.issubset({(item.id, item.version) for item in manifest.indicators})
        )
        if len(matches) != 1:
            raise _DraftRejection(
                status="invalid",
                code="catalog_release_unresolved",
                message="无法把全部技术条件绑定到唯一的服务端 Catalog 版本",
            )
        return matches[0]

    def _backtest(
        self,
        candidate: CandidateAst,
        anchor_date: date,
        *,
        listing_date: date | None = None,
    ) -> BacktestConfigV2:
        explicit = candidate.backtest_start is not None or candidate.backtest_end is not None
        if explicit:
            if candidate.backtest_start is None or candidate.backtest_end is None:
                raise _DraftRejection(
                    status="needs_clarification",
                    code="backtest_period_incomplete",
                    message="请一次提供完整的回测开始和结束日期",
                )
            if candidate.backtest_lookback_years is not None:
                raise _DraftRejection(
                    status="invalid",
                    code="backtest_period_conflict",
                    message="回测区间不能同时使用明确日期和近 N 年",
                )
            start = candidate.backtest_start
            end = candidate.backtest_end
        else:
            years = candidate.backtest_lookback_years or self._default_lookback_years
            end = anchor_date
            start = _subtract_years(end, years)
            if listing_date is not None:
                start = max(start, listing_date)
        if end > anchor_date:
            raise _DraftRejection(
                status="invalid",
                code="backtest_end_after_as_of_date",
                message="回测结束日期不能晚于服务端确认的当前日期",
            )
        return BacktestConfigV2(
            start=start,
            end=end,
            initial_cash_cny=self._initial_cash_cny,
        )

    def _resolve_producer(
        self,
        candidate: CandidateAst,
        *,
        instrument_id: InstrumentId,
        listing_date: date,
        security_master: TrustedSecurityMasterSnapshot,
    ) -> StrategyV2ProducerPin:
        if self._snapshot_resolver is None:
            return self._producer_pin(str(instrument_id))
        requested_period = self._preparation_period(candidate, listing_date=listing_date)
        try:
            selection = self._snapshot_resolver.resolve_or_prepare(
                instrument_id=instrument_id,
                requested_period=requested_period,
                security_master=security_master,
            )
        except TrustedSnapshotError as error:
            raise _DraftRejection(
                status="invalid",
                code="data_unavailable",
                message="服务端暂时无法为该标的准备可信的历史行情",
                requested_instrument=str(instrument_id),
            ) from error
        return StrategyV2ProducerPin(
            snapshot_id=selection.producer_snapshot_id,
            coverage_end=selection.coverage_end,
        )

    def _preparation_period(self, candidate: CandidateAst, *, listing_date: date) -> DateRange:
        if candidate.backtest_start is not None or candidate.backtest_end is not None:
            if candidate.backtest_start is None or candidate.backtest_end is None:
                raise _DraftRejection(
                    status="needs_clarification",
                    code="backtest_period_incomplete",
                    message="请一次提供完整的回测开始和结束日期",
                )
            return DateRange(candidate.backtest_start, candidate.backtest_end)
        years = candidate.backtest_lookback_years or self._default_lookback_years
        end = self._provider_anchor_date()
        # Reserve a small calendar buffer so anchoring the final period to the
        # last actually covered trading date never asks before acquisition.
        start = max(listing_date, _subtract_years(end, years) - timedelta(days=14))
        return DateRange(start, end)

    def _producer_pin(self, symbol: str) -> StrategyV2ProducerPin:
        pin = self._producer_pins.get(symbol, self._default_producer_pin)
        if pin is None:
            raise StrategyV2HttpServiceError(
                "data_unavailable",
                "server has no trusted immutable producer for this instrument",
            )
        return pin

    def _provider_anchor_date(self) -> date:
        anchors = [item.coverage_end for item in self._producer_pins.values()]
        if self._default_producer_pin is not None:
            anchors.append(self._default_producer_pin.coverage_end)
        if anchors:
            return min(anchors)
        return self._security_master_loader.load(
            self._security_master_snapshot_id
        ).metadata.coverage.end

    @staticmethod
    def _technical_condition(intent: IndicatorIntent) -> TechnicalConditionV2:
        return TechnicalConditionV2(
            indicator_id=intent.indicator_id,
            definition_version=intent.definition_version,
            params=intent.params_dict(),
            trigger=intent.trigger,
            value=intent.value,
        )

    @staticmethod
    def _condition_tree(
        conditions: tuple[TechnicalConditionV2, ...],
        join: Literal["all", "any"],
    ) -> TechnicalConditionV2 | AllConditionV2 | AnyConditionV2:
        if len(conditions) == 1:
            return conditions[0]
        if join == "all":
            return AllConditionV2(children=conditions)
        return AnyConditionV2(children=conditions)

    @staticmethod
    def _grounding_expectation(
        strategy: StrategySpecV2,
        path: str,
        condition: TechnicalConditionV2,
    ):
        grounding = next(
            (item for item in strategy.interpretation_coverage.groundings if item.dsl_path == path),
            None,
        )
        if grounding is None:
            raise StrategyV2HttpServiceError(
                "condition_grounding_incomplete",
                "persisted draft is missing condition grounding",
            )
        from ashare_lab.domain.strategy import ConditionGroundingExpectation

        return ConditionGroundingExpectation(
            dsl_path=path,
            source_start=grounding.source_start,
            source_end=grounding.source_end,
            source_text=grounding.source_text,
            condition_hash=condition_semantics_hash(condition),
        )

    def _unsupported_candidate(
        self,
        candidate: CandidateAst,
        command: StrategyV2DraftCommand,
    ) -> _DraftRejection:
        code = candidate.unsupported_code or "no_supported_signal_recognized"
        status: StrategyV2DraftStatus = (
            "needs_clarification" if code in _CLARIFICATION_CODES else "unsupported"
        )
        message = (
            "请一次补充完整且明确的买入、卖出条件"
            if status == "needs_clarification"
            else "这句话暂时不能还原为受限的纯技术策略"
        )
        return _DraftRejection(
            status=status,
            code=code,
            message=message,
            requested_instrument=command.instrument_context or candidate.instrument_symbol,
        )

    @staticmethod
    def _view(record: DraftRevisionV2) -> StrategyV2DraftView:
        if record.draft_status == "legacy":
            raise StrategyV2HttpServiceError(
                "draft_revision_not_recoverable",
                "legacy draft revision does not contain a persisted Strategy v2 candidate",
            )
        return StrategyV2DraftView(
            draft_id=record.draft_id,
            revision=record.revision,
            status=record.draft_status,
            provider=record.provider,
            created_at=record.created_at,
            strategy=record.candidate_strategy,
            strategy_hash=record.candidate_strategy_hash,
            clarification=record.clarification,
            diagnostic_code=record.diagnostic_code,
        )

    def _aware_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("draft clock must return a timezone-aware datetime")
        return value


def _subtract_years(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        # February 29 maps to February 28 without inventing a trading session.
        return value.replace(year=value.year - years, day=28)


__all__ = ["GroundedStrategyV2Issuer", "StrategyV2ProducerPin"]
