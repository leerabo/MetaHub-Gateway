-- PRD-061 回滚：移除状态聚合索引和维护备注，并把 status.viewed 精确降级为通用页面访问事件。
-- 维护备注属于用户数据；存在非空值、后续迁移或未知依赖时必须在首项持久 DDL 前失败关闭。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- 临时回滚守卫先检查业务备注和未知依赖，不触碰任何持久对象。
DROP TABLE IF EXISTS temp.prd061_down_guard;
CREATE TEMP TABLE prd061_down_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count = 0)
);
-- 汇总栈顶、字段、依赖和非空业务事实；非零值由 CHECK 原子阻断降级。
INSERT INTO prd061_down_guard(issue_count)
SELECT
  (SELECT CASE WHEN COUNT(*) = 1 THEN 0 ELSE 1 END
   FROM schema_migrations WHERE version = '0053' AND status = 'applied')
  + (SELECT COUNT(*) FROM schema_migrations WHERE status = 'applied' AND version > '0053')
  + (SELECT CASE WHEN COUNT(*) = 1 THEN 0 ELSE 1 END
     FROM pragma_table_info('providers') WHERE name = 'maintenance_note')
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('providers')) = 29 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('console_page_events')) = 12 THEN 0 ELSE 1 END
  -- 回滚同样会重建事件表；三个冻结索引必须名称、属性和列方向完全一致，且不能存在未知依赖。
  + CASE WHEN (
      SELECT COUNT(*) FROM pragma_index_list('console_page_events')
      WHERE origin='c' AND "unique"=0 AND partial=0
        AND name IN (
          'idx_console_page_events_tenant_time',
          'idx_console_page_events_user_time',
          'idx_console_page_events_item_time'
        )
    )=3 AND (
      SELECT COUNT(*) FROM pragma_index_list('console_page_events') WHERE origin='c'
    )=3 THEN 0 ELSE 1 END
  + CASE WHEN (
      SELECT group_concat(name || ':' || "desc", ',') FROM (
        SELECT name, "desc" FROM pragma_index_xinfo('idx_console_page_events_tenant_time')
        WHERE "key"=1 ORDER BY seqno
      )
    )='tenant_id:0,created_at:1' THEN 0 ELSE 1 END
  + CASE WHEN (
      SELECT group_concat(name || ':' || "desc", ',') FROM (
        SELECT name, "desc" FROM pragma_index_xinfo('idx_console_page_events_user_time')
        WHERE "key"=1 ORDER BY seqno
      )
    )='tenant_id:0,user_id:0,created_at:1' THEN 0 ELSE 1 END
  + CASE WHEN (
      SELECT group_concat(name || ':' || "desc", ',') FROM (
        SELECT name, "desc" FROM pragma_index_xinfo('idx_console_page_events_item_time')
        WHERE "key"=1 ORDER BY seqno
      )
    )='tenant_id:0,navigation_item_id:0,created_at:1' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema
     WHERE type='trigger' AND tbl_name='console_page_events')
  + (SELECT COUNT(*) FROM sqlite_schema
     WHERE type='view' AND sql IS NOT NULL
       AND instr(lower(sql), 'console_page_events')>0)
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name='console_page_events_prd061_old')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok')
  + (SELECT CASE WHEN COUNT(*) = 4 THEN 0 ELSE 1 END
     FROM sqlite_schema
     WHERE type='index' AND name IN (
       'idx_routing_attempts_tenant_provider_completed',
       'idx_stream_sessions_tenant_provider_completed',
       'idx_provider_health_logs_tenant_provider_time_request',
       'idx_provider_health_logs_tenant_provider_request_sequence'
     ))
  + (SELECT CASE WHEN COUNT(*) = 1 THEN 0 ELSE 1 END
     FROM sqlite_schema
     WHERE type='table' AND name='console_page_events'
       AND instr(lower(sql),'status.viewed') > 0)
  + (SELECT COUNT(*)
     FROM sqlite_schema
     WHERE sql IS NOT NULL
       AND instr(lower(sql), 'maintenance_note') > 0
       AND NOT (type = 'table' AND name = 'providers'));
-- 缺列会在此只读检查处中止；存在任何业务备注时也不能继续执行破坏性回滚。
INSERT INTO prd061_down_guard(issue_count)
SELECT COUNT(*) FROM providers WHERE maintenance_note IS NOT NULL;
DROP TABLE prd061_down_guard;

