# MetaHub SaaS 与云边威胁模型

版本：`saas-threat-model/v1`
所有者：PRD-090
方法：STRIDE；风险在后续 PRD 中关闭，本文件冻结跨阶段安全不变量。

## 1. 信任边界

1. 公网用户与 API Edge。
2. Global Identity 与 Workspace IAM。
3. 平台 IAM 与租户业务域。
4. Control Plane 与云 Gateway Cell。
5. SaaS 云端与客户 Edge Agent。
6. Edge Agent 与本地 Secret/Provider Endpoint。
7. Region、备份和对象存储边界。
8. 浏览器Cookie、Bearer兼容入口与子域边界。
9. 平台支持委托、客户审批与租户资源边界。

## 2. P0/P1 威胁与控制

| ID | STRIDE | 威胁 | 风险 | 冻结控制 | 验证责任 |
| --- | --- | --- | --- | --- | --- |
| TM-01 | S/E | 伪造 `tenant_id` 或 Workspace 切换获得他租户权限 | P0 | 身份和 active Workspace 只从后端 Session 恢复；资源复合 owner；跨租户同形 404 | PRD-091/094/137 |
| TM-02 | E | 平台support角色自动成为租户管理员或自批访问 | P0 | 平台/租户角色分域；JIT；申请人不得审批；客户和平台各一名不同审批者；限时、字段裁剪；break-glass告警和事后双复核 | PRD-095/137 |
| TM-03 | S/R | 复制 Edge 注册令牌或设备证书控制另一部署 | P0 | 单次短时注册、CSR PoP、设备本地不可导出密钥、独立Edge CA、SAN owner、mTLS、轮换、吊销、防重放 | PRD-120 |
| TM-04 | T/E | 伪造、降级或重放 Desired State | P0 | Ed25519签完整RFC 8785 envelope；固定domain/protocol；目标audience；单调持久高水位；过期；原子fsync apply/LKG | PRD-121 |
| TM-05 | I | Provider Key/Token/Prompt/Response 进入云端遥测或支持包 | P0 | 上传白名单、本地 Secret、客户预览、自动敏感扫描、云端明文命中 0 | PRD-122/123/125 |
| TM-06 | T/R | Usage 离线重放或丢水位导致少计/重计 | P0 | tenant+deployment+sequence 幂等、签名批次、单调 ACK、对账差异 | PRD-122 |
| TM-07 | E/D | 共享 Cell 的缓存、队列或连接池出现跨租户/噪声邻居 | P0 | owner-qualified key、舱壁、配额、公平队列、持续隔离测试 | PRD-101/106/137 |
| TM-08 | I | 自动故障切换把境内数据带到未授权 Region | P0 | 驻留策略是硬路由约束；无合规目标时失败关闭 | PRD-111/117 |
| TM-09 | S/E | 全局账号链接、旧同邮箱合并或邀请竞争劫持Membership | P0 | 未重新验证不合并；冲突队列；更严格状态并集；一次性意图、原子唯一约束、邀请audience、重认证 | PRD-091..094 |
| TM-10 | T/R | 支付回调重复/乱序造成重复权益或账务 | P0 | 原始字节验签、收据唯一、状态机、账本序列和三方对账 | PRD-103/104 |
| TM-11 | D | Identity、Redis、KMS 或配置分发故障拖垮 Gateway 热路径 | P1 | 热路径解耦、有界 LKG、固定 fail-open/closed 表、熔断和容量门禁 | PRD-106..113 |
| TM-12 | R | 高权限操作无法追责 | P1 | actor/reason/request_id、不可变审计、outbox、双人审批 | PRD-095/135 |
| TM-13 | T | 离线包乱序覆盖较新本地状态 | P1 | audience、签名、版本向量、过期、冲突报告、人工决策 | PRD-130 |
| TM-14 | I/E | 日志、指标标签或错误正文泄露账号/租户/上游秘密 | P0 | 固定错误、敏感字段 denylist+allowlist、高基数治理、证据扫描 | 全部 PRD |
| TM-15 | S/E | Cookie、Bearer或子域Cookie被混合解释并选择较高权限主体 | P0 | `__Host-` Cookie；无Domain；SameSite；Origin+CSRF；多凭据并存固定拒绝；issuer/audience/key域互斥 | PRD-091/094/116 |
| TM-16 | S/D | credential stuffing、登录注册时序或错误差异枚举账号 | P0 | 统一时序和响应；速率/设备/IP风险；MFA；不回显身份是否存在；失败事实可追踪 | PRD-091/092/110/116 |
| TM-17 | E | 旧default租户admin按邮箱被静默提升为platform_owner | P0 | 禁止邮箱推断；显式一次性bootstrap ceremony；强MFA；双人见证或恢复密钥；完成后关闭并审计 | PRD-095 |
| TM-18 | T/E | Workspace与tenant重绑或授权只检查tenants.status绕过注销状态 | P0 | 一一不可变绑定；同时复核Account、Membership、tenant_data_states和撤销tombstone；不接受客户端owner | PRD-091/093/094 |
| TM-19 | T/E | 签名root在离线节点撤销不生效或云端DB直接换根 | P0 | trust-manifest TTL；current root签名更新；双人批准离线recovery bundle；设备显式导入；暴露窗可见 | PRD-121/130 |
| TM-20 | I/E | Secret密文跨执行平面、owner、版本或用途移植 | P0 | 每secret/version独立DEK；AAD绑定execution_plane/workspace/deployment/secret/version/purpose；key domain隔离 | PRD-100/123 |
| TM-21 | E/I | 前端隐藏菜单被当作授权，或平台运营/品牌/Provider工程/商业账务页面泄漏到企业Workspace | P0 | 后端返回角色页面投影但API逐请求鉴权；直达URL/DOM/预取/迟到响应负测试；源码64项导航逐项处置；平台页面仅接受PlatformSession | PRD-094/095/120/139..147 |
| TM-22 | T/E/R | 平台套餐/商业账与企业BYOK内部成本账混用权限、价格或调整事实 | P0 | 独立账务域与角色；价格版本；企业只提交争议、平台独立裁决；双边对账但禁止互写；金额调整不可变审计 | PRD-098/099/104/144/147 |

