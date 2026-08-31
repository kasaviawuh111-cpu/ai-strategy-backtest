# 文档导航

文档按“标准、现状、设计、运行、参考”分层。规划不能作为完成证据，代码行为与文档冲突时按缺陷处理。

| 入口 | 回答的问题 | 更新规则 |
|---|---|---|
| [SPEC.md](SPEC.md) | 为什么做、做什么、交互、数据、算法、现状、验收与路线图 | 项目总规范；产品或工程语义变化时先更新 |
| [00-acceptance.md](00-acceptance.md) | 做到什么才算完成 | 验收基线；不混入进度汇报 |
| [status.md](status.md) | 目前哪些已证实、未完成或缺失 | 每次发布前按证据更新 |
| [architecture/01-technical-plan.md](architecture/01-technical-plan.md) | 目标架构、当前纵切和后续演进 | 目标与现状必须显式区分 |
| [architecture/adr/](architecture/adr/) | 为什么做出关键技术选择；含 PIT 容量、资金基准和公司行动决策 | 决策变化时新增或替代 ADR |
| [runbooks/](runbooks/) | 如何开发、部署、迁移和恢复 | 命令必须能静态核对并定期演练 |
| [reference/](reference/) | Catalog 覆盖与数据契约 | 由版本化数据或生成器校验 |
| [reference/reuse-first.md](reference/reuse-first.md) | 哪些成熟开源直接复用、哪些 A 股差异必须自有 | 引入前核对许可证、薄 adapter、双跑与回滚 |

资金账户的公司行动口径见 [A 股公司行动与资金账户契约](reference/corporate-actions-v1.md)。

产品范围、交互语义与工程护栏以本目录的 [Living Spec](SPEC.md) 为总入口；外部产品方案只保留为历史输入，不能覆盖已确认的当前契约与可复查证据。

文档规则：

1. 一个文件只回答一类问题。
2. README 只写当前可用入口；目标能力进入技术方案和验收基线。
3. 任何 `proved` 都必须链接到代码、测试或可复查运行证据。
4. `research_only`、`unavailable` 和“目录已登记”不得写成已支持。
5. 当前 checksum pin 只代表完整性校验；没有物理归档时不得称为永久快照。
6. 历史 E2E 若使用已废止的容量、基准或公司行动口径，只能保留为迁移记录，不能继续标记为验收证据。
