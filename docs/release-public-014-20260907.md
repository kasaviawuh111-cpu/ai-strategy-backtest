# 公网014：解析误拒、多轮保留与请求体积修复

状态：**014已发布，承接100%公网流量，独立公网验收通过。** 五条新会话原话均201 ready；三轮方案旅程保持用户自行提供股票，最终READY、回测succeeded并读回三类产物，手机报告页与思考文本已核对。[公网验收](releases/public-014-acceptance.json)／[部署状态](releases/public-014-deployment.json)／[本地冻结证据](releases/local-014-parse-20260907.json)

## 后台审计：不是同一种“解析失败”

通过 CloudBase `tcbr.SearchClsLog` 只读取得 **102条脱敏拒绝事件**，窗口为2026-09-07 00:00–12:10，日志时区UTC+08:00；未为采集而启用新日志服务。分版本为008共5条、009共31条、011共15条、012共46条、013共5条。[脱敏审计明细](releases/public-014-parse-audit-20260907.json)

- **102条不是102次用户失败，更不是失败率。** 同一请求可产生叶节点、参数、首轮、修正轮和最终拒绝等多条日志；服务范围还包括工程验证请求，不能认定均来自老板或同事。历史叶日志缺request_id，也不能仅按相邻时间归组。
- 012可以按request_id确认7次最终 `semantic_validation_failed`，另有1次 `request_too_large`。前者说明模型结果被本地校验拒绝，不等于模型或服务器没有连接；后者在发给供应商前就被本地字节上限拦截。
- 013截至查询窗口只检出1次首轮参数标注拒绝，未检出最终语义或传输拒绝；此前三次真实公网抽测ready也已有证据。这只能描述检索窗口和抽测，不能推导全站零失败。
- 011有一条方案Catalog拒绝明确缺 `/conditions/1/params/consecutive_days`，但日志没有对应trigger。不能因此认定所有缺字段均可补默认值，也不能用现有日志完整还原全部历史原句及模型正文。

## 本轮修复范围

相对013，补丁精确涉及以下 **7个后端运行文件**（均位于`ashare_lab/`）；测试和本文不计入运行文件数。304文件包清单已冻结，前端资产不变。[冻结清单](releases/public-014-source-manifest.json)

| 文件 | 修改与保留的约束 |
| --- | --- |
| `adapters/language/vibe_candidates.py` | 为模型已给出的均线收盘价参数补全默认来源标注；买卖混在同一原文引用时，只取唯一通过现有校验的原句子句，不改写原文。补全单日相对量能trigger不读取的`consecutive_days`结构字段。初轮真实测试又发现简单MACD省略参数仍被拒，新增仅在原句完全未指定参数时使用Catalog标准12/26/9的窄修复；显式、残缺或非标准参数不覆盖。缺参数、叶节点及参数拒绝日志关联request_id和attempt。 |
| `application/compile_strategy.py` | 未绑定股票的方案先过现有Catalog及执行口径检查；可填的相对量能非活跃结构字段与候选链路一致。绑定失败保留原方案和股票上下文，清除自动运行、刷新意图，返回执行校验诊断；保留的有效方案可以再次选择并直接绑定原DSL。选方向等待股票及最终READY都保留`instrument_suggestion_declined`，避免用户自行提供股票却被重新推荐。 |
| `api/schemas.py` | 允许`needs_clarification + idea_guidance_execution_invalid + idea_route`，避免“保留了方案”在输出层再次被拒。 |
| `api/routes/strategy_drafts.py` | 同步允许上述诊断携带方案；记录`strategy_draft_outcome`，将request_id、draft_id、revision、业务status与diagnostic_code关联。不能再只用HTTP201判断ready。 |
| `api/middleware.py` | 增加`X-Backend-Request-ID`保留应用日志关联键，避免网关覆盖标准`X-Request-ID`后无法准确查到后台请求。 |
| `adapters/language/vibe_strategy_advice.py` | 股票推荐、股票×策略配对及补查表，仅在更短时转为明确的`columns + rows`无损编码。保留全部行列、值、null、缺失与null的区别、单位、日期及原表边界；不抽样、不裁用户条件，不修改原始数据对象。 |
| `adapters/language/openai_compatible.py` | 增加安全的请求用途及超限时固定字段字节数诊断，只记字段名与大小，不记用户、模型或数据正文。256KiB请求上限和思考配置均不变，仍拒绝无法压缩到安全范围的输入。 |

