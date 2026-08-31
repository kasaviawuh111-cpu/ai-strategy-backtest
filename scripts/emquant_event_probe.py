"""Probe Choice regular-report event metadata without downloading article bodies."""

from __future__ import annotations

import argparse
import json
from typing import Any

from EmQuantAPI import c, eCfnMode_EndCount


def _quiet_log(_: bytes) -> int:
    return 1


def _serialize(result: Any) -> dict[str, Any]:
    return {
        "error_code": getattr(result, "ErrorCode", None),
        "error_message": getattr(result, "ErrorMsg", None),
        "codes": getattr(result, "Codes", []),
        "indicators": getattr(result, "Indicators", []),
        "dates": getattr(result, "Dates", []),
        "data": getattr(result, "Data", {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Choice 定期报告事件元数据探针")
    parser.add_argument("--code", default="300059.SZ")
    parser.add_argument("--end-time", default="20240501000000")
    parser.add_argument("--count", type=int, default=20)
    args = parser.parse_args()

    login = c.start("ForceLogin=0,RecordLoginInfo=0", _quiet_log)
    if login.ErrorCode != 0:
        print(f"登录失败：{login.ErrorCode} {login.ErrorMsg}")
        return 1

    try:
        result = c.cfn(
            args.code,
            "regularreport",
            eCfnMode_EndCount,
            f"endtime={args.end_time},count={args.count},Ispandas=0",
        )
        print(json.dumps(_serialize(result), ensure_ascii=False, indent=2, default=str))
        return 0 if result.ErrorCode == 0 else 1
    finally:
        c.stop()


if __name__ == "__main__":
    raise SystemExit(main())
