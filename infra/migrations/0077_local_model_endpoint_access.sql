-- PRD-084: tenant-owned local model Endpoint aggregate, call facts and durable outbox.
-- Endpoint URLs remain on the existing Provider row; this migration never stores credentials,
-- headers, prompts, responses, resolved addresses or raw upstream payloads in new fact tables.
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- Fail closed unless the exact 0076 schema, RBAC snapshot and Adapter v1 activation are present.
DROP TABLE IF EXISTS temp.prd084_up_guard;
CREATE TEMP TABLE prd084_up_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd084_up_guard_abort
BEFORE INSERT ON prd084_up_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd084_up_guard_failed'); END;
INSERT INTO prd084_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT version FROM schema_migrations
      WHERE status='applied' ORDER BY version DESC LIMIT 1)='0076' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=85
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=18)
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM (
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||description||
          char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code
      ))='08eee7f1eb3c329ae7d27f205afdd47aa69db4d702647a09c75271aac0433492'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=224
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM (
        SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code
      ))='cae76323d18bad139ecfd04950e2b3c50d37916dd41a2427a93bc3927acc7a84'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM tenants)=
      (SELECT COUNT(*) FROM provider_adapter_catalog_activations
       WHERE status='active' AND catalog_version='adapter-spec-v1'
         AND catalog_hash='9757f206d4bd0ec625d39173d4c5e5bd1f6d36132b3117008ffb1e5e7ba69870'
         AND spec_count=9)
      AND NOT EXISTS(
        SELECT 1 FROM tenants tenant
        WHERE (SELECT COUNT(*) FROM provider_adapter_catalog_activations activation
          WHERE activation.tenant_id=tenant.tenant_id AND activation.status='active')!=1)
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE tbl_name='providers'
      AND type='index')=10 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE tbl_name='providers'
      AND type='trigger' AND name IN(
        'trg_policy_template_provider_delete_prd068',
        'trg_policy_template_provider_update_prd068',
        'trg_providers_experiment_arm_delete_prd075',
        'trg_providers_experiment_arm_update_prd075'))=4 THEN 0 ELSE 1 END
  + CASE WHEN instr((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='providers'),'''migration_compatible''')>0
      AND instr((SELECT sql FROM sqlite_schema
        WHERE type='table' AND name='providers'),'''local_endpoint''')=0
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(SELECT 1 FROM pragma_table_info('routing_attempts')
      WHERE name IN('provider_call_status','provider_call_started_at'))
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name IN(
      'local_model_endpoints','local_model_catalog_entries',
      'local_endpoint_create_receipts','local_model_call_events',
      'local_model_call_event_outbox'))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd084_up_guard_abort;
DROP TABLE prd084_up_guard;

-- Rebuild providers with the sole semantic change of admitting literal local_endpoint.
DROP TRIGGER trg_policy_template_provider_delete_prd068;
DROP TRIGGER trg_policy_template_provider_update_prd068;
DROP TRIGGER trg_providers_experiment_arm_delete_prd075;
DROP TRIGGER trg_providers_experiment_arm_update_prd075;

CREATE TABLE providers_prd084_new (
  provider_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  provider_code TEXT NOT NULL,
  name TEXT NOT NULL,
  base_url TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('active','disabled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  provider_type TEXT NOT NULL DEFAULT 'openai_compatible' CHECK(provider_type IN(
    'openai_compatible','zhipu','dashscope','wenxin','volcengine','hunyuan',
    'deepseek','custom','migration_compatible','local_endpoint')),
  auth_type TEXT NOT NULL DEFAULT 'bearer' CHECK(auth_type IN('none','bearer','custom_header')),
  description TEXT,
  health_status TEXT NOT NULL DEFAULT 'unknown' CHECK(health_status IN('unknown','healthy','unhealthy')),
  last_tested_at TEXT,
  last_test_error TEXT,
  created_by TEXT,
  disabled_at TEXT,
  disabled_by TEXT,
  last_success_at TEXT,
  last_failure_at TEXT,
  last_probe_latency_ms INTEGER CHECK(last_probe_latency_ms IS NULL OR last_probe_latency_ms>=0),
  last_probe_http_status INTEGER CHECK(last_probe_http_status IS NULL OR last_probe_http_status BETWEEN 100 AND 599),
  last_probe_error_code TEXT,
  consecutive_probe_failures INTEGER NOT NULL DEFAULT 0 CHECK(consecutive_probe_failures>=0),
  health_generation INTEGER NOT NULL DEFAULT 0 CHECK(health_generation>=0),
  health_updated_at TEXT,
  next_probe_at TEXT,
  probe_lease_token TEXT,
  probe_lease_until TEXT,
  maintenance_note TEXT CHECK(maintenance_note IS NULL OR(
    typeof(maintenance_note)='text' AND length(maintenance_note) BETWEEN 1 AND 240
    AND maintenance_note=trim(maintenance_note) AND instr(maintenance_note,char(0))=0
    AND maintenance_note NOT GLOB(
      '*['||char(1)||'-'||char(31)||char(127)||'-'||char(159)||']*'))),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  UNIQUE(tenant_id,provider_code)
);
INSERT INTO providers_prd084_new(
  rowid,provider_id,tenant_id,provider_code,name,base_url,status,created_at,updated_at,
  provider_type,auth_type,description,health_status,last_tested_at,last_test_error,
  created_by,disabled_at,disabled_by,last_success_at,last_failure_at,
  last_probe_latency_ms,last_probe_http_status,last_probe_error_code,
  consecutive_probe_failures,health_generation,health_updated_at,next_probe_at,
  probe_lease_token,probe_lease_until,maintenance_note
)
SELECT rowid,provider_id,tenant_id,provider_code,name,base_url,status,created_at,updated_at,
  provider_type,auth_type,description,health_status,last_tested_at,last_test_error,
  created_by,disabled_at,disabled_by,last_success_at,last_failure_at,
  last_probe_latency_ms,last_probe_http_status,last_probe_error_code,
  consecutive_probe_failures,health_generation,health_updated_at,next_probe_at,
  probe_lease_token,probe_lease_until,maintenance_note
FROM providers;
DROP TABLE providers;
ALTER TABLE providers_prd084_new RENAME TO providers;

CREATE INDEX idx_providers_tenant_id ON providers(tenant_id);
CREATE INDEX idx_providers_provider_type ON providers(tenant_id,provider_type);
CREATE INDEX idx_providers_health_status ON providers(tenant_id,health_status);
CREATE INDEX idx_provider_health_due ON providers(status,next_probe_at,probe_lease_until);
CREATE INDEX idx_provider_health_tenant_status
  ON providers(tenant_id,health_status,health_updated_at);
CREATE UNIQUE INDEX uq_providers_tenant_id_prd047 ON providers(tenant_id,provider_id);
CREATE UNIQUE INDEX uq_providers_tenant_provider_prd051 ON providers(tenant_id,provider_id);
CREATE UNIQUE INDEX idx_providers_tenant_provider_owner_prd056
  ON providers(tenant_id,provider_id);

CREATE TRIGGER trg_policy_template_provider_update_prd068
BEFORE UPDATE ON providers FOR EACH ROW
WHEN EXISTS(
  SELECT 1 FROM policy_template_application_dependencies dependency
  JOIN policy_template_applications application
    ON application.tenant_id=dependency.tenant_id
   AND application.application_id=dependency.application_id
  WHERE dependency.tenant_id=OLD.tenant_id AND dependency.resource_type='provider'
    AND dependency.resource_id=OLD.provider_id AND application.status='applied')
