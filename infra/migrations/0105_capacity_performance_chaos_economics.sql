-- metahub:expected-version-v1
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd113_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd113_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0104' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'platform_roles','platform_operators','platform_job_types','platform_jobs',
      'gateway_cells','platform_regions','feature_rollouts','usage_ledger'))=8 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd113_up_guard;

CREATE TABLE platform_capacity_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.capacity.read','platform.capacity.test','platform.capacity.chaos',
    'platform.unit_economics.read')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-113'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=15)
);
CREATE TABLE platform_capacity_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code) REFERENCES platform_capacity_permission_extensions(permission_code)
);
INSERT INTO platform_capacity_permission_extensions VALUES
  ('platform.capacity.read','PRD-113',15),
  ('platform.capacity.test','PRD-113',15),
  ('platform.capacity.chaos','PRD-113',15),
  ('platform.unit_economics.read','PRD-113',15);
INSERT INTO platform_capacity_role_permissions VALUES
  ('platform_owner','platform.capacity.read'),
  ('platform_owner','platform.capacity.test'),
  ('platform_owner','platform.capacity.chaos'),
  ('platform_owner','platform.unit_economics.read'),
  ('platform_ops','platform.capacity.read'),
  ('platform_ops','platform.capacity.test'),
  ('platform_ops','platform.capacity.chaos'),
  ('platform_security','platform.capacity.read'),
  ('platform_security','platform.capacity.chaos'),
  ('platform_billing','platform.unit_economics.read');
CREATE TRIGGER trg_platform_capacity_permissions_insert_prd113
BEFORE INSERT ON platform_capacity_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_capacity_permission_immutable'); END;
CREATE TRIGGER trg_platform_capacity_permissions_update_prd113
BEFORE UPDATE ON platform_capacity_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_capacity_permission_immutable'); END;
CREATE TRIGGER trg_platform_capacity_permissions_delete_prd113
BEFORE DELETE ON platform_capacity_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_capacity_permission_immutable'); END;
CREATE TRIGGER trg_platform_capacity_roles_insert_prd113
BEFORE INSERT ON platform_capacity_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_capacity_permission_immutable'); END;
CREATE TRIGGER trg_platform_capacity_roles_update_prd113
BEFORE UPDATE ON platform_capacity_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_capacity_permission_immutable'); END;
CREATE TRIGGER trg_platform_capacity_roles_delete_prd113
BEFORE DELETE ON platform_capacity_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_capacity_permission_immutable'); END;

CREATE TABLE capacity_service_states(
  capacity_service_state_id TEXT PRIMARY KEY CHECK(length(capacity_service_state_id)=36
    AND substr(capacity_service_state_id,1,4)='css_'),
  service_code TEXT NOT NULL CHECK(service_code IN('gateway','control','status')),
  region_code TEXT NOT NULL CHECK(length(region_code) BETWEEN 3 AND 40
    AND region_code=lower(region_code) AND region_code NOT GLOB '*[^a-z0-9-]*'),
  demand_rps INTEGER NOT NULL CHECK(demand_rps BETWEEN 0 AND 10000000),
  capacity_rps INTEGER NOT NULL CHECK(capacity_rps BETWEEN 1 AND 10000000),
  threshold_bps INTEGER NOT NULL CHECK(threshold_bps BETWEEN 5000 AND 9500),
  slo_p95_ms INTEGER NOT NULL CHECK(slo_p95_ms BETWEEN 1 AND 60000),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(region_code) REFERENCES platform_regions(region_code),
  UNIQUE(service_code,region_code)
);
INSERT INTO capacity_service_states VALUES
  ('css_00000000000000000000000000000001','gateway','cn-north-1',800,2000,8000,2000,1,datetime('now'),datetime('now')),
  ('css_00000000000000000000000000000002','gateway','cn-east-1',400,1600,8000,2000,1,datetime('now'),datetime('now')),
  ('css_00000000000000000000000000000003','control','cn-north-1',120,500,7500,500,1,datetime('now'),datetime('now')),
  ('css_00000000000000000000000000000004','status','cn-north-1',10,2000,8000,500,1,datetime('now'),datetime('now'));
CREATE TRIGGER trg_capacity_service_states_update_prd113
BEFORE UPDATE ON capacity_service_states FOR EACH ROW
WHEN NEW.capacity_service_state_id!=OLD.capacity_service_state_id
  OR NEW.service_code!=OLD.service_code OR NEW.region_code!=OLD.region_code
  OR NEW.threshold_bps!=OLD.threshold_bps OR NEW.slo_p95_ms!=OLD.slo_p95_ms
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'capacity_state_transition_invalid'); END;
CREATE TRIGGER trg_capacity_service_states_delete_prd113
BEFORE DELETE ON capacity_service_states
BEGIN SELECT RAISE(ABORT,'capacity_state_immutable'); END;

