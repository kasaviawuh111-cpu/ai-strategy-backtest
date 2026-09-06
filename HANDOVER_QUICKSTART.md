# Quickstart：A 股自然语言回测接手

## 先看结论

- 仓库：`/Users/mima0000/Documents/回测`
- 分支：`codex/public-live-integration`
- HEAD：`b165bd3734c04d7ba68e60f76549da17aaedc870`
- 工作区：脏；27 个已跟踪修改、7 个未跟踪源码/测试文件，另有本交接文档。禁止直接提交或部署。
- 后端全量：`2476 passed, 2 failed, 1 skipped`；失败为 35→36 指标固定计数。
- 前端：179 tests pass；same-origin 22 pass；typecheck/lint/build:same-origin pass。
- 固定 WorkBuddy 前端可打开但 bundle 是 Mock。
- 公网 API 当前 HTTP 503 `SERVICE_FORBIDDEN`。
- 当前没有可证明的公网 Live 全链路。

## 第一小时

```bash
cd /Users/mima0000/Documents/回测
git status --short --branch
git diff --stat && git ls-files --others --exclude-standard
```

然后阅读：

```text
HANDOVER.md
docs/SPEC.md
docs/00-acceptance.md
THIRD_PARTY.yml
catalogs/signals/cn_a_technical.v1.manifest.json
contracts/strategy.v1.schema.json
ashare_lab/domain/strategy/models_v2.py
```

## 最小本地校验

```bash
uv sync --frozen
pnpm --dir web install --frozen-lockfile
make check
pnpm --dir web test
pnpm --dir web typecheck
pnpm --dir web lint
pnpm --dir web build:same-origin
```

已知 `make check` 不是全绿；不要为了绿而机械改测试，先确认第 36 个指标的产品与运行语义。

## 当前真实边界

- 技术：脏工作区目录 36 stable/134 triggers；不是全口语、全股票或公网覆盖证明。
- 财务：当前执行证据只到 `valuation.pe/pb/ps/pcf` 的接口直接值；其他 fail closed。
- 事件：真实本地快照只证明五类定期报告；事件本轮不作为发布承诺。
- 标的：300059 有本地日线；510300 只有证券身份、无本地行情；指数不支持且不得映射 ETF。
- 回测：日线、只做多、T+1、下一可交易日 09:30 开盘代理成交、同口径买入持有基准。
- 分钟：只有模型/探针，没有可执行链路。

## 开源策略

- 继续以薄适配为主：astock 基础、Vibe Trading 的观点候选、MOSS 公式参考。
- 不要复制整仓；A 股 PIT、数据许可、T+1、涨跌停、审计与 DSL 闸门必须保留。
- 许可证事实以 `THIRD_PARTY.yml` 为准。mootdx 已因许可冲突阻止；BaoStock 条款待确认。

## 发布前五道门

1. 工作区拆分审查、全量绿、clean commit。
2. CloudBase 当前版本/流量/镜像/环境事实查清，数据库可持久恢复。
3. API `/ready` 200；技术 smoke 最终 `succeeded`，不是 202。
4. 前端 bundle 明确无 `mock_demo`，浏览器 Network 命中公网 API。
5. run 可反查原话、DSL、snapshot、子 hash、provider、完整 Git SHA 和 result hash。

## 当前五大风险

1. 脏工作区和失败测试使代码身份不可复现。
2. 公网 API 被隔离，固定前端仍是 Mock。
3. v1 草稿/默认 SQLite/线程队列不具备生产重启恢复。
4. 真实数据只覆盖一只股票和有限事件，不能宣传全 A 股/69 事件。
5. API 无认证，数据商业授权与再分发权未确认。

所有未知项见 `HANDOVER.md` 的“待确认”。
