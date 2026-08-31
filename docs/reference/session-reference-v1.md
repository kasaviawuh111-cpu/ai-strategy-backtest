# A 股逐日证券状态契约 v1

`instrument_sessions.parquet` 为每只股票、每个日线交易日提供撮合所需的历史事实。它与 OHLCV 分开管理，避免从代码前缀或当天价格反推板块、ST、停牌和涨跌停规则。

## 主键与覆盖

- 主键：`stock_code + date`；不得重复。
- `stock_code`：六位代码，不带交易所后缀。
- 回测加载的每一根日线必须恰好对应一行；缺行、多行、跨标的行均拒绝执行。
- 文件在进程启动时固定 SHA-256；读取前后再次核验，内容变化即失败。

## 必需列

| 列 | 类型/示例 | 约束 |
|---|---|---|
| `stock_code` | string，`300059` | 六位 A 股代码 |
| `date` | date | 交易日 |
| `board` | enum | `main`、`chinext`、`star`、`bse` |
| `trading_status` | enum | `trading`、`suspended`、`delisted` |
| `previous_close` | decimal | 大于 0 |
| `upper_limit` | decimal/null | 与 `lower_limit` 同时有值或同时为空 |
| `lower_limit` | decimal/null | 有值时满足 `lower < previous_close < upper` |
| `lot_size` | integer | 大于 0 |
| `price_tick` | decimal | 大于 0 |
| `t_plus_one` | boolean | 必须是真布尔值 |
| `is_st` | boolean | 必须是真布尔值 |

## 生产要求

生产环境必须设置：

```dotenv
APP_ENV=production
SESSION_REFERENCE_MODE=parquet
SESSION_REFERENCE_PATH=/app/var/data/instrument_sessions.parquet
```

这里的 `/app/var/data` 是容器内固定挂载点；host 必须通过 `MARKET_DATA_PATH` 显式挂载一个内容寻址的 strict composite 目录，不能指向仓库 legacy `var/data`。

仓库 demo 文件虽然满足结构契约，但来源是研究推导，且假设东方财富历史期间没有 ST，不能作为全 A 股生产证券状态源。正式数据必须具备授权、逐日历史状态、可追溯版本与可按 SHA-256 找回的原始文件。

实现与验证入口：

- Adapter：`ashare_lab/adapters/market_data/parquet_sessions.py`
- 单元测试：`tests/unit/adapters/test_parquet_sessions.py`
- 运行装配：`ashare_lab/bootstrap.py`
