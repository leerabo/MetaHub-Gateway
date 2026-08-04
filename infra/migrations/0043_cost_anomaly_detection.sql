-- PRD-053 成本异常检测基础：新增可恢复检测运行、不可变异常证据、动作事实和临时限流覆盖。
-- 本迁移只消费 0041 正式 FinOps 账本，不改写分录；累计金额使用 canonical 十进制 TEXT，
-- 避免 SQLite SUM 或 JavaScript Number 在大额企业账本上产生溢出和精度漂移。
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

-- 在任何持久 DDL 前确认 0042、Audit 依赖和目标名称均处于预期状态。Audit 需要保真重建，
-- 因而未知索引、触发器、View 或外键依赖必须失败关闭，不能在 DROP TABLE 时静默丢失。
CREATE TEMP TABLE prd053_prerequisite_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd053_prerequisite_guard
BEFORE INSERT ON prd053_prerequisite_guard
FOR EACH ROW
WHEN NEW.issue_count != 0
BEGIN
  SELECT RAISE(ABORT, 'prd053_prerequisite_missing');
END;
-- 汇总前置版本、Audit 依赖和目标名称冲突，任一异常都在持久 DDL 前原子中止。
INSERT INTO prd053_prerequisite_guard(issue_count)
SELECT
  CASE WHEN (
    SELECT COUNT(*) FROM schema_migrations
    WHERE version='0042' AND status='applied'
  )=1 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='table' AND name IN (
      'tenants', 'users', 'environments', 'virtual_keys', 'request_logs',
      'finops_ledger_entries', 'audit_logs', 'webhook_endpoints',
      'webhook_deliveries', 'webhook_delivery_attempts'
    )
  )=10 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='index' AND tbl_name='audit_logs' AND sql IS NOT NULL
      AND name IN (
        'idx_audit_logs_tenant_time', 'idx_audit_logs_tenant_actor_time',
        'idx_audit_logs_tenant_object_time', 'idx_audit_logs_tenant_action_time',
        'idx_audit_logs_tenant_request', 'idx_audit_logs_dry_run_request_unique',
        'idx_audit_logs_model_quality_request_unique',
        'idx_audit_logs_finops_adjustment_unique'
      )
  )=8 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE type='index' AND tbl_name='audit_logs' AND sql IS NOT NULL
       AND name NOT IN (
         'idx_audit_logs_tenant_time', 'idx_audit_logs_tenant_actor_time',
         'idx_audit_logs_tenant_object_time', 'idx_audit_logs_tenant_action_time',
         'idx_audit_logs_tenant_request', 'idx_audit_logs_dry_run_request_unique',
         'idx_audit_logs_model_quality_request_unique',
         'idx_audit_logs_finops_adjustment_unique'
       ))
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='trigger' AND tbl_name='audit_logs' AND name IN (
      'trg_audit_logs_owner_insert', 'trg_audit_logs_immutable_update',
      'trg_audit_logs_immutable_delete', 'trg_audit_logs_prd050_conflict_insert',
      'trg_audit_logs_prd050_rowid_insert'
    )
  )=5 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE type='trigger' AND tbl_name='audit_logs' AND name NOT IN (
       'trg_audit_logs_owner_insert', 'trg_audit_logs_immutable_update',
       'trg_audit_logs_immutable_delete', 'trg_audit_logs_prd050_conflict_insert',
       'trg_audit_logs_prd050_rowid_insert'
     ))
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE type='view' AND lower(COALESCE(sql, '')) LIKE '%audit_logs%')
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE type='trigger' AND tbl_name!='audit_logs'
       AND lower(COALESCE(sql, '')) LIKE '%audit_logs%')
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE type='table' AND name!='audit_logs'
       AND lower(COALESCE(sql, '')) LIKE '%references%audit_logs%')
  + (SELECT COUNT(*) FROM sqlite_master WHERE name IN (
    'cost_anomaly_detection_runs', 'cost_anomalies', 'cost_anomaly_actions',
    'temporary_rate_limit_overrides', 'audit_logs_prd053_new'
  ));
DROP TRIGGER trg_prd053_prerequisite_guard;
DROP TABLE prd053_prerequisite_guard;

-- 每个 tenant + Environment + 5 分钟窗口只有一条可恢复运行。processing 行持有 lease；
-- final 行清空 lease，并保存本 revision 的完整性、来源计数和稳定错误码。
CREATE TABLE cost_anomaly_detection_runs (
  detection_run_id TEXT PRIMARY KEY CHECK (
    length(detection_run_id)=36
    AND substr(detection_run_id, 1, 4)='car_'
    AND substr(detection_run_id, 5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
  window_start TEXT NOT NULL,
  window_end TEXT NOT NULL,
  baseline_start TEXT NOT NULL,
  baseline_end TEXT NOT NULL,
  ledger_watermark INTEGER NOT NULL CHECK (
    typeof(ledger_watermark)='integer' AND ledger_watermark>=1
  ),
  evaluation_revision INTEGER NOT NULL CHECK (
    typeof(evaluation_revision)='integer' AND evaluation_revision>=1
  ),
  status TEXT NOT NULL CHECK (
    status IN ('processing', 'completed', 'partial', 'failed')
  ),
  environment_current_complete INTEGER CHECK (
    environment_current_complete IS NULL
    OR (
      typeof(environment_current_complete)='integer'
      AND environment_current_complete IN (0, 1)
    )
  ),
  environment_baseline_complete INTEGER CHECK (
    environment_baseline_complete IS NULL
    OR (
      typeof(environment_baseline_complete)='integer'
      AND environment_baseline_complete IN (0, 1)
    )
  ),
  partial_scope_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(partial_scope_count)='integer' AND partial_scope_count>=0
  ),
  source_entry_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(source_entry_count)='integer' AND source_entry_count>=0
  ),
  estimated_entry_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(estimated_entry_count)='integer'
    AND estimated_entry_count BETWEEN 0 AND source_entry_count
  ),
  legacy_entry_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(legacy_entry_count)='integer'
    AND legacy_entry_count BETWEEN 0 AND source_entry_count
  ),
  unpriced_entry_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(unpriced_entry_count)='integer' AND unpriced_entry_count>=0
  ),
  anomaly_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(anomaly_count)='integer' AND anomaly_count>=0
  ),
  suppressed_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(suppressed_count)='integer' AND suppressed_count>=0
  ),
  error_code TEXT CHECK (
    error_code IS NULL OR error_code IN (
      'cost_anomaly.detector_source_incomplete',
      'cost_anomaly.detector_source_owner_invalid',
      'cost_anomaly.detector_amount_invalid',
      'cost_anomaly.detector_schema_unavailable',
      'cost_anomaly.detector_database_unavailable',
      'cost_anomaly.detector_clock_invalid'
    )
  ),
  claim_token TEXT CHECK (
    claim_token IS NULL OR (
      length(claim_token) BETWEEN 16 AND 128 AND instr(claim_token, char(0))=0
    )
  ),
  lease_owner_id TEXT CHECK (
    lease_owner_id IS NULL OR (
      length(lease_owner_id) BETWEEN 1 AND 128 AND instr(lease_owner_id, char(0))=0
    )
  ),
  lease_expires_at TEXT,
  operation_request_id TEXT NOT NULL CHECK (
    length(operation_request_id) BETWEEN 1 AND 128
    AND instr(operation_request_id, char(0))=0
  ),
  operation_trace_id TEXT NOT NULL CHECK (
    length(operation_trace_id) BETWEEN 1 AND 128
    AND instr(operation_trace_id, char(0))=0
  ),
  started_at TEXT NOT NULL,
  completed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, environment_id)
    REFERENCES environments(tenant_id, environment_id),
  UNIQUE (tenant_id, environment_id, window_start),
  UNIQUE (tenant_id, environment_id, detection_run_id),
  CHECK (
    length(window_start)=20 AND window_start GLOB
      '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
    AND unixepoch(window_start) IS NOT NULL
    AND unixepoch(window_start) % 300 = 0
  ),
  CHECK (
    length(window_end)=20 AND unixepoch(window_end) IS NOT NULL
    AND unixepoch(window_end)-unixepoch(window_start)=300
  ),
  CHECK (
    length(baseline_start)=20 AND unixepoch(baseline_start) IS NOT NULL
    AND unixepoch(window_start)-unixepoch(baseline_start)=86400
  ),
  CHECK (
    length(baseline_end)=20 AND unixepoch(baseline_end) IS NOT NULL
    AND baseline_end=window_start
  ),
  CHECK (
    unixepoch(started_at) IS NOT NULL AND unixepoch(created_at) IS NOT NULL
    AND unixepoch(updated_at) IS NOT NULL
    AND unixepoch(started_at)>=unixepoch(created_at)
    AND unixepoch(updated_at)>=unixepoch(created_at)
  ),
  CHECK (
    (status='processing'
      AND environment_current_complete IS NULL
      AND environment_baseline_complete IS NULL
      AND partial_scope_count=0 AND source_entry_count=0
      AND estimated_entry_count=0 AND legacy_entry_count=0
      AND unpriced_entry_count=0 AND anomaly_count=0 AND suppressed_count=0
      AND error_code IS NULL AND completed_at IS NULL
      AND claim_token IS NOT NULL AND lease_owner_id IS NOT NULL
      AND lease_expires_at IS NOT NULL
      AND unixepoch(lease_expires_at)>unixepoch(started_at))
    OR (status='completed'
      AND environment_current_complete=1 AND environment_baseline_complete=1
      AND partial_scope_count=0 AND error_code IS NULL
      AND completed_at IS NOT NULL
      AND claim_token IS NULL AND lease_owner_id IS NULL AND lease_expires_at IS NULL)
    OR (status='partial'
      AND environment_current_complete IS NOT NULL
      AND environment_baseline_complete IS NOT NULL
      AND (environment_current_complete=0 OR environment_baseline_complete=0
        OR partial_scope_count>0)
      AND error_code='cost_anomaly.detector_source_incomplete'
      AND completed_at IS NOT NULL
      AND claim_token IS NULL AND lease_owner_id IS NULL AND lease_expires_at IS NULL)
    OR (status='failed'
      AND error_code IN (
        'cost_anomaly.detector_source_owner_invalid',
        'cost_anomaly.detector_amount_invalid',
        'cost_anomaly.detector_schema_unavailable',
        'cost_anomaly.detector_database_unavailable',
        'cost_anomaly.detector_clock_invalid'
      )
      AND completed_at IS NOT NULL
      AND claim_token IS NULL AND lease_owner_id IS NULL AND lease_expires_at IS NULL)
  ),
  CHECK (
    completed_at IS NULL OR (
      unixepoch(completed_at) IS NOT NULL
      AND unixepoch(completed_at)>=unixepoch(started_at)
      AND unixepoch(updated_at)>=unixepoch(completed_at)
    )
  )
);

