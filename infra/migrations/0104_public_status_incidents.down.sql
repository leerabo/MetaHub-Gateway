-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd112_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd112_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0104' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM status_publication_bundles)
  + (SELECT COUNT(*) FROM incident_updates)
  + (SELECT COUNT(*) FROM incidents)
  + (SELECT COUNT(*) FROM maintenance_windows)
  + (SELECT COUNT(*) FROM status_subscriptions)
  + (SELECT COUNT(*) FROM postmortems)
  + (SELECT COUNT(*) FROM status_commands)
  + (SELECT COUNT(*) FROM status_event_outbox)
  + CASE WHEN (SELECT COUNT(*) FROM status_components WHERE revision=1
      AND current_state='operational')=5 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_status_permission_extensions)=3 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_status_role_permissions)=9 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd112_down_guard;

DROP TRIGGER trg_status_events_delete_prd112;
DROP TRIGGER trg_status_events_update_prd112;
DROP TABLE status_event_outbox;
DROP TRIGGER trg_status_commands_delete_prd112;
DROP TRIGGER trg_status_commands_update_prd112;
DROP TABLE status_commands;
DROP TRIGGER trg_status_bundles_delete_prd112;
DROP TRIGGER trg_status_bundles_update_prd112;
DROP INDEX uq_status_bundle_head_prd112;
DROP TABLE status_publication_bundles;
DROP TRIGGER trg_postmortems_delete_prd112;
DROP TRIGGER trg_postmortems_update_prd112;
DROP TABLE postmortems;
DROP TRIGGER trg_status_subscriptions_delete_prd112;
DROP TRIGGER trg_status_subscriptions_update_prd112;
DROP INDEX idx_status_subscriptions_owner_prd112;
DROP TABLE status_subscriptions;
DROP TRIGGER trg_maintenance_delete_prd112;
DROP TRIGGER trg_maintenance_update_prd112;
DROP INDEX idx_maintenance_owner_prd112;
DROP TABLE maintenance_windows;
DROP TRIGGER trg_incident_updates_delete_prd112;
DROP TRIGGER trg_incident_updates_update_prd112;
DROP INDEX idx_incident_updates_timeline_prd112;
DROP TABLE incident_updates;
DROP TRIGGER trg_incident_scopes_delete_prd112;
DROP TRIGGER trg_incident_scopes_update_prd112;
DROP INDEX idx_incident_workspace_owner_prd112;
DROP TABLE incident_workspace_scopes;
DROP TRIGGER trg_incidents_delete_prd112;
DROP TRIGGER trg_incidents_update_prd112;
DROP INDEX idx_incidents_recent_prd112;
DROP TABLE incidents;
DROP TRIGGER trg_status_components_delete_prd112;
DROP TRIGGER trg_status_components_update_prd112;
DROP TABLE status_components;
DROP TRIGGER trg_platform_status_roles_delete_prd112;
DROP TRIGGER trg_platform_status_roles_update_prd112;
DROP TRIGGER trg_platform_status_roles_insert_prd112;
DROP TRIGGER trg_platform_status_permissions_delete_prd112;
DROP TRIGGER trg_platform_status_permissions_update_prd112;
DROP TRIGGER trg_platform_status_permissions_insert_prd112;
DROP TABLE platform_status_role_permissions;
DROP TABLE platform_status_permission_extensions;

CREATE TEMP TABLE prd112_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd112_down_final_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd112_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
