"""Resolve a historical grid anchor without falling back to a current quote."""
from datetime import date
from decimal import Decimal

from ashare_lab.domain.strategy.price_plans import GridParameters, GridSpecificationError


def bind_previous_close(params: GridParameters, *, start: date, end: date,
                        symbol: str, history, sessions) -> GridParameters:
    if params.anchor_mode != "previous_close":
        return params
    days = sorted(set(sessions))
    first = next((day for day in days if start <= day <= end), None)
    previous = next((day for day in reversed(days) if first and day < first), None)
    if first is None or previous is None or history.instrument_id != symbol:
        raise GridSpecificationError("起始日昨收价缺少匹配的交易日历或证券数据")
    rows = [row for row in history.rows if row.session_date == previous]
    if len(rows) != 1 or not Decimal(rows[0].raw_close).is_finite() or rows[0].raw_close <= 0:
        raise GridSpecificationError(f"缺少{previous}的有效收盘价，不能用最新价或开盘价替代")
    return GridParameters.model_validate({**params.model_dump(),
        "anchor_price": rows[0].raw_close,
        "anchor_quote_source": "historical_daily_raw_close",
        "anchor_quote_time_label": previous.isoformat(),
        "anchor_quote_retrieved_at": None,
        "anchor_quote_response_sha256": None,
    })