-- CostAnomaly 是首次跨阈值时冻结的解释证据。status 和 outbox 是仅有的可变区域；
-- 成本、窗口、scope、账本水位和配置快照在迟到重评后也不能漂移。
CREATE TABLE cost_anomalies (
  anomaly_sequence INTEGER PRIMARY KEY AUTOINCREMENT CHECK (anomaly_sequence>0),
  cost_anomaly_id TEXT NOT NULL UNIQUE CHECK (
    length(cost_anomaly_id)=36
    AND substr(cost_anomaly_id, 1, 4)='can_'
    AND substr(cost_anomaly_id, 5) NOT GLOB '*[^0-9a-f]*'
  ),
  event_id TEXT NOT NULL UNIQUE CHECK (
    length(event_id)=38
    AND substr(event_id, 1, 6)='caevt_'
    AND substr(event_id, 7) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  environment_id TEXT NOT NULL,
  detection_run_id TEXT NOT NULL,
  rule_type TEXT NOT NULL CHECK (
    rule_type IN ('tenant_spend_spike', 'virtual_key_spend_spike', 'virtual_key_off_hours')
  ),
  severity TEXT NOT NULL CHECK (severity IN ('warning', 'critical')),
  status TEXT NOT NULL DEFAULT 'open' CHECK (
    status IN ('open', 'acknowledged', 'dismissed', 'mitigated')
  ),
  scope_type TEXT NOT NULL CHECK (scope_type IN ('tenant', 'virtual_key')),
  scope_id TEXT NOT NULL CHECK (length(scope_id) BETWEEN 1 AND 128),
  virtual_key_id TEXT,
  top_virtual_key_id TEXT,
  project_id_snapshot TEXT,
  application_id_snapshot TEXT,
  window_start TEXT NOT NULL,
  window_end TEXT NOT NULL,
  baseline_start TEXT NOT NULL,
  baseline_end TEXT NOT NULL,
  current_cost_micros TEXT NOT NULL CHECK (
    typeof(current_cost_micros)='text' AND length(current_cost_micros) BETWEEN 1 AND 40
    AND current_cost_micros NOT GLOB '*[^0-9]*'
    AND (current_cost_micros='0' OR substr(current_cost_micros, 1, 1) BETWEEN '1' AND '9')
  ),
  baseline_total_micros TEXT NOT NULL CHECK (
    typeof(baseline_total_micros)='text' AND length(baseline_total_micros) BETWEEN 1 AND 40
    AND baseline_total_micros NOT GLOB '*[^0-9]*'
    AND (baseline_total_micros='0' OR substr(baseline_total_micros, 1, 1) BETWEEN '1' AND '9')
  ),
  baseline_bucket_count INTEGER NOT NULL DEFAULT 288 CHECK (
    typeof(baseline_bucket_count)='integer' AND baseline_bucket_count=288
  ),
  observed_ratio_millis TEXT CHECK (
    observed_ratio_millis IS NULL OR (
      typeof(observed_ratio_millis)='text'
      AND length(observed_ratio_millis) BETWEEN 1 AND 40
      AND observed_ratio_millis NOT GLOB '*[^0-9]*'
      AND (observed_ratio_millis='0'
        OR substr(observed_ratio_millis, 1, 1) BETWEEN '1' AND '9')
    )
  ),
  threshold_multiplier_millis INTEGER NOT NULL CHECK (
    typeof(threshold_multiplier_millis)='integer'
    AND threshold_multiplier_millis BETWEEN 1001 AND 100000
  ),
  minimum_cost_micros INTEGER NOT NULL CHECK (
    typeof(minimum_cost_micros)='integer'
    AND minimum_cost_micros BETWEEN 1 AND 1000000000000
  ),
  cooldown_minutes INTEGER NOT NULL DEFAULT 30 CHECK (
    typeof(cooldown_minutes)='integer' AND cooldown_minutes=30
  ),
  source_entry_count INTEGER NOT NULL CHECK (
    typeof(source_entry_count)='integer' AND source_entry_count>=1
  ),
  estimated_entry_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(estimated_entry_count)='integer'
    AND estimated_entry_count BETWEEN 0 AND source_entry_count
  ),
  legacy_entry_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(legacy_entry_count)='integer'
    AND legacy_entry_count BETWEEN 0 AND source_entry_count
  ),
  affected_virtual_key_count INTEGER NOT NULL CHECK (
    typeof(affected_virtual_key_count)='integer' AND affected_virtual_key_count>=1
  ),
  representative_request_id TEXT NOT NULL CHECK (
    length(representative_request_id) BETWEEN 1 AND 128
    AND instr(representative_request_id, char(0))=0
  ),
  representative_trace_id TEXT NOT NULL CHECK (
    length(representative_trace_id) BETWEEN 1 AND 128
    AND instr(representative_trace_id, char(0))=0
  ),
  source_ledger_sequence_min INTEGER NOT NULL CHECK (
    typeof(source_ledger_sequence_min)='integer' AND source_ledger_sequence_min>=1
  ),
  source_ledger_sequence_max INTEGER NOT NULL CHECK (
    typeof(source_ledger_sequence_max)='integer'
    AND source_ledger_sequence_max>=source_ledger_sequence_min
  ),
  recommended_action TEXT NOT NULL CHECK (
    recommended_action IN ('review_impacted_keys', 'apply_temporary_rate_limit')
  ),
  detection_version TEXT NOT NULL DEFAULT 'cost-anomaly-rules-v1' CHECK (
    detection_version='cost-anomaly-rules-v1'
  ),
  detected_at TEXT NOT NULL,
  mitigation_eligible_until TEXT NOT NULL,
  event_dispatch_status TEXT NOT NULL DEFAULT 'pending' CHECK (
    event_dispatch_status IN ('pending', 'processing', 'dispatched', 'failed')
  ),
  event_dispatch_attempts INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(event_dispatch_attempts)='integer'
    AND event_dispatch_attempts BETWEEN 0 AND 10
  ),
  event_next_attempt_at TEXT,
  event_dispatched_at TEXT,
  event_claim_token TEXT CHECK (
    event_claim_token IS NULL OR (
      length(event_claim_token) BETWEEN 16 AND 128
      AND instr(event_claim_token, char(0))=0
    )
  ),
  event_lease_owner_id TEXT CHECK (
    event_lease_owner_id IS NULL OR (
      length(event_lease_owner_id) BETWEEN 1 AND 128
      AND instr(event_lease_owner_id, char(0))=0
    )
  ),
  event_lease_expires_at TEXT,
  event_last_error_code TEXT CHECK (
    event_last_error_code IS NULL OR event_last_error_code IN (
      'webhook.event_type_invalid',
      'webhook.delivery_unavailable',
      'cost_anomaly.detector_database_unavailable'
    )
  ),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, environment_id)
    REFERENCES environments(tenant_id, environment_id),
  FOREIGN KEY (tenant_id, environment_id, detection_run_id)
    REFERENCES cost_anomaly_detection_runs(tenant_id, environment_id, detection_run_id),
  FOREIGN KEY (tenant_id, virtual_key_id)
    REFERENCES virtual_keys(tenant_id, virtual_key_id),
  FOREIGN KEY (tenant_id, top_virtual_key_id)
    REFERENCES virtual_keys(tenant_id, virtual_key_id),
  FOREIGN KEY (tenant_id, representative_request_id)
    REFERENCES request_logs(tenant_id, request_id),
  UNIQUE (tenant_id, cost_anomaly_id),
  UNIQUE (tenant_id, event_id),
  UNIQUE (tenant_id, environment_id, rule_type, scope_type, scope_id, window_start),
  CHECK (
    (rule_type='tenant_spend_spike' AND scope_type='tenant'
      AND scope_id=tenant_id AND virtual_key_id IS NULL
      AND severity='critical' AND recommended_action='review_impacted_keys'
      AND observed_ratio_millis IS NOT NULL)
    OR (rule_type='virtual_key_spend_spike' AND scope_type='virtual_key'
      AND scope_id=virtual_key_id AND virtual_key_id IS NOT NULL
      AND severity='critical' AND recommended_action='apply_temporary_rate_limit'
      AND observed_ratio_millis IS NOT NULL)
    OR (rule_type='virtual_key_off_hours' AND scope_type='virtual_key'
      AND scope_id=virtual_key_id AND virtual_key_id IS NOT NULL
      AND severity='warning' AND recommended_action='apply_temporary_rate_limit'
      AND observed_ratio_millis IS NULL)
  ),
  CHECK (scope_type!='virtual_key' OR top_virtual_key_id IS NULL OR top_virtual_key_id=virtual_key_id),
  CHECK (scope_type!='virtual_key' OR affected_virtual_key_count=1),
  CHECK (
    unixepoch(window_start) IS NOT NULL AND unixepoch(window_start) % 300=0
    AND unixepoch(window_end)-unixepoch(window_start)=300
    AND unixepoch(window_start)-unixepoch(baseline_start)=86400
    AND baseline_end=window_start
  ),
  CHECK (
    unixepoch(detected_at) IS NOT NULL
    AND unixepoch(mitigation_eligible_until)-unixepoch(detected_at)=86400
    AND unixepoch(created_at) IS NOT NULL AND unixepoch(updated_at) IS NOT NULL
    AND unixepoch(updated_at)>=unixepoch(created_at)
  ),
  CHECK (
    (event_dispatch_status='pending'
      AND event_next_attempt_at IS NOT NULL
      AND unixepoch(event_next_attempt_at) IS NOT NULL
      AND event_dispatched_at IS NULL
      AND event_claim_token IS NULL AND event_lease_owner_id IS NULL
      AND event_lease_expires_at IS NULL)
    OR (event_dispatch_status='processing'
      AND event_dispatch_attempts BETWEEN 1 AND 10
      AND event_dispatched_at IS NULL
      AND event_claim_token IS NOT NULL AND event_lease_owner_id IS NOT NULL
      AND event_lease_expires_at IS NOT NULL
      AND unixepoch(event_lease_expires_at) IS NOT NULL)
    OR (event_dispatch_status='dispatched'
      AND event_dispatched_at IS NOT NULL AND unixepoch(event_dispatched_at) IS NOT NULL
      AND event_claim_token IS NULL AND event_lease_owner_id IS NULL
      AND event_lease_expires_at IS NULL AND event_last_error_code IS NULL)
    OR (event_dispatch_status='failed'
      AND event_dispatch_attempts=10 AND event_dispatched_at IS NULL
      AND event_claim_token IS NULL AND event_lease_owner_id IS NULL
      AND event_lease_expires_at IS NULL AND event_last_error_code IS NOT NULL)
  )
);

