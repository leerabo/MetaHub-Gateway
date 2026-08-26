# PRD-119 测试记录

状态：已完成。官方 Provider 签名目录、区域/能力筛选、Workspace 实例化与预检、兼容说明、
Platform 发布和企业/平台 Console 已交付；本条保持独立设计、测试记录、敏感审计和提交。下一项为
`PRD-120 - Edge Agent注册、设备身份与mTLS`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| Migration | `0110 -> 0111 -> 0110 -> 0111`、fresh head、重复执行、Ed25519 seed 验签、目录不可变、业务事实 down guard、tenant deletion manifest |
| Enterprise API | Workspace Cookie、旧 Bearer 与 machine authority 的动态权限复核；目录筛选、严格 DTO、实例化 owner/CAS/幂等、兼容说明和 preflight |
| Platform API | Platform Cookie/CSRF、manage/security_read 分权、canonical manifest、Ed25519 发布、幂等重放、传播和双凭据失败关闭 |
| Security | endpoint 服务端重建、非 HTTPS/userinfo/query/fragment/IP/private/link-local/metadata/redirect 拒绝；企业与 Platform DTO/凭据域隔离 |
| Console | Provider Catalog 模式、四维筛选、实例化后续动作、preflight；Platform 发布/安全投影分离、一次性签名输入清理和移动布局 |
| Compatibility | Admin auth、Console navigation、Workspace 创建/切换、Provider 健康/Key/CRUD、PRD-118 machine identity |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| PRD-119 Migration | `3/3 passed` |
| PRD-119 API/security/SLO | `7/7 passed` |
| PRD-119 Console 专项 | `6/6 passed` |
| 必要后端回归 | `30/30 passed` |
| PRD-118 全量回归 | 首轮仅 machine auth p95 `21.41ms` 超过 `<20ms`；隔离节点通过，随后完整重跑 `6/6 passed`，阈值未调整 |
| Console 全量 | `1108/1108 passed`，零失败、零取消、零跳过 |
| Console lint | `365 files` 通过 |
| Python static | PRD-119 候选 Ruff、compileall 与 `git diff --check` 通过 |

## API、性能与 Browser

- 实际 Control API 关闭系统代理继承后连续查询 40 次，四维筛选
  `active + deepseek + chat + cn-mainland` 只返回一个 DeepSeek 条目，p95 为 `68.87ms`，满足 `<300ms`。
- 自动化在同一当前 schema 中验证 preflight 成功/幂等终态小于 30 秒；签名目录发布后立即读取新版本，
  满足传播小于 5 分钟。目录更新不修改既有租户 Provider。
- 真实 Browser 直接读取本地 seed 管理员凭据并仅在内存中使用，完成登录、Provider Catalog 四维筛选，
  页面显示“筛选已应用”和唯一 DeepSeek 卡片；页面横向溢出为 `0`，warning/error 为 `0`，验收后已退出。
- PRD-119 独立验收使用 Chromium；S4 G4 要求的 Edge/Firefox 与三档缩放在独立提交后统一复核，
  不以本条 Chromium 结果替代批次门禁。

## 安全与静止终态

- 目录 seed 只保存公开 Ed25519 验签材料；发布私钥只在测试内存中生成、签名后释放，不进入迁移、
  API 响应、Console state、日志、数据库或提交。
- Platform 不能读取 Workspace Provider Key；企业目录 DTO 不含签名、内部 adapter/error-map、运营开关、
  endpoint secret 或其他租户事实。preflight 只持久化 fixed outcome、latency 和 capability hash。
- 提交候选共 `41` 个文件，其中 `40` 个文本文件；本次新增/修改差异对 seed 密码、Provider/Virtual Key
  和 `.env` Secret 的逐字命中为 `0`，高风险访问密钥、长 Bearer 与 Private Key 格式命中为 `0`。
- 当前本地开发数据库扫描覆盖 `573` 张业务表、`3670` 行；6 个本地敏感值在文本列和原始数据库字节中的
  命中均为 `0`，高风险格式命中为 `0`。
- 暂存区和禁止文件在提交前独立复核；旧 `.pytest-*`、`.tmp-*`、用户文件以及 PRD-101..105 文档均不纳入本提交。

## 批次门禁

PRD-119 独立提交完成后执行 S4 G4，统一复核 0105..0111、协议/撤权、驻留消费者、机器最小权限、
Provider 签名/SSRF、三浏览器三档缩放、SLO、静止终态和敏感/禁止文件。G4 结果单独记录，
不得反向混入 PRD-119 功能提交的交付事实。
