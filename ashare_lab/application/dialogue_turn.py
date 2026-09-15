"""State-aware orchestration for one strategy-draft conversation turn.

The existing compiler remains the only strategy parser and execution gate.
This layer decides how the current sentence relates to server-owned dialogue
state before invoking that compiler, so an instrument supplied out of order is
not discarded merely because the previous question happened to ask for an
exit rule.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from ashare_lab.application.compile_strategy import (
    ClarificationSuggestion,
    ClarificationTurnOutcome,
    CompileOutcome,
    CompileStatus,
    StrategyCompiler,
    has_explicit_idea_instrument_reference,
)
from ashare_lab.application.dialogue_state import (
    DialogueState,
    VerifiedInstrumentMemory,
    verified_instrument_symbol,
)
from ashare_lab.application.turn_intent import (
    TurnIntent,
    classify_clarification_turn,
    extract_change_instrument_target,
)
from ashare_lab.ports.candidate_generation import CompileInput
from ashare_lab.ports.clarification_dialogue import ClarificationDialogueTurn, ClarificationOption
from ashare_lab.ports.instrument_resolution import InstrumentNameAmbiguous


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DialogueTurnPlan:
    """One state-aware routing decision and, when applicable, compiler turn."""

    intent: TurnIntent
    clarification_turn: ClarificationTurnOutcome | None = None
    verified_instrument: VerifiedInstrumentMemory | None = None
    pending_instrument_reuse: VerifiedInstrumentMemory | None = None


class DialogueTurnOrchestrator:
    """Route a turn using one atomic ``DialogueState`` projection."""

    def __init__(self, compiler: StrategyCompiler) -> None:
        self._compiler = compiler

    async def classify_intent(self, *, state: DialogueState, answer: str) -> TurnIntent | None:
        if (answer.strip() in state.available_option_ids or classify_clarification_turn(
                answer, has_options=bool(state.available_option_ids)) is TurnIntent.SELECT_OPTION):
            return None
        classify = getattr(self._compiler, "classify_dialogue_intent", None)
        if classify is None:
            return None
        return await classify(
            original_input=state.compile_input, prior_outcome=state.outcome,
            answer=answer, recent_turns=_model_recent_turns(state),
            backtest_results=state.backtest_results,
        )

    async def plan(
        self, *, state: DialogueState, answer: str, try_strategy_edit: bool = True,
        semantic_intent: TurnIntent | None = None,
    ) -> DialogueTurnPlan:
        # A current card ID is already an explicit selection, not prose for a
        # model or an instrument-name lookup. Resolve against this revision only.
        if answer.strip() in state.available_option_ids:
            turn = await self._compiler.answer_clarification(
                original_input=state.compile_input, prior_outcome=state.outcome,
                answer=answer.strip(), recent_turns=_model_recent_turns(state),
                semantic_intent=TurnIntent.SELECT_OPTION,
            )
            instrument = verified_instrument_symbol(turn.compile_input, turn.outcome)
            return DialogueTurnPlan(
                intent=TurnIntent.SELECT_OPTION, clarification_turn=turn,
                verified_instrument=(_compiled_instrument_memory(
                    instrument=instrument, outcome=turn.outcome, source="model_instrument_selection",
                ) if turn.revision_changed and instrument is not None
                    and instrument != state.verified_instrument_context else None),
            )
        intent = classify_clarification_turn(
            answer,
            has_options=bool(state.available_option_ids),
        )
        # Typed option IDs need no language interpretation. Other follow-ups
        # use server-owned context before choosing data, strategy or chat work.
        if semantic_intent is None:
            semantic_intent = await self.classify_intent(state=state, answer=answer)
        if semantic_intent is not None:
            intent = semantic_intent
        if intent is TurnIntent.SAFETY:
            return DialogueTurnPlan(intent=intent, clarification_turn=
                await self._compiler.safety_support_turn(
                    original_input=state.compile_input, prior_outcome=state.outcome,
                    answer=answer, recent_turns=_model_recent_turns(state),
                ))
        if intent is TurnIntent.VIEWPOINT:
            return DialogueTurnPlan(intent=intent, clarification_turn=
                await self._compiler.viewpoint_support_turn(
                    original_input=state.compile_input, prior_outcome=state.outcome,
                    answer=answer, recent_turns=_model_recent_turns(state),
                ))
        if (state.pending_slot in {"candidate_data_not_ready", "candidate_data_incomplete"}
                and intent is not TurnIntent.SELECT_OPTION
                and self._compiler.has_clarification_dialogue
                and intent not in _FRESH_STRATEGY_INTENTS):
            # Retained failed choices are recovery inputs, never selectable
            # options. Let the existing model distinguish retry from reselection.
            return await self._plan_instrument_choice(state=state, answer=answer, intent=intent)
        if semantic_intent in {TurnIntent.CASUAL, TurnIntent.CANCEL, TurnIntent.UNKNOWN}:
            assert semantic_intent is not None
            message = await self._compiler.compose_dialogue_response(
                answer=answer,
                question=("先回答本轮日常问题，末尾用一句非强制邀请接回当前策略修改；"
                          "不要求补充参数，不重复旧问题，不启动回测。"
                          if semantic_intent is TurnIntent.CASUAL else ""),
                recent_turns=_model_recent_turns(state),
                context=(f"当前意图：{semantic_intent.value}。已保存交易想法："
                         f"{state.compile_input.utterance}。待补充项："
                         f"{state.outcome.clarification or '无'}。"
                         "自然承接本轮表达，不修改或执行策略。用户暂不想继续时尊重暂停；"
                         "先直接回答用户的日常问题，再用一句自然的话接回当前策略；"
                         "已有待改条件就接着该条件，没有缺项可邀请调整买卖条件，"
                         "不要重复要求已提供的股票或规则，也不要把闲聊当作确认执行。"
                         "意图不明确时只询问必要问题。不得声称已删除、取消后台任务或修改条件。"),
            )
            return DialogueTurnPlan(intent=intent, clarification_turn=ClarificationTurnOutcome(
                reply_kind="clarification", assistant_message=message,
                outcome=replace(state.outcome, run_requested=False, refresh_data=False),
                compile_input=state.compile_input, revision_changed=False,
            ))
        route = state.outcome.idea_route
        if (semantic_intent is None and route is not None and route.proposals
                and all(item.strategy_template is not None for item in route.proposals)
                and any(item.instrument_symbol is not None for item in route.proposals)
                and _REUSE_REJECT_RE.fullmatch(answer.strip()) is not None):
            return await self._detach_idea_instruments(state=state, answer=answer, intent=intent)
        if try_strategy_edit:
            edited = await self.plan_strategy_edit(
                state=state, answer=answer, semantic_intent=semantic_intent,
            )
            if edited is not None:
                return edited

        # A stock plus a pause/run instruction is one semantic choice. Do not
        # send prose through the name-shaped fast path or repeat the old prompt.
        if (self._compiler.has_clarification_dialogue
                and state.pending_slot in {
                    "instrument_required", "instrument_reuse_confirmation",
                    "instrument_unconfirmed", "instrument_resolution_unavailable",
                    "idea_instrument_extraction_unavailable",
                }
                and intent not in _FRESH_STRATEGY_INTENTS
                and _REUSE_ACCEPT_RE.fullmatch(answer.strip()) is None
                and _REUSE_REJECT_RE.fullmatch(answer.strip()) is None):
            return await self._plan_instrument_choice(state=state, answer=answer, intent=intent)

        if intent is TurnIntent.DATA_QUERY:
            followup = await self._compiler.answer_idea_data_followup(
                original_input=state.compile_input, prior_outcome=state.outcome,
                answer=answer, recent_turns=_model_recent_turns(state),
            )
            if followup is not None:
                return DialogueTurnPlan(intent=TurnIntent.SUPPLEMENT, clarification_turn=followup)
            return DialogueTurnPlan(intent=intent)

        if (state.outcome.status is CompileStatus.UNSUPPORTED
                and semantic_intent is TurnIntent.SUPPLEMENT):
            # A rejected execution capability does not erase the user's intent.
            # Give the model both turns; do not patch timeframes/conditions with
            # lexical replacements or compile the isolated correction alone.
            combined = (
                "以下是同一策略的连续两轮输入。本轮明确修改的部分以本轮为准，"
                "未修改的股票、条件和设置保留。\n"
                f"原请求：{state.compile_input.utterance}\n本轮修改：{answer}"
            )
            continued = await self._plan_fresh_strategy(
                state=state, answer=combined, intent=TurnIntent.NEW_STRATEGY,
                semantic_intent=TurnIntent.NEW_STRATEGY,
            )
            if continued is not None:
                return replace(continued, intent=TurnIntent.SUPPLEMENT)

        if intent is TurnIntent.CHANGE_INSTRUMENT:
            if semantic_intent is not None:
                return await self._plan_instrument_choice(state=state, answer=answer, intent=intent)
            target = extract_change_instrument_target(answer)
            if target is not None:
                instrument = await self._compiler.resolve_instrument_context(target)
                if instrument is not None:
                    return DialogueTurnPlan(
                        intent=intent,
                        clarification_turn=await self._apply_instrument(
                            state=state,
                            instrument=instrument,
                        ),
                        verified_instrument=_resolved_instrument_memory(
                            instrument=instrument,
                            target=target,
                            source="instrument_change",
                        ),
                    )
            return DialogueTurnPlan(
                intent=intent,
                clarification_turn=self._clarify_unresolved_instrument_change(
                    state=state,
                    target=target or answer.strip(),
                ),
            )

        if state.pending_instrument_reuse is not None and (
            _REUSE_ACCEPT_RE.fullmatch(answer.strip()) is not None
            or _REUSE_REJECT_RE.fullmatch(answer.strip()) is not None
        ):
            return await self._plan_instrument_reuse_answer(
                state=state,
                answer=answer,
                intent=intent,
            )

        if (semantic_intent is None and intent is TurnIntent.UNKNOWN
                and _may_be_instrument_answer(state, answer)):
            instrument = await self._compiler.resolve_instrument_context(answer)
            if instrument is not None:
                return DialogueTurnPlan(
                    intent=TurnIntent.CHANGE_INSTRUMENT,
                    clarification_turn=await self._apply_instrument(
                        state=state,
                        instrument=instrument,
                    ),
                    verified_instrument=_resolved_instrument_memory(
                        instrument=instrument,
                        target=answer.strip(),
                        source="instrument_answer",
                    ),
                )

        if intent in _FRESH_STRATEGY_INTENTS:
            fresh_plan = await self._plan_fresh_strategy(
                state=state,
                answer=answer,
                intent=intent,
                semantic_intent=semantic_intent,
            )
            if fresh_plan is not None:
                return fresh_plan

        if state.pending_slot == "instrument_reuse_confirmation":
            return await self._plan_instrument_reuse_answer(
                state=state,
                answer=answer,
                intent=intent,
            )

        turn = await self._compiler.answer_clarification(
            original_input=state.compile_input,
            prior_outcome=state.outcome,
            answer=answer,
            recent_turns=_model_recent_turns(state),
            semantic_intent=semantic_intent,
        )
        instrument = verified_instrument_symbol(turn.compile_input, turn.outcome)
        return DialogueTurnPlan(
            intent=intent, clarification_turn=turn,
            verified_instrument=(_compiled_instrument_memory(
                instrument=instrument, outcome=turn.outcome, source="model_instrument_selection",
            ) if turn.revision_changed and instrument is not None
                and instrument != state.verified_instrument_context else None),
        )

    async def plan_strategy_edit(
        self, *, state: DialogueState, answer: str, explicit_edit: bool = False,
        semantic_intent: TurnIntent | None = None,
    ) -> DialogueTurnPlan | None:
        intent = semantic_intent or classify_clarification_turn(answer)
        if semantic_intent in {
            TurnIntent.CASUAL, TurnIntent.DATA_QUERY, TurnIntent.CANCEL,
            TurnIntent.UNKNOWN, TurnIntent.SAFETY, TurnIntent.VIEWPOINT,
        }:
            # An editor must not override an already classified non-edit turn,
            # even when the user entered through the edit UI.
            return None
        if not explicit_edit and intent in {
            TurnIntent.CANCEL, TurnIntent.VAGUE_STRATEGY,
        }:
            return None
        if intent is TurnIntent.VIEWPOINT:
            return None
        turn = await self._compiler.edit_current_strategy(
            original_input=state.compile_input, prior_outcome=state.outcome,
            answer=answer, recent_turns=_model_recent_turns(state),
            backtest_results=state.backtest_results,
        )
        if turn is None:
            return None
        instrument = verified_instrument_symbol(turn.compile_input, turn.outcome)
        return DialogueTurnPlan(
            intent=TurnIntent.SUPPLEMENT, clarification_turn=turn,
            verified_instrument=(_compiled_instrument_memory(
                instrument=instrument, outcome=turn.outcome, source="model_instrument_selection",
            ) if instrument is not None
                and instrument != state.verified_instrument_context else None),
        )

    async def _plan_fresh_strategy(
        self,
        *,
        state: DialogueState,
        answer: str,
        intent: TurnIntent,
        semantic_intent: TurnIntent | None = None,
    ) -> DialogueTurnPlan | None:
        """Compile a fresh sentence without silently inheriting the old symbol."""

        unsupported_strategy = state.outcome.status is CompileStatus.UNSUPPORTED
        confirmed_idea_instrument = (
            state.outcome.pending_idea_instrument is not None
            and state.outcome.pending_idea_instrument == state.verified_instrument_context
        )
        pending_instrument = (
            state.verified_instrument_context
            if intent is TurnIntent.NEW_STRATEGY
            # Unchosen fallback suggestions are not a user-selected strategy.
            # A genuinely fresh strategy must use the bounded instrument memory
            # and ask before reuse; an incidental idea_route cannot extend it.
            and (state.outcome.selected_idea_proposal is not None
                 or confirmed_idea_instrument or unsupported_strategy)
            and state.outcome.strategy is None else None
        )
        compile_input = CompileInput(
            utterance=answer.strip(),
            instrument_context=pending_instrument,
            as_of_date=state.compile_input.as_of_date,
            semantic_intent=semantic_intent.value if semantic_intent is not None else None,
        )
        remembered = state.pending_instrument_reuse or state.last_verified_instrument
        if (
            intent in {TurnIntent.VAGUE_STRATEGY, TurnIntent.VIEWPOINT}
            and remembered is not None
            and state.pending_slot != "instrument_required"
            and not has_explicit_idea_instrument_reference(answer)
        ):
            return await _instrument_reuse_confirmation(
                compiler=self._compiler,
                intent=intent,
                compile_input=compile_input,
                remembered=remembered,
            )

        outcome = await self._compiler.compile(compile_input)
        if (pending_instrument is not None
                and outcome.diagnostic_code == "instrument_context_mismatch"):
            compile_input = replace(compile_input, instrument_context=None)
            outcome = await self._compiler.compile(compile_input)
        # A failed/incomplete rule translation is not evidence that its stock
        # was omitted. Use the same server-verified recovery as the first turn
        # before offering a stock from conversation memory.
        compile_input, outcome = await self._compiler.recover_unsupported_identity(
            compile_input, outcome,
        )
        if (outcome.status not in {CompileStatus.READY, CompileStatus.NEEDS_CLARIFICATION}
                and not (unsupported_strategy and outcome.status is CompileStatus.UNSUPPORTED)):
            return None

        instrument = verified_instrument_symbol(compile_input, outcome)
        if instrument is not None:
            return DialogueTurnPlan(
                intent=intent,
                clarification_turn=await _fresh_replacement_turn(
                    compiler=self._compiler,
                    compile_input=compile_input,
                    outcome=outcome,
                ),
                verified_instrument=_compiled_instrument_memory(
                    instrument=instrument,
                    outcome=outcome,
                    source="fresh_strategy_compile",
                ),
            )

        if (remembered is not None and state.pending_slot != "instrument_required"
                and outcome.diagnostic_code == "instrument_required"
                and not has_explicit_idea_instrument_reference(answer)):
            return await _instrument_reuse_confirmation(
                compiler=self._compiler,
                intent=intent,
                compile_input=compile_input,
                remembered=remembered,
            )

        return DialogueTurnPlan(
            intent=intent,
            clarification_turn=await _fresh_replacement_turn(
                compiler=self._compiler,
                compile_input=compile_input,
                outcome=outcome,
            ),
        )

    async def _detach_idea_instruments(
        self, *, state: DialogueState, answer: str, intent: TurnIntent,
        message: str | None = None, recommend: bool = False,
    ) -> DialogueTurnPlan:
        """Keep the generated rules while the user supplies their own stock."""
        route = state.outcome.idea_route
        assert route is not None
        message = message or await self._compiler.compose_dialogue_response(
            answer=answer, question="你想用哪只股票？告诉我名称或代码就行。",
            context="用户决定自己选股票；保留现有三个策略方向，不重新推荐股票，等待用户输入。",
            recent_turns=_model_recent_turns(state),
        )
        outcome = replace(
            state.outcome, status=CompileStatus.NEEDS_CLARIFICATION,
            clarification=message, diagnostic_code=(
                "candidate_reselection_requested" if recommend else "idea_guidance_required"
            ),
            instrument_suggestion_declined=not recommend,
            stock_recommendations=(), instrument_candidates=(),
            run_requested=False, refresh_data=False,
            strategy=None, strategy_hash=None, revision_base_strategy=None,
            suggested_strategy=None, suggested_strategy_hash=None,
            suggested_strategy_choice_id=None, suggested_strategy_note=None,
            selected_idea_proposal=None, pending_idea_instrument=None,
            candidate_grounding=tuple(
                item for item in state.outcome.candidate_grounding
                if not item.path.startswith("/instrument")
            ),
            provenance=tuple(
                item for item in state.outcome.provenance
                if not item.path.startswith("/instrument")
            ),
            idea_route=replace(
                route,
                proposals=tuple(replace(
                    item, instrument_symbol=None, instrument_name=None, pairing_reason=None,
                    strategy=None, strategy_hash=None, capability_ids=(),
                ) for item in route.proposals),
                asset_mapping=replace(
                    route.asset_mapping, instrument_symbol=None, relation="unbound",
                    evidence_status="instrument_required", rationale="等待用户选择自己的股票。",
                ),
            ),
        )
        return DialogueTurnPlan(
            intent=intent,
            clarification_turn=ClarificationTurnOutcome(
                reply_kind="accepted", assistant_message=message, outcome=outcome,
                compile_input=replace(
                    state.compile_input, instrument_context=None,
                    utterance=answer.strip() if recommend else state.compile_input.utterance,
                    idea_context=(*state.compile_input.idea_context,
                                  f"此前交易想法：{state.compile_input.utterance}")
                    if recommend else state.compile_input.idea_context,
                ),
                revision_changed=True,
            ),
        )

    async def _plan_instrument_reuse_answer(
        self,
        *,
        state: DialogueState,
        answer: str,
        intent: TurnIntent,
    ) -> DialogueTurnPlan:
        normalized = answer.strip()
        remembered = state.pending_instrument_reuse
        if remembered is not None and _REUSE_ACCEPT_RE.fullmatch(normalized) is not None:
            turn = await self._apply_instrument(
                state=state,
                instrument=remembered.symbol,
            )
            return DialogueTurnPlan(
                intent=TurnIntent.CHANGE_INSTRUMENT,
                clarification_turn=turn,
                verified_instrument=replace(
                    remembered,
                    source="reuse_confirmation",
                    verified_at=datetime.now(UTC),
                    evidence=normalized,
                ),
            )
        if _REUSE_REJECT_RE.fullmatch(normalized) is not None or remembered is None:
            message = await self._compiler.compose_dialogue_response(
                answer=answer, question="你想用哪只股票？告诉我名称或代码就行。",
                context="用户不沿用推荐股票；原策略保留，还未绑定新股票。",
                recent_turns=_model_recent_turns(state),
            )
            outcome = replace(
                state.outcome, clarification=message,
                diagnostic_code=("idea_guidance_required" if state.outcome.idea_route
                                 else "instrument_required"),
                instrument_suggestion_declined=True,
            )
            return DialogueTurnPlan(
                intent=intent,
                clarification_turn=ClarificationTurnOutcome(
                    reply_kind="accepted",
                    assistant_message=message,
                    outcome=outcome,
                    compile_input=state.compile_input,
                    revision_changed=True,
                ),
            )
        return DialogueTurnPlan(
            intent=intent,
            clarification_turn=ClarificationTurnOutcome(
                reply_kind="clarification",
                assistant_message=await self._compiler.compose_dialogue_response(
                    answer=answer, question=f"是否沿用 {_instrument_label(remembered)}？",
                    context="用户尚未确认是否沿用上次股票；仅询问选择，不自动绑定。",
                    recent_turns=_model_recent_turns(state),
                ),
                outcome=state.outcome,
                compile_input=state.compile_input,
                revision_changed=False,
            ),
            pending_instrument_reuse=remembered,
        )

    async def _plan_instrument_choice(
        self, *, state: DialogueState, answer: str, intent: TurnIntent,
    ) -> DialogueTurnPlan:
        remembered = state.pending_instrument_reuse
        choices = {f"instrument:{item.symbol}": (item.symbol, item.name)
                   for item in state.outcome.instrument_candidates}
        if remembered is not None:
            choices.setdefault(
                f"instrument:{remembered.symbol}",
                (remembered.symbol, _instrument_label(remembered)),
            )
        assessment = await self._compiler.assess_instrument_clarification(
            original_input=state.compile_input, prior_outcome=state.outcome, answer=answer,
            pending_label=_instrument_label(remembered) if remembered else None,
            recent_turns=_model_recent_turns(state),
            options=tuple(ClarificationOption(id=key, title=name, preview=symbol)
                          for key, (symbol, name) in choices.items()),
        )
        outcome = state.outcome
        if assessment is None:
            message = "这次没能完成回复。你的方案和输入都已保留，可以直接重试。"
        else:
            message = assessment.natural_reply
            # Unclear/ordinary dialogue never grants execution authority.
            run_requested = (
                assessment.run_requested if assessment.run_requested is not None
                else outcome.pending_edit_run_requested or outcome.run_requested
            )
            name = assessment.instrument_name
            chosen = choices.get(assessment.selected_option_id or "")
            _LOGGER.info(
                "instrument_choice_assessment pending_slot=%s reply_kind=%s "
                "instrument_selected=%s name_present=%s name_in_answer=%s "
                "known_option=%s strategy_inspiration=%s recommendation_requested=%s",
                state.pending_slot, assessment.reply_kind, assessment.instrument_selected,
                name is not None, bool(name is not None and name in answer),
                chosen is not None, assessment.strategy_inspiration is not None,
                assessment.instrument_recommendation_requested,
            )
            if (assessment.reply_kind == "preference"
                    and (chosen is not None or (
                        assessment.instrument_selected and name is not None and name in answer
                    ))
                    and assessment.strategy_inspiration is None):
                name = chosen[1] if chosen else name
                assert name is not None
                candidates = ()
                try:
                    instrument = chosen[0] if chosen else (
                        await self._compiler.resolve_instrument_context(name, require_details=True)
                    )
                except InstrumentNameAmbiguous as exc:
                    instrument, candidates = None, exc.candidates[:3]
                    labels = '、'.join(item.name for item in candidates)
                    message = f"你说的“{name}”，是{labels}中的哪只？"
                except (OSError, TimeoutError):
                    instrument = None
                    message = (
                        "股票名称查询中断，暂时无法确认输入是否有效；"
                        "请检查名称或代码后重试。原规则已保留。"
                    )
                except LookupError:
                    instrument = None
                    message = f"未找到“{name}”对应的 A 股，请修改股票名称或代码。原规则已保留。"
                if instrument is not None:
                    binding_state = state
                    if (state.pending_slot in {"candidate_data_not_ready", "candidate_data_incomplete"}
                            and outcome.idea_route is not None):
                        # A newly supplied stock must still pass candidate
                        # preparation; do not turn a retained selected template
                        # straight into READY while its old cards were hidden.
                        binding_state = replace(state, outcome=replace(
                            outcome, selected_idea_proposal=None,
                        ))
                    turn = await self._apply_instrument(
                        state=binding_state, instrument=instrument, assistant_message=message,
                    )
                    ready = turn.outcome.status is CompileStatus.READY
                    return DialogueTurnPlan(
                        intent=TurnIntent.CHANGE_INSTRUMENT,
                        clarification_turn=replace(
                            turn,
                            outcome=replace(
                                turn.outcome,
                                run_requested=bool(ready and run_requested),
                                refresh_data=bool(ready and run_requested
                                                  and outcome.pending_edit_refresh_data),
                                pending_edit_run_requested=bool(not ready and run_requested),
                                pending_edit_refresh_data=bool(
                                    not ready and run_requested
                                    and outcome.pending_edit_refresh_data
                                ),
                            ),
                        ),
                        verified_instrument=_resolved_instrument_memory(
                            instrument=instrument, target=name, source="model_instrument_selection",
                        ),
                    )
                if not candidates and message == assessment.natural_reply:
                    message = f"这次还没核实“{name}”，请补充完整股票名称或代码。"
                outcome = replace(
                    outcome, diagnostic_code="instrument_required", clarification=message,
                    instrument_candidates=candidates, instrument_suggestion_declined=True,
                    run_requested=False, refresh_data=False,
                    pending_edit_run_requested=run_requested,
                    pending_edit_refresh_data=bool(
                        run_requested and outcome.pending_edit_refresh_data
                    ),
                )
                remembered = None
            elif assessment.instrument_recommendation_requested:
                if outcome.idea_route is not None:
                    return await self._detach_idea_instruments(
                        state=state, answer=answer, intent=TurnIntent.CHANGE_INSTRUMENT,
                        message=message, recommend=True,
                    )
                if (outcome.selected_idea_proposal is None
                        and state.pending_slot in {
                            "instrument_unconfirmed", "instrument_resolution_unavailable",
                            "idea_instrument_extraction_unavailable",
                        }):
                    # No validated template exists after identity failure.
                    # Carry the original style as context, while the latest
                    # explicit request now authorizes choosing other stocks.
                    request = CompileInput(
                        utterance=answer.strip(), as_of_date=state.compile_input.as_of_date,
                        semantic_intent="vague_strategy",
                        idea_inspiration=state.compile_input.utterance,
                        idea_context=(
                            "此前股票身份未完成核对，本轮用户明确改为请求推荐股票。"
                            "只保留此前策略风格与设置意图，不沿用此前未核实的股票，"
                            "不声称已有可执行规则；先给可编辑策略方向。",
                        ),
                    )
                    generated = await self._compiler.compile(request)
                    return DialogueTurnPlan(
                        intent=TurnIntent.CHANGE_INSTRUMENT,
                        clarification_turn=ClarificationTurnOutcome(
                            reply_kind="clarification", outcome=generated,
                            assistant_message=generated.clarification or message,
                            compile_input=request, revision_changed=True,
                        ),
                    )
                # Asking for candidates is independent of permission to execute.
                # Reopen the existing verified sample-offer path, preserving the
                # selected template and never treating a recommendation as a choice.
                outcome = replace(
                    outcome, instrument_suggestion_declined=False,
                    run_requested=False, refresh_data=False,
                    pending_edit_run_requested=False, pending_edit_refresh_data=False,
                )
                return DialogueTurnPlan(
                    intent=TurnIntent.CHANGE_INSTRUMENT,
                    clarification_turn=ClarificationTurnOutcome(
                        reply_kind="clarification", assistant_message=message,
                        outcome=outcome, compile_input=state.compile_input,
                        revision_changed=True,
                    ),
                )
            elif assessment.requires_new_data:
                return DialogueTurnPlan(intent=TurnIntent.DATA_QUERY)
            elif ((assessment.run_requested is False and assessment.strategy_inspiration is None)
                  or assessment.reply_kind == "cancelled"):
                outcome = replace(
                    outcome, run_requested=False, refresh_data=False,
                    pending_edit_run_requested=False, pending_edit_refresh_data=False,
                )
            elif state.pending_slot in {"candidate_data_not_ready", "candidate_data_incomplete"}:
                # A model-confirmed retry only reopens preparation of the same
                # stock/date/template; it does not select or execute a strategy.
                return DialogueTurnPlan(
                    intent=TurnIntent.SUPPLEMENT,
                    clarification_turn=ClarificationTurnOutcome(
                        reply_kind="clarification", assistant_message=message,
                        outcome=replace(outcome, run_requested=False, refresh_data=False),
                        compile_input=state.compile_input,
                        revision_changed=assessment.reply_kind == "preference",
                    ),
                )
            else:
                # A missing stock does not turn every later message into a
                # stock answer. Reuse the model assessment for normal dialogue,
                # rule supplements and new inspirations without classifying twice.
                turn = await self._compiler.answer_clarification(
                    original_input=state.compile_input, prior_outcome=state.outcome,
                    answer=answer, recent_turns=_model_recent_turns(state),
                    dialogue_assessment=assessment,
                )
                return DialogueTurnPlan(
                    intent=intent, clarification_turn=turn,
                    pending_instrument_reuse=remembered if not turn.revision_changed else None,
                )
        return DialogueTurnPlan(
            intent=intent, pending_instrument_reuse=remembered,
            clarification_turn=ClarificationTurnOutcome(
                reply_kind="clarification", assistant_message=message,
                outcome=outcome, compile_input=state.compile_input,
                revision_changed=outcome != state.outcome,
            ),
        )

    async def _apply_instrument(
        self,
        *,
        state: DialogueState,
        instrument: str,
        assistant_message: str | None = None,
    ) -> ClarificationTurnOutcome:
        compile_input = CompileInput(
            utterance=state.compile_input.utterance,
            instrument_context=instrument,
            as_of_date=state.compile_input.as_of_date,
        )
        outcome = self._compiler.rebind_current_strategy(compile_input, state.outcome)
        if outcome is None:
            outcome = self._compiler.bind_selected_idea(compile_input, state.outcome)
        if outcome is None:
            outcome = await self._compiler.compile(compile_input)
        outcome = replace(
            outcome, execution_settings=outcome.execution_settings.merged(
                state.outcome.execution_settings,
            ),
        )
        if outcome.status is CompileStatus.READY:
            message = assistant_message or await self._compiler.compose_ready_response(
                answer=instrument, outcome=outcome, recent_turns=_model_recent_turns(state),
            )
        else:
            question = outcome.clarification or "还需要再补充一项策略条件。"
            message = (question if outcome.idea_route is not None else
                       await self._compiler.compose_dialogue_response(
                           answer=instrument, question=question,
                           context=f"当前标的：{instrument}；交易想法：{compile_input.utterance}",
                           recent_turns=_model_recent_turns(state),
                       ))
        return ClarificationTurnOutcome(
            reply_kind="accepted",
            assistant_message=message,
            outcome=outcome,
            compile_input=compile_input,
            revision_changed=True,
        )

    @staticmethod
    def _clarify_unresolved_instrument_change(
        *,
        state: DialogueState,
        target: str,
    ) -> ClarificationTurnOutcome:
        return ClarificationTurnOutcome(
            reply_kind="clarification",
            assistant_message=(
                f"我还不能唯一确认“{target}”对应的 A 股。"
                "请补充 6 位证券代码；原标的和已有买卖规则都会保持不变。"
            ),
            outcome=state.outcome,
            compile_input=state.compile_input,
            revision_changed=False,
        )


def _model_recent_turns(state: DialogueState) -> tuple[ClarificationDialogueTurn, ...]:
    return tuple(
        ClarificationDialogueTurn(
            user_text=item.user_text, assistant_text=item.assistant_text,
            intent=item.intent, revision=item.revision, created_at=item.created_at,
            verified_instrument=({
                "symbol": item.verified_instrument.symbol,
                "name": item.verified_instrument.name,
                "source": item.verified_instrument.source,
                "evidence": item.verified_instrument.evidence,
            } if item.verified_instrument is not None else None),
        )
        for item in state.recent_turns[-20:]
    )


_SECURITY_CODE_RE = re.compile(r"^\d{6}(?:\.(?:SH|SZ|BJ))?$", re.IGNORECASE)
_PLAIN_CN_NAME_RE = re.compile(r"^[\u4e00-\u9fff]{2,12}$")
_DISCOURSE_OR_PRONOUN_RE = re.compile(r"^(?:而且|并且|另外|还有|同时|再加上|顺便|不过|但是|我|你)")
_REUSE_ACCEPT_RE = re.compile(
    r"^(?:是|是的|对|对的|好|好的|可以|行|确认|沿用|继续用|就用|"
    r"用上次的|用这只|用它|用这只回测|用它回测)(?:吧)?[。！!]*$"
)
_REUSE_REJECT_RE = re.compile(
    r"^(?:不|否|不是|不用|不要|不沿用|不使用|换一只|换一个|换标的|"
    r"重新选|另选|我自己选|我自己选股票|用我自己的股票)[。！!]*$"
)
_FRESH_STRATEGY_INTENTS = frozenset(
    {TurnIntent.NEW_STRATEGY, TurnIntent.VAGUE_STRATEGY}
)


def _may_be_instrument_answer(state: DialogueState, answer: str) -> bool:
    """Bound authoritative name resolution to code- or name-shaped answers.

    Arbitrary UNKNOWN prose must not trigger a live security-name lookup.  We
    retain the useful out-of-order company-name path only while no instrument
    has already been verified in server-owned dialogue state.
    """

    normalized = answer.strip()
    if _SECURITY_CODE_RE.fullmatch(normalized) is not None:
        return True
    return bool(
        state.verified_instrument_context is None
        and _DISCOURSE_OR_PRONOUN_RE.match(normalized) is None
        and _PLAIN_CN_NAME_RE.fullmatch(normalized) is not None
    )


async def _fresh_replacement_turn(
    *,
    compiler: StrategyCompiler,
    compile_input: CompileInput,
    outcome: CompileOutcome,
) -> ClarificationTurnOutcome:
    if outcome.status is CompileStatus.READY:
        message = await compiler.compose_ready_response(
            answer=compile_input.utterance, outcome=outcome,
        )
    else:
        question = outcome.clarification or "还需要再补充一项信息。"
        message = (question if outcome.idea_route is not None else
                   await compiler.compose_dialogue_response(
                       answer=compile_input.utterance, question=question,
                       context=f"这是新交易想法：{compile_input.utterance}；不要继续旧话题。",
                   ))
    return ClarificationTurnOutcome(
        reply_kind="accepted",
        assistant_message=message,
        outcome=outcome,
        compile_input=compile_input,
        revision_changed=True,
        suggestions=_outcome_suggestions(outcome),
    )


def _outcome_suggestions(outcome: CompileOutcome) -> tuple[ClarificationSuggestion, ...]:
    if outcome.idea_route is None:
        return ()
    return tuple(
        ClarificationSuggestion(
            id=item.id,
            title=item.title,
            preview=item.suggested_utterance,
        )
        for item in outcome.idea_route.proposals
    )


def _resolved_instrument_memory(
    *,
    instrument: str,
    target: str,
    source: str,
) -> VerifiedInstrumentMemory:
    # The resolver has already checked name/code agreement. Keep the original
    # evidence, but do not repeat its ticker in the display-name field.
    label = unicodedata.normalize("NFKC", target).strip()
    code = rf"{re.escape(instrument[:6])}(?:\.{re.escape(instrument[-2:])})?"
    name_pattern = r"[\u4e00-\u9fffA-Za-z*·\-]{2,20}"
    pair = re.fullmatch(rf"(?P<name>{name_pattern})\s*\(?\s*{code}\s*\)?", label, re.I) or re.fullmatch(
        rf"{code}\s*\(?\s*(?P<name>{name_pattern})\s*\)?", label, re.I,
    )
    return VerifiedInstrumentMemory(
        symbol=instrument,
        name=(pair.group("name") if pair else
              None if _SECURITY_CODE_RE.fullmatch(target) is not None else target),
        source=source,
        verified_at=datetime.now(UTC),
        evidence=target,
    )


def _compiled_instrument_memory(
    *,
    instrument: str,
    outcome: CompileOutcome,
    source: str,
) -> VerifiedInstrumentMemory:
    grounding = next(
        (item for item in outcome.candidate_grounding if item.path == "/instrument/symbol"),
        None,
    )
    evidence = None if grounding is None else grounding.text
    name = (
        evidence
        if evidence is not None and _PLAIN_CN_NAME_RE.fullmatch(evidence) is not None
        else None
    )
    return VerifiedInstrumentMemory(
        symbol=instrument,
        name=name,
        source=source,
        verified_at=datetime.now(UTC),
        evidence=evidence,
    )


def _instrument_label(instrument: VerifiedInstrumentMemory) -> str:
    if instrument.name is not None:
        return f"{instrument.name}（{instrument.symbol}）"
    return instrument.symbol


async def _instrument_reuse_confirmation(
    *,
    compiler: StrategyCompiler,
    intent: TurnIntent,
    compile_input: CompileInput,
    remembered: VerifiedInstrumentMemory,
) -> DialogueTurnPlan:
    label = _instrument_label(remembered)
    question = await compiler.compose_dialogue_response(
        answer=compile_input.utterance, question=f"是否沿用上次的 {label}？",
        context=f"用户的新交易想法：{compile_input.utterance}；上次已确认股票：{label}。"
                "新策略尚未绑定股票，可沿用上次或由用户另选。",
    )
    return DialogueTurnPlan(
        intent=intent,
        clarification_turn=ClarificationTurnOutcome(
            reply_kind="accepted",
            assistant_message=question,
            outcome=CompileOutcome(
                status=CompileStatus.NEEDS_CLARIFICATION,
                clarification=question,
                diagnostic_code="instrument_reuse_confirmation",
            ),
            compile_input=compile_input,
            revision_changed=True,
        ),
        pending_instrument_reuse=remembered,
    )


__all__ = ["DialogueTurnOrchestrator", "DialogueTurnPlan"]