本轮没有取消逐叶核验、Catalog、AND／OR、方向、周期、金额及交易口径检查；候选DSL仍来自模型，回测仍由现有确定性引擎执行。也没有为“修好所有报错”而放行未知指标或不完整交易规则。

### 请求超限的证据边界

012的request_id `48d18784-f2a8-42cc-8a77-85a03618b7ad`先有一次pro请求成功，下一次pro请求为266332字节、耗时1ms、response_bytes为0、status为None，超过本地262144字节上限。已确认**这次没有把请求发到供应商，不是供应商宕机**。

历史日志没有请求用途和分项大小，不能精确认定是哪张表膨胀。前次响应的2530400字节是SSE网络流量，不是JSON正文大小；真实transport已将JSON解析成对象，不能将“上一响应字符串二次转义”当作已证根因。

对当前真实推荐／配对请求构造器作本地合成宽表测试，结果如下。使用Mock HTTP，不冒充公网或真实供应商验收；原数据可以完整逆变换验证。

| 构造路径 | 原请求 | 无损编码后 |
| --- | ---: | ---: |
| 股票×策略配对原表 | 315572 B | 32079 B |
| 股票推荐原表 | 312257 B | 28764 B |
| 股票×策略配对补查表 | 314090 B | 30597 B |

三条原包均在网络前拒绝，编码后均单次通过Mock HTTP；全部行列和文本精确保留，`thinking=enabled`不变。这证明该重复列名膨胀机制及修复有效，不替代上述历史请求缺失的原包证据。

## 本地验收：保留初轮失败，不提前合并为“全通过”

### 第一轮真实输入

在生产形态本地入口、真实模型下，五个独立新会话各单次提交：均线5/20、美的完整RSI、平安银行价格条件、放量突破四条201 ready；**东方财富简单MACD一条201 unsupported，`candidate_provider_invalid_output`**。这些用例只做编译，不能称已分别完成回测。[首轮原始证据目录](/private/tmp/ashare-public-014-validation.4Omr2H/local-inputs)

初轮候选revision为`sha256:099bb30e1c102728ab256d49dd49c19dc98a9164a469eb572afa4b3f9c22f505`，不是最终014包。MACD缺省12/26/9修复是在此后加入；最终新会话五条全部201 ready，MACD实际返回fast12、slow26、signal9，金叉买、死叉卖。[最终真实证据](releases/local-014-parse-20260907.json)

### 第一轮三轮方案旅程

真实“生成三个均线方案 → 选择方案 → 用户提供股票”已得到ready，选定模板前后保持一致；之后回测`run:2a7cfb5b9f7b4f5b8547e83d336196c5`为succeeded。已取得summary、series、trades三类产物，242个曲线点、54条交易活动；实际观察到reasoning返回与报告页面。[原始旅程证据](/private/tmp/ashare-public-014-validation.4Omr2H/local-idea/evidence.json)

这份原始QA记录仍为`FAILED / Timed out: report UI`：脚本错误等待聊天中的“查看这次报告”，当时应用已经进入报告页。**引擎成功与脚本失败分别记录，原证据不改写为PASS。** 最终需要修正观察条件后验证实际报告页。

同轮另发现真实产品问题：用户已说“先不要推荐股票，我自己提供股票”，选方案后却丢失该标志，额外进行约89秒选股推荐。不能将其解释为单纯脚本错误。修复后完整旅程再次PASS：选方向只等待用户股票、不提供推荐，自行补东方财富后READY，原模板五类字段完全一致，回测及报告页通过；思考文本实际返回。

### 自动检查及既有基线

