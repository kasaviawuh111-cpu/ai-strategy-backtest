# B24：独立 MVP 发布记录

日期：2026-09-06。用户已明确“重新建一个测试地址”，旧 WorkBuddy 演示页和原 live/api 服务不覆盖。

## 当前结论

### 最新要求：公开免登录，取消测试账号

- 用户明确要求本批所有人直接使用，不存在测试账号区分。先前受邀 Basic Auth 由助手额外加入，不是本批需求；已改为显式 `PREVIEW_ACCESS_MODE=public`，公开版本不注入用户名／密码。
- 后台模型密钥、完整 HTTPS Origin 校验、短窗口限流、单实例任务容量和临时存储保持；不新增账号系统，不改原站。
- 本地76项定向检查、Ruff、Pyright通过。公开入口真实草稿 `cac8dd0b-c42a-4549-9236-088aea56e640` ready／201，运行 `run:fb4889c57281491088ce88aa252e7e79` succeeded，东方财富243个净值点及18条交易记录。
- 公开包：`/private/tmp/ashare-public-mvp-release.7GFN7Y`，内容摘要 `244dd789e136c3eb8901c50b7ae525d2567cd17e5d3474ff0bab938e6284a8be`。前端与上版一致，复用通过构建的产物；只有访问配置和对应说明发生变化。已提交任务 `2088497`，仅更新 `ashare-strategy-preview`；上传前内容哈希复核及真实凭据扫描通过。
- 已上线版本 `ashare-strategy-preview-003`，任务 `2088497` finished。公网读回内容摘要一致，实际配置为 public，用户名／密码均未配置；未带认证的页面与接口可访问，跨站写403。原有 live/api 和 WorkBuddy 服务不变。
- 全新无认证浏览器真实输入 C、点击“开始回测”，运行 `run:38652b4243eb45f19d2885f7a385513c` succeeded，结果摘要 `sha256:1f202608128b95504c2043b79f7f0f0bc41c62ef17783c15f49c1e28cedc687a`；东方财富243个净值点、18条交易记录。随后 DeepSeek-v4-pro 分析返回，页面显示两句结论和3个优化候选；非进度接口没有HTTP错误。
- 证据 `/private/tmp/ashare-mvp-validation.jMB5kt/public-browser-evidence.json`；已查看截图 `public-report.png` 和 `public-report-analysis.png`。平台默认域名首次可能显示“确定访问”，不需要账号。
- 本次先交付免登录临时 MVP，B29长期存储仍待办；B24保留用户体验验收。后续热修复或回滚也应保持公开免登录，不得直接恢复002的认证要求。下方为已上线前版历史，不能再据此要求用户输入测试账号。

### 已部署前版（历史：002，曾启用受邀访问）

