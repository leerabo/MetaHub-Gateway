# PRD-123 私有网关本地 Secret 托管与轮换

状态：已完成；迁移 0115、本地 schema v2、客户环境 KeyStore/KMS 信封加密、原子轮换、固定连通性测试、
云端零明文引用同步和 Workspace Console 已交付。依赖 PRD-084/120..122，范围以 Excel PRD-123 为准。

## Excel 权威合同

- 本地 API：`POST /local/secrets`、`POST /local/secrets/{id}/rotate`、`DELETE /local/secrets/{id}`、`POST /local/provider-connections/{id}/test`；云端 API：`GET /edge/secret-references`。
- 对象：`LocalSecret`、`SecretReference`、`SecretVersion`、`SecretRotation`、`ProviderConnectionTest`。
- 事件：`edge.secret_reference_changed`、`edge.secret_rotation_failed`、`edge.provider_test_completed`。
- SLO：本地解封 p95 < 10ms；轮换不中断已接受请求；云端明文 0。

## 1. 产品闭环

tenant security_admin 只在客户环境输入 Provider Key/Token。本地 owner 使用 OS KeyStore/客户 KMS 包装数据密钥，以 owner-bound AES-256-GCM 保存版本；云端只同步随机 reference ID、kind、状态、revision、最后测试固定 outcome 和轮换时间。调用链通过引用解析活跃版本，Secret 不进入 Desired State。轮换采用 prepare -> connectivity test -> activate -> drain -> retire；删除前检查 live/LKG/rollback/backup 引用。

## 2. 0115 与本地 schema

- 云端只新增 `edge_secret_references` 和脱敏状态/outbox，数据库约束明确禁止 ciphertext、endpoint、value、token 等字段。
- 本地 schema 保存 Secret/Version/Rotation/ConnectionTest；密文 AAD 绑定 deployment/workspace/tenant/reference/version/kind/hash，密钥版本显式记录。
- 本地进程以最小权限解封，解封结果使用短生命周期字节缓冲，不缓存到通用 config，不实现可读回显 API。
- 云端 0115 与本地 schema 均有独立 migration/version；备份策略默认排除 OS KeyStore 根密钥，PRD-129 用客户控制 envelope 另行处理。

## 3. 接口、权限与页面

`/local` 只监听 loopback/Unix socket 或客户明确绑定的管理接口，并要求本地 tenant session、CSRF/Origin、step-up；云端 Platform/Support/Agent 都无写权限。输入从浏览器安全表单或 stdin 进入，不允许 query/argv；响应永不返回值、密文、长度或可枚举差异。Console 只展示 reference、kind、状态、版本、最后测试 outcome，并在提交后立即清空 DOM 和内存值。

## 4. 第一轮自审：密钥所有权

- P0：复用云端 tenant encryption 会让云端具备解密能力。修正为客户环境独立 KMS/OS KeyStore，云端只有 reference。
- P0：把 Secret 放入 Desired State 即使加密也会上云。修正为 manifest 只含不透明 reference ID，本地解析。
- P0：支持诊断返回上游 body/endpoint 会泄漏。修正为固定 outcome、latency bucket 和 capability code。
- P1：Secret 输入通过 CLI argv 会进入进程列表/history。修正为 stdin/安全 FD/交互隐藏输入。

## 5. 第二轮自审：轮换与恢复

- P0：覆盖 active version 无法回退且中断并发请求。修正为不可变版本、原子 reference CAS、旧版本 drain 后再 retire。
- P0：删除只检查当前配置会破坏 LKG/rollback。修正为查询 live、staging、LKG、rollout 和 backup 全部引用。
- P1：错误信息按 Provider 响应变化可成为旁路。修正为固定错误映射和同形响应。
- P1：内存/日志/测试 fixture 残留会绕过 DB 扫描。修正为敏感审计覆盖源码、fixture、日志、进程参数、崩溃转储、DB 和制品。

两轮均为修正后通过，无未关闭 P0/P1。

## 6. 专项验证计划

0115 云端/本地迁移；错误 KMS/AAD/key version、并发轮换、测试失败回退、drain、引用阻断删除、动态撤权；本地监听边界与跨 tenant；真实 Provider stub 且零原文；三浏览器表单/剪贴板/迟到响应/登出清理；解封与无中断 SLO；云端 DB/日志/抓包/支持包/备份/命令行敏感明文 0。

## 7. 实施闭环

- Shared SDK 固定 `SecretReference`、本地密文 envelope、三类安全事件和 Agent sync batch/receipt exact
  schema；AES-256-GCM AAD 绑定 deployment/tenant/workspace/reference/version/kind/provider binding，错误只含
  固定 code，敏感 key/value 字段不进入 `repr`。
- Gateway `local_secrets` 使用独立 SQLite schema v2 和 deployment-bound KeyStore；Windows 使用 DPAPI，
  test/customer KMS 使用独立 protector。创建、测试、轮换、租约 drain、引用阻断删除、outbox 重传和重启
  恢复均持久化；Secret 只以可清零字节缓冲进入 tester，API 无读回路径。
- `/local` 管理面要求 loopback/显式 CIDR、exact Origin、`Sec-Fetch-Site`、无 forwarded headers、十分钟
  deployment-owned handoff Session、Secure `__Host-` Cookie、CSRF 和 step-up。handoff verifier 只保存 SHA-256
  摘要，常量时间比较，并实施有界客户端失败/锁定状态。
- Control 0115 只保存 `SecretReference` 安全投影、sync batch/receipt、固定事件和 outbox；Agent route 恢复
  mTLS、EdgeSession、deployment capability 与复合 owner，同一 batch/event 异 hash 失败关闭。Workspace
  `GET /edge/secret-references` 只授予显式 `edge.secret_reference.read`，平台角色没有本地写路径。
- Console 严格校验云端投影和本地响应；只有 exact `security_admin` 可显示本地写控件。handoff/Secret 输入
  不进入页面 state、Storage、URL 或日志，提交/取消/离页/登出和迟到响应均清空 DOM、字节缓冲与本地 Session。

## 8. 专项验证与批次边界

0114 -> 0115 往返/fresh/down guard、复合 owner、不可变事实、权限扩展、密文/KMS/AAD/key version、并发
轮换、失败回退、drain/lease、引用阻断删除、Provider 固定结果、sync 幂等/乱序/异 hash、local Origin/CIDR/
Cookie/CSRF/step-up、CORS、动态撤权、迟到响应、零 Storage 和解封 p95 均已执行，结果见
`docs/testing/PRD-123-tests.md`。

PRD-123 完成后下一权威项为 `PRD-124 - 云管私有Gateway安装包、预检与接入向导`。三浏览器三缩放、
真实客户 KMS/Provider、轮换并发长稳、云端/支持包全介质扫描和 0111..0122 全迁移矩阵仍按批次纪律保留到
PRD-130 独立提交后统一执行 G5，不能以本条聚焦测试和单浏览器 fail-closed 结果替代。
