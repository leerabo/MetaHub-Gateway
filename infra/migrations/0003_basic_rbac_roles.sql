-- 迁移说明：0003_basic_rbac_roles 属于 AI Gateway MVP 数据库演进，所有结构变更都要保持租户隔离、状态可追踪和回滚可读。


-- 创建 roles 表：保存该模块的核心业务数据，字段设计必须保持租户隔离和状态可追踪。
CREATE TABLE IF NOT EXISTS roles (
  role_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL CHECK (code IN ('admin', 'developer', 'viewer')),
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  UNIQUE (tenant_id, code)
);

-- 创建 idx_roles_tenant_id 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_roles_tenant_id ON roles(tenant_id);
