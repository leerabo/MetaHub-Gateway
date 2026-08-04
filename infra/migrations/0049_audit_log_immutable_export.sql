-- 迁移职责：为 PRD-058 建立稳定审计快照序列、不可变 AuditExport 与可恢复 source outbox，
-- 并在保留全部历史 rowid/NULL/字段的前提下扩展 audit.exported 动作。
-- 安全边界：导出事实只保存筛选指纹、规模与哈希，不保存 CSV、Prompt、Response、Token、
-- Provider Key、Authorization、Cookie、DLP pattern 或任意原始业务请求/响应正文。
BEGIN IMMEDIATE;

-- 0049 必须从完整 0048 栈顶执行；审计表存在未知外部依赖时不能冒险重建。
CREATE TEMP TABLE prd058_up_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd058_up_guard
BEFORE INSERT ON prd058_up_guard FOR EACH ROW WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd058_prerequisite_failed'); END;
-- 迁移记录、审计表和未知外部依赖必须同时满足，任何不完整前置状态都原子中止。
INSERT INTO prd058_up_guard(invalid_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
    WHERE version='0048' AND status='applied')=1 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
    WHERE type='table' AND name='audit_logs')=1 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema object
      WHERE object.sql IS NOT NULL
        AND (
          (object.type='view' AND lower(object.sql) LIKE '%audit_logs%')
          OR (object.type='trigger' AND object.tbl_name!='audit_logs'
              AND lower(object.sql) LIKE '%audit_logs%')
          OR (object.type='table' AND object.name!='audit_logs'
              AND lower(object.sql) LIKE '%references%audit_logs%')
        ))=0 THEN 0 ELSE 1 END;
DROP TRIGGER trg_prd058_up_guard;
DROP TABLE prd058_up_guard;

-- SQLite 不能原地扩展 CHECK 枚举，因此保真重建 0048 的最终审计合同。
CREATE TABLE audit_logs_prd058_new (
  audit_log_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  actor_email TEXT NOT NULL,
  actor_role TEXT NOT NULL CHECK (actor_role IN ('admin', 'developer', 'viewer')),
  action TEXT NOT NULL CHECK (
    action IN (
      'provider.created', 'provider.updated',
      'provider_key.created', 'provider_key.updated',
      'virtual_key.created', 'virtual_key.updated',
      'budget_rule.created', 'budget_rule.updated',
      'dlp_rule.created', 'dlp_rule.updated',
      'webhook_delivery.replayed', 'webhook_endpoint.secret_rotated',
      'webhook_dead_letter.archived',
      'usage_export.requested', 'usage_export.downloaded',
      'policy.created', 'policy.updated',
      'policy_snapshot.published', 'policy_snapshot.rolled_back',
      'policy.dry_run_executed',
      'routing_policy.created', 'routing_policy.updated',
      'model_quality_score.updated',
      'cache_affinity.created', 'cache_affinity.updated',
      'finops.adjustment_created',
      'cost_anomaly.acknowledged', 'cost_anomaly.dismissed',
      'cost_anomaly.temporary_rate_limit_applied',
      'cost_anomaly.temporary_rate_limit_released',
      'error_map.created', 'error_map.updated',
      'audit.exported'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'webhook_dead_letter',
      'usage_export_job', 'policy', 'policy_snapshot', 'policy_explanation',
      'routing_policy', 'model_quality_score', 'cache_affinity',
      'finops_adjustment', 'cost_anomaly', 'provider_error_map', 'audit_export'
    )
  ),
  object_id TEXT NOT NULL,
  object_name TEXT,
  before_summary_json TEXT NOT NULL CHECK (
    json_valid(before_summary_json) AND json_type(before_summary_json)='object'
  ),
  after_summary_json TEXT NOT NULL CHECK (
    json_valid(after_summary_json) AND json_type(after_summary_json)='object'
  ),
  changed_fields_json TEXT NOT NULL CHECK (
    json_valid(changed_fields_json) AND json_type(changed_fields_json)='array'
  ),
  client_ip TEXT,
  user_agent TEXT,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (tenant_id, audit_log_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);

-- 显式复制 rowid 和每一列；新枚举不能重排历史事实或改变 NULL 语义。
INSERT INTO audit_logs_prd058_new(
  rowid,audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
  action,object_type,object_id,object_name,before_summary_json,
  after_summary_json,changed_fields_json,client_ip,user_agent,
  request_id,trace_id,created_at
)
SELECT
  rowid,audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
  action,object_type,object_id,object_name,before_summary_json,
  after_summary_json,changed_fields_json,client_ip,user_agent,
  request_id,trace_id,created_at
FROM audit_logs;

CREATE TEMP TABLE prd058_audit_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  null_mismatch_count INTEGER NOT NULL,
  row_difference_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd058_audit_copy_guard
BEFORE INSERT ON prd058_audit_copy_guard FOR EACH ROW
WHEN NEW.source_count!=NEW.copied_count
  OR NEW.null_mismatch_count!=0 OR NEW.row_difference_count!=0
BEGIN SELECT RAISE(ABORT,'prd058_audit_copy_failed'); END;
-- 删除旧表前核对行数、NULL 分布和含 rowid 双向差集，禁止静默损坏历史证据。
INSERT INTO prd058_audit_copy_guard(
  source_count,copied_count,null_mismatch_count,row_difference_count
)
SELECT
  (SELECT COUNT(*) FROM audit_logs),
  (SELECT COUNT(*) FROM audit_logs_prd058_new),
  CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd058_new WHERE object_name IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd058_new WHERE client_ip IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd058_new WHERE user_agent IS NULL) THEN 0 ELSE 1 END,
  (SELECT COUNT(*) FROM (
    SELECT rowid,* FROM audit_logs
    EXCEPT SELECT rowid,* FROM audit_logs_prd058_new
  ))
  + (SELECT COUNT(*) FROM (
    SELECT rowid,* FROM audit_logs_prd058_new
    EXCEPT SELECT rowid,* FROM audit_logs
  ));
