"""Server-owned clarification choices for incomplete strategy sentences.

The choices are deliberately plain sentences, not executable ASTs.  The
compiler must translate and validate every sentence again before exposing it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ashare_lab.domain.market_data import AshareInstrumentCodeError, normalize_a_share_instrument
from ashare_lab.ports.candidate_generation import CandidateGroundingEvidence, CompileInput
from ashare_lab.ports.idea_routing import IdeaAssetMapping, IdeaProposal, IdeaRoute

_ACTION_RE = re.compile(r"(?<!超)(?:买入|卖出|买进|卖掉|(?<!购)买|卖)")
_ENTRY_ACTION_RE = re.compile(r"(?:买入|买进|(?<!购)买)")
_EXIT_ACTION_RE = re.compile(r"(?:卖出|卖掉|卖)")
_INDICATOR_RE = re.compile(
    r"(?:MACD|RSI|KDJ|CCI|OBV|BBI|EMA|均线|布林|成交量|量比|金叉|死叉)",
    re.IGNORECASE,
)
_CROSS_RE = re.compile(r"(?:金叉|死叉)")
_EVENT_RE = re.compile(r"(?:公告|报告|年报|季报|业绩预告|业绩快报|回购|增持|减持)")


@dataclass(frozen=True, slots=True)
class ClarificationGuidance:
    question: str
    route: IdeaRoute
    grounding: tuple[CandidateGroundingEvidence, ...]


@dataclass(frozen=True, slots=True)
class _Choice:
    label: str
    description: str
    utterance: str
    entry_summary: str
    exit_summary: str
    capability_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ActionClause:
    side: str
    start: int
    end: int
    text: str


def build_clarification_guidance(
    request: CompileInput,
    diagnostic_code: str,
) -> ClarificationGuidance | None:
    """Build bounded choices without claiming that any choice is executable."""

    instrument_symbol = _instrument_symbol(request.instrument_context)
    if instrument_symbol is None:
        return None
    original = request.utterance.strip()
    if not original:
        return None
    if diagnostic_code in {
        "entry_rule_not_recognized",
        "exit_rule_not_recognized",
        "strategy_rule_incomplete",
    } and _EVENT_RE.search(original):
        # The compiler can validate event syntax, but it cannot prove per-code
        # runtime snapshot coverage here.  Do not present such a choice as
        # selectable until the runtime coverage gate is available.
        return None

    grounding = _grounding(original, diagnostic_code)
    evidence = grounding[0].text if grounding else original
    choices: tuple[_Choice, ...]
    if diagnostic_code == "entry_rule_not_recognized":
        preserved_exit = _preserved_exit_rule(original)
        if re.search(r"(?:跌破|止损|回撤)", evidence):
            insight = "先限定什么时候认错，比只想着买点更完整"
        elif re.search(r"(?:持有|到期)", evidence):
            insight = "把持有期限写清楚，能避免一笔交易无限拖下去"
        else:
            insight = "至少一笔交易会在什么情况下结束，已经说清楚了"
        reason = (
            f"你已经把离场条件说清了——“{evidence}”，{insight}。"
            "缺的是什么时候进场；这个我不能替你定，因为换一种买入条件就是换了一条策略。"
        )
        question = "什么时候买？"
        choices = (
            _Choice(
                "均线转强",
                "5 日线上穿 20 日线，趋势刚转强时进",
                f"5日均线上穿20日均线买入，{preserved_exit}",
                "5 日均线上穿 20 日线",
                evidence,
                ("technical.ma_cross", "technical.ma"),
            ),
            _Choice(
                "MACD 金叉",
                "DIF 上穿 DEA，动能由弱转强时进",
                f"MACD金叉买入，{preserved_exit}",
                "MACD 金叉",
                evidence,
                ("technical.macd",),
            ),
            _Choice(
                "创 20 日新高",
                "突破近 20 日高点时进",
                f"创20日新高买入，{preserved_exit}",
                "创 20 日新高",
                evidence,
                ("price.rolling_high",),
            ),
        )
    elif diagnostic_code == "exit_rule_not_recognized":
        preserved_entry = _preserved_entry_rule(original)
        if re.search(r"(?:MACD|金叉)", evidence, re.IGNORECASE):
            insight = "这是在动能转强时进场"
        elif re.search(r"(?:上穿|突破|新高)", evidence):
            insight = "这是在趋势转强时进场"
        elif re.search(r"(?:RSI|超跌)", evidence, re.IGNORECASE):
            insight = "这是在弱势或超跌区间寻找入场"
        else:
            insight = "什么时候开始一笔交易，已经说清楚了"
        reason = (
            f"你已经把进场条件说清了——“{evidence}”，{insight}。"
            "缺的是什么时候离场；这个我不能替你定，因为技术转弱、持有到期和止损代表三种不同策略。"
        )
        question = "什么时候卖？"
        choices = (
            _Choice(
                "动能转弱",
                "MACD 死叉时离场",
                f"{preserved_entry}，MACD死叉卖出",
                evidence,
                "MACD 死叉",
                ("technical.macd",),
            ),
            _Choice(
                "持有 5 日",
                "买入成交后持有 5 个交易日",
                f"{preserved_entry}，持有5个交易日卖出",
                evidence,
                "持有 5 个交易日",
                ("strategy.holding_period",),
            ),
            _Choice(
                "亏损 8%",
                "持仓亏损达到 8% 时止损",
                f"{preserved_entry}，止损8%卖出",
                evidence,
                "持仓亏损 8%",
                ("strategy.stop_loss",),
            ),
        )
    elif diagnostic_code in {"ambiguous_obv_direction", "ambiguous_volume_direction"}:
        is_obv = diagnostic_code == "ambiguous_obv_direction"
        metric = "OBV" if is_obv else "成交量"
        reason = (
            f"你想用“{evidence}”决定买入，但方向还没定。上升与下降代表相反判断，这个我不能替你选。"
        )
        question = f"{metric} 往哪个方向时买？"
        if is_obv:
            choices = (
                _Choice(
                    "OBV 上升",
                    "量能累积向上时进，转为下降时出",
                    _replace_direction(
                        original,
                        metric="OBV",
                        direction="上升",
                        exit_direction="下降",
                    ),
                    "OBV 上升",
                    "OBV 下降",
                    ("technical.obv",),
                ),
                _Choice(
                    "OBV 下降",
                    "量能累积向下时进，转为上升时出",
                    _replace_direction(
                        original,
                        metric="OBV",
                        direction="下降",
                        exit_direction="上升",
                    ),
                    "OBV 下降",
                    "OBV 上升",
                    ("technical.obv",),
                ),
            )
        else:
            choices = (
                _Choice(
                    "放量上涨",
                    "成交量放大且价格上涨时进",
                    _replace_volume_direction(original, "上涨"),
                    "放量上涨 5%",
                    "MACD 死叉",
                    ("volume.price_confirmation", "technical.macd"),
                ),
                _Choice(
                    "放量下跌",
                    "成交量放大且价格下跌时进",
                    _replace_volume_direction(original, "下跌"),
                    "放量下跌 5%",
                    "MACD 死叉",
                    ("volume.price_confirmation", "technical.macd"),
                ),
            )
    elif diagnostic_code == "ambiguous_boolean_expression":
        variants = _boolean_variants(original)
        if variants is None:
            return None
        reason = (
            f"你给了“{evidence}”里的多组条件，但没有说明它们要同时满足，还是任一满足。"
            "这会改变触发次数，所以我不会替你选。"
        )
        question = "这些条件是“且”还是“或”？"
        choices = (
            _Choice(
                "都满足",
                "所有条件同时成立才触发",
                variants[0],
                "所有条件同时成立",
                "沿用原卖出条件",
                ("technical.macd",),
            ),
            _Choice(
                "任一满足",
                "其中一个条件成立就触发",
                variants[1],
                "任一条件成立",
                "沿用原卖出条件",
                ("technical.macd",),
            ),
        )
    elif diagnostic_code == "ambiguous_cross_indicator":
        cross_actions = _cross_actions(original)
        if cross_actions is None:
            return None
        entry_cross, exit_cross = cross_actions
        reason = (
            f"你已经说清了‘{entry_cross}买、{exit_cross}卖’，但金叉/死叉可以指 "
            "MACD、KDJ 或两条均线的交叉。这三种信号不是一回事，所以我不会替你默认。"
        )
        question = "‘金叉/死叉’指的是哪一类指标？"
        choices = _cross_family_choices(entry_cross, exit_cross)
    elif diagnostic_code == "strategy_rule_incomplete":
        reason = (
            f"你提到了“{evidence}”，但还没有把它和买卖时点连起来。"
            "我可以帮你补成完整规则，但不会替你悄悄选一种。"
        )
        question = "想怎么把它变成买卖规则？"
        choices = _complete_rule_choices(original)
    elif diagnostic_code == "no_supported_signal_recognized":
        reason = (
            "这句话里还没有能直接回测的买卖条件。你可以从一种常见逻辑开始，"
            "点选后仍会重新经过现有规则和能力校验。"
        )
        question = "先用哪种方式说清买卖？"
        choices = _generic_choices()
    else:
        return None

    proposals = tuple(_proposal(item) for item in choices)
    return ClarificationGuidance(
        question=question,
        route=IdeaRoute(
            understanding=reason,
            hypothesis="这些候选只补足当前缺口，用户已经说清楚的部分保持不变。",
            asset_mapping=IdeaAssetMapping(
                instrument_symbol=instrument_symbol,
                rationale="只沿用当前股票页标的，不替换证券。",
            ),
            proposals=proposals,
        ),
        grounding=grounding,
    )


def _proposal(choice: _Choice) -> IdeaProposal:
    digest = hashlib.sha256(choice.utterance.encode("utf-8")).hexdigest()[:12]
    return IdeaProposal(
        id=f"idea_{digest}",
        title=choice.label,
        hypothesis=choice.description,
        entry_summary=choice.entry_summary,
        exit_summary=choice.exit_summary,
        suggested_utterance=choice.utterance,
        capability_ids=choice.capability_ids,
        assumptions=(),
        confidence=1.0,
    )


def _instrument_symbol(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return normalize_a_share_instrument(value).value
    except AshareInstrumentCodeError:
        return None


def _grounding(
    utterance: str,
    diagnostic_code: str,
) -> tuple[CandidateGroundingEvidence, ...]:
    match: re.Match[str] | None = None
    if diagnostic_code == "entry_rule_not_recognized":
        clause = _unique_action_clause(utterance, side="exit")
        path = "/exit/0"
    elif diagnostic_code == "exit_rule_not_recognized":
        clause = _unique_action_clause(utterance, side="entry")
        path = "/entry/0"
    else:
        clause = None
        match = _INDICATOR_RE.search(utterance) or _ACTION_RE.search(utterance)
        path = "/clarification"
    if clause is not None:
        return (
            CandidateGroundingEvidence(
                path=path,
                start=clause.start,
                end=clause.end,
                text=clause.text,
            ),
        )
    if diagnostic_code in {"entry_rule_not_recognized", "exit_rule_not_recognized"}:
        return ()
    if match is None:
        return ()
    start = (
        max(
            utterance.rfind("，", 0, match.start()),
            utterance.rfind(",", 0, match.start()),
            utterance.rfind("。", 0, match.start()),
            utterance.rfind("；", 0, match.start()),
            utterance.rfind(";", 0, match.start()),
        )
        + 1
    )
    end_match = _ACTION_RE.search(utterance, match.end())
    end = len(utterance) if end_match is None else end_match.start()
    text = utterance[start:end].strip("，,。；; ")
    if not text:
        return ()
    text_start = utterance.find(text, start, end)
    return (
        CandidateGroundingEvidence(
            path=path,
            start=text_start,
            end=text_start + len(text),
            text=text,
        ),
    )


def _replace_direction(
    original: str,
    *,
    metric: str,
    direction: str,
    exit_direction: str,
) -> str:
    rewritten = re.sub(
        rf"{metric}[^，,。；;]{{0,8}}(?:买入|买进|买)",
        f"{metric}{direction}时买入",
        original,
        count=1,
        flags=re.IGNORECASE,
    )
    if rewritten == original:
        rewritten = f"{metric}{direction}时买入"
    if _EXIT_ACTION_RE.search(rewritten) is None:
        rewritten = f"{rewritten}，{metric}{exit_direction}时卖出"
    return rewritten


def _replace_volume_direction(original: str, direction: str) -> str:
    rewritten = re.sub(
        r"(?:成交量|量能)[^，,。；;]{0,8}(?:买入|买进|买)",
        f"放量{direction}5%买入",
        original,
        count=1,
    )
    if rewritten == original:
        rewritten = f"放量{direction}5%买入"
    if _EXIT_ACTION_RE.search(rewritten) is None:
        rewritten = f"{rewritten}，MACD死叉卖出"
    return rewritten


def _boolean_variants(original: str) -> tuple[str, str] | None:
    entries = _action_rule_bodies(original, entry=True)
    exits = _action_rule_bodies(original, entry=False)
    if len(entries) >= 2:
        left, right = entries[:2]
    elif len(entries) == 1:
        matches = tuple(_INDICATOR_RE.finditer(entries[0]))
        if len(matches) < 2:
            return None
        split = matches[1].start()
        left, right = entries[0][:split], entries[0][split:]
    else:
        return None
    exit_text = exits[0] if exits else "MACD死叉"
    return (
        f"{left}并且{right}买入，{exit_text}卖出",
        f"{left}或者{right}买入，{exit_text}卖出",
    )


def _cross_actions(original: str) -> tuple[str, str] | None:
    """Return the cross direction explicitly attached to each user action."""

    entries = _action_rule_bodies(original, entry=True)
    exits = _action_rule_bodies(original, entry=False)
    entry = next(
        (match.group() for body in entries if (match := _CROSS_RE.search(body)) is not None),
        None,
    )
    exit_ = next(
        (match.group() for body in exits if (match := _CROSS_RE.search(body)) is not None),
        None,
    )
    if entry is None or exit_ is None:
        return None
    return entry, exit_


def _cross_family_choices(entry_cross: str, exit_cross: str) -> tuple[_Choice, ...]:
    entry_ma_op = "上穿" if entry_cross == "金叉" else "下穿"
    exit_ma_op = "上穿" if exit_cross == "金叉" else "下穿"
    return (
        _Choice(
            "MACD",
            f"MACD {entry_cross}时进，{exit_cross}时出",
            f"MACD{entry_cross}买入，MACD{exit_cross}卖出",
            f"MACD {entry_cross}",
            f"MACD {exit_cross}",
            ("technical.macd",),
        ),
        _Choice(
            "KDJ",
            f"KDJ {entry_cross}时进，{exit_cross}时出",
            f"KDJ{entry_cross}买入，KDJ{exit_cross}卖出",
            f"KDJ {entry_cross}",
            f"KDJ {exit_cross}",
            ("technical.kdj",),
        ),
        _Choice(
            "5/20 日均线",
            f"5 日均线{entry_ma_op} 20 日均线时进，{exit_ma_op}时出",
            (f"5日均线{entry_ma_op}20日均线买入，5日均线{exit_ma_op}20日均线卖出"),
            f"5 日均线{entry_ma_op} 20 日均线",
            f"5 日均线{exit_ma_op} 20 日均线",
            ("technical.ma_cross",),
        ),
    )


def _action_rule_bodies(original: str, *, entry: bool) -> tuple[str, ...]:
    result: list[str] = []
    cursor = 0
    for match in _ACTION_RE.finditer(original):
        body = original[cursor : match.start()].strip("，,。；; ")
        cursor = match.end()
        is_entry = match.group().startswith("买")
        if body and is_entry is entry:
            result.append(body)
    return tuple(result)


def merge_clarification_supplement(original: str, answer: str) -> str:
    """Replace the answered action side while preserving one unambiguous opposite side.

    This is text normalization only.  The returned sentence must still pass the
    normal compiler and Catalog gates before it can become executable.
    """

    supplement_clauses = _action_clauses(answer)
    replacement_sides = {item.side for item in supplement_clauses}
    if not replacement_sides:
        return f"{original.strip()}，{answer.strip()}"
    if any(
        sum(item.side == side for item in supplement_clauses) != 1 for side in replacement_sides
    ):
        return f"{original.strip()}，{answer.strip()}"

    original_clauses = _action_clauses(original)
    selected: dict[str, _ActionClause] = {item.side: item for item in supplement_clauses}
    for side in ("entry", "exit"):
        if side in selected:
            continue
        matches = [item for item in original_clauses if item.side == side]
        if len(matches) != 1:
            return f"{original.strip()}，{answer.strip()}"
        selected[side] = matches[0]

    parts: list[str] = []
    prefix = _leading_context_prefix(original)
    normalized_clauses = [
        _normalize_supplement_clause(selected[side].text)
        if side in replacement_sides
        else selected[side].text
        for side in ("entry", "exit")
        if side in selected
    ]
    if prefix and not any(prefix in item for item in normalized_clauses):
        parts.append(prefix)
    parts.extend(normalized_clauses)
    period = _trailing_backtest_period(original)
    if period and period not in parts:
        parts.append(period)
    return "，".join(item.strip("，,。；; ") for item in parts if item.strip())


def _action_clauses(original: str) -> tuple[_ActionClause, ...]:
    actions = tuple(_ACTION_RE.finditer(original))
    clauses: list[_ActionClause] = []
    cursor = 0
    for action in actions:
        raw_start = cursor
        raw = original[raw_start : action.end()]
        stripped = raw.strip("，,。；; \n\t")
        if stripped:
            start = original.find(stripped, raw_start, action.end())
            side = "entry" if _ENTRY_ACTION_RE.fullmatch(action.group()) else "exit"
            clauses.append(
                _ActionClause(
                    side=side,
                    start=start,
                    end=start + len(stripped),
                    text=stripped,
                )
            )
        cursor = action.end()
    return tuple(clauses)


def _unique_action_clause(original: str, *, side: str) -> _ActionClause | None:
    matches = [item for item in _action_clauses(original) if item.side == side]
    return matches[0] if len(matches) == 1 else None


def _normalize_supplement_clause(value: str) -> str:
    return re.sub(r"^(?:那就|再|补充|改成|更正为|改为)\s*", "", value).strip()


def _leading_context_prefix(original: str) -> str | None:
    marker = re.search(
        r"(?:MACD|RSI|KDJ|CCI|OBV|BBI|EMA|均线|布林|股价|价格|收盘价|"
        r"上涨|下跌|涨幅|跌幅|突破|跌破|高于|低于|金叉|死叉)",
        original,
        re.IGNORECASE,
    )
    if marker is None:
        return None
    prefix = original[: marker.start()].strip("，,。；; ")
    if not prefix or _ACTION_RE.search(prefix):
        return None
    return prefix


def _trailing_backtest_period(original: str) -> str | None:
    actions = tuple(_ACTION_RE.finditer(original))
    if not actions:
        return None
    suffix = original[actions[-1].end() :].strip("，,。；; ")
    if not suffix or re.search(r"(?:回测|近\s*\d+\s*年|从\s*\d{4})", suffix) is None:
        return None
    return suffix


def _preserved_exit_rule(original: str) -> str:
    """Keep one explicit exit, independent of whether it appeared before entry."""

    clause = _unique_action_clause(original, side="exit")
    return clause.text if clause is not None else original


def _preserved_entry_rule(original: str) -> str:
    """Keep one explicit entry, dropping an unrecognized pseudo-exit if present."""

    clause = _unique_action_clause(original, side="entry")
    return clause.text if clause is not None else original


def _complete_rule_choices(original: str) -> tuple[_Choice, ...]:
    folded = original.casefold()
    if "rsi" in folded:
        return (
            _Choice(
                "超跌反转",
                "RSI 低于 30 进、高于 70 出",
                "RSI低于30买入，高于70卖出",
                "RSI 低于 30",
                "RSI 高于 70",
                ("technical.rsi",),
            ),
            _Choice(
                "中轴转强",
                "RSI 上穿 50 进、下穿 50 出",
                "RSI上穿50买入，下穿50卖出",
                "RSI 上穿 50",
                "RSI 下穿 50",
                ("technical.rsi",),
            ),
            _Choice(
                "深度超跌",
                "RSI 低于 20 进，持有 5 个交易日",
                "RSI低于20买入，持有5个交易日卖出",
                "RSI 低于 20",
                "持有 5 个交易日",
                ("technical.rsi", "strategy.holding_period"),
            ),
        )
    if "obv" in folded or "能量潮" in original:
        return (
            _Choice(
                "OBV 上升",
                "量能累积向上时进，转弱时出",
                "OBV上升买入，下降卖出",
                "OBV 上升",
                "OBV 下降",
                ("technical.obv",),
            ),
            _Choice(
                "OBV 下降",
                "量能累积向下时进，反向时出",
                "OBV下降买入，上升卖出",
                "OBV 下降",
                "OBV 上升",
                ("technical.obv",),
            ),
        )
    return _generic_choices()


def _generic_choices() -> tuple[_Choice, ...]:
    return (
        _Choice(
            "均线趋势",
            "上穿 20 日线进、跌破时出",
            "股价上穿20日均线买入，跌破20日均线卖出",
            "股价上穿 20 日均线",
            "股价跌破 20 日均线",
            ("technical.ma",),
        ),
        _Choice(
            "超跌反转",
            "RSI 低于 30 进、高于 70 出",
            "RSI低于30买入，高于70卖出",
            "RSI 低于 30",
            "RSI 高于 70",
            ("technical.rsi",),
        ),
        _Choice(
            "动量转强",
            "MACD 金叉进、死叉出",
            "MACD金叉买入，死叉卖出",
            "MACD 金叉",
            "MACD 死叉",
            ("technical.macd",),
        ),
    )
