# PRD-037 测试记录

PRD：`PRD-037 - SSE流式代理与超时看门狗`

## 覆盖范围

- 迁移：`0027_stream_sessions.sql` 新增 StreamSession、request_logs 流摘要、租户归属/状态迁移/终态一致性触发器，并验证 down 回滚和再次上行。
- 数据面：正常 SSE 增量透传，首块和相邻块超时，ASGI `http.disconnect` 并发通知，响应生成器取消，上游中途异常和所有路径的幂等资源关闭。
- 审计：上游网络调用前强制创建 starting 会话，2xx 握手后推进 active，终态保存分片数、已交付字节数、首末块时间和统一错误码。
- 协议：握手前返回统一 JSON；HTTP 200 后 timeout/unavailable 使用标准 `event:error`，不发送 `[DONE]`，不暴露上游异常、URL、Header 或正文。
- 用量与成本：正常、timeout、断开和读异常都写 request_logs；已有分片按安全字符计数形成部分估算 Usage，Provider usage 存在时继续使用上游口径。
- 事件：`stream.timeout` 仅在 timed_out 终态提交后进入有界非阻塞队列，使用稳定 event_id 和固定安全 payload。
- 单路由边界：流式握手失败、首块超时和中途超时均不执行 PRD-036 Fallback，不拼接第二模型输出。
- 控制面：admin/developer 可读取本租户列表摘要与 StreamSession 详情；viewer 后端拒绝；跨租户 request_id 返回 404。
- 前端：ReadableStream 增量读取、跨块 UTF-8、标准 SSE error、AbortSignal、连续提交取消旧流、主动停止、实时分片/字节数和最新 256 KiB 安全预览。
- 浏览器跨域：共享 FastAPI 运行时仅暴露 `X-Request-ID`/`X-Trace-ID` 两个安全响应头，使调试台可读取链路标识且不暴露 Authorization 等敏感头。
- 中文注释：新增 Python、JavaScript、SQL、测试定义及并发、租户、审计、错误和回滚关键分支通过强制门禁。

## 执行命令

```powershell
$env:PYTHONPATH='shared-sdk/python;control-plane-api/src;gateway-data-plane/src'
python -m pytest control-plane-api/tests -q
python -m pytest gateway-data-plane/tests -q
node --test console-web/tests/unit/*.test.mjs
node console-web/scripts/lint.mjs
python -m py_compile shared-sdk/python/metahub_shared/runtime/config.py gateway-data-plane/src/metahub_gateway/api/openai_compatible.py gateway-data-plane/src/metahub_gateway/streaming/sessions.py
python scripts/ci/check-comments.py
```

控制面完整套件在 Windows 本机包含大量重复 SQLite 迁移，单进程总耗时超过命令执行窗口；本次按不重叠文件批次完成全部 88 个已收集用例，所有批次均为 0 退出。

## 当前结果

- 数据库迁移与调用日志定向测试：10 passed；空库 0001-0027、重复迁移、0027 回滚、重新上行、跨租户/状态触发器和详情查询均通过。
- 控制面完整覆盖：88 passed；按 10、42、17、19 四个不重叠批次执行，覆盖 `control-plane-api/tests` 全部已收集用例。
- 数据面最终全量测试：81 passed；包含 5 个流状态机并发测试、3 个真实 StreamSession 数据库测试、真实 HTTP SSE 集成测试及 CORS 链路响应头白名单测试。
- 前端全量测试：125 passed；其中新增增量读取、SSE error、Abort、256 KiB 截断和调试台生命周期测试。
- 前端 lint：`console lint ok (85 files)`；Python 语法编译通过。
- 中文注释门禁：`comment coverage ok`，并排除 `.tmp` 生成缓存，避免目录联接重复扫描外部依赖。
- 运行态：开发库应用 0027 后，Gateway、Control API、Console 分别监听 8000、8001、5173，本地 SSE 模拟上游监听 11434。真实浏览器请求完成 4 个分片、226 字节，Usage 为 4/3/7，页面显示非空 request_id/trace_id，停止按钮与等待占位在完成态均不占布局。
- 响应式浏览器验收：1440x900 与 390x844 均无页面、表单或结果区横向溢出；移动端表单、状态、链路字段和预览区无重叠，预览区仅自身允许横向滚动。

## 关键验收用例

| 用例 | 输入/场景 | 预期 |
| --- | --- | --- |
| 主流程 | 多个 SSE 块间隔均低于阈值 | 原样增量输出，会话 completed，日志 success，Usage 可追踪，资源释放 |
| 首块超时 | 2xx 握手后上游不产生任何非空块 | 取消读取、关闭上游、会话 timed_out、标准 SSE error、发布一次 timeout 事件 |
| 中途超时 | 已输出若干块后超过块间阈值 | 保留已输出内容并追加 SSE error，部分 Usage 入账，不发送 `[DONE]` |
| 客户端断开 | 上游空闲时收到 ASGI `http.disconnect` | 立即取消上游读取，会话 client_disconnected，无 timeout 事件，无任务/连接泄漏 |
| 上游读异常 | HTTP 200 后网络读取抛出异常 | 不穿透异常原文，返回 unavailable SSE error，会话和失败日志可追踪 |
| 审计闸门 | StreamSession 创建或 active 推进失败 | Provider 不调用或已建连接立即关闭，统一 503，治理租约释放 |
| 流中路由 | 主候选握手失败、首块/中途 timeout | 不调用备用模型，不产生第二段模型内容或 Fallback 事件 |
| 权限与租户 | viewer 或其他租户读取会话详情 | 后端 403 或 404，不依赖前端隐藏，不暴露会话存在性 |
| 脱敏 | Key、Prompt、响应和原始异常包含敏感文本 | API、SSE error、日志、会话、Usage、Webhook 和控制台均不出现原文 |

## 运行态校正记录

1. 首次浏览器验收发现 Gateway 虽返回 `X-Request-ID`/`X-Trace-ID`，但 CORS 未声明 `Access-Control-Expose-Headers`，浏览器 JavaScript 只能得到 `-`。共享运行时已统一暴露这两个链路头，并新增精确白名单测试；Authorization、Provider Key 等敏感头不在暴露列表。
2. 完成态的停止按钮和空状态骨架使用原生 `hidden` 属性，但组件 `display` 样式会覆盖浏览器默认规则。全局增加 `[hidden] { display: none !important; }`，确保隐藏元素不占布局、不可见且不可聚焦。
3. 最终真实请求 `console_gateway_1784076957397` 完成，StreamSession/调用日志记录 4 个分片和 226 字节；浏览器可见 `trace_id=trc_755538cc598b4a7592cb9f7ba6f0cf53`，原始 SSE 预览包含两个内容块、Usage 块和 `[DONE]`。
