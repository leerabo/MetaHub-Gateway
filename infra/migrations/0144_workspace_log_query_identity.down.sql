-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE log_identity_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO log_identity_guard SELECT CASE WHEN (
  SELECT version FROM schema_migrations WHERE status='applied' ORDER BY version DESC LIMIT 1
)='0144' THEN 0 ELSE 1 END;
INSERT INTO log_identity_guard
SELECT count(*) FROM sqlite_schema schema JOIN pragma_foreign_key_list(schema.name) fk
WHERE schema.type='table' AND fk."table"='request_log_query_events';
INSERT INTO log_identity_guard
WITH expected(name,sha256) AS (VALUES ('request_log_query_events','41c3e32497066ca867a38d7f39cbda18e045018e639f50038d7e14f48466352a'),
('idx_request_log_query_events_actor_time_prd055','65cf42d522b473001494d40bcdf8ad1febbeba2482375ec612c49d51e442c497'),
('idx_request_log_query_events_pending_prd055','f4b15bda82de9963e6a5b33f3ce442b9050be54c0eec94503d48f6211750ebec'),
('trg_request_log_query_events_owner_insert_prd055','c18f4b3467a98244baaf4babaa41507b4144e38e176a16cf7bce993f6a30be53'),
('trg_request_log_query_events_state_insert_prd055','97b70ea959bd3bfbdfe603c1b41133519b6c36f3a3f6ee2305ac556a1d4c344c'),
('trg_request_log_query_events_conflict_insert_prd055','58a0228bfce5e304e278e2213bfdcf5b81703fc2b76301e755adc82f12736d36'),
('trg_request_log_query_events_rowid_insert_prd055','ba81202fd8aaf46ac61d374d26394412adbbb3e3d06ef6dc3d3a0a9031fe7a52'),
('trg_request_log_query_events_state_update_prd055','acb3cd07fca524516b8c47a1a97423e640395c6c5d1dad1639cb04e24ad6500d'),
('trg_request_log_query_events_immutable_delete_prd055','baf523eee7795c7c79a3db624440d43f0a513bed28996adca5024c79de6c9b82'),
('trg_request_log_query_events_keys_insert_prd055','23cd018aef5dfab0eae322ec85aef939f42876e248beddf1bb9ba22fda29364f'),
('trg_request_log_queries_product_event_insert_prd085','3ff88d20141a3684b2ce5a5138996a086fa467912bb790a16c6036abcce62ad5'))
SELECT count(*) FROM expected LEFT JOIN sqlite_schema actual USING(name)
WHERE actual.sql IS NULL OR metahub_sha256(actual.sql)<>expected.sha256;
INSERT INTO log_identity_guard SELECT CASE WHEN count(*)=11 THEN 0 ELSE 1 END
FROM sqlite_schema WHERE tbl_name='request_log_query_events' AND sql IS NOT NULL;
INSERT INTO log_identity_guard SELECT count(*) FROM tenant_deletion_trigger_manifest manifest
LEFT JOIN sqlite_schema actual ON actual.name=manifest.trigger_name
WHERE manifest.source_table='request_log_query_events' AND (actual.sql IS NULL OR actual.sql<>manifest.trigger_sql
  OR metahub_sha256(actual.sql)<>manifest.trigger_sha256);
INSERT INTO log_identity_guard SELECT CASE WHEN count(*)=1 THEN 0 ELSE 1 END
FROM tenant_deletion_table_manifest WHERE source_table='request_log_query_events';
INSERT INTO log_identity_guard SELECT CASE WHEN count(*)=8 THEN 0 ELSE 1 END
FROM tenant_deletion_trigger_manifest WHERE source_table='request_log_query_events';
INSERT INTO log_identity_guard SELECT count(*) FROM request_log_query_events WHERE workspace_session_id IS NOT NULL;

