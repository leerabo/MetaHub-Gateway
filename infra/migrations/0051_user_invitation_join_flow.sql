-- 迁移职责：为 PRD-060 建立 tenant-qualified Invitation、一次性 Token 哈希、状态机、
-- 强审计和两个不可变 source event，并保真扩展 0050 AuditLog 的 action/object 枚举。
-- 安全边界：原始 Token、密码、Authorization、Cookie、邮箱明文事件、Provider Key、
-- Prompt、Response 和 Webhook 正文都不得进入迁移新增的审计或事件列。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- 0051 只允许从完整 0050 栈顶升级；任何未知后续迁移或坏外键必须在首项持久 DDL 前失败。
CREATE TEMP TABLE prd060_up_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd060_up_guard
BEFORE INSERT ON prd060_up_guard FOR EACH ROW WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd060_prerequisite_failed'); END;
INSERT INTO prd060_up_guard(invalid_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
    WHERE version='0050' AND status='applied')=1 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM schema_migrations
      WHERE status='applied' AND version>'0050')=0 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='table' AND name IN (
        'tenants','users','roles','audit_logs','audit_log_export_sequences',
        'audit_exports','audit_export_events','role_permission_events'
      ))=8 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE name IN ('invitations','invitation_events'))=0 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check);
DROP TRIGGER trg_prd060_up_guard;
DROP TABLE prd060_up_guard;

-- TEMP 快照只服务本事务保真复核，不成为产品表或长期测试证据。
CREATE TEMP TABLE prd060_audit_before AS
SELECT rowid AS source_rowid,* FROM audit_logs;
CREATE TEMP TABLE prd060_sequences_before AS
SELECT rowid AS source_rowid,* FROM audit_log_export_sequences;
CREATE TEMP TABLE prd060_exports_before AS
SELECT rowid AS source_rowid,* FROM audit_exports;
CREATE TEMP TABLE prd060_export_events_before AS
SELECT rowid AS source_rowid,* FROM audit_export_events;
CREATE TEMP TABLE prd060_role_events_before AS
SELECT rowid AS source_rowid,* FROM role_permission_events;
CREATE TEMP TABLE prd060_sqlite_sequence_before AS
SELECT name,seq FROM sqlite_sequence WHERE name='audit_log_export_sequences';

