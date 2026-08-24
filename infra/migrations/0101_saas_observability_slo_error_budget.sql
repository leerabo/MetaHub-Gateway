-- Durable low-cardinality telemetry, versioned SLOs, budgets, and alert evidence.
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd109_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd109_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0100' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'platform_roles','platform_operators','platform_job_types','platform_jobs',
      'workspace_tenant_bindings','rollout_health_summaries'))=6 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd109')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd109_up_guard;

CREATE TABLE platform_observability_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.observability.read','platform.observability.manage',
    'platform.observability.security_read','platform.observability.alert_ack')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-109'),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=11)
);
CREATE TABLE platform_observability_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code) REFERENCES platform_observability_permission_extensions(permission_code)
);
INSERT INTO platform_observability_permission_extensions VALUES
  ('platform.observability.read','PRD-109',11),
  ('platform.observability.manage','PRD-109',11),
  ('platform.observability.security_read','PRD-109',11),
  ('platform.observability.alert_ack','PRD-109',11);
INSERT INTO platform_observability_role_permissions VALUES
  ('platform_owner','platform.observability.read'),
  ('platform_owner','platform.observability.manage'),
  ('platform_owner','platform.observability.alert_ack'),
  ('platform_ops','platform.observability.read'),
  ('platform_ops','platform.observability.manage'),
  ('platform_ops','platform.observability.alert_ack'),
  ('platform_security','platform.observability.security_read');
CREATE TRIGGER trg_platform_observability_permissions_insert_prd109
BEFORE INSERT ON platform_observability_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_observability_permission_immutable'); END;
CREATE TRIGGER trg_platform_observability_permissions_update_prd109
BEFORE UPDATE ON platform_observability_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_observability_permission_immutable'); END;
CREATE TRIGGER trg_platform_observability_permissions_delete_prd109
BEFORE DELETE ON platform_observability_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_observability_permission_immutable'); END;
CREATE TRIGGER trg_platform_observability_roles_insert_prd109
BEFORE INSERT ON platform_observability_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_observability_permission_immutable'); END;
CREATE TRIGGER trg_platform_observability_roles_update_prd109
BEFORE UPDATE ON platform_observability_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_observability_permission_immutable'); END;
CREATE TRIGGER trg_platform_observability_roles_delete_prd109
BEFORE DELETE ON platform_observability_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_observability_permission_immutable'); END;

CREATE TABLE service_catalog_entries(
  service_catalog_entry_id TEXT PRIMARY KEY CHECK(length(service_catalog_entry_id)=36
    AND substr(service_catalog_entry_id,1,4)='sce_'
    AND substr(service_catalog_entry_id,5) NOT GLOB '*[^0-9a-f]*'),
  service_code TEXT NOT NULL UNIQUE CHECK(length(service_code) BETWEEN 3 AND 40
    AND service_code=lower(service_code) AND service_code NOT GLOB '*[^a-z0-9_.-]*'),
  display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 80),
  owner_team_code TEXT NOT NULL CHECK(length(owner_team_code) BETWEEN 3 AND 40
    AND owner_team_code=lower(owner_team_code)
    AND owner_team_code NOT GLOB '*[^a-z0-9_.-]*'),
  service_tier INTEGER NOT NULL CHECK(service_tier BETWEEN 1 AND 3),
  golden_signals_json TEXT NOT NULL CHECK(json_valid(golden_signals_json)
    AND json_type(golden_signals_json)='array'
    AND length(CAST(golden_signals_json AS BLOB)) BETWEEN 2 AND 512),
  security_visible INTEGER NOT NULL CHECK(security_visible IN(0,1)),
  status TEXT NOT NULL CHECK(status IN('active','disabled')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);
