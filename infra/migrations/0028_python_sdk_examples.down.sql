-- 回滚说明：恢复 PRD-019 的单事件 CHECK；新增 sdk.example_downloaded 行因旧约束不兼容会被明确删除。

-- 清理上次异常中断可能留下的旧结构临时表，避免 CREATE TABLE 被残留对象阻断。
DROP TABLE IF EXISTS developer_portal_events_prd038_old;

-- 重建 PRD-019 原始结构，event_type 重新只接受 developer.quickstart_viewed。
CREATE TABLE developer_portal_events_prd038_old (
  event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  session_id TEXT,
  event_type TEXT NOT NULL CHECK (event_type IN ('developer.quickstart_viewed')),
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

-- 只保留旧 quickstart 历史；SDK 下载事件无法满足旧 CHECK，回滚时按设计发生可审计的数据丢失。
INSERT INTO developer_portal_events_prd038_old(
  event_id, tenant_id, user_id, session_id, event_type, object_id,
  path, request_id, trace_id, payload_json, created_at
)
SELECT
  event_id, tenant_id, user_id, session_id, event_type, object_id,
  path, request_id, trace_id, payload_json, created_at
FROM developer_portal_events
WHERE event_type='developer.quickstart_viewed';

-- 旧约束数据已复制完成，删除 PRD-038 扩展结构及其中的新事件类型数据。
DROP TABLE developer_portal_events;

-- 恢复应用层使用的稳定表名，使 0027 及更早代码继续访问原始事件表。
ALTER TABLE developer_portal_events_prd038_old RENAME TO developer_portal_events;

-- 重建 PRD-019 的租户时间索引，回滚后查询仍必须携带 tenant_id。
CREATE INDEX IF NOT EXISTS idx_developer_portal_events_tenant_time
  ON developer_portal_events(tenant_id, created_at DESC);

-- 重建 PRD-019 的租户用户索引，保留原有访问轨迹排查能力。
CREATE INDEX IF NOT EXISTS idx_developer_portal_events_user_time
  ON developer_portal_events(tenant_id, user_id, created_at DESC);

-- 重建 PRD-019 的事件类型索引，此时索引只包含 quickstart 事件。
CREATE INDEX IF NOT EXISTS idx_developer_portal_events_type_time
  ON developer_portal_events(tenant_id, event_type, created_at DESC);