AND(NEW.rowid!=OLD.rowid OR NEW.provider_id!=OLD.provider_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.provider_type!=OLD.provider_type
  OR NEW.base_url!=OLD.base_url OR NEW.auth_type!=OLD.auth_type
  OR(OLD.status!='active' AND NEW.status='active'))
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;
CREATE TRIGGER trg_policy_template_provider_delete_prd068
BEFORE DELETE ON providers FOR EACH ROW
WHEN EXISTS(
  SELECT 1 FROM policy_template_application_dependencies dependency
  JOIN policy_template_applications application
    ON application.tenant_id=dependency.tenant_id
   AND application.application_id=dependency.application_id
  WHERE dependency.tenant_id=OLD.tenant_id AND dependency.resource_type='provider'
    AND dependency.resource_id=OLD.provider_id AND application.status='applied')
BEGIN SELECT RAISE(ABORT,'policy_template_dependency_immutable'); END;
CREATE TRIGGER trg_providers_experiment_arm_update_prd075
BEFORE UPDATE ON providers FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM model_experiments experiment
    WHERE experiment.tenant_id=OLD.tenant_id AND experiment.status='active'
      AND OLD.provider_id IN(experiment.model_a_provider_id,experiment.model_b_provider_id))
  AND(NEW.rowid!=OLD.rowid OR NEW.provider_id!=OLD.provider_id
    OR NEW.tenant_id!=OLD.tenant_id OR NEW.provider_code!=OLD.provider_code
    OR NEW.status!=OLD.status OR NEW.provider_type!=OLD.provider_type
    OR NEW.base_url!=OLD.base_url OR NEW.auth_type!=OLD.auth_type)
BEGIN SELECT RAISE(ABORT,'experiment_arm_parent_immutable'); END;
CREATE TRIGGER trg_providers_experiment_arm_delete_prd075
BEFORE DELETE ON providers FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM model_experiments experiment
  WHERE experiment.tenant_id=OLD.tenant_id AND experiment.status='active'
    AND OLD.provider_id IN(experiment.model_a_provider_id,experiment.model_b_provider_id))
BEGIN SELECT RAISE(ABORT,'experiment_arm_parent_immutable'); END;

-- Existing rows are non-local, so the two added columns have a lossless legacy default.
ALTER TABLE routing_attempts ADD COLUMN provider_call_status TEXT NOT NULL
  DEFAULT 'not_applicable'
  CHECK(provider_call_status IN('not_applicable','not_called','called'));
ALTER TABLE routing_attempts ADD COLUMN provider_call_started_at TEXT CHECK(
  provider_call_started_at IS NULL OR(
    length(provider_call_started_at)=20 AND substr(provider_call_started_at,11,1)='T'
    AND substr(provider_call_started_at,20,1)='Z'));

