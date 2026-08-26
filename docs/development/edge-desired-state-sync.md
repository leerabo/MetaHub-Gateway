# Edge Desired State 同步运行边界

PRD-121 的 Agent 同步器使用 Control API 固定 HTTPS origin、PRD-120 设备 mTLS 证书和最长 45 秒的
`X-Edge-Session`。Workspace Cookie、Bearer、内部配置 Token 和调用方自报证书头都不属于该通道。

## Control 签名根

- `METAHUB_EDGE_SNAPSHOT_SIGNING_VERSION`：当前独立签名根版本，首版为 `1`。
- `METAHUB_EDGE_SNAPSHOT_SIGNING_SECRET`：只注入 Control；生产必须显式提供至少 32 字节的独立值，不能与
  设备 CA、Workspace/Platform Session、tenant data、Provider Key、文件或 Gateway placement 根复用。
- 私钥由独立 secret 派生且不入库；数据库只保存 `kid`、公开 Ed25519 材料和由当前根签名的 trust manifest。
  Compose 安装器从不可用占位符生成独立值，并执行长度与跨域复用检查。

## Agent 文件与 HTTP

`GatewayDesiredStateSynchronizer` 的默认 HTTP client 必须同时获得设备证书文件、对应私钥文件和 Control CA
证书文件；缺少任一文件、非 HTTPS Control URL 或 TLS 装载失败都会在出网前失败关闭。EdgeSession 由调用方
通过 callback 提供，以便在 45 秒窗口内主动续期；同步器不把 Session 写入磁盘、日志或异常。

拉取响应限制为 RFC 8785 canonical JSON 和 768 KiB。只有连接/超时类网络错误及明确的
`502/503/504` 可进入有界 LKG；`401/403/404`、其他 HTTP 状态、压缩/超限正文、duplicate key、非 canonical
JSON、ETag/hash、签名、owner、schema、TTL、generation 或 trust 冲突都会写入持久隔离标记。有效新快照
完成 slot、trust、高水位和 active pointer 的持久化后才发送确定性 ACK；ACK 网络失败可使用相同 payload
重试，不回滚本地已应用状态。

请求热路径只调用 `content()` / `decision()` 读取进程内原子引用，不执行 HTTP 或磁盘 I/O。安全 TTL 到期后
授权相关决策失败关闭；普通配置仅在签名 TTL 内继续服务并明确标记 `lkg/stale`。