CREATE TEMP TABLE log_identity_history AS SELECT rowid,query_event_id,event_id,tenant_id,actor_user_id,actor_session_id,filter_version,filter_fingerprint,filter_keys_json,saved_filter_id,sort_by,sort_direction,query_limit,query_offset,matched_count,returned_count,has_more,query_duration_ms,operation_request_id,operation_trace_id,event_dispatch_status,event_dispatch_attempts,event_next_attempt_at,event_dispatched_at,event_claim_token,event_lease_owner_id,event_lease_expires_at,event_last_error_code,executed_at,created_at,updated_at FROM request_log_query_events;
DROP TABLE request_log_query_events;
CREATE TABLE request_log_query_events (
  query_event_id TEXT PRIMARY KEY CHECK (
    length(query_event_id)=36 AND substr(query_event_id,1,4)='lqe_'
    AND substr(query_event_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  event_id TEXT NOT NULL UNIQUE CHECK (
    length(event_id)=38 AND substr(event_id,1,6)='lqevt_'
    AND substr(event_id,7) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  actor_session_id TEXT NOT NULL,
  filter_version INTEGER NOT NULL CHECK (filter_version=1),
  filter_fingerprint TEXT NOT NULL CHECK (
    length(filter_fingerprint)=64 AND filter_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  filter_keys_json TEXT NOT NULL CHECK (
    json_valid(filter_keys_json) AND json_type(filter_keys_json)='array'
  ),
  saved_filter_id TEXT,
  sort_by TEXT NOT NULL CHECK (sort_by IN ('created_at','latency_ms','total_tokens','estimated_cost_amount')),
  sort_direction TEXT NOT NULL CHECK (sort_direction IN ('asc','desc')),
  query_limit INTEGER NOT NULL CHECK (typeof(query_limit)='integer' AND query_limit BETWEEN 1 AND 100),
  query_offset INTEGER NOT NULL CHECK (typeof(query_offset)='integer' AND query_offset BETWEEN 0 AND 10000),
  matched_count INTEGER NOT NULL CHECK (typeof(matched_count)='integer' AND matched_count>=0),
  returned_count INTEGER NOT NULL CHECK (typeof(returned_count)='integer' AND returned_count BETWEEN 0 AND 100),
  has_more INTEGER NOT NULL CHECK (has_more IN (0,1)),
  query_duration_ms INTEGER NOT NULL CHECK (typeof(query_duration_ms)='integer' AND query_duration_ms>=0),
  operation_request_id TEXT NOT NULL CHECK (
    length(operation_request_id) BETWEEN 1 AND 128 AND operation_request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  operation_trace_id TEXT NOT NULL CHECK (
    length(operation_trace_id) BETWEEN 1 AND 128 AND operation_trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  event_dispatch_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (event_dispatch_status IN ('pending','processing','dispatched','failed')),
  event_dispatch_attempts INTEGER NOT NULL DEFAULT 0
    CHECK (typeof(event_dispatch_attempts)='integer' AND event_dispatch_attempts BETWEEN 0 AND 10),
  event_next_attempt_at TEXT,
  event_dispatched_at TEXT,
  event_claim_token TEXT,
  event_lease_owner_id TEXT,
  event_lease_expires_at TEXT,
  event_last_error_code TEXT CHECK (
    event_last_error_code IS NULL OR event_last_error_code IN (
      'webhook.event_type_invalid','webhook.payload_invalid','webhook.payload_sensitive',
      'webhook.delivery_unavailable','request_log.query_event_unavailable'
    )
  ),
  executed_at TEXT NOT NULL CHECK (unixepoch(executed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK (unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (actor_user_id) REFERENCES users(user_id),
  FOREIGN KEY (actor_session_id) REFERENCES sessions(session_id),
  FOREIGN KEY (saved_filter_id) REFERENCES saved_filters(saved_filter_id),
  CHECK (executed_at=created_at),
  CHECK (returned_count<=matched_count AND returned_count<=query_limit),
  -- has_more 是结果事实而非调用方输入，必须与分页位置和总命中数严格一致。
  CHECK (
    has_more=CASE
      WHEN query_offset+returned_count<matched_count THEN 1
      ELSE 0
    END
  ),
  CHECK (
    (event_dispatch_status='pending' AND event_dispatch_attempts BETWEEN 0 AND 9
      AND event_next_attempt_at IS NOT NULL AND unixepoch(event_next_attempt_at) IS NOT NULL
      AND event_dispatched_at IS NULL AND event_claim_token IS NULL
      AND event_lease_owner_id IS NULL AND event_lease_expires_at IS NULL)
    OR (event_dispatch_status='processing' AND event_dispatch_attempts BETWEEN 1 AND 10
      AND event_next_attempt_at IS NULL AND event_dispatched_at IS NULL
      AND event_claim_token IS NOT NULL AND event_lease_owner_id IS NOT NULL
      AND event_lease_expires_at IS NOT NULL)
    OR (event_dispatch_status='dispatched' AND event_dispatched_at IS NOT NULL
      AND event_next_attempt_at IS NULL AND event_claim_token IS NULL
      AND event_lease_owner_id IS NULL AND event_lease_expires_at IS NULL
      AND event_last_error_code IS NULL)
    OR (event_dispatch_status='failed' AND event_dispatch_attempts=10
      AND event_next_attempt_at IS NULL AND event_dispatched_at IS NULL
      AND event_claim_token IS NULL AND event_lease_owner_id IS NULL
      AND event_lease_expires_at IS NULL AND event_last_error_code IS NOT NULL)
  )
);
INSERT INTO request_log_query_events(rowid,query_event_id,event_id,tenant_id,actor_user_id,actor_session_id,filter_version,filter_fingerprint,filter_keys_json,saved_filter_id,sort_by,sort_direction,query_limit,query_offset,matched_count,returned_count,has_more,query_duration_ms,operation_request_id,operation_trace_id,event_dispatch_status,event_dispatch_attempts,event_next_attempt_at,event_dispatched_at,event_claim_token,event_lease_owner_id,event_lease_expires_at,event_last_error_code,executed_at,created_at,updated_at) SELECT rowid,query_event_id,event_id,tenant_id,actor_user_id,actor_session_id,filter_version,filter_fingerprint,filter_keys_json,saved_filter_id,sort_by,sort_direction,query_limit,query_offset,matched_count,returned_count,has_more,query_duration_ms,operation_request_id,operation_trace_id,event_dispatch_status,event_dispatch_attempts,event_next_attempt_at,event_dispatched_at,event_claim_token,event_lease_owner_id,event_lease_expires_at,event_last_error_code,executed_at,created_at,updated_at FROM log_identity_history;
INSERT INTO log_identity_guard SELECT count(*) FROM (
  SELECT rowid,query_event_id,event_id,tenant_id,actor_user_id,actor_session_id,filter_version,filter_fingerprint,filter_keys_json,saved_filter_id,sort_by,sort_direction,query_limit,query_offset,matched_count,returned_count,has_more,query_duration_ms,operation_request_id,operation_trace_id,event_dispatch_status,event_dispatch_attempts,event_next_attempt_at,event_dispatched_at,event_claim_token,event_lease_owner_id,event_lease_expires_at,event_last_error_code,executed_at,created_at,updated_at FROM request_log_query_events EXCEPT SELECT rowid,query_event_id,event_id,tenant_id,actor_user_id,actor_session_id,filter_version,filter_fingerprint,filter_keys_json,saved_filter_id,sort_by,sort_direction,query_limit,query_offset,matched_count,returned_count,has_more,query_duration_ms,operation_request_id,operation_trace_id,event_dispatch_status,event_dispatch_attempts,event_next_attempt_at,event_dispatched_at,event_claim_token,event_lease_owner_id,event_lease_expires_at,event_last_error_code,executed_at,created_at,updated_at FROM log_identity_history);
INSERT INTO log_identity_guard SELECT count(*) FROM (
  SELECT rowid,query_event_id,event_id,tenant_id,actor_user_id,actor_session_id,filter_version,filter_fingerprint,filter_keys_json,saved_filter_id,sort_by,sort_direction,query_limit,query_offset,matched_count,returned_count,has_more,query_duration_ms,operation_request_id,operation_trace_id,event_dispatch_status,event_dispatch_attempts,event_next_attempt_at,event_dispatched_at,event_claim_token,event_lease_owner_id,event_lease_expires_at,event_last_error_code,executed_at,created_at,updated_at FROM log_identity_history EXCEPT SELECT rowid,query_event_id,event_id,tenant_id,actor_user_id,actor_session_id,filter_version,filter_fingerprint,filter_keys_json,saved_filter_id,sort_by,sort_direction,query_limit,query_offset,matched_count,returned_count,has_more,query_duration_ms,operation_request_id,operation_trace_id,event_dispatch_status,event_dispatch_attempts,event_next_attempt_at,event_dispatched_at,event_claim_token,event_lease_owner_id,event_lease_expires_at,event_last_error_code,executed_at,created_at,updated_at FROM request_log_query_events);
DROP TABLE log_identity_history;
CREATE INDEX idx_request_log_query_events_actor_time_prd055
  ON request_log_query_events(tenant_id, actor_user_id, executed_at DESC, query_event_id DESC);
CREATE INDEX idx_request_log_query_events_pending_prd055
  ON request_log_query_events(event_dispatch_status, event_next_attempt_at, event_lease_expires_at, created_at)
  WHERE event_dispatch_status IN ('pending','processing');
CREATE TRIGGER trg_request_log_query_events_owner_insert_prd055
BEFORE INSERT ON request_log_query_events FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM users u WHERE u.user_id=NEW.actor_user_id AND u.tenant_id=NEW.tenant_id
)
OR NOT EXISTS (
  SELECT 1 FROM sessions s WHERE s.session_id=NEW.actor_session_id
    AND s.user_id=NEW.actor_user_id AND s.tenant_id=NEW.tenant_id
)
OR (NEW.saved_filter_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM saved_filters f WHERE f.saved_filter_id=NEW.saved_filter_id
    AND f.tenant_id=NEW.tenant_id AND f.owner_user_id=NEW.actor_user_id AND f.status='active'
))
BEGIN SELECT RAISE(ABORT,'request_log_query_event_owner_invalid'); END;
CREATE TRIGGER trg_request_log_query_events_state_insert_prd055
BEFORE INSERT ON request_log_query_events FOR EACH ROW
WHEN NOT (
  NEW.event_dispatch_status='pending' AND NEW.event_dispatch_attempts=0
  AND NEW.event_next_attempt_at=NEW.created_at
  AND NEW.event_dispatched_at IS NULL AND NEW.event_claim_token IS NULL
  AND NEW.event_lease_owner_id IS NULL AND NEW.event_lease_expires_at IS NULL
  AND NEW.event_last_error_code IS NULL
  AND NEW.executed_at=NEW.created_at AND NEW.updated_at=NEW.created_at
)
BEGIN SELECT RAISE(ABORT,'request_log_query_event_state_invalid'); END;
CREATE TRIGGER trg_request_log_query_events_conflict_insert_prd055
BEFORE INSERT ON request_log_query_events FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM request_log_query_events current
  WHERE current.query_event_id=NEW.query_event_id OR current.event_id=NEW.event_id
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'request_log_query_event_immutable'); END;
CREATE TRIGGER trg_request_log_query_events_rowid_insert_prd055
AFTER INSERT ON request_log_query_events FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'request_log_query_event_immutable'); END;
CREATE TRIGGER trg_request_log_query_events_state_update_prd055
BEFORE UPDATE ON request_log_query_events FOR EACH ROW
WHEN NOT (
  NEW.rowid=OLD.rowid AND NEW.query_event_id=OLD.query_event_id AND NEW.event_id=OLD.event_id
  AND NEW.tenant_id=OLD.tenant_id AND NEW.actor_user_id=OLD.actor_user_id
  AND NEW.actor_session_id=OLD.actor_session_id AND NEW.filter_version=OLD.filter_version
  AND NEW.filter_fingerprint=OLD.filter_fingerprint AND NEW.filter_keys_json=OLD.filter_keys_json
  AND NEW.saved_filter_id IS OLD.saved_filter_id AND NEW.sort_by=OLD.sort_by
  AND NEW.sort_direction=OLD.sort_direction AND NEW.query_limit=OLD.query_limit
  AND NEW.query_offset=OLD.query_offset AND NEW.matched_count=OLD.matched_count
  AND NEW.returned_count=OLD.returned_count AND NEW.has_more=OLD.has_more
  AND NEW.query_duration_ms=OLD.query_duration_ms
  AND NEW.operation_request_id=OLD.operation_request_id AND NEW.operation_trace_id=OLD.operation_trace_id
  AND NEW.executed_at=OLD.executed_at AND NEW.created_at=OLD.created_at
  AND unixepoch(NEW.updated_at)>unixepoch(OLD.updated_at)
  AND (
    (OLD.event_dispatch_status='pending' AND NEW.event_dispatch_status='processing'
      AND OLD.event_dispatch_attempts BETWEEN 0 AND 9
      AND unixepoch(OLD.event_next_attempt_at)<=unixepoch(NEW.updated_at)
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts+1
      AND NEW.event_next_attempt_at IS NULL AND NEW.event_dispatched_at IS NULL
      AND length(NEW.event_claim_token) BETWEEN 16 AND 128
      AND length(NEW.event_lease_owner_id) BETWEEN 1 AND 128
      AND unixepoch(NEW.event_lease_expires_at)>unixepoch(NEW.updated_at)
      AND NEW.event_last_error_code IS NULL)
    OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='processing'
      AND OLD.event_dispatch_attempts BETWEEN 1 AND 9
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts+1
      AND unixepoch(OLD.event_lease_expires_at)<=unixepoch(NEW.updated_at)
      AND NEW.event_next_attempt_at IS NULL AND NEW.event_dispatched_at IS NULL
      AND length(NEW.event_claim_token) BETWEEN 16 AND 128
      AND length(NEW.event_lease_owner_id) BETWEEN 1 AND 128
      AND unixepoch(NEW.event_lease_expires_at)>unixepoch(NEW.updated_at)
      AND NEW.event_last_error_code IS NULL)
    OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='dispatched'
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts
      AND unixepoch(OLD.event_lease_expires_at)>unixepoch(NEW.updated_at)
      AND NEW.event_dispatched_at=NEW.updated_at AND NEW.event_next_attempt_at IS NULL
      AND NEW.event_claim_token IS NULL AND NEW.event_lease_owner_id IS NULL
      AND NEW.event_lease_expires_at IS NULL AND NEW.event_last_error_code IS NULL)
    OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='pending'
      AND OLD.event_dispatch_attempts BETWEEN 1 AND 9
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts
      AND unixepoch(OLD.event_lease_expires_at)>unixepoch(NEW.updated_at)
      AND unixepoch(NEW.event_next_attempt_at)>unixepoch(NEW.updated_at)
      AND NEW.event_dispatched_at IS NULL AND NEW.event_claim_token IS NULL
      AND NEW.event_lease_owner_id IS NULL AND NEW.event_lease_expires_at IS NULL
      AND NEW.event_last_error_code IS NOT NULL)
    OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='failed'
      AND OLD.event_dispatch_attempts=10 AND NEW.event_dispatch_attempts=10
      AND NEW.event_next_attempt_at IS NULL AND NEW.event_dispatched_at IS NULL
      AND NEW.event_claim_token IS NULL AND NEW.event_lease_owner_id IS NULL
      AND NEW.event_lease_expires_at IS NULL AND NEW.event_last_error_code IS NOT NULL)
  )
)
BEGIN SELECT RAISE(ABORT,'request_log_query_event_immutable'); END;
CREATE TRIGGER trg_request_log_query_events_immutable_delete_prd055
BEFORE DELETE ON request_log_query_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'request_log_query_event_immutable'); END;
CREATE TRIGGER trg_request_log_query_events_keys_insert_prd055
BEFORE INSERT ON request_log_query_events FOR EACH ROW
WHEN NOT (
  json_valid(NEW.filter_keys_json)
  AND CASE WHEN json_valid(NEW.filter_keys_json)
    THEN json_type(NEW.filter_keys_json) END='array'
  AND (SELECT COUNT(*) FROM json_each(
    CASE WHEN json_valid(NEW.filter_keys_json) THEN NEW.filter_keys_json ELSE '[]' END
  ))<=28
  AND NOT EXISTS (
    SELECT 1 FROM json_each(
      CASE WHEN json_valid(NEW.filter_keys_json) THEN NEW.filter_keys_json ELSE '[]' END
    ) item
    WHERE item.type!='text' OR item.value NOT IN (
      'request_id','trace_id','status','model','request_model','resolved_model',
      'provider_code','virtual_key_id','environment_id','project_id','application_id',
      'snapshot_version','error_code','created_from','created_to','latency_ms_min',
      'latency_ms_max','total_tokens_min','total_tokens_max','cost_amount_min',
      'cost_amount_max','currency','terminal_stage','output_safety_status',
      'output_safety_mode','output_safety_exposure','output_safety_billing'
    )
  )
  AND NOT EXISTS (
    SELECT 1
    FROM json_each(
      CASE WHEN json_valid(NEW.filter_keys_json) THEN NEW.filter_keys_json ELSE '[]' END
    ) earlier
    JOIN json_each(
      CASE WHEN json_valid(NEW.filter_keys_json) THEN NEW.filter_keys_json ELSE '[]' END
    ) later ON CAST(earlier.key AS INTEGER)<CAST(later.key AS INTEGER)
    WHERE CAST(earlier.value AS TEXT)>=CAST(later.value AS TEXT)
  )
)
BEGIN SELECT RAISE(ABORT,'request_log_query_event_keys_invalid'); END;
CREATE TRIGGER trg_request_log_queries_product_event_insert_prd085
AFTER INSERT ON request_log_query_events FOR EACH ROW
BEGIN
  -- 每次权威查询审计记录生成一条租户内可追踪的日志查看事实。
  INSERT INTO product_events(
    product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id,
    operation_request_id,operation_trace_id,capture_mode,status,
    occurred_at,created_at,updated_at)
  SELECT
    'pev_'||substr(metahub_sha256(json_array(
      NEW.tenant_id,'request_logs.viewed','request_log_query',NEW.query_event_id)),1,32),
    NEW.tenant_id,'request_logs.viewed',NEW.actor_user_id,
    'request_log_query',NEW.query_event_id,
    NEW.operation_request_id,NEW.operation_trace_id,'live','recorded',NEW.executed_at,
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  WHERE NOT EXISTS(SELECT 1 FROM product_events current
    WHERE current.tenant_id=NEW.tenant_id AND current.source_kind='request_log_query'
      AND current.source_id=NEW.query_event_id
      AND current.event_type='request_logs.viewed');