INSERT INTO service_catalog_entries VALUES
  ('sce_00000000000000000000000000000001','gateway','Gateway Data Plane','platform_sre',1,
   '["availability","errors","latency","traffic"]',1,'active',1,datetime('now'),datetime('now')),
  ('sce_00000000000000000000000000000002','control','Control Plane API','platform_sre',1,
   '["availability","errors","latency","traffic"]',1,'active',1,datetime('now'),datetime('now'));
CREATE TRIGGER trg_service_catalog_update_prd109
BEFORE UPDATE ON service_catalog_entries FOR EACH ROW
WHEN NEW.service_catalog_entry_id!=OLD.service_catalog_entry_id
  OR NEW.service_code!=OLD.service_code OR NEW.display_name!=OLD.display_name
  OR NEW.owner_team_code!=OLD.owner_team_code OR NEW.service_tier!=OLD.service_tier
  OR NEW.golden_signals_json!=OLD.golden_signals_json
  OR NEW.security_visible!=OLD.security_visible OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'service_catalog_owner_immutable'); END;
CREATE TRIGGER trg_service_catalog_delete_prd109 BEFORE DELETE ON service_catalog_entries
BEGIN SELECT RAISE(ABORT,'service_catalog_immutable'); END;

CREATE TABLE service_level_objectives(
  service_level_objective_id TEXT PRIMARY KEY CHECK(length(service_level_objective_id)=36
    AND substr(service_level_objective_id,1,4)='slo_'
    AND substr(service_level_objective_id,5) NOT GLOB '*[^0-9a-f]*'),
  slo_code TEXT NOT NULL CHECK(length(slo_code) BETWEEN 3 AND 64
    AND slo_code=lower(slo_code) AND slo_code NOT GLOB '*[^a-z0-9_.-]*'),
  version_number INTEGER NOT NULL CHECK(version_number>=1),
  service_catalog_entry_id TEXT NOT NULL,
  display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 120),
  signal_class TEXT NOT NULL CHECK(signal_class IN('reliability','security')),
  sli_kind TEXT NOT NULL CHECK(sli_kind IN('availability','latency')),
  operation_code TEXT NOT NULL CHECK(length(operation_code) BETWEEN 2 AND 64
    AND operation_code=lower(operation_code)
    AND operation_code NOT GLOB '*[^a-z0-9_.-]*'),
  region_code TEXT NOT NULL CHECK(length(region_code) BETWEEN 2 AND 40
    AND region_code=lower(region_code) AND region_code NOT GLOB '*[^a-z0-9_.-]*'),
  window_minutes INTEGER NOT NULL CHECK(window_minutes BETWEEN 5 AND 43200),
  target_bps INTEGER NOT NULL CHECK(target_bps BETWEEN 1 AND 10000),
  latency_threshold_ms INTEGER CHECK(latency_threshold_ms IS NULL
    OR latency_threshold_ms IN(10,50,100,250,500,1000,2500,5000)),
  burn_warning_bps INTEGER NOT NULL CHECK(burn_warning_bps BETWEEN 10000 AND 100000000),
  burn_critical_bps INTEGER NOT NULL CHECK(burn_critical_bps>burn_warning_bps
    AND burn_critical_bps<=100000000),
  definition_sha256 TEXT NOT NULL CHECK(length(definition_sha256)=64
    AND definition_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_by_operator_id TEXT NOT NULL,
  reason_code TEXT NOT NULL CHECK(reason_code IN('slo_create','slo_revision')),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(service_catalog_entry_id) REFERENCES service_catalog_entries(service_catalog_entry_id),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(slo_code,version_number),
  UNIQUE(service_level_objective_id,version_number),
  CHECK((sli_kind='latency' AND latency_threshold_ms IS NOT NULL)
    OR(sli_kind='availability' AND latency_threshold_ms IS NULL))
);
CREATE TRIGGER trg_slo_insert_version_prd109
BEFORE INSERT ON service_level_objectives FOR EACH ROW
WHEN NEW.version_number != coalesce((SELECT max(version_number)+1
  FROM service_level_objectives WHERE slo_code=NEW.slo_code),1)
