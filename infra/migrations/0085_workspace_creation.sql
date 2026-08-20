PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd093_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd093_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0084' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd093')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd093_up_guard;

CREATE TABLE workspaces(
  workspace_id TEXT PRIMARY KEY CHECK(length(workspace_id)=36
    AND substr(workspace_id,1,4)='wsp_'
    AND substr(workspace_id,5) NOT GLOB '*[^0-9a-f]*'),
  kind TEXT NOT NULL CHECK(kind IN('personal','organization')),
  status TEXT NOT NULL CHECK(status IN('active','migration_conflict')),
  profile_state TEXT NOT NULL CHECK(profile_state IN('draft','confirmed','migration_conflict')),
  creation_source TEXT NOT NULL CHECK(creation_source IN(
    'user_created','legacy_bootstrap','legacy_backfill')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
  created_by_account_id TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(created_by_account_id) REFERENCES global_accounts(account_id),
  CHECK(creation_source!='user_created' OR created_by_account_id IS NOT NULL)
);
CREATE INDEX idx_workspaces_creator_prd093
  ON workspaces(created_by_account_id,status,created_at,workspace_id);
CREATE UNIQUE INDEX uq_workspaces_personal_creator_prd093
  ON workspaces(created_by_account_id)
  WHERE kind='personal' AND creation_source='user_created';
CREATE TRIGGER trg_workspaces_owner_update_prd093
BEFORE UPDATE ON workspaces FOR EACH ROW
WHEN NEW.workspace_id!=OLD.workspace_id OR NEW.kind!=OLD.kind
  OR NEW.creation_source!=OLD.creation_source
  OR (NEW.created_by_account_id IS NOT OLD.created_by_account_id AND NOT (
    OLD.creation_source IN('legacy_bootstrap','legacy_backfill')
    AND OLD.status='migration_conflict' AND NEW.status='active'
    AND OLD.created_by_account_id IS NULL AND NEW.created_by_account_id IS NOT NULL
  ))
  OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'workspace_owner_immutable'); END;

