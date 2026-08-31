"""Run a minimal, read-only Choice Quant API capability probe."""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from typing import Any

from EmQuantAPI import c

INDICATORS = (
    "OPEN",
    "HIGH",
    "LOW",
    "CLOSE",
    "VOLUME",
    "AMOUNT",
    "HIGHLIMIT",
    "LOWLIMIT",
    "TRADESTATUS",
)


def _quiet_log(_: bytes) -> int:
    return 1


def _check(result: Any, operation: str) -> Any:
    if not isinstance(result, c.EmQuantData):
        raise RuntimeError(f"{operation} 返回了异常类型：{type(result).__name__}")
    if result.ErrorCode != 0:
        raise RuntimeError(f"{operation} 失败：{result.ErrorCode} {result.ErrorMsg}")
    return result


def _series_rows(result: Any, code: str) -> list[dict[str, Any]]:
    indicator_series = result.Data.get(code)
    if indicator_series is None:
        raise RuntimeError(f"日线结果缺少证券代码 {code}")
    if len(indicator_series) != len(result.Indicators):
        raise RuntimeError("日线指标数量与返回数据不一致")

    rows: list[dict[str, Any]] = []
    for date_index, trade_date in enumerate(result.Dates):
        row: dict[str, Any] = {"date": trade_date}
        for indicator_index, indicator in enumerate(result.Indicators):
            values = indicator_series[indicator_index]
            if date_index >= len(values):
                raise RuntimeError(f"{indicator} 的序列长度不足")
            row[indicator.lower()] = values[date_index]
        rows.append(row)
    return rows


def _parse_args() -> argparse.Namespace:
    today = date.today()
    parser = argparse.ArgumentParser(description="Choice Quant API 最小只读探针")
    parser.add_argument("--code", default="300059.SZ")
    parser.add_argument("--start", default=(today - timedelta(days=30)).isoformat())
    parser.add_argument("--end", default=today.isoformat())
    parser.add_argument("--adjust-flag", choices=(1, 2, 3), default=1, type=int)
    return parser.parse_args()


def _audit_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    high_limit_rows = [row for row in rows if row.get("highlimit") == "是"]
    low_limit_rows = [row for row in rows if row.get("lowlimit") == "是"]
    abnormal_status_rows = [row for row in rows if row.get("tradestatus") not in {None, "正常交易"}]

    def is_one_price(row: dict[str, Any]) -> bool:
        prices = [row.get(field) for field in ("open", "high", "low", "close")]
        return all(price is not None for price in prices) and len(set(prices)) == 1

    one_price_high_limit = [row["date"] for row in high_limit_rows if is_one_price(row)]
    one_price_low_limit = [row["date"] for row in low_limit_rows if is_one_price(row)]
    missing_value_rows = [
        row["date"] for row in rows if any(value is None for value in row.values())
    ]
    return {
        "high_limit_days": [row["date"] for row in high_limit_rows],
        "low_limit_days": [row["date"] for row in low_limit_rows],
        "one_price_high_limit_days": one_price_high_limit,
        "one_price_low_limit_days": one_price_low_limit,
        "abnormal_trade_status": [
            {"date": row["date"], "status": row.get("tradestatus")} for row in abnormal_status_rows
        ],
        "rows_with_missing_values": missing_value_rows,
    }


def main() -> int:
    args = _parse_args()
    login = c.start("ForceLogin=0,RecordLoginInfo=0", _quiet_log)
    if login.ErrorCode != 0:
        print(f"登录失败：{login.ErrorCode} {login.ErrorMsg}")
        if login.ErrorCode in {10001014, 10001020}:
            print("请先运行：.venv/bin/python scripts/emquant_activate_sms.py")
        return 1

    try:
        calendar = _check(
            c.tradedates(args.start, args.end, "Market=CNSESH"),
            "交易日历",
        )
        series = _check(
            c.csd(
                args.code,
                ",".join(INDICATORS),
                args.start,
                args.end,
                f"Period=1,AdjustFlag={args.adjust_flag},Order=1,Ispandas=0,RECVtimeout=30",
            ),
            "不复权日线",
        )
        rows = _series_rows(series, args.code)
        if not rows:
            raise RuntimeError("日线查询成功但没有返回记录")

        quota_start = (date.today() - timedelta(days=29)).isoformat()
        quota_result = c.datastatistics(
            "",
            "FUNCENAME,FUNCNAME,SECUTYPE,PERIOD,STARTDATE,ENDDATE,"
            "THRESHOLD,USEDDATA,USEDRATIO,AVAILABEDATA,PACKAGENAME,EFFECTIVEDATE",
            f"StartDate={quota_start},EndDate={date.today().isoformat()},Ispandas=0",
        )
        quota_rows = 0
        quota_warning = None
        if isinstance(quota_result, c.EmQuantData) and quota_result.ErrorCode == 0:
            quota_rows = len(quota_result.Data)
        else:
            error_code = getattr(quota_result, "ErrorCode", "unknown")
            error_message = getattr(quota_result, "ErrorMsg", "unexpected response")
            quota_warning = f"流量查询未返回统计记录：{error_code} {error_message}"

        summary = {
            "status": "ok",
            "code": args.code,
            "range": {"start": args.start, "end": args.end},
            "adjust_flag": args.adjust_flag,
            "trading_days": len(calendar.Data),
            "daily_rows": len(rows),
            "market_days_without_daily_bar": max(len(calendar.Data) - len(rows), 0),
            "first_row": rows[0],
            "last_row": rows[-1],
            "audit": _audit_rows(rows),
            "quota_rows": quota_rows,
            "quota_warning": quota_warning,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        return 0
    except RuntimeError as error:
        print(error)
        return 1
    finally:
        c.stop()


if __name__ == "__main__":
    raise SystemExit(main())