BEGIN SELECT RAISE(ABORT,'slo_version_not_monotonic'); END;
CREATE TRIGGER trg_slo_update_prd109 BEFORE UPDATE ON service_level_objectives
BEGIN SELECT RAISE(ABORT,'slo_definition_immutable'); END;
CREATE TRIGGER trg_slo_delete_prd109 BEFORE DELETE ON service_level_objectives
BEGIN SELECT RAISE(ABORT,'slo_definition_immutable'); END;

CREATE TABLE service_level_objective_heads(
  service_level_objective_head_id TEXT PRIMARY KEY CHECK(length(service_level_objective_head_id)=36
    AND substr(service_level_objective_head_id,1,4)='slh_'),
  slo_code TEXT NOT NULL UNIQUE,
  current_slo_id TEXT NOT NULL UNIQUE,
  current_version INTEGER NOT NULL CHECK(current_version>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(slo_code,current_version)
    REFERENCES service_level_objectives(slo_code,version_number),
  FOREIGN KEY(current_slo_id,current_version)
    REFERENCES service_level_objectives(service_level_objective_id,version_number)
);
CREATE TRIGGER trg_slo_head_update_prd109
BEFORE UPDATE ON service_level_objective_heads FOR EACH ROW
WHEN NEW.service_level_objective_head_id!=OLD.service_level_objective_head_id
  OR NEW.slo_code!=OLD.slo_code OR NEW.current_slo_id=OLD.current_slo_id
  OR NEW.current_version!=OLD.current_version+1 OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'slo_head_not_monotonic'); END;
CREATE TRIGGER trg_slo_head_delete_prd109 BEFORE DELETE ON service_level_objective_heads
BEGIN SELECT RAISE(ABORT,'slo_head_immutable'); END;

CREATE TABLE telemetry_ingest_head(
  telemetry_ingest_head_id TEXT PRIMARY KEY CHECK(telemetry_ingest_head_id=
    'tih_00000000000000000000000000000001'),
  source_watermark INTEGER NOT NULL CHECK(source_watermark>=0),
  dropped_fact_count INTEGER NOT NULL CHECK(dropped_fact_count>=0),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);
INSERT INTO telemetry_ingest_head VALUES
  ('tih_00000000000000000000000000000001',0,0,1,datetime('now'));
CREATE TRIGGER trg_telemetry_ingest_head_update_prd109
BEFORE UPDATE ON telemetry_ingest_head FOR EACH ROW
WHEN NEW.telemetry_ingest_head_id!=OLD.telemetry_ingest_head_id
  OR NEW.source_watermark NOT IN(OLD.source_watermark,OLD.source_watermark+1)
  OR NEW.dropped_fact_count<OLD.dropped_fact_count
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'telemetry_watermark_not_monotonic'); END;
CREATE TRIGGER trg_telemetry_ingest_head_delete_prd109 BEFORE DELETE ON telemetry_ingest_head
BEGIN SELECT RAISE(ABORT,'telemetry_watermark_immutable'); END;

CREATE TABLE sli_time_buckets(
  sli_time_bucket_id TEXT PRIMARY KEY CHECK(length(sli_time_bucket_id)=36
    AND substr(sli_time_bucket_id,1,4)='slb_'),
  source_watermark INTEGER NOT NULL CHECK(source_watermark>=1),
  bucket_started_at TEXT NOT NULL CHECK(unixepoch(bucket_started_at) IS NOT NULL),
  service_code TEXT NOT NULL,
  operation_code TEXT NOT NULL CHECK(length(operation_code) BETWEEN 2 AND 64),
  region_code TEXT NOT NULL CHECK(length(region_code) BETWEEN 2 AND 40),
  status_class TEXT NOT NULL CHECK(status_class IN('2xx','3xx','4xx','5xx','other')),
  duration_bucket TEXT NOT NULL CHECK(duration_bucket IN(
    'le_10ms','le_50ms','le_100ms','le_250ms','le_500ms','le_1000ms',
    'le_2500ms','le_5000ms','gt_5000ms')),
  tenant_pseudonym TEXT NOT NULL CHECK(tenant_pseudonym='none'
    OR(length(tenant_pseudonym)=64 AND tenant_pseudonym NOT GLOB '*[^0-9a-f]*')),
  good_count INTEGER NOT NULL CHECK(good_count>=0),
  total_count INTEGER NOT NULL CHECK(total_count>=1 AND good_count<=total_count),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64
    AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(service_code) REFERENCES service_catalog_entries(service_code),
  UNIQUE(source_watermark,bucket_started_at,service_code,operation_code,region_code,
    status_class,duration_bucket,tenant_pseudonym)
);
CREATE INDEX idx_sli_buckets_window_prd109
  ON sli_time_buckets(service_code,region_code,bucket_started_at,source_watermark);
