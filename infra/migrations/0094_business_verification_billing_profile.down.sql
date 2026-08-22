-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;
-- 仅在无企业认证、证据、合同或开票业务事实时回滚，防止静默丢失客户资料。

CREATE TEMP TABLE prd102_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
-- 回滚前验证版本、业务表为空、权限种子未漂移且数据库仍完整。
INSERT INTO prd102_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0094' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM legal_entity_profiles)
  + (SELECT COUNT(*) FROM business_verification_intents)
  + (SELECT COUNT(*) FROM verification_evidence)
  + (SELECT COUNT(*) FROM contract_profiles)
  + (SELECT COUNT(*) FROM workspace_billing_profiles)
  + (SELECT COUNT(*) FROM verification_reviews)
  + (SELECT COUNT(*) FROM verification_evidence_download_tokens)
  + CASE WHEN (SELECT COUNT(*) FROM platform_verification_permission_extensions)=1
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_verification_role_permissions)=2
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd102_down_guard;

DROP TRIGGER trg_verification_download_owner_update_prd102;
DROP TABLE verification_evidence_download_tokens;
DROP TRIGGER trg_verification_reviews_delete_prd102;
DROP TRIGGER trg_verification_reviews_update_prd102;
DROP TABLE verification_reviews;
DROP TRIGGER trg_workspace_billing_profile_delete_prd102;
DROP TRIGGER trg_workspace_billing_profile_update_prd102;
DROP INDEX idx_workspace_billing_idempotency_prd102;
DROP INDEX idx_workspace_billing_tax_prd102;
DROP TABLE workspace_billing_profiles;
DROP TRIGGER trg_contract_profiles_delete_prd102;
DROP TRIGGER trg_contract_profiles_update_prd102;
DROP INDEX idx_contract_profiles_workspace_prd102;
DROP TABLE contract_profiles;
DROP TRIGGER trg_verification_evidence_delete_prd102;
DROP TRIGGER trg_verification_evidence_update_prd102;
DROP INDEX idx_verification_evidence_intent_prd102;
DROP TABLE verification_evidence;
DROP TRIGGER trg_business_verification_intent_delete_prd102;
DROP TRIGGER trg_business_verification_intent_update_prd102;
DROP INDEX idx_business_verification_queue_prd102;
DROP TABLE business_verification_intents;
DROP TRIGGER trg_legal_entity_delete_prd102;
DROP TRIGGER trg_legal_entity_owner_update_prd102;
DROP INDEX idx_legal_entity_registration_prd102;
DROP TABLE legal_entity_profiles;
DROP TRIGGER trg_platform_verification_role_permissions_delete_prd102;
DROP TRIGGER trg_platform_verification_role_permissions_update_prd102;
DROP TRIGGER trg_platform_verification_role_permissions_insert_prd102;
DROP TRIGGER trg_platform_verification_permissions_delete_prd102;
DROP TRIGGER trg_platform_verification_permissions_update_prd102;
DROP TRIGGER trg_platform_verification_permissions_insert_prd102;
DROP TABLE platform_verification_role_permissions;
DROP TABLE platform_verification_permission_extensions;

CREATE TEMP TABLE prd102_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
-- 删除完成后再次验证外键与数据库完整性，避免提交部分回滚结果。
INSERT INTO prd102_down_final_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd102_down_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