-- Invitation 保存接受建号所需邮箱，但 Token 只保存不可逆 SHA-256；所有 owner 都由复合关系约束。
CREATE TABLE invitations (
  invitation_id TEXT PRIMARY KEY CHECK (
    length(invitation_id)=36 AND substr(invitation_id,1,4)='inv_'
    AND substr(invitation_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  email TEXT NOT NULL CHECK (
    length(email) BETWEEN 3 AND 255 AND email=lower(trim(email))
    AND instr(email,'@')>1 AND instr(substr(email,instr(email,'@')+1),'@')=0
  ),
  email_fingerprint TEXT NOT NULL CHECK (
    length(email_fingerprint)=64 AND email_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  role_code TEXT NOT NULL CHECK (
    role_code IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  ),
  token_hash TEXT NOT NULL UNIQUE CHECK (
    length(token_hash)=64 AND token_hash NOT GLOB '*[^0-9a-f]*'
  ),
  status TEXT NOT NULL CHECK (status IN ('pending','accepted','expired')),
  invited_by TEXT NOT NULL,
  accepted_user_id TEXT,
  expires_at TEXT NOT NULL CHECK (
    length(expires_at)=20 AND expires_at GLOB '????-??-??T??:??:??Z'
    AND substr(expires_at,1,4) BETWEEN '0001' AND '9999'
    AND strftime('%Y-%m-%dT%H:%M:%SZ',expires_at,'+0 seconds') IS NOT NULL
    AND strftime('%Y-%m-%dT%H:%M:%SZ',expires_at,'+0 seconds')=expires_at
  ),
  accepted_at TEXT CHECK (
    accepted_at IS NULL OR (
      length(accepted_at)=20 AND accepted_at GLOB '????-??-??T??:??:??Z'
      AND substr(accepted_at,1,4) BETWEEN '0001' AND '9999'
      AND strftime('%Y-%m-%dT%H:%M:%SZ',accepted_at,'+0 seconds') IS NOT NULL
      AND strftime('%Y-%m-%dT%H:%M:%SZ',accepted_at,'+0 seconds')=accepted_at
    )
  ),
  created_at TEXT NOT NULL CHECK (
    length(created_at)=20 AND created_at GLOB '????-??-??T??:??:??Z'
    AND substr(created_at,1,4) BETWEEN '0001' AND '9999'
    AND strftime('%Y-%m-%dT%H:%M:%SZ',created_at,'+0 seconds') IS NOT NULL
    AND strftime('%Y-%m-%dT%H:%M:%SZ',created_at,'+0 seconds')=created_at
  ),
  updated_at TEXT NOT NULL CHECK (
    length(updated_at)=20 AND updated_at GLOB '????-??-??T??:??:??Z'
    AND substr(updated_at,1,4) BETWEEN '0001' AND '9999'
    AND strftime('%Y-%m-%dT%H:%M:%SZ',updated_at,'+0 seconds') IS NOT NULL
    AND strftime('%Y-%m-%dT%H:%M:%SZ',updated_at,'+0 seconds')=updated_at
  ),
  UNIQUE (tenant_id,invitation_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,role_code) REFERENCES roles(tenant_id,code),
  FOREIGN KEY (tenant_id,invited_by) REFERENCES users(tenant_id,user_id),
  FOREIGN KEY (tenant_id,accepted_user_id) REFERENCES users(tenant_id,user_id),
  CHECK (unixepoch(expires_at)>unixepoch(created_at)),
  CHECK (unixepoch(updated_at)>=unixepoch(created_at)),
  CHECK (
    (status='pending' AND accepted_user_id IS NULL AND accepted_at IS NULL)
    OR (status='expired' AND accepted_user_id IS NULL AND accepted_at IS NULL)
    OR (status='accepted' AND accepted_user_id IS NOT NULL AND accepted_at IS NOT NULL
        AND unixepoch(accepted_at)>=unixepoch(created_at))
  )
);
CREATE INDEX idx_invitations_tenant_time_prd060
  ON invitations(tenant_id,created_at DESC,invitation_id DESC);
CREATE INDEX idx_invitations_tenant_status_prd060
  ON invitations(tenant_id,status,expires_at,invitation_id);
CREATE UNIQUE INDEX idx_invitations_pending_email_prd060
  ON invitations(tenant_id,email) WHERE status='pending';

-- 插入必须绑定 active tenant、active role 和同租户 active 邀请者，且不能邀请已有用户。
CREATE TRIGGER trg_invitations_owner_insert_prd060
BEFORE INSERT ON invitations FOR EACH ROW
WHEN NEW.status!='pending' OR NEW.accepted_user_id IS NOT NULL OR NEW.accepted_at IS NOT NULL
OR NEW.updated_at!=NEW.created_at
OR unixepoch(NEW.expires_at)<=unixepoch(NEW.created_at)
OR unixepoch(NEW.expires_at)>unixepoch(NEW.created_at,'+168 hours')
OR NOT EXISTS (
  SELECT 1 FROM tenants tenant
  WHERE tenant.tenant_id=NEW.tenant_id AND tenant.status='active'
)
OR NOT EXISTS (
  SELECT 1 FROM roles role
  WHERE role.tenant_id=NEW.tenant_id AND role.code=NEW.role_code AND role.status='active'
)
OR NOT EXISTS (
  SELECT 1 FROM users actor
  WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.invited_by
    AND actor.status='active'
)
OR NOT EXISTS (
  SELECT 1
  FROM users actor
  JOIN roles actor_role
    ON actor_role.tenant_id=actor.tenant_id AND actor_role.code=actor.role
  JOIN role_permissions relation
    ON relation.tenant_id=actor_role.tenant_id AND relation.role_id=actor_role.role_id
  JOIN permissions permission
    ON permission.tenant_id=relation.tenant_id
   AND permission.permission_id=relation.permission_id
  WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.invited_by
    AND actor.status='active' AND actor_role.status='active'
    AND permission.code='user.manage' AND permission.status='active'
)
OR EXISTS (
  SELECT 1 FROM users user
  WHERE user.tenant_id=NEW.tenant_id AND user.email=NEW.email
)
BEGIN SELECT RAISE(ABORT,'invitation_owner_mismatch'); END;

-- 冲突守卫后创建，使 SQLite BEFORE INSERT 逆序执行时优先返回 immutable 而不是 owner 差异。
CREATE TRIGGER trg_invitations_conflict_insert_prd060
BEFORE INSERT ON invitations FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM invitations current
  WHERE current.invitation_id=NEW.invitation_id OR current.token_hash=NEW.token_hash
     OR (current.tenant_id=NEW.tenant_id AND current.email=NEW.email
         AND current.status='pending')
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'invitation_immutable'); END;

-- 运行期只允许 pending 单向进入 accepted/expired；所有身份、凭据摘要和邀请意图不可变。
CREATE TRIGGER trg_invitations_state_update_prd060
BEFORE UPDATE ON invitations FOR EACH ROW
WHEN NOT (
  OLD.status='pending' AND NEW.status IN ('accepted','expired')
  AND NEW.invitation_id=OLD.invitation_id AND NEW.tenant_id=OLD.tenant_id
  AND NEW.email=OLD.email AND NEW.email_fingerprint=OLD.email_fingerprint
  AND NEW.role_code=OLD.role_code AND NEW.token_hash=OLD.token_hash
  AND NEW.invited_by=OLD.invited_by AND NEW.expires_at=OLD.expires_at
  AND NEW.created_at=OLD.created_at
  AND unixepoch(NEW.updated_at)>=unixepoch(OLD.updated_at)
  AND (
    (NEW.status='expired' AND NEW.accepted_user_id IS NULL AND NEW.accepted_at IS NULL
      AND unixepoch(NEW.updated_at)>=unixepoch(OLD.expires_at))
    OR
    (NEW.status='accepted' AND NEW.accepted_user_id IS NOT NULL
      AND NEW.accepted_at=NEW.updated_at
      AND unixepoch(NEW.accepted_at)<unixepoch(OLD.expires_at)
      AND EXISTS (
        SELECT 1
        FROM users inviter
        JOIN roles inviter_role
          ON inviter_role.tenant_id=inviter.tenant_id AND inviter_role.code=inviter.role
        JOIN role_permissions relation
          ON relation.tenant_id=inviter_role.tenant_id
         AND relation.role_id=inviter_role.role_id
        JOIN permissions permission
          ON permission.tenant_id=relation.tenant_id
         AND permission.permission_id=relation.permission_id
        WHERE inviter.tenant_id=OLD.tenant_id AND inviter.user_id=OLD.invited_by
          AND inviter.status='active' AND inviter_role.status='active'
          AND permission.code='user.manage' AND permission.status='active'
      )
      AND EXISTS (
        SELECT 1 FROM users accepted
        WHERE accepted.tenant_id=OLD.tenant_id AND accepted.user_id=NEW.accepted_user_id
          AND accepted.email=OLD.email AND accepted.role=OLD.role_code
          AND accepted.status='active'
      ))
  )
)
BEGIN SELECT RAISE(ABORT,'invitation_immutable'); END;
CREATE TRIGGER trg_invitations_delete_prd060
BEFORE DELETE ON invitations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'invitation_immutable'); END;
CREATE TRIGGER trg_invitations_rowid_insert_prd060
AFTER INSERT ON invitations FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'invitation_immutable'); END;

