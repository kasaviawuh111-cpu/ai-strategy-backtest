# B14 / B15 优化追问链路只读审查

记录日期：2026-09-06。项目目录：`/Users/mima0000/Documents/回测`。

## 状态与证据边界

本记录仅保存本轮源码只读审查结论，**未实施修复、未运行测试、未调用真实模型或数据、未验证浏览器链路，也未发布**。本文不是 B14 / B15 验收通过证明。

用户随后反馈 B28，主线程转向原位编辑预览；B14 / B15 本轮暂停实施。保存本文不改变问题清单状态，不表示下面的实施建议已经获选或完成。下一 Go 轮继续前应重新核对源码，文中行号仅定位审查时版本，以函数名为准。

本次只新增这份任务文档；没有改生产代码、测试或问题清单，未执行任何 Git 操作。

## 验收原句与预期

来源为 [问题清单](/Users/mima0000/Documents/回测/docs/bug-backlog-and-acceptance.md:46)。

| 编号 | 原句 / 操作 | 应观察到的结果 |
| --- | --- | --- |
| B14 | 已有亏损报告后输入“你推荐的策略怎么还是亏钱的？”；再输入“改一下，给我一个你认为更合理的方案并回测” | 首句承接实际亏损，不擅自执行；第二句形成具体新方案，并按明确运行意图取得真实新报告。不能保证盈利或以相对跑赢淡化亏损。 |
| B15 | 报告后点击一个优化 chip，完成后输入“这个已经试过了，还有别的方向吗？” | 新候选不重复已尝试方案；旧报告和新报告均保留。比较真实变化，不把亏损或零成交称为优化成功。 |

## 当前关键路径

### 1. 自然语言追问与编辑

- [VibeStrategyEditor](/Users/mima0000/Documents/回测/ashare_lab/adapters/language/vibe_strategy_editing.py:80) 的 `_ProviderEdit` 区分 `discuss`、`request_optimization`、`select_optimization`、`apply` 等意图。
- 审查时 `_ProviderEdit.requires_exactly_one_strategy_for_apply` 仅允许 `apply / change_instrument / select_optimization` 携带 `run_requested=true`，`request_optimization` 不允许。
- 同文件约第 182–197 行的 Prompt 要求“给调整方向 / 优化方案”返回 `request_optimization`、`strategy=null`、`run_requested=false`，并说明本轮只提出候选。亏损质疑本身返回 `discuss` 是合理边界；问题是“给一个更合理的方案并回测”没有独立、明确的执行路径描述，容易同样落入只提候选的分支。
- [编译器编辑分支](/Users/mima0000/Documents/回测/ashare_lab/application/compile_strategy.py:609) 将 `discuss / request_optimization` 转为 `NEEDS_CLARIFICATION`；后者设置 `strategy_optimization_requested`，保留基线策略和费用，但不传运行意图。

### 2. 根据报告生成优化建议

当前实际函数名是 [_requested_backtest_review](/Users/mima0000/Documents/回测/ashare_lab/api/routes/strategy_drafts.py:1775)，不是 `_resolve_optimization_request`。

- 初次提交和澄清答复两条 API 路径都会调用它；澄清路径只在 `revision_changed` 时处理。
- 它从当前对话报告中找完整策略和成交配置都匹配的最近已完成报告。没有匹配报告时返回明确不可优化提示；不应取消这道版本边界。
- 找到报告后调用 [build_backtest_review](/Users/mima0000/Documents/回测/ashare_lab/api/routes/backtest_runs.py:291)，传 `run_id` 和本轮 `user_request`。
- 返回后使用 `review.analysis` 与 `review.conclusion` 作为正文，附带候选，但不修改策略、不创建新回测。两段文字均来自模型；这里不是规则生成优化方案。

**为什么可能看起来只复述旧报告：** `build_backtest_review` 每次都会调用配置的 review advisor，并非直接取旧摘要缓存；但它收到的事实仍是原基线报告，新运行尚未发生。无论再次生成多少次摘要，都不能据此称已经优化或回测了新方案。

### 3. 前端执行门与候选点击

- [App.tsx 编译成功处理](/Users/mima0000/Documents/回测/web/src/App.tsx:806) 只在响应为 `compiled` 且 `runRequested=true` 时保留自动重跑标记；澄清成功处理采取同样判断。
- [自动重跑 effect](/Users/mima0000/Documents/回测/web/src/App.tsx:1149) 还要求草稿可运行、无待澄清、无现有运行等条件。提交时的前端 `options.rerun` 不能替代后端授权；不应为修 B14 绕过这道门。
- 直接点击已有候选走 [optimizationMutation](/Users/mima0000/Documents/回测/web/src/App.tsx:1068) → [backtestApi.createOptimization](/Users/mima0000/Documents/回测/web/src/shared/api/client.ts:504)。后者保存准确的候选 DSL，再提交新回测，不重新解析 `suggestedUtterance`。
- [fromBacktestOptimizationCandidate](/Users/mima0000/Documents/回测/web/src/shared/api/contract.ts:694) 基于来源草稿投影候选，继续使用来源成交配置。前端成功处理保留来源报告并切换到新草稿 / 运行。这些是源码观察，本轮没有点击验收。

## B15 已确认的历史缺口

