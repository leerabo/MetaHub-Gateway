# PRD-096 测试记录

状态：已完成。PRD-096 的 0088 迁移、平台 Workspace 目录、运营详情、生命周期、风险标签、Owner 通知、
Console 交互和真实浏览器验收均已通过；权威下一项为 `PRD-097 - Workspace onboarding 重建`。
`saas_incremental_roadmap_complete=false`，`all_108_prds_complete=false`。

## 已执行门禁

| 测试组 | 实际结果 | 覆盖 |
| --- | --- | --- |
| DB/API/SEC/GR/FLOW/PERF | PRD-096 API `5 passed`；migration `1 passed` | 九个 Platform Workspace API、五角色字段投影、HMAC 游标、健康 fresh/stale/损坏源、风险职责分离、CAS/幂等、20 路冻结并发和非 pristine 回滚阻断 |
| Compatibility | PRD-093..095 六文件兼容组通过 | Workspace 创建、Membership/切换、Platform IAM/Session 与 0085..0087 能力在 0088 上继续有效 |
| WEB/UI/UCD/COMP | PRD-095/096 focused `14 passed`；Console full `996 passed`；lint `316 files` | 精确搜索、组合筛选、详情/健康/事件、写后全快照刷新、迟到响应隔离、角色控件裁剪、移动端堆叠行和敏感 Storage 零调用 |
| Static | 定向 Ruff、Python compile 和 `git diff --check` 通过 | 新增服务、路由、迁移、测试、Console 与规划文件；全仓注释检查仍受既有基线阻断，不计为本 PRD 通过项 |

## 迁移与数据验收

- 0088 建立 Workspace operational state、risk tag、health projection、action intent、Owner notification
  outbox、operational event 与 event outbox；最新迁移为 `0088`，共 88 条。
- pristine 0088 可回退到 0087 并重新 apply；存在 suspend/restore/risk/contact/action/event 等安全事实时
  down 原子失败，不留下部分回滚。
- 浏览器隔离运行库有 4 个 Workspace；完成风险标签、Owner 通知、冻结与恢复后，目标 Workspace 最终为
  `active`、operational revision `3`，保留 1 个 active 风险标签、1 个通知 outbox 和 2 个生命周期事件。
- `PRAGMA integrity_check=ok`，foreign key violation 为 0。

## 授权与并发结论

- 目录、详情、健康和事件均由 PlatformSession 逐请求鉴权；owner、ops、support、security、
  customer-success 只获得各自后端投影，Owner 联系方式、成员邮箱、Provider Secret、Token、Prompt、Response
  和源健康信号不会进入响应。
- 目录 search 只接受精确 Workspace ID 或组织 slug；cursor 绑定角色、Operator revision、筛选条件和方向，
  篡改或跨角色复用固定失败。
- suspend/restore 使用 operational revision、If-Match 与 Idempotency-Key；20 路同目标冻结并发恰有一个提交，
  旧 WorkspaceSession 立即撤销，restore 不复活 deletion/conflict 状态。
- platform_security 只管理 security/compliance 标签，platform_customer_success 只管理 payment/adoption
  标签；通知只创建受控模板 outbox，不返回 Owner 地址。

## 浏览器验收

- 应用内 Browser 连接 `http://localhost:5173` 与 `http://127.0.0.1:8001`，直接使用本地测试身份完成
  GlobalSession 登录和真实 TOTP PlatformSession 换取；验证码提交后立即清空变量与 DOM。
- 目录实测 4 条记录；精确 slug 搜索只返回 1 条，active + Region + organization 组合筛选返回 4 条。
  详情中的 health、risk、Owner contact、action intent 和 event timeline 均来自真实 0088 API。
- 实际添加 security risk tag、创建 Owner 通知、经页面内确认执行 suspend，再经独立确认执行 restore；最终
  状态为 active、revision 3，事件时间线同时显示 suspended 与 restored。
- 390、1280、1440 三档 root 横向溢出均为 0；390 档 7 个目录字段全部使用可见标签堆叠，当前导航项自动
  滚入可视区；1280 档冻结/恢复确认按钮具有可读前景与背景；1440 档目录表格和 14 项导航稳定。
- 浏览器 console error/warning、Control ERROR/CRITICAL 和 Console 服务错误均为 0；截图中敏感输入为 0。
- 安全截图位于 Git 忽略目录 `artifacts/prd096/browser/`：
  `workspace-directory-390.png` SHA-256
  `6820a7f577d85626fab155501f137d2cacebb3f0efc43cccf1920432813773e9`；
  `workspace-lifecycle-confirm-1280.png` SHA-256
  `98d17fc3a89ce57777fd4a5eea373021511e96affa40b60463d75790a3c8f239`；
  `workspace-directory-1440.png` SHA-256
  `3f685a184dbb1d4e6935a6dbe0895a4f5e9ed4f73267a19a405290388762b7ed`。

## 敏感审计

源码、测试、迁移、文档和生成 Excel 执行私钥、云密钥与生产 token 模式扫描；新增命中为 0。Git 候选中
没有 SQLite、WAL、SHM、TOTP 文件、浏览器 profile、Cookie、网络正文或原始 Owner 联系方式。测试凭据只
存在于受控本地夹具，浏览器验收结束后已清除内存变量，并在最终清理中删除临时凭据文件和数据库。

本记录关闭 PRD-096；下一权威需求为 `PRD-097 - Workspace onboarding 重建`。