CREATE TABLE capacity_scenarios(
  capacity_scenario_id TEXT PRIMARY KEY CHECK(length(capacity_scenario_id)=36
    AND substr(capacity_scenario_id,1,4)='csc_'),
  scenario_code TEXT NOT NULL UNIQUE CHECK(length(scenario_code) BETWEEN 3 AND 64
    AND scenario_code=lower(scenario_code)
    AND scenario_code NOT GLOB '*[^a-z0-9-]*'),
  service_code TEXT NOT NULL CHECK(service_code IN('gateway','control','status')),
  run_kind TEXT NOT NULL CHECK(run_kind IN('load','soak')),
  max_tenant_count INTEGER NOT NULL CHECK(max_tenant_count BETWEEN 1 AND 500),
  max_target_rps INTEGER NOT NULL CHECK(max_target_rps BETWEEN 1 AND 100000),
  max_duration_seconds INTEGER NOT NULL CHECK(max_duration_seconds BETWEEN 10 AND 259200),
  target_peak_multiplier_bps INTEGER NOT NULL CHECK(target_peak_multiplier_bps BETWEEN 10000 AND 30000),
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64
    AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('active','disabled')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(scenario_code,service_code),
  UNIQUE(scenario_code,service_code,run_kind)
);
INSERT INTO capacity_scenarios VALUES
  ('csc_00000000000000000000000000000001','gateway-release','gateway','load',200,10000,3600,20000,'5f24c3049b4da0105935b1b2d49f3e88de76d550f3d3dc3028efd71ac360b5e4','active',datetime('now')),
  ('csc_00000000000000000000000000000002','gateway-soak','gateway','soak',200,5000,259200,20000,'a85b5fa397f2d7701e35d4496bc836d1b47009565e96f3007314b941292055ce','active',datetime('now')),
  ('csc_00000000000000000000000000000003','control-read','control','load',100,2000,3600,20000,'a2fb6a16a547d693b4ad512b1f6005c5f57828a47ef4ab39aa3920e71ddf51e0','active',datetime('now')),
  ('csc_00000000000000000000000000000004','status-read','status','load',100,5000,3600,20000,'43db72b15e57cc55b3cdf930faedf8ed7f45c084e4dd88061d1e41a0da8ff111','active',datetime('now'));
CREATE TRIGGER trg_capacity_scenarios_update_prd113 BEFORE UPDATE ON capacity_scenarios
BEGIN SELECT RAISE(ABORT,'capacity_scenario_immutable'); END;
CREATE TRIGGER trg_capacity_scenarios_delete_prd113 BEFORE DELETE ON capacity_scenarios
BEGIN SELECT RAISE(ABORT,'capacity_scenario_immutable'); END;

CREATE TABLE performance_baselines(
  performance_baseline_id TEXT PRIMARY KEY CHECK(length(performance_baseline_id)=36
    AND substr(performance_baseline_id,1,4)='pbl_'),
  service_code TEXT NOT NULL CHECK(service_code IN('gateway','control','status')),
  scenario_code TEXT NOT NULL,
  environment_class TEXT NOT NULL CHECK(environment_class IN('dev','staging','production')),
  version INTEGER NOT NULL CHECK(version>=1),
  sample_count INTEGER NOT NULL CHECK(sample_count>=100),
  p50_ms INTEGER NOT NULL CHECK(p50_ms>=0),
  p95_ms INTEGER NOT NULL CHECK(p95_ms>=p50_ms),
  p99_ms INTEGER NOT NULL CHECK(p99_ms>=p95_ms),
  throughput_rps_milli INTEGER NOT NULL CHECK(throughput_rps_milli>=0),
  error_rate_bps INTEGER NOT NULL CHECK(error_rate_bps BETWEEN 0 AND 10000),
  cpu_peak_bps INTEGER NOT NULL CHECK(cpu_peak_bps BETWEEN 0 AND 10000),
  memory_peak_mib INTEGER NOT NULL CHECK(memory_peak_mib>=0),
  sample_window_seconds INTEGER NOT NULL CHECK(sample_window_seconds>=10),
  result_sha256 TEXT NOT NULL CHECK(length(result_sha256)=64
    AND result_sha256 NOT GLOB '*[^0-9a-f]*'),
  source_load_test_run_id TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(scenario_code,service_code)
    REFERENCES capacity_scenarios(scenario_code,service_code),
  FOREIGN KEY(source_load_test_run_id) REFERENCES load_test_runs(load_test_run_id),
  UNIQUE(service_code,scenario_code,environment_class,version)
);
CREATE TRIGGER trg_performance_baselines_update_prd113 BEFORE UPDATE ON performance_baselines
BEGIN SELECT RAISE(ABORT,'performance_baseline_immutable'); END;
CREATE TRIGGER trg_performance_baselines_delete_prd113 BEFORE DELETE ON performance_baselines
BEGIN SELECT RAISE(ABORT,'performance_baseline_immutable'); END;