-- 动作是 append-only 幂等事实。target_override_id 形成与 override 的循环关系，因此不建循环
-- 外键，而由 owner trigger 在 release action 插入时校验同 tenant、同 anomaly 的 active override。
CREATE TABLE cost_anomaly_actions (
  cost_anomaly_action_id TEXT PRIMARY KEY CHECK (
    length(cost_anomaly_action_id)=36
    AND substr(cost_anomaly_action_id, 1, 4)='caa_'
    AND substr(cost_anomaly_action_id, 5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  cost_anomaly_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  action_type TEXT NOT NULL CHECK (
    action_type IN (
      'acknowledge', 'dismiss', 'apply_temporary_rate_limit',
      'release_temporary_rate_limit'
    )
  ),
  status_before TEXT NOT NULL CHECK (
    status_before IN ('open', 'acknowledged', 'dismissed', 'mitigated')
  ),
  status_after TEXT NOT NULL CHECK (
    status_after IN ('open', 'acknowledged', 'dismissed', 'mitigated')
  ),
  dismiss_reason TEXT CHECK (
    dismiss_reason IS NULL OR dismiss_reason IN (
      'false_positive', 'expected_activity', 'resolved_without_action', 'other'
    )
  ),
  duration_minutes INTEGER CHECK (
    duration_minutes IS NULL OR (
      typeof(duration_minutes)='integer' AND duration_minutes BETWEEN 5 AND 1440
    )
  ),
  rpm_limit INTEGER CHECK (
    rpm_limit IS NULL OR (typeof(rpm_limit)='integer' AND rpm_limit BETWEEN 1 AND 100000000)
  ),
  tpm_limit INTEGER CHECK (
    tpm_limit IS NULL OR (typeof(tpm_limit)='integer' AND tpm_limit BETWEEN 1 AND 100000000)
  ),
  concurrency_limit INTEGER CHECK (
    concurrency_limit IS NULL OR (
      typeof(concurrency_limit)='integer' AND concurrency_limit BETWEEN 1 AND 100000000
    )
  ),
  target_override_id TEXT,
  effective_rpm_before INTEGER CHECK (
    effective_rpm_before IS NULL OR (
      typeof(effective_rpm_before)='integer' AND effective_rpm_before BETWEEN 1 AND 100000000
    )
  ),
  effective_tpm_before INTEGER CHECK (
    effective_tpm_before IS NULL OR (
      typeof(effective_tpm_before)='integer' AND effective_tpm_before BETWEEN 1 AND 100000000
    )
  ),
  effective_concurrency_before INTEGER CHECK (
    effective_concurrency_before IS NULL OR (
      typeof(effective_concurrency_before)='integer'
      AND effective_concurrency_before BETWEEN 1 AND 100000000
    )
  ),
  effective_rpm_after INTEGER CHECK (
    effective_rpm_after IS NULL OR (
      typeof(effective_rpm_after)='integer' AND effective_rpm_after BETWEEN 1 AND 100000000
    )
  ),
  effective_tpm_after INTEGER CHECK (
    effective_tpm_after IS NULL OR (
      typeof(effective_tpm_after)='integer' AND effective_tpm_after BETWEEN 1 AND 100000000
    )
  ),
  effective_concurrency_after INTEGER CHECK (
    effective_concurrency_after IS NULL OR (
      typeof(effective_concurrency_after)='integer'
      AND effective_concurrency_after BETWEEN 1 AND 100000000
    )
  ),
  operation_request_id TEXT NOT NULL CHECK (
    length(operation_request_id) BETWEEN 1 AND 128
    AND instr(operation_request_id, char(0))=0
  ),
  operation_trace_id TEXT NOT NULL CHECK (
    length(operation_trace_id) BETWEEN 1 AND 128
    AND instr(operation_trace_id, char(0))=0
  ),
  idempotency_key_sha256 TEXT NOT NULL CHECK (
    length(idempotency_key_sha256)=64
    AND idempotency_key_sha256 NOT GLOB '*[^0-9a-f]*'
  ),
  intent_sha256 TEXT NOT NULL CHECK (
    length(intent_sha256)=64 AND intent_sha256 NOT GLOB '*[^0-9a-f]*'
  ),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, cost_anomaly_id)
    REFERENCES cost_anomalies(tenant_id, cost_anomaly_id),
  FOREIGN KEY (tenant_id, actor_user_id) REFERENCES users(tenant_id, user_id),
  UNIQUE (tenant_id, cost_anomaly_action_id),
  UNIQUE (tenant_id, actor_user_id, idempotency_key_sha256),
  CHECK (
    (action_type='acknowledge' AND status_before='open' AND status_after='acknowledged'
      AND dismiss_reason IS NULL AND duration_minutes IS NULL
      AND rpm_limit IS NULL AND tpm_limit IS NULL AND concurrency_limit IS NULL
      AND target_override_id IS NULL
      AND effective_rpm_before IS NULL AND effective_tpm_before IS NULL
      AND effective_concurrency_before IS NULL AND effective_rpm_after IS NULL
      AND effective_tpm_after IS NULL AND effective_concurrency_after IS NULL)
    OR (action_type='dismiss' AND status_before IN ('open', 'acknowledged')
      AND status_after='dismissed' AND dismiss_reason IS NOT NULL
      AND duration_minutes IS NULL AND rpm_limit IS NULL AND tpm_limit IS NULL
      AND concurrency_limit IS NULL AND target_override_id IS NULL
      AND effective_rpm_before IS NULL AND effective_tpm_before IS NULL
      AND effective_concurrency_before IS NULL AND effective_rpm_after IS NULL
      AND effective_tpm_after IS NULL AND effective_concurrency_after IS NULL)
    OR (action_type='apply_temporary_rate_limit'
      AND status_before IN ('open', 'acknowledged') AND status_after='mitigated'
      AND dismiss_reason IS NULL AND duration_minutes IS NOT NULL
      AND (rpm_limit IS NOT NULL OR tpm_limit IS NOT NULL OR concurrency_limit IS NOT NULL)
      AND target_override_id IS NULL)
    OR (action_type='release_temporary_rate_limit'
      AND status_before='mitigated' AND status_after='mitigated'
      AND dismiss_reason IS NULL AND duration_minutes IS NULL
      AND rpm_limit IS NULL AND tpm_limit IS NULL AND concurrency_limit IS NULL
      AND target_override_id IS NOT NULL)
  )
);

-- 临时覆盖是永久限流之上的独立收紧层。active 到期按服务端时间自然失效；唯一合法更新
-- 是在到期前一次性绑定 release action，永久规则和其他 anomaly 的覆盖均不会被改写。
CREATE TABLE temporary_rate_limit_overrides (
  temporary_rate_limit_override_id TEXT PRIMARY KEY CHECK (
    length(temporary_rate_limit_override_id)=36
    AND substr(temporary_rate_limit_override_id, 1, 4)='trl_'
    AND substr(temporary_rate_limit_override_id, 5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  virtual_key_id TEXT NOT NULL,
  source_type TEXT NOT NULL DEFAULT 'cost_anomaly' CHECK (source_type='cost_anomaly'),
  source_id TEXT NOT NULL,
  source_action_id TEXT NOT NULL,
  release_action_id TEXT,
  rpm_limit INTEGER CHECK (
    rpm_limit IS NULL OR (typeof(rpm_limit)='integer' AND rpm_limit BETWEEN 1 AND 100000000)
  ),
  tpm_limit INTEGER CHECK (
    tpm_limit IS NULL OR (typeof(tpm_limit)='integer' AND tpm_limit BETWEEN 1 AND 100000000)
  ),
  concurrency_limit INTEGER CHECK (
    concurrency_limit IS NULL OR (
      typeof(concurrency_limit)='integer' AND concurrency_limit BETWEEN 1 AND 100000000
    )
  ),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'released')),
  starts_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  created_by TEXT NOT NULL,
  released_by TEXT,
  released_at TEXT,
  operation_request_id TEXT NOT NULL CHECK (
    length(operation_request_id) BETWEEN 1 AND 128
    AND instr(operation_request_id, char(0))=0
  ),
  operation_trace_id TEXT NOT NULL CHECK (
    length(operation_trace_id) BETWEEN 1 AND 128
    AND instr(operation_trace_id, char(0))=0
  ),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, virtual_key_id)
    REFERENCES virtual_keys(tenant_id, virtual_key_id),
  FOREIGN KEY (tenant_id, source_id)
    REFERENCES cost_anomalies(tenant_id, cost_anomaly_id),
  FOREIGN KEY (tenant_id, source_action_id)
    REFERENCES cost_anomaly_actions(tenant_id, cost_anomaly_action_id),
  FOREIGN KEY (tenant_id, release_action_id)
    REFERENCES cost_anomaly_actions(tenant_id, cost_anomaly_action_id),
  FOREIGN KEY (tenant_id, created_by) REFERENCES users(tenant_id, user_id),
  FOREIGN KEY (tenant_id, released_by) REFERENCES users(tenant_id, user_id),
  UNIQUE (tenant_id, temporary_rate_limit_override_id),
  UNIQUE (tenant_id, source_id),
  UNIQUE (tenant_id, source_action_id),
  UNIQUE (tenant_id, release_action_id),
  CHECK (rpm_limit IS NOT NULL OR tpm_limit IS NOT NULL OR concurrency_limit IS NOT NULL),
  CHECK (
    unixepoch(starts_at) IS NOT NULL AND unixepoch(expires_at) IS NOT NULL
    AND unixepoch(expires_at)-unixepoch(starts_at) BETWEEN 300 AND 86400
    AND (unixepoch(expires_at)-unixepoch(starts_at)) % 60=0
    AND unixepoch(created_at) IS NOT NULL AND unixepoch(updated_at) IS NOT NULL
    AND unixepoch(updated_at)>=unixepoch(created_at)
  ),
  CHECK (
    (status='active' AND release_action_id IS NULL
      AND released_by IS NULL AND released_at IS NULL)
    OR (status='released' AND release_action_id IS NOT NULL
      AND released_by IS NOT NULL AND released_at IS NOT NULL
      AND unixepoch(released_at) IS NOT NULL
      AND unixepoch(released_at)>=unixepoch(starts_at)
      AND unixepoch(released_at)<unixepoch(expires_at))
  )
);

