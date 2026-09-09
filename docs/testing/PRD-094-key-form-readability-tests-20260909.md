# PRD-094 KEY 表单字号验证

- 修复前真实浏览器在使用者创建后的布局检查失败：`.artifacts/s1-browser-v1fzaqv_/browser.json`。检查发现 KEY 表单的继承字号小于 14px。
- 修复后 `.artifacts/s1-browser-6y8nim3f/acceptance.json` 为 passed。1280x900 和 390x844 各完成注册、组织创建、两位使用者、两把独立 KEY、轮换和员工停用，共 10 个阶段。
- 六个布局采样的文字 input/select 最小字号均为 16px，页面横向溢出均为 false。已查看 `1280-completed.png`、`390-completed.png`；员工表单在手机纵向排列，无重叠。KEY 明细继续使用原有表格内部滚动，本增量不宣称其全部列同时可见。
- 相关前端合并单元结果：`.artifacts/g6-security-20260907/s1-browser-final-frontend-20260909.xml`。未为这次窄 CSS 修复重复全套 SaaS 测试。

这是当前源码、Chromium 和两个视口的证据。Edge/Firefox、1440、多 DPR、读屏及正式发布仍需各自的验证；不把此修复当作整个 PRD-094 完成。
