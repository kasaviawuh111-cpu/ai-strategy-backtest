"""示例 4: 注册一个自己的信号原子 (核心扩展点)

演示三步加因子:
  1. 写 compute 函数 — 输入 ohlcv 等 DataFrame, 返回 np.int8 数组 (1=当日触发)
  2. register(SignalAtom(...)) 入库 — 声明数据依赖 (deps) 和所需历史天数 (min_warmup)
  3. 直接在 run_backtest_custom 里按 id 使用 — 不需要改任何引擎代码

用法:
    export ASTOCK_DATA_DIR=/path/to/astock_data
    python examples/custom_atom_demo.py --stock 600519
"""
import argparse
import json
import sys

import numpy as np

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 支持不安装直接运行

from astock_backtest import (
    BacktestCustomRequest,
    DataDep,
    DataStore,
    SignalAtom,
    SignalCategory,
    register,
    run_backtest_custom,
)


# ── 第 1 步: 写计算函数 ─────────────────────────────────────────────
# 约定: 接收 ohlcv/valuation/financial/dividend/index 关键字参数 (按 deps 喂入),
# params 里的自定义参数也会以关键字传入; 返回 np.int8 数组, 长度 = len(ohlcv)。
def compute_breakout_with_volume(ohlcv, n=60, vol_mult=1.5, **kwargs):
    """放量创 N 日新高: 收盘价 > 前 N 日最高收盘 且 成交量 > N 日均量 * vol_mult"""
    close = ohlcv["close"].to_numpy()
    volume = ohlcv["volume"].to_numpy(dtype="float64")

    prev_high = (
        ohlcv["close"].rolling(n).max().shift(1).to_numpy()
    )
    vol_ma = ohlcv["volume"].rolling(n).mean().to_numpy()

    out = (close > prev_high) & (volume > vol_ma * vol_mult)
    return np.nan_to_num(out).astype(np.int8)


# ── 第 2 步: 注册入库 ───────────────────────────────────────────────
register(SignalAtom(
    id="my_breakout_60d_volume",
    professional_name="放量创 60 日新高",
    layman_name="价格突破近三个月高点且成交明显放大",
    category=SignalCategory.PRICE_ACTION,
    deps=(DataDep.OHLCV,),          # 只依赖日线表; 需要估值/财务时加 DataDep.VALUATION 等
    min_warmup=61,                  # 前 61 天数据不足, 引擎自动强制为 0
    compute_fn=compute_breakout_with_volume,
    params={"n": 60, "vol_mult": 1.5},
))


# ── 第 3 步: 直接用 ─────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="自定义信号原子示例")
    parser.add_argument("--stock", required=True, help="股票代码, 例 600519")
    args = parser.parse_args()

    req = BacktestCustomRequest(
        stock_code=args.stock,
        buy_atoms=["my_breakout_60d_volume"],   # 刚注册的原子, 按 id 引用
        holding_days=10,                        # 触发后固定持有 10 个交易日
    )
    print("加载数据 (首次约几十秒) ...")
    store = DataStore()
    result = run_backtest_custom(req, store)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, default=str)
    print()


if __name__ == "__main__":
    main()