-- Scheduler、租户列表、滚动冷却和 Gateway overlay 都必须从 tenant-first 索引进入；
-- 0042 已提供四条正式账本部分索引，本迁移不重复创建成本源索引。
CREATE INDEX idx_cost_anomaly_runs_status_window_prd053
  ON cost_anomaly_detection_runs(status, window_start, lease_expires_at, detection_run_id);
CREATE INDEX idx_cost_anomaly_runs_tenant_window_prd053
  ON cost_anomaly_detection_runs(tenant_id, environment_id, window_start, ledger_watermark);
CREATE INDEX idx_cost_anomalies_tenant_status_sequence_prd053
  ON cost_anomalies(tenant_id, status, anomaly_sequence DESC);
CREATE INDEX idx_cost_anomalies_rule_time_prd053
  ON cost_anomalies(tenant_id, environment_id, rule_type, window_start DESC, anomaly_sequence DESC);
CREATE INDEX idx_cost_anomalies_scope_time_prd053
  ON cost_anomalies(
    tenant_id, environment_id, rule_type, scope_type, scope_id,
    window_start DESC, anomaly_sequence DESC
  );
CREATE INDEX idx_cost_anomalies_pending_event_prd053
  ON cost_anomalies(
    event_dispatch_status, event_next_attempt_at, event_lease_expires_at, anomaly_sequence
  )
  WHERE event_dispatch_status IN ('pending', 'processing');
CREATE INDEX idx_cost_anomaly_actions_tenant_anomaly_prd053
  ON cost_anomaly_actions(tenant_id, cost_anomaly_id, created_at, cost_anomaly_action_id);
CREATE INDEX idx_temporary_rate_limit_overrides_key_prd053
  ON temporary_rate_limit_overrides(
    tenant_id, virtual_key_id, status, starts_at, expires_at,
    temporary_rate_limit_override_id
  )
  WHERE status='active';

-- Run 必须绑定同租户 Environment，并由当前窗口正式消费证明候选存在；watermark
-- 还必须精确等于 baseline_start 到 window_end 内相关正式 Usage Charge 的实际 MAX。
-- 两道条件共同阻止空窗口 run、跨 owner 序号和不存在的高水位遮蔽迟到重评。
CREATE TRIGGER trg_cost_anomaly_runs_owner_insert_prd053
BEFORE INSERT ON cost_anomaly_detection_runs
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM environments environment
  WHERE environment.tenant_id=NEW.tenant_id
    AND environment.environment_id=NEW.environment_id
)
OR NOT EXISTS (
  SELECT 1 FROM finops_ledger_entries entry
  WHERE entry.tenant_id=NEW.tenant_id
    AND entry.environment_id=NEW.environment_id
    AND entry.entry_type='usage_charge'
    AND entry.direction='debit'
    AND entry.status='posted'
    AND unixepoch(entry.created_at)>=unixepoch(NEW.window_start)
    AND unixepoch(entry.created_at)<unixepoch(NEW.window_end)
)
OR NEW.ledger_watermark IS NOT (
  SELECT MAX(entry.ledger_sequence)
  FROM finops_ledger_entries entry
  WHERE entry.tenant_id=NEW.tenant_id
    AND entry.environment_id=NEW.environment_id
    AND entry.entry_type='usage_charge'
    AND entry.direction='debit'
    AND entry.status='posted'
    AND unixepoch(entry.created_at)>=unixepoch(NEW.baseline_start)
    AND unixepoch(entry.created_at)<unixepoch(NEW.window_end)
)
BEGIN
  SELECT RAISE(ABORT, 'cost_anomaly_run_owner_mismatch');
END;

-- REPLACE 在 recursive_triggers=OFF 时可能绕过 DELETE trigger，因此 INSERT 前覆盖 PK、
-- rowid 和窗口唯一键；隐式 rowid 的 -1 哨兵不参与冲突判断。
CREATE TRIGGER trg_cost_anomaly_runs_conflict_insert_prd053
BEFORE INSERT ON cost_anomaly_detection_runs
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM cost_anomaly_detection_runs current
  WHERE current.detection_run_id=NEW.detection_run_id
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
    OR (
      current.tenant_id=NEW.tenant_id
      AND current.environment_id=NEW.environment_id
      AND current.window_start=NEW.window_start
    )
)
BEGIN
  SELECT RAISE(ABORT, 'cost_anomaly_run_immutable');
END;
CREATE TRIGGER trg_cost_anomaly_runs_rowid_insert_prd053
AFTER INSERT ON cost_anomaly_detection_runs
FOR EACH ROW
WHEN NEW.rowid<0
BEGIN
  SELECT RAISE(ABORT, 'cost_anomaly_run_immutable');
END;

