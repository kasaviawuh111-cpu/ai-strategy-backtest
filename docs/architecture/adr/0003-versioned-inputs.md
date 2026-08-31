# ADR-0003：回测输入不可变并可重放

- 状态：Accepted
- 日期：2026-08-28

## 决策

每次运行创建 `RunManifest`，锁定规范 DSL、Catalog、默认值、执行/信号行情、公司行动、事件/观察、市场规则、费用、容量模式、日历、引擎版本、精确 40 位 Git SHA 和随机种子。运行结果以该清单为复现入口，不重新调用大模型。

大体量行情和运行事件保存在 Parquet/对象存储；PostgreSQL 保存版本、状态、URI、校验和与审计元数据；Redis 只用于队列、进度、限流和可丢缓存。

## 不变量

- Catalog release 和 DataSnapshot 发布后不可原地修改。
- 数据修订产生新版本，不覆盖旧回测。
- 相同 RunManifest 的事件日志和结果哈希必须一致。
- strict composite 或 production 的 dirty 工作树、伪 revision 和缺失公司行动/事件观察文件必须拒绝就绪。
