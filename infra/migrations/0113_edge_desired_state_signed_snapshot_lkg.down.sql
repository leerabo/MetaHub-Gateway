-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd121_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd121_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0113' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM edge_snapshot_signing_roots)
  + (SELECT COUNT(*) FROM edge_trust_manifests)
  + (SELECT COUNT(*) FROM edge_trust_manifest_heads)
  + (SELECT COUNT(*) FROM edge_desired_states)
  + (SELECT COUNT(*) FROM edge_desired_state_approvals)
  + (SELECT COUNT(*) FROM edge_snapshot_artifacts)
  + (SELECT COUNT(*) FROM edge_signed_snapshots)
  + (SELECT COUNT(*) FROM edge_desired_state_heads)
  + (SELECT COUNT(*) FROM edge_apply_attempts)
  + (SELECT COUNT(*) FROM edge_acks)
  + (SELECT COUNT(*) FROM edge_lkg_observations)
  + (SELECT COUNT(*) FROM workspace_edge_state_commands)
  + (SELECT COUNT(*) FROM edge_desired_state_events)
  + (SELECT COUNT(*) FROM edge_desired_state_event_outbox)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd121_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd121_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd121_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd121_delete;
CREATE TEMP TABLE prd121_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd121;
DROP TABLE tenant_deletion_manifest_extensions_prd121;
DELETE FROM tenant_deletion_trigger_manifest
WHERE source_table IN(SELECT source_table FROM prd121_manifest_sources);
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd121_manifest_sources);
DROP TABLE prd121_manifest_sources;

DROP INDEX idx_edge_desired_outbox_delivery_prd121;
DROP TABLE edge_desired_state_event_outbox;
DROP TRIGGER trg_edge_desired_events_delete_prd121;
DROP TRIGGER trg_edge_desired_events_update_prd121;
DROP INDEX idx_edge_desired_events_owner_prd121;
DROP TABLE edge_desired_state_events;
DROP TRIGGER trg_workspace_edge_state_commands_delete_prd121;
DROP TRIGGER trg_workspace_edge_state_commands_update_prd121;
DROP TABLE workspace_edge_state_commands;
DROP TRIGGER trg_edge_lkg_delete_prd121;
DROP TRIGGER trg_edge_lkg_update_prd121;
DROP INDEX idx_edge_lkg_owner_prd121;
DROP TABLE edge_lkg_observations;
DROP TRIGGER trg_edge_acks_delete_prd121;
DROP TRIGGER trg_edge_acks_update_prd121;
DROP TABLE edge_acks;
DROP TRIGGER trg_edge_apply_attempts_delete_prd121;
DROP TRIGGER trg_edge_apply_attempts_update_prd121;
DROP INDEX idx_edge_apply_attempts_owner_prd121;
DROP TABLE edge_apply_attempts;
DROP TRIGGER trg_edge_desired_heads_delete_prd121;
DROP TRIGGER trg_edge_desired_heads_update_prd121;
DROP INDEX idx_edge_desired_heads_lag_prd121;
DROP TABLE edge_desired_state_heads;
DROP TRIGGER trg_edge_signed_snapshots_delete_prd121;
DROP TRIGGER trg_edge_signed_snapshots_update_prd121;
DROP INDEX idx_edge_signed_snapshots_pull_prd121;
DROP TABLE edge_signed_snapshots;
DROP TRIGGER trg_edge_snapshot_artifacts_delete_prd121;
DROP TRIGGER trg_edge_snapshot_artifacts_update_prd121;
DROP TABLE edge_snapshot_artifacts;
DROP TRIGGER trg_edge_desired_approvals_delete_prd121;
DROP TRIGGER trg_edge_desired_approvals_update_prd121;
DROP TABLE edge_desired_state_approvals;
DROP TRIGGER trg_edge_desired_states_delete_prd121;
DROP TRIGGER trg_edge_desired_states_transition_prd121;
DROP INDEX idx_edge_desired_states_owner_prd121;
DROP TABLE edge_desired_states;
DROP TRIGGER trg_edge_trust_heads_delete_prd121;
DROP TRIGGER trg_edge_trust_heads_update_prd121;
DROP TABLE edge_trust_manifest_heads;
DROP TRIGGER trg_edge_trust_manifests_delete_prd121;
DROP TRIGGER trg_edge_trust_manifests_update_prd121;
DROP TABLE edge_trust_manifests;
DROP TRIGGER trg_edge_signing_roots_delete_prd121;
DROP TRIGGER trg_edge_signing_roots_update_prd121;
DROP TABLE edge_snapshot_signing_roots;

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

CREATE TEMP TABLE prd121_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd121_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'edge_snapshot_signing_roots','edge_trust_manifests','edge_trust_manifest_heads',
      'edge_desired_states','edge_desired_state_approvals','edge_snapshot_artifacts',
      'edge_signed_snapshots','edge_desired_state_heads','edge_apply_attempts','edge_acks',
      'edge_lkg_observations','workspace_edge_state_commands','edge_desired_state_events',
      'edge_desired_state_event_outbox','tenant_deletion_manifest_extensions_prd121'))=0
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd121_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