CREATE TRIGGER trg_sli_buckets_update_prd109 BEFORE UPDATE ON sli_time_buckets
BEGIN SELECT RAISE(ABORT,'sli_bucket_immutable'); END;
CREATE TRIGGER trg_sli_buckets_delete_prd109 BEFORE DELETE ON sli_time_buckets
BEGIN SELECT RAISE(ABORT,'sli_bucket_immutable'); END;

CREATE TABLE error_budgets(
  error_budget_id TEXT PRIMARY KEY CHECK(length(error_budget_id)=36
    AND substr(error_budget_id,1,4)='ebg_'),
  service_level_objective_id TEXT NOT NULL,
  slo_version INTEGER NOT NULL CHECK(slo_version>=1),
  window_started_at TEXT NOT NULL CHECK(unixepoch(window_started_at) IS NOT NULL),
  window_ended_at TEXT NOT NULL CHECK(unixepoch(window_ended_at) IS NOT NULL),
  total_count INTEGER NOT NULL CHECK(total_count>=0),
  good_count INTEGER NOT NULL CHECK(good_count>=0 AND good_count<=total_count),
  allowed_bad_count INTEGER NOT NULL CHECK(allowed_bad_count>=0),
  consumed_bad_count INTEGER NOT NULL CHECK(consumed_bad_count>=0),
  remaining_bad_count INTEGER NOT NULL CHECK(remaining_bad_count>=0),
  burn_rate_bps INTEGER NOT NULL CHECK(burn_rate_bps>=0),
  health_status TEXT NOT NULL CHECK(health_status IN(
    'healthy','warning','critical','exhausted','unavailable')),
  source_watermark INTEGER NOT NULL CHECK(source_watermark>=0),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64
    AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
  computed_at TEXT NOT NULL CHECK(unixepoch(computed_at) IS NOT NULL),
  FOREIGN KEY(service_level_objective_id,slo_version)
    REFERENCES service_level_objectives(service_level_objective_id,version_number),
  UNIQUE(service_level_objective_id,slo_version,window_ended_at,source_watermark),
  CHECK(unixepoch(window_ended_at)>unixepoch(window_started_at)),
  CHECK(remaining_bad_count=max(allowed_bad_count-consumed_bad_count,0))
);
CREATE INDEX idx_error_budgets_latest_prd109
  ON error_budgets(service_level_objective_id,computed_at DESC,error_budget_id DESC);
CREATE TRIGGER trg_error_budgets_update_prd109 BEFORE UPDATE ON error_budgets
BEGIN SELECT RAISE(ABORT,'error_budget_immutable'); END;
CREATE TRIGGER trg_error_budgets_delete_prd109 BEFORE DELETE ON error_budgets
BEGIN SELECT RAISE(ABORT,'error_budget_immutable'); END;

