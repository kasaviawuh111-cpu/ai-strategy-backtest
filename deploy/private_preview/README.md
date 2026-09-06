# 免登录临时 MVP

用户已接受临时存储，优先发布 MVP。本入口只有 `private_skill_ephemeral`，不混用长期存储。
固定启动：`python -m deploy.private_preview.entrypoint`。一个 ASGI 进程、一个回测 worker、
最多两个等待回测；本批公开免登录，同源写请求校验，关闭原始推理流。

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
