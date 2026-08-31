"""信号原子基础设施 — base class + registry + 枚举

每个原子是 SignalAtom 子类的 frozen 实例, 用 register() 装饰器或函数调用入库.
原子的 compute() 接收所需数据 (ohlcv / valuation / financial / index / dividend),
返回 np.int8 数组 (1=触发, 0=未触发), shape = len(ohlcv).

反向求解上下文决定原子是当买入信号用还是当卖出信号用, 原子本身不预设方向.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd


class SignalCategory(str, Enum):
    TECHNICAL = "technical"        # A 类: 技术指标 (MA / MACD / RSI / KDJ / 布林)
    PRICE_ACTION = "price_action"  # B 类: 价格行为 (新高新低 / 单日涨跌 / 连涨连跌 / K 线形态)
    VOLUME = "volume"              # C 类: 量能 (放量缩量 / 量价配合 / 换手率)
    FUNDAMENTAL = "fundamental"    # D 类: 估值/基本面 (PE/PB 历史分位 / ROE / 营收利润增长 / 股息)
    EVENT_TIME = "event_time"      # E 类: 事件/时间 (月初季末 / 节假日 / 涨停后 N 天)
    BENCHMARK = "benchmark"        # F 类: 跟基准对比 (跑赢沪深 300 / 相对强弱)


class DataDep(str, Enum):
    OHLCV = "ohlcv"
    VALUATION = "valuation"
    FINANCIAL = "financial"
    DIVIDEND = "dividend"
    INDEX = "index"


@dataclass(frozen=True)
class SignalAtom:
    """一个信号原子.

    属性:
      id: 后端代码用的稳定 id (例 "ma_5_cross_up_10")
      professional_name: 专业名 (例 "MA5 上穿 MA10")
      layman_name: 白话名 (例 "短期均价突破 10 日均价")
      category: 类别枚举 (用于智能裁剪)
      deps: 数据依赖 tuple (例 (DataDep.OHLCV,) / (DataDep.OHLCV, DataDep.VALUATION))
      min_warmup: 需要的历史天数, 引擎应在这之前强制返 0
      compute_fn: 实际计算函数, 签名 (ohlcv, **other_data) -> np.ndarray[int8]
      params: 参数 dict (工厂模式实例化时传入, 用于在 compute_fn 内取值)
    """

    id: str
    professional_name: str
    layman_name: str
    category: SignalCategory
    deps: tuple[DataDep, ...]
    min_warmup: int
    compute_fn: Callable[..., np.ndarray]
    params: dict[str, Any] = field(default_factory=dict)

    def compute(
        self,
        ohlcv: pd.DataFrame,
        valuation: Optional[pd.DataFrame] = None,
        financial: Optional[pd.DataFrame] = None,
        dividend: Optional[pd.DataFrame] = None,
        index: Optional[pd.DataFrame] = None,
    ) -> np.ndarray:
        """计算每日是否触发.

        返回 np.int8, shape=(len(ohlcv),), 1=触发, 0=未触发.
        前 min_warmup 行强制 0 (history 不足).
        """
        out = self.compute_fn(
            ohlcv=ohlcv,
            valuation=valuation,
            financial=financial,
            dividend=dividend,
            index=index,
            **self.params,
        )
        if not isinstance(out, np.ndarray):
            raise TypeError(
                f"atom {self.id} compute_fn 返了 {type(out)}, 期望 np.ndarray"
            )
        if out.dtype != np.int8:
            out = out.astype(np.int8)
        if len(out) != len(ohlcv):
            raise ValueError(
                f"atom {self.id} 输出 shape={out.shape} 不匹配 ohlcv len={len(ohlcv)}"
            )
        if self.min_warmup > 0:
            out[: self.min_warmup] = 0
        return out


# id -> SignalAtom 实例
ATOM_REGISTRY: dict[str, SignalAtom] = {}


def register(atom: SignalAtom) -> SignalAtom:
    """注册原子. 重名报错, 防漂."""
    if atom.id in ATOM_REGISTRY:
        raise ValueError(f"原子 id 重复注册: {atom.id}")
    ATOM_REGISTRY[atom.id] = atom
    return atom
