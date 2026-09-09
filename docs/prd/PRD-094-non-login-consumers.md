# PRD-094 免登录使用者增量

## 领域与设计

用户确认的首期流程：管理员创建组织、接入模型和路由、建立使用者目录并发放独立 KEY。使用者不创建 GlobalAccount、legacy User 或 Membership，不需要邮箱、密码或管理后台会话。创建组织的 Owner 不是平台管理员。

使用者 ID 不变，允许同名、改姓名/部门及多个 KEY。KEY 归属只在创建时绑定且不可转移，轮换保留 KEY ID、历史归属和限制。禁用使用者阻止全部关联 KEY 的后续鉴权，单独启用 KEY 不得绕过。

接口、迁移、逐次权限复核、审计原子性、两轮编码前自审见 `S1-organization-consumer-trial-batch-design.md` 的使用者合同与两轮自审。最终并发决策为 `credential_revision` CAS，不使用短前缀作版本。

## 交付与未完成项

本地已交付组织管理员目录、发放、轮换、停用与 Gateway 身份关联。使用者费用可通过 tenant + KEY 的不可变关联追溯，但按人聚合查询、财务界面和兼容工具端到端仍未交付。未来 SSO 需显式受控绑定，不按姓名或邮箱自动合并、不隐式提权。

验证记录：`docs/testing/PRD-094-consumers-tests-20260909.md`。敏感审计：`docs/testing/PRD-094-consumers-sensitive-audit-20260909.md`。本记录不替代原 Membership、邀请、Owner 转移与空间切换的整条验收。

## 浏览器表单可读性补充

真实创建使用者后发现既有 KEY 表单标签 13px、额度标签 11px 与输入 12px，未满足用户对字号的反馈。第一轮自审限定修改 KEY 创建与限制表单，不扩大为全站样式重构；创建标签 15px、额度标签 14px、文字输入 16px，保留稳定网格及现有窄屏换行。第二轮自审要求在真实 1280px/390px 控制台检查输入字号和溢出，排除没有文字的 checkbox/radio，不通过降低文字输入标准让检查变绿。KEY 一次性明文截图前必须隐藏。