-- AuditLog 扩展两个 Invitation 动作；显式复制 rowid 保护 0050 审计、导出与权限事件关系。
CREATE TABLE audit_logs_prd060_new (
  audit_log_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  actor_email TEXT NOT NULL,
  actor_role TEXT NOT NULL CHECK (
    actor_role IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  ),
  action TEXT NOT NULL CHECK (
    action IN (
      'provider.created','provider.updated',
      'provider_key.created','provider_key.updated',
      'virtual_key.created','virtual_key.updated',
      'budget_rule.created','budget_rule.updated',
      'dlp_rule.created','dlp_rule.updated',
      'webhook_delivery.replayed','webhook_endpoint.secret_rotated',
      'webhook_dead_letter.archived',
      'usage_export.requested','usage_export.downloaded',
      'policy.created','policy.updated',
      'policy_snapshot.published','policy_snapshot.rolled_back',
      'policy.dry_run_executed',
      'routing_policy.created','routing_policy.updated',
      'model_quality_score.updated',
      'cache_affinity.created','cache_affinity.updated',
      'finops.adjustment_created',
      'cost_anomaly.acknowledged','cost_anomaly.dismissed',
      'cost_anomaly.temporary_rate_limit_applied',
      'cost_anomaly.temporary_rate_limit_released',
      'error_map.created','error_map.updated',
      'audit.exported','role.permission_changed',
      'user.invited','invitation.accepted'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider','provider_key','virtual_key','budget_rule','dlp_rule',
      'webhook_delivery','webhook_endpoint','webhook_dead_letter',
      'usage_export_job','policy','policy_snapshot','policy_explanation',
      'routing_policy','model_quality_score','cache_affinity',
      'finops_adjustment','cost_anomaly','provider_error_map','audit_export',
      'role_permission','invitation'
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
  UNIQUE (tenant_id,audit_log_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);
INSERT INTO audit_logs_prd060_new(
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

CREATE TEMP TABLE prd060_audit_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  null_mismatch_count INTEGER NOT NULL,
  row_difference_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd060_audit_copy_guard
BEFORE INSERT ON prd060_audit_copy_guard FOR EACH ROW
WHEN NEW.source_count!=NEW.copied_count
  OR NEW.null_mismatch_count!=0 OR NEW.row_difference_count!=0
BEGIN SELECT RAISE(ABORT,'prd060_audit_copy_failed'); END;
INSERT INTO prd060_audit_copy_guard(
  source_count,copied_count,null_mismatch_count,row_difference_count
)
SELECT
  (SELECT COUNT(*) FROM audit_logs),
  (SELECT COUNT(*) FROM audit_logs_prd060_new),
  CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd060_new WHERE object_name IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd060_new WHERE client_ip IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd060_new WHERE user_agent IS NULL) THEN 0 ELSE 1 END,
  (SELECT COUNT(*) FROM (
    SELECT rowid,* FROM audit_logs
    EXCEPT SELECT rowid,* FROM audit_logs_prd060_new
  ))
  + (SELECT COUNT(*) FROM (
    SELECT rowid,* FROM audit_logs_prd060_new
    EXCEPT SELECT rowid,* FROM audit_logs
  ));
DROP TRIGGER trg_prd060_audit_copy_guard;
DROP TABLE prd060_audit_copy_guard;

-- 所有引用 audit_logs 的外部 owner trigger 必须在父表切换前移除，随后按 0050 原定义恢复。
DROP TRIGGER trg_role_permission_events_owner_insert_prd059;
DROP TRIGGER trg_audit_log_export_sequences_owner_insert_prd058;
DROP TRIGGER trg_audit_exports_owner_insert_prd058;
DROP TRIGGER trg_audit_logs_owner_insert;
DROP TRIGGER trg_audit_logs_immutable_update;
DROP TRIGGER trg_audit_logs_immutable_delete;
DROP TRIGGER trg_audit_logs_prd050_conflict_insert;
DROP TRIGGER trg_audit_logs_prd050_rowid_insert;
DROP TRIGGER trg_audit_logs_export_sequence_insert_prd058;
DROP TABLE audit_logs;
ALTER TABLE audit_logs_prd060_new RENAME TO audit_logs;

-- 恢复 0050 全部 tenant-first 索引，并为两个 Invitation 动作增加请求幂等冲突索引。
CREATE INDEX idx_audit_logs_tenant_time
  ON audit_logs(tenant_id,created_at DESC,audit_log_id DESC);
CREATE INDEX idx_audit_logs_tenant_actor_time
  ON audit_logs(tenant_id,actor_user_id,created_at DESC);
CREATE INDEX idx_audit_logs_tenant_object_time
  ON audit_logs(tenant_id,object_type,object_id,created_at DESC);
CREATE INDEX idx_audit_logs_tenant_action_time
  ON audit_logs(tenant_id,action,created_at DESC);
CREATE INDEX idx_audit_logs_tenant_request
  ON audit_logs(tenant_id,request_id);
CREATE UNIQUE INDEX idx_audit_logs_dry_run_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='policy.dry_run_executed';
CREATE UNIQUE INDEX idx_audit_logs_model_quality_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='model_quality_score.updated';
CREATE UNIQUE INDEX idx_audit_logs_finops_adjustment_unique
  ON audit_logs(tenant_id,object_id)
  WHERE action='finops.adjustment_created';
CREATE UNIQUE INDEX idx_audit_logs_role_permission_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='role.permission_changed';
CREATE UNIQUE INDEX idx_audit_logs_invitation_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action IN ('user.invited','invitation.accepted');

-- 每条 AuditLog 的稳定导出水位仍必须绑定同租户父事实。
CREATE TRIGGER trg_audit_log_export_sequences_owner_insert_prd058
BEFORE INSERT ON audit_log_export_sequences FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM audit_logs audit
  WHERE audit.tenant_id=NEW.tenant_id AND audit.audit_log_id=NEW.audit_log_id
)
BEGIN SELECT RAISE(ABORT,'audit_log_export_sequence_owner_mismatch'); END;

-- 恢复 PRD-058 导出 owner 守卫，继续校验 actor、快照边界、过滤器和事件摘要。
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

-- SQLite 同类 BEFORE INSERT 触发器按后创建者优先执行，重建冲突守卫以保留 immutable 优先级。
DROP TRIGGER trg_audit_exports_conflict_insert_prd058;
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

-- 恢复 PRD-059 source event 对重建后 AuditLog 的 tenant-qualified 反向绑定。
CREATE TRIGGER trg_role_permission_events_owner_insert_prd059
BEFORE INSERT ON role_permission_events FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM roles role
  WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id
    AND role.code!='admin' AND role.status='active'
    AND role.permission_revision=NEW.after_revision
    AND role.matrix_sha256=NEW.after_sha256
)
OR NOT EXISTS (
  SELECT 1 FROM users actor
  WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.actor_user_id
    AND actor.status='active' AND actor.role=NEW.actor_role
)
OR NOT EXISTS (
  SELECT 1 FROM audit_logs audit
  WHERE audit.tenant_id=NEW.tenant_id AND audit.audit_log_id=NEW.audit_log_id
    AND audit.actor_user_id=NEW.actor_user_id AND audit.actor_role=NEW.actor_role
    AND audit.action='role.permission_changed'
    AND audit.object_type='role_permission' AND audit.object_id=NEW.role_id
    AND audit.request_id=NEW.request_id AND audit.trace_id=NEW.trace_id
    AND audit.created_at=NEW.created_at
    AND json_extract(audit.before_summary_json,'$.permission_revision')=NEW.before_revision
    AND json_extract(audit.before_summary_json,'$.matrix_sha256')=NEW.before_sha256
    AND json_extract(audit.after_summary_json,'$.permission_revision')=NEW.after_revision
    AND json_extract(audit.after_summary_json,'$.matrix_sha256')=NEW.after_sha256
    AND json_extract(audit.after_summary_json,'$.permission_count')=(
      SELECT COUNT(*) FROM role_permissions relation
      WHERE relation.tenant_id=NEW.tenant_id AND relation.role_id=NEW.role_id
    )
    AND json_extract(audit.before_summary_json,'$.permission_count')=(
      SELECT COUNT(*) FROM role_permissions relation
      WHERE relation.tenant_id=NEW.tenant_id AND relation.role_id=NEW.role_id
    )-(SELECT COUNT(*) FROM json_each(NEW.added_permissions_json))
     +(SELECT COUNT(*) FROM json_each(NEW.removed_permissions_json))
)
OR NEW.delivery_status!='pending' OR NEW.last_error_code IS NOT NULL
OR NEW.dispatched_at IS NOT NULL OR NEW.updated_at!=NEW.created_at
OR (SELECT COUNT(*) FROM json_each(NEW.added_permissions_json))
  +(SELECT COUNT(*) FROM json_each(NEW.removed_permissions_json))=0
OR EXISTS (
  SELECT 1 FROM json_each(NEW.added_permissions_json) item
  WHERE item.type!='text'
    OR NOT EXISTS (
      SELECT 1
      FROM rbac_role_permission_templates role_template
      JOIN roles role ON role.code=role_template.role_code
      WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id
        AND role_template.permission_code=item.value
    )
    OR NOT EXISTS (
      SELECT 1
      FROM role_permissions relation
      JOIN permissions permission
        ON permission.tenant_id=relation.tenant_id
       AND permission.permission_id=relation.permission_id
      WHERE relation.tenant_id=NEW.tenant_id AND relation.role_id=NEW.role_id
        AND permission.code=item.value
    )
)
OR EXISTS (
  SELECT 1 FROM json_each(NEW.removed_permissions_json) item
  WHERE item.type!='text'
    OR NOT EXISTS (
      SELECT 1
      FROM rbac_role_permission_templates role_template
      JOIN roles role ON role.code=role_template.role_code
      WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id
        AND role_template.permission_code=item.value
    )
    OR EXISTS (
      SELECT 1
      FROM role_permissions relation
      JOIN permissions permission
        ON permission.tenant_id=relation.tenant_id
       AND permission.permission_id=relation.permission_id
      WHERE relation.tenant_id=NEW.tenant_id AND relation.role_id=NEW.role_id
        AND permission.code=item.value
    )
)
OR (SELECT COUNT(*) FROM json_each(NEW.added_permissions_json))
  !=(SELECT COUNT(DISTINCT value) FROM json_each(NEW.added_permissions_json))
OR (SELECT COUNT(*) FROM json_each(NEW.removed_permissions_json))
  !=(SELECT COUNT(DISTINCT value) FROM json_each(NEW.removed_permissions_json))
OR EXISTS (
  SELECT 1 FROM json_each(NEW.added_permissions_json) added
  JOIN json_each(NEW.removed_permissions_json) removed ON removed.value=added.value
)
OR EXISTS (
  SELECT 1 FROM json_each(NEW.added_permissions_json) item
  JOIN json_each(NEW.added_permissions_json) next ON next.key=item.key+1
  WHERE CAST(item.value AS TEXT)>=CAST(next.value AS TEXT)
)
OR EXISTS (
  SELECT 1 FROM json_each(NEW.removed_permissions_json) item
  JOIN json_each(NEW.removed_permissions_json) next ON next.key=item.key+1
  WHERE CAST(item.value AS TEXT)>=CAST(next.value AS TEXT)
)
OR (SELECT COUNT(*) FROM json_each(NEW.payload_json))!=9
OR EXISTS (
  SELECT 1 FROM json_each(NEW.payload_json) field
  WHERE field.key NOT IN (
    'schema_version','role_id','role_code','before_revision','after_revision',
    'before_sha256','after_sha256','added_permissions','removed_permissions'
  )
)
OR json_extract(NEW.payload_json,'$.schema_version')!=1
OR json_extract(NEW.payload_json,'$.role_id')!=NEW.role_id
OR json_extract(NEW.payload_json,'$.before_revision')!=NEW.before_revision
OR json_extract(NEW.payload_json,'$.after_revision')!=NEW.after_revision
OR json_extract(NEW.payload_json,'$.before_sha256')!=NEW.before_sha256
OR json_extract(NEW.payload_json,'$.after_sha256')!=NEW.after_sha256
OR json(json_extract(NEW.payload_json,'$.added_permissions'))
  !=json(NEW.added_permissions_json)
OR json(json_extract(NEW.payload_json,'$.removed_permissions'))
  !=json(NEW.removed_permissions_json)
OR NOT EXISTS (
  SELECT 1 FROM roles role
  WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id
    AND role.code=json_extract(NEW.payload_json,'$.role_code')
)
OR EXISTS (
  SELECT 1 FROM role_permission_events current
  WHERE current.event_id=NEW.event_id
     OR (current.tenant_id=NEW.tenant_id AND current.actor_user_id=NEW.actor_user_id
         AND current.request_id=NEW.request_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_event_invalid'); END;

-- Audit owner 守卫保留 0050 全部分支，并把两个 Invitation 动作反向绑定到同租户终态。
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
OR (
  NEW.action='role.permission_changed'
  AND (
    NEW.object_type!='role_permission'
    OR NOT EXISTS (
      SELECT 1 FROM roles role
      WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.object_id
        AND role.code!='admin' AND role.status='active'
        AND role.permission_revision=json_extract(NEW.after_summary_json,'$.permission_revision')
        AND role.matrix_sha256=json_extract(NEW.after_summary_json,'$.matrix_sha256')
        AND json_extract(NEW.before_summary_json,'$.permission_revision')+1=role.permission_revision
    )
  )
)
OR (NEW.object_type='role_permission' AND NEW.action!='role.permission_changed')
OR (
  NEW.action IN ('user.invited','invitation.accepted')
  AND (
    NEW.object_type!='invitation' OR NEW.object_name IS NOT NULL
    OR NOT EXISTS (
      SELECT 1
      FROM invitations invitation
      JOIN users actor
        ON actor.tenant_id=invitation.tenant_id AND actor.user_id=NEW.actor_user_id
      WHERE invitation.tenant_id=NEW.tenant_id
        AND invitation.invitation_id=NEW.object_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active'
        AND (
          (
            NEW.action='user.invited'
            AND invitation.status='pending'
            AND invitation.invited_by=NEW.actor_user_id
            AND invitation.created_at=NEW.created_at
            AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=0
            AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=5
            AND NOT EXISTS (
              SELECT 1 FROM json_each(NEW.after_summary_json) field
              WHERE field.key NOT IN (
                'email_fingerprint','role_code','status','expires_at','accepted_user_id'
              )
            )
            AND json_extract(NEW.after_summary_json,'$.email_fingerprint')
              =invitation.email_fingerprint
            AND json_extract(NEW.after_summary_json,'$.role_code')=invitation.role_code
            AND json_extract(NEW.after_summary_json,'$.status')='pending'
            AND json_extract(NEW.after_summary_json,'$.expires_at')=invitation.expires_at
            AND json_type(NEW.after_summary_json,'$.accepted_user_id')='null'
            AND json(NEW.changed_fields_json)
              =json('["email_fingerprint","expires_at","role_code","status"]')
          )
          OR
          (
            NEW.action='invitation.accepted'
            AND invitation.status='accepted'
            AND invitation.accepted_user_id=NEW.actor_user_id
            AND invitation.email=actor.email
            AND invitation.accepted_at=NEW.created_at
            AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=5
            AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=5
            AND NOT EXISTS (
              SELECT 1 FROM json_each(NEW.before_summary_json) field
              WHERE field.key NOT IN (
                'email_fingerprint','role_code','status','expires_at','accepted_user_id'
              )
            )
            AND NOT EXISTS (
              SELECT 1 FROM json_each(NEW.after_summary_json) field
              WHERE field.key NOT IN (
                'email_fingerprint','role_code','status','expires_at','accepted_user_id'
              )
            )
            AND json_extract(NEW.before_summary_json,'$.email_fingerprint')
              =invitation.email_fingerprint
            AND json_extract(NEW.before_summary_json,'$.role_code')=invitation.role_code
            AND json_extract(NEW.before_summary_json,'$.status')='pending'
            AND json_extract(NEW.before_summary_json,'$.expires_at')=invitation.expires_at
            AND json_type(NEW.before_summary_json,'$.accepted_user_id')='null'
            AND json_extract(NEW.after_summary_json,'$.email_fingerprint')
              =invitation.email_fingerprint
            AND json_extract(NEW.after_summary_json,'$.role_code')=invitation.role_code
            AND json_extract(NEW.after_summary_json,'$.status')='accepted'
            AND json_extract(NEW.after_summary_json,'$.expires_at')=invitation.expires_at
            AND json_extract(NEW.after_summary_json,'$.accepted_user_id')
              =invitation.accepted_user_id
            AND json(NEW.changed_fields_json)=json('["accepted_user_id","status"]')
          )
        )
    )
  )
)
OR (
  NEW.object_type='invitation'
  AND NEW.action NOT IN ('user.invited','invitation.accepted')
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;

CREATE TRIGGER trg_audit_logs_immutable_update
BEFORE UPDATE ON audit_logs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_immutable_delete
BEFORE DELETE ON audit_logs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;

-- 冲突守卫与唯一索引使用相同键，确保重复请求统一收敛为不可变事实冲突。
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
    OR (
      NEW.action IN ('user.invited','invitation.accepted')
      AND current.action IN ('user.invited','invitation.accepted')
      AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
)
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_prd050_rowid_insert
AFTER INSERT ON audit_logs FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;

-- 每条新 AuditLog 与业务事务一起获得唯一稳定导出水位，映射失败必须回滚原事实。
CREATE TRIGGER trg_audit_logs_export_sequence_insert_prd058
AFTER INSERT ON audit_logs FOR EACH ROW
BEGIN
  INSERT INTO audit_log_export_sequences(tenant_id,audit_log_id)
  VALUES(NEW.tenant_id,NEW.audit_log_id);
END;

-- source event 只保存不可逆邮箱指纹和业务关联，不复制邮箱、Token、密码或 Webhook 正文。
CREATE TABLE invitation_events (
  invitation_event_id TEXT PRIMARY KEY CHECK (
    length(invitation_event_id)=38 AND substr(invitation_event_id,1,6)='ivevt_'
    AND substr(invitation_event_id,7) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  invitation_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (
    event_type IN ('user.invited','invitation.accepted')
  ),
  actor_user_id TEXT NOT NULL,
  actor_role TEXT NOT NULL CHECK (
    actor_role IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  ),
  audit_log_id TEXT NOT NULL UNIQUE,
  request_id TEXT NOT NULL CHECK (
    length(request_id) BETWEEN 1 AND 128
    AND request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  trace_id TEXT NOT NULL CHECK (
    length(trace_id) BETWEEN 1 AND 160
    AND trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  payload_json TEXT NOT NULL CHECK (
    json_valid(payload_json) AND json_type(payload_json)='object'
    AND length(CAST(payload_json AS BLOB))<=8192
  ),
  delivery_status TEXT NOT NULL CHECK (
    delivery_status IN ('pending','dispatched','failed')
  ),
  last_error_code TEXT CHECK (
    last_error_code IS NULL OR last_error_code='webhook.delivery_unavailable'
  ),
  created_at TEXT NOT NULL CHECK (
    length(created_at)=20 AND created_at GLOB '????-??-??T??:??:??Z'
    AND substr(created_at,1,4) BETWEEN '0001' AND '9999'
    AND strftime('%Y-%m-%dT%H:%M:%SZ',created_at,'+0 seconds') IS NOT NULL
    AND strftime('%Y-%m-%dT%H:%M:%SZ',created_at,'+0 seconds')=created_at
  ),
  updated_at TEXT NOT NULL CHECK (
    length(updated_at)=20 AND updated_at GLOB '????-??-??T??:??:??Z'
    AND substr(updated_at,1,4) BETWEEN '0001' AND '9999'
    AND strftime('%Y-%m-%dT%H:%M:%SZ',updated_at,'+0 seconds') IS NOT NULL
    AND strftime('%Y-%m-%dT%H:%M:%SZ',updated_at,'+0 seconds')=updated_at
  ),
  dispatched_at TEXT CHECK (
    dispatched_at IS NULL OR (
      length(dispatched_at)=20 AND dispatched_at GLOB '????-??-??T??:??:??Z'
      AND substr(dispatched_at,1,4) BETWEEN '0001' AND '9999'
      AND strftime('%Y-%m-%dT%H:%M:%SZ',dispatched_at,'+0 seconds') IS NOT NULL
      AND strftime('%Y-%m-%dT%H:%M:%SZ',dispatched_at,'+0 seconds')=dispatched_at
    )
  ),
  UNIQUE (tenant_id,invitation_event_id),
  UNIQUE (tenant_id,invitation_id,event_type),
  UNIQUE (tenant_id,actor_user_id,request_id),
  FOREIGN KEY (tenant_id,invitation_id) REFERENCES invitations(tenant_id,invitation_id),
  FOREIGN KEY (tenant_id,actor_user_id) REFERENCES users(tenant_id,user_id),
  FOREIGN KEY (tenant_id,audit_log_id) REFERENCES audit_logs(tenant_id,audit_log_id),
  CHECK (unixepoch(updated_at)>=unixepoch(created_at)),
  CHECK (
    (delivery_status='pending' AND last_error_code IS NULL AND dispatched_at IS NULL
      AND updated_at=created_at)
    OR (delivery_status='dispatched' AND last_error_code IS NULL
      AND dispatched_at IS NOT NULL AND dispatched_at=updated_at)
    OR (delivery_status='failed' AND last_error_code='webhook.delivery_unavailable'
      AND dispatched_at IS NOT NULL AND dispatched_at=updated_at)
  )
);
CREATE INDEX idx_invitation_events_tenant_time_prd060
  ON invitation_events(tenant_id,created_at DESC,invitation_event_id DESC);
CREATE INDEX idx_invitation_events_tenant_status_prd060
  ON invitation_events(tenant_id,delivery_status,updated_at,invitation_event_id);

-- 初始事件必须由当前 Invitation、同租户 actor 和对应 AuditLog 完整复演，payload 精确六字段。
CREATE TRIGGER trg_invitation_events_owner_insert_prd060
BEFORE INSERT ON invitation_events FOR EACH ROW
WHEN NEW.delivery_status!='pending' OR NEW.last_error_code IS NOT NULL
OR NEW.dispatched_at IS NOT NULL OR NEW.updated_at!=NEW.created_at
OR NOT EXISTS (
  SELECT 1 FROM users actor
  WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.actor_user_id
    AND actor.role=NEW.actor_role AND actor.status='active'
)
OR NOT EXISTS (
  SELECT 1 FROM audit_logs audit
  WHERE audit.tenant_id=NEW.tenant_id AND audit.audit_log_id=NEW.audit_log_id
    AND audit.actor_user_id=NEW.actor_user_id AND audit.actor_role=NEW.actor_role
    AND audit.action=NEW.event_type AND audit.object_type='invitation'
    AND audit.object_id=NEW.invitation_id AND audit.object_name IS NULL
    AND audit.request_id=NEW.request_id AND audit.trace_id=NEW.trace_id
    AND audit.created_at=NEW.created_at
)
OR NOT EXISTS (
  SELECT 1 FROM invitations invitation
  WHERE invitation.tenant_id=NEW.tenant_id
    AND invitation.invitation_id=NEW.invitation_id
    AND (
      (
        NEW.event_type='user.invited'
        AND invitation.status='pending'
        AND invitation.invited_by=NEW.actor_user_id
        AND invitation.created_at=NEW.created_at
        AND (SELECT COUNT(*) FROM json_each(NEW.payload_json))=6
        AND (SELECT COUNT(DISTINCT key) FROM json_each(NEW.payload_json))=6
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.payload_json) field
          WHERE field.key NOT IN (
            'schema_version','invitation_id','email_fingerprint',
            'role_code','expires_at','invited_at'
          )
        )
        AND json_type(NEW.payload_json,'$.schema_version')='integer'
        AND json_extract(NEW.payload_json,'$.schema_version')=1
        AND json_extract(NEW.payload_json,'$.invitation_id')=invitation.invitation_id
        AND json_extract(NEW.payload_json,'$.email_fingerprint')
          =invitation.email_fingerprint
        AND json_extract(NEW.payload_json,'$.role_code')=invitation.role_code
        AND json_extract(NEW.payload_json,'$.expires_at')=invitation.expires_at
        AND json_extract(NEW.payload_json,'$.invited_at')=invitation.created_at
      )
      OR
      (
        NEW.event_type='invitation.accepted'
        AND invitation.status='accepted'
        AND invitation.accepted_user_id=NEW.actor_user_id
        AND invitation.accepted_at=NEW.created_at
        AND (SELECT COUNT(*) FROM json_each(NEW.payload_json))=6
        AND (SELECT COUNT(DISTINCT key) FROM json_each(NEW.payload_json))=6
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.payload_json) field
          WHERE field.key NOT IN (
            'schema_version','invitation_id','user_id',
            'email_fingerprint','role_code','accepted_at'
          )
        )
        AND json_type(NEW.payload_json,'$.schema_version')='integer'
        AND json_extract(NEW.payload_json,'$.schema_version')=1
        AND json_extract(NEW.payload_json,'$.invitation_id')=invitation.invitation_id
        AND json_extract(NEW.payload_json,'$.user_id')=invitation.accepted_user_id
        AND json_extract(NEW.payload_json,'$.email_fingerprint')
          =invitation.email_fingerprint
        AND json_extract(NEW.payload_json,'$.role_code')=invitation.role_code
        AND json_extract(NEW.payload_json,'$.accepted_at')=invitation.accepted_at
      )
    )
)
BEGIN SELECT RAISE(ABORT,'invitation_event_owner_mismatch'); END;

-- 冲突守卫后创建以优先返回不可变错误，避免重复 ID 被 owner 差异掩盖。
CREATE TRIGGER trg_invitation_events_conflict_insert_prd060
BEFORE INSERT ON invitation_events FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM invitation_events current
  WHERE current.invitation_event_id=NEW.invitation_event_id
     OR current.audit_log_id=NEW.audit_log_id
     OR (current.tenant_id=NEW.tenant_id
         AND current.invitation_id=NEW.invitation_id
         AND current.event_type=NEW.event_type)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'invitation_event_immutable'); END;

-- 投递状态只允许一次 pending -> dispatched/failed，业务证据和关联字段全部不可改写。
CREATE TRIGGER trg_invitation_events_state_update_prd060
BEFORE UPDATE ON invitation_events FOR EACH ROW
WHEN NOT (
  OLD.delivery_status='pending'
  AND NEW.delivery_status IN ('dispatched','failed')
  AND NEW.invitation_event_id=OLD.invitation_event_id
  AND NEW.tenant_id=OLD.tenant_id AND NEW.invitation_id=OLD.invitation_id
  AND NEW.event_type=OLD.event_type AND NEW.actor_user_id=OLD.actor_user_id
  AND NEW.actor_role=OLD.actor_role AND NEW.audit_log_id=OLD.audit_log_id
  AND NEW.request_id=OLD.request_id AND NEW.trace_id=OLD.trace_id
  AND NEW.payload_json=OLD.payload_json AND NEW.created_at=OLD.created_at
  AND NEW.dispatched_at=NEW.updated_at
  AND unixepoch(NEW.updated_at)>=unixepoch(OLD.updated_at)
  AND (
    (NEW.delivery_status='dispatched' AND NEW.last_error_code IS NULL)
    OR (NEW.delivery_status='failed'
      AND NEW.last_error_code='webhook.delivery_unavailable')
  )
)
BEGIN SELECT RAISE(ABORT,'invitation_event_immutable'); END;
CREATE TRIGGER trg_invitation_events_delete_prd060
BEFORE DELETE ON invitation_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'invitation_event_immutable'); END;
CREATE TRIGGER trg_invitation_events_rowid_insert_prd060
AFTER INSERT ON invitation_events FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'invitation_event_immutable'); END;