CREATE TABLE capacity_forecasts(
  capacity_forecast_id TEXT PRIMARY KEY CHECK(length(capacity_forecast_id)=36
    AND substr(capacity_forecast_id,1,4)='cfc_'),
  service_code TEXT NOT NULL CHECK(service_code IN('gateway','control','status')),
  region_code TEXT NOT NULL,
  horizon_days INTEGER NOT NULL CHECK(horizon_days IN(30,90)),
  model_code TEXT NOT NULL CHECK(model_code='linear-v1'),
  input_demand_rps INTEGER NOT NULL CHECK(input_demand_rps>=0),
  input_capacity_rps INTEGER NOT NULL CHECK(input_capacity_rps>=1),
  input_aggregate_sha256 TEXT NOT NULL CHECK(length(input_aggregate_sha256)=64
    AND input_aggregate_sha256 NOT GLOB '*[^0-9a-f]*'),
  predicted_demand_rps INTEGER CHECK(predicted_demand_rps IS NULL OR predicted_demand_rps>=0),
  predicted_capacity_rps INTEGER CHECK(predicted_capacity_rps IS NULL OR predicted_capacity_rps>=1),
  confidence_bps INTEGER CHECK(confidence_bps IS NULL OR confidence_bps BETWEEN 0 AND 10000),
  error_outcome_bps INTEGER CHECK(error_outcome_bps IS NULL OR error_outcome_bps BETWEEN 0 AND 10000),
  recommendation_code TEXT CHECK(recommendation_code IS NULL OR recommendation_code IN(
    'hold','scale-within-30d','scale-within-90d')),
  status TEXT NOT NULL CHECK(status IN('queued','running','succeeded','failed')),
  platform_job_id TEXT NOT NULL UNIQUE,
  created_by_operator_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  FOREIGN KEY(region_code) REFERENCES platform_regions(region_code),
  FOREIGN KEY(platform_job_id) REFERENCES platform_jobs(platform_job_id),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(service_code,region_code,horizon_days,model_code,created_at),
  CHECK((status IN('queued','running') AND completed_at IS NULL)
    OR(status IN('succeeded','failed') AND completed_at IS NOT NULL)),
  CHECK((status='succeeded' AND predicted_demand_rps IS NOT NULL
      AND predicted_capacity_rps IS NOT NULL AND confidence_bps IS NOT NULL
      AND recommendation_code IS NOT NULL)
    OR(status!='succeeded'))
);
CREATE TRIGGER trg_capacity_forecasts_update_prd113
BEFORE UPDATE ON capacity_forecasts FOR EACH ROW
WHEN NEW.capacity_forecast_id!=OLD.capacity_forecast_id
  OR NEW.service_code!=OLD.service_code OR NEW.region_code!=OLD.region_code
  OR NEW.horizon_days!=OLD.horizon_days OR NEW.model_code!=OLD.model_code
  OR NEW.input_demand_rps!=OLD.input_demand_rps
  OR NEW.input_capacity_rps!=OLD.input_capacity_rps
  OR NEW.input_aggregate_sha256!=OLD.input_aggregate_sha256
  OR NEW.platform_job_id!=OLD.platform_job_id
  OR NEW.created_by_operator_id!=OLD.created_by_operator_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at OR OLD.status IN('succeeded','failed')
  OR NOT(NEW.status=OLD.status OR(OLD.status='queued' AND NEW.status IN('running','failed'))
    OR(OLD.status='running' AND NEW.status IN('succeeded','failed')))
BEGIN SELECT RAISE(ABORT,'capacity_forecast_transition_invalid'); END;
CREATE TRIGGER trg_capacity_forecasts_delete_prd113 BEFORE DELETE ON capacity_forecasts
BEGIN SELECT RAISE(ABORT,'capacity_forecast_immutable'); END;

