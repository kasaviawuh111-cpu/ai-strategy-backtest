# A 股单股策略回测 H5

手机端优先的 A 股单股策略回测界面。用户输入一句买卖规则，页面将它还原为可编辑条件，再展示信号、决策、委托、成交或未成交直到账户净值变化的因果轨迹。

当前正式入口是 `src/main.tsx → src/App.tsx`。界面保留东方财富橙色语义、规整对话与渐进卡片；Mock 与 Live 共用同一套页面，Mock 以轻量“界面预览”标识、`data-api-mode`、请求隔离与自动化校验保持边界，不得作为真实回测证据。

会话采用只追加结构：完成一次回测后，“换个条件”会把旧策略收为不可编辑记录，并保留旧结果供对比。首页策略卡只显示买入、卖出和区间；默认 100 万元初始资金只在二级参数页编辑，首页与回测报告均不默认展示。

## 本地 Mock

要求 Node.js 20.19+ 或 22.12+，推荐 pnpm 11。

```bash
cp .env.example .env.local
pnpm install
pnpm dev
```

打开 `http://127.0.0.1:5173` 后：

1. 输入或保留“东方财富 MACD 金叉买入，死叉卖出，回测近 5 年”；
2. 点击发送按钮“识别交易规则”；
3. 检查买入、卖出和区间；如需调整，打开“成交与费用”；
4. 点击“开始回测”；
5. 点击“查看完整报告”，先看一句结论和关键指标，再向下检查净值与回撤、每笔买卖及因果轨迹；成交规则、数据来源与运行证据通过唯一入口进入三级详情页。

页面另提供“年度报告发布后买入，MACD 死叉卖出”和“同花顺发年报提到 AI 次数超过 5 次就买入，3 个交易日后卖出”的 Mock 事件预览。Mock 能力表仅用于离线开发；自动化必须验证它不发起任何 `/api/*` 请求。老板验收与任何公网交付入口必须使用 Live 构建，不能显示 Mock 标识、返回预览结果，或在 API 失败时静默回退。

Live 编译规则不受当前回测数据可用性阻断：页面会先展示后端返回的 StrategySpec，再通过 `/api/v1/capabilities` 说明“能理解、能准备、当前快照可回测”三层状态；只有保存修订和开始回测时才 fail closed。能力接口返回的名称、trigger、参数 schema、范围、单位和默认值优先用于渲染；后端暂未下发 metadata 时只使用通用中文化，不在前端复制 35 个指标或 69 个事件目录。当前 strict `1f26…/4bd9…` 只对五类定期报告提供 pinned 数据能力，不把其他 Catalog 事件显示成已有真实数据。固定快照事件须为 `backtest_available + pinned_snapshot`；on-demand 事件须为 `preparation_available + request_preparation`，表示随后按本次股票与区间生成并 pin，不表示当前快照已有数据。无法识别的原话不会兜底成 MACD；只说“MACD”“RSI”等裸指标会集中澄清一次买入和卖出方向，不会静默套用默认策略。

## 股票页上下文

产品不固定东方财富。宿主股票页通过 URL 注入当前 A 股，Mock 与 Live 都把同一身份传给编译接口：

```text
/?symbol=600519.SH&market=CN_A
/?instrument_context=000001.SZ
```

支持沪、深、北交所规范代码；非 A 股、交易所后缀错配和无效代码 fail closed，不会静默切回东方财富。URL 中的 `name` / `instrument_name` 只是不可信展示参数，当前不会被用来确认证券身份；没有证券主数据校验时，页面只展示规范代码。宿主没有传入股票且页面独立打开时，东方财富 `300059.SZ` 只是演示默认值。某个标的能进入页面不代表它已有真实行情、session、公司行动或事件覆盖；Live API 缺数据时必须返回明确的“当前数据未覆盖”。

## 当前结构

```text
src/
├── main.tsx                    # 唯一正式入口
├── App.tsx                     # 对话、编译、运行与页面编排
├── app/
│   └── AppProviders.tsx        # QueryClient 与错误边界
├── components/                 # 策略/运行/结果卡、图表与基础组件
├── screens/                    # 参数、连续报告、成交规则详情与因果轨迹页面
├── shared/
│   ├── api/                    # Live 契约、HTTP client、Mock 与测试
│   ├── config/                 # 前后端共享约束的前端映射
│   └── instrument-context.ts   # 股票页 A 股身份解析与 fail-closed
├── styles/                     # token 与正式 UI 样式
├── types.ts                    # 页面模型
└── view-model.ts               # API 结果到页面模型的纯转换
```

