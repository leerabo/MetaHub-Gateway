-- 建立企业认证、法定主体、受控证据、合同和开票资料的加密权威边界。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd102_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
-- 前置守卫要求 0093 为当前版本、依赖主体完整且不存在本 PRD 残留对象。
INSERT INTO prd102_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0093' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspaces','workspace_tenant_bindings','workspace_memberships',
      'platform_roles','platform_operators','platform_audit_logs'))=6 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd102')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd102_up_guard;

CREATE TABLE platform_verification_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code='platform.verification.review'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-102'),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=6)
);
CREATE TABLE platform_verification_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code) REFERENCES platform_verification_permission_extensions(permission_code)
);
-- 审核写权限只授予 verification 专职角色和 platform owner，读取权限仍沿用既有目录。
INSERT INTO platform_verification_permission_extensions VALUES
  ('platform.verification.review','PRD-102',6);
-- 平台 owner 与 verification reviewer 获得审核决定能力，其他平台角色保持不可写。
INSERT INTO platform_verification_role_permissions VALUES
  ('platform_owner','platform.verification.review'),
  ('platform_verification','platform.verification.review');
CREATE TRIGGER trg_platform_verification_permissions_insert_prd102
BEFORE INSERT ON platform_verification_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_verification_permission_immutable'); END;
CREATE TRIGGER trg_platform_verification_permissions_update_prd102
BEFORE UPDATE ON platform_verification_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_verification_permission_immutable'); END;
CREATE TRIGGER trg_platform_verification_permissions_delete_prd102
BEFORE DELETE ON platform_verification_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_verification_permission_immutable'); END;
CREATE TRIGGER trg_platform_verification_role_permissions_insert_prd102
BEFORE INSERT ON platform_verification_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_verification_permission_immutable'); END;
CREATE TRIGGER trg_platform_verification_role_permissions_update_prd102
BEFORE UPDATE ON platform_verification_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_verification_permission_immutable'); END;
CREATE TRIGGER trg_platform_verification_role_permissions_delete_prd102
BEFORE DELETE ON platform_verification_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_verification_permission_immutable'); END;

