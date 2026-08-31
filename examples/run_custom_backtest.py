"""示例 2: 信号原子组合回测

从原子库 (ATOM_REGISTRY) 里挑 1-2 个买入信号 (AND 关系), 配一个持有规则, 跑单股回测。

用法:
    export ASTOCK_DATA_DIR=/path/to/astock_data
    python examples/run_custom_backtest.py --stock 600519 --atoms macd_golden_cross --hold-days 10
    python examples/run_custom_backtest.py --stock 600519 --atoms macd_golden_cross,vol_ratio_2 --stop-loss 5 --take-profit 15
    python examples/run_custom_backtest.py --list-atoms     # 列出全部原子
"""
import argparse
import json
import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 支持不安装直接运行

from astock_backtest import (
    ATOM_REGISTRY,
    BacktestCustomRequest,
    DataStore,
    run_backtest_custom,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="信号原子组合回测示例")
    parser.add_argument("--stock", help="股票代码, 例 600519")
    parser.add_argument("--atoms", help="买入信号 atom_id, 逗号分隔 1-2 个 (AND 关系)")
    parser.add_argument("--hold-days", type=int, default=None, help="固定持有 N 个交易日")
    parser.add_argument("--stop-loss", type=float, default=None, help="止损百分比, 例 5 = 跌 5%% 止损")
    parser.add_argument("--take-profit", type=float, default=None, help="止盈百分比")
    parser.add_argument("--start", default=None, help="起始日 YYYYMMDD")
    parser.add_argument("--end", default=None, help="结束日 YYYYMMDD")
    parser.add_argument("--list-atoms", action="store_true", help="按类别列出全部信号原子")
    args = parser.parse_args()

    if args.list_atoms:
        by_cat: dict[str, list] = {}
        for atom in ATOM_REGISTRY.values():
            by_cat.setdefault(atom.category.value, []).append(atom)
        for cat, atoms in sorted(by_cat.items()):
            print(f"\n== {cat} ({len(atoms)}) ==")
            for a in atoms:
                print(f"  {a.id:32s} {a.professional_name}")
        return

    if not args.stock or not args.atoms:
        parser.error("--stock 与 --atoms 必填 (或用 --list-atoms 查看原子库)")

    req = BacktestCustomRequest(
        stock_code=args.stock,
        buy_atoms=args.atoms.split(","),
        holding_days=args.hold_days,
        stop_loss_pct=args.stop_loss,
        take_profit_pct=args.take_profit,
        start_date=args.start,
        end_date=args.end,
    )
    print("加载数据 (首次约几十秒) ...")
    store = DataStore()
    result = run_backtest_custom(req, store)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, default=str)
    print()


if __name__ == "__main__":
    main()