CREATE TABLE platform_alerts(
  platform_alert_id TEXT PRIMARY KEY CHECK(length(platform_alert_id)=36
    AND substr(platform_alert_id,1,4)='alt_'),
  dedupe_sha256 TEXT NOT NULL UNIQUE CHECK(length(dedupe_sha256)=64),
  service_level_objective_id TEXT NOT NULL,
  slo_version INTEGER NOT NULL CHECK(slo_version>=1),
  service_code TEXT NOT NULL,
  region_code TEXT NOT NULL,
  severity TEXT NOT NULL CHECK(severity IN('warning','critical')),
  state TEXT NOT NULL CHECK(state IN('firing','suppressed','resolved')),
  safe_reason_code TEXT NOT NULL CHECK(safe_reason_code IN(
    'slo_breached','error_budget_exhausted')),
  source_watermark INTEGER NOT NULL CHECK(source_watermark>=0),
  first_seen_at TEXT NOT NULL CHECK(unixepoch(first_seen_at) IS NOT NULL),
  last_seen_at TEXT NOT NULL CHECK(unixepoch(last_seen_at) IS NOT NULL),
  suppressed_until TEXT CHECK(suppressed_until IS NULL OR unixepoch(suppressed_until) IS NOT NULL),
  resolved_at TEXT CHECK(resolved_at IS NULL OR unixepoch(resolved_at) IS NOT NULL),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(service_level_objective_id,slo_version)
    REFERENCES service_level_objectives(service_level_objective_id,version_number),
  FOREIGN KEY(service_code) REFERENCES service_catalog_entries(service_code),
  CHECK((state='resolved' AND resolved_at IS NOT NULL)
    OR(state!='resolved' AND resolved_at IS NULL))
);
CREATE INDEX idx_platform_alerts_state_prd109
  ON platform_alerts(state,severity,last_seen_at DESC,platform_alert_id);
CREATE TRIGGER trg_platform_alerts_update_prd109
BEFORE UPDATE ON platform_alerts FOR EACH ROW
WHEN NEW.platform_alert_id!=OLD.platform_alert_id OR NEW.dedupe_sha256!=OLD.dedupe_sha256
  OR NEW.service_level_objective_id!=OLD.service_level_objective_id
  OR NEW.slo_version!=OLD.slo_version OR NEW.service_code!=OLD.service_code
  OR NEW.region_code!=OLD.region_code OR NEW.safe_reason_code!=OLD.safe_reason_code
  OR NEW.source_watermark<OLD.source_watermark OR NEW.first_seen_at!=OLD.first_seen_at
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at
  OR NOT((OLD.state='firing' AND NEW.state IN('firing','suppressed','resolved'))
    OR(OLD.state='suppressed' AND NEW.state IN('firing','suppressed','resolved'))
    OR(OLD.state='resolved' AND NEW.state IN('resolved','firing')))
BEGIN SELECT RAISE(ABORT,'platform_alert_transition_invalid'); END;
CREATE TRIGGER trg_platform_alerts_delete_prd109 BEFORE DELETE ON platform_alerts
BEGIN SELECT RAISE(ABORT,'platform_alert_immutable'); END;

CREATE TABLE platform_alert_transitions(
  platform_alert_transition_id TEXT PRIMARY KEY CHECK(length(platform_alert_transition_id)=36
    AND substr(platform_alert_transition_id,1,4)='atr_'),
  platform_alert_id TEXT NOT NULL,
  from_state TEXT CHECK(from_state IS NULL OR from_state IN('firing','suppressed','resolved')),
  to_state TEXT NOT NULL CHECK(to_state IN('firing','suppressed','resolved')),
  alert_revision INTEGER NOT NULL CHECK(alert_revision>=1),
  safe_reason_code TEXT NOT NULL CHECK(safe_reason_code IN(
    'slo_breached','error_budget_exhausted','budget_recovered',
    'notification_suppressed','notification_resumed')),
  source_watermark INTEGER NOT NULL CHECK(source_watermark>=0),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(platform_alert_id) REFERENCES platform_alerts(platform_alert_id),
  UNIQUE(platform_alert_id,alert_revision)
);
CREATE TRIGGER trg_platform_alert_transitions_update_prd109
BEFORE UPDATE ON platform_alert_transitions
BEGIN SELECT RAISE(ABORT,'platform_alert_transition_immutable'); END;
CREATE TRIGGER trg_platform_alert_transitions_delete_prd109
BEFORE DELETE ON platform_alert_transitions
BEGIN SELECT RAISE(ABORT,'platform_alert_transition_immutable'); END;