-- 前置门禁已经证明主 schema 中没有同名对象，禁止为了方便而删除未知持久对象。
CREATE TABLE console_page_events_prd061_old (
  event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  session_id TEXT,
  event_type TEXT NOT NULL DEFAULT 'console.page_viewed' CHECK (
    event_type IN (
      'console.page_viewed',
      'dashboard.viewed',
      'migration.guide_viewed',
      'help.competitor_viewed'
    )
  ),
  navigation_item_id TEXT NOT NULL,
  target_section TEXT NOT NULL,
  path TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (user_id) REFERENCES users(user_id),
  FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
-- 除 status.viewed 精确映射外复制全部原列和 rowid，保留访问证据而不删除事件。
INSERT INTO console_page_events_prd061_old(
  rowid, event_id, tenant_id, user_id, session_id, event_type, navigation_item_id,
  target_section, path, request_id, trace_id, payload_json, created_at
)
SELECT
  rowid, event_id, tenant_id, user_id, session_id,
  CASE WHEN event_type = 'status.viewed' THEN 'console.page_viewed' ELSE event_type END,
  navigation_item_id, target_section, path, request_id, trace_id, payload_json, created_at
FROM console_page_events;

-- 降级只允许 event_type 发生预期映射；其余列和 rowid 必须逐行保持不变。
DROP TABLE IF EXISTS temp.prd061_event_down_guard;
CREATE TEMP TABLE prd061_event_down_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count = 0)
);
-- 逐列比较源表和降级副本，仅允许冻结的 event_type 映射发生变化。
INSERT INTO prd061_event_down_guard(issue_count)
SELECT
  ABS(
    (SELECT COUNT(*) FROM console_page_events)
    - (SELECT COUNT(*) FROM console_page_events_prd061_old)
  )
  + (SELECT COUNT(*)
     FROM console_page_events source
     LEFT JOIN console_page_events_prd061_old target ON target.rowid = source.rowid
     WHERE target.rowid IS NULL
        OR target.event_id IS NOT source.event_id
        OR target.tenant_id IS NOT source.tenant_id
        OR target.user_id IS NOT source.user_id
        OR target.session_id IS NOT source.session_id
        OR target.event_type IS NOT CASE
             WHEN source.event_type = 'status.viewed' THEN 'console.page_viewed'
             ELSE source.event_type END
        OR target.navigation_item_id IS NOT source.navigation_item_id
        OR target.target_section IS NOT source.target_section
        OR target.path IS NOT source.path
        OR target.request_id IS NOT source.request_id
        OR target.trace_id IS NOT source.trace_id
        OR target.payload_json IS NOT source.payload_json
        OR target.created_at IS NOT source.created_at);
DROP TABLE prd061_event_down_guard;

DROP TABLE console_page_events;
-- 复制保真验证通过后才恢复旧事件表名称，避免中途失败破坏可查询性。
ALTER TABLE console_page_events_prd061_old RENAME TO console_page_events;
CREATE INDEX idx_console_page_events_tenant_time
  ON console_page_events(tenant_id, created_at DESC);
CREATE INDEX idx_console_page_events_user_time
  ON console_page_events(tenant_id, user_id, created_at DESC);
CREATE INDEX idx_console_page_events_item_time
  ON console_page_events(tenant_id, navigation_item_id, created_at DESC);

DROP INDEX idx_routing_attempts_tenant_provider_completed;
DROP INDEX idx_stream_sessions_tenant_provider_completed;
DROP INDEX idx_provider_health_logs_tenant_provider_time_request;
DROP INDEX idx_provider_health_logs_tenant_provider_request_sequence;
-- 四个查询索引移除且备注已证明全空后，才允许删除用户数据列。
ALTER TABLE providers DROP COLUMN maintenance_note;

-- 提交前证明 0052 结构、索引移除、事件枚举和数据库完整性均已恢复。
DROP TABLE IF EXISTS temp.prd061_down_final_guard;
CREATE TEMP TABLE prd061_down_final_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count = 0)
);
-- 提交前复核 0052 列数、索引移除、事件约束和数据库完整性，异常整体回滚。
INSERT INTO prd061_down_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('providers'))=28 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_table_info('providers') WHERE name='maintenance_note')
  + (SELECT COUNT(*) FROM sqlite_schema
     WHERE type='index' AND name IN (
       'idx_routing_attempts_tenant_provider_completed',
       'idx_stream_sessions_tenant_provider_completed',
       'idx_provider_health_logs_tenant_provider_time_request',
       'idx_provider_health_logs_tenant_provider_request_sequence'
     ))
  + CASE WHEN instr((
      SELECT lower(sql) FROM sqlite_schema
      WHERE type='table' AND name='console_page_events'
    ), 'status.viewed')=0 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd061_down_final_guard;

COMMIT;
PRAGMA foreign_keys = ON;
