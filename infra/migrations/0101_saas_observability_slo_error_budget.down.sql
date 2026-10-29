-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd109_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd109_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0101' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM observability_event_outbox)
  + (SELECT COUNT(*) FROM observability_commands)
  + (SELECT COUNT(*) FROM telemetry_policies)
  + (SELECT COUNT(*) FROM on_call_routes)
  + (SELECT COUNT(*) FROM alert_acknowledgements)
  + (SELECT COUNT(*) FROM platform_alert_transitions)
  + (SELECT COUNT(*) FROM platform_alerts)
  + (SELECT COUNT(*) FROM error_budgets)
  + (SELECT COUNT(*) FROM sli_time_buckets)
  + (SELECT COUNT(*) FROM service_level_objective_heads)
  + (SELECT COUNT(*) FROM service_level_objectives)
  + CASE WHEN (SELECT source_watermark||':'||dropped_fact_count||':'||revision
      FROM telemetry_ingest_head)='0:0:1' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM service_catalog_entries WHERE revision=1)=2
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_observability_permission_extensions)=4
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_observability_role_permissions)=7
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT status||':'||revision FROM platform_job_types
      WHERE type_code='observability.aggregate')='active:2' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd109_down_guard;

DROP TRIGGER trg_observability_event_delete_prd109;
DROP TRIGGER trg_observability_event_update_prd109;
DROP TABLE observability_event_outbox;
DROP TRIGGER trg_observability_commands_delete_prd109;
DROP TRIGGER trg_observability_commands_update_prd109;
DROP TABLE observability_commands;
DROP TRIGGER trg_telemetry_policies_delete_prd109;
DROP TRIGGER trg_telemetry_policies_update_prd109;
DROP TABLE telemetry_policies;
DROP TRIGGER trg_on_call_routes_delete_prd109;
DROP TRIGGER trg_on_call_routes_owner_update_prd109;
DROP TABLE on_call_routes;
DROP TRIGGER trg_alert_ack_delete_prd109;
DROP TRIGGER trg_alert_ack_update_prd109;
DROP TABLE alert_acknowledgements;
DROP TRIGGER trg_platform_alert_transitions_delete_prd109;
DROP TRIGGER trg_platform_alert_transitions_update_prd109;
DROP TABLE platform_alert_transitions;
DROP TRIGGER trg_platform_alerts_delete_prd109;
DROP TRIGGER trg_platform_alerts_update_prd109;
DROP TABLE platform_alerts;
DROP TRIGGER trg_error_budgets_delete_prd109;
DROP TRIGGER trg_error_budgets_update_prd109;
DROP TABLE error_budgets;
DROP TRIGGER trg_sli_buckets_delete_prd109;
DROP TRIGGER trg_sli_buckets_update_prd109;
DROP TABLE sli_time_buckets;
DROP TRIGGER trg_telemetry_ingest_head_delete_prd109;
DROP TRIGGER trg_telemetry_ingest_head_update_prd109;
DROP TABLE telemetry_ingest_head;
DROP TRIGGER trg_slo_head_delete_prd109;
DROP TRIGGER trg_slo_head_update_prd109;
DROP TABLE service_level_objective_heads;
DROP TRIGGER trg_slo_delete_prd109;
DROP TRIGGER trg_slo_update_prd109;
DROP TRIGGER trg_slo_insert_version_prd109;
DROP TABLE service_level_objectives;
DROP TRIGGER trg_service_catalog_delete_prd109;
DROP TRIGGER trg_service_catalog_update_prd109;
DROP TABLE service_catalog_entries;
DROP TRIGGER trg_platform_observability_roles_delete_prd109;
DROP TRIGGER trg_platform_observability_roles_update_prd109;
DROP TRIGGER trg_platform_observability_roles_insert_prd109;
DROP TRIGGER trg_platform_observability_permissions_delete_prd109;
DROP TRIGGER trg_platform_observability_permissions_update_prd109;
DROP TRIGGER trg_platform_observability_permissions_insert_prd109;
DROP TABLE platform_observability_role_permissions;
DROP TABLE platform_observability_permission_extensions;

DROP TRIGGER trg_platform_job_types_update_prd106;
UPDATE platform_job_types
SET status='disabled',revision=1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE type_code='observability.aggregate' AND status='active' AND revision=2;
CREATE TRIGGER trg_platform_job_types_update_prd106
BEFORE UPDATE ON platform_job_types FOR EACH ROW
WHEN NEW.type_code!=OLD.type_code OR NEW.contract_version!=OLD.contract_version
  OR NEW.artifact_sha256!=OLD.artifact_sha256 OR NEW.queue_code!=OLD.queue_code
  OR NEW.resource_class!=OLD.resource_class OR NEW.max_attempts!=OLD.max_attempts
  OR NEW.timeout_seconds!=OLD.timeout_seconds OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'platform_job_type_immutable'); END;

CREATE TEMP TABLE prd109_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd109_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT status||':'||revision FROM platform_job_types
      WHERE type_code='observability.aggregate')='disabled:1' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd109_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
