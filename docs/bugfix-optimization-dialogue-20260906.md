# B14 / B15 优化追问：已实施范围与待验收记录

日期：2026-09-06。项目目录：`/Users/mima0000/Documents/回测`。

最新进展见 [B14/B15真实链路记录](bugfix-optimization-live-20260906.md)：B14两句原文已在正式页面连续完成；B15候选点击、后续隔离接口的历史排重及准确选择已真实执行，另发现并继续修复确认文字串版。两项仍待用户验收，未关闭、未公开发布。

下文保留此前“代码及定向回归阶段”的记录，所述尚未真实验收、前端测试失败等是当时状态；后续真实运行及测试修复以以上新记录为准，不抹掉中间失败。

前序只读审查见 [optimization-followup-audit-20260906.md](optimization-followup-audit-20260906.md)。该审查中的“尚未实施”描述的是当时状态，本文记录之后实际实施的增量。

## 原始验收场景

| 项目 | 原句或操作 | 仍需在正式本地页面验证的结果 |
| --- | --- | --- |
| B14 | 已有亏损报告后输入“你推荐的策略怎么还是亏钱的？”；再输入“改一下，给我一个你认为更合理的方案并回测” | 第一句承接实际亏损且不执行；第二句形成具体的新方案并产生真实新报告。不能承诺盈利或用相对跑赢淡化亏损。 |
| B15 | 报告后点击一个优化 chip，完成后输入“这个已经试过了，还有别的方向吗？” | 新候选不重复所提供历史中的已运行、已展示方案；旧报告与新报告均保留，按真实指标说明变化，不把亏损、零成交或变差说成优化成功。 |

## B14 已实施

- `VibeStrategyEditor` 的 Prompt 更新为 `strategy-edit.prompt.v22`，明确区分只问原因、只要多个候选，以及授权模型制定一条方案并立即回测。
- 对第二句原文，Prompt 要求模型返回 `apply`、完整的新 `StrategySpec` 和 `run_requested=true`。模型选择一项买入或卖出调整，另一侧规则、股票、日期、本金和成交配置保留；候选条件继续受现有能力目录约束。
- “先给几个方向”“先别跑”“只看看”继续走不执行的候选路径；单纯亏损质疑继续走讨论路径。已有明确候选仍按准确候选选择，不根据说明文字重新生成一份策略。
- 复用现有编辑、Catalog 校验和前端自动重跑入口。未新增执行器，也未绕过后端 `run_requested` 授权门。新报告出现前不得声称盈利或改善。

上述是模型合同与程序传递的实施结果。fixture 返回 `apply` 后能保留运行意图，并不证明真实模型已经会把原句稳定识别为 `apply`；这仍需下文真实链路验收。

## B15 已实施

### 服务端历史与版本边界

- `BacktestReviewRequest` 新增已完成运行、已展示方案和报告角色上下文。`build_backtest_review(..., dialogue_results=...)` 从对话事实提取引用，再利用现有 `load_backtest_dialogue_results` 和服务端存储重新读取结果与准确的 review 版本。
- 历史运行必须已经完成且结果通过内容完整性校验；未校验旧结果、被修改的结果或浏览器直接提交的指标不能进入模型比较。策略、实际成交设置、费用和结果均来自服务端存储。
- 同一次运行可携带多个曾展示的 review 响应指纹。模型明确区分 `completed` 与 `unrun`，未运行方案不能充当收益证据。
- 历史排重依据完整策略和对应的实际成交设置，不依据标题或 `model-opt-1` 这种批次内编号。服务端排除与当前成交设置相同的历史完整策略及已展示候选；本批候选也继续排重和做固定边界校验。过滤后不足两条则返回既有候选不可用错误，保留已完成报告，不伪造替代方案。
- 报告角色为本次 `current`、本次之前最近一个不同策略或成交配置版本 `previousDifferentStrategy`、所提供历史中的最早报告 `earliest`。重复运行同一完整版本不充当上一不同版本，仅改费用也属于不同版本。
- 股票、回测设置、实际数据区间或成交设置不一致时标记比较受限；历史费用不完整时不填入当前默认费用。模型收到收益、最大回撤和交易次数的真实新旧值。
- 模型最多使用近期 20 个运行、20 个 review 引用。缺失的历史 review 可省略，并向模型明确提供不可读取数量；不能声称排除了全部历史。结果完整性不匹配仍阻断。单个准确选择引用失效仍返回明确错误，不能猜选。

### 接口与前端传递

`POST /api/v1/backtest-runs/{run_id}/review` 增加可选请求体，无请求体的旧调用仍可用。

```json
{
  "related_run_ids": ["run:previous", "run:current"],
  "related_reviews": [
    {
      "run_id": "run:previous",
      "response_hash": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  ]
}
```

示例仅说明结构，不是本轮真实运行证据。两个数组各最多 20 项；review 来源运行须属于引用运行范围。请求体不接受浏览器自报收益、成交等事实。

- 创建草稿和澄清接口也新增可选 `related_reviews`，保留原单个 `related_review` 供准确选择使用。两条接口均把引用传入同一历史加载器，优化追问调用继续把已加载对话结果传给 review。
- 前端保存当前浏览器标签页中曾展示的 review 指纹，保留同一运行的多个响应版本；按近期运行过滤引用，随创建、澄清和下一次报告 review 一起发送。显式新对话会清空这份引用历史。
- 自动分析新报告和手动分析已有报告都携带相应的会话引用。无引用时维持无请求体调用；Mock 模式不伪造 AI 分析或优化回测。
- 复用原候选点击提交入口：提交服务端已校验候选的准确 DSL，使用来源草稿的成交配置，并保留旧报告。没有增加另一套客户端执行流程。

