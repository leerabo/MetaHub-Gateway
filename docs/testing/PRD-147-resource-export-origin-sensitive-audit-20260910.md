# PRD-147 GET 导出来源敏感审计

## 范围与边界

本独立增量仅修改共享资源来源校验和客户端 origin-only referrer，不修改组织角色、员工身份、平台开关、CSRF Secret 或导出所有者合同。

- 非空/重复/错误 Origin 不可通过 Referer 回退。回退只允许 GET、唯一 same-origin Fetch Metadata、唯一结构化且精确匹配允许列表的 Referer。
- 仍要求 Workspace Cookie、header/cookie CSRF 一致及会话内 CSRF 绑定。来源证据不授权匿名、机器 KEY、跨组织或 Platform/Support 混合凭据。
- 控制台 no-referrer meta 保持。只有需要 CSRF 的 GET 发送来源域名，不发送页面路径、查询、片段或原始业务内容。
- 测试来源域名使用 example.test；本地测试 fixture 仅写新建的 `.tmp/s1-browser-*` 数据库。无付费模型调用，不把合成用量说成真实推理。
- 测试诊断只记录固定错误码、SQLite 错误分类及代码函数名，不记录 SQL、Cookie、来源原文或导出正文。

独立暂存审计见 `.artifacts/s2-origin-index-audit-20260910.json`，逐文件核验精确 9 文件允许列表、UTF-8、SHA-256 及 Provider KEY、私钥、AWS 凭据、JWT 模式，0 项命中。文档补齐后重新运行相同审计，文件摘要以该 JSON 为准；本扫描不代表整个仓库无敏感信息。

实际暂存代码导出后通过后端 33 项和前端 95 项，生产资产摘要为 `612407cddff53af76d08eeb3e866d7333c1e86ea09af11a249981181b75e0d13`。工作区源码浏览器报告 `.artifacts/s1-browser-zvjj5lze/acceptance.json` 通过三视口 CSV 下载及其归属断言，但不是独立暂存生产包浏览器验收；其中两次页面访问统计 503 仍保留调查。本次没有改动试用服务、账号、数据库或原始密钥。
