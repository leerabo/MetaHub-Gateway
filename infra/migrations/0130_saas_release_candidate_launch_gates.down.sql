-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd138_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd138_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0130' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM launch_candidates)
  + (SELECT COUNT(*) FROM launch_candidate_artifacts)
  + (SELECT COUNT(*) FROM launch_evidence)
  + (SELECT COUNT(*) FROM launch_go_no_go_decisions)
  + (SELECT COUNT(*) FROM launch_waves)
  + (SELECT COUNT(*) FROM launch_rollbacks)
  + (SELECT COUNT(*) FROM post_launch_reviews)
  + (SELECT COUNT(*) FROM launch_events)
  + (SELECT COUNT(*) FROM launch_event_outbox)
  + (SELECT COUNT(*) FROM launch_action_commands)
  + CASE WHEN (SELECT COUNT(*) FROM launch_gate_policies)=20 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd138_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd138_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd138_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd138_delete;
CREATE TEMP TABLE prd138_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd138;
DROP TABLE tenant_deletion_manifest_extensions_prd138;
DELETE FROM tenant_deletion_trigger_manifest;
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd138_manifest_sources);
DROP TABLE prd138_manifest_sources;

DROP TRIGGER trg_launch_scheduler_delete_prd138;
DROP TRIGGER trg_launch_scheduler_update_prd138;
DROP TABLE launch_scheduler_state;
DROP TRIGGER trg_launch_action_delete_prd138;
DROP TRIGGER trg_launch_action_update_prd138;
DROP TABLE launch_action_commands;
DROP TRIGGER trg_launch_outbox_delete_prd138;
DROP TRIGGER trg_launch_outbox_update_prd138;
DROP TRIGGER trg_launch_outbox_owner_insert_prd138;
DROP INDEX idx_launch_outbox_due_prd138;
DROP TABLE launch_event_outbox;
DROP TRIGGER trg_launch_event_delete_prd138;
DROP TRIGGER trg_launch_event_update_prd138;
DROP TRIGGER trg_launch_event_owner_insert_prd138;
DROP TABLE launch_events;
DROP TRIGGER trg_launch_review_delete_prd138;
DROP TRIGGER trg_launch_review_update_prd138;
DROP TABLE post_launch_reviews;
DROP TRIGGER trg_launch_rollback_delete_prd138;
DROP TRIGGER trg_launch_rollback_update_prd138;
DROP TABLE launch_rollbacks;
DROP TRIGGER trg_launch_wave_delete_prd138;
DROP TRIGGER trg_launch_wave_update_prd138;
DROP TABLE launch_waves;
DROP TRIGGER trg_launch_decision_delete_prd138;
DROP TRIGGER trg_launch_decision_update_prd138;
DROP TRIGGER trg_launch_decision_insert_prd138;
DROP TABLE launch_go_no_go_decisions;
DROP TRIGGER trg_launch_evidence_delete_prd138;
DROP TRIGGER trg_launch_evidence_update_prd138;
DROP INDEX idx_launch_evidence_expiry_prd138;
DROP TABLE launch_evidence;
DROP TRIGGER trg_launch_artifact_delete_prd138;
DROP TRIGGER trg_launch_artifact_update_prd138;
DROP TABLE launch_candidate_artifacts;
DROP TRIGGER trg_launch_candidate_delete_prd138;
DROP TRIGGER trg_launch_candidate_update_prd138;
DROP TABLE launch_candidates;
DROP TRIGGER trg_launch_gate_delete_prd138;
DROP TRIGGER trg_launch_gate_update_prd138;
DROP TRIGGER trg_launch_gate_insert_prd138;
DROP TABLE launch_gate_policies;
DROP TRIGGER trg_platform_launch_roles_delete_prd138;
DROP TRIGGER trg_platform_launch_roles_update_prd138;
DROP TRIGGER trg_platform_launch_roles_insert_prd138;
DROP TRIGGER trg_platform_launch_permissions_delete_prd138;
DROP TRIGGER trg_platform_launch_permissions_update_prd138;
DROP TRIGGER trg_platform_launch_permissions_insert_prd138;
DROP TABLE platform_launch_role_permissions;
DROP TABLE platform_launch_permission_extensions;

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

CREATE TEMP TABLE prd138_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd138_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'platform_launch_permission_extensions','platform_launch_role_permissions',
      'launch_gate_policies','launch_candidates','launch_candidate_artifacts',
      'launch_evidence','launch_go_no_go_decisions','launch_waves','launch_rollbacks',
      'post_launch_reviews','launch_events','launch_event_outbox',
      'launch_action_commands','launch_scheduler_state',
      'tenant_deletion_manifest_extensions_prd138'))=0 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd138_down_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