-- 运行行只允许三种完整更新：final 在更高相关水位重新领取、过期 processing 接管，
-- 或当前 claim 在同 revision/watermark 下完成。进入 processing 时必须再次证明水位
-- 等于同租户、同 Environment、同检测区间正式 Usage Charge 的实际 MAX；否则伪造的
-- 高水位会永久遮蔽后续迟到分录。首次 operation request/trace 贯穿全部重评并冻结。
CREATE TRIGGER trg_cost_anomaly_runs_state_update_prd053
BEFORE UPDATE ON cost_anomaly_detection_runs
FOR EACH ROW
WHEN NOT (
  NEW.detection_run_id=OLD.detection_run_id
  AND NEW.tenant_id=OLD.tenant_id
  AND NEW.environment_id=OLD.environment_id
  AND NEW.window_start=OLD.window_start
  AND NEW.window_end=OLD.window_end
  AND NEW.baseline_start=OLD.baseline_start
  AND NEW.baseline_end=OLD.baseline_end
  AND NEW.operation_request_id=OLD.operation_request_id
  AND NEW.operation_trace_id=OLD.operation_trace_id
  AND NEW.created_at=OLD.created_at
  AND unixepoch(NEW.updated_at)>=unixepoch(OLD.updated_at)
  AND (
    (
      OLD.status IN ('completed', 'partial', 'failed')
      AND NEW.status='processing'
      AND NEW.evaluation_revision=OLD.evaluation_revision+1
      AND NEW.ledger_watermark>OLD.ledger_watermark
      AND NEW.ledger_watermark=(
        SELECT MAX(entry.ledger_sequence)
        FROM finops_ledger_entries entry
        WHERE entry.tenant_id=NEW.tenant_id
          AND entry.environment_id=NEW.environment_id
          AND entry.entry_type='usage_charge'
          AND entry.direction='debit'
          AND entry.status='posted'
          AND unixepoch(entry.created_at)>=unixepoch(NEW.baseline_start)
          AND unixepoch(entry.created_at)<unixepoch(NEW.window_end)
      )
      AND NEW.claim_token IS NOT OLD.claim_token
      AND unixepoch(NEW.started_at)>=unixepoch(OLD.updated_at)
    )
    OR (
      OLD.status='processing' AND NEW.status='processing'
      AND NEW.evaluation_revision=OLD.evaluation_revision+1
      AND NEW.ledger_watermark>=OLD.ledger_watermark
      AND NEW.ledger_watermark=(
        SELECT MAX(entry.ledger_sequence)
        FROM finops_ledger_entries entry
        WHERE entry.tenant_id=NEW.tenant_id
          AND entry.environment_id=NEW.environment_id
          AND entry.entry_type='usage_charge'
          AND entry.direction='debit'
          AND entry.status='posted'
          AND unixepoch(entry.created_at)>=unixepoch(NEW.baseline_start)
          AND unixepoch(entry.created_at)<unixepoch(NEW.window_end)
      )
      AND NEW.claim_token IS NOT OLD.claim_token
      AND unixepoch(OLD.lease_expires_at)<=unixepoch(NEW.started_at)
      AND unixepoch(NEW.started_at)>=unixepoch(OLD.started_at)
    )
    OR (
      OLD.status='processing'
      AND NEW.status IN ('completed', 'partial', 'failed')
      AND NEW.evaluation_revision=OLD.evaluation_revision
      AND NEW.ledger_watermark=OLD.ledger_watermark
      AND NEW.operation_request_id=OLD.operation_request_id
      AND NEW.operation_trace_id=OLD.operation_trace_id
      AND NEW.started_at=OLD.started_at
      AND NEW.completed_at IS NOT NULL
    )
  )
)
BEGIN
  SELECT RAISE(ABORT, 'cost_anomaly_run_cas_invalid');
END;
CREATE TRIGGER trg_cost_anomaly_runs_immutable_delete_prd053
BEFORE DELETE ON cost_anomaly_detection_runs
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'cost_anomaly_run_immutable');
END;


-- 异常证据必须绑定仍处于 processing 的同窗口 run，并由该 run watermark 内的正式
-- CNY Usage Charge 证明代表请求、scope、Project/Application 快照和 source sequence。
CREATE TRIGGER trg_cost_anomalies_owner_insert_prd053
BEFORE INSERT ON cost_anomalies
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM cost_anomaly_detection_runs run
  WHERE run.tenant_id=NEW.tenant_id
    AND run.environment_id=NEW.environment_id
    AND run.detection_run_id=NEW.detection_run_id
    AND run.status='processing'
    AND run.window_start=NEW.window_start
    AND run.window_end=NEW.window_end
    AND run.baseline_start=NEW.baseline_start
    AND run.baseline_end=NEW.baseline_end
    AND NEW.source_ledger_sequence_max<=run.ledger_watermark
)
OR NOT EXISTS (
  SELECT 1 FROM finops_ledger_entries entry
  WHERE entry.ledger_sequence=NEW.source_ledger_sequence_min
    AND entry.tenant_id=NEW.tenant_id
    AND entry.environment_id=NEW.environment_id
    AND entry.entry_type='usage_charge' AND entry.direction='debit' AND entry.status='posted'
    AND entry.created_at>=NEW.baseline_start AND entry.created_at<NEW.window_end
    AND (NEW.scope_type='tenant' OR entry.virtual_key_id=NEW.virtual_key_id)
)
OR NOT EXISTS (
  SELECT 1 FROM finops_ledger_entries entry
  WHERE entry.ledger_sequence=NEW.source_ledger_sequence_max
    AND entry.tenant_id=NEW.tenant_id
    AND entry.environment_id=NEW.environment_id
    AND entry.entry_type='usage_charge' AND entry.direction='debit' AND entry.status='posted'
    AND entry.created_at>=NEW.baseline_start AND entry.created_at<NEW.window_end
    AND (NEW.scope_type='tenant' OR entry.virtual_key_id=NEW.virtual_key_id)
)
OR NOT EXISTS (
  SELECT 1 FROM finops_ledger_entries entry
  WHERE entry.tenant_id=NEW.tenant_id
    AND entry.environment_id=NEW.environment_id
    AND entry.source_request_id=NEW.representative_request_id
    AND entry.source_trace_id=NEW.representative_trace_id
    AND entry.entry_type='usage_charge' AND entry.direction='debit' AND entry.status='posted'
    AND entry.cny_normalization_status='normalized'
    AND entry.price_evidence_status IN ('priced', 'legacy_amount_only')
    AND typeof(entry.normalized_cny_micros)='integer'
    AND entry.created_at>=NEW.window_start AND entry.created_at<NEW.window_end
    AND entry.ledger_sequence BETWEEN NEW.source_ledger_sequence_min
      AND NEW.source_ledger_sequence_max
    AND (NEW.scope_type='tenant' OR entry.virtual_key_id=NEW.virtual_key_id)
    AND entry.project_id IS NEW.project_id_snapshot
    AND entry.application_id IS NEW.application_id_snapshot
)
OR (
  NEW.top_virtual_key_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM finops_ledger_entries entry
    WHERE entry.tenant_id=NEW.tenant_id
      AND entry.environment_id=NEW.environment_id
      AND entry.virtual_key_id=NEW.top_virtual_key_id
      AND entry.entry_type='usage_charge' AND entry.direction='debit' AND entry.status='posted'
      AND entry.created_at>=NEW.window_start AND entry.created_at<NEW.window_end
      AND entry.ledger_sequence<=NEW.source_ledger_sequence_max
  )
)
BEGIN
  SELECT RAISE(ABORT, 'cost_anomaly_owner_mismatch');
END;

CREATE TRIGGER trg_cost_anomalies_conflict_insert_prd053
BEFORE INSERT ON cost_anomalies
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM cost_anomalies current
  WHERE current.cost_anomaly_id=NEW.cost_anomaly_id
    OR current.event_id=NEW.event_id
    OR (NEW.anomaly_sequence!=-1 AND current.anomaly_sequence=NEW.anomaly_sequence)
    OR (
      current.tenant_id=NEW.tenant_id
      AND current.environment_id=NEW.environment_id
      AND current.rule_type=NEW.rule_type
      AND current.scope_type=NEW.scope_type
      AND current.scope_id=NEW.scope_id
      AND current.window_start=NEW.window_start
    )
)
BEGIN
  SELECT RAISE(ABORT, 'cost_anomaly_immutable');
END;