CREATE TABLE local_model_endpoints(
  local_endpoint_id TEXT PRIMARY KEY CHECK(length(local_endpoint_id)=36
    AND substr(local_endpoint_id,1,4)='lme_'
    AND substr(local_endpoint_id,5) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  initial_model_id TEXT NOT NULL,
  engine_kind TEXT NOT NULL CHECK(engine_kind IN('openai_compatible','vllm','ollama')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(typeof(revision)='integer' AND revision>=1),
  catalog_revision INTEGER NOT NULL DEFAULT 0
    CHECK(typeof(catalog_revision)='integer' AND catalog_revision>=0),
  catalog_status TEXT NOT NULL DEFAULT 'unknown'
    CHECK(catalog_status IN('unknown','ready','error')),
  last_discovered_at TEXT CHECK(last_discovered_at IS NULL OR(
    length(last_discovered_at)=20 AND substr(last_discovered_at,11,1)='T'
    AND substr(last_discovered_at,20,1)='Z')),
  last_catalog_error_code TEXT CHECK(last_catalog_error_code IS NULL OR(
    length(last_catalog_error_code) BETWEEN 3 AND 96
    AND last_catalog_error_code=lower(last_catalog_error_code)
    AND last_catalog_error_code NOT GLOB '*[^a-z0-9._]*')),
  created_by TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN('active','disabled')),
  created_at TEXT NOT NULL CHECK(length(created_at)=20
    AND substr(created_at,11,1)='T' AND substr(created_at,20,1)='Z'),
  updated_at TEXT NOT NULL CHECK(length(updated_at)=20
    AND substr(updated_at,11,1)='T' AND substr(updated_at,20,1)='Z'),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(tenant_id,provider_id) REFERENCES providers(tenant_id,provider_id),
  FOREIGN KEY(tenant_id,initial_model_id) REFERENCES models(tenant_id,model_id),
  FOREIGN KEY(tenant_id,created_by) REFERENCES users(tenant_id,user_id),
  UNIQUE(tenant_id,local_endpoint_id),
  UNIQUE(tenant_id,provider_id),
  CHECK(updated_at>=created_at),
  CHECK((catalog_status='unknown' AND catalog_revision=0 AND last_discovered_at IS NULL
      AND last_catalog_error_code IS NULL)
    OR(catalog_status='ready' AND catalog_revision>=1 AND last_discovered_at IS NOT NULL
      AND last_catalog_error_code IS NULL)
    OR(catalog_status='error' AND last_catalog_error_code IS NOT NULL))
);
CREATE INDEX idx_local_model_endpoints_tenant_status_prd084
  ON local_model_endpoints(tenant_id,status,created_at DESC);

CREATE TABLE local_model_catalog_entries(
  local_model_catalog_entry_id TEXT PRIMARY KEY CHECK(
    length(local_model_catalog_entry_id)=38
    AND substr(local_model_catalog_entry_id,1,6)='lmcat_'
    AND substr(local_model_catalog_entry_id,7) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  local_endpoint_id TEXT NOT NULL,
  provider_model_id TEXT NOT NULL CHECK(length(provider_model_id) BETWEEN 1 AND 160
    AND provider_model_id=trim(provider_model_id) AND instr(provider_model_id,char(0))=0
    AND provider_model_id NOT GLOB(
      '*['||char(1)||'-'||char(31)||char(127)||'-'||char(159)||']*')),
  availability_status TEXT NOT NULL CHECK(availability_status IN('available','missing')),
  first_seen_revision INTEGER NOT NULL CHECK(
    typeof(first_seen_revision)='integer' AND first_seen_revision>=1),
  last_seen_revision INTEGER NOT NULL CHECK(
    typeof(last_seen_revision)='integer' AND last_seen_revision>=first_seen_revision),
  linked_model_id TEXT,
  created_at TEXT NOT NULL CHECK(length(created_at)=20
    AND substr(created_at,11,1)='T' AND substr(created_at,20,1)='Z'),
  updated_at TEXT NOT NULL CHECK(length(updated_at)=20
    AND substr(updated_at,11,1)='T' AND substr(updated_at,20,1)='Z'),
  FOREIGN KEY(tenant_id,local_endpoint_id)
    REFERENCES local_model_endpoints(tenant_id,local_endpoint_id),
  FOREIGN KEY(tenant_id,linked_model_id) REFERENCES models(tenant_id,model_id),
  UNIQUE(tenant_id,local_endpoint_id,provider_model_id),
  UNIQUE(tenant_id,local_model_catalog_entry_id),
  CHECK(updated_at>=created_at)
);
CREATE INDEX idx_local_model_catalog_entries_status_prd084
  ON local_model_catalog_entries(tenant_id,local_endpoint_id,availability_status,
    last_seen_revision DESC);

CREATE TABLE local_endpoint_create_receipts(
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128
    AND request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'),
  intent_hash TEXT NOT NULL CHECK(length(intent_hash)=64
    AND intent_hash NOT GLOB '*[^0-9a-f]*'),
  local_endpoint_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(length(created_at)=20
    AND substr(created_at,11,1)='T' AND substr(created_at,20,1)='Z'),
  PRIMARY KEY(tenant_id,request_id),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(tenant_id,local_endpoint_id)
    REFERENCES local_model_endpoints(tenant_id,local_endpoint_id)
);

CREATE TABLE local_model_call_events(
  local_model_call_event_id TEXT PRIMARY KEY CHECK(length(local_model_call_event_id)=37
    AND substr(local_model_call_event_id,1,5)='lmce_'
    AND substr(local_model_call_event_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128
    AND request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128
    AND trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'),
  routing_attempt_id TEXT NOT NULL CHECK(length(routing_attempt_id)=36
    AND substr(routing_attempt_id,1,4)='rta_'
    AND substr(routing_attempt_id,5) NOT GLOB '*[^0-9a-f]*'),
  local_endpoint_id TEXT NOT NULL,
  provider_id TEXT NOT NULL CHECK(length(provider_id) BETWEEN 1 AND 160
    AND provider_id NOT GLOB '*[^A-Za-z0-9_.:-]*'),
  model_id TEXT NOT NULL CHECK(length(model_id) BETWEEN 1 AND 160
    AND model_id NOT GLOB '*[^A-Za-z0-9_.:-]*'),
  attempt_sequence INTEGER NOT NULL CHECK(attempt_sequence IN(1,2)),
  attempt_role TEXT NOT NULL CHECK(attempt_role IN('primary','fallback')),
  execution_status TEXT NOT NULL CHECK(execution_status='called'),
  outcome TEXT NOT NULL CHECK(outcome IN('succeeded','failed','outcome_unknown')),
  error_code TEXT CHECK(error_code IS NULL OR(length(error_code) BETWEEN 3 AND 96
    AND error_code=lower(error_code) AND error_code NOT GLOB '*[^a-z0-9._]*')),
  error_category TEXT CHECK(error_category IS NULL OR
    error_category IN('timeout','rate_limited','upstream_5xx','other')),
  http_status INTEGER CHECK(http_status IS NULL OR http_status BETWEEN 100 AND 599),
  retryable INTEGER NOT NULL CHECK(retryable IN(0,1)),
  latency_ms INTEGER NOT NULL CHECK(latency_ms BETWEEN 0 AND 86400000),
  usage_status TEXT NOT NULL CHECK(
    usage_status IN('pending','recorded','unavailable','not_applicable')),
  completed_at TEXT NOT NULL CHECK(length(completed_at)=20
    AND substr(completed_at,11,1)='T' AND substr(completed_at,20,1)='Z'),
  FOREIGN KEY(tenant_id,routing_attempt_id)
    REFERENCES routing_attempts(tenant_id,routing_attempt_id),
  FOREIGN KEY(tenant_id,local_endpoint_id)
    REFERENCES local_model_endpoints(tenant_id,local_endpoint_id),
  FOREIGN KEY(tenant_id,provider_id) REFERENCES providers(tenant_id,provider_id),
  FOREIGN KEY(tenant_id,model_id) REFERENCES models(tenant_id,model_id),
  UNIQUE(tenant_id,routing_attempt_id),
  UNIQUE(tenant_id,local_model_call_event_id),
  CHECK((outcome='succeeded' AND error_code IS NULL AND error_category IS NULL
      AND usage_status IN('pending','recorded'))
    OR(outcome='failed' AND error_code IS NOT NULL
      AND error_code!='gateway.local_endpoint_outcome_unknown'
      AND usage_status IN('unavailable','not_applicable'))
    OR(outcome='outcome_unknown'
      AND error_code='gateway.local_endpoint_outcome_unknown'
      AND error_category='other' AND http_status IS NULL AND retryable=0
      AND usage_status='unavailable')),
  CHECK((attempt_sequence=1)=(attempt_role='primary'))
);
CREATE INDEX idx_local_model_call_events_request_prd084
  ON local_model_call_events(tenant_id,request_id,attempt_sequence);

CREATE TABLE local_model_call_event_outbox(
  local_model_call_event_outbox_id TEXT PRIMARY KEY CHECK(
    length(local_model_call_event_outbox_id)=37
    AND substr(local_model_call_event_outbox_id,1,5)='lmco_'
    AND substr(local_model_call_event_outbox_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  local_model_call_event_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('pending','leased','sent','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(
    typeof(attempt_count)='integer' AND attempt_count BETWEEN 0 AND 10),
  lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(
    typeof(lease_generation)='integer' AND lease_generation>=0),
  lease_owner_id TEXT,
  lease_token TEXT CHECK(lease_token IS NULL OR(length(lease_token)=64
    AND lease_token NOT GLOB '*[^0-9a-f]*')),
  lease_expires_at TEXT,
  next_attempt_at TEXT,
  error_code TEXT CHECK(error_code IS NULL OR(length(error_code) BETWEEN 3 AND 96
    AND error_code=lower(error_code) AND error_code NOT GLOB '*[^a-z0-9._]*')),
  delivered_at TEXT,
  created_at TEXT NOT NULL CHECK(length(created_at)=20
    AND substr(created_at,11,1)='T' AND substr(created_at,20,1)='Z'),
  updated_at TEXT NOT NULL CHECK(length(updated_at)=20
    AND substr(updated_at,11,1)='T' AND substr(updated_at,20,1)='Z'),
  FOREIGN KEY(tenant_id,local_model_call_event_id)
    REFERENCES local_model_call_events(tenant_id,local_model_call_event_id),
  UNIQUE(tenant_id,local_model_call_event_id),
  UNIQUE(tenant_id,local_model_call_event_outbox_id),
  CHECK(updated_at>=created_at),
  CHECK((status='pending' AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND next_attempt_at IS NOT NULL
      AND error_code IS NULL AND delivered_at IS NULL)
    OR(status='leased' AND attempt_count BETWEEN 1 AND 10
      AND lease_owner_id IS NOT NULL AND length(lease_owner_id) BETWEEN 1 AND 96
      AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL
      AND next_attempt_at IS NULL AND error_code IS NULL AND delivered_at IS NULL)
    OR(status='sent' AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND next_attempt_at IS NULL
      AND error_code IS NULL AND delivered_at IS NOT NULL)
    OR(status='failed' AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND next_attempt_at IS NULL
      AND error_code IS NOT NULL AND delivered_at IS NULL))
);
CREATE INDEX idx_local_model_call_outbox_claim_prd084
  ON local_model_call_event_outbox(status,next_attempt_at,tenant_id,created_at);

-- Aggregate ownership is checked in both directions so direct SQL cannot create or drift a child.
CREATE TRIGGER trg_local_model_endpoints_owner_insert_prd084
BEFORE INSERT ON local_model_endpoints FOR EACH ROW
WHEN NEW.revision!=1 OR NEW.catalog_revision!=0 OR NEW.catalog_status!='unknown'
  OR NEW.last_discovered_at IS NOT NULL OR NEW.last_catalog_error_code IS NOT NULL
  OR NEW.status!='active'
  OR NOT EXISTS(SELECT 1 FROM providers provider
    WHERE provider.tenant_id=NEW.tenant_id AND provider.provider_id=NEW.provider_id
      AND provider.provider_type='local_endpoint' AND provider.status='active')
  OR NOT EXISTS(SELECT 1 FROM models model
    WHERE model.tenant_id=NEW.tenant_id AND model.model_id=NEW.initial_model_id
      AND model.provider_id=NEW.provider_id AND model.status='active')
  OR NOT EXISTS(SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.created_by
      AND actor.status='active')
BEGIN SELECT RAISE(ABORT,'local_endpoint_owner_mismatch'); END;

CREATE TRIGGER trg_local_model_endpoints_update_prd084
BEFORE UPDATE ON local_model_endpoints FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.local_endpoint_id!=OLD.local_endpoint_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.provider_id!=OLD.provider_id
  OR NEW.initial_model_id!=OLD.initial_model_id OR NEW.engine_kind!=OLD.engine_kind
  OR NEW.revision!=OLD.revision OR NEW.created_by!=OLD.created_by
  OR NEW.status!=OLD.status OR NEW.created_at!=OLD.created_at
  OR NEW.updated_at<OLD.updated_at
  OR NOT EXISTS(SELECT 1 FROM providers provider
    WHERE provider.tenant_id=NEW.tenant_id AND provider.provider_id=NEW.provider_id
      AND provider.provider_type='local_endpoint' AND provider.status='active')
  OR NOT EXISTS(SELECT 1 FROM models model
    WHERE model.tenant_id=NEW.tenant_id AND model.model_id=NEW.initial_model_id
      AND model.provider_id=NEW.provider_id)
  OR NOT(
    (NEW.catalog_revision=OLD.catalog_revision+1 AND NEW.catalog_status='ready'
      AND NEW.last_discovered_at IS NOT NULL
      AND(OLD.last_discovered_at IS NULL OR NEW.last_discovered_at>=OLD.last_discovered_at)
      AND NEW.last_catalog_error_code IS NULL)
    OR(NEW.catalog_revision=OLD.catalog_revision AND NEW.catalog_status='error'
      AND NEW.last_discovered_at IS OLD.last_discovered_at
      AND NEW.last_catalog_error_code IS NOT NULL))
BEGIN SELECT RAISE(ABORT,'local_endpoint_immutable'); END;

CREATE TRIGGER trg_local_model_endpoints_delete_prd084
BEFORE DELETE ON local_model_endpoints FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'local_endpoint_immutable'); END;

CREATE TRIGGER trg_local_endpoint_provider_update_prd084
BEFORE UPDATE ON providers FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM local_model_endpoints endpoint
    WHERE endpoint.tenant_id=OLD.tenant_id AND endpoint.provider_id=OLD.provider_id)
  AND(NEW.rowid!=OLD.rowid OR NEW.provider_id!=OLD.provider_id
    OR NEW.tenant_id!=OLD.tenant_id OR NEW.provider_code!=OLD.provider_code
    OR NEW.name!=OLD.name OR NEW.base_url!=OLD.base_url
    OR NEW.provider_type!=OLD.provider_type OR NEW.auth_type!=OLD.auth_type
    OR NEW.description IS NOT OLD.description OR NEW.status!=OLD.status
    OR NEW.created_by IS NOT OLD.created_by OR NEW.created_at!=OLD.created_at
    OR NEW.disabled_at IS NOT OLD.disabled_at OR NEW.disabled_by IS NOT OLD.disabled_by)
BEGIN SELECT RAISE(ABORT,'local_endpoint_provider_immutable'); END;

CREATE TRIGGER trg_local_endpoint_provider_delete_prd084
BEFORE DELETE ON providers FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM local_model_endpoints endpoint
  WHERE endpoint.tenant_id=OLD.tenant_id AND endpoint.provider_id=OLD.provider_id)
BEGIN SELECT RAISE(ABORT,'local_endpoint_provider_immutable'); END;

CREATE TRIGGER trg_local_endpoint_model_update_prd084
BEFORE UPDATE ON models FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM local_model_endpoints endpoint
    WHERE endpoint.tenant_id=OLD.tenant_id AND endpoint.initial_model_id=OLD.model_id)
  AND(NEW.rowid!=OLD.rowid OR NEW.model_id!=OLD.model_id
    OR NEW.tenant_id!=OLD.tenant_id OR NEW.provider_id!=OLD.provider_id)
BEGIN SELECT RAISE(ABORT,'local_endpoint_model_immutable'); END;

CREATE TRIGGER trg_local_endpoint_model_delete_prd084
BEFORE DELETE ON models FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM local_model_endpoints endpoint
  WHERE endpoint.tenant_id=OLD.tenant_id AND endpoint.initial_model_id=OLD.model_id)
BEGIN SELECT RAISE(ABORT,'local_endpoint_model_immutable'); END;

CREATE TRIGGER trg_local_model_catalog_entries_owner_insert_prd084
BEFORE INSERT ON local_model_catalog_entries FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM local_model_endpoints endpoint
    WHERE endpoint.tenant_id=NEW.tenant_id
      AND endpoint.local_endpoint_id=NEW.local_endpoint_id
      AND endpoint.catalog_revision>=NEW.last_seen_revision
      AND endpoint.catalog_revision>=NEW.first_seen_revision)
  OR(NEW.linked_model_id IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM local_model_endpoints endpoint JOIN models model
      ON model.tenant_id=endpoint.tenant_id AND model.provider_id=endpoint.provider_id
    WHERE endpoint.tenant_id=NEW.tenant_id
      AND endpoint.local_endpoint_id=NEW.local_endpoint_id
      AND model.model_id=NEW.linked_model_id))
