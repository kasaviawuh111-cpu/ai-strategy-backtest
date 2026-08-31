import threading
import time
from typing import Optional

import numpy as np
import pandas as pd

from astock_backtest.config import settings

TRADING_DAYS_PER_YEAR = 252
STALE_DAYS = 30  # ohlcv 最末日期早于 (全局 date_end - STALE_DAYS) 的视为停牌/退市


class DataStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._loaded = False
        self._load_time_ms: int = 0
        self._loaded_at: Optional[str] = None

        self.stock_list: pd.DataFrame = pd.DataFrame()
        self.ohlcv: pd.DataFrame = pd.DataFrame()
        self.index_daily: pd.DataFrame = pd.DataFrame()
        self.valuation: pd.DataFrame = pd.DataFrame()
        self.financial: pd.DataFrame = pd.DataFrame()
        self.dividend: pd.DataFrame = pd.DataFrame()

        self.ohlcv_by_code: dict[str, pd.DataFrame] = {}
        self.valuation_by_code: dict[str, pd.DataFrame] = {}
        self.financial_by_code: dict[str, pd.DataFrame] = {}
        self.dividend_by_code: dict[str, pd.DataFrame] = {}
        self.code_name_map: dict[str, str] = {}

        self.latest_factors: pd.DataFrame = pd.DataFrame()

        self.date_range_start: Optional[str] = None
        self.date_range_end: Optional[str] = None

        self.load()

    def load(self) -> None:
        with self._lock:
            t0 = time.perf_counter()
            data_dir = settings.DATA_DIR

            stock_list = pd.read_parquet(data_dir / "stock_list.parquet")
            ohlcv = pd.read_parquet(data_dir / "daily_ohlcv.parquet")
            index_daily = pd.read_parquet(data_dir / "index_daily.parquet")
            valuation = pd.read_parquet(data_dir / "valuation_daily.parquet")
            financial = pd.read_parquet(data_dir / "financial_quarterly.parquet")
            dividend = pd.read_parquet(data_dir / "dividend_history.parquet")

            ohlcv = ohlcv.sort_values(["stock_code", "date"]).reset_index(drop=True)
            # date_int 预计算 (yyyymmdd int32): Phase B engine_cache 跟 signal-scanner
            # signal_triggers.trigger_date 对齐用. 不影响 Phase A 任何逻辑.
            ohlcv["date_int"] = (
                ohlcv["date"].dt.year * 10000
                + ohlcv["date"].dt.month * 100
                + ohlcv["date"].dt.day
            ).astype("int32")
            valuation = valuation.sort_values(["stock_code", "date"]).reset_index(drop=True)
            financial = financial.sort_values(
                ["stock_code", "publish_deadline"]
            ).reset_index(drop=True)
            dividend = dividend.sort_values(
                ["stock_code", "ex_dividend_date"]
            ).reset_index(drop=True)
            index_daily = index_daily.sort_values("date").reset_index(drop=True)

            ohlcv_by_code = {
                code: group.reset_index(drop=True)
                for code, group in ohlcv.groupby("stock_code", sort=False)
            }
            valuation_by_code = {
                code: group.reset_index(drop=True)
                for code, group in valuation.groupby("stock_code", sort=False)
            }
            financial_by_code = {
                code: group.reset_index(drop=True)
                for code, group in financial.groupby("stock_code", sort=False)
            }
            dividend_by_code = {
                code: group.reset_index(drop=True)
                for code, group in dividend.groupby("stock_code", sort=False)
            }
            code_name_map = dict(
                zip(stock_list["stock_code"], stock_list["stock_name"])
            )

            date_start = ohlcv["date"].min()
            date_end = ohlcv["date"].max()

            self.stock_list = stock_list
            self.ohlcv = ohlcv
            self.index_daily = index_daily
            self.valuation = valuation
            self.financial = financial
            self.dividend = dividend
            self.ohlcv_by_code = ohlcv_by_code
            self.valuation_by_code = valuation_by_code
            self.financial_by_code = financial_by_code
            self.dividend_by_code = dividend_by_code
            self.code_name_map = code_name_map
            self.date_range_start = date_start.strftime("%Y-%m-%d") if pd.notna(date_start) else None
            self.date_range_end = date_end.strftime("%Y-%m-%d") if pd.notna(date_end) else None

            self.latest_factors = self._build_latest_factors(date_end)

            self._load_time_ms = int((time.perf_counter() - t0) * 1000)
            self._loaded_at = time.strftime("%Y-%m-%d %H:%M:%S")
            self._loaded = True

    def _build_latest_factors(self, global_date_end: pd.Timestamp) -> pd.DataFrame:
        """为每只股票预计算最新一期因子, 筛选时 O(n) 扫描一次即可."""
        stale_cutoff = global_date_end - pd.Timedelta(days=STALE_DAYS)
        rows = []
        for _, meta in self.stock_list.iterrows():
            code = meta["stock_code"]
            row = {
                "stock_code": code,
                "stock_name": meta["stock_name"],
                "market": meta["market"],
                "industry": meta.get("industry", "其他") if "industry" in meta else "其他",
                "industry_code": meta.get("industry_code") if "industry_code" in meta else None,
                "pe_ttm": np.nan,
                "pb": np.nan,
                "ps_ttm": np.nan,
                "total_mv": np.nan,
                "roe": np.nan,
                "revenue_growth_yoy": np.nan,
                "profit_growth_yoy": np.nan,
                "dividend_yield": np.nan,
                "momentum_60": np.nan,
                "volatility_60": np.nan,
                "turnover_avg_20": np.nan,
                "latest_close": np.nan,
                "latest_date": None,
                "is_active": False,
            }

            ohlcv = self.ohlcv_by_code.get(code)
            if ohlcv is not None and not ohlcv.empty:
                last = ohlcv.iloc[-1]
                row["latest_close"] = float(last["close"])
                row["latest_date"] = last["date"].strftime("%Y-%m-%d") if pd.notna(last["date"]) else None
                row["is_active"] = bool(last["date"] >= stale_cutoff)

                closes = ohlcv["close"].astype(float)
                if len(closes) >= 61:
                    c_now = float(closes.iloc[-1])
                    c_then = float(closes.iloc[-61])
                    if c_then > 0:
                        row["momentum_60"] = c_now / c_then - 1.0
                if len(closes) >= 61:
                    rets = closes.tail(61).pct_change().dropna()
                    if not rets.empty:
                        row["volatility_60"] = float(rets.std() * np.sqrt(TRADING_DAYS_PER_YEAR))
                if "turnover_rate" in ohlcv.columns and len(ohlcv) >= 20:
                    tr = ohlcv["turnover_rate"].tail(20).astype(float)
                    mean_tr = tr.mean()
                    if pd.notna(mean_tr):
                        row["turnover_avg_20"] = float(mean_tr)

            val = self.valuation_by_code.get(code)
            if val is not None and not val.empty:
                last_v = val.iloc[-1]
                pe = last_v.get("pe_ttm")
                pb = last_v.get("pb")
                ps = last_v.get("ps_ttm")
                mv = last_v.get("total_mv")
                if pd.notna(pe) and pe > 0:
                    row["pe_ttm"] = float(pe)
                if pd.notna(pb) and pb > 0:
                    row["pb"] = float(pb)
                if pd.notna(ps) and ps > 0:
                    row["ps_ttm"] = float(ps)
                if pd.notna(mv):
                    row["total_mv"] = float(mv)

            fin = self.financial_by_code.get(code)
            if fin is not None and not fin.empty:
                eligible = fin[fin["publish_deadline"] <= global_date_end]
                if not eligible.empty:
                    last_f = eligible.iloc[-1]
                    for k in ("roe", "revenue_growth_yoy", "profit_growth_yoy"):
                        v = last_f.get(k)
                        if pd.notna(v):
                            row[k] = float(v)

            div = self.dividend_by_code.get(code)
            if div is not None and not div.empty and pd.notna(row["latest_close"]) and row["latest_close"] > 0:
                cutoff = global_date_end - pd.Timedelta(days=365)
                recent = div[
                    (div["ex_dividend_date"] >= cutoff)
                    & (div["ex_dividend_date"] <= global_date_end)
                    & (div["progress"] == "实施")
                ]
                total = float(recent["dividend_per_share"].sum()) if not recent.empty else 0.0
                row["dividend_yield"] = total / row["latest_close"]

            rows.append(row)

        df = pd.DataFrame(rows)
        return df

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def load_time_ms(self) -> int:
        return self._load_time_ms

    @property
    def loaded_at(self) -> Optional[str]:
        return self._loaded_at

    @property
    def stock_count(self) -> int:
        return len(self.stock_list)

    def get_ohlcv(self, stock_code: str) -> pd.DataFrame:
        df = self.ohlcv_by_code.get(stock_code)
        return df.copy() if df is not None else pd.DataFrame()

    def get_valuation(self, stock_code: str) -> pd.DataFrame:
        df = self.valuation_by_code.get(stock_code)
        return df.copy() if df is not None else pd.DataFrame()

    def get_financial(self, stock_code: str) -> pd.DataFrame:
        df = self.financial_by_code.get(stock_code)
        return df.copy() if df is not None else pd.DataFrame()

    def get_dividend(self, stock_code: str) -> pd.DataFrame:
        df = self.dividend_by_code.get(stock_code)
        return df.copy() if df is not None else pd.DataFrame()


store: Optional[DataStore] = None


def get_store() -> DataStore:
    global store
    if store is None:
        store = DataStore()
    return store
