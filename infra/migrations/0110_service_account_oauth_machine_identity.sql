-- metahub:expected-version-v1
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd118_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd118_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0109' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_tenant_bindings','global_accounts','workspace_memberships',
      'workspace_governance_permission_extensions','tenant_deletion_table_manifest',
      'tenant_deletion_manifest_extensions'))=6 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd118')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd118_up_guard;

-- Workspace machine-identity administration is separate from the legacy tenant RBAC catalog.
-- It is available only to the explicitly assigned Workspace roles below.
CREATE TABLE workspace_machine_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'machine_identity.read','machine_identity.manage')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-118'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=29)
);
CREATE TABLE workspace_machine_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'owner','admin','developer','security_admin','finance_admin','legal_admin','auditor','viewer')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code) REFERENCES workspace_machine_permission_extensions(permission_code)
);
INSERT INTO workspace_machine_permission_extensions VALUES
  ('machine_identity.read','PRD-118',29),
  ('machine_identity.manage','PRD-118',29);
INSERT INTO workspace_machine_role_permissions VALUES
  ('owner','machine_identity.read'),('owner','machine_identity.manage'),
  ('security_admin','machine_identity.read'),('security_admin','machine_identity.manage'),
  ('developer','machine_identity.read');
CREATE TRIGGER trg_workspace_machine_permissions_insert_prd118
BEFORE INSERT ON workspace_machine_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_machine_permission_immutable'); END;
CREATE TRIGGER trg_workspace_machine_permissions_update_prd118
BEFORE UPDATE ON workspace_machine_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_machine_permission_immutable'); END;
CREATE TRIGGER trg_workspace_machine_permissions_delete_prd118
BEFORE DELETE ON workspace_machine_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_machine_permission_immutable'); END;
CREATE TRIGGER trg_workspace_machine_roles_insert_prd118
BEFORE INSERT ON workspace_machine_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_machine_permission_immutable'); END;
CREATE TRIGGER trg_workspace_machine_roles_update_prd118
BEFORE UPDATE ON workspace_machine_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_machine_permission_immutable'); END;
CREATE TRIGGER trg_workspace_machine_roles_delete_prd118
BEFORE DELETE ON workspace_machine_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_machine_permission_immutable'); END;

-- This catalog is both the grant ceiling and the route allowlist. A scope can be valid in
-- tenant RBAC and still remain unavailable to machine principals unless it is listed here.
CREATE TABLE machine_permission_catalog(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'provider.read','request_log.read')),
  scope_template TEXT NOT NULL CHECK(scope_template IN(
    'provider_reader','observability_reader')),
  http_method TEXT NOT NULL CHECK(http_method='GET'),
  route_template TEXT NOT NULL CHECK(route_template IN('/providers','/request-logs')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=1),
  status TEXT NOT NULL CHECK(status='active'),
  UNIQUE(http_method,route_template),
  UNIQUE(scope_template,permission_code)
);
INSERT INTO machine_permission_catalog VALUES
  ('provider.read','provider_reader','GET','/providers',1,'active'),
  ('request_log.read','observability_reader','GET','/request-logs',1,'active');
CREATE TRIGGER trg_machine_permission_catalog_insert_prd118
BEFORE INSERT ON machine_permission_catalog
BEGIN SELECT RAISE(ABORT,'machine_permission_catalog_immutable'); END;
CREATE TRIGGER trg_machine_permission_catalog_update_prd118
BEFORE UPDATE ON machine_permission_catalog
BEGIN SELECT RAISE(ABORT,'machine_permission_catalog_immutable'); END;
CREATE TRIGGER trg_machine_permission_catalog_delete_prd118
BEFORE DELETE ON machine_permission_catalog
BEGIN SELECT RAISE(ABORT,'machine_permission_catalog_immutable'); END;

