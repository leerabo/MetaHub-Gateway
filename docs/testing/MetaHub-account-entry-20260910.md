# 登录落点与账号设置分区验收

范围为本轮登录后误入综合账号页、缺少资料编辑入口、偏好加载失败及表单堆叠反馈。
设计与两轮自审见 `docs/prd/MetaHub-account-entry-revision-20260910.md`。
不修改 schema，不重置试用数据，不调用外部模型。本记录不代表全部增量 PRD 已完成。

## 改动与安全边界

- 已有组织的普通登录和会话恢复进入控制台；无组织仍只显示创建组织主流程。
- 主动打开账号设置、组织选择、邀请和恢复保证级别会话不自动进入控制台。
- WorkspaceSession 必须匹配账号组织目录且授权摘要有效。跳转目标固定，不接收任意外部 URL。
- 账号页一次只渲染一个栏目，只请求当前领域；栏目切换取消旧请求并清理一次性敏感值。
- 接入引导的所有合法步骤保留实际配置入口，资料步骤可以编辑组织名称。
- 语言偏好以内存中的已验证 GlobalSession 标识隔离 owner，不扩展公开账号 DTO。
- 偏好接口允许浏览器同时携带账号与空间 Cookie，但只使用账号会话和账号 CSRF 授权。
  平台或支持会话混用、Authorization、缺失或重复账号会话、错误 CSRF、跨域写入继续拒绝。

## 已完成验证

- 前端分区回归 `.artifacts/account-sections-full-20260910.xml`：2018 项通过，无失败或跳过。
  该轮早于最终偏好 owner 修复和最后一项新增入口用例，不冒充最终源码全量重跑。
- 最终相关入口与导航回归 `.artifacts/account-entry-final-focused-20260910.xml`：32 项通过。
- Cookie 共存与拒绝边界 `.artifacts/account-preference-cookies-after-20260910.xml`：9 项通过。
- 偏好 CAS、幂等、outbox 和领域隔离 API `.artifacts/account-preference-api-20260910.xml`：2 项通过。
- 最终前端 lint、生产边界检查通过：272 个模块、290 个发布资源；后端受影响文件 Ruff 通过。

## 浏览器发现与修复

- `.artifacts/s1-browser-9l7phx3z` 暴露偏好读取依赖不存在的公开 account_id 字段，已修复。
- `.artifacts/s1-browser-ntzm8d85` 暴露正常双会话 Cookie 被偏好接口拒绝，已修复。
- `.artifacts/s1-browser-elofba6w` 桌面流程和手机账号栏目已走通，但新增多轮登录与历史流程
  超过原脚本 240 秒总时限而终止。组织流程总时限改为 420 秒，每次浏览器操作仍为 12 秒。
- `.artifacts/s1-browser-wu0xboqt` 在备份完整性检查同时运行时登录超时，不能算成功证据。
  备份完成后单独重跑，未放宽每次操作的时限。
- 偏好保存断言要求真实 PATCH 返回 200、无错误提示，历史返回重新读取后仍显示已确认。

## 发布准备

候选为 `.tmp/trial-entry-stage-Xm8fHI/console-web/dist`，资产集摘要
`6a5b2f5857f73a3bdce56a110acc377952f5154b7af452e99b2f90fbee79a5e6`。
发布前只读备份 `.tmp/account-entry-release-20260910/database-verified.sqlite3`，
schema 0150，完整性与外键检查通过。首次备份校验缺少项目 SQLite UDF，
加载原项目校验函数后对已完成备份只读复查通过，没有重写在线数据库。
发布脚本要求浏览器成功证据、候选资产摘要、备份摘要、旧版本摘要和服务进程身份都匹配。

## 最终验收与发布

- `.artifacts/s1-browser-h99gcm9p/acceptance.json`：同一生产候选全流程通过。
  桌面 1280 和手机 390 各完成注册、创建组织、正常登录、会话恢复、显式账号栏目、
  真实偏好保存与重读、密码栏目隔离、历史导航、缓存恢复、组织名称保存、使用者和 KEY。
- 两个视口合计 16 条流程、16 个引导目标验证通过；浏览器异常、资源失败、外部请求和
  真实模型调用均为 0。所有截图无横向溢出，表单输入字号不小于 16px。
  桌面和手机的组织选择、语言偏好及引导页面截图已人工查看。
- 2026-09-10 13:27 本地发布到 `http://localhost:5173/`，Control 健康检查通过。
  `.tmp/account-entry-release-20260910/publication.json` 保存发布证据，旧前端保留在
  同目录 `previous-dist`；只重启已核实身份的 Control，未关闭用户浏览器或重启前端服务。
- `.tmp/account-entry-release-20260910/post-publication-audit.json`：线上 290 个资源逐个
  请求并核对长度和 SHA-256，与已验收候选完全一致；源码资产集重新计算也一致。
- 发布后只读完整性、外键和 schema 0150 检查通过。备份与在线的 17 张身份、会话、
  使用者、KEY、计量和财务表无新增、删除或已有字段变化；无需重新注册或创建组织。
- 测试进程与隔离服务均已结束；正常试用服务继续运行。本轮未提交混合工作区改动。
