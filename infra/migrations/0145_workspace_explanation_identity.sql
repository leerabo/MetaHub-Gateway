BEGIN IMMEDIATE;

CREATE TEMP TABLE explanation_identity_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO explanation_identity_guard SELECT CASE WHEN (
  SELECT version FROM schema_migrations WHERE status='applied' ORDER BY version DESC LIMIT 1
)='0144' THEN 0 ELSE 1 END;
INSERT INTO explanation_identity_guard
SELECT count(*) FROM sqlite_schema schema JOIN pragma_foreign_key_list(schema.name) fk
WHERE schema.type='table' AND fk."table"='request_explanations';
INSERT INTO explanation_identity_guard
WITH expected(name,sha256) AS (VALUES ('request_explanations','a8a4d2fd3d0ce2e5203cf356925e0ed6c1a703e55ab571f1ff720f8b8abbf2bb'),
('idx_request_explanations_request_time_prd054','f80c1892bacbdfc7f3ee4b22d60100bacf5c5b553cb3183bec74e93917bc6038'),
('idx_request_explanations_actor_time_prd054','10ea4df3e5507357a6b0c85f4f6a43cfab3009a1a1f00b29b5e2ab8ba4b4bca2'),
('idx_request_explanations_pending_event_prd054','cf32fff4a31343173f2a40f6847ea772e26d6a92f05c226a8dfd873435cd047d'),
('trg_request_explanations_owner_insert_prd054','d67ff8f0d3e646674b6687d9bac4e78a87fb863a1c373af981adede632ede79e'),
('trg_request_explanations_conflict_insert_prd054','9f1a7e3a54a441c9ceedf900c6669f490fcbc13f62f5807532f57e868e68f085'),
('trg_request_explanations_rowid_insert_prd054','1a0061627c7f76c492304a4245caff7fd01fd877c84e6c7d8f9e208b6a3bd9b6'),
('trg_request_explanations_state_update_prd054','a78d2b9478da13319623704cdf3f99854899852b74c47b267eb18f66d1f1de2c'),
('trg_request_explanations_immutable_delete_prd054','2703791bc5b6a130842d218f9f1a7bd3017c9c057f0827cfa39afa149863a4dd'))
SELECT count(*) FROM expected LEFT JOIN sqlite_schema actual USING(name)
WHERE actual.sql IS NULL OR metahub_sha256(actual.sql)<>expected.sha256;
INSERT INTO explanation_identity_guard SELECT CASE WHEN count(*)=9 THEN 0 ELSE 1 END
FROM sqlite_schema WHERE tbl_name='request_explanations' AND sql IS NOT NULL;
INSERT INTO explanation_identity_guard SELECT count(*) FROM tenant_deletion_trigger_manifest manifest
LEFT JOIN sqlite_schema actual ON actual.name=manifest.trigger_name
WHERE manifest.source_table='request_explanations' AND (actual.sql IS NULL OR actual.sql<>manifest.trigger_sql
  OR metahub_sha256(actual.sql)<>manifest.trigger_sha256);
INSERT INTO explanation_identity_guard SELECT CASE WHEN count(*)=1 THEN 0 ELSE 1 END
FROM tenant_deletion_table_manifest WHERE source_table='request_explanations';
INSERT INTO explanation_identity_guard SELECT CASE WHEN count(*)=5 THEN 0 ELSE 1 END
FROM tenant_deletion_trigger_manifest WHERE source_table='request_explanations';

