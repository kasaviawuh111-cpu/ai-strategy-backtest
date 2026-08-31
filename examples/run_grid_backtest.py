"""示例 3: 网格交易回测

区间 [下限, 上限] 划 N 个网格, 价格下穿格线买、上穿格线卖。
含 A 股细节: 底仓建仓 / 每槽 T+1 保护 / 佣金+印花税+滑点 / 涨跌停 / 组合止损熔断。

用法:
    export ASTOCK_DATA_DIR=/path/to/astock_data
    python examples/run_grid_backtest.py --stock 601398 --lower 4.5 --upper 6.5 --grids 10
"""
import argparse
import json
import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 支持不安装直接运行

from astock_backtest import DataStore, run_grid_backtest


def main() -> None:
    parser = argparse.ArgumentParser(description="网格交易回测示例")
    parser.add_argument("--stock", required=True, help="股票代码, 例 601398")
    parser.add_argument("--lower", type=float, required=True, help="网格价格下限")
    parser.add_argument("--upper", type=float, required=True, help="网格价格上限")
    parser.add_argument("--grids", type=int, default=10, help="网格数量 (2-50)")
    parser.add_argument("--start", default=None, help="起始日 YYYYMMDD")
    parser.add_argument("--end", default=None, help="结束日 YYYYMMDD")
    parser.add_argument("--capital", type=float, default=100000.0)
    parser.add_argument("--base-ratio", type=float, default=0.5, help="底仓比例 (0-1)")
    args = parser.parse_args()

    print("加载数据 (首次约几十秒) ...")
    store = DataStore()
    result = run_grid_backtest(
        store=store,
        stock_code=args.stock,
        price_lower=args.lower,
        price_upper=args.upper,
        grid_count=args.grids,
        start_date=args.start,
        end_date=args.end,
        initial_capital=args.capital,
        base_position_ratio=args.base_ratio,
    )
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, default=str)
    print()


if __name__ == "__main__":
    main()
