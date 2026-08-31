"""示例 1: 经典策略回测

用内置策略 (双均线/MACD/RSI/KDJ/布林等 13 个) 对单只股票跑回测。

用法:
    export ASTOCK_DATA_DIR=/path/to/astock_data
    python examples/run_strategy_backtest.py --stock 600519 --strategy dual_ma
    python examples/run_strategy_backtest.py --stock 600519 --strategy macd --start 20230101
    python examples/run_strategy_backtest.py --list          # 列出全部策略
"""
import argparse
import json
import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 支持不安装直接运行

from astock_backtest import (
    STRATEGY_REGISTRY,
    DataStore,
    run_backtest,
    to_digest_backtest,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="经典策略回测示例")
    parser.add_argument("--stock", help="股票代码, 例 600519")
    parser.add_argument("--strategy", default="dual_ma", help="策略 id (--list 查看全部)")
    parser.add_argument("--params", default="{}", help='策略参数 JSON, 例 \'{"fast": 5, "slow": 20}\'')
    parser.add_argument("--start", default=None, help="起始日 YYYYMMDD")
    parser.add_argument("--end", default=None, help="结束日 YYYYMMDD")
    parser.add_argument("--capital", type=float, default=100000.0)
    parser.add_argument("--list", action="store_true", help="列出全部内置策略")
    args = parser.parse_args()

    if args.list:
        for sid, cls in STRATEGY_REGISTRY.items():
            print(f"{sid:20s} {cls.display_name}")
        return

    if not args.stock:
        parser.error("--stock 必填 (或用 --list 查看策略)")

    print("加载数据 (首次约几十秒) ...")
    store = DataStore()
    result = run_backtest(
        store=store,
        stock_code=args.stock,
        strategy_id=args.strategy,
        params=json.loads(args.params),
        start_date=args.start,
        end_date=args.end,
        initial_capital=args.capital,
    )
    digest = to_digest_backtest(result)
    json.dump(digest, sys.stdout, ensure_ascii=False, indent=2, default=str)
    print()


if __name__ == "__main__":
    main()
