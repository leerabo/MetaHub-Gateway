# PRD-147 路由启动准备敏感审计

日期：2026-09-09。范围：Control supervisor、专项测试、设计/测试及门禁状态记录。

- 启动只消费框架路由上下文，不发 HTTP，不执行 endpoint/dependency，不读取账户或密钥。
- 能力检测保留旧 FastAPI 路径；真实 0.115.0 兼容测试使用隔离依赖目录，不改变运行环境。
- 原有 lifespan 和队列顺序、关闭与提交 fence 不变；启动准备失败不进入这些生命周期。
- 真实运行只重放既有反馈，没有付费模型请求。测试 Cookie 留在内存，工具只注销自身会话。
- 账务发布前后 13 组事实行数和哈希相同；failed outbox 和 checkpoint 原样保留。
- 旧运行 dropped=129/backend_failure=17、PRD-049 的 503 和反例测试原件均未删除。
- 全部 540 个 Control 路由上下文有元数据测试，不能冒充这些业务接口均已完成功能验收。

机器报告：`.artifacts/g6-security-20260907/prd-147-route-preparation-sensitive-audit-r1.json`。
同目录 `prd147-route-preparation-key-audit-r1.json` 检查现有 Kimi Key 和加密主密钥的精确
字节，不打印秘密。源码/证据采用严格 UTF-8、凭据模式、暂存字节和固定 baseline 校验。

这是明确范围的代码与证据自审，不代替独立安全会签、SLO、全产品 QA 或 72 小时验收。