1. [BacktestReviewRequest](/Users/mima0000/Documents/回测/ashare_lab/ports/backtest_review.py:23) 目前只带本次策略、结果、证据分级和用户问题，没有“已运行方案”“已展示候选”或上一版比较结果。
2. [VibeBacktestReviewAdvisor](/Users/mima0000/Documents/回测/ashare_lab/adapters/language/vibe_backtest_review.py:143) 的模型输入因此只知道当前基线，无法可靠识别此前尝试过的所有方案。Prompt 只要求本批互不相同且不同于基线。
3. [build_backtest_review 的 seen_hashes](/Users/mima0000/Documents/回测/ashare_lab/api/routes/backtest_runs.py:340) 只从当前基线 hash 开始，再排除本批重复；没有历史排重。换一轮生成时，同样的旧方案仍可能再次出现。
4. 新报告的自动 AI 分析仍只收到本次结果及同股持有基准，没有上一版策略结果；不能据此声称比上次变好。现有编辑模型的 `_report_references` 已能根据完整策略和成交配置选定 `current / previousDifferentStrategy / earliest`，可复用其版本定义，避免另造比较标准。

## 最小实施建议（待实施）

### 优先处理 B14 的明确重跑

优先复用现有 `apply` 链路，不新增一套执行器：

1. Prompt 明确区分“只问原因”“要几个方向但不运行”“授权模型制定一条方案并立即回测”。
2. 对 B14 第二句，允许模型依据当前规则和已核验报告产生一条具体新 `StrategySpec`，返回 `apply + run_requested=true`。模型生成条件，代码只做现有 Catalog、股票、日期、费用及执行边界检查。
3. 校验成功返回 `READY`，沿现有前端自动重跑入口执行。新结果出来前不得声称改善或盈利。
4. 仅请求多个方向、先看看或明确不运行时，继续走 `request_optimization`，维持无自动执行的边界。

如果最终选择先让深度 review 生成多个候选再执行，也必须让模型明确选定其中一条，并保留用户授权；不能让服务端未经模型选择就固定取候选一。本轮未选择或实施这个替代方案。

### 再处理 B15 的历史排重与比较

1. 利用本会话已通过服务端加载的 `state.backtest_results`，给 review 模型传已执行策略、实际费用与结果；同时带当前已展示、可核验来源的候选。不要信任浏览器自报指标。
2. 明确区分“已展示但未跑”与“已真实运行”，避免把两者当成同一种尝试记录。
3. 模型据历史提出不同方向；服务端在现有 `seen_hashes` 边界进一步排除完全相同方案。比较执行版本时仍使用完整策略和成交配置；不要只按标题或 `model-opt-1` 这种局部编号排重。
4. 真正的新报告完成后，使用同股票、同区间、已记录费用的新旧报告比较收益、回撤和交易次数；区间或费用不同应点明。零成交、仍亏损或变差均如实表述。
5. 不增加专门历史数据库或新模型客户端。当前对话引用最多保留近期 20 个运行；未加载到的更早历史不能声称已排除。先覆盖 B15 原句，不扩成无限优化搜索。

## 定向测试入口与缺失用例

以下是现有入口，仅记录名称，**本次没有执行**。这些测试中的 fixture 通过也不能代替真实模型 / 数据验收。

- [test_vibe_strategy_editing.py](/Users/mima0000/Documents/回测/tests/unit/adapters/language/test_vibe_strategy_editing.py)：
  - `test_loss_feedback_keeps_user_goal_and_does_not_defend_or_execute_recommendation`
  - `test_non_editing_model_reply_cannot_request_execution`
  - `test_optimization_selection_uses_exact_server_candidate_and_rejects_stale_base`
  - `test_version_comparison_keeps_ordered_reports_and_distinguishes_previous_from_earliest`
- [test_backtest_review_contract.py](/Users/mima0000/Documents/回测/tests/contract/api/test_backtest_review_contract.py)：
  - `test_optimization_request_preserves_rules_and_runs_through_both_draft_endpoints`
  - 现有输入是“给几个方向，不要跑”和“改下，我要一个盈利的策略”，断言不创建运行；不要为了 B14 放宽这些无明确立即执行授权的断言。
- [client.test.ts](/Users/mima0000/Documents/回测/web/src/shared/api/client.test.ts:253)：
  - `submits a reviewed server-compiled strategy directly as a fresh run`

建议只补本次改变直接需要的回归：B14 明确授权重跑产生新策略并保留运行意图；B15 已执行方案不再被作为新候选，以及旧新结果角色保持正确。若复用 `apply`，无需更改“非编辑回复不可运行”的 schema 测试。

## 下一 Go 轮执行顺序

1. 确认 B28 当前改动与服务状态，避免正在运行真实请求时触发后端重载。
2. 复核本记录的函数和分支仍有效，实施 B14 最小链路，再做最小定向回归。
3. 用正式本地页面、真实模型和真实东方财富数据走 B14 两句原文，核对新策略、真实新运行终态与新旧报告；单有 `READY` 或 `queued` 不算通过。
4. 接着实施并验收 B15 原句，确认返回不同候选、点击后的实际策略与报告一致，且没有虚称改善。
5. 根据真实结果记录剩余问题，不提前将 B14 / B15 标成已修或已验收，不发布未经本地验收的版本。