BEGIN SELECT RAISE(ABORT,'local_endpoint_catalog_owner_mismatch'); END;

CREATE TRIGGER trg_local_model_catalog_entries_update_prd084
BEFORE UPDATE ON local_model_catalog_entries FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.local_model_catalog_entry_id!=OLD.local_model_catalog_entry_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.local_endpoint_id!=OLD.local_endpoint_id
  OR NEW.provider_model_id!=OLD.provider_model_id
  OR NEW.first_seen_revision!=OLD.first_seen_revision
  OR NEW.created_at!=OLD.created_at OR NEW.updated_at<OLD.updated_at
  OR NEW.last_seen_revision<OLD.last_seen_revision
  OR NOT EXISTS(SELECT 1 FROM local_model_endpoints endpoint
    WHERE endpoint.tenant_id=NEW.tenant_id
      AND endpoint.local_endpoint_id=NEW.local_endpoint_id
      AND endpoint.catalog_revision>=NEW.last_seen_revision)
  OR(NEW.linked_model_id IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM local_model_endpoints endpoint JOIN models model
      ON model.tenant_id=endpoint.tenant_id AND model.provider_id=endpoint.provider_id
    WHERE endpoint.tenant_id=NEW.tenant_id
      AND endpoint.local_endpoint_id=NEW.local_endpoint_id
      AND model.model_id=NEW.linked_model_id))
BEGIN SELECT RAISE(ABORT,'local_endpoint_catalog_immutable'); END;

CREATE TRIGGER trg_local_model_catalog_entries_delete_prd084
BEFORE DELETE ON local_model_catalog_entries FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'local_endpoint_catalog_immutable'); END;

CREATE TRIGGER trg_local_endpoint_create_receipts_owner_insert_prd084
BEFORE INSERT ON local_endpoint_create_receipts FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM local_model_endpoints endpoint
  WHERE endpoint.tenant_id=NEW.tenant_id
    AND endpoint.local_endpoint_id=NEW.local_endpoint_id)
  OR EXISTS(SELECT 1 FROM local_endpoint_create_receipts receipt
    WHERE(receipt.tenant_id=NEW.tenant_id AND receipt.request_id=NEW.request_id)
      OR(NEW.rowid!=-1 AND receipt.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'local_endpoint_receipt_owner_mismatch'); END;
CREATE TRIGGER trg_local_endpoint_create_receipts_update_prd084
BEFORE UPDATE ON local_endpoint_create_receipts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'local_endpoint_receipt_immutable'); END;
CREATE TRIGGER trg_local_endpoint_create_receipts_delete_prd084
BEFORE DELETE ON local_endpoint_create_receipts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'local_endpoint_receipt_immutable'); END;