DROP TRIGGER trg_prd058_audit_copy_guard;
DROP TABLE prd058_audit_copy_guard;
DROP TABLE audit_logs;
-- 复制守卫通过后才切换正式表名，事务外读者不会看到半迁移结构。
ALTER TABLE audit_logs_prd058_new RENAME TO audit_logs;

-- 恢复全部既有 tenant-first 索引，并增加复合候选键供同租户 FK 使用。
CREATE INDEX idx_audit_logs_tenant_time
  ON audit_logs(tenant_id, created_at DESC, audit_log_id DESC);
CREATE INDEX idx_audit_logs_tenant_actor_time
  ON audit_logs(tenant_id, actor_user_id, created_at DESC);
CREATE INDEX idx_audit_logs_tenant_object_time
  ON audit_logs(tenant_id, object_type, object_id, created_at DESC);
CREATE INDEX idx_audit_logs_tenant_action_time
  ON audit_logs(tenant_id, action, created_at DESC);
CREATE INDEX idx_audit_logs_tenant_request
  ON audit_logs(tenant_id, request_id);
CREATE UNIQUE INDEX idx_audit_logs_dry_run_request_unique
  ON audit_logs(tenant_id, actor_user_id, request_id)
  WHERE action='policy.dry_run_executed';
CREATE UNIQUE INDEX idx_audit_logs_model_quality_request_unique
  ON audit_logs(tenant_id, actor_user_id, request_id)
  WHERE action='model_quality_score.updated';
CREATE UNIQUE INDEX idx_audit_logs_finops_adjustment_unique
  ON audit_logs(tenant_id, object_id)
  WHERE action='finops.adjustment_created';

-- AUTOINCREMENT 是导出快照唯一允许使用的稳定水位；普通 audit_logs.rowid 不参与快照。
CREATE TABLE audit_log_export_sequences (
  audit_log_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  audit_log_id TEXT NOT NULL UNIQUE,
  UNIQUE (tenant_id, audit_log_id),
  FOREIGN KEY (tenant_id, audit_log_id)
    REFERENCES audit_logs(tenant_id, audit_log_id)
);
CREATE INDEX idx_audit_log_export_sequences_tenant_sequence_prd058
  ON audit_log_export_sequences(tenant_id, audit_log_sequence);

