# PRD-112 测试记录

状态：公共状态独立读面、事故/维护沟通、订阅验证与退订、安全审批、签名发布和 Platform Console 已完成专项实现与验证。每次 pytest 均使用新的显式 `--basetemp`，未复用或删除任何 `.pytest-*` 目录。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| Migration | `0103 -> 0104 -> 0103 -> 0104`、fresh、复合 owner、不可变事实、down guard、权限矩阵、完整性与外键 |
| Public read plane | 无 Control 数据库导入、签名/摘要/generation/expiry 严格校验、原子 head、有效缓存回退、损坏与过期失败关闭 |
| Publication lifecycle | 初始全正常基线、系统与人工 publisher 区分、未临期不重复发布、TTL 剩余三分之一时续签、发布失败安全重放 |
| Incident and maintenance | reliability 直接发布、security 双人审批与禁止自批、动态撤权、更新/监控/解决/复盘时间线、维护独立状态机与幂等重放 |
| Tenant isolation | 公共 projection 排除 Workspace 私有事故，私有查询只接受当前 WorkspaceSession 复合 owner |
| Subscription privacy | pending 验证、退订、单次 token 重放、联系地址加密、blind index、token hash、DTO/事件/审计不含明文 |
| Console | strict DTO、Cookie/CSRF/CAS/idempotency、权限裁剪、导航锁、响应式表格/表单、禁止浏览器持久存储 |
| Compatibility | Console PRD-109/110/111/112 四代聚焦回归，以及既有 fresh-schema 当前头断言更新到 `0104` |

## 专项结果

| 检查 | 结果 |
| --- | --- |
| PRD-112 Migration | `3 passed` |
| PRD-112 API / publication | `7 passed` |
| Deployment Compose / scripts | `9 passed` |
| Console PRD-109/110/111/112 | `21 passed` |
| Console lint | `351 files` 通过 |
| PRD-112 Python Ruff | `All checks passed` |
| Python compileall | PRD-112 涉及源码树通过 |
| 真实迁移 smoke | 当前头 `0104`、`integrity_check=ok`、外键违规 `0` |
| Compose 语法 | 当前主机未安装 Docker CLI，无法执行；YAML/服务合同由仓库测试继续覆盖 |
| `git diff --check` | 通过；Compose 示例仅有 Git 行尾转换提示 |
| 新增内容敏感字面量扫描 | 无真实凭据、私钥、TOTP seed、生产 Token、联系地址明文或内部状态对象路径命中 |

批次级全数据库、完整集成/流程、真实浏览器、性能和安全回归按约定在 PRD-113 完成后统一执行。PRD-112 的公共状态页可用性 `99.99%`、p95 `<500ms` 和事故确认后 10 分钟内首条更新门限将在该批次门禁中验证。

## 验证结论

- `status-page` 是独立只读 ASGI 容器，只挂载只读 bundle volume，不导入 Control 数据库模块，也不继承 Control 的共享 env/数据库/认证配置；Control 停止后仍可读取最后一个未过期签名版本。
- Control 以 `system` publisher 幂等发布初始全正常 bundle，并在 TTL 剩余三分之一时续签；人工发布使用 `operator` publisher，二者不会混淆审计归属。
- 新版本先写不可变对象并完成验签，最后原子替换 `head.json`；签名、KMS 或存储失败不覆盖旧 head，重放不重复业务事实。
- 公共 DTO 只包含公开 component/region code 与人工公开文案；Workspace/tenant/account、内部告警文本、拓扑、Secret、Prompt/Response 和联系人不进入 bundle。
- security 事故要求不同 operator 的实时 `platform.status.security_approve`；发布者不能自批，撤权后审批失败关闭。
- 订阅联系人使用独立密钥加密并以 blind index 去重，验证/退订 token 只保存 hash；公开 mutation 返回同形结果以降低枚举风险。
- Console 只使用 Cookie 会话，写请求绑定 CSRF、revision 与幂等键；只读、写入和安全审批控件按权限独立裁剪。