CREATE TEMP TABLE explanation_identity_history AS SELECT rowid,request_explanation_id,event_id,tenant_id,source_request_id,source_trace_id,actor_user_id,actor_session_id,source_status,status,evidence_version,section_status_json,missing_sections_json,truncated_sections_json,source_created_at,source_updated_at,operation_request_id,operation_trace_id,event_dispatch_status,event_dispatch_attempts,event_next_attempt_at,event_dispatched_at,event_claim_token,event_lease_owner_id,event_lease_expires_at,event_last_error_code,viewed_at,created_at,updated_at FROM request_explanations;
DROP TABLE request_explanations;
CREATE TABLE request_explanations (
  request_explanation_id TEXT PRIMARY KEY CHECK (
    length(request_explanation_id)=36
    AND substr(request_explanation_id, 1, 4)='rxe_'
    AND substr(request_explanation_id, 5) NOT GLOB '*[^0-9a-f]*'
  ),
  event_id TEXT NOT NULL UNIQUE CHECK (
    length(event_id)=38
    AND substr(event_id, 1, 6)='rxevt_'
    AND substr(event_id, 7) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  source_request_id TEXT NOT NULL CHECK (
    length(source_request_id) BETWEEN 1 AND 128
    AND source_request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  source_trace_id TEXT NOT NULL CHECK (
    length(source_trace_id) BETWEEN 1 AND 128
    AND source_trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  actor_user_id TEXT NOT NULL,
  actor_session_id TEXT,
  source_status TEXT NOT NULL CHECK (source_status IN ('success', 'failed')),
  status TEXT NOT NULL CHECK (status IN ('complete', 'partial')),
  evidence_version TEXT NOT NULL DEFAULT 'request_explanation_v1'
    CHECK (evidence_version='request_explanation_v1'),
  section_status_json TEXT NOT NULL CHECK (
    json_valid(section_status_json) AND json_type(section_status_json)='object'
  ),
  missing_sections_json TEXT NOT NULL CHECK (
    json_valid(missing_sections_json) AND json_type(missing_sections_json)='array'
  ),
  truncated_sections_json TEXT NOT NULL CHECK (
    json_valid(truncated_sections_json) AND json_type(truncated_sections_json)='array'
  ),
  source_created_at TEXT NOT NULL CHECK (unixepoch(source_created_at) IS NOT NULL),
  source_updated_at TEXT NOT NULL CHECK (
    unixepoch(source_updated_at) IS NOT NULL
    AND unixepoch(source_updated_at)>=unixepoch(source_created_at)
  ),
  operation_request_id TEXT NOT NULL CHECK (
    length(operation_request_id) BETWEEN 1 AND 128
    AND operation_request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  operation_trace_id TEXT NOT NULL CHECK (
    length(operation_trace_id) BETWEEN 1 AND 128
    AND operation_trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  event_dispatch_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (event_dispatch_status IN ('pending', 'processing', 'dispatched', 'failed')),
  event_dispatch_attempts INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(event_dispatch_attempts)='integer'
    AND event_dispatch_attempts BETWEEN 0 AND 10
  ),
  event_next_attempt_at TEXT,
  event_dispatched_at TEXT,
  event_claim_token TEXT CHECK (
    event_claim_token IS NULL
    OR (length(event_claim_token) BETWEEN 16 AND 128 AND instr(event_claim_token, char(0))=0)
  ),
  event_lease_owner_id TEXT CHECK (
    event_lease_owner_id IS NULL
    OR (length(event_lease_owner_id) BETWEEN 1 AND 128 AND instr(event_lease_owner_id, char(0))=0)
  ),
  event_lease_expires_at TEXT,
  event_last_error_code TEXT CHECK (
    event_last_error_code IS NULL OR event_last_error_code IN (
      'webhook.event_type_invalid',
      'webhook.payload_invalid',
      'webhook.payload_sensitive',
      'webhook.delivery_unavailable',
      'request_explanation.event_unavailable'
    )
  ),
  viewed_at TEXT NOT NULL CHECK (unixepoch(viewed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK (unixepoch(updated_at) IS NOT NULL),
  workspace_session_id TEXT REFERENCES workspace_sessions(workspace_session_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, source_request_id) REFERENCES request_logs(tenant_id, request_id),
  FOREIGN KEY (tenant_id, actor_user_id) REFERENCES users(tenant_id, user_id),
  FOREIGN KEY (actor_session_id) REFERENCES sessions(session_id),
  UNIQUE (tenant_id, request_explanation_id),
  UNIQUE (tenant_id, event_id),
  CHECK (viewed_at=created_at),
  CHECK (
    (event_dispatch_status='pending'
      AND event_dispatch_attempts BETWEEN 0 AND 9
      AND event_next_attempt_at IS NOT NULL
      AND unixepoch(event_next_attempt_at) IS NOT NULL
      AND event_dispatched_at IS NULL
      AND event_claim_token IS NULL AND event_lease_owner_id IS NULL
      AND event_lease_expires_at IS NULL)
    OR (event_dispatch_status='processing'
      AND event_dispatch_attempts BETWEEN 1 AND 10
      AND event_next_attempt_at IS NULL AND event_dispatched_at IS NULL
      AND event_claim_token IS NOT NULL AND event_lease_owner_id IS NOT NULL
      AND event_lease_expires_at IS NOT NULL
      AND unixepoch(event_lease_expires_at)>unixepoch(updated_at))
    OR (event_dispatch_status='dispatched'
      AND event_dispatched_at IS NOT NULL
      AND unixepoch(event_dispatched_at) IS NOT NULL
      AND event_next_attempt_at IS NULL AND event_claim_token IS NULL
      AND event_lease_owner_id IS NULL AND event_lease_expires_at IS NULL
      AND event_last_error_code IS NULL)
    OR (event_dispatch_status='failed'
      AND event_dispatch_attempts=10 AND event_dispatched_at IS NULL
      AND event_next_attempt_at IS NULL AND event_claim_token IS NULL
      AND event_lease_owner_id IS NULL AND event_lease_expires_at IS NULL
      AND event_last_error_code IS NOT NULL)
  )
,
  CONSTRAINT ck_explanation_session_domain_prd147 CHECK(
    (actor_session_id IS NULL)<>(workspace_session_id IS NULL))
);
INSERT INTO request_explanations(rowid,request_explanation_id,event_id,tenant_id,source_request_id,source_trace_id,actor_user_id,actor_session_id,source_status,status,evidence_version,section_status_json,missing_sections_json,truncated_sections_json,source_created_at,source_updated_at,operation_request_id,operation_trace_id,event_dispatch_status,event_dispatch_attempts,event_next_attempt_at,event_dispatched_at,event_claim_token,event_lease_owner_id,event_lease_expires_at,event_last_error_code,viewed_at,created_at,updated_at) SELECT rowid,request_explanation_id,event_id,tenant_id,source_request_id,source_trace_id,actor_user_id,actor_session_id,source_status,status,evidence_version,section_status_json,missing_sections_json,truncated_sections_json,source_created_at,source_updated_at,operation_request_id,operation_trace_id,event_dispatch_status,event_dispatch_attempts,event_next_attempt_at,event_dispatched_at,event_claim_token,event_lease_owner_id,event_lease_expires_at,event_last_error_code,viewed_at,created_at,updated_at FROM explanation_identity_history;
INSERT INTO explanation_identity_guard SELECT count(*) FROM (
  SELECT rowid,request_explanation_id,event_id,tenant_id,source_request_id,source_trace_id,actor_user_id,actor_session_id,source_status,status,evidence_version,section_status_json,missing_sections_json,truncated_sections_json,source_created_at,source_updated_at,operation_request_id,operation_trace_id,event_dispatch_status,event_dispatch_attempts,event_next_attempt_at,event_dispatched_at,event_claim_token,event_lease_owner_id,event_lease_expires_at,event_last_error_code,viewed_at,created_at,updated_at FROM request_explanations EXCEPT SELECT rowid,request_explanation_id,event_id,tenant_id,source_request_id,source_trace_id,actor_user_id,actor_session_id,source_status,status,evidence_version,section_status_json,missing_sections_json,truncated_sections_json,source_created_at,source_updated_at,operation_request_id,operation_trace_id,event_dispatch_status,event_dispatch_attempts,event_next_attempt_at,event_dispatched_at,event_claim_token,event_lease_owner_id,event_lease_expires_at,event_last_error_code,viewed_at,created_at,updated_at FROM explanation_identity_history);
INSERT INTO explanation_identity_guard SELECT count(*) FROM (
  SELECT rowid,request_explanation_id,event_id,tenant_id,source_request_id,source_trace_id,actor_user_id,actor_session_id,source_status,status,evidence_version,section_status_json,missing_sections_json,truncated_sections_json,source_created_at,source_updated_at,operation_request_id,operation_trace_id,event_dispatch_status,event_dispatch_attempts,event_next_attempt_at,event_dispatched_at,event_claim_token,event_lease_owner_id,event_lease_expires_at,event_last_error_code,viewed_at,created_at,updated_at FROM explanation_identity_history EXCEPT SELECT rowid,request_explanation_id,event_id,tenant_id,source_request_id,source_trace_id,actor_user_id,actor_session_id,source_status,status,evidence_version,section_status_json,missing_sections_json,truncated_sections_json,source_created_at,source_updated_at,operation_request_id,operation_trace_id,event_dispatch_status,event_dispatch_attempts,event_next_attempt_at,event_dispatched_at,event_claim_token,event_lease_owner_id,event_lease_expires_at,event_last_error_code,viewed_at,created_at,updated_at FROM request_explanations);
DROP TABLE explanation_identity_history;
CREATE INDEX idx_request_explanations_request_time_prd054
  ON request_explanations(tenant_id, source_request_id, viewed_at DESC, request_explanation_id DESC);
CREATE INDEX idx_request_explanations_actor_time_prd054
  ON request_explanations(tenant_id, actor_user_id, viewed_at DESC, request_explanation_id DESC);
CREATE INDEX idx_request_explanations_pending_event_prd054
  ON request_explanations(
    event_dispatch_status, event_next_attempt_at, event_lease_expires_at, created_at,
    request_explanation_id
  )
  WHERE event_dispatch_status IN ('pending', 'processing');
CREATE TRIGGER trg_request_explanations_owner_insert_prd054
BEFORE INSERT ON request_explanations
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM request_logs request
  WHERE request.tenant_id=NEW.tenant_id
    AND request.request_id=NEW.source_request_id
    AND request.trace_id=NEW.source_trace_id
    AND request.status=NEW.source_status
    AND request.created_at=NEW.source_created_at
    AND coalesce(request.updated_at, request.created_at)=NEW.source_updated_at
)
OR NOT EXISTS (
  SELECT 1 FROM users user
  WHERE user.tenant_id=NEW.tenant_id AND user.user_id=NEW.actor_user_id
)
OR NOT (
  (NEW.workspace_session_id IS NULL AND NEW.actor_session_id IS NOT NULL AND EXISTS (
  SELECT 1 FROM sessions session
  WHERE session.tenant_id=NEW.tenant_id
    AND session.user_id=NEW.actor_user_id
    AND session.session_id=NEW.actor_session_id
))
  OR (NEW.actor_session_id IS NULL AND NEW.workspace_session_id IS NOT NULL AND EXISTS(
  SELECT 1 FROM workspace_sessions session
  JOIN workspace_memberships member ON member.workspace_membership_id=session.membership_id
  JOIN workspace_tenant_bindings binding ON binding.workspace_id=session.workspace_id
  JOIN users actor ON actor.user_id=member.legacy_user_id
  WHERE session.workspace_session_id=NEW.workspace_session_id
    AND session.tenant_id=NEW.tenant_id AND member.tenant_id=NEW.tenant_id
    AND binding.tenant_id=NEW.tenant_id AND actor.tenant_id=NEW.tenant_id
    AND member.workspace_id=session.workspace_id AND member.account_id=session.account_id
    AND member.legacy_user_id=NEW.actor_user_id
))
)
OR (SELECT COUNT(*) FROM json_each(NEW.section_status_json))!=11
OR EXISTS (
  SELECT 1 FROM json_each(NEW.section_status_json) section
  WHERE section.key NOT IN (
    'authentication', 'rate_limit', 'budget', 'dlp', 'policy', 'routing',
    'keypool', 'provider_response', 'usage_and_finops', 'logs', 'webhooks'
  )
  OR section.type!='text'
  OR section.value NOT IN ('complete', 'partial', 'not_recorded', 'not_applicable', 'pending')
)
OR EXISTS (
  SELECT 1 FROM json_each(NEW.missing_sections_json) section
  WHERE section.type!='text' OR section.value NOT IN (
    'authentication', 'rate_limit', 'budget', 'dlp', 'policy', 'routing',
    'keypool', 'provider_response', 'usage_and_finops', 'logs', 'webhooks'
  )
)
OR EXISTS (
  SELECT 1 FROM json_each(NEW.truncated_sections_json) section
  WHERE section.type!='text' OR section.value NOT IN (
    'authentication', 'rate_limit', 'budget', 'dlp', 'policy', 'routing',
    'keypool', 'provider_response', 'usage_and_finops', 'logs', 'webhooks'
  )
)
OR (SELECT COUNT(*) FROM json_each(NEW.missing_sections_json))
  !=(SELECT COUNT(DISTINCT value) FROM json_each(NEW.missing_sections_json))
OR (SELECT COUNT(*) FROM json_each(NEW.truncated_sections_json))
  !=(SELECT COUNT(DISTINCT value) FROM json_each(NEW.truncated_sections_json))
OR EXISTS (
  SELECT 1
  FROM json_each(NEW.missing_sections_json) missing
  JOIN json_each(NEW.truncated_sections_json) truncated ON truncated.value=missing.value
)
OR (
  NEW.status='complete'
  AND (
    json_array_length(NEW.missing_sections_json)!=0
    OR json_array_length(NEW.truncated_sections_json)!=0
  )
)
OR (
  NEW.status='partial'
  AND json_array_length(NEW.missing_sections_json)=0
  AND json_array_length(NEW.truncated_sections_json)=0
)
BEGIN
  SELECT RAISE(ABORT, 'request_explanation_owner_mismatch');
END;
CREATE TRIGGER trg_request_explanations_conflict_insert_prd054
BEFORE INSERT ON request_explanations
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM request_explanations current
  WHERE current.request_explanation_id=NEW.request_explanation_id
    OR current.event_id=NEW.event_id
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN
  SELECT RAISE(ABORT, 'request_explanation_immutable');
END;
CREATE TRIGGER trg_request_explanations_rowid_insert_prd054
AFTER INSERT ON request_explanations
FOR EACH ROW
WHEN NEW.rowid<0
BEGIN
  SELECT RAISE(ABORT, 'request_explanation_immutable');
END;
CREATE TRIGGER trg_request_explanations_state_update_prd054
BEFORE UPDATE ON request_explanations
FOR EACH ROW
WHEN NOT (
  NEW.rowid=OLD.rowid
  AND NEW.request_explanation_id=OLD.request_explanation_id
  AND NEW.event_id=OLD.event_id
  AND NEW.tenant_id=OLD.tenant_id
  AND NEW.source_request_id=OLD.source_request_id
  AND NEW.source_trace_id=OLD.source_trace_id
  AND NEW.actor_user_id=OLD.actor_user_id
  AND NEW.actor_session_id IS OLD.actor_session_id
  AND NEW.workspace_session_id IS OLD.workspace_session_id
  AND NEW.source_status=OLD.source_status
  AND NEW.status=OLD.status
  AND NEW.evidence_version=OLD.evidence_version
  AND NEW.section_status_json=OLD.section_status_json
  AND NEW.missing_sections_json=OLD.missing_sections_json
  AND NEW.truncated_sections_json=OLD.truncated_sections_json
  AND NEW.source_created_at=OLD.source_created_at
  AND NEW.source_updated_at=OLD.source_updated_at
  AND NEW.operation_request_id=OLD.operation_request_id
  AND NEW.operation_trace_id=OLD.operation_trace_id
  AND NEW.viewed_at=OLD.viewed_at
  AND NEW.created_at=OLD.created_at
  AND unixepoch(NEW.updated_at)>=unixepoch(OLD.updated_at)
  AND (
    (OLD.event_dispatch_status='pending' AND NEW.event_dispatch_status='processing'
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts+1
      AND unixepoch(OLD.event_next_attempt_at)<=unixepoch(NEW.updated_at)
      AND NEW.event_next_attempt_at IS NULL
      AND NEW.event_claim_token IS NOT OLD.event_claim_token)
    OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='processing'
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts+1
      AND unixepoch(OLD.event_lease_expires_at)<=unixepoch(NEW.updated_at)
      AND NEW.event_next_attempt_at IS NULL
      AND NEW.event_claim_token IS NOT OLD.event_claim_token)
    OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='pending'
      AND OLD.event_dispatch_attempts<10
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts
      AND NEW.event_next_attempt_at IS NOT NULL
      AND unixepoch(NEW.event_next_attempt_at)>unixepoch(NEW.updated_at)
      AND NEW.event_claim_token IS NULL AND NEW.event_lease_owner_id IS NULL
      AND NEW.event_lease_expires_at IS NULL
      AND NEW.event_last_error_code IS NOT NULL)
    OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='dispatched'
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts
      AND NEW.event_dispatched_at IS NOT NULL
      AND NEW.event_next_attempt_at IS NULL
      AND NEW.event_claim_token IS NULL AND NEW.event_lease_owner_id IS NULL
      AND NEW.event_lease_expires_at IS NULL
      AND NEW.event_last_error_code IS NULL)
    OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='failed'
      AND NEW.event_dispatch_attempts=10
      AND NEW.event_next_attempt_at IS NULL
      AND NEW.event_claim_token IS NULL AND NEW.event_lease_owner_id IS NULL
      AND NEW.event_lease_expires_at IS NULL
      AND NEW.event_last_error_code IS NOT NULL)
  )
)
BEGIN
  SELECT RAISE(ABORT, 'request_explanation_state_invalid');
END;
CREATE TRIGGER trg_request_explanations_immutable_delete_prd054
BEFORE DELETE ON request_explanations
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'request_explanation_immutable');
END;
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
UPDATE tenant_deletion_table_manifest SET
  table_sql_sha256=metahub_sha256((SELECT sql FROM sqlite_schema WHERE name='request_explanations')),
  column_signature_sha256=metahub_sha256((SELECT group_concat(record,char(30)) FROM(
    SELECT cid||char(31)||name||char(31)||type||char(31)||"notnull"||char(31)
      ||coalesce(dflt_value,'')||char(31)||pk AS record
    FROM pragma_table_info('request_explanations') ORDER BY cid))),
  trigger_set_sha256=metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
    SELECT name||char(31)||metahub_sha256(sql) AS record FROM sqlite_schema
    WHERE type='trigger' AND tbl_name='request_explanations' ORDER BY name)),''))
WHERE source_table='request_explanations';
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
UPDATE tenant_deletion_trigger_manifest SET
  trigger_sql=(SELECT sql FROM sqlite_schema WHERE name=trigger_name),
  trigger_sha256=metahub_sha256((SELECT sql FROM sqlite_schema WHERE name=trigger_name))
WHERE source_table='request_explanations';
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
INSERT INTO explanation_identity_guard SELECT count(*) FROM pragma_foreign_key_check;
DROP TABLE explanation_identity_guard;
COMMIT;
