# PRD-147 资料与 SDK 敏感审计

SDK 只迁接四个固定路径；旧 quickstart 仍拒绝仅 Workspace Cookie，不扩大旧通用守卫。Workspace 请求不携带 Bearer，混合凭据失败关闭。前端下载不把凭据写入 URL、Storage 或文件名，离页与切组织丢弃旧 Blob 结果。

元数据连接由现有事务工具拥有，重新开启快照复核权限；下载生成后强制验证成员会话，此步骤不受分析事件 fail-open 控制。API 用例证明生成期间撤权不返回 ZIP，也不记录成功下载事件。

两种 ZIP 验证精确允许文件集合和 CRC，`.env.example` 仅含公开占位符，不包含真实 `.env`、数据库、缓存或密钥。Workspace 分析事件保留 tenant/user/request，legacy session_id 置空而非伪造旧会话，事件连接成功或失败均关闭。

秘密扫描报告只记录文件、哈希与规则，不输出凭据正文。所有 API/浏览器测试使用隔离身份及数据库，不读取用户浏览器会话、不使用真实模型 KEY。本轮完整 SaaS 安全与性能验收仍未结束。

最终补充：PostgreSQL 业务断言复用 SQLite 场景，实际通过两项原生数据库用例。浏览器完成两个组织、四个使用者和四个关联 KEY，新增全局账号/legacy User/Membership 各仅两条，证明使用者创建没有暗中生成登录身份；平台操作员新增数为 0。隔离数据库外键检查通过，所有模型请求为 0。

`.artifacts/s1-profile-sdk-batch-20260910/index-audit.json` 对最终暂存文件逐个记录摘要，秘密扫描和 JavaScript 语法检查无发现。改动 Python 行的 AST/Ruff 检查无发现；未将真实配置、数据库、下载 ZIP 或运行日志纳入提交。