-- A call event is an exact immutable projection of one terminal called routing attempt.
CREATE TRIGGER trg_local_model_call_events_owner_insert_prd084
BEFORE INSERT ON local_model_call_events FOR EACH ROW
WHEN NEW.local_model_call_event_id!='lmce_'||substr(metahub_sha256(NEW.routing_attempt_id),1,32)
  OR NOT EXISTS(
    SELECT 1 FROM routing_attempts attempt
    JOIN local_model_endpoints endpoint
      ON endpoint.tenant_id=attempt.tenant_id AND endpoint.provider_id=attempt.provider_id
    WHERE attempt.tenant_id=NEW.tenant_id
      AND attempt.routing_attempt_id=NEW.routing_attempt_id
      AND attempt.request_id=NEW.request_id AND attempt.trace_id=NEW.trace_id
      AND attempt.model_id=NEW.model_id AND attempt.provider_id=NEW.provider_id
      AND endpoint.local_endpoint_id=NEW.local_endpoint_id
      AND attempt.attempt_sequence=NEW.attempt_sequence
      AND attempt.attempt_role=NEW.attempt_role
      AND attempt.provider_call_status='called' AND NEW.execution_status='called'
      AND attempt.status IN('succeeded','failed')
      AND NEW.outcome=CASE
        WHEN attempt.error_code='gateway.local_endpoint_outcome_unknown'
          THEN 'outcome_unknown' ELSE attempt.status END
      AND NEW.error_code IS attempt.error_code
      AND NEW.error_category IS attempt.error_category
      AND NEW.http_status IS attempt.upstream_http_status
      AND NEW.retryable=attempt.retryable AND NEW.latency_ms IS attempt.latency_ms
      AND NEW.usage_status=attempt.usage_status
      AND NEW.completed_at=attempt.completed_at)
  OR EXISTS(SELECT 1 FROM local_model_call_events current
    WHERE current.local_model_call_event_id=NEW.local_model_call_event_id
      OR(current.tenant_id=NEW.tenant_id
        AND current.routing_attempt_id=NEW.routing_attempt_id)
      OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'local_model_call_event_owner_mismatch'); END;
CREATE TRIGGER trg_local_model_call_events_update_prd084
BEFORE UPDATE ON local_model_call_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'local_model_call_event_immutable'); END;
CREATE TRIGGER trg_local_model_call_events_delete_prd084
BEFORE DELETE ON local_model_call_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'local_model_call_event_immutable'); END;

CREATE TRIGGER trg_local_model_call_outbox_owner_insert_prd084
BEFORE INSERT ON local_model_call_event_outbox FOR EACH ROW
WHEN NEW.local_model_call_event_outbox_id!='lmco_'||
      substr(metahub_sha256(NEW.local_model_call_event_id),1,32)
  OR NEW.status!='pending' OR NEW.attempt_count!=0 OR NEW.lease_generation!=0
  OR NEW.created_at!=NEW.updated_at OR NEW.next_attempt_at<NEW.created_at
  OR NOT EXISTS(SELECT 1 FROM local_model_call_events event
    WHERE event.tenant_id=NEW.tenant_id
      AND event.local_model_call_event_id=NEW.local_model_call_event_id)
  OR EXISTS(SELECT 1 FROM local_model_call_event_outbox current
    WHERE current.local_model_call_event_outbox_id=NEW.local_model_call_event_outbox_id
      OR(current.tenant_id=NEW.tenant_id
        AND current.local_model_call_event_id=NEW.local_model_call_event_id)
      OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'local_model_call_outbox_owner_mismatch'); END;

CREATE TRIGGER trg_local_model_call_outbox_transition_prd084
BEFORE UPDATE ON local_model_call_event_outbox FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.local_model_call_event_outbox_id!=OLD.local_model_call_event_outbox_id
  OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.local_model_call_event_id!=OLD.local_model_call_event_id
  OR NEW.created_at!=OLD.created_at OR NEW.updated_at<OLD.updated_at
  OR NOT(
    (OLD.status='pending' AND NEW.status='leased'
      AND NEW.attempt_count=OLD.attempt_count+1
      AND NEW.lease_generation=OLD.lease_generation+1)
    OR(OLD.status='leased' AND NEW.status='leased'
      AND NEW.attempt_count=OLD.attempt_count
      AND NEW.lease_generation=OLD.lease_generation+1)
    OR(OLD.status='leased' AND NEW.status='pending'
      AND NEW.attempt_count=OLD.attempt_count
      AND NEW.lease_generation=OLD.lease_generation+1)
    OR(OLD.status='leased' AND NEW.status IN('sent','failed')
      AND NEW.attempt_count=OLD.attempt_count
      AND NEW.lease_generation=OLD.lease_generation+1))
BEGIN SELECT RAISE(ABORT,'local_model_call_outbox_transition_invalid'); END;
CREATE TRIGGER trg_local_model_call_outbox_delete_prd084
BEFORE DELETE ON local_model_call_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'local_model_call_outbox_immutable'); END;

-- The routing gate forbids orphan local Providers and records the exact network-call fence.
CREATE TRIGGER trg_routing_attempt_local_owner_insert_prd084
BEFORE INSERT ON routing_attempts FOR EACH ROW
WHEN(
    EXISTS(SELECT 1 FROM providers provider
      WHERE provider.tenant_id=NEW.tenant_id AND provider.provider_id=NEW.provider_id
        AND provider.provider_type='local_endpoint')
    AND(length(NEW.routing_attempt_id)!=36
      OR substr(NEW.routing_attempt_id,1,4)!='rta_'
      OR substr(NEW.routing_attempt_id,5) GLOB '*[^0-9a-f]*'
      OR NEW.status!='started' OR NEW.provider_call_status!='not_called'
      OR NEW.provider_call_started_at IS NOT NULL
      OR NOT EXISTS(SELECT 1 FROM local_model_endpoints endpoint
        JOIN models model ON model.tenant_id=endpoint.tenant_id
          AND model.provider_id=endpoint.provider_id
        WHERE endpoint.tenant_id=NEW.tenant_id
          AND endpoint.provider_id=NEW.provider_id AND endpoint.status='active'
          AND endpoint.local_endpoint_id IS NOT NULL AND model.model_id=NEW.model_id))
  )
  OR(
    NOT EXISTS(SELECT 1 FROM providers provider
      WHERE provider.tenant_id=NEW.tenant_id AND provider.provider_id=NEW.provider_id
        AND provider.provider_type='local_endpoint')
    AND(NEW.provider_call_status!='not_applicable'
      OR NEW.provider_call_started_at IS NOT NULL))
BEGIN SELECT RAISE(ABORT,'routing_attempt_local_owner_mismatch'); END;