- 独立地址：[策略回测 MVP](https://ashare-strategy-preview-305722-11-1330091763.sh.run.tcloudbase.com/)。服务 `ashare-strategy-preview`，当前版本 `ashare-strategy-preview-002`；只更新新服务，未更改旧 live/api 或 WorkBuddy 页面。
- 创建任务 `2088369`、绑定实际 HTTPS Origin 的任务 `2088407` 均完成。单实例，CPU1／内存2GB，1个ASGI进程。部署内容清单摘要 `7015ab1ab69d4e197525599a0705db87c67b63e832c84b3f8b0308cf3e614db9`，不是Git提交SHA。
- 未认证页面/API均401；认证后可读；跨站写403；结果不缓存。平台测试域名首次在浏览器显示“页面访问提示”，点“确定访问”后进入测试账号登录。
- 云端实际输入 C→ready/201；原策略运行 `run:d23aadcc7ec14967b988a22205c009d6` 成功；买入改为30日新高后 `run:e4d5d52c6c2b4757b41582849dc67b22` 成功；保持新规则换美的集团后 `run:34f6ceefe0504814a903bb99c77282d6` 成功。均为东方财富来源、243个净值点，交易记录分别18／18／36条；三份报告有独立resultHash且原报告仍可读取。
- 云端DeepSeek分析通过后台查询返回200，用时62.3秒；不受单条普通HTTP等待超过60秒的中断影响。没有原始reasoning输出。
- 临时记录可能在容器重建后丢失；长期存储为B29待办。5,567只股票名称／代码随源码包保留。尚未宣称重建保留记录、多租户隔离或所有历史Bug验收通过。
- 验证记录在本地 `/private/tmp/ashare-mvp-validation.jMB5kt/cloud-evidence.json`；旧访问说明 `access.txt` 在取消账号后废弃，本文不写入密码。源包位于 `/private/tmp/ashare-mvp-release.3ey6IK`，上传前针对真实凭据扫描无匹配。
- 新浏览器实际点击“开始回测”后已进入完整报告，运行 `run:f6837135fb024248a3b537b7bc129586`，三个结果接口200；自动脚本此前等“查看报告”按钮属于判据错误（实际已经在报告页）。截图 `/private/tmp/ashare-mvp-validation.jMB5kt/cloud-journey-failed.png` 名称含 failed，但实际内容为成功报告，不能当作产品失败。

### 临时 MVP 接线验证（最新）

- 已接入唯一 `private_skill_ephemeral` 工厂，保留 APP_ENV=production 的密钥／HTTPS检查；整站认证、单实例所需单进程、1个回测线程＋2个等待位，原始推理关闭。独立 SQLite，不复用用户本地库。
- 云托管普通 HTTP 存在 60 秒限制。仅本次预览构建启用 `Prefer: respond-async`，模型请求立即返回 202，前端轮询原结果；普通回测 202 不被误认为分析等待。最多2个在途分析、已完成结果保留10分钟，刷新或查询中断不自动重发收费 POST。服务重建会失去这些临时结果。
- 定向验证：后端访问／工厂／等待桥与发布打包共74项，前端 API 客户端22项通过；Ruff与Pyright通过，非Mock构建成功。
- 本地真实新入口草稿 `f30fc429-9c30-42a9-b717-fc7993ca519f` ready／201；回测 `run:6d93576d46254b99a21c8c2836e377c9` succeeded，东方财富来源，243个净值点、18条交易记录；DeepSeek分析返回200、用时83.45秒。此为本地新入口闭环，不是公网验证。
- 本地测试时内容摘要 `637723981fac427cbeb518961fd97735a2aeea474b728742f135127dd566086e`；最终包另修了并发容量检查、前端预览条件和Docker运行环境接线，摘要为 `7015ab1ab69d4e197525599a0705db87c67b63e832c84b3f8b0308cf3e614db9`。两者不冒充同一SHA；最终包上线还需真实验证。
- 发布包只含白名单源码、目录资源和同源前端，5,567只名称／代码齐全；不含本地密钥、用户数据库、历史缓存或Git目录。临时访问凭据在包外保存，不写入本记录。

以下是本轮准备过程的历史记录；上方“已部署的临时 MVP”为最新状态。用户已明确批准先采用临时存储发布 MVP，长期存储另记 B29；数据库原生连接不再阻挡本次临时版。独立受保护 Skill 入口使用专用临时 SQLite，不复制本地用户会话数据库。

临时版的会话、策略和回测记录在容器重建时可能丢失；代码附带的全 A 股名称／代码名录随版本保留。旧 WorkBuddy 页面、原 live/api 服务均不改变。真实模型与东方财富取数、访问保护及本地关键链路验证仍是发布条件。

这是一轮发布准备，不是“网站已发布”或“所有 Bug 已通过”的声明。B05 维持用户验收关闭。

## 已落地的本地改动

### 对话数据库迁移与权限

- Alembic 新增 `20260906_0006`，将 `dialogue_drafts`、`dialogue_draft_revisions`、`dialogue_idempotency`、`dialogue_backtest_reviews` 纳入升级与漂移检查。
- 空库正常建表；由 create_all 先建的四张对话表，在结构完全兼容时接管并保留行，结构不符则在新增表之前拒绝。未版本化的整库不能因此自动 stamp；既有真实库需另行核对，不能直接套用。
- 三张历史表禁止 UPDATE/DELETE；草稿主表可进行 CAS 版本更新。直接连接和管理 API 两套 ACL 同步此差异，运行角色不获得 DELETE。
- 不允许默认 downgrade 删除对话历史；代码回退不等于数据库删表回退。旧离线 SQL 导出路径不能执行此版本的在线预检，应使用真实单连接迁移。
- 测试只操作临时 SQLite 数据库及离线 PostgreSQL 权限桩，没有修改本机 `var/ashare.db` 或云端数据库。

### 测试站入口保护模块

- `deploy/private_preview/access.py` 提供最终 ASGI 应用的外层保护，覆盖页面、资源、API、文档及进度。
- 使用浏览器原生 Basic Auth；配置缺失、密码过短、非 HTTPS 来源启动拒绝。认证头在进入业务应用前剥离，返回内容不缓存，不输出密码。
- 写请求要求严格匹配配置的 HTTPS Origin；默认同一进程最多两项在途写请求、每 60 秒最多 12 次，超额立即 429。进度读取仍可继续。
- **模块尚未接入实际云服务**。它不是多租户权限，也不是后台回测队列容量或跨实例成本配额。回测 202 返回后的后台执行需另外限制，不能把请求并发上限说成任务并发证明。
- 未删除本机模型进度能力，未放宽 `eastmoney_skill` 的 production 门禁；新测试配置需要单独接线并关闭原始推理输出。

### 当前前端构建

- 使用现有 `build:live`，显式清空 `VITE_DATA_AS_OF_DATE`；当前入口同源 `/api`、非 Mock，不使用旧固定截止日期构建脚本。
- 构建成功：`index-BKKdCcDq.js`、`index-CgjWTU-W.css`。检查未出现 `mock_demo`、旧 CloudBase API 地址或 localhost 8011/5184 固定地址。
- 构建有单个 JS 约 500 kB 的体积提示，未为发布额外重构前端。构建与标记检查不证明云端真实回测成功。

### 登录确认后的任务容量修复

- `ThreadBacktestJobQueue` 和 `SkillBacktestService` 新增可选 `max_pending`；默认 `None` 保持旧行为。新站接入时可显式选择 1 个执行线程、最多 2 个等待任务。
- 队列满时立即拒绝，保存该次 run 为 `FAILED / backtest_queue_full`，API 返回 503 和可重试说明；没有继续取数，也不留下永久 queued。正常完成、执行异常和提交异常均释放容量。
- 主工作区实际定向结果：队列／Skill admission 12 项、既有 backtest-runs API 合同 60 项通过，相关 Ruff、Pyright、diff 检查通过。测试使用离线阻塞桩，不宣称完成新的真实回测。
- 应用前只读确认本机活动回测为 0。文件变动触发已有本地监视器自动重载，随后后端日志确认正常启动。云服务未改变，新站容量配置尚未启用。
- 受保护 Skill factory 已在隔离目录形成候选补丁并通过 24 项合同测试，但未集成；它要求真实 PostgreSQL，不通过切换 SQLite 或降低 APP_ENV 绕过要求。现有 public 权限检查仅适用专用库，不作为共享 schema 隔离证明。候选补丁位于 `/private/tmp/private-preview-factory.1LEjcR/factory.patch`，后续必须复审并完成当前存储选择后再接线。

## 最小验证

- 迁移、存储及权限相关：57 项通过。包含四表接管、缺表、不兼容拒绝、禁止覆盖、跨引擎读取、幂等、CAS、分析保存、损坏 JSON 拒绝及旧 CloudBase 合同。
- 访问保护：42 项通过。覆盖未认证不进入业务、跨站写拒绝、并发与短窗口限制、异常释放、配置检查。
- 相关 Ruff、Pyright 和 diff 检查通过。以上为本地定向证据，不是实际 PostgreSQL 权限、数据库连接或云端发布证明。

## 云端只读事实

已通过官方 CloudBase CLI 3.8.1 的既有登录查询当前环境；未把云端凭证复制到源代码、参数或输出。

| 检查 | 结果与边界 |
| --- | --- |
| 环境列表 | `test-d6gwxiamcd87743af` 显示 NORMAL；CLI 此列表基于计费信息，单独不能证明运行就绪。 |
| 原有服务列表 | `ashare-backtest-live`、`ashare-backtest-api` 均 normal；未改变它们。 |
| 旧 TCBR 环境视图 | 返回 UNAVAILABLE、Databases=[]；不能据此直接认定欠费或没有 PostgreSQL。 |
| 当前单环境详情 | `DescribeEnvInfo` 为 NORMAL，独立 PostgreSQL 数组有一项；旧字段视图不完整。 |
| 数据库管控面 | `ExecutePGSql` 的只读 SELECT 1 返回 1（请求 `f8149dc3-7dde-4bd3-8462-cec47ed23cad`）；不是 psycopg TCP 连接证据。 |
| 项目隔离资源 | 未发现本项目十张应用表、ashare 前缀 schema/role；未读取业务行。不能借用其他服务数据或全局修改共享 public 权限。 |
| 原生连接参数 | CLI 当前 PG 资源元数据不含 host/port/连接串。登录后查看 PostgreSQL 配置页：实例为共享集群，内网地址为 `-`；开启外网、关联安全组、重置密码、SSL 与升级独享入口均不可用。没有获得可供 psycopg 使用的真实端点。不能从实例 ID 或某次 SQL 后端端口猜测连接地址。 |
| 浏览器控制台 | 已登录；套餐页显示有效期至 2026-10-02。没有重置密码、升级套餐、创建新账号或修改网络。共享实例直连不可用的具体原因没有官方结论，不归因为欠费或套餐等级。 |

官方资料：[原生 PostgreSQL 连接](https://docs.cloudbase.net/database/postgresql/connecting-to-postgresql)、[云托管默认公网入口](https://docs.cloudbase.net/run/develop/access/client)。默认云托管域名不自带业务鉴权，因此需整站保护而不是仅保护前端。

### 登录后的发布选择

- 保留测试记录的版本：仍需可实际连接、权限隔离的持久存储；不能将管理 API 的 SELECT 1 当成原生数据库已接通。
- 临时体验版本：可以另做明确的临时存储配置，但服务重建时测试会话和报告可能丢失，不能继承 B26 的持久化通过结论。代码附带的全 A 股名称／代码名录不受此限制；它与用户测试记录不是同一类缓存。
- 用户后续已明确接受临时存储，并要求长期存储加入待办 B29。采用专用临时配置，不擅自购买资源、升级套餐或重写整套存储层，旧服务继续保持不动。

## 后续接线顺序

1. 接入明确的 private_skill_ephemeral 配置，绑定独立临时数据目录；原生数据库、运行角色及重建保留记录延期至 B29。
2. 明确独立测试部署配置，复用当前 Skill 服务与同源前端；绑定整站保护和安全的进度输出。
3. 限制后台任务并定义异常重启行为。服务独占确认前不批量改旧运行状态；不自动重新触发收费模型或取数；已完成结果不修改。
4. 在本地完成新部署入口的真实策略、执行、分析链路，再新建云服务；检查 HTTPS 保护、真实报告及临时存储声明。云端请求有 60 秒时限，同步模型链路需实际验证，不凭前端构建通过认定可上线。
5. 交付新测试地址、进入方式、已知限制及回滚方法。其余体验 Bug 留作后续热修复，不要求用户一次验收全表。
