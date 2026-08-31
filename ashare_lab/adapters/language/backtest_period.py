"""Fail-closed parsing for a natural-language backtest period.

The parser intentionally supports a small, deterministic grammar.  A date-like
request that does not fit that grammar is rejected instead of being replaced by
the compiler's default lookback window.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

_DATE_TOKEN = (
    r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{4}/\d{1,2}/\d{1,2}|"
    r"\d{4}\.\d{1,2}\.\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日?)"
)
_DATE_TOKEN_RE = re.compile(rf"(?<!\d){_DATE_TOKEN}(?!\d)")
_DATE_RANGE_RE = re.compile(rf"(?P<start>{_DATE_TOKEN})(?:至|到|~|～|—|–)(?P<end>{_DATE_TOKEN})")
_CHINESE_YEAR_COUNTS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_RELATIVE_YEAR_COUNT = r"(?:\d{1,3}|[零〇一二三四五六七八九十])"
_RELATIVE_YEARS_RE = re.compile(
    rf"(?:回测(?:近|最近|过去)?|(?:近|最近|过去))"
    rf"(?P<years>{_RELATIVE_YEAR_COUNT})年(?:内)?(?!至|到|~|～|—|–|前|后|至今)"
)
_UNSUPPORTED_TIME_HINT_RE = re.compile(
    r"(?:回测|回测区间|回测时间)[^，。；;]{0,24}"
    r"(?:[零〇一二两三四五六七八九十百]+年|\d+(?:年|个月|月|天|日)|"
    r"去年|今年|明年|年初|年末|至今|截至|开始|结束)"
)


@dataclass(frozen=True, slots=True)
class BacktestPeriodIntent:
    """A fully parsed period, relative lookback, or a fail-closed diagnostic."""

    start: date | None = None
    end: date | None = None
    lookback_years: int | None = None
    diagnostic_code: str | None = None


def parse_backtest_period(utterance: str) -> BacktestPeriodIntent:
    """Parse the supported period grammar without guessing missing fields."""

    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", utterance)).strip()
    date_tokens = tuple(_DATE_TOKEN_RE.finditer(text))
    range_matches = tuple(_DATE_RANGE_RE.finditer(text))
    relative_matches = tuple(_RELATIVE_YEARS_RE.finditer(text))

    if date_tokens:
        if relative_matches:
            return BacktestPeriodIntent(diagnostic_code="backtest_date_range_ambiguous")
        if len(date_tokens) == 1:
            return BacktestPeriodIntent(diagnostic_code="backtest_date_range_incomplete")
        if len(date_tokens) != 2 or len(range_matches) != 1:
            return BacktestPeriodIntent(diagnostic_code="backtest_date_range_ambiguous")
        range_match = range_matches[0]
        if (
            range_match.start("start") != date_tokens[0].start()
            or range_match.end("start") != date_tokens[0].end()
            or range_match.start("end") != date_tokens[1].start()
            or range_match.end("end") != date_tokens[1].end()
        ):
            return BacktestPeriodIntent(diagnostic_code="backtest_date_range_ambiguous")
        try:
            start = _parse_date(range_match.group("start"))
            end = _parse_date(range_match.group("end"))
        except ValueError:
            return BacktestPeriodIntent(diagnostic_code="backtest_date_invalid")
        if start > end:
            return BacktestPeriodIntent(diagnostic_code="backtest_date_range_reversed")
        return BacktestPeriodIntent(start=start, end=end)

    if len(relative_matches) > 1:
        return BacktestPeriodIntent(diagnostic_code="backtest_date_range_ambiguous")
    if relative_matches:
        years = _parse_year_count(relative_matches[0].group("years"))
        if not 1 <= years <= 100:
            return BacktestPeriodIntent(diagnostic_code="backtest_lookback_invalid")
        return BacktestPeriodIntent(lookback_years=years)

    if "回测" in text and re.search(r"(?<!\d)\d{4}(?:[-/.]\d{1,2})?(?!\d)", text) is not None:
        return BacktestPeriodIntent(diagnostic_code="backtest_date_range_unsupported")
    if _UNSUPPORTED_TIME_HINT_RE.search(text) is not None:
        return BacktestPeriodIntent(diagnostic_code="backtest_date_range_unsupported")
    return BacktestPeriodIntent()


def _parse_year_count(value: str) -> int:
    if value.isascii():
        return int(value)
    return _CHINESE_YEAR_COUNTS[value]


def _parse_date(value: str) -> date:
    if "年" in value:
        match = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日?", value)
        if match is None:  # pragma: no cover - protected by _DATE_TOKEN
            raise ValueError("unsupported date")
        year, month, day = (int(part) for part in match.groups())
    else:
        separator = "-" if "-" in value else "/" if "/" in value else "."
        year, month, day = (int(part) for part in value.split(separator))
    return date(year, month, day)
