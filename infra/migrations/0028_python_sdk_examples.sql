-- 迁移说明：0028_python_sdk_examples 扩展开发者门户事件类型，支持审计成功的 Python 示例下载。

-- 清理上次异常中断可能留下的新表；正式旧表仍是唯一可信数据源，不能在此删除或改写。
DROP TABLE IF EXISTS developer_portal_events_prd038_new;

-- SQLite 不能原地修改 CHECK，因此建立同结构新表，并只增加 sdk.example_downloaded 事件类型。
CREATE TABLE developer_portal_events_prd038_new (
  event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  session_id TEXT,
  event_type TEXT NOT NULL CHECK (
    event_type IN ('developer.quickstart_viewed', 'sdk.example_downloaded')
  ),
  object_id TEXT NOT NULL,
  path TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (user_id) REFERENCES users(user_id),
  FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

-- 复制全部旧 quickstart 事件，保证升级不会丢失已有开发者门户访问审计。
INSERT INTO developer_portal_events_prd038_new(
  event_id, tenant_id, user_id, session_id, event_type, object_id,
  path, request_id, trace_id, payload_json, created_at
)
SELECT
  event_id, tenant_id, user_id, session_id, event_type, object_id,
  path, request_id, trace_id, payload_json, created_at
FROM developer_portal_events;

-- 新表已完整接收旧数据后删除旧表，避免应用在同名表之间读到不一致约束。
DROP TABLE developer_portal_events;

-- 恢复稳定业务表名，应用层无需感知 SQLite 的 CHECK 重建过程。
ALTER TABLE developer_portal_events_prd038_new RENAME TO developer_portal_events;

-- 恢复按租户和时间查询的审计索引，所有事件检索必须先限定 tenant_id。
CREATE INDEX IF NOT EXISTS idx_developer_portal_events_tenant_time
  ON developer_portal_events(tenant_id, created_at DESC);

-- 恢复按租户用户查询的索引，支撑定位具体开发者的下载行为。
CREATE INDEX IF NOT EXISTS idx_developer_portal_events_user_time
  ON developer_portal_events(tenant_id, user_id, created_at DESC);

-- 恢复按事件类型查询的索引，支撑区分 quickstart 浏览与 SDK 下载统计。
CREATE INDEX IF NOT EXISTS idx_developer_portal_events_type_time
  ON developer_portal_events(tenant_id, event_type, created_at DESC);
