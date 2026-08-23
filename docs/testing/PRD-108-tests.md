# PRD-108 测试记录

状态：配置分发数据库、平台 API、worker、Gateway 同步与 Console 已完成专项实现和验证。每次 pytest 均使用新的显式 `--basetemp`，未复用或删除任何 `.pytest-*` 目录。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| Migration | `0099 -> 0100`、fresh、pristine down/up、分发事实阻断 down、权限矩阵、重建 Job 激活、完整性与外键 |
| Platform API/worker | PlatformSession、CSRF、权限、幂等冲突、closed DTO、worker 发布、full/delta、ETag/304、聚合水位、重建结果 |
| Internal owner/ACK | internal token、Gateway identity、有效 lease、Cell placement、URL 非授权、ACK 精确绑定、重复与迟到 ACK、单调水位 |
| Crypto/runtime | envelope 加密、Ed25519 测试签名、UUID owner 合同、source/manifest hash、signature 和撤权水位矛盾 fail closed、KMS 不可用 |
| Gateway | HTTP full/delta/304、准确 ACK 路径、撤权水位单调、断网 LKG、2 秒撤权 deadline、30 秒普通 LKG、重启重新验签与原时间恢复 |
| Console | strict DTO、Cookie/CSRF/idempotency、read/rebuild 独立裁剪、水位/ACK lag/stale/LKG/重建、窄屏标签行、无 Storage override |

## 专项结果

| 检查 | 结果 |
| --- | --- |
| PRD-108 Migration | `3 passed` |
| PRD-108 API/runtime | `3 passed` |
| Gateway synchronization | `3 passed` |
| Console focused | `5 passed` |
| Console lint | `339 files` 通过 |
| PRD-108 Python Ruff | 候选 Python 文件 `All checks passed` |
| Python compileall | PRD-108 涉及源码树通过 |
| `git diff --check` | 通过 |
| 新增内容敏感字面量扫描 | 无命中 |
| PRD-095 navigation/session 回归 | `1 passed` |
| PRD-106 platform jobs 回归 | `11 passed` |
| PRD-107 feature rollout 回归 | `6 passed` |

批次级数据库、真实浏览器、性能与安全回归按约定在 PRD-113 完成后统一执行。

## 验证结论

- 数据库 head 是唯一分发真值；snapshot/delta/head/outbox 同事务提交。
- Gateway owner 由服务身份、活动 lease、Cell 和 workspace placement 共同确定。
- full/delta 使用同一最终 manifest hash；ETag 只对当前 head 返回 304。
- ACK body 不能声明 owner；迟到 ACK 不降低 applied、LKG 或撤权水位。
- 完整性失败立即关闭并删除 LKG；只有可证明的传输不可达使用有界 LKG。
- 重启从签名 full wire 恢复原始成功时间，不会延长安全窗口。
- Console 只展示聚合水位和结果摘要，不展示 manifest、Secret 引用、签名或内部地址。