CREATE TRIGGER trg_audit_log_export_sequences_owner_insert_prd058
BEFORE INSERT ON audit_log_export_sequences FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM audit_logs audit
  WHERE audit.tenant_id=NEW.tenant_id AND audit.audit_log_id=NEW.audit_log_id
)
BEGIN SELECT RAISE(ABORT,'audit_log_export_sequence_owner_mismatch'); END;
CREATE TRIGGER trg_audit_log_export_sequences_conflict_insert_prd058
BEFORE INSERT ON audit_log_export_sequences FOR EACH ROW
-- 业务代码只能让 AUTOINCREMENT 分配水位；显式 0/正数会破坏“0=空快照”和单调性。
WHEN NEW.rowid!=-1 OR EXISTS (
  SELECT 1 FROM audit_log_export_sequences current
  WHERE current.audit_log_id=NEW.audit_log_id
)
BEGIN SELECT RAISE(ABORT,'audit_log_export_sequence_immutable'); END;
CREATE TRIGGER trg_audit_log_export_sequences_rowid_insert_prd058
AFTER INSERT ON audit_log_export_sequences FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'audit_log_export_sequence_immutable'); END;
CREATE TRIGGER trg_audit_log_export_sequences_update_prd058
BEFORE UPDATE ON audit_log_export_sequences FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_export_sequence_immutable'); END;
CREATE TRIGGER trg_audit_log_export_sequences_delete_prd058
BEFORE DELETE ON audit_log_export_sequences FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_export_sequence_immutable'); END;

-- 历史回填按稳定业务排序分配水位；映射本身随后永远不可更新或删除。
INSERT INTO audit_log_export_sequences(tenant_id,audit_log_id)
SELECT tenant_id,audit_log_id FROM audit_logs
ORDER BY created_at ASC,audit_log_id ASC;

CREATE TEMP TABLE prd058_sequence_copy_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd058_sequence_copy_guard
BEFORE INSERT ON prd058_sequence_copy_guard FOR EACH ROW WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd058_sequence_backfill_failed'); END;
-- 回填必须实现 AuditLog 与稳定序列一一同租户映射，缺失或错 owner 都阻断升级。
INSERT INTO prd058_sequence_copy_guard(invalid_count)
SELECT
  ABS((SELECT COUNT(*) FROM audit_logs)
      -(SELECT COUNT(*) FROM audit_log_export_sequences))
  + (SELECT COUNT(*) FROM audit_logs audit
      LEFT JOIN audit_log_export_sequences sequence
        ON sequence.tenant_id=audit.tenant_id AND sequence.audit_log_id=audit.audit_log_id
      WHERE sequence.audit_log_id IS NULL)
  + (SELECT COUNT(*) FROM audit_log_export_sequences sequence
      LEFT JOIN audit_logs audit
        ON audit.tenant_id=sequence.tenant_id AND audit.audit_log_id=sequence.audit_log_id
      WHERE audit.audit_log_id IS NULL);
DROP TRIGGER trg_prd058_sequence_copy_guard;
DROP TABLE prd058_sequence_copy_guard;