CREATE TABLE alert_acknowledgements(
  alert_acknowledgement_id TEXT PRIMARY KEY CHECK(length(alert_acknowledgement_id)=36
    AND substr(alert_acknowledgement_id,1,4)='aak_'),
  platform_alert_id TEXT NOT NULL,
  operator_id TEXT NOT NULL,
  alert_revision INTEGER NOT NULL CHECK(alert_revision>=1),
  reason_code TEXT NOT NULL CHECK(reason_code IN('investigating','known_issue','mitigating')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  acknowledged_at TEXT NOT NULL CHECK(unixepoch(acknowledged_at) IS NOT NULL),
  FOREIGN KEY(platform_alert_id) REFERENCES platform_alerts(platform_alert_id),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(operator_id,idempotency_sha256)
);
CREATE TRIGGER trg_alert_ack_update_prd109 BEFORE UPDATE ON alert_acknowledgements
BEGIN SELECT RAISE(ABORT,'alert_acknowledgement_immutable'); END;
CREATE TRIGGER trg_alert_ack_delete_prd109 BEFORE DELETE ON alert_acknowledgements
BEGIN SELECT RAISE(ABORT,'alert_acknowledgement_immutable'); END;

CREATE TABLE on_call_routes(
  on_call_route_id TEXT PRIMARY KEY CHECK(length(on_call_route_id)=36
    AND substr(on_call_route_id,1,4)='ocr_'),
  service_code TEXT NOT NULL,
  severity TEXT NOT NULL CHECK(severity IN('warning','critical')),
  destination_ref_sha256 TEXT NOT NULL CHECK(length(destination_ref_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('active','disabled')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(service_code) REFERENCES service_catalog_entries(service_code),
  UNIQUE(service_code,severity)
);
CREATE TRIGGER trg_on_call_routes_owner_update_prd109
BEFORE UPDATE ON on_call_routes FOR EACH ROW
WHEN NEW.on_call_route_id!=OLD.on_call_route_id OR NEW.service_code!=OLD.service_code
  OR NEW.severity!=OLD.severity OR NEW.destination_ref_sha256!=OLD.destination_ref_sha256
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'on_call_route_owner_immutable'); END;
CREATE TRIGGER trg_on_call_routes_delete_prd109 BEFORE DELETE ON on_call_routes
BEGIN SELECT RAISE(ABORT,'on_call_route_immutable'); END;

CREATE TABLE telemetry_policies(
  telemetry_policy_id TEXT PRIMARY KEY CHECK(length(telemetry_policy_id)=36
    AND substr(telemetry_policy_id,1,4)='tpy_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  policy_code TEXT NOT NULL CHECK(length(policy_code) BETWEEN 3 AND 64),
  version_number INTEGER NOT NULL CHECK(version_number>=1),
  allowed_labels_json TEXT NOT NULL CHECK(json_valid(allowed_labels_json)
    AND json_type(allowed_labels_json)='array'
    AND length(CAST(allowed_labels_json AS BLOB)) BETWEEN 2 AND 512),
  allowed_labels_sha256 TEXT NOT NULL CHECK(length(allowed_labels_sha256)=64),
  sampling_bps INTEGER NOT NULL CHECK(sampling_bps BETWEEN 0 AND 10000),
  cardinality_limit INTEGER NOT NULL CHECK(cardinality_limit BETWEEN 1 AND 10000),
  drop_unknown INTEGER NOT NULL CHECK(drop_unknown=1),
  status TEXT NOT NULL CHECK(status IN('active','disabled')),
  created_by_operator_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(workspace_id,policy_code,version_number)
);
CREATE TRIGGER trg_telemetry_policies_update_prd109 BEFORE UPDATE ON telemetry_policies
BEGIN SELECT RAISE(ABORT,'telemetry_policy_immutable'); END;
CREATE TRIGGER trg_telemetry_policies_delete_prd109 BEFORE DELETE ON telemetry_policies
BEGIN SELECT RAISE(ABORT,'telemetry_policy_immutable'); END;

CREATE TABLE observability_commands(
  observability_command_id TEXT PRIMARY KEY CHECK(length(observability_command_id)=36
    AND substr(observability_command_id,1,4)='ocm_'),
  operator_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(action_code='slo.version'),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_slo_id TEXT NOT NULL,
  result_version INTEGER NOT NULL CHECK(result_version>=1),
  aggregation_job_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(result_slo_id,result_version)
    REFERENCES service_level_objectives(service_level_objective_id,version_number),
  FOREIGN KEY(aggregation_job_id) REFERENCES platform_jobs(platform_job_id),
  UNIQUE(operator_id,idempotency_sha256)
);
CREATE TRIGGER trg_observability_commands_update_prd109 BEFORE UPDATE ON observability_commands
BEGIN SELECT RAISE(ABORT,'observability_command_immutable'); END;
CREATE TRIGGER trg_observability_commands_delete_prd109 BEFORE DELETE ON observability_commands
BEGIN SELECT RAISE(ABORT,'observability_command_immutable'); END;

CREATE TABLE observability_event_outbox(
  observability_event_outbox_id TEXT PRIMARY KEY CHECK(length(observability_event_outbox_id)=36
    AND substr(observability_event_outbox_id,1,4)='oeo_'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'slo.breached','error_budget.exhausted','alert.triggered','alert.resolved')),
  service_code TEXT NOT NULL,
  service_level_objective_id TEXT NOT NULL,
  slo_version INTEGER NOT NULL CHECK(slo_version>=1),
  platform_alert_id TEXT,
  severity TEXT NOT NULL CHECK(severity IN('info','warning','critical')),
  safe_reason_code TEXT NOT NULL CHECK(length(safe_reason_code) BETWEEN 3 AND 64),
  source_watermark INTEGER NOT NULL CHECK(source_watermark>=0),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(service_code) REFERENCES service_catalog_entries(service_code),
  FOREIGN KEY(service_level_objective_id,slo_version)
    REFERENCES service_level_objectives(service_level_objective_id,version_number),
  FOREIGN KEY(platform_alert_id) REFERENCES platform_alerts(platform_alert_id)
);
CREATE TRIGGER trg_observability_event_update_prd109 BEFORE UPDATE ON observability_event_outbox
BEGIN SELECT RAISE(ABORT,'observability_event_immutable'); END;
CREATE TRIGGER trg_observability_event_delete_prd109 BEFORE DELETE ON observability_event_outbox
BEGIN SELECT RAISE(ABORT,'observability_event_immutable'); END;

UPDATE platform_job_types
SET status='active',revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE type_code='observability.aggregate' AND status='disabled' AND revision=1;

CREATE TEMP TABLE prd109_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd109_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM platform_observability_permission_extensions)=4
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_observability_role_permissions)=7
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM service_catalog_entries)=2 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT source_watermark||':'||dropped_fact_count||':'||revision
      FROM telemetry_ingest_head)='0:0:1' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT status||':'||revision FROM platform_job_types
      WHERE type_code='observability.aggregate')='active:2' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'service_catalog_entries','service_level_objectives','service_level_objective_heads',
      'telemetry_ingest_head','sli_time_buckets','error_budgets','platform_alerts',
      'platform_alert_transitions','alert_acknowledgements','on_call_routes',
      'telemetry_policies','observability_commands','observability_event_outbox'))=13
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd109_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
