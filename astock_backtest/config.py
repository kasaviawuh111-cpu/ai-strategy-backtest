"""数据目录配置

环境变量 ASTOCK_DATA_DIR 指向 parquet 数据目录 (astock-data-toolkit 的输出目录),
默认为当前工作目录下的 ./astock_data。

需要的 6 张表 (schema 约定见 README「数据从哪来」):
  stock_list.parquet / daily_ohlcv.parquet / index_daily.parquet /
  valuation_daily.parquet / financial_quarterly.parquet / dividend_history.parquet
"""
import os
from pathlib import Path


class Settings:
    DATA_DIR: Path = Path(os.getenv("ASTOCK_DATA_DIR", "./astock_data"))


settings = Settings()