-- 同步导出不保存 CSV BLOB，只保存足以确定性复核本地下载文件的不可变终态。
CREATE TABLE audit_exports (
  audit_export_id TEXT PRIMARY KEY CHECK (
    length(audit_export_id)=36 AND substr(audit_export_id,1,4)='aex_'
    AND substr(audit_export_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  audit_log_id TEXT NOT NULL UNIQUE CHECK (
    length(audit_log_id) BETWEEN 1 AND 128
    AND audit_log_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  status TEXT NOT NULL CHECK (status='completed'),
  format TEXT NOT NULL CHECK (format='csv'),
  schema_version INTEGER NOT NULL CHECK (schema_version=1),
  filters_json TEXT NOT NULL CHECK (
    json_valid(filters_json) AND json_type(filters_json)='object'
    AND length(CAST(filters_json AS BLOB))<=4096
  ),
  filter_fingerprint TEXT NOT NULL CHECK (
    length(filter_fingerprint)=64 AND filter_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  snapshot_sequence INTEGER NOT NULL CHECK (
    typeof(snapshot_sequence)='integer' AND snapshot_sequence>=0
  ),
  snapshot_audit_log_id TEXT,
  snapshot_created_at TEXT CHECK (
    snapshot_created_at IS NULL OR unixepoch(snapshot_created_at) IS NOT NULL
  ),
  row_count INTEGER NOT NULL CHECK (
    typeof(row_count)='integer' AND row_count BETWEEN 0 AND 10000
  ),
  content_size_bytes INTEGER NOT NULL CHECK (
    typeof(content_size_bytes)='integer' AND content_size_bytes BETWEEN 1 AND 16777216
  ),
  content_sha256 TEXT NOT NULL CHECK (
    length(content_sha256)=64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'
  ),
  file_name TEXT NOT NULL CHECK (
    length(file_name) BETWEEN 5 AND 128
    AND substr(file_name,-4)='.csv'
    AND file_name NOT GLOB '*[^A-Za-z0-9._-]*'
    AND instr(file_name,'/')=0 AND instr(file_name,'\')=0
  ),
  request_id TEXT NOT NULL CHECK (
    length(request_id) BETWEEN 1 AND 128
    AND request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  trace_id TEXT NOT NULL CHECK (
    length(trace_id) BETWEEN 1 AND 160
    AND trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  event_id TEXT NOT NULL CHECK (
    length(event_id)=39 AND substr(event_id,1,7)='aexevt_'
    AND substr(event_id,8) NOT GLOB '*[^0-9a-f]*'
  ),
  event_payload_json TEXT NOT NULL CHECK (
    json_valid(event_payload_json) AND json_type(event_payload_json)='object'
    AND length(CAST(event_payload_json AS BLOB))<=4096
  ),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK (
    unixepoch(updated_at) IS NOT NULL AND updated_at=created_at
  ),
  UNIQUE (tenant_id, audit_export_id),
  UNIQUE (tenant_id, requested_by, request_id),
  UNIQUE (tenant_id, event_id),
  UNIQUE (tenant_id, audit_export_id, event_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, requested_by) REFERENCES users(tenant_id, user_id),
  FOREIGN KEY (tenant_id, audit_log_id)
    REFERENCES audit_logs(tenant_id, audit_log_id)
    DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX idx_audit_exports_tenant_time
  ON audit_exports(tenant_id, created_at DESC, audit_export_id DESC);
CREATE INDEX idx_audit_exports_tenant_actor_time
  ON audit_exports(tenant_id, requested_by, created_at DESC);

-- source outbox 只保存可变派发状态；不可变事件身份和 payload 仍以 AuditExport 为准。
CREATE TABLE audit_export_events (
  audit_export_event_id TEXT PRIMARY KEY CHECK (
    length(audit_export_event_id)=39 AND substr(audit_export_event_id,1,7)='aexevt_'
    AND substr(audit_export_event_id,8) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  audit_export_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','processing','dispatched','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(attempt_count)='integer' AND attempt_count BETWEEN 0 AND 10
  ),
  next_attempt_at TEXT,
  claim_token TEXT,
  claim_owner TEXT,
  lease_until TEXT,
  last_error_code TEXT CHECK (
    last_error_code IS NULL OR last_error_code IN (
      'webhook.delivery_unavailable','audit_export.event_unavailable'
    )
  ),
  dispatched_at TEXT,
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK (unixepoch(updated_at) IS NOT NULL),
  UNIQUE (tenant_id, audit_export_event_id),
  UNIQUE (tenant_id, audit_export_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, audit_export_id, audit_export_event_id)
    REFERENCES audit_exports(tenant_id, audit_export_id, event_id),
  CHECK (
    (status='pending' AND attempt_count BETWEEN 0 AND 9
      AND next_attempt_at IS NOT NULL AND unixepoch(next_attempt_at) IS NOT NULL
      AND claim_token IS NULL AND claim_owner IS NULL AND lease_until IS NULL
      AND dispatched_at IS NULL)
    OR (status='processing' AND attempt_count BETWEEN 1 AND 10
      AND next_attempt_at IS NULL AND dispatched_at IS NULL
      AND claim_token IS NOT NULL AND length(claim_token)=32
      AND claim_token NOT GLOB '*[^0-9a-f]*'
      AND claim_owner IS NOT NULL AND length(claim_owner) BETWEEN 1 AND 128
      AND claim_owner NOT GLOB '*[^A-Za-z0-9._:-]*'
      AND lease_until IS NOT NULL AND unixepoch(lease_until) IS NOT NULL
      AND last_error_code IS NULL)
    OR (status='dispatched' AND attempt_count BETWEEN 1 AND 10
      AND next_attempt_at IS NULL AND dispatched_at IS NOT NULL
      AND unixepoch(dispatched_at) IS NOT NULL
      AND claim_token IS NULL AND claim_owner IS NULL AND lease_until IS NULL
      AND last_error_code IS NULL)
    OR (status='failed' AND attempt_count=10
      AND next_attempt_at IS NULL AND dispatched_at IS NULL
      AND claim_token IS NULL AND claim_owner IS NULL AND lease_until IS NULL
      AND last_error_code IS NOT NULL)
  )
);
CREATE INDEX idx_audit_export_events_due_prd058
  ON audit_export_events(status,next_attempt_at,lease_until,created_at,audit_export_event_id)
  WHERE status IN ('pending','processing');
CREATE INDEX idx_audit_export_events_tenant_status_prd058
  ON audit_export_events(tenant_id,status,updated_at,audit_export_event_id);

-- AuditExport INSERT 同时证明 actor、快照边界、幂等键与安全事件摘要；预留 AuditLog ID 必须尚未使用。
CREATE TRIGGER trg_audit_exports_owner_insert_prd058
BEFORE INSERT ON audit_exports FOR EACH ROW
WHEN NOT (
  NOT EXISTS (SELECT 1 FROM audit_logs audit WHERE audit.audit_log_id=NEW.audit_log_id)
  AND EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.requested_by
  )
  AND (
    (NEW.snapshot_sequence=0
      AND NEW.snapshot_audit_log_id IS NULL AND NEW.snapshot_created_at IS NULL)
    OR
    (NEW.snapshot_sequence>0
      AND NEW.snapshot_audit_log_id IS NOT NULL AND NEW.snapshot_created_at IS NOT NULL
      AND EXISTS (
        SELECT 1
        FROM audit_log_export_sequences sequence
        JOIN audit_logs audit
          ON audit.tenant_id=sequence.tenant_id
         AND audit.audit_log_id=sequence.audit_log_id
        WHERE sequence.tenant_id=NEW.tenant_id
          AND sequence.audit_log_sequence=NEW.snapshot_sequence
          AND sequence.audit_log_id=NEW.snapshot_audit_log_id
          AND audit.created_at=NEW.snapshot_created_at
      ))
  )
  AND json_valid(NEW.filters_json)
  AND CASE WHEN json_valid(NEW.filters_json) THEN json_type(NEW.filters_json) END='object'
  AND NOT EXISTS (
    SELECT 1 FROM json_each(
      CASE WHEN json_valid(NEW.filters_json) THEN NEW.filters_json ELSE '{}' END
    ) filter
    WHERE filter.key NOT IN (
      'created_from','created_to','actor_user_id','action','object_type','object_id','request_id'
    ) OR filter.type NOT IN ('text','null')
  )
  AND json_valid(NEW.event_payload_json)
  AND CASE WHEN json_valid(NEW.event_payload_json)
    THEN json_type(NEW.event_payload_json) END='object'
  AND (SELECT COUNT(*) FROM json_each(
    CASE WHEN json_valid(NEW.event_payload_json) THEN NEW.event_payload_json ELSE '{}' END
  ))=8
  AND (SELECT COUNT(DISTINCT key) FROM json_each(
    CASE WHEN json_valid(NEW.event_payload_json) THEN NEW.event_payload_json ELSE '{}' END
  ))=8
  AND NOT EXISTS (
    SELECT 1 FROM json_each(
      CASE WHEN json_valid(NEW.event_payload_json) THEN NEW.event_payload_json ELSE '{}' END
    ) field
    WHERE field.key NOT IN (
      'audit_export_id','status','format','schema_version','row_count',
      'content_size_bytes','content_sha256','filter_fingerprint'
    )
  )
  AND json_extract(NEW.event_payload_json,'$.audit_export_id')=NEW.audit_export_id
  AND json_extract(NEW.event_payload_json,'$.status')=NEW.status
  AND json_extract(NEW.event_payload_json,'$.format')=NEW.format
  AND json_type(NEW.event_payload_json,'$.schema_version')='integer'
  AND json_extract(NEW.event_payload_json,'$.schema_version')=NEW.schema_version
  AND json_type(NEW.event_payload_json,'$.row_count')='integer'
  AND json_extract(NEW.event_payload_json,'$.row_count')=NEW.row_count
  AND json_type(NEW.event_payload_json,'$.content_size_bytes')='integer'
  AND json_extract(NEW.event_payload_json,'$.content_size_bytes')=NEW.content_size_bytes
  AND json_extract(NEW.event_payload_json,'$.content_sha256')=NEW.content_sha256
  AND json_extract(NEW.event_payload_json,'$.filter_fingerprint')=NEW.filter_fingerprint
)
BEGIN SELECT RAISE(ABORT,'audit_export_owner_mismatch'); END;

CREATE TRIGGER trg_audit_exports_conflict_insert_prd058
BEFORE INSERT ON audit_exports FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM audit_exports current
  WHERE current.audit_export_id=NEW.audit_export_id
     OR current.audit_log_id=NEW.audit_log_id
     OR (current.tenant_id=NEW.tenant_id AND current.requested_by=NEW.requested_by
         AND current.request_id=NEW.request_id)
     OR (current.tenant_id=NEW.tenant_id AND current.event_id=NEW.event_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'audit_export_immutable'); END;
CREATE TRIGGER trg_audit_exports_rowid_insert_prd058
AFTER INSERT ON audit_exports FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'audit_export_immutable'); END;
CREATE TRIGGER trg_audit_exports_update_prd058
BEFORE UPDATE ON audit_exports FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_export_immutable'); END;
CREATE TRIGGER trg_audit_exports_delete_prd058
BEFORE DELETE ON audit_exports FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_export_immutable'); END;

-- 新 source event 只能是与同事务 AuditExport 一一对应的 pending intent。
CREATE TRIGGER trg_audit_export_events_owner_insert_prd058
BEFORE INSERT ON audit_export_events FOR EACH ROW
WHEN NOT (
  NEW.audit_export_event_id=(
    SELECT export.event_id FROM audit_exports export
    WHERE export.tenant_id=NEW.tenant_id AND export.audit_export_id=NEW.audit_export_id
  )
  AND NEW.status='pending' AND NEW.attempt_count=0
  AND NEW.next_attempt_at=NEW.created_at AND NEW.updated_at=NEW.created_at
  AND NEW.claim_token IS NULL AND NEW.claim_owner IS NULL AND NEW.lease_until IS NULL
  AND NEW.last_error_code IS NULL AND NEW.dispatched_at IS NULL
)
BEGIN SELECT RAISE(ABORT,'audit_export_event_owner_mismatch'); END;

-- 只允许 due claim、过期 lease 接管、持有效 lease 成功/退避/失败四类状态转换。
CREATE TRIGGER trg_audit_export_events_state_update_prd058
BEFORE UPDATE ON audit_export_events FOR EACH ROW
WHEN NOT (
  NEW.rowid=OLD.rowid
  AND NEW.audit_export_event_id=OLD.audit_export_event_id
  AND NEW.tenant_id=OLD.tenant_id
  AND NEW.audit_export_id=OLD.audit_export_id
  AND NEW.created_at=OLD.created_at
  AND unixepoch(NEW.updated_at)>=unixepoch(OLD.updated_at)
  AND (
    (
      (
        (OLD.status='pending' AND unixepoch(OLD.next_attempt_at)<=unixepoch(NEW.updated_at))
        OR
        (OLD.status='processing' AND OLD.attempt_count BETWEEN 1 AND 9
          AND unixepoch(OLD.lease_until)<=unixepoch(NEW.updated_at))
      )
      AND NEW.status='processing'
      AND NEW.attempt_count=OLD.attempt_count+1
      AND NEW.next_attempt_at IS NULL AND NEW.dispatched_at IS NULL
      AND NEW.claim_token IS NOT NULL AND length(NEW.claim_token)=32
      AND NEW.claim_token NOT GLOB '*[^0-9a-f]*'
      AND (OLD.status!='processing' OR NEW.claim_token IS NOT OLD.claim_token)
      AND NEW.claim_owner IS NOT NULL AND length(NEW.claim_owner) BETWEEN 1 AND 128
      AND NEW.claim_owner NOT GLOB '*[^A-Za-z0-9._:-]*'
      AND unixepoch(NEW.lease_until)=unixepoch(NEW.updated_at)+30
      AND NEW.last_error_code IS NULL
    )
    OR
    (
      OLD.status='processing'
      AND NEW.attempt_count=OLD.attempt_count
      AND NEW.claim_token IS NULL AND NEW.claim_owner IS NULL AND NEW.lease_until IS NULL
      AND (
        (
          unixepoch(OLD.lease_until)>unixepoch(NEW.updated_at)
          AND (
            (NEW.status='dispatched' AND NEW.dispatched_at=NEW.updated_at
              AND NEW.next_attempt_at IS NULL AND NEW.last_error_code IS NULL)
            OR
            (OLD.attempt_count<10 AND NEW.status='pending'
              AND NEW.dispatched_at IS NULL AND NEW.last_error_code IS NOT NULL
              AND unixepoch(NEW.next_attempt_at)=unixepoch(NEW.updated_at)+CASE
                WHEN OLD.attempt_count=1 THEN 5
                WHEN OLD.attempt_count=2 THEN 30
                ELSE 300 END)
            OR
            (OLD.attempt_count=10 AND NEW.status='failed'
              AND NEW.next_attempt_at IS NULL AND NEW.dispatched_at IS NULL
              AND NEW.last_error_code IS NOT NULL)
          )
        )
        OR
        (OLD.attempt_count=10
          AND unixepoch(OLD.lease_until)<=unixepoch(NEW.updated_at)
          AND NEW.status='failed' AND NEW.next_attempt_at IS NULL
          AND NEW.dispatched_at IS NULL AND NEW.last_error_code IS NOT NULL)
      )
    )
  )
)
BEGIN SELECT RAISE(ABORT,'audit_export_event_state_invalid'); END;

CREATE TRIGGER trg_audit_export_events_conflict_insert_prd058
BEFORE INSERT ON audit_export_events FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM audit_export_events current
  WHERE current.audit_export_event_id=NEW.audit_export_event_id
     OR (current.tenant_id=NEW.tenant_id AND current.audit_export_id=NEW.audit_export_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'audit_export_event_immutable'); END;
CREATE TRIGGER trg_audit_export_events_rowid_insert_prd058
AFTER INSERT ON audit_export_events FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'audit_export_event_immutable'); END;
CREATE TRIGGER trg_audit_export_events_delete_prd058
BEFORE DELETE ON audit_export_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_export_event_immutable'); END;

-- Audit owner trigger 继承 0048 全部分支，并将 audit.exported 与预留 ID 精确反向绑定。
CREATE TRIGGER trg_audit_logs_owner_insert
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM users user
  WHERE user.user_id=NEW.actor_user_id AND user.tenant_id=NEW.tenant_id
)
OR (
  NEW.action='finops.adjustment_created'
  AND (
    NEW.object_type!='finops_adjustment'
    OR NOT EXISTS (
      SELECT 1 FROM finops_adjustments adjustment
      WHERE adjustment.finops_adjustment_id=NEW.object_id
        AND adjustment.tenant_id=NEW.tenant_id
        AND adjustment.actor_user_id=NEW.actor_user_id
    )
  )
)
OR (NEW.object_type='finops_adjustment' AND NEW.action!='finops.adjustment_created')
OR (
  NEW.action IN (
    'cost_anomaly.acknowledged','cost_anomaly.dismissed',
    'cost_anomaly.temporary_rate_limit_applied',
    'cost_anomaly.temporary_rate_limit_released'
  )
  AND (
    NEW.object_type!='cost_anomaly'
    OR NOT EXISTS (
      SELECT 1 FROM cost_anomaly_actions action
      JOIN cost_anomalies anomaly
        ON anomaly.tenant_id=action.tenant_id
       AND anomaly.cost_anomaly_id=action.cost_anomaly_id
      WHERE action.tenant_id=NEW.tenant_id
        AND action.cost_anomaly_id=NEW.object_id
        AND action.actor_user_id=NEW.actor_user_id
        AND action.operation_request_id=NEW.request_id
        AND action.operation_trace_id=NEW.trace_id
        AND CASE action.action_type
          WHEN 'acknowledge' THEN 'cost_anomaly.acknowledged'
          WHEN 'dismiss' THEN 'cost_anomaly.dismissed'
          WHEN 'apply_temporary_rate_limit' THEN 'cost_anomaly.temporary_rate_limit_applied'
          WHEN 'release_temporary_rate_limit' THEN 'cost_anomaly.temporary_rate_limit_released'
        END=NEW.action
    )
  )
)
OR (
  NEW.object_type='cost_anomaly'
  AND NEW.action NOT IN (
    'cost_anomaly.acknowledged','cost_anomaly.dismissed',
    'cost_anomaly.temporary_rate_limit_applied',
    'cost_anomaly.temporary_rate_limit_released'
  )
)
OR (
  NEW.action IN ('error_map.created','error_map.updated')
  AND (
    NEW.object_type!='provider_error_map'
    OR NOT EXISTS (
      SELECT 1 FROM provider_error_map_events event
      JOIN provider_error_map_versions version
        ON version.tenant_id=event.tenant_id
       AND version.provider_error_map_version_id=event.provider_error_map_version_id
      JOIN provider_error_maps map
        ON map.tenant_id=event.tenant_id
       AND map.provider_error_map_id=event.provider_error_map_id
       AND map.current_version=event.rule_version
      WHERE event.tenant_id=NEW.tenant_id
        AND event.provider_error_map_id=NEW.object_id
        AND event.actor_user_id=NEW.actor_user_id
        AND event.request_id=NEW.request_id
        AND event.trace_id=NEW.trace_id
        AND (
          (NEW.action='error_map.created' AND event.operation='create')
          OR (NEW.action='error_map.updated' AND event.operation IN ('update','enable','disable'))
        )
    )
  )
)
OR (
  NEW.object_type='provider_error_map'
  AND NEW.action NOT IN ('error_map.created','error_map.updated')
)
OR (
  NEW.action='webhook_dead_letter.archived'
  AND (
    NEW.object_type!='webhook_dead_letter'
    OR NOT EXISTS (
      SELECT 1 FROM webhook_dead_letter_events dead_letter
      WHERE dead_letter.tenant_id=NEW.tenant_id
        AND dead_letter.dead_letter_event_id=NEW.object_id
        AND dead_letter.status='archived'
        AND dead_letter.archived_by=NEW.actor_user_id
    )
  )
)
OR (
  NEW.object_type='webhook_dead_letter'
  AND NEW.action!='webhook_dead_letter.archived'
)
OR (
  NEW.action='audit.exported'
  AND (
    NEW.object_type!='audit_export'
    OR NOT EXISTS (
      SELECT 1 FROM audit_exports export
      WHERE export.tenant_id=NEW.tenant_id
        AND export.audit_export_id=NEW.object_id
        AND export.audit_log_id=NEW.audit_log_id
        AND export.requested_by=NEW.actor_user_id
        AND export.request_id=NEW.request_id
        AND export.trace_id=NEW.trace_id
        AND export.created_at=NEW.created_at
    )
  )
)
OR (NEW.object_type='audit_export' AND NEW.action!='audit.exported')
OR (
  EXISTS (SELECT 1 FROM audit_exports export WHERE export.audit_log_id=NEW.audit_log_id)
  AND NOT (
    NEW.action='audit.exported' AND NEW.object_type='audit_export'
    AND EXISTS (
      SELECT 1 FROM audit_exports export
      WHERE export.tenant_id=NEW.tenant_id
        AND export.audit_export_id=NEW.object_id
        AND export.audit_log_id=NEW.audit_log_id
        AND export.requested_by=NEW.actor_user_id
        AND export.request_id=NEW.request_id
        AND export.trace_id=NEW.trace_id
        AND export.created_at=NEW.created_at
    )
  )
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;

CREATE TRIGGER trg_audit_logs_immutable_update
BEFORE UPDATE ON audit_logs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_immutable_delete
BEFORE DELETE ON audit_logs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_prd050_conflict_insert
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM audit_logs current
  WHERE current.audit_log_id=NEW.audit_log_id
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
    OR (
      NEW.action IN ('policy.dry_run_executed','model_quality_score.updated')
      AND current.action=NEW.action AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
    OR (
      NEW.action='finops.adjustment_created'
      AND current.action='finops.adjustment_created'
      AND current.tenant_id=NEW.tenant_id AND current.object_id=NEW.object_id
    )
    OR (
      NEW.action IN (
        'cost_anomaly.acknowledged','cost_anomaly.dismissed',
        'cost_anomaly.temporary_rate_limit_applied',
        'cost_anomaly.temporary_rate_limit_released'
      )
      AND current.action=NEW.action AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
    OR (
      NEW.action IN ('error_map.created','error_map.updated')
      AND current.action=NEW.action AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
)
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_prd050_rowid_insert
AFTER INSERT ON audit_logs FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;

-- 每条新 AuditLog 在同一事务获得唯一稳定水位；映射失败必须回滚原审计写入。
CREATE TRIGGER trg_audit_logs_export_sequence_insert_prd058
AFTER INSERT ON audit_logs FOR EACH ROW
BEGIN
  -- 自动映射与原 AuditLog 共用事务，序列写入失败会撤销审计事实。
  INSERT INTO audit_log_export_sequences(tenant_id,audit_log_id)
  VALUES(NEW.tenant_id,NEW.audit_log_id);
END;

-- 终态自检登记全部核心对象并再次验证双向映射，避免部分 schema 被误提交。
CREATE TEMP TABLE prd058_final_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd058_final_guard
BEFORE INSERT ON prd058_final_guard FOR EACH ROW WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd058_final_schema_invalid'); END;
-- 提交前再次核对核心表和映射计数，避免未来编辑遗漏 trigger 或保真步骤。
INSERT INTO prd058_final_guard(invalid_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
    WHERE type='table' AND name IN (
      'audit_logs','audit_log_export_sequences','audit_exports','audit_export_events'
    ))=4 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs)
    =(SELECT COUNT(*) FROM audit_log_export_sequences) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM audit_logs audit
      LEFT JOIN audit_log_export_sequences sequence
        ON sequence.tenant_id=audit.tenant_id AND sequence.audit_log_id=audit.audit_log_id
      WHERE sequence.audit_log_id IS NULL);
DROP TRIGGER trg_prd058_final_guard;
DROP TABLE prd058_final_guard;

COMMIT;