- 直接相关检查为**263 passed、1 deselected，4.21s**，涵盖候选归一化、原文引用、修正诊断、候选生成、推荐配对、request上下文及API配对恢复。新增偏好保留后，compiler/API定向组93 passed、6 deselected，完整配对contract文件11 passed；这些分组有重叠，不相加为总数。最终真实五例和多轮旅程也通过。
- 排除项`test_bounded_provider_exit_and_cannot_be_rewritten_as_first_of`已在原基线同样失败：ALL退出关系现已支持，旧断言尚未同步。本轮不改该无关预期，也不将其删除后称全量测试通过。
- 分项adapter检查：advice 29条、transport 41条通过；三文件Ruff、Pyright及diff检查通过。上述分组与汇总可能重叠，不相加成新的总数。
- 七个运行文件额外Pyright核对发现`vibe_candidates.py`19条既有错误；固定补丁前`f9c77b1`与本次`b1c6909`在相同strict配置、Pyright 1.1.411下，完整诊断内容、位置与规则逐项一致，新增0。不能称整个候选文件类型检查通过。[基线对比证据](/private/tmp/ashare-pyright-baseline.1VFvsP/comparison.json)
- 前端只读核对表明，新诊断携带原2–3项idea_route走既有通用DTO，卡片／股票chips仍能发proposal.id并沿用最新draft/revision；没有依据要求为此重建UI。现有DTO抽测2条通过。App交互抽测1条通过、2条在旧“标题必须含股票名”的断言失败，卡片展示及点选步骤已执行；该标题已为原有抽象策略名，不是本轮修改造成，前端文件未改。

## 冻结与公网验收

- 源码提交`b1c6909113d7aaf24d42900ae39dd6dc6cf1b74a`已推送；运行文件相对该提交无脏修改，用户其他文档与输出目录未纳入发布。
- 最终本地运行revision `sha256:624a8e3de173762bc7eb474fb23a6e8d833645add528d25f5125878a0e9cc23d`；304文件发布包revision `sha256:d64e21e1f5895e980c8397be1d96e9c3be13ae9e0b1e5dee44ecd3b0ad08eee3`。两者哈希算法／范围不同，各自有清单证据，不混用。
- CloudBase任务`2092281` finished，`ashare-strategy-preview-014`100%流量；公网`/api/v1/preview-meta`读回上述包revision。先等到新revision后才提交本轮公网测试，没有重复部署。
- 公网五个全新会话：茅台5/20、美的RSI五条件、东方财富15日均量放量突破、MACD金叉死叉、平安银行价格条件，均单次提交后201 ready，逐项核对DSL数值及关系。它们只证明本次编译，不冒充五个独立回测。
- 公网真实三轮：生成完整均线方案→选“双均线金叉死叉趋势（5/20）”→自行补东方财富；不再启动被拒绝的股票推荐，最终READY。对应MA5/20金叉、死叉或回撤10%的已选规则准确保留；本地另外验证了数据库中原模板的完整字段一致，公网未读取生产数据库。
- 公网回测`run:95e59cfc6e734b29a165276343f7e3f6` succeeded，summary／series／trades均200，手机报告实际可见；返回的深度模型思考文本也实际观察到。后台快解析仍为flash／thinking disabled，深度方案为pro／thinking enabled／high；配置原样保留，不宣称所有调用都开启思考。
- 八个验收请求的`X-Backend-Request-ID`均在真实CLS中找到对应业务终态，五条ready与三轮needs_clarification→needs_clarification→ready逐项相符；另有14条模型传输成功日志。仅核对这些实际提交的请求，不算全站成功率。[公网请求关联证据](releases/public-014-request-correlation.json)

发布约束均保持：免登录、已有能力、数据来源、临时存储、引擎计算、深度思考展示及256KiB传输上限未改。此次修复已验证的误拒、方案接续及请求膨胀机制；**不把五例新会话成功写成全站零失败率，也不整体关闭报告数字复核等无关尾项。** 缺原始上下文的历史失败保留证据边界，后续以独立request_id和业务终态定位。[剩余事项](bug-backlog-and-acceptance.md)
