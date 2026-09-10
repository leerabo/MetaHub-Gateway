# PRD-094 用量身份增量敏感审计

- `consumer_id` 由 tenant + KEY 的不可变关联恢复，不信任客户端姓名、邮箱或传入 tenant。历史记录不按当前 active 状态过滤。
- 使用者姓名/部门只有现有目录授权主体可取得；finance_admin 单独持有用量读权限时只取得稳定 ID。响应释放前再次检查授权，不通过前端隐藏代替后端拒绝。
- 人员目录标签是当前值，不声明历史部门快照。导出继续固定 CSV 列，不复制员工姓名/部门或为员工建立登录身份。
- 已人工复核服务、DTO、过滤及授权测试。共享高置信字节扫描见 `.artifacts/g6-security-20260907/workspace-usage-worktree-audit-20260909.json`，是工作树扫描而非最终暂存审计。

2026-09-10 共享用量消费方代码在 PRD-099 `043f24e`，其精确暂存源文件扫描见 `.artifacts/s2-prd099-index-audit-20260910.json`；本 PRD 独立记录审计见 `.artifacts/s2-prd094-index-audit-20260910.json`。本记录不覆盖未来 SSO 绑定或真实模型调用。
