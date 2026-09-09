# PRD-146 导航别名与提示生命周期敏感审计

日期：2026-09-09。范围：主入口候选转换、合法导航提示清理、定向测试及两端资产摘要。

- 精确七个暂存文件：main.js、prd146ConsoleEntry.test.mjs、前后端 production manifest、
  PRD-146 设计、测试记录与本审计。不纳入主页复核文档、用户 PRD-101..105 或 outputs。
- `.tmp/g6_resource_probe_audit.mjs 146-navigation-notices` 检查严格 UTF-8、高置信凭据模式、
  精确暂存名单/字节、来源与原始证据 SHA256，报告为
  `.artifacts/g6-security-20260907/prd-146-navigation-notices-sensitive-audit-r1.json`。
- 精确 Kimi Key 与主加密密钥只在既有本地审计进程内存中解密和比对，不输出其值。
  报告 `prd146-navigation-notices-key-audit-r1.json` 包含暂存文件、当前证据与本地日志的摘要。
- 保留原始红灯、完整前端和浏览器证据；截图只含现有公开壳层及反馈聚合，不含凭据或请求正文。
- 受保护基线 SHA256 应保持
  `572173FBF10027815EA1E1ECBDD201D3B7084F6335A86E436A762A68945F3DB6`。
- 真实调用只重放原有幂等反馈并读取日志/汇总；独立脚本只注销自己创建的会话。
  未提交模型请求、修改账务历史、放宽权限或变更 Cookie 设置。

结果以同名 JSON 报告为准。模式扫描及本地精确字节检查不等同于独立安全会签或 G6/G8 验收。