END;
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
UPDATE tenant_deletion_table_manifest SET
  table_sql_sha256=metahub_sha256((SELECT sql FROM sqlite_schema WHERE name='request_log_query_events')),
  column_signature_sha256=metahub_sha256((SELECT group_concat(record,char(30)) FROM(
    SELECT cid||char(31)||name||char(31)||type||char(31)||"notnull"||char(31)
      ||coalesce(dflt_value,'')||char(31)||pk AS record
    FROM pragma_table_info('request_log_query_events') ORDER BY cid))),
  trigger_set_sha256=metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
    SELECT name||char(31)||metahub_sha256(sql) AS record FROM sqlite_schema
    WHERE type='trigger' AND tbl_name='request_log_query_events' ORDER BY name)),''))
WHERE source_table='request_log_query_events';
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
UPDATE tenant_deletion_trigger_manifest SET
  trigger_sql=(SELECT sql FROM sqlite_schema WHERE name=trigger_name),
  trigger_sha256=metahub_sha256((SELECT sql FROM sqlite_schema WHERE name=trigger_name))
WHERE source_table='request_log_query_events';
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
INSERT INTO log_identity_guard SELECT count(*) FROM pragma_foreign_key_check;
DROP TABLE log_identity_guard;
COMMIT;
PRAGMA foreign_keys = ON;
