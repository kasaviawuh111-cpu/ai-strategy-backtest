# 公网007：保留异步接线的多轮承接修正版

2026-09-06。承接006同一批用户授权修改；**当前仍在精确发布包的本地浏览器验证，尚未声明007上线**。

## 与006的差异

业务代码、对话承接、策略参数和界面不再改动。前端重新使用原有`VITE_PRIVATE_PREVIEW=true`构建，保留异步结果查询与匿名队列标识。打包器强制检查这两个关键标记，缺任一个即拒绝出包；8项定向测试通过。

本次构建命令（web目录）：

```sh
VITE_PRIVATE_PREVIEW=true VITE_USE_MOCK=false VITE_API_BASE_URL= VITE_DATA_AS_OF_DATE=2026-09-06 ./node_modules/.bin/vite build --config vite.same-origin.config.ts
```

源包`sha256:ab9ac90f989e8cfe89051d45b2dc8dfc68d3e29b7ffc60a7c7bc57437795208f`。本地专用HTTPS服务加载同一源包及生产工厂，以真实移动尺寸浏览器确认首次策略POST为202且带异步Location，再完成回测、报告与思考可见性；自签测试证书仅供本机验收，不修改系统信任或公网证书。

本批多轮行为、首字母换股、安全支持、数据错误分层的实现证据见[修复记录](bugfix-conversation-recovery-20260906.md)。006中间发布及验收失败单独留存，见[006记录](release-public-006-20260906.md)。

最终云版本、匿名入口、资产哈希和真实公网关键路径在验证完成后补入。