CREATE TABLE legal_entity_profiles(
  legal_entity_profile_id TEXT PRIMARY KEY CHECK(length(legal_entity_profile_id)=36
    AND substr(legal_entity_profile_id,1,4)='lep_'),
  workspace_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL UNIQUE,
  country_code TEXT NOT NULL CHECK(length(country_code)=2 AND country_code=upper(country_code)),
  entity_type TEXT NOT NULL CHECK(entity_type IN('company','nonprofit','government')),
  legal_name_ciphertext TEXT NOT NULL CHECK(length(legal_name_ciphertext) BETWEEN 64 AND 8192),
  registration_number_ciphertext TEXT NOT NULL CHECK(
    length(registration_number_ciphertext) BETWEEN 64 AND 8192),
  registration_blind_index TEXT NOT NULL CHECK(
    length(registration_blind_index)=76 AND substr(registration_blind_index,1,12)='hmac-sha256:'),
  key_version INTEGER NOT NULL CHECK(key_version>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  status TEXT NOT NULL CHECK(status IN('active','superseded')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE INDEX idx_legal_entity_registration_prd102
  ON legal_entity_profiles(registration_blind_index,status);
CREATE TRIGGER trg_legal_entity_owner_update_prd102
BEFORE UPDATE ON legal_entity_profiles FOR EACH ROW
WHEN NEW.legal_entity_profile_id!=OLD.legal_entity_profile_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
BEGIN SELECT RAISE(ABORT,'legal_entity_owner_immutable'); END;
CREATE TRIGGER trg_legal_entity_delete_prd102
BEFORE DELETE ON legal_entity_profiles
BEGIN SELECT RAISE(ABORT,'legal_entity_immutable'); END;

CREATE TABLE business_verification_intents(
  business_verification_intent_id TEXT PRIMARY KEY CHECK(
    length(business_verification_intent_id)=36
    AND substr(business_verification_intent_id,1,4)='bvi_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  legal_entity_profile_id TEXT NOT NULL,
  profile_revision INTEGER NOT NULL CHECK(profile_revision>=1),
  profile_sha256 TEXT NOT NULL CHECK(length(profile_sha256)=64),
  evidence_manifest_sha256 TEXT NOT NULL CHECK(length(evidence_manifest_sha256)=64),
  country_code TEXT NOT NULL CHECK(length(country_code)=2),
  entity_type TEXT NOT NULL CHECK(entity_type IN('company','nonprofit','government')),
  status TEXT NOT NULL CHECK(status IN(
    'submitted','in_review','approved','rejected','expired')),
  reason_code TEXT CHECK(reason_code IS NULL OR reason_code IN(
    'verified','document_invalid','identity_mismatch','scan_failed','profile_changed','expired')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  revision INTEGER NOT NULL CHECK(revision>=1),
  submitted_at TEXT NOT NULL CHECK(unixepoch(submitted_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  reviewed_at TEXT CHECK(reviewed_at IS NULL OR unixepoch(reviewed_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  UNIQUE(workspace_id,idempotency_sha256),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(legal_entity_profile_id) REFERENCES legal_entity_profiles(legal_entity_profile_id),
  CHECK((status IN('submitted','in_review') AND reason_code IS NULL AND reviewed_at IS NULL)
    OR(status IN('approved','rejected','expired') AND reason_code IS NOT NULL))
);
CREATE INDEX idx_business_verification_queue_prd102
  ON business_verification_intents(status,submitted_at,business_verification_intent_id);
CREATE TRIGGER trg_business_verification_intent_update_prd102
BEFORE UPDATE ON business_verification_intents FOR EACH ROW
WHEN NEW.business_verification_intent_id!=OLD.business_verification_intent_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.legal_entity_profile_id!=OLD.legal_entity_profile_id
  OR NEW.profile_revision!=OLD.profile_revision OR NEW.profile_sha256!=OLD.profile_sha256
  OR NEW.evidence_manifest_sha256!=OLD.evidence_manifest_sha256
  OR NEW.country_code!=OLD.country_code OR NEW.entity_type!=OLD.entity_type
  OR NEW.idempotency_sha256!=OLD.idempotency_sha256
  OR NEW.request_sha256!=OLD.request_sha256 OR NEW.submitted_at!=OLD.submitted_at
  OR NEW.expires_at!=OLD.expires_at OR NEW.revision!=OLD.revision+1
  OR NOT((OLD.status='submitted' AND NEW.status IN('in_review','approved','rejected','expired'))
    OR(OLD.status='in_review' AND NEW.status IN('approved','rejected','expired')))
BEGIN SELECT RAISE(ABORT,'business_verification_intent_immutable'); END;
CREATE TRIGGER trg_business_verification_intent_delete_prd102
BEFORE DELETE ON business_verification_intents
BEGIN SELECT RAISE(ABORT,'business_verification_intent_immutable'); END;

CREATE TABLE verification_evidence(
  verification_evidence_id TEXT PRIMARY KEY CHECK(length(verification_evidence_id)=36
    AND substr(verification_evidence_id,1,4)='bve_'),
  business_verification_intent_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  evidence_type TEXT NOT NULL CHECK(evidence_type IN(
    'business_license','registration_certificate','authorization_letter')),
  object_reference_ciphertext TEXT NOT NULL CHECK(
    length(object_reference_ciphertext) BETWEEN 64 AND 8192),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  mime_type TEXT NOT NULL CHECK(mime_type IN('application/pdf','image/png','image/jpeg')),
  size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND 10485760),
  kms_key_version INTEGER NOT NULL CHECK(kms_key_version>=1),
  encryption_key_version INTEGER NOT NULL CHECK(encryption_key_version>=1),
  malware_status TEXT NOT NULL CHECK(malware_status IN('passed','blocked','pending','failed')),
  dlp_status TEXT NOT NULL CHECK(dlp_status IN('passed','blocked','pending','failed')),
  status TEXT NOT NULL CHECK(status IN('active','tombstoned')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  tombstoned_at TEXT CHECK(tombstoned_at IS NULL OR unixepoch(tombstoned_at) IS NOT NULL),
  FOREIGN KEY(business_verification_intent_id)
    REFERENCES business_verification_intents(business_verification_intent_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  CHECK((status='active' AND tombstoned_at IS NULL)
    OR(status='tombstoned' AND tombstoned_at IS NOT NULL))
);
CREATE INDEX idx_verification_evidence_intent_prd102
  ON verification_evidence(business_verification_intent_id,status,verification_evidence_id);
CREATE TRIGGER trg_verification_evidence_update_prd102
BEFORE UPDATE ON verification_evidence FOR EACH ROW
WHEN NEW.verification_evidence_id!=OLD.verification_evidence_id
  OR NEW.business_verification_intent_id!=OLD.business_verification_intent_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.evidence_type!=OLD.evidence_type
  OR NEW.object_reference_ciphertext!=OLD.object_reference_ciphertext
  OR NEW.content_sha256!=OLD.content_sha256 OR NEW.mime_type!=OLD.mime_type
  OR NEW.size_bytes!=OLD.size_bytes OR NEW.kms_key_version!=OLD.kms_key_version
  OR NEW.encryption_key_version!=OLD.encryption_key_version
  OR NEW.malware_status!=OLD.malware_status OR NEW.dlp_status!=OLD.dlp_status
  OR NEW.created_at!=OLD.created_at
  OR NOT(OLD.status='active' AND NEW.status='tombstoned')
BEGIN SELECT RAISE(ABORT,'verification_evidence_immutable'); END;
CREATE TRIGGER trg_verification_evidence_delete_prd102
BEFORE DELETE ON verification_evidence
BEGIN SELECT RAISE(ABORT,'verification_evidence_immutable'); END;

CREATE TABLE contract_profiles(
  contract_profile_id TEXT PRIMARY KEY CHECK(length(contract_profile_id)=36
    AND substr(contract_profile_id,1,4)='ctp_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  business_verification_intent_id TEXT NOT NULL UNIQUE,
  legal_entity_profile_id TEXT NOT NULL,
  profile_revision INTEGER NOT NULL CHECK(profile_revision>=1),
  profile_sha256 TEXT NOT NULL CHECK(length(profile_sha256)=64),
  signing_status TEXT NOT NULL CHECK(signing_status IN('verified','signed','expired')),
  immutable_version_reference TEXT NOT NULL UNIQUE CHECK(
    length(immutable_version_reference) BETWEEN 16 AND 128),
  effective_at TEXT NOT NULL CHECK(unixepoch(effective_at) IS NOT NULL),
  expires_at TEXT CHECK(expires_at IS NULL OR unixepoch(expires_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(business_verification_intent_id)
    REFERENCES business_verification_intents(business_verification_intent_id),
  FOREIGN KEY(legal_entity_profile_id) REFERENCES legal_entity_profiles(legal_entity_profile_id)
);
CREATE INDEX idx_contract_profiles_workspace_prd102
  ON contract_profiles(workspace_id,created_at,contract_profile_id);
CREATE TRIGGER trg_contract_profiles_update_prd102
BEFORE UPDATE ON contract_profiles
BEGIN SELECT RAISE(ABORT,'contract_profile_immutable'); END;
CREATE TRIGGER trg_contract_profiles_delete_prd102
BEFORE DELETE ON contract_profiles
BEGIN SELECT RAISE(ABORT,'contract_profile_immutable'); END;

CREATE TABLE workspace_billing_profiles(
  workspace_billing_profile_id TEXT PRIMARY KEY CHECK(
    length(workspace_billing_profile_id)=36
    AND substr(workspace_billing_profile_id,1,4)='wbp_'),
  workspace_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL UNIQUE,
  legal_entity_profile_id TEXT,
  invoice_title_ciphertext TEXT NOT NULL CHECK(
    length(invoice_title_ciphertext) BETWEEN 64 AND 8192),
  tax_number_ciphertext TEXT NOT NULL CHECK(length(tax_number_ciphertext) BETWEEN 64 AND 8192),
  tax_number_blind_index TEXT NOT NULL CHECK(
    length(tax_number_blind_index)=76 AND substr(tax_number_blind_index,1,12)='hmac-sha256:'),
  address_ciphertext TEXT,
  phone_ciphertext TEXT,
  bank_name_ciphertext TEXT,
  bank_account_ciphertext TEXT,
  key_version INTEGER NOT NULL CHECK(key_version>=1),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  revision INTEGER NOT NULL CHECK(revision>=1),
  verification_status TEXT NOT NULL CHECK(verification_status IN(
    'unverified','verified','stale')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(legal_entity_profile_id) REFERENCES legal_entity_profiles(legal_entity_profile_id)
);
CREATE INDEX idx_workspace_billing_tax_prd102
  ON workspace_billing_profiles(tax_number_blind_index,verification_status);
CREATE UNIQUE INDEX idx_workspace_billing_idempotency_prd102
  ON workspace_billing_profiles(workspace_id,idempotency_sha256);
CREATE TRIGGER trg_workspace_billing_profile_update_prd102
BEFORE UPDATE ON workspace_billing_profiles FOR EACH ROW
WHEN NEW.workspace_billing_profile_id!=OLD.workspace_billing_profile_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
BEGIN SELECT RAISE(ABORT,'workspace_billing_profile_owner_immutable'); END;
CREATE TRIGGER trg_workspace_billing_profile_delete_prd102
BEFORE DELETE ON workspace_billing_profiles
BEGIN SELECT RAISE(ABORT,'workspace_billing_profile_immutable'); END;

CREATE TABLE verification_reviews(
  verification_review_id TEXT PRIMARY KEY CHECK(length(verification_review_id)=36
    AND substr(verification_review_id,1,4)='bvr_'),
  business_verification_intent_id TEXT NOT NULL UNIQUE,
  reviewer_operator_id TEXT NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN('approve','reject')),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'verified','document_invalid','identity_mismatch','scan_failed','profile_changed')),
  evidence_manifest_sha256 TEXT NOT NULL CHECK(length(evidence_manifest_sha256)=64),
  profile_sha256 TEXT NOT NULL CHECK(length(profile_sha256)=64),
  idempotency_sha256 TEXT NOT NULL UNIQUE CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(business_verification_intent_id)
    REFERENCES business_verification_intents(business_verification_intent_id),
  FOREIGN KEY(reviewer_operator_id) REFERENCES platform_operators(operator_id)
);
CREATE TRIGGER trg_verification_reviews_update_prd102
BEFORE UPDATE ON verification_reviews
BEGIN SELECT RAISE(ABORT,'verification_review_immutable'); END;
CREATE TRIGGER trg_verification_reviews_delete_prd102
BEFORE DELETE ON verification_reviews
BEGIN SELECT RAISE(ABORT,'verification_review_immutable'); END;

CREATE TABLE verification_evidence_download_tokens(
  verification_evidence_download_token_id TEXT PRIMARY KEY CHECK(
    length(verification_evidence_download_token_id)=36
    AND substr(verification_evidence_download_token_id,1,4)='vdt_'),
  business_verification_intent_id TEXT NOT NULL,
  verification_evidence_id TEXT NOT NULL,
  platform_session_id TEXT NOT NULL,
  token_hmac TEXT NOT NULL UNIQUE CHECK(length(token_hmac)=64),
  status TEXT NOT NULL CHECK(status IN('active','used','expired')),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  used_at TEXT CHECK(used_at IS NULL OR unixepoch(used_at) IS NOT NULL),
  FOREIGN KEY(business_verification_intent_id)
    REFERENCES business_verification_intents(business_verification_intent_id),
  FOREIGN KEY(verification_evidence_id)
    REFERENCES verification_evidence(verification_evidence_id),
  FOREIGN KEY(platform_session_id) REFERENCES platform_sessions(platform_session_id),
  CHECK((status='active' AND used_at IS NULL) OR(status IN('used','expired')))
);
CREATE INDEX idx_verification_download_expiry_prd102
  ON verification_evidence_download_tokens(status,expires_at);
CREATE TRIGGER trg_verification_download_owner_update_prd102
BEFORE UPDATE ON verification_evidence_download_tokens FOR EACH ROW
WHEN NEW.verification_evidence_download_token_id!=OLD.verification_evidence_download_token_id
  OR NEW.business_verification_intent_id!=OLD.business_verification_intent_id
  OR NEW.verification_evidence_id!=OLD.verification_evidence_id
  OR NEW.platform_session_id!=OLD.platform_session_id OR NEW.token_hmac!=OLD.token_hmac
  OR NEW.expires_at!=OLD.expires_at OR NEW.created_at!=OLD.created_at
  OR NOT(OLD.status='active' AND NEW.status IN('used','expired'))
BEGIN SELECT RAISE(ABORT,'verification_download_owner_immutable'); END;

CREATE TEMP TABLE prd102_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
-- 最终守卫确认权限种子、外键与数据库完整性，异常时回滚整个 0094 迁移。
INSERT INTO prd102_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM platform_verification_permission_extensions)=1
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_verification_role_permissions)=2
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd102_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
