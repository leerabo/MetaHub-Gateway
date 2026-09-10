# PRD-147 浏览器 GET 导出来源验证

## 问题与范围

真实财务浏览器验收 `.artifacts/s1-browser-580gfa72/acceptance.json` 在同源 GET `/usage/export.csv` 返回 `403 onboarding.origin_invalid`。请求有 Workspace Cookie 和 CSRF Header，但浏览器不为同源 GET 添加 Origin。固定 GET 导出合同还被审计和解释导出复用，不能在前端伪造浏览器禁止设置的 Origin，也不能删除 CSRF 验证。

## 第一轮自审

- 保留已有 Origin 优先逻辑；只要携带 Origin，就必须唯一且精确匹配服务器配置的允许来源。非法、空、null、重复或逗号拼接的 Origin 不进入回退。
- 仅 GET 且完全没有 Origin 时启用回退。要求唯一的 `Sec-Fetch-Site: same-origin` 和唯一的 Referer，以标准 URL 解析器提取来源并匹配同一允许列表。
- 拒绝跨站、same-site、缺少来源、相对 URL、非 HTTP(S)、用户信息、片段、控制字符和重复 Referer。不能用字符串前缀判断域名。

## 第二轮自审

- 来源验证仍先于 `_workspace_csrf` 和数据库会话认证；正确来源不能代替 CSRF，KEY、Platform/Support 和混合凭据规则不变。
- POST/PUT/PATCH/DELETE 不放宽，不为 HEAD/OPTIONS 开放导出回退。非浏览器 API 测试可以沿用合法 Origin。
- 不记录 Referer 原文、Cookie、CSRF 或导出内容。测试只记录固定错误码、请求状态和合成数据结果。
- 控制台全局 `no-referrer` 保持。只在 Workspace、GET、csrfRequired 三者同时成立时由共享资源客户端设置 `referrerPolicy: origin`，浏览器只发送来源域名，不能包含页面路径、查询或片段。其它请求不改变已有来源策略；不能靠调整全站 meta 标签放开 Referer。
- 共享守卫变更需要来源负矩阵、真实 Cookie API 及浏览器 CSV 下载验证，不以单元通过代表整批验收。导航偶发 503 独立保留，不作为本修复已解决的事项。
