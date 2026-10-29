CREATE TABLE dr_run_recovery_bindings(
  disaster_recovery_run_id TEXT PRIMARY KEY REFERENCES disaster_recovery_runs(disaster_recovery_run_id),
  recovery_point_id TEXT NOT NULL REFERENCES recovery_points(recovery_point_id),
  platform_job_id TEXT NOT NULL UNIQUE REFERENCES platform_jobs(platform_job_id),
  plan_manifest_sha256 TEXT NOT NULL CHECK(plan_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  source_region_id TEXT NOT NULL REFERENCES platform_regions(region_id),
  target_region_id TEXT NOT NULL REFERENCES platform_regions(region_id),
  expected_epoch BIGINT NOT NULL CHECK(expected_epoch>=0),
  fencing_epoch BIGINT NOT NULL CHECK(fencing_epoch=expected_epoch+1),
  manifest_sha256 TEXT NOT NULL CHECK(manifest_sha256 ~ '^[0-9a-f]{64}$'),
  content_sha256 TEXT NOT NULL CHECK(content_sha256 ~ '^[0-9a-f]{64}$'),
  object_key_sha256 TEXT NOT NULL CHECK(object_key_sha256 ~ '^[0-9a-f]{64}$'),
  schema_head TEXT NOT NULL CHECK(schema_head ~ '^[0-9]{4}$'),
  bound_at TEXT NOT NULL CHECK(unixepoch(bound_at) IS NOT NULL),
  CHECK(source_region_id!=target_region_id)
);
CREATE FUNCTION dr_recovery_binding_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path TO pg_catalog, public, pg_temp AS $$
BEGIN
  IF TG_OP!='INSERT' THEN RAISE EXCEPTION 'dr_recovery_binding_immutable'; END IF;
  IF NOT EXISTS(
    SELECT 1 FROM disaster_recovery_runs run
    JOIN recovery_points point ON point.recovery_point_id=NEW.recovery_point_id
    JOIN backup_sets backup ON backup.backup_id=point.backup_id
    JOIN backup_restore_verifications verification ON verification.backup_id=backup.backup_id
    JOIN platform_jobs job ON job.platform_job_id=NEW.platform_job_id
    WHERE run.disaster_recovery_run_id=NEW.disaster_recovery_run_id AND run.status='planned'
      AND run.platform_job_id IS NULL AND job.type_code='dr.run' AND job.status='queued'
      AND run.plan_manifest_sha256=NEW.plan_manifest_sha256
      AND run.source_region_id=NEW.source_region_id AND run.target_region_id=NEW.target_region_id
      AND run.expected_epoch=NEW.expected_epoch AND run.fencing_epoch=NEW.fencing_epoch
      AND point.scope_kind='platform' AND point.workspace_id IS NULL AND point.tenant_id IS NULL
      AND backup.scope_kind='platform' AND backup.workspace_id IS NULL AND backup.tenant_id IS NULL
      AND backup.backup_type='full' AND backup.source_backend='postgresql' AND backup.status='ready'
      AND point.manifest_sha256=NEW.manifest_sha256 AND backup.manifest_sha256=NEW.manifest_sha256
      AND backup.content_sha256=NEW.content_sha256
      AND point.schema_head=NEW.schema_head AND backup.schema_head=NEW.schema_head
      AND verification.schema_head=NEW.schema_head AND verification.manifest_sha256=NEW.manifest_sha256
      AND verification.status='passed' AND verification.integrity_status='ok'
      AND julianday(point.source_created_at)=julianday(backup.created_at)
      AND NEW.schema_head=(SELECT version FROM schema_migrations WHERE status='applied'
        ORDER BY version DESC LIMIT 1)
  ) OR EXISTS(SELECT 1 FROM schema_migrations WHERE status='failed') THEN
    RAISE EXCEPTION 'dr_recovery_binding_invalid';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_dr_recovery_binding_guard BEFORE INSERT OR UPDATE OR DELETE ON dr_run_recovery_bindings
FOR EACH ROW EXECUTE FUNCTION dr_recovery_binding_guard();

CREATE FUNCTION dr_run_binding_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path TO pg_catalog, public, pg_temp AS $$
BEGIN
  IF TG_OP='INSERT' THEN
    IF NEW.status NOT IN('planned','failed','aborted','rollback_required') THEN
      RAISE EXCEPTION 'dr_recovery_binding_required';
    END IF;
  ELSE
    IF (NEW.status IN('approved','prechecking','running','verifying','succeeded') AND NOT EXISTS(
      SELECT 1 FROM dr_run_recovery_bindings binding
      WHERE binding.disaster_recovery_run_id=NEW.disaster_recovery_run_id
        AND binding.platform_job_id=NEW.platform_job_id
        AND binding.plan_manifest_sha256=NEW.plan_manifest_sha256
        AND binding.source_region_id=NEW.source_region_id AND binding.target_region_id=NEW.target_region_id
        AND binding.expected_epoch=NEW.expected_epoch AND binding.fencing_epoch=NEW.fencing_epoch
    )) OR (EXISTS(SELECT 1 FROM dr_run_recovery_bindings binding
      WHERE binding.disaster_recovery_run_id=OLD.disaster_recovery_run_id)
      AND OLD.platform_job_id IS NOT NULL AND NEW.platform_job_id IS DISTINCT FROM OLD.platform_job_id)
      OR (OLD.started_at IS NOT NULL AND NEW.started_at IS DISTINCT FROM OLD.started_at) THEN
      RAISE EXCEPTION 'dr_recovery_binding_required';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_dr_run_binding_guard BEFORE INSERT OR UPDATE ON disaster_recovery_runs
FOR EACH ROW EXECUTE FUNCTION dr_run_binding_guard();
