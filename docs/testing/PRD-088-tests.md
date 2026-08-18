# PRD-088 客户/渠道白标配置测试记录

日期：2026-08-18

结论：PRD-088 的 0081、RBAC revision 23、BrandingConfig、EVT-100/source outbox、Branding API、
PAGE-120、公共登录品牌和认证后 Console shell 已通过本记录所列门禁。PRD-089 未启动；
`all_108_prds_complete=false`。

## 1. 范围与两轮自审

- Excel `01_PRD总表` 第 89 行、`03_M2-M3扩展交付` 第 25 行、PAGE-120、API-400/401、OBJ-121、
  EVT-100 与 TC-0175/0176 共同冻结本条，不按编号猜测范围。
- 第一轮关闭 Logo/登录文案/域名泄漏、tenant 枚举、SSRF、先提交后发事件和订阅追溯问题。
- 第二轮关闭动态撤权、Partner branding 混用、revision CAS、迟到响应、Blob URL、换租户 CSS/title 残留和
  audit/source event/outbox 非原子写入问题，无未关闭 P0/P1。
- 所有业务表和事实均 tenant-qualified；后端逐请求校验 Session tenant、RBAC 与 owner，前端隐藏入口不作为授权。

## 2. 自动化门禁

| 分组 | 结果 | 覆盖 |
| --- | ---: | --- |
| PRD-088 Branding 集成 | `4/4 passed` | API、PNG、公共投影、租户/RBAC、导航、审计与 outbox |
| PRD-088 Migration 0081 | `9/9 passed` | fresh/repeat/down/re-up、guard、既有自定义角色与新 tenant |
| Console Branding | `9/9 passed` | strict client、PATCH/CAS、Logo、PAGE-120、shell/login 与生命周期 |
| Console 全量回归 | `935/935 passed` | 所有现有 Console 单元合同 |
| 静态检查 | `passed` | Ruff 精确范围和 Console lint |

## 3. Migration 与数据库

- 从空 SQLite 实际应用 `0001..0081`，最新版本 `81/0081`；重复应用、回退 0080、重升 0081 均验证。
- 每阶段 `PRAGMA integrity_check=ok`、`PRAGMA foreign_key_check` 为 0；0081 安装 BrandingConfig、
  immutable event/outbox、owner/size/CAS/lease/down guard 和 RBAC revision 23。
- 真实运行库最终为 `schema=0081`、BrandingConfig 1、audit/event/outbox 各 3；实际运行 worker 后 3 条 outbox
  均为 `no_subscription`，pending/processing 为 0，敏感哨兵命中 0。

## 4. Control/API 与恢复

- admin GET/PATCH、viewer GET/403 PATCH、无认证 401、未知/非法字段、If-Match 冲突、Logo 校验/清除、
  公共 slug 默认投影、跨租户和动态撤权均返回统一 code/request_id/trace_id。
- 受保护 JSON 和所有错误为 `no-store, private`；Logo 端点保留 `private, no-store`。生产外层中间件只补缺失的
  缓存边界，不覆盖端点专用安全 Header。
- 审计、EVT-100 和 outbox 在同一事务写入；事务失败整体 rollback。覆盖空订阅终态、租约接管、旧 worker CAS、
  固定退避、十次失败终态和快照不追溯后来订阅。
- 真实 HTTP 最终结果：`admin_get=200 viewer_get=200 viewer_patch=403 dynamic_revoke=403`
  `public_fallback=200 no_store=ok`。

## 5. Browser PAGE-120

- 真实 Console/Control 与 Chromium 验证管理员保存后 revision `3 -> 4`，页面只显示安全 request ID；viewer
  全字段只读、没有保存按钮并显示只读状态。
- 动态撤销 `branding.read` 后 Branding 导航和 PAGE-120 立即消失，旧租户产品名、Logo、CSS 和 title 清理并
  恢复默认 MetaHub；恢复权限后只读页面重新可见。
- `1280x720`、`390x844`、`1440x900` 三档页面均非空、无遮挡且无横向溢出；对应 scrollWidth 为
  `1265`、`375`、`1425`。验收结束已重置 Browser viewport。
- 浏览器凭据、Token、Logo 正文、品牌正文和响应正文均未写入正式证据。

## 6. 隐私与禁止文件

- Provider Key、Token、Prompt、Response、Logo base64、登录文案和域名原文在日志、审计、事件、outbox 和
  正式文档中的敏感哨兵命中均为 0。
- 候选提交只允许源码、测试、迁移、文档和开发状态工作表；`.env`、SQLite/sidecar、日志、cache、coverage、
  build、Office 临时文件、私钥、失败诊断 artifact 和 `.tmp` 不进入 Git。
- 工作簿只更新 `12_开发状态`，保持公式、样式、合并单元格和其他工作表不变；所有总体状态保持 False。

## 7. 下一项交叉确认

Excel `01_PRD总表` 第 90 行、`03_M2-M3扩展交付` 第 26 行、PAGE-121、API-402..406、OBJ-122/123、
EVT-101/102 与 TC-0177/0178 共同确认下一条权威未完成项为：

`PRD-089 | M2-企业增强 | P2 | 扩展层 | 数据导出删除 | 租户数据导出与注销流程`

本次只登记，不实现 PRD-089；`all_108_prds_complete=false`。
