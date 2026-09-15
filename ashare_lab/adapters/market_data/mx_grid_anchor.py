"""Pin a current MX quote as an explicitly current-anchored research parameter."""
from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
import re
from zoneinfo import ZoneInfo

from ashare_lab.domain.market_data import normalize_a_share_instrument
from ashare_lab.domain.strategy.price_plans import GridParameters
from ashare_lab.ports.live_market_data import LiveFinanceDataResult, LiveMarketDataResult


class MissingLatestGridQuoteError(ValueError):
    """A readable provider response did not supply one identified current quote."""


_SCREEN_LATEST = re.compile(
    r"最新价[（(]元[）)]\s*(\d{4})[.\-/](\d{2})[.\-/](\d{2})(?:\s+\d{2}:\d{2}(?::\d{2})?)?"
)


def _screen_quotes(result: LiveMarketDataResult, symbol: str) -> set[tuple[Decimal, str]]:
    quotes: set[tuple[Decimal, str]] = set()
    for row in result.rows:
        code = next((row[key] for key in ("证券代码", "股票代码", "代码") if key in row), None)
        try:
            if normalize_a_share_instrument(str(code)).value != symbol:
                continue
        except ValueError:
            continue
        for column in result.columns:
            match = _SCREEN_LATEST.fullmatch(column)
            if match is None:
                continue
            try:
                quote_date = date(*(int(value) for value in match.groups()))
                price = Decimal(str(row.get(column)))
            except (ValueError, InvalidOperation):
                continue
            if quote_date > result.provenance.retrieved_at.astimezone(ZoneInfo("Asia/Shanghai")).date():
                continue
            if price.is_finite() and 0 < price <= 1_000_000 and price % Decimal("0.01") == 0:
                quotes.add((price, column[match.start(1):]))
    return quotes


def bind_latest_grid_anchor(params: GridParameters, symbol: str,
                            result: LiveFinanceDataResult | LiveMarketDataResult) -> GridParameters:
    quotes = _screen_quotes(result, symbol) if isinstance(result, LiveMarketDataResult) else set()
    for table in result.tables if isinstance(result, LiveFinanceDataResult) else ():
        # A current price must not come from another stock, a historical close,
        # or the support/resistance columns returned alongside the quote.
        if table.get("code") != symbol:
            continue
        raw, names = table.get("rawTable"), table.get("nameMap")
        if not isinstance(raw, Mapping) or not isinstance(names, Mapping):
            continue
        if names.get("ZXJ_f2_3") != "最新价":
            continue
        values, times = raw.get("ZXJ_f2_3"), raw.get("headName")
        if not isinstance(values, list) or len(values) != 1:
            continue
        if not isinstance(times, list) or len(times) != 1 or not isinstance(times[0], str):
            continue
        try:
            price = Decimal(str(values[0]))
        except InvalidOperation:
            continue
        if price.is_finite() and 0 < price <= 1_000_000 and price % Decimal("0.01") == 0:
            quotes.add((price, times[0]))
    if len(quotes) != 1:
        raise MissingLatestGridQuoteError("未取得该股票唯一的行情最新价")
    price, time_label = quotes.pop()
    # Explicit user bounds and yuan spacing are never scaled to another price.
    return GridParameters.model_validate({**params.model_dump(),
        "anchor_price": price,
        "anchor_quote_source": result.provider,
        "anchor_quote_retrieved_at": result.provenance.retrieved_at.isoformat(),
        "anchor_quote_time_label": time_label,
        "anchor_quote_response_sha256": result.provenance.response_sha256,
    })
