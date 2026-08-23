-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd108_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd108_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0100' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM config_event_outbox)
  + (SELECT COUNT(*) FROM config_rebuild_jobs)
  + (SELECT COUNT(*) FROM cache_watermarks)
  + (SELECT COUNT(*) FROM config_acks)
  + (SELECT COUNT(*) FROM config_deltas)
  + (SELECT COUNT(*) FROM config_snapshot_heads)
  + (SELECT COUNT(*) FROM config_snapshots)
  + CASE WHEN (SELECT COUNT(*) FROM platform_config_permission_extensions)=2 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_config_role_permissions)=5 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT status||':'||revision FROM platform_job_types
      WHERE type_code='config.snapshot.rebuild')='active:2' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd108_down_guard;

DROP TRIGGER trg_config_event_outbox_delete_prd108;
DROP TRIGGER trg_config_event_outbox_update_prd108;
DROP TABLE config_event_outbox;
DROP TRIGGER trg_config_rebuild_jobs_delete_prd108;
DROP TRIGGER trg_config_rebuild_jobs_owner_update_prd108;
DROP TABLE config_rebuild_jobs;
DROP TRIGGER trg_cache_watermarks_delete_prd108;
DROP TRIGGER trg_cache_watermarks_owner_update_prd108;
DROP TABLE cache_watermarks;
DROP TRIGGER trg_config_acks_delete_prd108;
DROP TRIGGER trg_config_acks_update_prd108;
DROP TABLE config_acks;
DROP TRIGGER trg_config_deltas_delete_prd108;
DROP TRIGGER trg_config_deltas_update_prd108;
DROP TABLE config_deltas;
DROP TRIGGER trg_config_snapshot_heads_delete_prd108;
DROP TRIGGER trg_config_snapshot_heads_owner_update_prd108;
DROP TABLE config_snapshot_heads;
DROP TRIGGER trg_config_snapshots_delete_prd108;
DROP TRIGGER trg_config_snapshots_update_prd108;
DROP TABLE config_snapshots;
DROP TRIGGER trg_platform_config_role_permissions_delete_prd108;
DROP TRIGGER trg_platform_config_role_permissions_update_prd108;
DROP TRIGGER trg_platform_config_role_permissions_insert_prd108;
DROP TRIGGER trg_platform_config_permissions_delete_prd108;
DROP TRIGGER trg_platform_config_permissions_update_prd108;
DROP TRIGGER trg_platform_config_permissions_insert_prd108;
DROP TABLE platform_config_role_permissions;
DROP TABLE platform_config_permission_extensions;

DROP TRIGGER trg_platform_job_types_update_prd106;
UPDATE platform_job_types
SET status='disabled',revision=1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE type_code='config.snapshot.rebuild' AND status='active' AND revision=2;
CREATE TRIGGER trg_platform_job_types_update_prd106
BEFORE UPDATE ON platform_job_types FOR EACH ROW
WHEN NEW.type_code!=OLD.type_code OR NEW.contract_version!=OLD.contract_version
  OR NEW.artifact_sha256!=OLD.artifact_sha256 OR NEW.queue_code!=OLD.queue_code
  OR NEW.resource_class!=OLD.resource_class OR NEW.max_attempts!=OLD.max_attempts
  OR NEW.timeout_seconds!=OLD.timeout_seconds OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'platform_job_type_immutable'); END;

CREATE TEMP TABLE prd108_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd108_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT status||':'||revision FROM platform_job_types
      WHERE type_code='config.snapshot.rebuild')='disabled:1' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd108_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