CREATE TABLE workspace_tenant_bindings(
  workspace_tenant_binding_id TEXT PRIMARY KEY CHECK(
    length(workspace_tenant_binding_id)=36
    AND substr(workspace_tenant_binding_id,1,4)='wtb_'
    AND substr(workspace_tenant_binding_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN('active','migration_conflict')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id) REFERENCES workspaces(workspace_id),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  UNIQUE(workspace_id,tenant_id)
);
CREATE TRIGGER trg_workspace_tenant_bindings_update_prd093
BEFORE UPDATE ON workspace_tenant_bindings FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_tenant_binding_immutable'); END;
CREATE TRIGGER trg_workspace_tenant_bindings_delete_prd093
BEFORE DELETE ON workspace_tenant_bindings FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_tenant_binding_immutable'); END;

CREATE TABLE workspace_regions(
  workspace_region_id TEXT PRIMARY KEY CHECK(length(workspace_region_id)=36
    AND substr(workspace_region_id,1,4)='wrg_'
    AND substr(workspace_region_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL UNIQUE,
  region_code TEXT NOT NULL CHECK(length(region_code) BETWEEN 3 AND 40
    AND region_code=lower(region_code)
    AND region_code NOT GLOB '*[^a-z0-9-]*'),
  residency_policy_version TEXT NOT NULL CHECK(length(residency_policy_version) BETWEEN 1 AND 80),
  status TEXT NOT NULL CHECK(status IN('active','migration_conflict')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE TRIGGER trg_workspace_regions_update_prd093
BEFORE UPDATE ON workspace_regions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_region_immutable'); END;
CREATE TRIGGER trg_workspace_regions_delete_prd093
BEFORE DELETE ON workspace_regions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_region_immutable'); END;

CREATE TABLE organization_profiles(
  organization_profile_id TEXT PRIMARY KEY CHECK(length(organization_profile_id)=36
    AND substr(organization_profile_id,1,4)='org_'
    AND substr(organization_profile_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 160),
  slug_normalized TEXT NOT NULL UNIQUE CHECK(length(slug_normalized) BETWEEN 3 AND 48
    AND slug_normalized=lower(slug_normalized)
    AND slug_normalized NOT GLOB '*[^a-z0-9-]*'
    AND substr(slug_normalized,1,1) NOT GLOB '[^a-z0-9]'
    AND substr(slug_normalized,-1,1) NOT GLOB '[^a-z0-9]'),
  profile_state TEXT NOT NULL CHECK(profile_state IN('draft','confirmed','migration_conflict')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id) REFERENCES workspaces(workspace_id)
);
CREATE TRIGGER trg_organization_profiles_owner_update_prd093
BEFORE UPDATE ON organization_profiles FOR EACH ROW
WHEN NEW.organization_profile_id!=OLD.organization_profile_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.slug_normalized!=OLD.slug_normalized
  OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'organization_profile_owner_immutable'); END;

CREATE TABLE personal_profiles(
  personal_profile_id TEXT PRIMARY KEY CHECK(length(personal_profile_id)=36
    AND substr(personal_profile_id,1,4)='per_'
    AND substr(personal_profile_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL UNIQUE,
  account_id TEXT NOT NULL,
  display_label TEXT NOT NULL CHECK(length(trim(display_label)) BETWEEN 1 AND 120),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id) REFERENCES workspaces(workspace_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id)
);
CREATE TRIGGER trg_personal_profiles_owner_update_prd093
BEFORE UPDATE ON personal_profiles FOR EACH ROW
WHEN NEW.personal_profile_id!=OLD.personal_profile_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.account_id!=OLD.account_id
  OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'personal_profile_owner_immutable'); END;

CREATE TABLE workspace_memberships(
  workspace_membership_id TEXT PRIMARY KEY CHECK(length(workspace_membership_id)=36
    AND substr(workspace_membership_id,1,4)='wmb_'
    AND substr(workspace_membership_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  legacy_user_id TEXT NOT NULL,
  role_code TEXT NOT NULL CHECK(role_code='owner'),
  status TEXT NOT NULL CHECK(status='active'),
  source TEXT NOT NULL CHECK(source IN('workspace_creator','legacy_backfill')),
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(tenant_id,legacy_user_id) REFERENCES users(tenant_id,user_id),
  UNIQUE(workspace_id,account_id),
  UNIQUE(tenant_id,legacy_user_id)
);
CREATE INDEX idx_workspace_memberships_account_prd093
  ON workspace_memberships(account_id,status,created_at,workspace_id);
CREATE TRIGGER trg_workspace_memberships_update_prd093
BEFORE UPDATE ON workspace_memberships FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_membership_foundation_immutable'); END;
CREATE TRIGGER trg_workspace_memberships_delete_prd093
BEFORE DELETE ON workspace_memberships FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_membership_foundation_immutable'); END;

CREATE TABLE workspace_provisioning_jobs(
  workspace_provisioning_job_id TEXT PRIMARY KEY CHECK(
    length(workspace_provisioning_job_id)=36
    AND substr(workspace_provisioning_job_id,1,4)='wpj_'
    AND substr(workspace_provisioning_job_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL UNIQUE,
  generation INTEGER NOT NULL DEFAULT 1 CHECK(generation>=1),
  status TEXT NOT NULL CHECK(status IN('core_committed','ready','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 20),
  error_code TEXT CHECK(error_code IS NULL OR (length(error_code) BETWEEN 3 AND 80
    AND error_code=lower(error_code) AND error_code NOT GLOB '*[^a-z0-9._]*')),
  next_attempt_at TEXT CHECK(next_attempt_at IS NULL OR unixepoch(next_attempt_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  CHECK((status='failed' AND error_code IS NOT NULL)
    OR(status!='failed' AND error_code IS NULL))
);

CREATE TABLE workspace_environment_origins(
  workspace_environment_origin_id TEXT PRIMARY KEY CHECK(
    length(workspace_environment_origin_id)=36
    AND substr(workspace_environment_origin_id,1,4)='weo_'
    AND substr(workspace_environment_origin_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  environment_id TEXT NOT NULL UNIQUE,
  origin TEXT NOT NULL CHECK(origin IN('system_created','legacy_existing','user_created')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(tenant_id,environment_id) REFERENCES environments(tenant_id,environment_id),
  UNIQUE(workspace_id,environment_id)
);
CREATE TRIGGER trg_workspace_environment_origins_update_prd093
BEFORE UPDATE ON workspace_environment_origins FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_environment_origin_immutable'); END;
CREATE TRIGGER trg_workspace_environment_origins_delete_prd093
BEFORE DELETE ON workspace_environment_origins FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_environment_origin_immutable'); END;

CREATE TABLE workspace_events(
  workspace_event_id TEXT PRIMARY KEY CHECK(length(workspace_event_id)=36
    AND substr(workspace_event_id,1,4)='wev_'
    AND substr(workspace_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'workspace.created','workspace.profile_confirmed','workspace.provisioning_failed')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 3 AND 80
    AND reason_code=lower(reason_code) AND reason_code NOT GLOB '*[^a-z0-9._]*'),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)
    AND json_type(payload_json)='object'
    AND length(CAST(payload_json AS BLOB))<=2048),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id)
);
CREATE INDEX idx_workspace_events_workspace_prd093
  ON workspace_events(workspace_id,occurred_at,workspace_event_id);
CREATE TRIGGER trg_workspace_events_update_prd093
BEFORE UPDATE ON workspace_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_event_immutable'); END;
CREATE TRIGGER trg_workspace_events_delete_prd093
BEFORE DELETE ON workspace_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_event_immutable'); END;

CREATE TABLE workspace_event_outbox(
  workspace_event_outbox_id TEXT PRIMARY KEY CHECK(length(workspace_event_outbox_id)=36
    AND substr(workspace_event_outbox_id,1,4)='wob_'
    AND substr(workspace_event_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_event_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN('pending','processing','delivered','dead')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 20),
  generation INTEGER NOT NULL DEFAULT 0 CHECK(generation>=0),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_event_id) REFERENCES workspace_events(workspace_event_id),
  CHECK((status IN('pending','processing','dead') AND delivered_at IS NULL)
    OR(status='delivered' AND delivered_at IS NOT NULL))
);
CREATE INDEX idx_workspace_event_outbox_claim_prd093
  ON workspace_event_outbox(status,next_attempt_at,created_at,workspace_event_outbox_id);

CREATE TABLE workspace_user_projection_claims(
  workspace_user_projection_claim_id TEXT PRIMARY KEY CHECK(
    length(workspace_user_projection_claim_id)=36
    AND substr(workspace_user_projection_claim_id,1,4)='wpc_'
    AND substr(workspace_user_projection_claim_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  legacy_user_id TEXT NOT NULL,
  credential_revision INTEGER NOT NULL CHECK(credential_revision>=1),
  email_normalized TEXT NOT NULL CHECK(length(email_normalized) BETWEEN 3 AND 320
    AND email_normalized=lower(trim(email_normalized))),
  status TEXT NOT NULL CHECK(status IN('pending','consumed')),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  consumed_at TEXT CHECK(consumed_at IS NULL OR unixepoch(consumed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  UNIQUE(tenant_id,legacy_user_id),
  CHECK((status='pending' AND consumed_at IS NULL)
    OR(status='consumed' AND consumed_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_workspace_projection_claim_pending_prd093
  ON workspace_user_projection_claims(tenant_id,legacy_user_id)
  WHERE status='pending';
CREATE TRIGGER trg_workspace_projection_claims_update_prd093
BEFORE UPDATE ON workspace_user_projection_claims FOR EACH ROW
WHEN NOT (
  OLD.status='pending' AND NEW.status='consumed' AND NEW.consumed_at IS NOT NULL
  AND NEW.workspace_user_projection_claim_id=OLD.workspace_user_projection_claim_id
  AND NEW.workspace_id=OLD.workspace_id AND NEW.tenant_id=OLD.tenant_id
  AND NEW.account_id=OLD.account_id AND NEW.legacy_user_id=OLD.legacy_user_id
  AND NEW.credential_revision=OLD.credential_revision
  AND NEW.email_normalized=OLD.email_normalized AND NEW.expires_at=OLD.expires_at
  AND NEW.created_at=OLD.created_at
)
BEGIN SELECT RAISE(ABORT,'workspace_projection_claim_immutable'); END;
CREATE TRIGGER trg_workspace_projection_claims_delete_prd093
BEFORE DELETE ON workspace_user_projection_claims FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_projection_claim_immutable'); END;

CREATE TABLE workspace_creation_requests(
  workspace_creation_request_id TEXT PRIMARY KEY CHECK(
    length(workspace_creation_request_id)=36
    AND substr(workspace_creation_request_id,1,4)='wcr_'
    AND substr(workspace_creation_request_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL,
  idempotency_key_hmac TEXT NOT NULL CHECK(length(idempotency_key_hmac)=64
    AND idempotency_key_hmac NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status='completed'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(workspace_id) REFERENCES workspaces(workspace_id),
  UNIQUE(account_id,idempotency_key_hmac)
);
CREATE TRIGGER trg_workspace_creation_requests_update_prd093
BEFORE UPDATE ON workspace_creation_requests FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_creation_request_immutable'); END;
CREATE TRIGGER trg_workspace_creation_requests_delete_prd093
BEFORE DELETE ON workspace_creation_requests FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_creation_request_immutable'); END;

CREATE TABLE workspace_tenant_creation_claims(
  workspace_tenant_creation_claim_id TEXT PRIMARY KEY CHECK(
    length(workspace_tenant_creation_claim_id)=36
    AND substr(workspace_tenant_creation_claim_id,1,4)='wtc_'
    AND substr(workspace_tenant_creation_claim_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL UNIQUE,
  account_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('pending','consumed')),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  consumed_at TEXT CHECK(consumed_at IS NULL OR unixepoch(consumed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id) REFERENCES workspaces(workspace_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  CHECK((status='pending' AND consumed_at IS NULL)
    OR(status='consumed' AND consumed_at IS NOT NULL))
);
CREATE TRIGGER trg_workspace_tenant_creation_claims_update_prd093
BEFORE UPDATE ON workspace_tenant_creation_claims FOR EACH ROW
WHEN NOT (
  OLD.status='pending' AND NEW.status='consumed' AND NEW.consumed_at IS NOT NULL
  AND NEW.workspace_tenant_creation_claim_id=OLD.workspace_tenant_creation_claim_id
  AND NEW.workspace_id=OLD.workspace_id AND NEW.tenant_id=OLD.tenant_id
  AND NEW.account_id=OLD.account_id AND NEW.expires_at=OLD.expires_at
  AND NEW.created_at=OLD.created_at
)
BEGIN SELECT RAISE(ABORT,'workspace_tenant_creation_claim_immutable'); END;
CREATE TRIGGER trg_workspace_tenant_creation_claims_delete_prd093
BEFORE DELETE ON workspace_tenant_creation_claims FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'workspace_tenant_creation_claim_immutable'); END;

-- Existing tenants receive deterministic organization projections. No event is emitted for
-- backfill, so pristine down can distinguish migration equivalence from new product facts.
INSERT INTO workspaces(
  workspace_id,kind,status,profile_state,creation_source,revision,
  created_by_account_id,created_at,updated_at)
SELECT
  'wsp_'||substr(metahub_sha256(t.tenant_id),1,32),'organization',
  CASE WHEN EXISTS(SELECT 1 FROM users u WHERE u.tenant_id=t.tenant_id
        AND u.role='admin' AND u.status='active')
      AND EXISTS(SELECT 1 FROM environments e WHERE e.tenant_id=t.tenant_id
        AND e.code='dev' AND e.status='active')
    THEN 'active' ELSE 'migration_conflict' END,
  CASE WHEN EXISTS(SELECT 1 FROM users u WHERE u.tenant_id=t.tenant_id
        AND u.role='admin' AND u.status='active')
      AND EXISTS(SELECT 1 FROM environments e WHERE e.tenant_id=t.tenant_id
        AND e.code='dev' AND e.status='active')
    THEN 'confirmed' ELSE 'migration_conflict' END,
  CASE WHEN t.slug='default' THEN 'legacy_bootstrap' ELSE 'legacy_backfill' END,1,
  (SELECT l.account_id FROM users u JOIN account_identity_links l
    ON l.legacy_tenant_id=u.tenant_id AND l.legacy_user_id=u.user_id
    WHERE u.tenant_id=t.tenant_id AND u.role='admin' AND u.status='active'
    ORDER BY u.created_at,u.user_id LIMIT 1),
  t.created_at,t.updated_at
FROM tenants t;

INSERT INTO workspace_tenant_bindings(
  workspace_tenant_binding_id,workspace_id,tenant_id,status,created_at)
SELECT 'wtb_'||substr(metahub_sha256(t.tenant_id),1,32),
  'wsp_'||substr(metahub_sha256(t.tenant_id),1,32),t.tenant_id,'active',t.created_at
FROM tenants t JOIN workspaces w
  ON w.workspace_id='wsp_'||substr(metahub_sha256(t.tenant_id),1,32);

INSERT INTO workspace_regions(
  workspace_region_id,workspace_id,tenant_id,region_code,
  residency_policy_version,status,created_at)
SELECT 'wrg_'||substr(metahub_sha256(t.tenant_id),1,32),w.workspace_id,t.tenant_id,
  'cn-north-1','legacy-domestic-v1','active',t.created_at
FROM tenants t JOIN workspaces w
  ON w.workspace_id='wsp_'||substr(metahub_sha256(t.tenant_id),1,32);

INSERT INTO organization_profiles(
  organization_profile_id,workspace_id,name,slug_normalized,profile_state,
  revision,created_at,updated_at)
SELECT 'org_'||substr(metahub_sha256(t.tenant_id),1,32),w.workspace_id,t.name,
  lower(t.slug),w.profile_state,1,t.created_at,t.updated_at
FROM tenants t JOIN workspaces w
  ON w.workspace_id='wsp_'||substr(metahub_sha256(t.tenant_id),1,32);

INSERT INTO workspace_memberships(
  workspace_membership_id,workspace_id,tenant_id,account_id,legacy_user_id,
  role_code,status,source,revision,created_at,updated_at)
SELECT 'wmb_'||substr(metahub_sha256(t.tenant_id),1,32),w.workspace_id,t.tenant_id,
  l.account_id,u.user_id,'owner','active','legacy_backfill',1,t.created_at,t.updated_at
FROM tenants t JOIN workspaces w
  ON w.workspace_id='wsp_'||substr(metahub_sha256(t.tenant_id),1,32)
JOIN users u ON u.user_id=(SELECT candidate.user_id FROM users candidate
  WHERE candidate.tenant_id=t.tenant_id AND candidate.role='admin'
    AND candidate.status='active' ORDER BY candidate.created_at,candidate.user_id LIMIT 1)
JOIN account_identity_links l
  ON l.legacy_tenant_id=u.tenant_id AND l.legacy_user_id=u.user_id
WHERE w.status='active';

INSERT INTO workspace_provisioning_jobs(
  workspace_provisioning_job_id,workspace_id,tenant_id,generation,status,
  attempt_count,error_code,next_attempt_at,created_at,updated_at)
SELECT 'wpj_'||substr(metahub_sha256(t.tenant_id),1,32),w.workspace_id,t.tenant_id,1,
  CASE WHEN w.status='active' THEN 'ready' ELSE 'failed' END,0,
  CASE WHEN w.status='active' THEN NULL ELSE 'workspace.migration_conflict' END,
  NULL,t.created_at,t.updated_at
FROM tenants t JOIN workspaces w
  ON w.workspace_id='wsp_'||substr(metahub_sha256(t.tenant_id),1,32);

INSERT INTO workspace_environment_origins(
  workspace_environment_origin_id,workspace_id,tenant_id,environment_id,origin,created_at)
SELECT 'weo_'||substr(metahub_sha256(e.tenant_id||char(31)||e.environment_id),1,32),
  'wsp_'||substr(metahub_sha256(e.tenant_id),1,32),e.tenant_id,e.environment_id,
  'legacy_existing',e.created_at
FROM environments e;

-- Tenants created by legacy seed/import code after 0085 receive the same deterministic
-- projection. A user-created Workspace places an internal pending claim before Tenant insert,
-- so this compatibility trigger cannot manufacture a second Workspace.
CREATE TRIGGER trg_tenants_workspace_backfill_prd093
AFTER INSERT ON tenants FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM workspace_tenant_creation_claims c
  WHERE c.tenant_id=NEW.tenant_id AND c.status='pending'
    AND c.expires_at>=strftime('%Y-%m-%dT%H:%M:%SZ','now'))
BEGIN
  INSERT INTO workspaces(
    workspace_id,kind,status,profile_state,creation_source,revision,
    created_by_account_id,created_at,updated_at)
  VALUES(
    'wsp_'||substr(metahub_sha256(NEW.tenant_id),1,32),'organization',
    'migration_conflict','migration_conflict',
    CASE WHEN NEW.slug='default' THEN 'legacy_bootstrap' ELSE 'legacy_backfill' END,
    1,NULL,NEW.created_at,NEW.updated_at);
  INSERT INTO workspace_tenant_bindings(
    workspace_tenant_binding_id,workspace_id,tenant_id,status,created_at)
  VALUES('wtb_'||substr(metahub_sha256(NEW.tenant_id),1,32),
    'wsp_'||substr(metahub_sha256(NEW.tenant_id),1,32),NEW.tenant_id,
    'active',NEW.created_at);
  INSERT INTO workspace_regions(
    workspace_region_id,workspace_id,tenant_id,region_code,
    residency_policy_version,status,created_at)
  VALUES('wrg_'||substr(metahub_sha256(NEW.tenant_id),1,32),
    'wsp_'||substr(metahub_sha256(NEW.tenant_id),1,32),NEW.tenant_id,
    'cn-north-1','legacy-domestic-v1','active',NEW.created_at);
  INSERT INTO organization_profiles(
    organization_profile_id,workspace_id,name,slug_normalized,profile_state,
    revision,created_at,updated_at)
  VALUES('org_'||substr(metahub_sha256(NEW.tenant_id),1,32),
    'wsp_'||substr(metahub_sha256(NEW.tenant_id),1,32),NEW.name,lower(NEW.slug),
    'migration_conflict',1,NEW.created_at,NEW.updated_at);
  INSERT INTO workspace_provisioning_jobs(
    workspace_provisioning_job_id,workspace_id,tenant_id,generation,status,
    attempt_count,error_code,next_attempt_at,created_at,updated_at)
  VALUES('wpj_'||substr(metahub_sha256(NEW.tenant_id),1,32),
    'wsp_'||substr(metahub_sha256(NEW.tenant_id),1,32),NEW.tenant_id,1,'failed',0,
    'workspace.migration_conflict',NULL,NEW.created_at,NEW.updated_at);
  INSERT OR IGNORE INTO workspace_environment_origins(
    workspace_environment_origin_id,workspace_id,tenant_id,environment_id,origin,created_at)
  SELECT 'weo_'||substr(metahub_sha256(e.tenant_id||char(31)||e.environment_id),1,32),
    'wsp_'||substr(metahub_sha256(e.tenant_id),1,32),e.tenant_id,e.environment_id,
    'legacy_existing',e.created_at
  FROM environments e WHERE e.tenant_id=NEW.tenant_id;
END;

CREATE TRIGGER trg_environments_workspace_origin_prd093
AFTER INSERT ON environments FOR EACH ROW
BEGIN
  INSERT OR IGNORE INTO workspace_environment_origins(
    workspace_environment_origin_id,workspace_id,tenant_id,environment_id,origin,created_at)
  SELECT 'weo_'||substr(metahub_sha256(NEW.tenant_id||char(31)||NEW.environment_id),1,32),
    b.workspace_id,NEW.tenant_id,NEW.environment_id,'legacy_existing',NEW.created_at
  FROM workspace_tenant_bindings b
  JOIN workspaces w ON w.workspace_id=b.workspace_id
  WHERE b.tenant_id=NEW.tenant_id AND w.creation_source!='user_created';
END;

-- A pending projection claim is an internal, one-use proof. A malformed or expired claim
-- aborts the User insert instead of falling through to the conflict-isolated legacy branch.
CREATE TRIGGER trg_users_projection_claim_validate_prd093
BEFORE INSERT ON users FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM workspace_user_projection_claims c
    WHERE c.tenant_id=NEW.tenant_id AND c.legacy_user_id=NEW.user_id
      AND c.status='pending')
 AND NOT EXISTS(
    SELECT 1 FROM workspace_user_projection_claims c
    JOIN global_accounts a ON a.account_id=c.account_id
    JOIN account_credentials credential ON credential.account_id=a.account_id
      AND credential.credential_type='password' AND credential.status='active'
    WHERE c.tenant_id=NEW.tenant_id AND c.legacy_user_id=NEW.user_id
      AND c.status='pending' AND c.expires_at>=strftime('%Y-%m-%dT%H:%M:%SZ','now')
      AND c.credential_revision=a.credential_revision
      AND c.email_normalized=lower(trim(NEW.email))
      AND credential.secret_hash=NEW.password_hash
      AND a.status='active' AND a.login_enabled=1
      AND EXISTS(SELECT 1 FROM account_verifications verification
        WHERE verification.account_id=a.account_id)
 )
BEGIN SELECT RAISE(ABORT,'workspace_projection_claim_invalid'); END;

DROP TRIGGER trg_users_identity_insert_prd091;
CREATE TRIGGER trg_users_identity_insert_prd093
AFTER INSERT ON users FOR EACH ROW
BEGIN
  INSERT INTO global_accounts(
    account_id,email_normalized,phone_e164,display_name,status,login_enabled,
    credential_revision,session_revision,mfa_state,failed_login_count,locked_until,
    last_login_at,created_at,updated_at)
  SELECT
    'gac_'||substr(metahub_sha256(NEW.tenant_id||char(31)||NEW.user_id),1,32),
    lower(trim(NEW.email)),NULL,NEW.display_name,
    CASE WHEN length(trim(NEW.email))!=length(CAST(trim(NEW.email) AS BLOB))
        OR length(trim(NEW.email)) NOT BETWEEN 3 AND 320
        OR instr(trim(NEW.email),'@')<=1
        OR instr(substr(trim(NEW.email),instr(trim(NEW.email),'@')+1),'@')>0
        OR instr(trim(NEW.email),' ')>0
        OR metahub_password_hash_algorithm(NEW.password_hash) IS NULL
        OR (SELECT COUNT(*) FROM users x
          WHERE lower(trim(x.email))=lower(trim(NEW.email)))!=1
      THEN 'migration_conflict' ELSE NEW.status END,
    CASE WHEN length(trim(NEW.email))=length(CAST(trim(NEW.email) AS BLOB))
        AND length(trim(NEW.email)) BETWEEN 3 AND 320
        AND instr(trim(NEW.email),'@')>1
        AND instr(substr(trim(NEW.email),instr(trim(NEW.email),'@')+1),'@')=0
        AND instr(trim(NEW.email),' ')=0
        AND metahub_password_hash_algorithm(NEW.password_hash) IS NOT NULL
        AND (SELECT COUNT(*) FROM users x
          WHERE lower(trim(x.email))=lower(trim(NEW.email)))=1
      THEN 1 ELSE 0 END,
    1,1,'not_enrolled',NEW.failed_login_count,NEW.locked_until,NEW.last_login_at,
    NEW.created_at,NEW.updated_at
  WHERE NOT EXISTS(SELECT 1 FROM workspace_user_projection_claims c
    WHERE c.tenant_id=NEW.tenant_id AND c.legacy_user_id=NEW.user_id
      AND c.status='pending');

  INSERT INTO account_credentials(
    credential_id,account_id,credential_type,algorithm,secret_hash,status,version,
    created_at,updated_at,rotated_at)
  SELECT
    'acr_'||substr(metahub_sha256(NEW.tenant_id||char(31)||NEW.user_id),1,32),
    'gac_'||substr(metahub_sha256(NEW.tenant_id||char(31)||NEW.user_id),1,32),
    'password',coalesce(metahub_password_hash_algorithm(NEW.password_hash),'unknown'),
    NEW.password_hash,'active',1,NEW.created_at,NEW.updated_at,NULL
  WHERE NOT EXISTS(SELECT 1 FROM workspace_user_projection_claims c
    WHERE c.tenant_id=NEW.tenant_id AND c.legacy_user_id=NEW.user_id
      AND c.status='pending');

  INSERT INTO account_identity_links(
    identity_link_id,account_id,source_type,legacy_tenant_id,legacy_user_id,status,linked_at)
  SELECT
    'ail_'||substr(metahub_sha256(NEW.tenant_id||char(31)||NEW.user_id),1,32),
    'gac_'||substr(metahub_sha256(NEW.tenant_id||char(31)||NEW.user_id),1,32),
    'legacy_user',NEW.tenant_id,NEW.user_id,'active',NEW.created_at
  WHERE NOT EXISTS(SELECT 1 FROM workspace_user_projection_claims c
    WHERE c.tenant_id=NEW.tenant_id AND c.legacy_user_id=NEW.user_id
      AND c.status='pending');

  INSERT INTO account_identity_links(
    identity_link_id,account_id,source_type,legacy_tenant_id,legacy_user_id,status,linked_at)
  SELECT
    'ail_'||substr(metahub_sha256(NEW.tenant_id||char(31)||NEW.user_id),1,32),
    c.account_id,'legacy_user',NEW.tenant_id,NEW.user_id,'active',NEW.created_at
  FROM workspace_user_projection_claims c
  WHERE c.tenant_id=NEW.tenant_id AND c.legacy_user_id=NEW.user_id
    AND c.status='pending';

  INSERT OR IGNORE INTO workspace_memberships(
    workspace_membership_id,workspace_id,tenant_id,account_id,legacy_user_id,
    role_code,status,source,revision,created_at,updated_at)
  SELECT 'wmb_'||substr(metahub_sha256(NEW.tenant_id),1,32),w.workspace_id,NEW.tenant_id,
    l.account_id,NEW.user_id,'owner','active','legacy_backfill',1,NEW.created_at,NEW.updated_at
  FROM workspaces w
  JOIN workspace_tenant_bindings b ON b.workspace_id=w.workspace_id
  JOIN account_identity_links l ON l.legacy_tenant_id=NEW.tenant_id
    AND l.legacy_user_id=NEW.user_id AND l.status='active'
  JOIN global_accounts a ON a.account_id=l.account_id
  WHERE b.tenant_id=NEW.tenant_id AND w.creation_source!='user_created'
    AND NEW.role='admin' AND NEW.status='active'
    AND a.status='active' AND a.login_enabled=1
    AND EXISTS(SELECT 1 FROM environments e WHERE e.tenant_id=NEW.tenant_id
      AND e.code='dev' AND e.status='active')
    AND NOT EXISTS(SELECT 1 FROM workspace_memberships existing
      WHERE existing.workspace_id=w.workspace_id);

  UPDATE workspaces
  SET status='active',profile_state='confirmed',created_by_account_id=(
      SELECT l.account_id FROM account_identity_links l
      WHERE l.legacy_tenant_id=NEW.tenant_id AND l.legacy_user_id=NEW.user_id),
      updated_at=NEW.updated_at
  WHERE workspace_id='wsp_'||substr(metahub_sha256(NEW.tenant_id),1,32)
    AND creation_source!='user_created'
    AND EXISTS(SELECT 1 FROM workspace_memberships m
      WHERE m.workspace_id=workspaces.workspace_id AND m.legacy_user_id=NEW.user_id);
  UPDATE organization_profiles SET profile_state='confirmed',updated_at=NEW.updated_at
  WHERE workspace_id='wsp_'||substr(metahub_sha256(NEW.tenant_id),1,32)
    AND EXISTS(SELECT 1 FROM workspace_memberships m
      WHERE m.workspace_id=organization_profiles.workspace_id
        AND m.legacy_user_id=NEW.user_id);
  UPDATE workspace_provisioning_jobs
  SET status='ready',error_code=NULL,updated_at=NEW.updated_at
  WHERE tenant_id=NEW.tenant_id AND EXISTS(SELECT 1 FROM workspace_memberships m
    WHERE m.workspace_id=workspace_provisioning_jobs.workspace_id
      AND m.legacy_user_id=NEW.user_id);

  UPDATE global_accounts SET status='migration_conflict',login_enabled=0,updated_at=NEW.updated_at
  WHERE account_id IN(
    SELECT l.account_id FROM account_identity_links l JOIN users u
      ON u.tenant_id=l.legacy_tenant_id AND u.user_id=l.legacy_user_id
    WHERE lower(trim(u.email))=lower(trim(NEW.email))
  ) AND (SELECT COUNT(*) FROM users x
    WHERE lower(trim(x.email))=lower(trim(NEW.email)))>1
    AND NOT EXISTS(SELECT 1 FROM workspace_user_projection_claims c
      WHERE c.tenant_id=NEW.tenant_id AND c.legacy_user_id=NEW.user_id
        AND c.status='pending');

  INSERT OR IGNORE INTO identity_migration_conflicts(
    conflict_id,account_id,identifier_fingerprint,reason_code,status,detected_at)
  SELECT 'icf_'||substr(metahub_sha256(l.legacy_tenant_id||char(31)||l.legacy_user_id),1,32),
    l.account_id,metahub_sha256(lower(trim(NEW.email))),'duplicate_identifier','open',
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM account_identity_links l JOIN users u
    ON u.tenant_id=l.legacy_tenant_id AND u.user_id=l.legacy_user_id
  WHERE lower(trim(u.email))=lower(trim(NEW.email))
    AND (SELECT COUNT(*) FROM users x
      WHERE lower(trim(x.email))=lower(trim(NEW.email)))>1
    AND NOT EXISTS(SELECT 1 FROM workspace_user_projection_claims c
      WHERE c.tenant_id=NEW.tenant_id AND c.legacy_user_id=NEW.user_id
        AND c.status='pending');

  INSERT OR IGNORE INTO identity_migration_conflicts(
    conflict_id,account_id,identifier_fingerprint,reason_code,status,detected_at)
  SELECT 'icf_'||substr(metahub_sha256(NEW.tenant_id||char(31)||NEW.user_id),1,32),
    'gac_'||substr(metahub_sha256(NEW.tenant_id||char(31)||NEW.user_id),1,32),
    metahub_sha256(lower(trim(NEW.email))),
    CASE WHEN length(trim(NEW.email))!=length(CAST(trim(NEW.email) AS BLOB))
        THEN 'identifier_requires_claim'
      WHEN metahub_password_hash_algorithm(NEW.password_hash) IS NULL
        THEN 'credential_unrecognized'
      ELSE 'identifier_invalid' END,
    'open',strftime('%Y-%m-%dT%H:%M:%SZ','now')
  WHERE (SELECT status FROM global_accounts WHERE account_id=
    'gac_'||substr(metahub_sha256(NEW.tenant_id||char(31)||NEW.user_id),1,32))
    ='migration_conflict'
    AND NOT EXISTS(SELECT 1 FROM workspace_user_projection_claims c
      WHERE c.tenant_id=NEW.tenant_id AND c.legacy_user_id=NEW.user_id
        AND c.status='pending');

  UPDATE workspace_user_projection_claims
  SET status='consumed',consumed_at=NEW.created_at
  WHERE tenant_id=NEW.tenant_id AND legacy_user_id=NEW.user_id AND status='pending';
END;

DROP TRIGGER trg_users_identity_password_update_prd091;
CREATE TRIGGER trg_users_identity_password_update_prd093
AFTER UPDATE OF password_hash ON users FOR EACH ROW WHEN NEW.password_hash!=OLD.password_hash
BEGIN
  UPDATE account_credentials
  SET secret_hash=NEW.password_hash,
      algorithm=coalesce(metahub_password_hash_algorithm(NEW.password_hash),'unknown'),
      version=version+1,updated_at=NEW.updated_at,rotated_at=NEW.updated_at
  WHERE account_id=(SELECT account_id FROM account_identity_links
    WHERE source_type='legacy_user' AND legacy_tenant_id=NEW.tenant_id
      AND legacy_user_id=NEW.user_id);
  UPDATE global_accounts
  SET credential_revision=credential_revision+1,session_revision=session_revision+1,
      updated_at=NEW.updated_at,
      status=CASE WHEN metahub_password_hash_algorithm(NEW.password_hash) IS NOT NULL
        THEN CASE WHEN login_enabled=1 THEN NEW.status ELSE status END
        ELSE 'migration_conflict' END,
      login_enabled=CASE WHEN metahub_password_hash_algorithm(NEW.password_hash) IS NOT NULL
        THEN login_enabled ELSE 0 END
  WHERE account_id=(SELECT account_id FROM account_identity_links
    WHERE source_type='legacy_user' AND legacy_tenant_id=NEW.tenant_id
      AND legacy_user_id=NEW.user_id);
  UPDATE global_sessions
  SET status='revoked',revoked_at=NEW.updated_at,revoked_reason='credential_rotated',
      updated_at=NEW.updated_at
  WHERE account_id=(SELECT account_id FROM account_identity_links
    WHERE source_type='legacy_user' AND legacy_tenant_id=NEW.tenant_id
      AND legacy_user_id=NEW.user_id) AND status='active';
  UPDATE users
  SET password_hash=NEW.password_hash,updated_at=NEW.updated_at
  WHERE (tenant_id,user_id) IN(
    SELECT legacy_tenant_id,legacy_user_id FROM account_identity_links
    WHERE account_id=(SELECT account_id FROM account_identity_links
      WHERE source_type='legacy_user' AND legacy_tenant_id=NEW.tenant_id
        AND legacy_user_id=NEW.user_id)
      AND source_type='legacy_user' AND status='active'
  ) AND NOT(tenant_id=NEW.tenant_id AND user_id=NEW.user_id)
    AND password_hash!=NEW.password_hash;
END;

CREATE TEMP TABLE prd093_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd093_up_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM workspaces)=(SELECT COUNT(*) FROM tenants) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM workspace_tenant_bindings)=(SELECT COUNT(*) FROM tenants)
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM workspaces w JOIN workspace_tenant_bindings b
        ON b.workspace_id=w.workspace_id
      WHERE w.status='active' AND NOT EXISTS(
        SELECT 1 FROM workspace_memberships m
        WHERE m.workspace_id=w.workspace_id AND m.role_code='owner' AND m.status='active'))
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_foreign_key_check)=0 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check='ok')=1
      THEN 0 ELSE 1 END;
DROP TABLE prd093_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
