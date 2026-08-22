# PRD-101 测试记录

状态：实现、专项与受影响全量回归、静态门禁和真实浏览器验收均已完成。

## 已执行专项

| 测试组 | 当前结果 | 覆盖 |
| --- | --- | --- |
| Migration | `3 passed` | 0092->0093、fresh、pristine down/up、非 pristine 回退阻断、完整性/FK |
| Runtime/config | `6 passed` | 签名、过期、错 tenant、四层 429/503、续租、显式 Cell、生产独立密钥与 masked 摘要 |
| Platform API/delivery | `1 passed` | PlatformSession/权限、CSRF/CAS/幂等、原子审计/outbox、短 TTL 续签、验签同步、跨 Cell 撤销 |
| Gateway hot path | `3 passed` | 既有 fallback 回归、chat/embeddings/stream 启用态、正文解析前 503、流终态释放 |
| Gateway OpenAI-compatible | `30 passed` | 完整集成文件，覆盖非流、流式、embeddings、fallback 与既有治理回归 |
| Streaming usage | `12 passed` | 流式完成、取消、异常与用量最终化，确认 lease 始终释放 |
| Virtual Key auth | `1 passed` | Virtual Key 权威恢复 tenant/workspace，拒绝客户端覆盖 owner |
| Config summary | `3 passed` | 新配置默认值、生产密钥约束和摘要脱敏 |
| Compose/deploy | `9 passed` | Compose 环境透传、部署配置校验和生产失败关闭 |
| Console focused | `3 passed` | 严格 Cell/placement DTO、Cookie+CSRF+CAS+幂等、权限裁剪和敏感字段拒绝 |
| Console full | `1016 passed` | 控制台全量 Node 回归 |
| Console lint | `325 files` | 控制台源码与测试静态检查 |

每次 pytest 调用均使用新的显式 `--basetemp` 并关闭 cacheprovider。专项测试没有记录或输出本地凭据、TOTP、
Cookie、Session、签名密钥、Provider Key、DSN 或客户正文。

## 已验证边界

- 数据面身份只由 Virtual Key 恢复 tenant/workspace；placement 在正文解析前执行，客户端字段不能覆盖 owner。
- 签名制品绑定 tenant、workspace、Cell、revision、generation、key version 与 TTL；续签不回退 generation。
- Control placement、平台审计、Cell event、config outbox 和签名制品同事务提交；幂等重放不产生第二事件。
- 最新签名 generation 指向新 Cell 时，旧 Cell 删除本地快照，新 Cell 才安装；失败制品保留 LKG 直到显式过期。
- chat、embeddings 和 stream 共用容量外壳；流式 lease 在最终化后释放，后续请求可立即获得唯一令牌。
- Console 与平台响应不展示签名、Provider、内部地址或 tenant owner。

## 最终门禁

- PRD-101 新增 Python 模块和测试通过 Ruff；新增源码通过定向中文注释门禁。
- Python `compileall`、`pip check` 与 `git diff --check` 均通过。
- 真实本地 Control API、Gateway 和 Console 分别运行于 `localhost:8000`、`localhost:8001` 和
  `localhost:5173`；签名 placement 成功安装，Gateway 请求通过 placement admission 后到达
  Provider Key 校验，并按预期返回 `503 provider_key.unavailable`。
- 真实浏览器在 390x844、1280x900 和 1440x900 三个视口完成验收：无页面或非容器溢出、无控件碰撞，
  Gateway 导航完整可见，两条 Cell 与 placement 表单正常呈现，控制台无错误或警告，也无敏感字段残留。
- 浏览器验收完成后已恢复默认视口。浏览器技能用于真实页面交互、三视口布局和控制台状态验证，结论不依赖
  静态 DOM 推断。