## 3. 统一失败策略

| 依赖/场景 | 策略 |
| --- | --- |
| Session、Membership、RBAC 不可用或损坏 | 失败关闭，不回退 Token claim 或前端角色 |
| 同请求出现多种可解释身份 | 固定失败，不任选Cookie、Bearer、Platform、Support或Device身份 |
| DLP、安全阻断、预算准入不可用 | Provider 前失败关闭 |
| 配置分发暂不可用 | 使用未过期 LKG；超过陈旧上限按配置类别失败关闭 |
| 日志/遥测管道不可用 | Gateway 使用有界本地安全队列；不得记录正文；满载时按固定降级并告警 |
| Webhook、邮件、通知不可用 | 不回滚已提交业务事实；持久 outbox 重试 |
| KMS 无法解封新 Secret | 不调用 Provider；已有短时内存租约按明确期限处理 |
| Edge 与云端断网 | 本地 LKG 继续服务；积压白名单遥测；不伪造云端实时状态 |
| 离线trust manifest过期 | 安全与授权配置失败关闭；不声称签名root或设备撤销实时生效 |
| 驻留合规目标不可用 | 禁止跨区自动恢复，进入受控降级 |

## 4. Owner与驻留验证矩阵

| 面 | 必须绑定的owner/位置 | 失败要求 |
| --- | --- | --- |
| SQL、RLS与复合外键 | workspace/tenant + object；主Region | owner或生命周期不一致即失败关闭 |
| Redis、限流与cache | workspace/tenant + environment + generation；同Region | 禁止全局裸key和跨owner命中 |
| Queue、lease与outbox | workspace/tenant + job/source/delivery | claim后、副作用前重验revision |
| Object、search与vector | workspace前缀/partition + KMS context；允许Region | 服务端生成路径；禁止裸index |
| Logs、metrics、trace与spool | 受控owner摘要；同驻留Region | 标签白名单；正文和Secret命中0 |
| Backup、DR与临时文件 | 获准Region和保留策略 | 无合规目标禁止跨区恢复 |
| Edge遥测与支持证据 | workspace/tenant + deployment/grant | 上传字段白名单、客户预览、到期删除 |

完整data-class x location合同以`docs/architecture/saas-target-architecture.md`第5.1节为准；CDN/WAF、监控、缓存、备份和临时文件都不得排除在驻留检查之外。

## 5. 证据限制

安全测试可使用固定占位哨兵，但哨兵不得形似真实 Provider Key、Token、密码、Prompt 或 Response。正式证据只保存计数、错误码、hash、水位、版本和脱敏截图；不保存请求/响应正文、证件原件、本地文件路径或第三方异常原文。
