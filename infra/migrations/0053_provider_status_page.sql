-- PRD-061 内部 Provider 状态页：维护备注、低频状态聚合索引和访问事件枚举。
-- 安全边界：迁移不复制 Provider 描述、凭据或调用正文；事件表重建必须保留全部既有审计事实。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- 临时守卫不写入主 schema；所有持久 DDL 前先证明迁移栈和依赖结构正好位于 0052。
DROP TABLE IF EXISTS temp.prd061_up_guard;
CREATE TEMP TABLE prd061_up_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count = 0)
);
-- 把版本栈、依赖表和未知列问题合并为单一零值守卫，任一异常都在持久 DDL 前终止。
INSERT INTO prd061_up_guard(issue_count)
SELECT
  (SELECT CASE WHEN COUNT(*) = 1 THEN 0 ELSE 1 END
   FROM schema_migrations WHERE version = '0052' AND status = 'applied')
  + (SELECT COUNT(*) FROM schema_migrations WHERE status = 'applied' AND version > '0052')
  + (SELECT CASE WHEN COUNT(*) = 5 THEN 0 ELSE 1 END
     FROM sqlite_schema
     WHERE type = 'table'
       AND name IN (
         'providers', 'routing_attempts', 'stream_sessions',
         'provider_health_probe_logs', 'console_page_events'
       ))
  + (SELECT CASE WHEN COUNT(*) = 0 THEN 0 ELSE 1 END
     FROM pragma_table_info('providers') WHERE name = 'maintenance_note')
  -- 0052 栈顶的五张依赖表列数必须精确匹配；额外未知列也可能在重建/回滚时丢失，必须先阻断。
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('providers')) = 28 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('routing_attempts')) = 27 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('stream_sessions')) = 25 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('provider_health_probe_logs')) = 17 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('console_page_events')) = 12 THEN 0 ELSE 1 END
  -- 事件表会被重建；只允许 0030 冻结的三个普通索引，任何额外索引、触发器或依赖视图都必须先人工处理。
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
  -- 重建目标名属于主 schema；若被未知对象占用，绝不能用 DROP IF EXISTS 静默删除。
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name='console_page_events_prd061_new')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok')
  -- 聚合和回滚直接依赖的关键列逐项验证，不能只凭表名与总列数猜测兼容。
  + (SELECT CASE WHEN COUNT(*) = 8 THEN 0 ELSE 1 END
     FROM pragma_table_info('routing_attempts')
     WHERE name IN (
       'routing_attempt_id','tenant_id','provider_id','request_id','trace_id',
       'status','latency_ms','completed_at'
     ))
  + (SELECT CASE WHEN COUNT(*) = 9 THEN 0 ELSE 1 END
     FROM pragma_table_info('stream_sessions')
     WHERE name IN (
       'stream_session_id','tenant_id','provider_id','request_id','trace_id',
       'status','started_at','first_chunk_at','completed_at'
     ))
  + (SELECT CASE WHEN COUNT(*) = 10 THEN 0 ELSE 1 END
     FROM pragma_table_info('provider_health_probe_logs')
     WHERE name IN (
       'probe_log_id','tenant_id','provider_id','request_id','trace_id',
       'attempt_sequence','result_status','latency_ms','error_code','checked_at'
     ))
  + (SELECT CASE WHEN COUNT(*) = 12 THEN 0 ELSE 1 END
     FROM pragma_table_info('console_page_events')
     WHERE name IN (
       'event_id', 'tenant_id', 'user_id', 'session_id', 'event_type',
       'navigation_item_id', 'target_section', 'path', 'request_id', 'trace_id',
       'payload_json', 'created_at'
     ));
DROP TABLE prd061_up_guard;

-- 维护备注是明确允许状态页读者查看的新字段，绝不从语义不同的 description 自动回填。
-- 应用层还会执行 Unicode strip 与同一长度校验；数据库约束负责阻止旁路写入空白和 C0/C1 控制字符。
ALTER TABLE providers ADD COLUMN maintenance_note TEXT NULL CHECK (
  maintenance_note IS NULL
  OR (
    typeof(maintenance_note) = 'text'
    AND length(maintenance_note) BETWEEN 1 AND 240
    AND maintenance_note = trim(maintenance_note)
    AND instr(maintenance_note, char(0)) = 0
    AND maintenance_note NOT GLOB (
      '*[' || char(1) || '-' || char(31) || char(127) || '-' || char(159) || ']*'
    )
  )
);

-- 两个 partial index 只覆盖具有完成时间的终态，支撑租户内 Provider 24 小时聚合与七天故障排序。
CREATE INDEX idx_routing_attempts_tenant_provider_completed
  ON routing_attempts(tenant_id, provider_id, completed_at DESC, routing_attempt_id DESC)
  WHERE status IN ('succeeded', 'failed') AND completed_at IS NOT NULL;
CREATE INDEX idx_stream_sessions_tenant_provider_completed
  ON stream_sessions(tenant_id, provider_id, completed_at DESC, stream_session_id DESC)
  WHERE status IN ('completed', 'timed_out', 'client_disconnected', 'upstream_failed')
    AND completed_at IS NOT NULL;