CREATE TRIGGER trg_routing_attempt_local_fence_update_prd084
BEFORE UPDATE ON routing_attempts FOR EACH ROW
WHEN(
    EXISTS(SELECT 1 FROM providers provider
      WHERE provider.tenant_id=OLD.tenant_id AND provider.provider_id=OLD.provider_id
        AND provider.provider_type='local_endpoint')
    AND(NEW.rowid!=OLD.rowid OR NEW.routing_attempt_id!=OLD.routing_attempt_id
      OR NEW.tenant_id!=OLD.tenant_id OR NEW.request_id!=OLD.request_id
      OR NEW.trace_id!=OLD.trace_id OR NEW.model_alias_id IS NOT OLD.model_alias_id
      OR NEW.fallback_policy_id IS NOT OLD.fallback_policy_id
      OR NEW.fallback_policy_version IS NOT OLD.fallback_policy_version
      OR NEW.attempt_sequence!=OLD.attempt_sequence OR NEW.attempt_role!=OLD.attempt_role
      OR NEW.trigger_type!=OLD.trigger_type
      OR NEW.trigger_error_code IS NOT OLD.trigger_error_code
      OR NEW.model_id!=OLD.model_id OR NEW.internal_model_id!=OLD.internal_model_id
      OR NEW.provider_id!=OLD.provider_id OR NEW.provider_code!=OLD.provider_code
      OR(OLD.status='started' AND NEW.status='failed'
        AND((NEW.provider_call_status='called' AND NEW.usage_status!='unavailable')
          OR(NEW.provider_call_status='not_called'
            AND NEW.usage_status!='not_applicable')))
      OR NOT(
      (OLD.provider_call_status='not_called'
        AND NEW.provider_call_status='not_called'
        AND NEW.provider_call_started_at IS NULL)
      OR(OLD.provider_call_status='not_called' AND NEW.provider_call_status='called'
        AND OLD.status='started' AND NEW.status='started'
        AND OLD.provider_call_started_at IS NULL
        AND NEW.provider_call_started_at IS NOT NULL
        AND NEW.provider_call_started_at>=OLD.created_at)
      OR(OLD.provider_call_status='called' AND NEW.provider_call_status='called'
        AND NEW.provider_call_started_at=OLD.provider_call_started_at)))
  )
  OR(
    NOT EXISTS(SELECT 1 FROM providers provider
      WHERE provider.tenant_id=OLD.tenant_id AND provider.provider_id=OLD.provider_id
        AND provider.provider_type='local_endpoint')
    AND(NEW.provider_call_status!='not_applicable'
      OR NEW.provider_call_started_at IS NOT NULL))
BEGIN SELECT RAISE(ABORT,'routing_attempt_local_fence_invalid'); END;

CREATE TRIGGER trg_routing_attempt_local_event_prd084
AFTER UPDATE OF status ON routing_attempts FOR EACH ROW
WHEN OLD.status='started' AND NEW.status IN('succeeded','failed')
  AND NEW.provider_call_status='called'
  AND EXISTS(SELECT 1 FROM providers provider
    WHERE provider.tenant_id=NEW.tenant_id AND provider.provider_id=NEW.provider_id
      AND provider.provider_type='local_endpoint')
BEGIN
  INSERT INTO local_model_call_events(
    local_model_call_event_id,tenant_id,request_id,trace_id,routing_attempt_id,
    local_endpoint_id,provider_id,model_id,attempt_sequence,attempt_role,
    execution_status,outcome,error_code,error_category,http_status,retryable,
    latency_ms,usage_status,completed_at
  )
  SELECT 'lmce_'||substr(metahub_sha256(NEW.routing_attempt_id),1,32),
    NEW.tenant_id,NEW.request_id,NEW.trace_id,NEW.routing_attempt_id,
    endpoint.local_endpoint_id,NEW.provider_id,NEW.model_id,
    NEW.attempt_sequence,NEW.attempt_role,'called',
    CASE WHEN NEW.error_code='gateway.local_endpoint_outcome_unknown'
      THEN 'outcome_unknown' ELSE NEW.status END,NEW.error_code,
    NEW.error_category,NEW.upstream_http_status,NEW.retryable,NEW.latency_ms,
    NEW.usage_status,NEW.completed_at
  FROM local_model_endpoints endpoint
  WHERE endpoint.tenant_id=NEW.tenant_id AND endpoint.provider_id=NEW.provider_id;
END;

CREATE TRIGGER trg_local_model_call_events_outbox_insert_prd084
AFTER INSERT ON local_model_call_events FOR EACH ROW
BEGIN
  INSERT INTO local_model_call_event_outbox(
    local_model_call_event_outbox_id,tenant_id,local_model_call_event_id,status,
    attempt_count,lease_generation,lease_owner_id,lease_token,lease_expires_at,
    next_attempt_at,error_code,delivered_at,created_at,updated_at
  ) VALUES(
    'lmco_'||substr(metahub_sha256(NEW.local_model_call_event_id),1,32),
    NEW.tenant_id,NEW.local_model_call_event_id,'pending',0,0,NULL,NULL,NULL,
    NEW.completed_at,NULL,NULL,NEW.completed_at,NEW.completed_at
  );
END;

-- Atomically supersede each tenant's v1 activation and install the frozen v2 identity.
CREATE TEMP TABLE prd084_adapter_switch AS
SELECT activation_id,tenant_id,strftime('%Y-%m-%dT%H:%M:%SZ','now') AS switch_at
FROM provider_adapter_catalog_activations
WHERE status='active' AND catalog_version='adapter-spec-v1'
  AND catalog_hash='9757f206d4bd0ec625d39173d4c5e5bd1f6d36132b3117008ffb1e5e7ba69870'
  AND spec_count=9;

DROP TRIGGER trg_tenants_adapter_spec_initialize_prd063;
DROP TRIGGER trg_provider_adapter_activations_immutable_delete_prd063;
DROP TRIGGER trg_provider_adapter_activations_immutable_update_prd063;
DROP TRIGGER trg_provider_adapter_activations_owner_insert_prd063;

UPDATE provider_adapter_catalog_activations
SET status='superseded',superseded_at=(SELECT switch_at FROM prd084_adapter_switch
      WHERE prd084_adapter_switch.activation_id=provider_adapter_catalog_activations.activation_id),
  updated_at=(SELECT switch_at FROM prd084_adapter_switch
      WHERE prd084_adapter_switch.activation_id=provider_adapter_catalog_activations.activation_id)
WHERE activation_id IN(SELECT activation_id FROM prd084_adapter_switch);

INSERT INTO provider_adapter_catalog_activations(
  activation_id,tenant_id,catalog_version,catalog_hash,spec_count,status,changed,
  request_id,trace_id,activated_by,activated_at,superseded_at,created_at,updated_at
)
SELECT 'paca_'||lower(hex(randomblob(16))),tenant_id,'adapter-spec-v2',
  'c1cc9a5c9f977308fa3e06ab6914c28d9f9bef4ad3fe07eb950d1c6a76a2addf',
  10,'active',1,'req_prd084_migrate_'||substr(activation_id,6),
  'trc_prd084_migrate_'||substr(activation_id,6),'adapter-spec-publisher',
  switch_at,NULL,switch_at,switch_at
FROM prd084_adapter_switch;

CREATE TRIGGER trg_provider_adapter_activations_owner_insert_prd063
BEFORE INSERT ON provider_adapter_catalog_activations FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR(NEW.activated_by='adapter-spec-bootstrap' AND(
    NEW.catalog_version!='adapter-spec-v2'
    OR NEW.catalog_hash!='c1cc9a5c9f977308fa3e06ab6914c28d9f9bef4ad3fe07eb950d1c6a76a2addf'
    OR NEW.spec_count!=10 OR NEW.status!='active' OR NEW.changed!=0
    OR NEW.request_id NOT LIKE 'req_prd084_bootstrap_%'
    OR NEW.trace_id NOT LIKE 'trc_prd084_bootstrap_%'))