-- 提交前逐表复核 0050 证据的 rowid、全列、NULL 分布、稳定水位和全部外键。
CREATE TEMP TABLE prd060_final_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd060_final_guard
BEFORE INSERT ON prd060_final_guard FOR EACH ROW WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd060_final_schema_invalid'); END;
INSERT INTO prd060_final_guard(invalid_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='table' AND name IN (
        'invitations','invitation_events','audit_logs','audit_log_export_sequences',
        'audit_exports','audit_export_events','role_permission_events'
      ))=7 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('invitations'))=13
      AND (SELECT COUNT(*) FROM pragma_table_info('invitation_events'))=15
      AND (SELECT COUNT(*) FROM pragma_table_info('audit_logs'))=17
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='index' AND name IN (
        'idx_invitations_tenant_time_prd060','idx_invitations_tenant_status_prd060',
        'idx_invitations_pending_email_prd060','idx_audit_logs_invitation_request_unique',
        'idx_invitation_events_tenant_time_prd060',
        'idx_invitation_events_tenant_status_prd060'
      ))=6 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name IN (
        'trg_invitations_owner_insert_prd060','trg_invitations_conflict_insert_prd060',
        'trg_invitations_state_update_prd060','trg_invitations_delete_prd060',
        'trg_invitations_rowid_insert_prd060',
        'trg_audit_log_export_sequences_owner_insert_prd058',
        'trg_audit_exports_owner_insert_prd058','trg_audit_exports_conflict_insert_prd058',
        'trg_role_permission_events_owner_insert_prd059','trg_audit_logs_owner_insert',
        'trg_audit_logs_immutable_update','trg_audit_logs_immutable_delete',
        'trg_audit_logs_prd050_conflict_insert','trg_audit_logs_prd050_rowid_insert',
        'trg_audit_logs_export_sequence_insert_prd058',
        'trg_invitation_events_owner_insert_prd060',
        'trg_invitation_events_conflict_insert_prd060',
        'trg_invitation_events_state_update_prd060',
        'trg_invitation_events_delete_prd060','trg_invitation_events_rowid_insert_prd060'
      ))=20 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs)
      =(SELECT COUNT(*) FROM audit_log_export_sequences) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM invitations)=0
      AND (SELECT COUNT(*) FROM invitation_events)=0 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd060_audit_before
      EXCEPT SELECT rowid,* FROM audit_logs
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs
      EXCEPT SELECT * FROM prd060_audit_before
    ))
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd060_sequences_before
      EXCEPT SELECT rowid,* FROM audit_log_export_sequences
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_log_export_sequences
      EXCEPT SELECT * FROM prd060_sequences_before
    ))
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd060_exports_before
      EXCEPT SELECT rowid,* FROM audit_exports
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_exports
      EXCEPT SELECT * FROM prd060_exports_before
    ))
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd060_export_events_before
      EXCEPT SELECT rowid,* FROM audit_export_events
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_export_events
      EXCEPT SELECT * FROM prd060_export_events_before
    ))
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd060_role_events_before
      EXCEPT SELECT rowid,* FROM role_permission_events
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM role_permission_events
      EXCEPT SELECT * FROM prd060_role_events_before
    ))
  + (SELECT COUNT(*) FROM (
      SELECT * FROM prd060_sqlite_sequence_before
      EXCEPT SELECT name,seq FROM sqlite_sequence
      WHERE name='audit_log_export_sequences'
    ))
  + (SELECT COUNT(*) FROM (
      SELECT name,seq FROM sqlite_sequence
      WHERE name='audit_log_export_sequences'
      EXCEPT SELECT * FROM prd060_sqlite_sequence_before
    ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check);
DROP TRIGGER trg_prd060_final_guard;
DROP TABLE prd060_final_guard;

DROP TABLE prd060_audit_before;
DROP TABLE prd060_sequences_before;
DROP TABLE prd060_exports_before;
DROP TABLE prd060_export_events_before;
DROP TABLE prd060_role_events_before;
DROP TABLE prd060_sqlite_sequence_before;

COMMIT;
PRAGMA foreign_keys = ON;
