# PRD-147 企业控制台同源入口验证

## 范围与结果

本增量修复从账号页创建组织后进入企业控制台时，Control API 错误切换到 localhost:8001 的问题。设计与两轮自审：`docs/prd/PRD-147-console-control-origin.md`。

- 原实现下，三个 hosted origin 单元用例失败；浏览器第一次真实开通也在组织跳转阶段失败，产生 4 次被阻止的外部地址请求。证据：`.artifacts/s1-browser-1glmoos6/browser.json`。
- 修复后 Console 入口单元文件 69 项通过。证据：`.artifacts/g6-security-20260907/console-origin-unit-20260909.xml`。新增断言覆盖 HTTPS 域名、自定义端口、同源本地测试域、IPv6 与 HTTPS loopback，并校验两个 Control 变量一致。
- 最终相关前端测试合并运行 identity、workspaceConsumers、virtualKeys、prd146ConsoleEntry，结果见 `.artifacts/g6-security-20260907/s1-browser-final-frontend-20260909.xml`。
- 真实 Chromium 在 1280x900 和 390x844 下完成注册、创建组织、使用者、KEY、轮换与停用。证据：`.artifacts/s1-browser-6y8nim3f/acceptance.json`；10 个阶段通过，0 个页面异常、0 个越界 Origin 请求。

## 证据边界

浏览器使用 `s1.localhost` 隔离同源服务，加载当前源码字节而非生产 dist；真实 Cookie、CSRF、页面跳转和 API 均未被替换。该 HTTP 证据不替代真实 HTTPS 反向代理部署验证。只证明 Control 入口，不证明 Gateway 公网地址正确或成功模型推理。

每次测试使用独立基线副本，不读用户 .env 或复用用户浏览器会话；成功后的隔离数据库已删除，浏览器与服务退出。后台 worker 生命周期关闭，不能由此推断调度或 72 小时稳定性。
