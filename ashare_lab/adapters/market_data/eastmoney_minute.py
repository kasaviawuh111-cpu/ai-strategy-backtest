"""Public Eastmoney minute acquisition, not an intraday fill simulator.

Request/field mapping: AKShare stock_zh_a_hist_min_em (MIT), pinned to
8e95744b79ae22326308ccd2b4e62650c5b53c55. Reuses our public-daily HTTP channel.
One-minute trends are a recent window (ndays=5), not arbitrary history.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal

import httpx

from ashare_lab.adapters.host_http import HostThrottledHttpClient
from ashare_lab.adapters.market_data.eastmoney_daily import (
    _HEADERS,
    AKSHARE_REQUEST_TOKEN,
    AKSHARE_SOURCE_COMMIT,
    EASTMONEY_DAILY_URL,
    SHANGHAI,
    _identity,
    _retryable_http_error,
)
from ashare_lab.domain.shared import InstrumentId

MinutePeriod = Literal[1, 5, 15, 30, 60]
TRENDS_URL = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
# 同协议镜像节点：主节点链路被重置时的最后一次尝试才启用，实际所用地址会写入采集清单。
DELAY_MIRROR_HOST = "https://push2delay.eastmoney.com"


def _delay_mirror(url: str) -> str:
    return DELAY_MIRROR_HOST + httpx.URL(url).path


class EastmoneyMinuteSourceError(RuntimeError):
    def __init__(self, code: str, message: str, *, attempts: int = 1) -> None:
        super().__init__(message)
        self.code, self.attempts = code, attempts


@dataclass(frozen=True)
class EastmoneyMinuteRow:
    timestamp: datetime
    open: Decimal
    close: Decimal
    high: Decimal
    low: Decimal
    volume_lots: Decimal
    volume_shares: Decimal
    amount_cny: Decimal
    source_row: str


@dataclass(frozen=True)
class EastmoneyMinuteCollection:
    instrument_id: str
    period_minutes: int
    requested_start: date
    requested_end: date
    retrieved_at: datetime
    request_url: str
    request_params: dict[str, str]
    response_sha256: str
    attempts: int
    rows: tuple[EastmoneyMinuteRow, ...]

    def manifest(self) -> dict[str, object]:
        result = asdict(self)
        warnings = [
            "公开接口仅返回其保留窗口；指定起止日期不保证该区间完整。",
            "1分钟接口请求最近5个交易日；不提供任意历史1分钟回溯。",
            "分钟标签、集合竞价与缺失分钟仍需和交易日历核对，不能直接证明逐笔成交。",
            "盘中采集不含标签晚于采集时刻的形成中K线（该根未走完，已剔除）。",
            "本文件是取数结果，不自动替换已生效回测的数据源。",
        ]
        if self.request_url.startswith(DELAY_MIRROR_HOST):
            warnings.append("本次经 push2delay 镜像节点采集（主节点链路不可用时的末次回退），"
                            "时效性需以采集时刻与末根时间戳核对。")
        result.update({
            "schema_version": "eastmoney.public-minute-acquisition.v1",
            "provider": "eastmoney_push2his_public", "price_basis": "unadjusted",
            "volume_source_unit": "lot", "volume_lot_size_shares": 100,
            "timestamp_timezone": "Asia/Shanghai",
            "timestamp_semantics": "provider_label_not_promoted_to_execution_bar",
            "coverage": {"first": self.rows[0].timestamp, "last": self.rows[-1].timestamp,
                         "rows": len(self.rows), "complete_requested_range_verified": False},
            "source_mapping": f"akshare@{AKSHARE_SOURCE_COMMIT}:stock_zh_a_hist_min_em",
            "warnings": warnings,
        })
        return result


class EastmoneyMinuteResearchSource:
    def __init__(self, *, transport: httpx.BaseTransport | None = None,
                 timeout: float = 15, max_attempts: int = 3) -> None:
        if max_attempts < 1 or max_attempts > 3:
            raise ValueError("max_attempts must be 1..3")
        self.timeout, self.max_attempts = timeout, max_attempts
        self.http = HostThrottledHttpClient(transport=transport, timeout=timeout)

    def __enter__(self) -> EastmoneyMinuteResearchSource:
        return self

    def __exit__(self, *_args: object) -> None:
        self.http.close()

    def fetch(self, *, instrument_id: str, start: date, end: date,
              period: MinutePeriod = 1) -> EastmoneyMinuteCollection:
        if start > end or period not in {1, 5, 15, 30, 60}:
            raise ValueError("日期须有序，分钟周期须为1、5、15、30或60")
        canonical, secid, market, code = _identity(InstrumentId(instrument_id))
        params = {"secid": secid, "ut": AKSHARE_REQUEST_TOKEN,
                  "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"}
        if period == 1:
            url = TRENDS_URL
            params.update({"fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                           "ndays": "5", "iscr": "0"})
        else:
            url = EASTMONEY_DAILY_URL
            params.update({"fields2": ",".join(f"f{i}" for i in range(51, 62)),
                           "klt": str(period), "fqt": "0", "beg": "0", "end": "20500000"})
        for attempt in range(1, self.max_attempts + 1):
            # 镜像节点仅提供 trends2 当日 1 分钟；kline 类周期不做镜像回退。
            attempt_url = (url if attempt < self.max_attempts or period != 1
                           else _delay_mirror(url)) if self.max_attempts > 1 else url
            try:
                response = self.http.get(attempt_url, params=params, headers=_HEADERS,
                                         min_interval=float(attempt), timeout=self.timeout)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                if attempt < self.max_attempts and _retryable_http_error(exc):
                    continue
                status = (exc.response.status_code
                          if isinstance(exc, httpx.HTTPStatusError) else None)
                failure = "rate_limited" if status == 429 else (
                    "access_denied" if status in {401, 403} else "network_error"
                )
                raise EastmoneyMinuteSourceError(
                    failure, f"分钟行情请求未完成：{type(exc).__name__}", attempts=attempt,
                ) from exc
            try:
                payload = response.json()
            except ValueError as exc:
                raise EastmoneyMinuteSourceError("response_format", "分钟行情未返回JSON") from exc
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict) or payload.get("rc") != 0:
                raise EastmoneyMinuteSourceError("no_data", "分钟行情接口没有返回有效数据")
            if data.get("code") != code or data.get("market") != market:
                if attempt < self.max_attempts:
                    continue
                raise EastmoneyMinuteSourceError("instrument_mismatch", "重取后股票身份仍不一致",
                                                attempts=attempt)
            received = datetime.now(UTC)
            rows = decode_minute_rows(data.get("trends" if period == 1 else "klines"),
                                      received_at=received, period_minutes=period)
            selected = tuple(row for row in rows if start <= row.timestamp.date() <= end)
            if not selected:
                available = f"{rows[0].timestamp.date()} 至 {rows[-1].timestamp.date()}"
                raise EastmoneyMinuteSourceError("range_unavailable",
                                                f"请求区间不在本次返回窗口内；可见窗口：{available}")
            return EastmoneyMinuteCollection(
                str(canonical), period, start, end, received, attempt_url, params,
                "sha256:" + hashlib.sha256(response.content).hexdigest(), attempt, selected,
            )
        raise AssertionError("bounded acquisition loop exhausted")


def decode_minute_rows(raw: object, *, received_at: datetime,
                       period_minutes: int = 1) -> tuple[EastmoneyMinuteRow, ...]:
    if not isinstance(raw, list) or not raw:
        raise EastmoneyMinuteSourceError("no_data", "本次没有返回分钟行情")
    # 时间标签语义（2026-09-09 收盘后完整交易日校准，241 根）：
    # 标签为该根 K 线的结束时刻（bar_end）；首行 09:30 是开盘集合竞价
    # （09:25–09:30）的独立结果柱，午后首行为 13:01。盘中响应的最后一根
    # 通常是尚未走完的形成中 K 线（其标签结束时刻晚于采集时刻），剔除而
    # 不参与研究数据；超出单周期容差的未来标签仍是异常。
    forming_tolerance = timedelta(minutes=period_minutes)
    parsed: dict[datetime, EastmoneyMinuteRow] = {}
    for line in raw:
        try:
            fields = line.split(",")
            if len(fields) not in {8, 11}:
                raise ValueError("incorrect field count")
            stamp = datetime.strptime(fields[0], "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI)
            op, close, high, low, volume, amount = map(Decimal, fields[1:7])
            if not all(v.is_finite() for v in (op, close, high, low, volume, amount)):
                raise ValueError("non-finite value")
            if min(op, close, high, low) <= 0 or min(volume, amount) < 0:
                raise ValueError("invalid price/volume")
            if low > min(op, close) or high < max(op, close) or low > high:
                raise ValueError("inconsistent OHLC")
            if stamp > received_at:
                if stamp - received_at <= forming_tolerance:
                    continue
                raise ValueError("invalid timestamp")
            if stamp.weekday() >= 5 or not (
                time(9, 30) <= stamp.time() <= time(11, 30)
                or time(13) <= stamp.time() <= time(15)
            ):
                raise ValueError("invalid timestamp")
            row = EastmoneyMinuteRow(stamp, op, close, high, low, volume,
                                    volume * 100, amount, line)
            if stamp in parsed and parsed[stamp] != row:
                raise ValueError("conflicting duplicate timestamp")
            parsed[stamp] = row
        except (ValueError, InvalidOperation, AttributeError, TypeError) as exc:
            raise EastmoneyMinuteSourceError("invalid_market_data",
                                            "分钟数据的时间、价格或数量不一致") from exc
    return tuple(parsed[key] for key in sorted(parsed))


def calibrate_session_timestamp_semantics(
    rows: tuple[EastmoneyMinuteRow, ...], *, session: date, period_minutes: int,
) -> dict[str, object]:
    """Identify a complete normal-session label convention without guessing.

    The public trends and kline endpoints must each prove their own convention.
    A partial intraday response is deliberately ``unverified`` rather than an
    approximation of bar_start or bar_end.
    """
    if period_minutes not in {1, 5, 15, 30, 60}:
        raise ValueError("分钟周期须为1、5、15、30或60")
    observed = tuple(row.timestamp for row in rows if row.timestamp.date() == session)
    if len(observed) != len(set(observed)):
        return {"status": "unverified", "reason": "duplicate_timestamps"}
    for semantics in ("bar_start", "bar_end"):
        expected = _normal_session_grid(session, period_minutes=period_minutes, semantics=semantics)
        if observed == expected:
            return {
                "status": "verified",
                "semantics": semantics,
                "session": session.isoformat(),
                "intervalMinutes": period_minutes,
                "rowCount": len(observed),
                "firstTimestamp": observed[0].isoformat(),
                "lastTimestamp": observed[-1].isoformat(),
            }
    return {
        "status": "unverified",
        "reason": "not_a_complete_normal_session_grid",
        "session": session.isoformat(),
        "intervalMinutes": period_minutes,
        "rowCount": len(observed),
        "firstTimestamp": observed[0].isoformat() if observed else None,
        "lastTimestamp": observed[-1].isoformat() if observed else None,
    }


def _normal_session_grid(
    session: date, *, period_minutes: int, semantics: Literal["bar_start", "bar_end"],
) -> tuple[datetime, ...]:
    """Return the 09:30–11:30 / 13:00–15:00 continuous-auction grid."""
    starts = ((time(9, 30), time(11, 30)), (time(13), time(15)))
    labels: list[datetime] = []
    for start_time, end_time in starts:
        current = datetime.combine(session, start_time, SHANGHAI)
        end = datetime.combine(session, end_time, SHANGHAI)
        while current < end:
            labels.append(
                current if semantics == "bar_start" else current + timedelta(minutes=period_minutes)
            )
            current += timedelta(minutes=period_minutes)
    return tuple(labels)