BEGIN SELECT RAISE(ABORT,'provider_adapter_activation_owner_invalid'); END;
CREATE TRIGGER trg_provider_adapter_activations_immutable_update_prd063
BEFORE UPDATE ON provider_adapter_catalog_activations FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.activation_id!=OLD.activation_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.catalog_version!=OLD.catalog_version
  OR NEW.catalog_hash!=OLD.catalog_hash OR NEW.spec_count!=OLD.spec_count
  OR NEW.changed!=OLD.changed OR NEW.request_id!=OLD.request_id
  OR NEW.trace_id!=OLD.trace_id OR NEW.activated_by!=OLD.activated_by
  OR NEW.activated_at!=OLD.activated_at OR NEW.created_at!=OLD.created_at
  OR OLD.status!='active' OR NEW.status!='superseded'
  OR OLD.superseded_at IS NOT NULL OR NEW.superseded_at IS NULL
  OR NEW.updated_at!=NEW.superseded_at OR NEW.updated_at<OLD.updated_at
BEGIN SELECT RAISE(ABORT,'provider_adapter_activation_immutable'); END;
CREATE TRIGGER trg_provider_adapter_activations_immutable_delete_prd063
BEFORE DELETE ON provider_adapter_catalog_activations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_adapter_activation_immutable'); END;
CREATE TRIGGER trg_tenants_adapter_spec_initialize_prd063
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  INSERT INTO provider_adapter_catalog_activations(
    activation_id,tenant_id,catalog_version,catalog_hash,spec_count,status,changed,
    request_id,trace_id,activated_by,activated_at,superseded_at,created_at,updated_at
  ) VALUES(
    'paca_'||lower(hex(randomblob(16))),NEW.tenant_id,'adapter-spec-v2',
    'c1cc9a5c9f977308fa3e06ab6914c28d9f9bef4ad3fe07eb950d1c6a76a2addf',
    10,'active',0,'req_prd084_bootstrap_'||lower(hex(randomblob(12))),
    'trc_prd084_bootstrap_'||lower(hex(randomblob(12))),
    'adapter-spec-bootstrap',strftime('%Y-%m-%dT%H:%M:%SZ','now'),NULL,
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'));
END;
DROP TABLE prd084_adapter_switch;

-- Expand only default revision-18 role matrices; customized roles remain byte-for-byte intact.
CREATE TEMP TABLE prd084_roles_to_expand AS
SELECT role.role_id,role.tenant_id,role.code
FROM roles role
WHERE role.code IN('admin','developer','ops_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '5af655a2ffa8cb4f14d84bec71e1b0feb17e4a1e2d982c9fc9cbe711e74cead7'
    WHEN 'developer' THEN '69328ccc0d6741562250f7b83fe7cf3fce8509db4b092b6917b475e3dd446b8d'
    WHEN 'ops_admin' THEN 'dc3641880859dff9a82069c6c4b1f32049320aa9406e0f885770ad643da04c1e'
  END
  AND NOT EXISTS(
    SELECT 1 FROM rbac_role_permission_templates template
    WHERE template.role_code=role.code AND NOT EXISTS(
      SELECT 1 FROM role_permissions relation JOIN permissions permission
        ON permission.tenant_id=relation.tenant_id
       AND permission.permission_id=relation.permission_id
      WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
        AND permission.code=template.permission_code))
  AND NOT EXISTS(
    SELECT 1 FROM role_permissions relation JOIN permissions permission
      ON permission.tenant_id=relation.tenant_id
     AND permission.permission_id=relation.permission_id
    WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
      AND NOT EXISTS(SELECT 1 FROM rbac_role_permission_templates template
        WHERE template.role_code=role.code AND template.permission_code=permission.code));

DROP TRIGGER trg_tenants_rbac_initialize_prd059;
DROP TRIGGER trg_roles_owner_insert_prd059;
DROP TRIGGER trg_roles_matrix_update_prd059;
DROP TRIGGER trg_role_permissions_owner_insert_prd059;
DROP TRIGGER trg_role_permissions_delete_prd059;
DROP TRIGGER trg_rbac_permission_templates_insert_prd059;
DROP TRIGGER trg_rbac_permission_templates_update_prd059;
DROP TRIGGER trg_rbac_permission_templates_delete_prd059;
DROP TRIGGER trg_rbac_role_templates_insert_prd059;
DROP TRIGGER trg_rbac_role_templates_update_prd059;
DROP TRIGGER trg_rbac_role_templates_delete_prd059;
DROP TRIGGER trg_permissions_owner_insert_prd059;
DROP TRIGGER trg_permissions_update_prd059;
DROP TRIGGER trg_permissions_delete_prd059;
DROP TRIGGER trg_permissions_rowid_insert_prd059;

CREATE TABLE rbac_permission_templates_prd084_new(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=19)
);
INSERT INTO rbac_permission_templates_prd084_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision)
SELECT rowid,permission_code,category,name,description,risk_level,19
FROM rbac_permission_templates;
INSERT INTO rbac_permission_templates_prd084_new(
  permission_code,category,name,description,risk_level,catalog_revision
) VALUES
  ('local_endpoint.read','provider','读取本地模型 Endpoint',
   '读取当前租户脱敏的本地模型 Endpoint 聚合与目录摘要。','read',19),
  ('local_endpoint.manage','provider','管理本地模型 Endpoint',
   '原子注册当前租户本地模型 Endpoint、初始模型与可选凭据。','critical',19),
  ('local_endpoint.test','provider','测试本地模型 Endpoint',
   '执行当前租户受限健康探测与模型目录发现。','write',19);
DROP TABLE rbac_permission_templates;
ALTER TABLE rbac_permission_templates_prd084_new RENAME TO rbac_permission_templates;

INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
VALUES
  ('admin','local_endpoint.read',1),('admin','local_endpoint.manage',1),
  ('admin','local_endpoint.test',1),('developer','local_endpoint.read',1),
  ('developer','local_endpoint.test',1),('ops_admin','local_endpoint.read',1),
  ('ops_admin','local_endpoint.manage',1),('ops_admin','local_endpoint.test',1);

CREATE TABLE permissions_prd084_new(
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=19),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),
  UNIQUE(tenant_id,permission_id)
);
INSERT INTO permissions_prd084_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  19,status,created_at,updated_at FROM permissions;
INSERT INTO permissions_prd084_new(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at)
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
  template.category,template.name,template.description,template.risk_level,19,'active',
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant JOIN rbac_permission_templates template
  ON template.permission_code IN(
    'local_endpoint.read','local_endpoint.manage','local_endpoint.test');
DROP TABLE permissions;
ALTER TABLE permissions_prd084_new RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);

INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
  permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd084_roles_to_expand role JOIN permissions permission
  ON permission.tenant_id=role.tenant_id
WHERE(role.code IN('admin','ops_admin') AND permission.code IN(
    'local_endpoint.read','local_endpoint.manage','local_endpoint.test'))
  OR(role.code='developer' AND permission.code IN(
    'local_endpoint.read','local_endpoint.test'));
UPDATE roles SET permission_revision=permission_revision+1,
  matrix_sha256=CASE code
    WHEN 'admin' THEN '76c109f34e195c8bdce18c7b6bdb9305aac0bc4dc292d1f46a8cd5def631d074'
    WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1'
    WHEN 'ops_admin' THEN 'aae41c1ea441876e4b810ba4f80f0de4ac7b1d8cb2a6386c07e3564ab7e44dba'
  END
WHERE role_id IN(SELECT role_id FROM prd084_roles_to_expand);