-- 证据字段全集保持不变；业务状态变化与 outbox 状态变化必须互斥，避免一个 UPDATE
-- 同时伪造处置和投递结果。outbox takeover 只能在旧 lease 到期后增加 source attempt。
CREATE TRIGGER trg_cost_anomalies_state_update_prd053
BEFORE UPDATE ON cost_anomalies
FOR EACH ROW
WHEN NOT (
  NEW.anomaly_sequence=OLD.anomaly_sequence
  AND NEW.cost_anomaly_id=OLD.cost_anomaly_id
  AND NEW.event_id=OLD.event_id
  AND NEW.tenant_id=OLD.tenant_id
  AND NEW.environment_id=OLD.environment_id
  AND NEW.detection_run_id=OLD.detection_run_id
  AND NEW.rule_type=OLD.rule_type
  AND NEW.severity=OLD.severity
  AND NEW.scope_type=OLD.scope_type
  AND NEW.scope_id=OLD.scope_id
  AND NEW.virtual_key_id IS OLD.virtual_key_id
  AND NEW.top_virtual_key_id IS OLD.top_virtual_key_id
  AND NEW.project_id_snapshot IS OLD.project_id_snapshot
  AND NEW.application_id_snapshot IS OLD.application_id_snapshot
  AND NEW.window_start=OLD.window_start
  AND NEW.window_end=OLD.window_end
  AND NEW.baseline_start=OLD.baseline_start
  AND NEW.baseline_end=OLD.baseline_end
  AND NEW.current_cost_micros=OLD.current_cost_micros
  AND NEW.baseline_total_micros=OLD.baseline_total_micros
  AND NEW.baseline_bucket_count=OLD.baseline_bucket_count
  AND NEW.observed_ratio_millis IS OLD.observed_ratio_millis
  AND NEW.threshold_multiplier_millis=OLD.threshold_multiplier_millis
  AND NEW.minimum_cost_micros=OLD.minimum_cost_micros
  AND NEW.cooldown_minutes=OLD.cooldown_minutes
  AND NEW.source_entry_count=OLD.source_entry_count
  AND NEW.estimated_entry_count=OLD.estimated_entry_count
  AND NEW.legacy_entry_count=OLD.legacy_entry_count
  AND NEW.affected_virtual_key_count=OLD.affected_virtual_key_count
  AND NEW.representative_request_id=OLD.representative_request_id
  AND NEW.representative_trace_id=OLD.representative_trace_id
  AND NEW.source_ledger_sequence_min=OLD.source_ledger_sequence_min
  AND NEW.source_ledger_sequence_max=OLD.source_ledger_sequence_max
  AND NEW.recommended_action=OLD.recommended_action
  AND NEW.detection_version=OLD.detection_version
  AND NEW.detected_at=OLD.detected_at
  AND NEW.mitigation_eligible_until=OLD.mitigation_eligible_until
  AND NEW.created_at=OLD.created_at
  AND unixepoch(NEW.updated_at)>unixepoch(OLD.updated_at)
  AND (
    (
      NEW.event_dispatch_status=OLD.event_dispatch_status
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts
      AND NEW.event_next_attempt_at IS OLD.event_next_attempt_at
      AND NEW.event_dispatched_at IS OLD.event_dispatched_at
      AND NEW.event_claim_token IS OLD.event_claim_token
      AND NEW.event_lease_owner_id IS OLD.event_lease_owner_id
      AND NEW.event_lease_expires_at IS OLD.event_lease_expires_at
      AND NEW.event_last_error_code IS OLD.event_last_error_code
      AND (
        (OLD.status='open' AND NEW.status IN ('acknowledged', 'dismissed', 'mitigated'))
        OR (OLD.status='acknowledged' AND NEW.status IN ('dismissed', 'mitigated'))
      )
    )
    OR (
      NEW.status=OLD.status
      AND (
        (OLD.event_dispatch_status='pending' AND NEW.event_dispatch_status='processing'
          AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts+1
          AND unixepoch(OLD.event_next_attempt_at)<=unixepoch(NEW.updated_at)
          AND NEW.event_claim_token IS NOT OLD.event_claim_token)
        OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='processing'
          AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts+1
          AND unixepoch(OLD.event_lease_expires_at)<=unixepoch(NEW.updated_at)
          AND NEW.event_claim_token IS NOT OLD.event_claim_token)
        OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='pending'
          AND OLD.event_dispatch_attempts<10
          AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts
          AND NEW.event_last_error_code IS NOT NULL
          AND unixepoch(NEW.event_next_attempt_at)>unixepoch(NEW.updated_at))
        OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='dispatched'
          AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts
          AND NEW.event_dispatched_at IS NOT NULL)
        OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='failed'
          AND NEW.event_dispatch_attempts=10
          AND NEW.event_last_error_code IS NOT NULL)
      )
    )
  )
)
BEGIN
  SELECT RAISE(ABORT, 'cost_anomaly_state_invalid');
END;
CREATE TRIGGER trg_cost_anomalies_immutable_delete_prd053
BEFORE DELETE ON cost_anomalies
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'cost_anomaly_immutable');
END;

-- Action 插入时复核当前 anomaly 状态、actor、Key 当前归属和处置有效期。apply 的
-- effective_after 必须逐维等于 before 与提交限制的最小值，并至少有一维真正收紧。
CREATE TRIGGER trg_cost_anomaly_actions_owner_insert_prd053
BEFORE INSERT ON cost_anomaly_actions
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM users actor
  WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.actor_user_id
)
OR NOT EXISTS (
  SELECT 1 FROM cost_anomalies anomaly
  WHERE anomaly.tenant_id=NEW.tenant_id
    AND anomaly.cost_anomaly_id=NEW.cost_anomaly_id
    AND anomaly.status=NEW.status_before
)
OR (
  NEW.action_type='apply_temporary_rate_limit'
  AND NOT EXISTS (
    SELECT 1
    FROM cost_anomalies anomaly
    JOIN virtual_keys key
      ON key.tenant_id=anomaly.tenant_id
     AND key.virtual_key_id=anomaly.virtual_key_id
    WHERE anomaly.tenant_id=NEW.tenant_id
      AND anomaly.cost_anomaly_id=NEW.cost_anomaly_id
      AND anomaly.scope_type='virtual_key'
      AND key.status='active'
      AND key.environment_id=anomaly.environment_id
      AND unixepoch(NEW.created_at)<=unixepoch(anomaly.mitigation_eligible_until)
      AND NOT EXISTS (
        SELECT 1 FROM temporary_rate_limit_overrides existing
        WHERE existing.tenant_id=anomaly.tenant_id
          AND existing.source_id=anomaly.cost_anomaly_id
      )
  )
)
OR (
  NEW.action_type='apply_temporary_rate_limit'
  AND NOT (
    (NEW.rpm_limit IS NULL AND NEW.effective_rpm_after IS NEW.effective_rpm_before)
    OR (NEW.rpm_limit IS NOT NULL
      AND NEW.effective_rpm_after=CASE
        WHEN NEW.effective_rpm_before IS NULL OR NEW.rpm_limit<NEW.effective_rpm_before
          THEN NEW.rpm_limit ELSE NEW.effective_rpm_before END)
  )
)
OR (
  NEW.action_type='apply_temporary_rate_limit'
  AND NOT (
    (NEW.tpm_limit IS NULL AND NEW.effective_tpm_after IS NEW.effective_tpm_before)
    OR (NEW.tpm_limit IS NOT NULL
      AND NEW.effective_tpm_after=CASE
        WHEN NEW.effective_tpm_before IS NULL OR NEW.tpm_limit<NEW.effective_tpm_before
          THEN NEW.tpm_limit ELSE NEW.effective_tpm_before END)
  )
)
OR (
  NEW.action_type='apply_temporary_rate_limit'
  AND NOT (
    (NEW.concurrency_limit IS NULL
      AND NEW.effective_concurrency_after IS NEW.effective_concurrency_before)
    OR (NEW.concurrency_limit IS NOT NULL
      AND NEW.effective_concurrency_after=CASE
        WHEN NEW.effective_concurrency_before IS NULL
          OR NEW.concurrency_limit<NEW.effective_concurrency_before
          THEN NEW.concurrency_limit ELSE NEW.effective_concurrency_before END)
  )
)
OR (
  NEW.action_type='apply_temporary_rate_limit'
  AND NOT (
    (NEW.effective_rpm_before IS NULL AND NEW.effective_rpm_after IS NOT NULL)
    OR (NEW.effective_rpm_before IS NOT NULL
      AND NEW.effective_rpm_after<NEW.effective_rpm_before)
    OR (NEW.effective_tpm_before IS NULL AND NEW.effective_tpm_after IS NOT NULL)
    OR (NEW.effective_tpm_before IS NOT NULL
      AND NEW.effective_tpm_after<NEW.effective_tpm_before)
    OR (NEW.effective_concurrency_before IS NULL
      AND NEW.effective_concurrency_after IS NOT NULL)
    OR (NEW.effective_concurrency_before IS NOT NULL
      AND NEW.effective_concurrency_after<NEW.effective_concurrency_before)
  )
)
OR (
  NEW.action_type='release_temporary_rate_limit'
  AND NOT EXISTS (
    SELECT 1 FROM temporary_rate_limit_overrides override
    WHERE override.tenant_id=NEW.tenant_id
      AND override.temporary_rate_limit_override_id=NEW.target_override_id
      AND override.source_id=NEW.cost_anomaly_id
      AND override.status='active'
      AND unixepoch(NEW.created_at)<unixepoch(override.expires_at)
  )
)
OR (
  NEW.action_type='release_temporary_rate_limit'
  AND (
    (NEW.effective_rpm_before IS NULL AND NEW.effective_rpm_after IS NOT NULL)
    OR (NEW.effective_rpm_before IS NOT NULL AND NEW.effective_rpm_after IS NOT NULL
      AND NEW.effective_rpm_after<NEW.effective_rpm_before)
    OR (NEW.effective_tpm_before IS NULL AND NEW.effective_tpm_after IS NOT NULL)
    OR (NEW.effective_tpm_before IS NOT NULL AND NEW.effective_tpm_after IS NOT NULL
      AND NEW.effective_tpm_after<NEW.effective_tpm_before)
    OR (NEW.effective_concurrency_before IS NULL
      AND NEW.effective_concurrency_after IS NOT NULL)
    OR (NEW.effective_concurrency_before IS NOT NULL
      AND NEW.effective_concurrency_after IS NOT NULL
      AND NEW.effective_concurrency_after<NEW.effective_concurrency_before)
  )
)
BEGIN
  SELECT RAISE(ABORT, 'cost_anomaly_action_owner_mismatch');
END;

CREATE TRIGGER trg_cost_anomaly_actions_conflict_insert_prd053
BEFORE INSERT ON cost_anomaly_actions
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM cost_anomaly_actions current
  WHERE current.cost_anomaly_action_id=NEW.cost_anomaly_action_id
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
    OR (
      current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id
      AND current.idempotency_key_sha256=NEW.idempotency_key_sha256
    )
)
BEGIN
  SELECT RAISE(ABORT, 'cost_anomaly_action_immutable');
END;
CREATE TRIGGER trg_cost_anomaly_actions_rowid_insert_prd053
AFTER INSERT ON cost_anomaly_actions
FOR EACH ROW
WHEN NEW.rowid<0
BEGIN
  SELECT RAISE(ABORT, 'cost_anomaly_action_immutable');
END;
CREATE TRIGGER trg_cost_anomaly_actions_immutable_update_prd053
BEFORE UPDATE ON cost_anomaly_actions
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'cost_anomaly_action_immutable');
END;
CREATE TRIGGER trg_cost_anomaly_actions_immutable_delete_prd053
BEFORE DELETE ON cost_anomaly_actions
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'cost_anomaly_action_immutable');
END;