### 联调审查后补修的两个问题

1. 多个历史报告同时具有可选择的单个 `review` 时，现有选择器会在遇到旧策略时拒绝当前候选。已修为所有历史批次仅进入 `reviews`，只有明确单个引用对应的准确 `(run_id, response_hash)` 才进入选择器使用的 `review`。定向用例已验证旧版与当前版都曾展示时，当前 `model-opt-1` 能准确解析。
2. 新旧指标比较要求曾与“只谈盈亏及下一步”的文案限制冲突。review Prompt 最终更新为 `backtest-review.prompt.v11`：普通版本比较简短使用收益及回撤或交易次数，零成交优先说明；亏损质疑优先承接未达目标。仍保持两行、每行最多 56 字，不堆全部指标，不承诺改善。

## 已执行的最小验证

以下均为本地 fixture、合同测试或静态检查，不是正式模型、真实行情与用户验收。执行范围存在重叠，不累计为一个总通过数。

| 验证 | 实际结果 | 证据范围 |
| --- | --- | --- |
| B14 编辑模型定向测试 | 根线程记录 `8 passed` | Prompt v22 与编辑结果传递、亏损回应及不执行边界。 |
| review API 合同与 review adapter 单元文件 | `45 passed in 2.48s` | 历史已运行／未运行排重、多批次来源、版本角色、费用差异、完整性、引用上限及旧调用。发生在上述两个联调补修之前。 |
| 既有报告讨论上下文定向测试 | `6 passed, 24 deselected in 0.32s` | `test_strategy_editing_contract.py -k result_discussion_loads_reports`，报告上下文继续传递且保留编辑行为。 |
| 联调补修后的三个直接用例 | `3 passed, 42 deselected in 0.49s` | 旧／当前多批次下准确选择、已存 review 版本兼容，以及比较上下文与文案条件。未把补修前的 45 项表述为补修后再次全跑。 |
| B15 前端 API／序列化两个测试文件 | `78 passed, 1 failed`，共 79 项 | `contract.test.ts`、`client.test.ts`。唯一失败为既有进度显示用例“latest eight backend steps”：测试期待 8，既有 `DIALOGUE_PROGRESS_LIMIT=12` 使该输入得到 9；B15 没有修改该进度逻辑或断言，不能将此组写成全绿。 |
| B15 前端会话与 B26 原分析重试定向用例 | `2 passed, 65 skipped` | `App.test.tsx -t 'B15\|B26 offers the existing analysis retry'`；验证多版指纹经创建／澄清传给后续 review，新对话清空，并保持现有重试入口。 |
| 静态检查 | 相关 Ruff 通过；生产代码 Pyright `0 errors`；B15 前端 `tsc -b --pretty false` 退出码 0 | 只证明对应静态检查通过。前端文件随后交回 B28 修改，不将这次 TypeScript 结果外推为全部后续 UI 改动的验证。 |

后端完整 review 文件的测试入口：

```sh
.venv/bin/pytest -q tests/contract/api/test_backtest_review_contract.py tests/unit/adapters/language/test_vibe_backtest_review.py
```

联调补修后的定向选择覆盖 `rejects_completed_and_exposed`、`completed_verified_run`、`receives_executed_versions`。没有运行全量后端或前端测试套件，也没有为本文重新执行测试。

## 主要修改位置

- 后端编辑与调用：`ashare_lab/adapters/language/vibe_strategy_editing.py`、`ashare_lab/api/routes/strategy_drafts.py`。
- 后端 review 与合同：`ashare_lab/ports/backtest_review.py`、`ashare_lab/adapters/language/vibe_backtest_review.py`、`ashare_lab/api/routes/backtest_runs.py`、`ashare_lab/api/schemas.py`。
- 前端引用保存与请求：`web/src/App.tsx`、`web/src/shared/api/client.ts`、`web/src/shared/api/contract.ts`、`web/src/shared/api/types.ts`。
- 对应回归：上述编辑／review 后端测试文件，以及 `web/src/App.test.tsx`、`web/src/shared/api/client.test.ts`、`web/src/shared/api/contract.test.ts`。

## 尚未完成的验收

1. 在正式本地页面用真实模型执行 B14 两句原文，确认首句不新增运行，第二句形成实际不同的策略，并通过现有入口产生新的真实回测终态。
2. 对新运行核对 `succeeded`、summary、series、trades 及结果指纹，确认股票、日期、本金与成交设置符合用户授权。只有 `ready`、`queued` 或页面渲染成功不足以通过。
3. 执行 B15 原始点击与追问，检查模型给出不同候选、候选点击后实际执行的策略一致、旧新报告都能查看；再验证多版已展示候选下的准确文字选择。
4. 对照实际新旧收益、回撤、交易次数与费用，核验两行分析没有虚称盈利、改善或已排除所有历史。记录零成交、仍亏损、变差和比较受限时的真实文案。
5. 完成用户审核后再更新问题总表。B28 的当前页面验收、其他条目的既有真实回测或静态检查，均不能替代 B14 / B15 的验收。

本轮 B14 / B15 实施与本文记录均未调用新的真实模型／数据任务，未进行对应真实浏览器验收，也未公开发布。