业务组件不直接调用 `fetch`。`src/shared/api/client.ts` 在 Mock 与 Live 之间切换；Live DTO、Strategy DSL 和页面模型的转换集中在 `src/shared/api/contract.ts`。

## Live API

```bash
VITE_USE_MOCK=false
VITE_API_BASE_URL=
VITE_DATA_AS_OF_DATE=2026-08-06
```

普通本地开发仍可运行 `pnpm dev`，让空的 `VITE_API_BASE_URL` 通过原有 5173 同源代理访问 8000；
这条链路不计入最终隔离验收。最终 Live 必须另起 strict API 8011，并使用：

```bash
VITE_API_PROXY_TARGET=http://127.0.0.1:8011 \
VITE_API_BASE_URL= \
VITE_DATA_AS_OF_DATE=2026-08-06 \
pnpm dev:live
```

`dev:live` 固定 `VITE_USE_MOCK=false`、`127.0.0.1:5184` 和 `strictPort=true`，只代理 `/api`、
`/health` 到精确的 `http://127.0.0.1:8011`。代理变量缺失、Mock 模式、远端/其他端口、路径别名或
非空 `VITE_API_BASE_URL` 都会 fail closed。`VITE_DATA_AS_OF_DATE` 是可回放数据的业务截止日，
不使用浏览器当天日期。

公网静态发布使用另一条明确门禁，不复用本机代理配置：

```bash
VITE_API_BASE_URL=https://api.example.cn \
VITE_DATA_AS_OF_DATE=2026-08-06 \
pnpm build:public-live
```

`build:public-live` 固定 `VITE_USE_MOCK=false`，并拒绝空地址、HTTP、localhost、私网 IP、带凭据或
路径的地址。发布前必须先让该 HTTPS API 正确开放 CORS（只允许实际 H5 域名和所需方法/请求头）。
Choice、数据库等凭据只能保留在服务端，不能写入任何 `VITE_*` 变量。真实接口断网、超时或返回
非 JSON 时，页面会明确失败且不会静默切换成 Mock。生成 `dist` 只证明构建配置正确；在公网浏览器
完成技术与事件旅程前，仍属于 Live-unverified。

Live HTTP client 只对传输层断连、超时和可重试 5xx 做最多 3 次有限重试；认证、Schema、能力不足和业务错误不重试。创建、修订与提交类 POST 在同一次用户动作的重试中复用稳定幂等键，避免网络抖动生成多个 draft 或 run。

拿到真实前后端 HTTPS 地址后，发布前后的可重复门禁是：

```bash
# 1. 构建并自动检查 dist：必须含真实 API 地址，不得含 Mock 执行标记
VITE_API_BASE_URL=https://真实-api-域名 \
VITE_DATA_AS_OF_DATE=2026-08-06 \
pnpm build:public-live

# 2. 发布后验证技术、事件和不支持策略；默认覆盖 320/390/768/1280
python scripts/smoke_public.py \
  --frontend-url https://真实-h5-域名 \
  --api-url https://真实-api-域名 \
  --expected-producer-snapshot-id composite:<64位小写十六进制>
```

若能导出本次后端日志，可追加 `--backend-log-file /绝对路径/backend.log`，把浏览器响应中的
`X-Request-ID` 与服务端日志逐条核对。公网 smoke 会拒绝 CORS 不匹配、API 返回 `200 text/html`
的静态 SPA fallback、无效 JSON、Mock 身份、错误快照身份和缺少真实结果接口等情况。

| 页面动作 | API |
|---|---|
| 识别规则 | `POST /api/v1/strategy-drafts` |
| 保存编辑 | `POST /api/v1/strategy-drafts/{draft_id}/revisions` |
| 创建回测 | `POST /api/v1/backtest-runs` |
| 轮询/取消 | `GET /api/v1/backtest-runs/{run_id}` / `POST .../cancel` |
| 读取结果 | `GET .../summary`、`.../series`、`.../trades` |