CREATE TRIGGER trg_rbac_permission_templates_insert_prd059
BEFORE INSERT ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_permission_templates_update_prd059
BEFORE UPDATE ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_permission_templates_delete_prd059
BEFORE DELETE ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_insert_prd059
BEFORE INSERT ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_update_prd059
BEFORE UPDATE ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_delete_prd059
BEFORE DELETE ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_permissions_owner_insert_prd059
BEFORE INSERT ON permissions FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM rbac_permission_templates template
  WHERE template.permission_code=NEW.code AND template.category=NEW.category
    AND template.name=NEW.name AND template.description=NEW.description
    AND template.risk_level=NEW.risk_level
    AND template.catalog_revision=NEW.catalog_revision)
  OR NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id)
  OR NEW.status!='active'
  OR EXISTS(SELECT 1 FROM permissions current
    WHERE current.permission_id=NEW.permission_id
      OR(current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
      OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_update_prd059
BEFORE UPDATE ON permissions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_delete_prd059
BEFORE DELETE ON permissions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_rowid_insert_prd059
AFTER INSERT ON permissions FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_roles_owner_insert_prd059
BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id)
  OR NEW.code NOT IN('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN '76c109f34e195c8bdce18c7b6bdb9305aac0bc4dc292d1f46a8cd5def631d074'
    WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1'
    WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
    WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
    WHEN 'security_admin' THEN 'aeb4d6b081dce8ed36c6513b567aba4be395816c5a5ad2f1d939527a50ad14df'
    WHEN 'ops_admin' THEN 'aae41c1ea441876e4b810ba4f80f0de4ac7b1d8cb2a6386c07e3564ab7e44dba'
  END
  OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END
  OR EXISTS(SELECT 1 FROM roles current WHERE current.role_id=NEW.role_id
    OR(current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
    OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_matrix_update_prd059
BEFORE UPDATE ON roles FOR EACH ROW
WHEN OLD.code='admin' OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code OR NEW.name!=OLD.name
  OR NEW.description!=OLD.description OR NEW.status!=OLD.status
  OR NEW.created_at!=OLD.created_at
  OR NEW.is_permission_immutable!=OLD.is_permission_immutable
  OR NEW.permission_revision!=OLD.permission_revision+1
  OR NEW.matrix_sha256=OLD.matrix_sha256 OR length(NEW.matrix_sha256)!=64
  OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*'
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_owner_insert_prd059
BEFORE INSERT ON role_permissions FOR EACH ROW
WHEN NOT EXISTS(
  SELECT 1 FROM roles role JOIN permissions permission
    ON permission.tenant_id=role.tenant_id
  JOIN rbac_role_permission_templates template ON template.role_code=role.code
    AND template.permission_code=permission.code
  WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id
    AND role.status='active' AND permission.permission_id=NEW.permission_id
    AND permission.status='active')
  OR EXISTS(SELECT 1 FROM role_permissions current
    WHERE current.role_permission_id=NEW.role_permission_id
      OR(current.tenant_id=NEW.tenant_id AND current.role_id=NEW.role_id
        AND current.permission_id=NEW.permission_id)
      OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_owner_mismatch'); END;
CREATE TRIGGER trg_role_permissions_delete_prd059
BEFORE DELETE ON role_permissions FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM roles role WHERE role.tenant_id=OLD.tenant_id
  AND role.role_id=OLD.role_id AND role.code='admin')
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;

CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  INSERT INTO roles(role_id,tenant_id,code,name,description,status,created_at,updated_at,
    permission_revision,matrix_sha256,is_permission_immutable)
  SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,catalog.role_code,
    CASE catalog.role_code WHEN 'admin' THEN 'Admin' WHEN 'developer' THEN 'Developer'
      WHEN 'viewer' THEN 'Viewer' WHEN 'finance_admin' THEN 'FinanceAdmin'
      WHEN 'security_admin' THEN 'SecurityAdmin' WHEN 'ops_admin' THEN 'OpsAdmin' END,
    CASE catalog.role_code
      WHEN 'admin' THEN '租户最高管理员，拥有完整权限且矩阵不可修改。'
      WHEN 'developer' THEN '负责模型接入、Key、调试和请求排障，不读取正式账单。'
      WHEN 'viewer' THEN '仅查看基础概览和公开控制台信息。'
      WHEN 'finance_admin' THEN '负责用量、账本、预算、价格、成本异常、套餐额度和账单争议。'
      WHEN 'security_admin' THEN '负责审计、DLP、策略、错误归一和安全事件。'
      WHEN 'ops_admin' THEN '负责 Provider、环境、项目、Key、模型、路由、批量任务和在线实验运维。'
    END,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,
    CASE catalog.role_code
      WHEN 'admin' THEN '76c109f34e195c8bdce18c7b6bdb9305aac0bc4dc292d1f46a8cd5def631d074'
      WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1'
      WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
      WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
      WHEN 'security_admin' THEN 'aeb4d6b081dce8ed36c6513b567aba4be395816c5a5ad2f1d939527a50ad14df'
      WHEN 'ops_admin' THEN 'aae41c1ea441876e4b810ba4f80f0de4ac7b1d8cb2a6386c07e3564ab7e44dba'
    END,CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,19,'active',
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;
  INSERT INTO role_permissions(
    role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,role.role_id,
    permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM roles role JOIN rbac_role_permission_templates template
    ON template.role_code=role.code
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
    AND permission.code=template.permission_code
  WHERE role.tenant_id=NEW.tenant_id;
END;
DROP TABLE prd084_roles_to_expand;

DROP TABLE IF EXISTS temp.prd084_up_final_guard;
CREATE TEMP TABLE prd084_up_final_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd084_up_final_guard_abort
BEFORE INSERT ON prd084_up_final_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd084_up_final_invalid'); END;
INSERT INTO prd084_up_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=88
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=19)
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||description||
          char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code
      ))='f18c61ce9d657a22a0592abb0922407b7bda7cc1130d93fe5eea959ee1dbd3b0'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=232
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code
      ))='0a448a84577e046654ed83d384f6cb59930477fcec78156f478a2aab66a5e239'
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM permissions permission LEFT JOIN rbac_permission_templates template
        ON template.permission_code=permission.code
      WHERE template.permission_code IS NULL OR permission.catalog_revision!=19
        OR permission.category!=template.category OR permission.name!=template.name
        OR permission.description!=template.description
        OR permission.risk_level!=template.risk_level)
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM tenants)=
      (SELECT COUNT(*) FROM provider_adapter_catalog_activations
       WHERE status='active' AND catalog_version='adapter-spec-v2'
         AND catalog_hash='c1cc9a5c9f977308fa3e06ab6914c28d9f9bef4ad3fe07eb950d1c6a76a2addf'
         AND spec_count=10)
      AND NOT EXISTS(SELECT 1 FROM tenants tenant
        WHERE(SELECT COUNT(*) FROM provider_adapter_catalog_activations activation
          WHERE activation.tenant_id=tenant.tenant_id AND activation.status='active')!=1)
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE tbl_name='providers'
      AND type='index')=10 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE tbl_name='providers'
      AND type='trigger' AND name IN(
        'trg_policy_template_provider_delete_prd068',
        'trg_policy_template_provider_update_prd068',
        'trg_providers_experiment_arm_delete_prd075',
        'trg_providers_experiment_arm_update_prd075',
        'trg_local_endpoint_provider_update_prd084',
        'trg_local_endpoint_provider_delete_prd084'))=6 THEN 0 ELSE 1 END
  + CASE WHEN instr((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='providers'),'''local_endpoint''')>0
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'local_model_endpoints','local_model_catalog_entries',
      'local_endpoint_create_receipts','local_model_call_events',
      'local_model_call_event_outbox'))=5 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name LIKE '%_prd084')=23 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('routing_attempts')
      WHERE name IN('provider_call_status','provider_call_started_at'))=2
      AND NOT EXISTS(SELECT 1 FROM routing_attempts
        WHERE provider_call_status!='not_applicable' OR provider_call_started_at IS NOT NULL)
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd084_up_final_guard_abort;
DROP TABLE prd084_up_final_guard;

COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