CREATE TABLE load_test_runs(
  load_test_run_id TEXT PRIMARY KEY CHECK(length(load_test_run_id)=36
    AND substr(load_test_run_id,1,4)='ltr_'),
  scenario_code TEXT NOT NULL,
  service_code TEXT NOT NULL CHECK(service_code IN('gateway','control','status')),
  run_kind TEXT NOT NULL CHECK(run_kind IN('load','soak')),
  environment_class TEXT NOT NULL CHECK(environment_class IN('dev','staging','production')),
  region_code TEXT NOT NULL,
  synthetic_tenant_count INTEGER NOT NULL CHECK(synthetic_tenant_count BETWEEN 1 AND 500),
  target_rps INTEGER NOT NULL CHECK(target_rps BETWEEN 1 AND 100000),
  duration_seconds INTEGER NOT NULL CHECK(duration_seconds BETWEEN 10 AND 259200),
  scenario_manifest_sha256 TEXT NOT NULL CHECK(length(scenario_manifest_sha256)=64
    AND scenario_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('queued','running','succeeded','failed','stopped')),
  platform_job_id TEXT NOT NULL UNIQUE,
  created_by_operator_id TEXT NOT NULL,
  sample_count INTEGER CHECK(sample_count IS NULL OR sample_count>=0),
  p50_ms INTEGER CHECK(p50_ms IS NULL OR p50_ms>=0),
  p95_ms INTEGER CHECK(p95_ms IS NULL OR p95_ms>=0),
  p99_ms INTEGER CHECK(p99_ms IS NULL OR p99_ms>=0),
  throughput_rps_milli INTEGER CHECK(throughput_rps_milli IS NULL OR throughput_rps_milli>=0),
  error_rate_bps INTEGER CHECK(error_rate_bps IS NULL OR error_rate_bps BETWEEN 0 AND 10000),
  cpu_peak_bps INTEGER CHECK(cpu_peak_bps IS NULL OR cpu_peak_bps BETWEEN 0 AND 10000),
  memory_peak_mib INTEGER CHECK(memory_peak_mib IS NULL OR memory_peak_mib>=0),
  open_connection_delta INTEGER CHECK(open_connection_delta IS NULL OR open_connection_delta>=0),
  handle_delta INTEGER CHECK(handle_delta IS NULL OR handle_delta>=0),
  fairness_jain_millionths INTEGER CHECK(fairness_jain_millionths IS NULL
    OR fairness_jain_millionths BETWEEN 0 AND 1000000),
  controlled_degradation INTEGER CHECK(controlled_degradation IS NULL
    OR controlled_degradation IN(0,1)),
  regression_status TEXT CHECK(regression_status IS NULL OR regression_status IN(
    'no_baseline','insufficient_confidence','within_baseline','regressed')),
  result_sha256 TEXT CHECK(result_sha256 IS NULL OR length(result_sha256)=64),
  safe_outcome_code TEXT CHECK(safe_outcome_code IS NULL
    OR length(safe_outcome_code) BETWEEN 3 AND 96),
  revision INTEGER NOT NULL CHECK(revision>=1),
  started_at TEXT CHECK(started_at IS NULL OR unixepoch(started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(scenario_code,service_code,run_kind)
    REFERENCES capacity_scenarios(scenario_code,service_code,run_kind),
  FOREIGN KEY(region_code) REFERENCES platform_regions(region_code),
  FOREIGN KEY(platform_job_id) REFERENCES platform_jobs(platform_job_id),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK((status='queued' AND started_at IS NULL AND completed_at IS NULL)
    OR(status='running' AND started_at IS NOT NULL AND completed_at IS NULL)
    OR(status IN('succeeded','failed','stopped') AND completed_at IS NOT NULL)),
  CHECK((status='succeeded' AND sample_count IS NOT NULL AND p50_ms IS NOT NULL
      AND p95_ms IS NOT NULL AND p99_ms IS NOT NULL AND result_sha256 IS NOT NULL
      AND regression_status IS NOT NULL)
    OR status!='succeeded')
);
CREATE INDEX idx_load_test_runs_recent_prd113
  ON load_test_runs(created_at DESC,load_test_run_id DESC);
CREATE TRIGGER trg_load_test_runs_update_prd113
BEFORE UPDATE ON load_test_runs FOR EACH ROW
WHEN NEW.load_test_run_id!=OLD.load_test_run_id OR NEW.scenario_code!=OLD.scenario_code
  OR NEW.service_code!=OLD.service_code OR NEW.run_kind!=OLD.run_kind
  OR NEW.environment_class!=OLD.environment_class OR NEW.region_code!=OLD.region_code
  OR NEW.synthetic_tenant_count!=OLD.synthetic_tenant_count OR NEW.target_rps!=OLD.target_rps
  OR NEW.duration_seconds!=OLD.duration_seconds
  OR NEW.scenario_manifest_sha256!=OLD.scenario_manifest_sha256
  OR NEW.platform_job_id!=OLD.platform_job_id
  OR NEW.created_by_operator_id!=OLD.created_by_operator_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at OR OLD.status IN('succeeded','failed','stopped')
  OR NOT(NEW.status=OLD.status OR(OLD.status='queued' AND NEW.status IN('running','failed','stopped'))
    OR(OLD.status='running' AND NEW.status IN('succeeded','failed','stopped')))
BEGIN SELECT RAISE(ABORT,'load_test_transition_invalid'); END;
CREATE TRIGGER trg_load_test_runs_delete_prd113 BEFORE DELETE ON load_test_runs
BEGIN SELECT RAISE(ABORT,'load_test_immutable'); END;

CREATE TABLE chaos_fault_catalog(
  chaos_fault_id TEXT PRIMARY KEY CHECK(length(chaos_fault_id)=36
    AND substr(chaos_fault_id,1,4)='cft_'),
  fault_code TEXT NOT NULL UNIQUE CHECK(length(fault_code) BETWEEN 3 AND 64
    AND fault_code=lower(fault_code) AND fault_code NOT GLOB '*[^a-z0-9-]*'),
  service_code TEXT NOT NULL CHECK(service_code IN('gateway','control','status','shared')),
  max_blast_tenants INTEGER NOT NULL CHECK(max_blast_tenants BETWEEN 1 AND 100),
  max_duration_seconds INTEGER NOT NULL CHECK(max_duration_seconds BETWEEN 10 AND 3600),
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64
    AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('active','disabled')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL)
);
INSERT INTO chaos_fault_catalog VALUES
  ('cft_00000000000000000000000000000001','dependency-timeout','shared',25,900,'1259aae8d8f794dd5c1e91d47b37e217c0b439d6225f9e77a423f3bfeb9f2197','active',datetime('now')),
  ('cft_00000000000000000000000000000002','redis-unavailable','shared',25,900,'0589a0498286307bc74fc6809654ad31b7644cd306fe544e744280b5657f0219','active',datetime('now')),
  ('cft_00000000000000000000000000000003','kms-unavailable','control',10,600,'b3c8103edddb19854fd56d08c0fca9018de3901018d33c6e638b411a2bad9c33','active',datetime('now')),
  ('cft_00000000000000000000000000000004','object-store-unavailable','control',10,600,'2cd452f9fcae0318b11d6ae0d8cf61d2ac94cf8129b98da4f79c5ff43ec055cc','active',datetime('now')),
  ('cft_00000000000000000000000000000005','cell-isolation','gateway',50,900,'d73b9b2f948a4b4e8fb51f06d5fd981baaf1883b7747491562e1a854403cec62','active',datetime('now')),
  ('cft_00000000000000000000000000000006','region-isolation','gateway',100,600,'3aea2b4f77b5745f147d8b641e9fcb1f738201e64a21d366fd853517e3bab024','active',datetime('now')),
  ('cft_00000000000000000000000000000007','resource-pressure','shared',25,900,'c6b7d791403fdbaf00a44c0a124029577ce3c5813d5002da89ce91368ab134fe','active',datetime('now'));
CREATE TRIGGER trg_chaos_fault_catalog_update_prd113 BEFORE UPDATE ON chaos_fault_catalog
BEGIN SELECT RAISE(ABORT,'chaos_fault_immutable'); END;
CREATE TRIGGER trg_chaos_fault_catalog_delete_prd113 BEFORE DELETE ON chaos_fault_catalog
BEGIN SELECT RAISE(ABORT,'chaos_fault_immutable'); END;

CREATE TABLE chaos_experiments(
  chaos_experiment_id TEXT PRIMARY KEY CHECK(length(chaos_experiment_id)=36
    AND substr(chaos_experiment_id,1,4)='che_'),
  fault_code TEXT NOT NULL,
  service_code TEXT NOT NULL CHECK(service_code IN('gateway','control','status','shared')),
  environment_class TEXT NOT NULL CHECK(environment_class IN('staging','production')),
  region_code TEXT NOT NULL,
  blast_tenant_count INTEGER NOT NULL CHECK(blast_tenant_count BETWEEN 1 AND 100),
  duration_seconds INTEGER NOT NULL CHECK(duration_seconds BETWEEN 10 AND 3600),
  objective_p95_ms INTEGER NOT NULL CHECK(objective_p95_ms BETWEEN 1 AND 60000),
  objective_error_rate_bps INTEGER NOT NULL CHECK(objective_error_rate_bps BETWEEN 0 AND 10000),
  stop_error_rate_bps INTEGER NOT NULL CHECK(stop_error_rate_bps BETWEEN 1 AND 10000
    AND stop_error_rate_bps>=objective_error_rate_bps),
  fault_manifest_sha256 TEXT NOT NULL CHECK(length(fault_manifest_sha256)=64
    AND fault_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN(
    'pending_approval','queued','running','succeeded','failed','killed')),
  platform_job_id TEXT UNIQUE,
  initiated_by_operator_id TEXT NOT NULL,
  kill_generation INTEGER NOT NULL CHECK(kill_generation>=0),
  observed_p95_ms INTEGER CHECK(observed_p95_ms IS NULL OR observed_p95_ms>=0),
  observed_error_rate_bps INTEGER CHECK(observed_error_rate_bps IS NULL
    OR observed_error_rate_bps BETWEEN 0 AND 10000),
  objective_met INTEGER CHECK(objective_met IS NULL OR objective_met IN(0,1)),
  stop_reason_code TEXT CHECK(stop_reason_code IS NULL
    OR stop_reason_code IN('completed','objective_missed','hard_limit','operator_kill','runner_unavailable')),
  result_sha256 TEXT CHECK(result_sha256 IS NULL OR(length(result_sha256)=64
    AND result_sha256 NOT GLOB '*[^0-9a-f]*')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  started_at TEXT CHECK(started_at IS NULL OR unixepoch(started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(fault_code) REFERENCES chaos_fault_catalog(fault_code),
  FOREIGN KEY(region_code) REFERENCES platform_regions(region_code),
  FOREIGN KEY(platform_job_id) REFERENCES platform_jobs(platform_job_id),
  FOREIGN KEY(initiated_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK((status='pending_approval' AND platform_job_id IS NULL AND started_at IS NULL
      AND completed_at IS NULL)
    OR(status='queued' AND platform_job_id IS NOT NULL AND started_at IS NULL
      AND completed_at IS NULL)
    OR(status='running' AND platform_job_id IS NOT NULL AND started_at IS NOT NULL
      AND completed_at IS NULL)
    OR(status IN('succeeded','failed','killed') AND completed_at IS NOT NULL)),
  CHECK((status IN('succeeded','failed') AND objective_met IS NOT NULL
      AND stop_reason_code IS NOT NULL AND result_sha256 IS NOT NULL)
    OR status NOT IN('succeeded','failed'))
);
CREATE INDEX idx_chaos_experiments_recent_prd113
  ON chaos_experiments(created_at DESC,chaos_experiment_id DESC);
CREATE UNIQUE INDEX uq_chaos_active_scope_prd113
  ON chaos_experiments(service_code,region_code)
  WHERE status IN('queued','running');
CREATE TRIGGER trg_chaos_experiments_update_prd113
BEFORE UPDATE ON chaos_experiments FOR EACH ROW
WHEN NEW.chaos_experiment_id!=OLD.chaos_experiment_id
  OR NEW.fault_code!=OLD.fault_code OR NEW.service_code!=OLD.service_code
  OR NEW.environment_class!=OLD.environment_class OR NEW.region_code!=OLD.region_code
  OR NEW.blast_tenant_count!=OLD.blast_tenant_count
  OR NEW.duration_seconds!=OLD.duration_seconds OR NEW.objective_p95_ms!=OLD.objective_p95_ms
  OR NEW.objective_error_rate_bps!=OLD.objective_error_rate_bps
  OR NEW.stop_error_rate_bps!=OLD.stop_error_rate_bps
  OR NEW.fault_manifest_sha256!=OLD.fault_manifest_sha256
  OR NEW.initiated_by_operator_id!=OLD.initiated_by_operator_id
  OR NEW.kill_generation<OLD.kill_generation OR NEW.kill_generation>OLD.kill_generation+1
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at OR OLD.status IN('succeeded','failed','killed')
  OR NOT(NEW.status=OLD.status
    OR(OLD.status='pending_approval' AND NEW.status IN('queued','killed'))
    OR(OLD.status='queued' AND NEW.status IN('running','failed','killed'))
    OR(OLD.status='running' AND NEW.status IN('succeeded','failed','killed')))
BEGIN SELECT RAISE(ABORT,'chaos_experiment_transition_invalid'); END;
CREATE TRIGGER trg_chaos_experiments_delete_prd113 BEFORE DELETE ON chaos_experiments
BEGIN SELECT RAISE(ABORT,'chaos_experiment_immutable'); END;

CREATE TABLE chaos_approvals(
  chaos_approval_id TEXT PRIMARY KEY CHECK(length(chaos_approval_id)=36
    AND substr(chaos_approval_id,1,4)='cap_'),
  chaos_experiment_id TEXT NOT NULL,
  operator_id TEXT NOT NULL,
  approval_kind TEXT NOT NULL CHECK(approval_kind IN('execution','security_review')),
  authority_sha256 TEXT NOT NULL CHECK(length(authority_sha256)=64
    AND authority_sha256 NOT GLOB '*[^0-9a-f]*'),
  approved_at TEXT NOT NULL CHECK(unixepoch(approved_at) IS NOT NULL),
  FOREIGN KEY(chaos_experiment_id) REFERENCES chaos_experiments(chaos_experiment_id),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(chaos_experiment_id,operator_id),
  UNIQUE(chaos_experiment_id,approval_kind)
);
CREATE TRIGGER trg_chaos_approvals_update_prd113 BEFORE UPDATE ON chaos_approvals
BEGIN SELECT RAISE(ABORT,'chaos_approval_immutable'); END;
CREATE TRIGGER trg_chaos_approvals_delete_prd113 BEFORE DELETE ON chaos_approvals
BEGIN SELECT RAISE(ABORT,'chaos_approval_immutable'); END;

CREATE TABLE platform_cost_allocations(
  platform_cost_allocation_id TEXT PRIMARY KEY CHECK(length(platform_cost_allocation_id)=36
    AND substr(platform_cost_allocation_id,1,4)='pca_'),
  billing_period TEXT NOT NULL CHECK(length(billing_period)=7
    AND substr(billing_period,5,1)='-' AND date(billing_period||'-01') IS NOT NULL),
  service_code TEXT NOT NULL CHECK(service_code IN('gateway','control','status','shared')),
  region_code TEXT NOT NULL,
  cost_category TEXT NOT NULL CHECK(cost_category IN(
    'compute','database','cache','object_storage','network','kms','support')),
  amount_micros INTEGER NOT NULL CHECK(amount_micros>=0),
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64
    AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
  allocation_version INTEGER NOT NULL CHECK(allocation_version>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(region_code) REFERENCES platform_regions(region_code),
  UNIQUE(billing_period,service_code,region_code,cost_category,allocation_version)
);
CREATE TRIGGER trg_platform_cost_allocations_update_prd113
BEFORE UPDATE ON platform_cost_allocations
BEGIN SELECT RAISE(ABORT,'platform_cost_allocation_immutable'); END;
CREATE TRIGGER trg_platform_cost_allocations_delete_prd113
BEFORE DELETE ON platform_cost_allocations
BEGIN SELECT RAISE(ABORT,'platform_cost_allocation_immutable'); END;

CREATE TABLE unit_economics_snapshots(
  unit_economics_snapshot_id TEXT PRIMARY KEY CHECK(length(unit_economics_snapshot_id)=36
    AND substr(unit_economics_snapshot_id,1,4)='ues_'),
  billing_period TEXT NOT NULL,
  service_code TEXT NOT NULL CHECK(service_code IN('gateway','control','status','shared')),
  request_count INTEGER NOT NULL CHECK(request_count>=0),
  input_tokens INTEGER NOT NULL CHECK(input_tokens>=0),
  output_tokens INTEGER NOT NULL CHECK(output_tokens>=0),
  total_tokens INTEGER NOT NULL CHECK(total_tokens=input_tokens+output_tokens),
  allocated_cost_micros INTEGER NOT NULL CHECK(allocated_cost_micros>=0),
  cost_per_request_micros INTEGER CHECK(cost_per_request_micros IS NULL
    OR cost_per_request_micros>=0),
  cost_per_million_tokens_micros INTEGER CHECK(cost_per_million_tokens_micros IS NULL
    OR cost_per_million_tokens_micros>=0),
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  usage_watermark TEXT NOT NULL CHECK(length(usage_watermark)=64
    AND usage_watermark NOT GLOB '*[^0-9a-f]*'),
  allocation_version INTEGER NOT NULL CHECK(allocation_version>=1),
  snapshot_sha256 TEXT NOT NULL CHECK(length(snapshot_sha256)=64
    AND snapshot_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(billing_period,service_code,currency,allocation_version),
  CHECK((request_count=0 AND cost_per_request_micros IS NULL)
    OR(request_count>0 AND cost_per_request_micros IS NOT NULL)),
  CHECK((total_tokens=0 AND cost_per_million_tokens_micros IS NULL)
    OR(total_tokens>0 AND cost_per_million_tokens_micros IS NOT NULL))
);
CREATE TRIGGER trg_unit_economics_update_prd113 BEFORE UPDATE ON unit_economics_snapshots
BEGIN SELECT RAISE(ABORT,'unit_economics_immutable'); END;
CREATE TRIGGER trg_unit_economics_delete_prd113 BEFORE DELETE ON unit_economics_snapshots
BEGIN SELECT RAISE(ABORT,'unit_economics_immutable'); END;

CREATE TABLE performance_rollout_blocks(
  performance_rollout_block_id TEXT PRIMARY KEY CHECK(length(performance_rollout_block_id)=36
    AND substr(performance_rollout_block_id,1,4)='prb_'),
  service_code TEXT NOT NULL CHECK(service_code IN('gateway','control','status')),
  scenario_code TEXT NOT NULL,
  environment_class TEXT NOT NULL CHECK(environment_class IN('dev','staging','production')),
  load_test_run_id TEXT NOT NULL UNIQUE,
  performance_baseline_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('active','cleared')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  cleared_at TEXT CHECK(cleared_at IS NULL OR unixepoch(cleared_at) IS NOT NULL),
  FOREIGN KEY(load_test_run_id) REFERENCES load_test_runs(load_test_run_id),
  FOREIGN KEY(performance_baseline_id) REFERENCES performance_baselines(performance_baseline_id),
  FOREIGN KEY(scenario_code,service_code)
    REFERENCES capacity_scenarios(scenario_code,service_code),
  CHECK((status='active' AND cleared_at IS NULL)
    OR(status='cleared' AND cleared_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_performance_rollout_block_prd113
  ON performance_rollout_blocks(service_code,scenario_code,environment_class)
  WHERE status='active';
CREATE TRIGGER trg_performance_rollout_blocks_update_prd113
BEFORE UPDATE ON performance_rollout_blocks FOR EACH ROW
WHEN NEW.performance_rollout_block_id!=OLD.performance_rollout_block_id
  OR NEW.service_code!=OLD.service_code OR NEW.scenario_code!=OLD.scenario_code
  OR NEW.environment_class!=OLD.environment_class OR NEW.load_test_run_id!=OLD.load_test_run_id
  OR NEW.performance_baseline_id!=OLD.performance_baseline_id
  OR NEW.created_at!=OLD.created_at OR OLD.status!='active' OR NEW.status!='cleared'
BEGIN SELECT RAISE(ABORT,'performance_rollout_block_transition_invalid'); END;
CREATE TRIGGER trg_performance_rollout_blocks_delete_prd113
BEFORE DELETE ON performance_rollout_blocks
BEGIN SELECT RAISE(ABORT,'performance_rollout_block_immutable'); END;

CREATE TABLE capacity_commands(
  capacity_command_id TEXT PRIMARY KEY CHECK(length(capacity_command_id)=36
    AND substr(capacity_command_id,1,4)='ccm_'),
  operator_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(action_code IN(
    'forecast.create','load_test.create','chaos.create','chaos.approve','chaos.kill')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64
    AND idempotency_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  result_object_type TEXT NOT NULL CHECK(result_object_type IN(
    'capacity_forecast','load_test_run','chaos_experiment')),
  result_object_id TEXT NOT NULL CHECK(length(result_object_id)=36),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(operator_id,idempotency_sha256)
);
CREATE TRIGGER trg_capacity_commands_update_prd113 BEFORE UPDATE ON capacity_commands
BEGIN SELECT RAISE(ABORT,'capacity_command_immutable'); END;
CREATE TRIGGER trg_capacity_commands_delete_prd113 BEFORE DELETE ON capacity_commands
BEGIN SELECT RAISE(ABORT,'capacity_command_immutable'); END;

CREATE TABLE capacity_event_outbox(
  capacity_event_outbox_id TEXT PRIMARY KEY CHECK(length(capacity_event_outbox_id)=36
    AND substr(capacity_event_outbox_id,1,4)='ceo_'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'capacity.threshold_reached','performance.regressed','chaos.objective_missed')),
  service_code TEXT NOT NULL CHECK(service_code IN('gateway','control','status','shared')),
  scenario_code TEXT,
  outcome_code TEXT NOT NULL CHECK(length(outcome_code) BETWEEN 3 AND 96),
  aggregate_sha256 TEXT NOT NULL CHECK(length(aggregate_sha256)=64
    AND aggregate_sha256 NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('pending','delivered')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count>=0),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  CHECK((status='pending' AND delivered_at IS NULL)
    OR(status='delivered' AND delivered_at IS NOT NULL))
);
CREATE TRIGGER trg_capacity_events_update_prd113
BEFORE UPDATE ON capacity_event_outbox FOR EACH ROW
WHEN NEW.capacity_event_outbox_id!=OLD.capacity_event_outbox_id
  OR NEW.event_type!=OLD.event_type OR NEW.service_code!=OLD.service_code
  OR NEW.scenario_code IS NOT OLD.scenario_code OR NEW.outcome_code!=OLD.outcome_code
  OR NEW.aggregate_sha256!=OLD.aggregate_sha256 OR NEW.request_id!=OLD.request_id
  OR NEW.trace_id!=OLD.trace_id OR NEW.occurred_at!=OLD.occurred_at
  OR OLD.status!='pending' OR NEW.status!='delivered'
  OR NEW.attempt_count!=OLD.attempt_count+1 OR NEW.delivered_at IS NULL
BEGIN SELECT RAISE(ABORT,'capacity_event_transition_invalid'); END;
CREATE TRIGGER trg_capacity_events_delete_prd113 BEFORE DELETE ON capacity_event_outbox
BEGIN SELECT RAISE(ABORT,'capacity_event_immutable'); END;

DROP TRIGGER trg_platform_job_types_update_prd106;
UPDATE platform_job_types
SET status='active',revision=2,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE type_code='capacity.run' AND status='disabled' AND revision=1;
CREATE TRIGGER trg_platform_job_types_update_prd106
BEFORE UPDATE ON platform_job_types FOR EACH ROW
WHEN NEW.type_code!=OLD.type_code OR NEW.contract_version!=OLD.contract_version
  OR NEW.artifact_sha256!=OLD.artifact_sha256 OR NEW.queue_code!=OLD.queue_code
  OR NEW.resource_class!=OLD.resource_class OR NEW.max_attempts!=OLD.max_attempts
  OR NEW.timeout_seconds!=OLD.timeout_seconds OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'platform_job_type_immutable'); END;

CREATE TEMP TABLE prd113_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd113_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT status||':'||revision FROM platform_job_types
      WHERE type_code='capacity.run')='active:2' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_capacity_permission_extensions)=4 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_capacity_role_permissions)=10 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM capacity_service_states)=4 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM capacity_scenarios)=4 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM chaos_fault_catalog)=7 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd113_up_final_guard;
COMMIT;
