"""Check which Choice indicator names match a security and function type."""

from __future__ import annotations

import argparse
import json
from typing import Any

from EmQuantAPI import c

DEFAULT_INDICATORS = (
    "OPEN",
    "HIGH",
    "LOW",
    "CLOSE",
    "VOLUME",
    "AMOUNT",
    "ADJFACTOR",
    "ADJFACTORQ",
    "LIMITUP",
    "LIMITDOWN",
    "UPPERLIMIT",
    "LOWERLIMIT",
    "UPLIMITPRICE",
    "DOWNLIMITPRICE",
    "HIGHLIMIT",
    "LOWLIMIT",
    "ISSUSPEND",
    "ISSUSPENDED",
    "TRADESTATUS",
    "TRADESTATE",
    "ISST",
    "STFLAG",
)


def _quiet_log(_: bytes) -> int:
    return 1


def _serialize(result: Any) -> dict[str, Any]:
    return {
        "error_code": getattr(result, "ErrorCode", None),
        "error_message": getattr(result, "ErrorMsg", None),
        "columns": getattr(result, "Indicators", []),
        "rows": getattr(result, "Data", {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Choice 指标简称校验")
    parser.add_argument("--code", default="300059.SZ")
    parser.add_argument("--indicators", default=",".join(DEFAULT_INDICATORS))
    args = parser.parse_args()

    login = c.start("ForceLogin=0,RecordLoginInfo=0", _quiet_log)
    if login.ErrorCode != 0:
        print(f"登录失败：{login.ErrorCode} {login.ErrorMsg}")
        return 1

    try:
        checks = {
            function_type: _serialize(c.cfc(args.code, args.indicators, f"FunType={function_type}"))
            for function_type in ("csd", "css")
        }
        print(json.dumps(checks, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        c.stop()


if __name__ == "__main__":
    raise SystemExit(main())