CREATE TABLE service_accounts(
  service_account_id TEXT PRIMARY KEY CHECK(length(service_account_id)=36
    AND substr(service_account_id,1,4)='svc_'
    AND substr(service_account_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  name TEXT NOT NULL CHECK(length(name) BETWEEN 2 AND 80
    AND name=lower(name) AND name NOT GLOB '*[^a-z0-9_-]*'),
  display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 2 AND 120),
  status TEXT NOT NULL CHECK(status IN('active','disabled','revoked')),
  scope_template TEXT NOT NULL CHECK(scope_template IN(
    'provider_reader','observability_reader')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=1),
  constraint_sha256 TEXT NOT NULL CHECK(length(constraint_sha256)=64
    AND constraint_sha256 NOT GLOB '*[^0-9a-f]*'),
  constraint_generation INTEGER NOT NULL CHECK(constraint_generation>=1),
  ip_allowlist_json TEXT NOT NULL CHECK(json_valid(ip_allowlist_json)
    AND json_type(ip_allowlist_json)='array' AND length(ip_allowlist_json)<=2048),
  mtls_fingerprint_sha256 TEXT CHECK(mtls_fingerprint_sha256 IS NULL
    OR(length(mtls_fingerprint_sha256)=64
      AND mtls_fingerprint_sha256 NOT GLOB '*[^0-9a-f]*')),
  token_ttl_seconds INTEGER NOT NULL CHECK(token_ttl_seconds BETWEEN 60 AND 3600),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_by_account_id TEXT NOT NULL,
  updated_by_account_id TEXT NOT NULL,
  last_used_at TEXT CHECK(last_used_at IS NULL OR unixepoch(last_used_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(created_by_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(updated_by_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,name),
  UNIQUE(workspace_id,tenant_id,service_account_id),
  CHECK((status='revoked' AND revoked_at IS NOT NULL)
    OR(status!='revoked' AND revoked_at IS NULL))
);
CREATE INDEX idx_service_accounts_owner_prd118
  ON service_accounts(workspace_id,tenant_id,status,created_at,service_account_id);
CREATE TRIGGER trg_service_accounts_update_prd118
BEFORE UPDATE ON service_accounts FOR EACH ROW
WHEN NEW.service_account_id!=OLD.service_account_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.name!=OLD.name OR NEW.display_name!=OLD.display_name
  OR NEW.scope_template!=OLD.scope_template OR NEW.catalog_revision!=OLD.catalog_revision
  OR NEW.constraint_sha256!=OLD.constraint_sha256
  OR NEW.ip_allowlist_json!=OLD.ip_allowlist_json
  OR NEW.mtls_fingerprint_sha256 IS NOT OLD.mtls_fingerprint_sha256
  OR NEW.token_ttl_seconds!=OLD.token_ttl_seconds
  OR NEW.created_by_account_id!=OLD.created_by_account_id OR NEW.created_at!=OLD.created_at
  OR NEW.revision NOT IN(OLD.revision,OLD.revision+1)
  OR NEW.constraint_generation NOT IN(OLD.constraint_generation,OLD.constraint_generation+1)
  OR (NEW.revision=OLD.revision AND NEW.constraint_generation!=OLD.constraint_generation)
  OR (NEW.revision=OLD.revision+1 AND NEW.constraint_generation!=OLD.constraint_generation+1)
  OR (NEW.revision=OLD.revision AND (NEW.status!=OLD.status
      OR NEW.updated_by_account_id!=OLD.updated_by_account_id
      OR NEW.revoked_at IS NOT OLD.revoked_at))
  OR (NEW.revision=OLD.revision+1 AND NOT(
      OLD.status IN('active','disabled') AND NEW.status='revoked'
      AND OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL))
  OR (NEW.last_used_at IS NOT NULL AND OLD.last_used_at IS NOT NULL
      AND NEW.last_used_at<OLD.last_used_at)
  OR NEW.updated_at<OLD.updated_at
BEGIN SELECT RAISE(ABORT,'service_account_transition_invalid'); END;
CREATE TRIGGER trg_service_accounts_delete_prd118
BEFORE DELETE ON service_accounts
BEGIN SELECT RAISE(ABORT,'service_account_immutable'); END;

CREATE TABLE service_account_scopes(
  service_account_id TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  PRIMARY KEY(service_account_id,permission_code),
  FOREIGN KEY(service_account_id) REFERENCES service_accounts(service_account_id),
  FOREIGN KEY(permission_code) REFERENCES machine_permission_catalog(permission_code)
);
CREATE TRIGGER trg_service_account_scopes_insert_prd118
BEFORE INSERT ON service_account_scopes FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM service_accounts account
  JOIN machine_permission_catalog catalog
    ON catalog.permission_code=NEW.permission_code
   AND catalog.scope_template=account.scope_template AND catalog.status='active'
  WHERE account.service_account_id=NEW.service_account_id)
BEGIN SELECT RAISE(ABORT,'service_account_scope_invalid'); END;
CREATE TRIGGER trg_service_account_scopes_update_prd118
BEFORE UPDATE ON service_account_scopes
BEGIN SELECT RAISE(ABORT,'service_account_scope_immutable'); END;
CREATE TRIGGER trg_service_account_scopes_delete_prd118
BEFORE DELETE ON service_account_scopes
BEGIN SELECT RAISE(ABORT,'service_account_scope_immutable'); END;

CREATE TABLE oauth_clients(
  oauth_client_id TEXT PRIMARY KEY CHECK(length(oauth_client_id)=36
    AND substr(oauth_client_id,1,4)='ocl_'
    AND substr(oauth_client_id,5) NOT GLOB '*[^0-9a-f]*'),
  client_id TEXT NOT NULL UNIQUE CHECK(length(client_id)=36
    AND substr(client_id,1,4)='moc_'
    AND substr(client_id,5) NOT GLOB '*[^0-9a-f]*'),
  service_account_id TEXT NOT NULL UNIQUE,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  audience TEXT NOT NULL CHECK(audience='metahub-control'),
  status TEXT NOT NULL CHECK(status IN('active','rotating','revoked')),
  credential_generation INTEGER NOT NULL CHECK(credential_generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  last_used_at TEXT CHECK(last_used_at IS NULL OR unixepoch(last_used_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,service_account_id)
    REFERENCES service_accounts(workspace_id,tenant_id,service_account_id),
  UNIQUE(workspace_id,tenant_id,oauth_client_id),
  CHECK((status='revoked' AND revoked_at IS NOT NULL)
    OR(status!='revoked' AND revoked_at IS NULL))
);
CREATE INDEX idx_oauth_clients_owner_prd118
  ON oauth_clients(workspace_id,tenant_id,status,created_at,oauth_client_id);
CREATE TRIGGER trg_oauth_clients_delete_prd118
BEFORE DELETE ON oauth_clients
BEGIN SELECT RAISE(ABORT,'oauth_client_immutable'); END;

CREATE TABLE client_credentials(
  client_credential_id TEXT PRIMARY KEY CHECK(length(client_credential_id)=36
    AND substr(client_credential_id,1,4)='occ_'
    AND substr(client_credential_id,5) NOT GLOB '*[^0-9a-f]*'),
  oauth_client_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  secret_hash TEXT NOT NULL CHECK(length(secret_hash)=64
    AND secret_hash NOT GLOB '*[^0-9a-f]*'),
  secret_prefix TEXT NOT NULL CHECK(length(secret_prefix)=12
    AND substr(secret_prefix,1,4)='mcs_'),
  status TEXT NOT NULL CHECK(status IN('active','grace','revoked')),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  grace_expires_at TEXT CHECK(grace_expires_at IS NULL
    OR unixepoch(grace_expires_at) IS NOT NULL),
  last_used_at TEXT CHECK(last_used_at IS NULL OR unixepoch(last_used_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,oauth_client_id)
    REFERENCES oauth_clients(workspace_id,tenant_id,oauth_client_id),
  UNIQUE(oauth_client_id,generation),
  UNIQUE(workspace_id,tenant_id,client_credential_id),
  CHECK((status='active' AND grace_expires_at IS NULL AND revoked_at IS NULL)
    OR(status='grace' AND grace_expires_at IS NOT NULL AND revoked_at IS NULL)
    OR(status='revoked' AND revoked_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_client_credential_active_prd118
  ON client_credentials(oauth_client_id) WHERE status='active';
CREATE INDEX idx_client_credentials_owner_prd118
  ON client_credentials(workspace_id,tenant_id,oauth_client_id,status,generation);
CREATE TRIGGER trg_client_credentials_insert_prd118
BEFORE INSERT ON client_credentials FOR EACH ROW
WHEN NEW.status!='active' OR NOT EXISTS(
  SELECT 1 FROM oauth_clients client
  WHERE client.oauth_client_id=NEW.oauth_client_id
    AND client.workspace_id=NEW.workspace_id AND client.tenant_id=NEW.tenant_id
    AND client.status='active' AND (
      NEW.generation=client.credential_generation
      OR(NEW.generation=client.credential_generation+1
        AND NOT EXISTS(SELECT 1 FROM client_credentials active
          WHERE active.oauth_client_id=client.oauth_client_id AND active.status='active')
        AND EXISTS(SELECT 1 FROM client_credentials previous
          WHERE previous.oauth_client_id=client.oauth_client_id
            AND previous.generation=client.credential_generation
            AND previous.status='grace'))))
BEGIN SELECT RAISE(ABORT,'client_credential_owner_invalid'); END;
CREATE TRIGGER trg_client_credentials_update_prd118
BEFORE UPDATE ON client_credentials FOR EACH ROW
WHEN NEW.client_credential_id!=OLD.client_credential_id
  OR NEW.oauth_client_id!=OLD.oauth_client_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.generation!=OLD.generation OR NEW.secret_hash!=OLD.secret_hash
  OR NEW.secret_prefix!=OLD.secret_prefix OR NEW.expires_at!=OLD.expires_at
  OR NEW.created_at!=OLD.created_at OR OLD.status='revoked'
  OR (OLD.last_used_at IS NOT NULL AND (NEW.last_used_at IS NULL
      OR NEW.last_used_at<OLD.last_used_at))
  OR NEW.updated_at<OLD.updated_at
  OR NOT(
    (NEW.status=OLD.status AND NEW.grace_expires_at IS OLD.grace_expires_at
      AND NEW.revoked_at IS OLD.revoked_at)
    OR(OLD.status='active' AND NEW.status='grace'
      AND OLD.grace_expires_at IS NULL AND NEW.grace_expires_at IS NOT NULL
      AND NEW.revoked_at IS OLD.revoked_at AND NEW.last_used_at IS OLD.last_used_at)
    OR(OLD.status IN('active','grace') AND NEW.status='revoked'
      AND NEW.grace_expires_at IS OLD.grace_expires_at
      AND OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL
      AND NEW.last_used_at IS OLD.last_used_at))
BEGIN SELECT RAISE(ABORT,'client_credential_transition_invalid'); END;
CREATE TRIGGER trg_client_credentials_delete_prd118
BEFORE DELETE ON client_credentials
BEGIN SELECT RAISE(ABORT,'client_credential_immutable'); END;
CREATE TRIGGER trg_oauth_clients_update_prd118
BEFORE UPDATE ON oauth_clients FOR EACH ROW
WHEN NEW.oauth_client_id!=OLD.oauth_client_id OR NEW.client_id!=OLD.client_id
  OR NEW.service_account_id!=OLD.service_account_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.audience!=OLD.audience OR NEW.created_at!=OLD.created_at
  OR OLD.status='revoked'
  OR NEW.revision NOT IN(OLD.revision,OLD.revision+1)
  OR NEW.credential_generation NOT IN(
      OLD.credential_generation,OLD.credential_generation+1)
  OR (OLD.last_used_at IS NOT NULL AND (NEW.last_used_at IS NULL
      OR NEW.last_used_at<OLD.last_used_at))
  OR NEW.updated_at<OLD.updated_at
  OR (NEW.revision=OLD.revision AND (
      NEW.status!=OLD.status
      OR NEW.credential_generation!=OLD.credential_generation
      OR NEW.revoked_at IS NOT OLD.revoked_at))
  OR (NEW.revision=OLD.revision+1 AND NOT(
      (OLD.status='active' AND NEW.status='active'
        AND NEW.credential_generation=OLD.credential_generation+1
        AND NEW.revoked_at IS OLD.revoked_at
        AND NEW.last_used_at IS OLD.last_used_at
        AND EXISTS(SELECT 1 FROM client_credentials credential
          WHERE credential.oauth_client_id=OLD.oauth_client_id
            AND credential.generation=NEW.credential_generation
            AND credential.status='active'))
      OR(OLD.status IN('active','rotating') AND NEW.status='revoked'
        AND NEW.credential_generation=OLD.credential_generation
        AND OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL
        AND NEW.last_used_at IS OLD.last_used_at)))
BEGIN SELECT RAISE(ABORT,'oauth_client_transition_invalid'); END;

CREATE TABLE access_token_grants(
  access_token_grant_id TEXT PRIMARY KEY CHECK(length(access_token_grant_id)=36
    AND substr(access_token_grant_id,1,4)='atg_'
    AND substr(access_token_grant_id,5) NOT GLOB '*[^0-9a-f]*'),
  token_hash TEXT NOT NULL UNIQUE CHECK(length(token_hash)=64
    AND token_hash NOT GLOB '*[^0-9a-f]*'),
  service_account_id TEXT NOT NULL,
  oauth_client_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  scopes_json TEXT NOT NULL CHECK(json_valid(scopes_json)
    AND json_type(scopes_json)='array' AND length(scopes_json)<=512),
  audience TEXT NOT NULL CHECK(audience='metahub-control'),
  operator_permission_revision INTEGER NOT NULL CHECK(operator_permission_revision>=1),
  operator_authorization_fingerprint TEXT NOT NULL CHECK(
    length(operator_authorization_fingerprint)=64
    AND operator_authorization_fingerprint NOT GLOB '*[^0-9a-f]*'),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=1),
  credential_generation INTEGER NOT NULL CHECK(credential_generation>=1),
  constraint_generation INTEGER NOT NULL CHECK(constraint_generation>=1),
  status TEXT NOT NULL CHECK(status IN('active','revoked')),
  issued_at TEXT NOT NULL CHECK(unixepoch(issued_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  last_used_at TEXT CHECK(last_used_at IS NULL OR unixepoch(last_used_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,service_account_id)
    REFERENCES service_accounts(workspace_id,tenant_id,service_account_id),
  FOREIGN KEY(workspace_id,tenant_id,oauth_client_id)
    REFERENCES oauth_clients(workspace_id,tenant_id,oauth_client_id),
  UNIQUE(workspace_id,tenant_id,access_token_grant_id),
  CHECK(unixepoch(expires_at)>unixepoch(issued_at)),
  CHECK((status='active' AND revoked_at IS NULL)
    OR(status='revoked' AND revoked_at IS NOT NULL))
);
CREATE INDEX idx_access_token_grants_auth_prd118
  ON access_token_grants(token_hash,status,expires_at);
CREATE INDEX idx_access_token_grants_owner_prd118
  ON access_token_grants(workspace_id,tenant_id,service_account_id,status,expires_at);
CREATE TRIGGER trg_access_token_grants_insert_prd118
BEFORE INSERT ON access_token_grants FOR EACH ROW
WHEN json_array_length(NEW.scopes_json)<1 OR NOT EXISTS(
  SELECT 1 FROM oauth_clients client
  JOIN service_accounts account
    ON account.service_account_id=client.service_account_id
   AND account.workspace_id=client.workspace_id AND account.tenant_id=client.tenant_id
  JOIN client_credentials credential
    ON credential.oauth_client_id=client.oauth_client_id
   AND credential.generation=NEW.credential_generation
  WHERE client.oauth_client_id=NEW.oauth_client_id
    AND account.service_account_id=NEW.service_account_id
    AND client.workspace_id=NEW.workspace_id AND client.tenant_id=NEW.tenant_id
    AND client.status='active' AND account.status='active'
    AND credential.status IN('active','grace'))
  OR EXISTS(SELECT 1 FROM json_each(NEW.scopes_json) requested
    WHERE requested.type!='text' OR NOT EXISTS(
      SELECT 1 FROM service_account_scopes assigned
      JOIN machine_permission_catalog catalog
        ON catalog.permission_code=assigned.permission_code AND catalog.status='active'
      WHERE assigned.service_account_id=NEW.service_account_id
        AND assigned.permission_code=requested.value))
BEGIN SELECT RAISE(ABORT,'access_token_grant_owner_invalid'); END;
CREATE TRIGGER trg_access_token_grants_update_prd118
BEFORE UPDATE ON access_token_grants FOR EACH ROW
WHEN NEW.access_token_grant_id!=OLD.access_token_grant_id
  OR NEW.token_hash!=OLD.token_hash
  OR NEW.service_account_id!=OLD.service_account_id
  OR NEW.oauth_client_id!=OLD.oauth_client_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.scopes_json!=OLD.scopes_json OR NEW.audience!=OLD.audience
  OR NEW.operator_permission_revision!=OLD.operator_permission_revision
  OR NEW.operator_authorization_fingerprint!=OLD.operator_authorization_fingerprint
  OR NEW.catalog_revision!=OLD.catalog_revision
  OR NEW.credential_generation!=OLD.credential_generation
  OR NEW.constraint_generation!=OLD.constraint_generation
  OR NEW.issued_at!=OLD.issued_at OR NEW.created_at!=OLD.created_at
  OR OLD.status='revoked'
  OR (OLD.last_used_at IS NOT NULL AND (NEW.last_used_at IS NULL
      OR NEW.last_used_at<OLD.last_used_at))
  OR NEW.updated_at<OLD.updated_at
  OR NOT(
    (NEW.status=OLD.status AND NEW.expires_at=OLD.expires_at
      AND NEW.revoked_at IS OLD.revoked_at)
    OR(OLD.status='active' AND NEW.status='active'
      AND NEW.expires_at<OLD.expires_at
      AND NEW.revoked_at IS OLD.revoked_at
      AND NEW.last_used_at IS OLD.last_used_at)
    OR(OLD.status='active' AND NEW.status='revoked'
      AND NEW.expires_at=OLD.expires_at
      AND OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL
      AND NEW.last_used_at IS OLD.last_used_at))
BEGIN SELECT RAISE(ABORT,'access_token_grant_transition_invalid'); END;
CREATE TRIGGER trg_access_token_grants_delete_prd118
BEFORE DELETE ON access_token_grants
BEGIN SELECT RAISE(ABORT,'access_token_grant_immutable'); END;

CREATE TABLE machine_identity_audits(
  machine_identity_audit_id TEXT PRIMARY KEY CHECK(length(machine_identity_audit_id)=36
    AND substr(machine_identity_audit_id,1,4)='mia_'
    AND substr(machine_identity_audit_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  subject_type TEXT NOT NULL CHECK(subject_type IN('human','service_account')),
  subject_id TEXT NOT NULL CHECK(length(subject_id) BETWEEN 4 AND 80),
  action_code TEXT NOT NULL CHECK(action_code IN(
    'service_account.created','oauth_client.created','oauth.token_issued',
    'service_account.credential_rotated','service_account.revoked',
    'machine.provider_listed','machine.request_log_listed')),
  resource_type TEXT NOT NULL CHECK(resource_type IN(
    'service_account','oauth_client','access_token_grant','provider_collection',
    'request_log_collection')),
  resource_id TEXT NOT NULL CHECK(length(resource_id) BETWEEN 1 AND 80),
  outcome TEXT NOT NULL CHECK(outcome IN('success','denied')),
  permission_code TEXT CHECK(permission_code IS NULL OR permission_code IN(
    'machine_identity.manage','provider.read','request_log.read')),
  resource_revision INTEGER NOT NULL CHECK(resource_revision>=0),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128
    AND request_id NOT GLOB '*[^A-Za-z0-9._:-]*'),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128
    AND trace_id NOT GLOB '*[^A-Za-z0-9._:-]*'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE INDEX idx_machine_identity_audits_owner_prd118
  ON machine_identity_audits(workspace_id,tenant_id,created_at,machine_identity_audit_id);
CREATE TRIGGER trg_machine_identity_audits_insert_prd118
BEFORE INSERT ON machine_identity_audits FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM workspace_tenant_bindings binding
      WHERE binding.workspace_id=NEW.workspace_id AND binding.tenant_id=NEW.tenant_id)
  OR (NEW.subject_type='human' AND NOT EXISTS(
      SELECT 1 FROM workspace_memberships member
      WHERE member.workspace_id=NEW.workspace_id AND member.tenant_id=NEW.tenant_id
        AND member.account_id=NEW.subject_id AND member.status='active'
        AND member.role_code!='service_account'))
  OR (NEW.subject_type='service_account' AND NOT EXISTS(
      SELECT 1 FROM service_accounts account
      WHERE account.workspace_id=NEW.workspace_id AND account.tenant_id=NEW.tenant_id
        AND account.service_account_id=NEW.subject_id))
  OR NOT(
    (NEW.subject_type='human' AND NEW.outcome='success'
      AND NEW.action_code='service_account.created'
      AND NEW.resource_type='service_account' AND EXISTS(
        SELECT 1 FROM service_accounts account
        WHERE account.workspace_id=NEW.workspace_id AND account.tenant_id=NEW.tenant_id
          AND account.service_account_id=NEW.resource_id
          AND account.revision=NEW.resource_revision))
    OR(NEW.subject_type='human' AND NEW.outcome='success'
      AND NEW.action_code='oauth_client.created'
      AND NEW.resource_type='oauth_client' AND EXISTS(
        SELECT 1 FROM oauth_clients client
        WHERE client.workspace_id=NEW.workspace_id AND client.tenant_id=NEW.tenant_id
          AND client.oauth_client_id=NEW.resource_id
          AND client.revision=NEW.resource_revision))
    OR(NEW.subject_type='human' AND NEW.outcome='success'
      AND NEW.action_code IN(
        'service_account.credential_rotated','service_account.revoked')
      AND NEW.resource_type='service_account' AND EXISTS(
        SELECT 1 FROM service_accounts account
        WHERE account.workspace_id=NEW.workspace_id AND account.tenant_id=NEW.tenant_id
          AND account.service_account_id=NEW.resource_id))
    OR(NEW.subject_type='service_account' AND NEW.outcome='success'
      AND NEW.action_code='oauth.token_issued'
      AND NEW.resource_type='access_token_grant' AND EXISTS(
        SELECT 1 FROM access_token_grants grant_record
        WHERE grant_record.workspace_id=NEW.workspace_id
          AND grant_record.tenant_id=NEW.tenant_id
          AND grant_record.service_account_id=NEW.subject_id
          AND grant_record.access_token_grant_id=NEW.resource_id))
    OR(NEW.subject_type='service_account'
      AND NEW.action_code='machine.provider_listed'
      AND NEW.resource_type='provider_collection' AND NEW.resource_id='/providers')
    OR(NEW.subject_type='service_account'
      AND NEW.action_code='machine.request_log_listed'
      AND NEW.resource_type='request_log_collection'
      AND NEW.resource_id='/request-logs'))
BEGIN SELECT RAISE(ABORT,'machine_identity_audit_owner_invalid'); END;
CREATE TRIGGER trg_machine_identity_audits_update_prd118
BEFORE UPDATE ON machine_identity_audits
BEGIN SELECT RAISE(ABORT,'machine_identity_audit_immutable'); END;
CREATE TRIGGER trg_machine_identity_audits_delete_prd118
BEFORE DELETE ON machine_identity_audits
BEGIN SELECT RAISE(ABORT,'machine_identity_audit_immutable'); END;

CREATE TABLE machine_identity_event_outbox(
  machine_identity_outbox_id TEXT PRIMARY KEY CHECK(length(machine_identity_outbox_id)=36
    AND substr(machine_identity_outbox_id,1,4)='mio_'
    AND substr(machine_identity_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  machine_identity_audit_id TEXT NOT NULL UNIQUE,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'service_account.created','service_account.credential_rotated',
    'service_account.revoked')),
  aggregate_id TEXT NOT NULL,
  aggregate_revision INTEGER NOT NULL CHECK(aggregate_revision>=1),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)
    AND json_type(payload_json)='object' AND length(payload_json)<=1024),
  status TEXT NOT NULL CHECK(status IN('pending','delivering','delivered','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 20),
  next_attempt_at TEXT CHECK(next_attempt_at IS NULL OR unixepoch(next_attempt_at) IS NOT NULL),
  lease_owner_id TEXT,
  lease_token TEXT,
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL
    OR unixepoch(lease_expires_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(machine_identity_audit_id)
    REFERENCES machine_identity_audits(machine_identity_audit_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  CHECK((status='pending' AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND delivered_at IS NULL)
    OR(status='delivering' AND lease_owner_id IS NOT NULL AND lease_token IS NOT NULL
      AND lease_expires_at IS NOT NULL AND delivered_at IS NULL)
    OR(status='delivered' AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND delivered_at IS NOT NULL)
    OR(status='failed' AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND delivered_at IS NULL))
);
CREATE INDEX idx_machine_identity_outbox_delivery_prd118
  ON machine_identity_event_outbox(status,next_attempt_at,lease_expires_at,created_at);
CREATE TRIGGER trg_machine_identity_outbox_insert_prd118
BEFORE INSERT ON machine_identity_event_outbox FOR EACH ROW
WHEN NOT EXISTS(
    SELECT 1 FROM machine_identity_audits audit
    JOIN service_accounts account
      ON account.workspace_id=audit.workspace_id AND account.tenant_id=audit.tenant_id
     AND account.service_account_id=NEW.aggregate_id
    WHERE audit.machine_identity_audit_id=NEW.machine_identity_audit_id
      AND audit.workspace_id=NEW.workspace_id AND audit.tenant_id=NEW.tenant_id
      AND audit.action_code=NEW.event_type AND audit.subject_type='human'
      AND audit.resource_type='service_account' AND audit.resource_id=NEW.aggregate_id
      AND audit.outcome='success' AND audit.resource_revision=NEW.aggregate_revision)
  OR (SELECT COUNT(*) FROM json_each(NEW.payload_json))!=CASE NEW.event_type
      WHEN 'service_account.revoked' THEN 3 ELSE 4 END
  OR EXISTS(SELECT 1 FROM json_each(NEW.payload_json) item
      WHERE item.key NOT IN(
        'service_account_id','revision','status','constraint_generation',
        'credential_generation'))
  OR json_extract(NEW.payload_json,'$.service_account_id')!=NEW.aggregate_id
  OR json_extract(NEW.payload_json,'$.revision')!=NEW.aggregate_revision
  OR json_extract(NEW.payload_json,'$.status') NOT IN('active','revoked')
  OR (NEW.event_type='service_account.created' AND (
      json_extract(NEW.payload_json,'$.status')!='active'
      OR json_type(NEW.payload_json,'$.constraint_generation')!='integer'
      OR json_type(NEW.payload_json,'$.credential_generation') IS NOT NULL))
  OR (NEW.event_type='service_account.credential_rotated' AND (
      json_extract(NEW.payload_json,'$.status')!='active'
      OR json_type(NEW.payload_json,'$.credential_generation')!='integer'
      OR json_type(NEW.payload_json,'$.constraint_generation') IS NOT NULL))
  OR (NEW.event_type='service_account.revoked' AND (
      json_extract(NEW.payload_json,'$.status')!='revoked'
      OR json_type(NEW.payload_json,'$.credential_generation') IS NOT NULL
      OR json_type(NEW.payload_json,'$.constraint_generation') IS NOT NULL))
BEGIN SELECT RAISE(ABORT,'machine_identity_outbox_owner_invalid'); END;
CREATE TRIGGER trg_machine_identity_outbox_update_prd118
BEFORE UPDATE ON machine_identity_event_outbox FOR EACH ROW
WHEN NEW.machine_identity_outbox_id!=OLD.machine_identity_outbox_id
  OR NEW.machine_identity_audit_id!=OLD.machine_identity_audit_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.event_type!=OLD.event_type OR NEW.aggregate_id!=OLD.aggregate_id
  OR NEW.aggregate_revision!=OLD.aggregate_revision OR NEW.payload_json!=OLD.payload_json
  OR NEW.created_at!=OLD.created_at OR NEW.updated_at<=OLD.updated_at
  OR NOT(
    (OLD.status='pending' AND NEW.status='delivering'
      AND NEW.attempt_count=OLD.attempt_count+1
      AND NEW.next_attempt_at IS OLD.next_attempt_at)
    OR(OLD.status='delivering' AND NEW.status='delivering'
      AND NEW.attempt_count=OLD.attempt_count
      AND NEW.next_attempt_at IS OLD.next_attempt_at
      AND NEW.lease_owner_id=OLD.lease_owner_id AND NEW.lease_token=OLD.lease_token
      AND NEW.lease_expires_at>OLD.lease_expires_at)
    OR(OLD.status='delivering' AND NEW.status='delivered'
      AND NEW.attempt_count=OLD.attempt_count
      AND NEW.next_attempt_at IS OLD.next_attempt_at)
    OR(OLD.status='delivering' AND NEW.status IN('pending','failed')
      AND NEW.attempt_count=OLD.attempt_count
      AND NEW.next_attempt_at IS NOT NULL)
    OR(OLD.status='failed' AND NEW.status='pending'
      AND NEW.attempt_count=OLD.attempt_count
      AND NEW.next_attempt_at IS NOT NULL))
BEGIN SELECT RAISE(ABORT,'machine_identity_outbox_transition_invalid'); END;
CREATE TRIGGER trg_machine_identity_outbox_delete_prd118
BEFORE DELETE ON machine_identity_event_outbox
BEGIN SELECT RAISE(ABORT,'machine_identity_outbox_immutable'); END;

-- Unknown client identifiers are rate limited by a keyed digest only. No client secret,
-- Authorization value, network address, or request body is persisted.
CREATE TABLE oauth_token_attempt_guards(
  client_key_sha256 TEXT PRIMARY KEY CHECK(length(client_key_sha256)=64
    AND client_key_sha256 NOT GLOB '*[^0-9a-f]*'),
  failure_count INTEGER NOT NULL CHECK(failure_count BETWEEN 0 AND 20),
  window_started_at TEXT NOT NULL CHECK(unixepoch(window_started_at) IS NOT NULL),
  blocked_until TEXT CHECK(blocked_until IS NULL OR unixepoch(blocked_until) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);

-- One immutable command record makes every PRD-118 write safely replayable without
-- persisting plaintext credentials. Secret-bearing responses are re-derived from the
-- server key, the caller-supplied idempotency key, and this opaque command identifier.
CREATE TABLE machine_identity_commands(
  machine_identity_command_id TEXT PRIMARY KEY CHECK(
    length(machine_identity_command_id)=36
    AND substr(machine_identity_command_id,1,4)='mic_'
    AND substr(machine_identity_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  subject_type TEXT NOT NULL CHECK(subject_type IN('human','service_account')),
  subject_id TEXT NOT NULL CHECK(length(subject_id) BETWEEN 4 AND 80),
  action_code TEXT NOT NULL CHECK(action_code IN(
    'service_account.create','oauth_client.create','oauth_client.rotate',
    'service_account.revoke','oauth.token.issue')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64
    AND idempotency_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  resource_type TEXT NOT NULL CHECK(resource_type IN(
    'service_account','oauth_client','access_token_grant')),
  resource_id TEXT NOT NULL CHECK(length(resource_id)=36),
  result_revision INTEGER NOT NULL CHECK(result_revision>=1),
  result_generation INTEGER NOT NULL CHECK(result_generation>=0),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128
    AND request_id NOT GLOB '*[^A-Za-z0-9._:-]*'),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128
    AND trace_id NOT GLOB '*[^A-Za-z0-9._:-]*'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(workspace_id,subject_type,subject_id,action_code,idempotency_sha256),
  UNIQUE(workspace_id,tenant_id,machine_identity_command_id)
);
CREATE INDEX idx_machine_identity_commands_owner_prd118
  ON machine_identity_commands(
    workspace_id,tenant_id,subject_type,subject_id,action_code,created_at);
CREATE TRIGGER trg_machine_identity_commands_insert_prd118
BEFORE INSERT ON machine_identity_commands FOR EACH ROW
WHEN NOT(
  (NEW.subject_type='human' AND EXISTS(
    SELECT 1 FROM workspace_memberships member
    WHERE member.workspace_id=NEW.workspace_id AND member.tenant_id=NEW.tenant_id
      AND member.account_id=NEW.subject_id AND member.status='active'))
  OR(NEW.subject_type='service_account' AND EXISTS(
    SELECT 1 FROM service_accounts account
    WHERE account.workspace_id=NEW.workspace_id AND account.tenant_id=NEW.tenant_id
      AND account.service_account_id=NEW.subject_id)))
  OR NOT(
    (NEW.action_code IN('service_account.create','service_account.revoke')
      AND NEW.resource_type='service_account' AND NEW.result_generation=0
      AND EXISTS(SELECT 1 FROM service_accounts account
        WHERE account.workspace_id=NEW.workspace_id AND account.tenant_id=NEW.tenant_id
          AND account.service_account_id=NEW.resource_id
          AND account.revision=NEW.result_revision))
    OR(NEW.action_code IN('oauth_client.create','oauth_client.rotate')
      AND NEW.resource_type='oauth_client' AND NEW.result_generation>=1
      AND EXISTS(SELECT 1 FROM oauth_clients client
        WHERE client.workspace_id=NEW.workspace_id AND client.tenant_id=NEW.tenant_id
          AND client.oauth_client_id=NEW.resource_id
          AND client.revision=NEW.result_revision
          AND client.credential_generation=NEW.result_generation))
    OR(NEW.action_code='oauth.token.issue' AND NEW.subject_type='service_account'
      AND NEW.resource_type='access_token_grant' AND NEW.result_generation>=1
      AND EXISTS(SELECT 1 FROM access_token_grants grant
        WHERE grant.workspace_id=NEW.workspace_id AND grant.tenant_id=NEW.tenant_id
          AND grant.access_token_grant_id=NEW.resource_id
          AND grant.service_account_id=NEW.subject_id
          AND grant.credential_generation=NEW.result_generation)))
BEGIN SELECT RAISE(ABORT,'machine_identity_command_owner_invalid'); END;
CREATE TRIGGER trg_machine_identity_commands_update_prd118
BEFORE UPDATE ON machine_identity_commands
BEGIN SELECT RAISE(ABORT,'machine_identity_command_immutable'); END;
CREATE TRIGGER trg_machine_identity_commands_delete_prd118
BEFORE DELETE ON machine_identity_commands
BEGIN SELECT RAISE(ABORT,'machine_identity_command_immutable'); END;

-- Register every new tenant-owned table in the fail-closed deletion inventory introduced by
-- PRD-089 and refreshed by PRD-117.
CREATE TABLE tenant_deletion_manifest_extensions_prd118(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-118'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;

CREATE TEMP TABLE prd118_manifest_sources(
  source_table TEXT PRIMARY KEY,disposition TEXT NOT NULL,retention_class TEXT);
INSERT INTO prd118_manifest_sources VALUES
  ('service_accounts','delete',NULL),
  ('oauth_clients','delete',NULL),
  ('client_credentials','delete',NULL),
  ('access_token_grants','delete',NULL),
  ('machine_identity_commands','archive_then_delete','security'),
  ('machine_identity_audits','archive_then_delete','security'),
  ('machine_identity_event_outbox','archive_then_delete','security');
INSERT INTO tenant_deletion_table_manifest(
  source_table,disposition,retention_class,table_sql_sha256,column_signature_sha256,
  trigger_set_sha256,catalog_revision,created_at)
SELECT table_record.name,source.disposition,source.retention_class,
  metahub_sha256(table_record.sql),
  metahub_sha256((SELECT group_concat(record,char(30)) FROM(
    SELECT cid||char(31)||name||char(31)||type||char(31)||"notnull"||char(31)
      ||coalesce(dflt_value,'')||char(31)||pk AS record
    FROM pragma_table_info(table_record.name) ORDER BY cid))),
  metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
    SELECT trigger_record.name||char(31)||metahub_sha256(trigger_record.sql) AS record
    FROM sqlite_schema trigger_record WHERE trigger_record.type='trigger'
      AND trigger_record.tbl_name=table_record.name ORDER BY trigger_record.name)),'')),
  1,strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM sqlite_schema table_record
JOIN prd118_manifest_sources source ON source.source_table=table_record.name
WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd118(
  source_table,tenant_key_column,source_prd,created_at)
SELECT source_table,'tenant_id','PRD-118',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd118_manifest_sources ORDER BY source_table;
DROP TABLE prd118_manifest_sources;

DELETE FROM tenant_deletion_trigger_manifest;
INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
SELECT trigger_record.name,trigger_record.tbl_name,trigger_record.sql,
  metahub_sha256(trigger_record.sql)
FROM sqlite_schema trigger_record
JOIN tenant_deletion_table_manifest manifest ON manifest.source_table=trigger_record.tbl_name
WHERE trigger_record.type='trigger' ORDER BY trigger_record.tbl_name,trigger_record.name;
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_table_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd118_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd118
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd118_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd118
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd118_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd118
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd118_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd118_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM workspace_machine_permission_extensions)=2
    AND (SELECT COUNT(*) FROM workspace_machine_role_permissions)=5 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM machine_permission_catalog)=2 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'service_accounts','service_account_scopes','oauth_clients','client_credentials',
      'access_token_grants','machine_identity_audits','machine_identity_event_outbox',
      'oauth_token_attempt_guards','machine_identity_commands'))=9 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd118)=7
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(SELECT 1 FROM workspace_memberships
      WHERE role_code='service_account' AND status='active') THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd118_up_final_guard;
COMMIT;
