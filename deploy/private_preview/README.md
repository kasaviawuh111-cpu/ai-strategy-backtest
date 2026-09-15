# 免登录临时 MVP

## 本地一致的发布入口（2026-09-14）

使用 `scripts/prepare_private_preview_bundle.py` 生成上下文，并使用包根 Dockerfile；
不能改用旧 `scripts/prepare_cloudbase_bundle.py` 或 `deploy/cloudbase/deployment_entrypoint.py`。
旧入口服务于 on_demand_snapshot 合同，不是当前已验收 Skill 产品，不通过改它的环境变量冒充同一入口。
包内附带 `full-data.env.example`，密钥只从平台注入，不复制本机 dotenv 或钥匙串。

- 回测运行时：`eastmoney_skill`，调用与本地相同的 `_compose_skill_app`。
- 联网搜索：腾讯 WSA 优先，失败后复用本地 DuckDuckGo → Bing RSS 顺序；腾讯专用 key 未注入或配置成旧搜索模式，启动拒绝，不静默降级。
- 模型分工：Flash 快速解析；V4 Pro、thinking enabled、reasoning high 负责规划/复盘。漏配后继承 Flash 的旧默认值不能通过发布校验。
- 分钟能力保持开启，数据未就绪仍阻止完整版本发布。用户暂缓上传不等于授权静默关闭分钟功能。

## 超时与网关

`deploy/docker/nginx.conf` 的 `proxy_read_timeout 65s` 是上游相邻读取间的空闲超时，不是整项回测的时长。
该 Nginx 文件不在此包中，当前 Skill 镜像直接启动 ASGI；不能把改这个文件当作已修改 CloudBase 网关。
保留 65 秒，不统一延长。模型生成、澄清、修改、数据准备、提交回测及报告解读请求均通过
`Prefer: respond-async` 立即返回 202，再以短 GET 轮询；后台任务另有 600 秒执行边界，排队容量也有限制。
重复提交用相同幂等键复用任务，不因网络抖动重复调用模型。构建检查必须保留 async 客户端标记。

腾讯文档列出云托管请求超时限制，但具体调用通道及当前服务配置还需上线前实查；本地配置不能覆盖外层网关。
上线验收须核对长任务首次请求快速返回 202、轮询正常、终态结果可读，而不只是 /health 返回成功。
参考：[Nginx读取超时定义](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_read_timeout)、
[CloudBase使用限制](https://docs.cloudbase.net/run/limitation)。

## 运行边界

用户已接受临时存储，优先发布 MVP。本入口只有 `private_skill_ephemeral`，不混用长期存储。
固定启动：`python -m deploy.private_preview.entrypoint`。一个 ASGI 进程、一个回测 worker、
最多两个等待回测；本批公开免登录，同源写请求校验。与本地一致，展示真实处理状态，不向用户展示内部思考文本。

本次完整数据发布必须启用分钟能力。默认挂载 `/data/stock_1min` 和
`/data/market-calendar.json`，分别由 `EXTERNAL_MINUTE_ROOT`、`MINUTE_MARKET_CALENDAR_PATH`
配置。外接盘分钟目录须完整上传到私有存储后再挂载；挂载数据可只读，但运行用户
UID 10001 必须能遍历目录和读取文件。缓存目录位于 `/app/var/cache`，须可写，已有市场数据缓存需随发布迁移。
启动时拦截分钟开关关闭、目录缺失/空目录、样本不可读或日历无效，不能关闭能力来绕过权限故障。
此启动检查不代替全量文件 SHA-256 对比和线上回测验收。

必需声明：`DEPLOYMENT_PROFILE=private_skill_ephemeral`、`APP_ENV=production`、
`PERSISTENCE_MODE=ephemeral`、`RESTART_RECOVERY_VERIFIED=false`、`INITIALIZE_SCHEMA=true`。
普通 `create_skill_app` 的 production 禁令不变，不读本地 dotenv，也不回退到本地密钥。

容器使用 `PREVIEW_STATE_ROOT=/app/var/ephemeral` 和
`DATABASE_URL=sqlite+pysqlite:////app/var/ephemeral/ashare.db`。本地验收可用专用、无符号链接
的临时绝对目录，但数据库必须固定为其下的 `ashare.db`，禁止已有项目数据库和内存库。
不会删除已有文件。不调用 PG helper；长期存储单独待办。

重启、更新或容器替换后，对话和回测记录可能丢失，不声称重启任务恢复。
`GET /api/v1/preview-meta` 返回 persistence=ephemeral 和 revision，供验收使用。

本批访问配置：`PREVIEW_ACCESS_MODE=public` 和完整 HTTPS `PREVIEW_ORIGIN`。
不配置 `PREVIEW_USERNAME`／`PREVIEW_PASSWORD`，无需注册或测试账号，所有访问者使用同一功能。
后台密钥、写请求来源校验及容量限制仍保留；后端模型端点必须 HTTPS，密钥仅在运行环境注入。
为兼容旧配置，未显式选择 public 时保留 private 模式及其 Basic Auth 配置检查；公开部署必须明确选择 public。

`CODE_REVISION` 接受经发布流程证明的干净 40 位 Git SHA，或 `sha256:<64位内容清单摘要>`。
后者明确是 workspace 内容快照，不能冒充旧提交。打包流程负责生成和核对实际内容清单；
factory 格式检查不是完整发布证明。该文件与合同测试都不代表已经部署。
# COS 分钟数据导入索引

生产启动不会遍历 COS 分钟文件。每次更新分钟数据后，使用
`scripts/build_minute_availability_index.py` 对完整导入清单生成
`minute-availability.json`，与该批文件一起放在分钟目录中。
索引保存数据版本、文件数及真实数据截止日；缺失时发布失败，不回退到扫描。
单只股票的可用性及回测区间仍由实际文件检查，不由索引替代。