-- 新覆盖必须与 Key 当前归属、source anomaly、apply action 和 duration/limit 完全一致。
-- anomaly status 允许在同事务内先或后切换为 mitigated，但 action.status_before 已冻结原状态。
CREATE TRIGGER trg_temporary_rate_limit_overrides_owner_insert_prd053
BEFORE INSERT ON temporary_rate_limit_overrides
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1
  FROM cost_anomalies anomaly
  JOIN virtual_keys key
    ON key.tenant_id=anomaly.tenant_id
   AND key.virtual_key_id=anomaly.virtual_key_id
  JOIN cost_anomaly_actions action
    ON action.tenant_id=anomaly.tenant_id
   AND action.cost_anomaly_id=anomaly.cost_anomaly_id
   AND action.cost_anomaly_action_id=NEW.source_action_id
  WHERE anomaly.tenant_id=NEW.tenant_id
    AND anomaly.cost_anomaly_id=NEW.source_id
    AND anomaly.scope_type='virtual_key'
    AND anomaly.virtual_key_id=NEW.virtual_key_id
    AND anomaly.status IN ('open', 'acknowledged', 'mitigated')
    AND key.status='active'
    AND key.environment_id=anomaly.environment_id
    AND action.action_type='apply_temporary_rate_limit'
    AND action.status_after='mitigated'
    AND action.actor_user_id=NEW.created_by
    AND action.rpm_limit IS NEW.rpm_limit
    AND action.tpm_limit IS NEW.tpm_limit
    AND action.concurrency_limit IS NEW.concurrency_limit
    AND action.operation_request_id=NEW.operation_request_id
    AND action.operation_trace_id=NEW.operation_trace_id
    AND action.created_at=NEW.starts_at
    AND NEW.created_at=NEW.starts_at
    AND action.duration_minutes=(unixepoch(NEW.expires_at)-unixepoch(NEW.starts_at))/60
)
BEGIN
  SELECT RAISE(ABORT, 'temporary_rate_limit_override_owner_mismatch');
END;

CREATE TRIGGER trg_temporary_rate_limit_overrides_conflict_insert_prd053
BEFORE INSERT ON temporary_rate_limit_overrides
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM temporary_rate_limit_overrides current
  WHERE current.temporary_rate_limit_override_id=NEW.temporary_rate_limit_override_id
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
    OR (current.tenant_id=NEW.tenant_id AND current.source_id=NEW.source_id)
    OR (current.tenant_id=NEW.tenant_id AND current.source_action_id=NEW.source_action_id)
    OR (NEW.release_action_id IS NOT NULL
      AND current.tenant_id=NEW.tenant_id
      AND current.release_action_id=NEW.release_action_id)
)
BEGIN
  SELECT RAISE(ABORT, 'temporary_rate_limit_override_immutable');
END;
CREATE TRIGGER trg_temporary_rate_limit_overrides_rowid_insert_prd053
AFTER INSERT ON temporary_rate_limit_overrides
FOR EACH ROW
WHEN NEW.rowid<0
BEGIN
  SELECT RAISE(ABORT, 'temporary_rate_limit_override_immutable');
END;

-- active 覆盖只允许在 expires_at 前释放一次；limits、source、owner、创建操作和到期时点
-- 全部保持不变，release action 必须是同 tenant/anomaly/override 的当前操作者动作。
CREATE TRIGGER trg_temporary_rate_limit_overrides_release_update_prd053
BEFORE UPDATE ON temporary_rate_limit_overrides
FOR EACH ROW
WHEN NOT (
  OLD.status='active' AND NEW.status='released'
  AND NEW.temporary_rate_limit_override_id=OLD.temporary_rate_limit_override_id
  AND NEW.tenant_id=OLD.tenant_id
  AND NEW.virtual_key_id=OLD.virtual_key_id
  AND NEW.source_type=OLD.source_type
  AND NEW.source_id=OLD.source_id
  AND NEW.source_action_id=OLD.source_action_id
  AND NEW.rpm_limit IS OLD.rpm_limit
  AND NEW.tpm_limit IS OLD.tpm_limit
  AND NEW.concurrency_limit IS OLD.concurrency_limit
  AND NEW.starts_at=OLD.starts_at
  AND NEW.expires_at=OLD.expires_at
  AND NEW.created_by=OLD.created_by
  AND NEW.operation_request_id=OLD.operation_request_id
  AND NEW.operation_trace_id=OLD.operation_trace_id
  AND NEW.created_at=OLD.created_at
  AND NEW.updated_at=NEW.released_at
  AND unixepoch(NEW.released_at)<unixepoch(OLD.expires_at)
  AND EXISTS (
    SELECT 1 FROM cost_anomaly_actions action
    WHERE action.tenant_id=OLD.tenant_id
      AND action.cost_anomaly_id=OLD.source_id
      AND action.cost_anomaly_action_id=NEW.release_action_id
      AND action.actor_user_id=NEW.released_by
      AND action.action_type='release_temporary_rate_limit'
      AND action.target_override_id=OLD.temporary_rate_limit_override_id
      AND action.created_at=NEW.released_at
  )
)
BEGIN
  SELECT RAISE(ABORT, 'temporary_rate_limit_override_release_invalid');
END;
CREATE TRIGGER trg_temporary_rate_limit_overrides_immutable_delete_prd053
BEFORE DELETE ON temporary_rate_limit_overrides
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'temporary_rate_limit_override_immutable');
END;

-- Audit 只扩展四类成本异常处置；旧 17 列、rowid、NULL 和 JSON 正文逐值保真。
CREATE TABLE audit_logs_prd053_new (
  audit_log_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  actor_email TEXT NOT NULL,
  actor_role TEXT NOT NULL CHECK (actor_role IN ('admin', 'developer', 'viewer')),
  action TEXT NOT NULL CHECK (
    action IN (
      'provider.created', 'provider.updated',
      'provider_key.created', 'provider_key.updated',
      'virtual_key.created', 'virtual_key.updated',
      'budget_rule.created', 'budget_rule.updated',
      'dlp_rule.created', 'dlp_rule.updated',
      'webhook_delivery.replayed', 'webhook_endpoint.secret_rotated',
      'usage_export.requested', 'usage_export.downloaded',
      'policy.created', 'policy.updated',
      'policy_snapshot.published', 'policy_snapshot.rolled_back',
      'policy.dry_run_executed',
      'routing_policy.created', 'routing_policy.updated',
      'model_quality_score.updated',
      'cache_affinity.created', 'cache_affinity.updated',
      'finops.adjustment_created',
      'cost_anomaly.acknowledged', 'cost_anomaly.dismissed',
      'cost_anomaly.temporary_rate_limit_applied',
      'cost_anomaly.temporary_rate_limit_released'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'usage_export_job', 'policy',
      'policy_snapshot', 'policy_explanation', 'routing_policy',
      'model_quality_score', 'cache_affinity', 'finops_adjustment', 'cost_anomaly'
    )
  ),
  object_id TEXT NOT NULL,
  object_name TEXT,
  before_summary_json TEXT NOT NULL CHECK (
    json_valid(before_summary_json) AND json_type(before_summary_json)='object'
  ),
  after_summary_json TEXT NOT NULL CHECK (
    json_valid(after_summary_json) AND json_type(after_summary_json)='object'
  ),
  changed_fields_json TEXT NOT NULL CHECK (
    json_valid(changed_fields_json) AND json_type(changed_fields_json)='array'
  ),
  client_ip TEXT,
  user_agent TEXT,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);
-- 显式复制 rowid 与 17 个历史字段，确保 Audit 枚举扩展不改写既有审计事实。
INSERT INTO audit_logs_prd053_new(
  rowid, audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
  action, object_type, object_id, object_name, before_summary_json,
  after_summary_json, changed_fields_json, client_ip, user_agent,
  request_id, trace_id, created_at
)
SELECT
  rowid, audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
  action, object_type, object_id, object_name, before_summary_json,
  after_summary_json, changed_fields_json, client_ip, user_agent,
  request_id, trace_id, created_at
FROM audit_logs;

CREATE TEMP TABLE prd053_audit_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  null_mismatch_count INTEGER NOT NULL,
  row_difference_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd053_audit_copy_guard
BEFORE INSERT ON prd053_audit_copy_guard
FOR EACH ROW
WHEN NEW.source_count!=NEW.copied_count
  OR NEW.null_mismatch_count!=0
  OR NEW.row_difference_count!=0
BEGIN
  SELECT RAISE(ABORT, 'prd053_audit_copy_failed');
END;
-- 对比计数、NULL 分布和双向行差，只有逐值保真时才允许切换正式 Audit 表。
INSERT INTO prd053_audit_copy_guard(
  source_count, copied_count, null_mismatch_count, row_difference_count
)
SELECT
  (SELECT COUNT(*) FROM audit_logs),
  (SELECT COUNT(*) FROM audit_logs_prd053_new),
  CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd053_new WHERE object_name IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd053_new WHERE client_ip IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd053_new WHERE user_agent IS NULL) THEN 0 ELSE 1 END,
  (SELECT COUNT(*) FROM (
    SELECT rowid, * FROM audit_logs
    EXCEPT SELECT rowid, * FROM audit_logs_prd053_new
  ))
  + (SELECT COUNT(*) FROM (
    SELECT rowid, * FROM audit_logs_prd053_new
    EXCEPT SELECT rowid, * FROM audit_logs
  ));
