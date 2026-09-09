# 企业控制台同源控制面入口

## 问题与合同

账号/平台页面在非本地部署时使用页面同源 Control API，但企业控制台 main.js 固定初始化为 localhost:8001。组织创建后跳转到 console.html 会切换到另一组 Host-only Cookie 及服务，远程部署无法延续组织会话。

本增量只修复 Control API 入口：localhost、127.0.0.1 和 IPv6 loopback 保持同主机的 8001 开发端口；其它部署沿用当前页面 origin（包括 HTTPS 和非默认端口），与账号/平台入口合同一致。不从查询参数、Storage 或业务 DTO 接受任意服务地址。

## 第一轮自审

- 页面来源决定 Control 入口，账号与企业控制台不再切换到客户端 localhost；不改变 Cookie 属性、域或任何后端权限规则。
- localhost 与 127.0.0.1 的既有开发路径保留；IPv6 使用 URL.hostname 的标准方括号形式。不将站点跨源地址改写成看似同源的凭据转发。
- Gateway 的无 Cookie 调用地址是独立合同，本次不猜测外部网关域名。后续完整模型调用仍需验证实际部署提供的网关地址。

## 第二轮自审

- 从真实 main.js 提取初始化段，在单元中验证普通 HTTPS 域名、自定义端口、localhost/IPv4/IPv6；不是仅测试未被入口使用的 helper。
- 真实浏览器由隔离同源站点提交注册/组织/使用者/KEY，阻断其它 Origin，不通过响应替身或路由重定向掩盖固定地址问题。
- 两个 API 基址变量必须相同，禁止资源页和导航页各自使用一套 Cookie origin。JS 单元不能替代浏览器 Cookie 和页面跳转验证。
