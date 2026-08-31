"""涨跌停板识别 (按股票代码前缀判断板块)

阈值留 0.2% 容差 (实际涨停常为 9.97%~10.00%, 避免漏判)
ST 股 5% 限制暂不处理, 留 v2
"""


def limit_threshold(stock_code: str) -> float:
    """返回涨跌停幅度阈值 (百分数值, 如 9.8 表示 9.8%)"""
    if not stock_code or len(stock_code) < 3:
        return 9.8
    prefix3 = stock_code[:3]
    prefix2 = stock_code[:2]
    if prefix3 in ("300", "301"):
        return 19.6
    if prefix3 in ("688", "689"):
        return 19.6
    if prefix2 in ("43", "83", "87", "88") or prefix2 in ("82", "92"):
        return 29.4
    return 9.8


def is_limit_up(pct_change: float, stock_code: str) -> bool:
    if pct_change is None:
        return False
    return pct_change >= limit_threshold(stock_code)


def is_limit_down(pct_change: float, stock_code: str) -> bool:
    if pct_change is None:
        return False
    return pct_change <= -limit_threshold(stock_code)
