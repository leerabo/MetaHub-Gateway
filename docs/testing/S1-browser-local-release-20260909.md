# S1 真实开通流程与本地发布

## 本次交付

- `2fb2d80`：PRD-147 修复企业控制台 Control 同源地址，避免组织创建后访问客户端 localhost。
- `428da69`：PRD-094 放大 KEY 创建与额度表单文字。创建标签 15px、额度标签/摘要 14px、文字输入 16px、额度输入最小高度 40px。
- 独立隔离 Chromium 1280x900、390x844 完成注册、组织创建、使用者、独立 KEY、轮换和停用，10 阶段通过。原始证据 `.artifacts/s1-browser-6y8nim3f/acceptance.json`；详细边界见各 PRD 测试记录。
- 最终相关前端 104 项通过，0 failures/errors：`.artifacts/g6-security-20260907/s1-browser-final-frontend-20260909.xml`。

## 发布验证

本地入口 `http://localhost:5173/`。2026-09-09 13:23 UTC 发布后验证：`.artifacts/g6-security-20260907/s1-browser-release-http-20260909.json` 为 passed，逐个 HTTP 校验全部 289 个资源，并与生产转换后的当前源码字节一致。前后端资产摘要为 `02ca3886b2150ce1cca03f94d323a3bc6a567cd14c20d93593858820f677856d`。

主页、账号页、隐私页、状态页以及 Control/Gateway/Status health 均为 200；匿名访问使用者、KEY 和平台注册设置为预期 401。已复查已发布注册页桌面与手机截图，0 页面异常、无页面横向溢出、文字输入最小 16px。发布验证只读取页面和发放匿名验证码，不新建真实账号、不解答真实服务验证码、不调用模型。

发布前 dist 备份 `.artifacts/g6-security-20260907/s1-dist-before-origin-font-20260909.zip`；构建确认 output 为 console-web 的直接子目录 dist 后重新生成产物。Control 经 PID/程序路径/创建时间/端口复核后从 21572 更新至 22396；前端 PID 22020、Gateway PID 6956、Status PID 6892 保持运行。未执行迁移、恢复数据库或撤销用户会话。

## 明确未完成

隔离浏览器验证当前源码字节，发布 HTTP 校验所有产物，不能合称已对真实试用数据库完成全部浏览器写入。有效 KEY 的无效 feedback 请求返回 422，只证明鉴权；完整兼容工具的模型调用、公网 Gateway 地址、SMTP、企业 SSO、多浏览器/Linux/多 DPR、容量和 72 小时均需继续验证。

注册策略、组织本地创建、使用者及用量财务的较大增量仍有未整理的工作树代码，不能把上述独立修复提交说成整个批次代码已提交。平台计量旧提交另发现暂存错位，修复提交 `ef78328` 已按 `PRD-147-metering-commit-alignment-tests-20260909.md` 从 index 导出独立验证，运行中的正确工作树未改变。

本次只发布已验证的本地试用增量，完整 Excel 目标保持未完成，不以 72 小时观察阻塞试用。