-- 时间索引先找窗口候选 request；request 索引再回取同一探测的全部 Key attempt，避免跨边界拆组。
CREATE INDEX idx_provider_health_logs_tenant_provider_time_request
  ON provider_health_probe_logs(
    tenant_id, provider_id, checked_at DESC, request_id, attempt_sequence, probe_log_id
  );
CREATE INDEX idx_provider_health_logs_tenant_provider_request_sequence
  ON provider_health_probe_logs(
    tenant_id, provider_id, request_id, attempt_sequence, checked_at, probe_log_id
  );

-- 只扩展事件枚举；显式复制 rowid 和全部十二列，避免重建改变审计记录的稳定内部顺序。
-- 上方门禁已经证明该持久名称未被占用，因此这里不允许先删后建。
CREATE TABLE console_page_events_prd061_new (
  event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  session_id TEXT,
  event_type TEXT NOT NULL DEFAULT 'console.page_viewed' CHECK (
    event_type IN (
      'console.page_viewed',
      'dashboard.viewed',
      'migration.guide_viewed',
      'help.competitor_viewed',
      'status.viewed'
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
-- 显式复制 rowid 和十二列，确保扩展事件枚举不会重排或改写既有访问审计事实。
INSERT INTO console_page_events_prd061_new(
  rowid, event_id, tenant_id, user_id, session_id, event_type, navigation_item_id,
  target_section, path, request_id, trace_id, payload_json, created_at
)
SELECT
  rowid, event_id, tenant_id, user_id, session_id, event_type, navigation_item_id,
  target_section, path, request_id, trace_id, payload_json, created_at
FROM console_page_events;

-- 双向差集、行数和 rowid 同时通过后才允许替换原表；任一漂移由 CHECK 中止整个事务。
DROP TABLE IF EXISTS temp.prd061_event_copy_guard;
CREATE TEMP TABLE prd061_event_copy_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count = 0)
);
-- 双向差集和行数必须同时为零，防止仅凭主键数量相同掩盖字段漂移。
INSERT INTO prd061_event_copy_guard(issue_count)
SELECT
  ABS(
    (SELECT COUNT(*) FROM console_page_events)
    - (SELECT COUNT(*) FROM console_page_events_prd061_new)
  )
  + (SELECT COUNT(*) FROM (
      SELECT rowid, event_id, tenant_id, user_id, session_id, event_type,
             navigation_item_id, target_section, path, request_id, trace_id,
             payload_json, created_at
      FROM console_page_events
      EXCEPT
      SELECT rowid, event_id, tenant_id, user_id, session_id, event_type,
             navigation_item_id, target_section, path, request_id, trace_id,
             payload_json, created_at
      FROM console_page_events_prd061_new
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid, event_id, tenant_id, user_id, session_id, event_type,
             navigation_item_id, target_section, path, request_id, trace_id,
             payload_json, created_at
      FROM console_page_events_prd061_new
      EXCEPT
      SELECT rowid, event_id, tenant_id, user_id, session_id, event_type,
             navigation_item_id, target_section, path, request_id, trace_id,
             payload_json, created_at
      FROM console_page_events
    ));
DROP TABLE prd061_event_copy_guard;

DROP TABLE console_page_events;
-- 仅在复制守卫通过后替换正式事件表，避免失败路径留下半成品结构。
ALTER TABLE console_page_events_prd061_new RENAME TO console_page_events;
CREATE INDEX idx_console_page_events_tenant_time
  ON console_page_events(tenant_id, created_at DESC);
CREATE INDEX idx_console_page_events_user_time
  ON console_page_events(tenant_id, user_id, created_at DESC);
CREATE INDEX idx_console_page_events_item_time
  ON console_page_events(tenant_id, navigation_item_id, created_at DESC);

-- 在事务提交前再次验证最终结构和数据库完整性；测试后的外部检查不能替代这里的原子门禁。
DROP TABLE IF EXISTS temp.prd061_up_final_guard;
CREATE TEMP TABLE prd061_up_final_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count = 0)
);
-- 提交前再次核对列、索引、事件枚举和数据库完整性，任何漂移都回滚整个 0053。
INSERT INTO prd061_up_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('providers') WHERE name='maintenance_note')=1
    THEN 0 ELSE 1 END
  + CASE WHEN (
      SELECT COUNT(*) FROM sqlite_schema
      WHERE type='index' AND name IN (
        'idx_routing_attempts_tenant_provider_completed',
        'idx_stream_sessions_tenant_provider_completed',
        'idx_provider_health_logs_tenant_provider_time_request',
        'idx_provider_health_logs_tenant_provider_request_sequence',
        'idx_console_page_events_tenant_time',
        'idx_console_page_events_user_time',
        'idx_console_page_events_item_time'
      )
    )=7 THEN 0 ELSE 1 END
  + CASE WHEN instr((
      SELECT lower(sql) FROM sqlite_schema
      WHERE type='table' AND name='console_page_events'
    ), 'status.viewed')>0 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd061_up_final_guard;

COMMIT;
PRAGMA foreign_keys = ON;