正式结果只有同时满足以下条件时，页面才显示“身份已记录”：

- `dataSchemaVersion` 精确为 loader slice 的 `local-parquet.market-data.v3`；
- `producerSnapshotSchemaVersion` 精确为上游快照的 `ashare-lab.composite-research-snapshot.v2`，两层身份不得混用；
- Git revision 是 40 位小写十六进制 SHA；
- Snapshot checksum、Catalog hash、Strategy hash 是完整 `sha256:...` 身份；
- Snapshot ID 存在。

事件 Live smoke 还要求 provider、严格秒级首次可得时间、`exact` 或 `vendor_observed` 时间质量、已校验状态、来源 URL 和原始响应 SHA-256。这里验证的是当前 pinned 年度报告策略的秒级真实数据，不是否定后端已实现的 `date_only_conservative` 降级；当前没有 date-only Live 工件，因此本轮 smoke 不放宽。最终 Live 验收必须连接基于 clean Git SHA 启动的 strict API；dirty 或 Mock 环境不得声称通过。

## 质量门禁

```bash
pnpm typecheck
pnpm lint
pnpm test
pnpm build
```

当前 Vitest 基线为 11 个测试文件、149 个测试；覆盖正式 `main.tsx → src/App.tsx` 入口、动态能力合同、Live 编译与回测能力解耦、Mock、公网构建门禁、截图原句的年报正文词频策略、一次澄清信息保留、策略确认、连续历史回合、首页本金隐藏、净值/回撤点位和完整因果轨迹。

Mock 浏览器验收让技术策略和截图原句的年报正文词频策略覆盖 320、390、768、1280px；390px 还完整走到结果、交易筛选与因果链。脚本同时覆盖年度报告事件、无法识别和一次澄清，并检查“界面预览”标识、Mock 零后端请求、横向溢出和浏览器错误：

```bash
../.venv/bin/python scripts/smoke_mock.py
```

脚本默认每次创建新的空目录 `artifacts/mock-runs/<run-id>/`，不会复用旧截图，也不会对 `artifacts/` 做通配删除。需要指定目录时可设置 `SMOKE_ARTIFACTS_DIR`；若目录非空，脚本会拒绝运行。

`scripts/smoke_live.py` 默认只连接独立 Live 地址 `http://127.0.0.1:5184`，同时验证技术策略和年度报告事件，并覆盖 320、390、768、1280px。它不是后端 fixture；只有真实 API 返回完整 v2 身份、与预期完全一致的 Composite producer 和事件证据时才能通过。可按需缩小范围：

```bash
../.venv/bin/python scripts/smoke_live.py \
  --strategy technical \
  --width 390 \
  --base-url http://127.0.0.1:5184 \
  --expected-producer-snapshot-id "$EXPECTED_COMPOSITE_ID"
../.venv/bin/python scripts/smoke_live.py \
  --strategy event \
  --width 390 \
  --base-url http://127.0.0.1:5184 \
  --expected-producer-snapshot-id "$EXPECTED_COMPOSITE_ID"
```

`EXPECTED_COMPOSITE_ID` 必须替换成实际 `composite:<64 位小写十六进制>`。脚本拒绝旧 5173、
其他主机/协议以及非空的复用截图目录；未显式提供 expected producer ID 也会在启动浏览器前失败。

不要在 strict API 和最终 clean integration commit 尚未就绪时，把上述 Live 命令写成已通过的证据。

当前日线执行只能证明“使用已发布的当日开盘价作为成交代理”，不能证明 09:25 集合竞价或 09:30 的精确逐笔成交。前端把 09:30 仅展示为代理记录边界，并同时显示 `daily_bar_open_proxy` 时间质量；更精确的时点必须等待分钟、逐笔或集合竞价数据闭环。

## WebView 约束

- 支持 320、390、768、1280px，无横向滚动；
- 使用 `env(safe-area-inset-*)` 适配 iOS 安全区；
- 关键控件有可见键盘焦点，不依赖 hover；
- 动画遵循 `prefers-reduced-motion`；
- 手机端主操作贴近底部，但不会遮挡内容；
- 文案只解释历史规则与执行结果，不提供收益承诺或投资建议。