DROP TRIGGER trg_prd053_audit_copy_guard;
DROP TABLE prd053_audit_copy_guard;
DROP TABLE audit_logs;
-- 保真门禁通过后再切换正式名称，避免任何读取方看到半套审计结构。
ALTER TABLE audit_logs_prd053_new RENAME TO audit_logs;

CREATE INDEX idx_audit_logs_tenant_time
  ON audit_logs(tenant_id, created_at DESC, audit_log_id DESC);
CREATE INDEX idx_audit_logs_tenant_actor_time
  ON audit_logs(tenant_id, actor_user_id, created_at DESC);
CREATE INDEX idx_audit_logs_tenant_object_time
  ON audit_logs(tenant_id, object_type, object_id, created_at DESC);
CREATE INDEX idx_audit_logs_tenant_action_time
  ON audit_logs(tenant_id, action, created_at DESC);
CREATE INDEX idx_audit_logs_tenant_request
  ON audit_logs(tenant_id, request_id);
CREATE UNIQUE INDEX idx_audit_logs_dry_run_request_unique
  ON audit_logs(tenant_id, actor_user_id, request_id)
  WHERE action='policy.dry_run_executed';
CREATE UNIQUE INDEX idx_audit_logs_model_quality_request_unique
  ON audit_logs(tenant_id, actor_user_id, request_id)
  WHERE action='model_quality_score.updated';
CREATE UNIQUE INDEX idx_audit_logs_finops_adjustment_unique
  ON audit_logs(tenant_id, object_id)
  WHERE action='finops.adjustment_created';

-- Audit owner 同时复核用户、FinOps Adjustment 和成本异常 action；服务层即使遗漏
-- tenant 条件，也不能把处置审计挂到其他租户、其他 actor 或其他 operation。
CREATE TRIGGER trg_audit_logs_owner_insert
BEFORE INSERT ON audit_logs
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM users user
  WHERE user.user_id=NEW.actor_user_id AND user.tenant_id=NEW.tenant_id
)
OR (
  NEW.action='finops.adjustment_created'
  AND (
    NEW.object_type!='finops_adjustment'
    OR NOT EXISTS (
      SELECT 1 FROM finops_adjustments adjustment
      WHERE adjustment.finops_adjustment_id=NEW.object_id
        AND adjustment.tenant_id=NEW.tenant_id
        AND adjustment.actor_user_id=NEW.actor_user_id
    )
  )
)
OR (NEW.object_type='finops_adjustment' AND NEW.action!='finops.adjustment_created')
OR (
  NEW.action IN (
    'cost_anomaly.acknowledged', 'cost_anomaly.dismissed',
    'cost_anomaly.temporary_rate_limit_applied',
    'cost_anomaly.temporary_rate_limit_released'
  )
  AND (
    NEW.object_type!='cost_anomaly'
    OR NOT EXISTS (
      SELECT 1
      FROM cost_anomaly_actions action
      JOIN cost_anomalies anomaly
        ON anomaly.tenant_id=action.tenant_id
       AND anomaly.cost_anomaly_id=action.cost_anomaly_id
      WHERE action.tenant_id=NEW.tenant_id
        AND action.cost_anomaly_id=NEW.object_id
        AND action.actor_user_id=NEW.actor_user_id
        AND action.operation_request_id=NEW.request_id
        AND action.operation_trace_id=NEW.trace_id
        AND CASE action.action_type
          WHEN 'acknowledge' THEN 'cost_anomaly.acknowledged'
          WHEN 'dismiss' THEN 'cost_anomaly.dismissed'
          WHEN 'apply_temporary_rate_limit'
            THEN 'cost_anomaly.temporary_rate_limit_applied'
          WHEN 'release_temporary_rate_limit'
            THEN 'cost_anomaly.temporary_rate_limit_released'
        END=NEW.action
    )
  )
)
OR (
  NEW.object_type='cost_anomaly'
  AND NEW.action NOT IN (
    'cost_anomaly.acknowledged', 'cost_anomaly.dismissed',
    'cost_anomaly.temporary_rate_limit_applied',
    'cost_anomaly.temporary_rate_limit_released'
  )
)
BEGIN
  SELECT RAISE(ABORT, 'audit_log_owner_mismatch');
END;
CREATE TRIGGER trg_audit_logs_immutable_update
BEFORE UPDATE ON audit_logs
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'audit_log_immutable');
END;
CREATE TRIGGER trg_audit_logs_immutable_delete
BEFORE DELETE ON audit_logs
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'audit_log_immutable');
END;
CREATE TRIGGER trg_audit_logs_prd050_conflict_insert
BEFORE INSERT ON audit_logs
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM audit_logs current
  WHERE current.audit_log_id=NEW.audit_log_id
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
    OR (
      NEW.action IN ('policy.dry_run_executed', 'model_quality_score.updated')
      AND current.action=NEW.action
      AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id
      AND current.request_id=NEW.request_id
    )
    OR (
      NEW.action='finops.adjustment_created'
      AND current.action='finops.adjustment_created'
      AND current.tenant_id=NEW.tenant_id
      AND current.object_id=NEW.object_id
    )
    OR (
      NEW.action IN (
        'cost_anomaly.acknowledged', 'cost_anomaly.dismissed',
        'cost_anomaly.temporary_rate_limit_applied',
        'cost_anomaly.temporary_rate_limit_released'
      )
      AND current.action=NEW.action
      AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id
      AND current.request_id=NEW.request_id
    )
)
BEGIN
  SELECT RAISE(ABORT, 'audit_log_immutable');
END;
CREATE TRIGGER trg_audit_logs_prd050_rowid_insert
AFTER INSERT ON audit_logs
FOR EACH ROW
WHEN NEW.rowid<0
BEGIN
  SELECT RAISE(ABORT, 'audit_log_immutable');
END;

-- 提交前核对四表、索引、全部状态机 trigger、Audit 枚举及数据库完整性。任何一项
-- 缺失都会回滚整个 0043，不能把半套检测或半套处置结构登记为 applied。
CREATE TEMP TABLE prd053_up_integrity_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd053_up_integrity_guard
BEFORE INSERT ON prd053_up_integrity_guard
FOR EACH ROW
WHEN NEW.issue_count!=0
BEGIN
  SELECT RAISE(ABORT, 'prd053_up_integrity_failed');
END;
-- 汇总目标表、索引、触发器、Audit 枚举和 SQLite 完整性，异常时回滚整个 0043。
INSERT INTO prd053_up_integrity_guard(issue_count)
SELECT
  CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='table' AND name IN (
      'cost_anomaly_detection_runs', 'cost_anomalies',
      'cost_anomaly_actions', 'temporary_rate_limit_overrides', 'audit_logs'
    )
  )=5 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='index' AND name IN (
      'idx_cost_anomaly_runs_status_window_prd053',
      'idx_cost_anomaly_runs_tenant_window_prd053',
      'idx_cost_anomalies_tenant_status_sequence_prd053',
      'idx_cost_anomalies_rule_time_prd053',
      'idx_cost_anomalies_scope_time_prd053',
      'idx_cost_anomalies_pending_event_prd053',
      'idx_cost_anomaly_actions_tenant_anomaly_prd053',
      'idx_temporary_rate_limit_overrides_key_prd053'
    )
  )=8 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='trigger' AND name IN (
      'trg_cost_anomaly_runs_owner_insert_prd053',
      'trg_cost_anomaly_runs_conflict_insert_prd053',
      'trg_cost_anomaly_runs_rowid_insert_prd053',
      'trg_cost_anomaly_runs_state_update_prd053',
      'trg_cost_anomaly_runs_immutable_delete_prd053',
      'trg_cost_anomalies_owner_insert_prd053',
      'trg_cost_anomalies_conflict_insert_prd053',
      'trg_cost_anomalies_state_update_prd053',
      'trg_cost_anomalies_immutable_delete_prd053',
      'trg_cost_anomaly_actions_owner_insert_prd053',
      'trg_cost_anomaly_actions_conflict_insert_prd053',
      'trg_cost_anomaly_actions_rowid_insert_prd053',
      'trg_cost_anomaly_actions_immutable_update_prd053',
      'trg_cost_anomaly_actions_immutable_delete_prd053',
      'trg_temporary_rate_limit_overrides_owner_insert_prd053',
      'trg_temporary_rate_limit_overrides_conflict_insert_prd053',
      'trg_temporary_rate_limit_overrides_rowid_insert_prd053',
      'trg_temporary_rate_limit_overrides_release_update_prd053',
      'trg_temporary_rate_limit_overrides_immutable_delete_prd053',
      'trg_audit_logs_owner_insert', 'trg_audit_logs_immutable_update',
      'trg_audit_logs_immutable_delete', 'trg_audit_logs_prd050_conflict_insert',
      'trg_audit_logs_prd050_rowid_insert'
    )
  )=24 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='index' AND tbl_name='audit_logs' AND sql IS NOT NULL
  )=8 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='trigger' AND tbl_name='audit_logs'
  )=5 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT sql LIKE '%cost_anomaly.temporary_rate_limit_released%'
      AND sql LIKE '%cost_anomaly%' FROM sqlite_master
    WHERE type='table' AND name='audit_logs'
  ) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + CASE WHEN (SELECT integrity_check FROM pragma_integrity_check)='ok' THEN 0 ELSE 1 END;
DROP TRIGGER trg_prd053_up_integrity_guard;
DROP TABLE prd053_up_integrity_guard;

COMMIT;
