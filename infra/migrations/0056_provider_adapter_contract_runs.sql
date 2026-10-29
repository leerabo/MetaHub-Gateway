-- PRD-064：Provider Adapter 合同运行、用例与失败事件 Source Outbox。
-- 所有业务事实显式携带 tenant_id，只保存固定状态、计数和不可逆 hash；不保存 Endpoint、
-- Header、Provider Key、Token、Prompt、Response、上游错误正文或合同断言正文。

CREATE TABLE adapter_contract_runs (
  adapter_contract_run_id TEXT PRIMARY KEY CHECK (
    length(adapter_contract_run_id)=36
    AND substr(adapter_contract_run_id,1,4)='acr_'
    AND substr(adapter_contract_run_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  source_request_id TEXT NOT NULL CHECK (
    length(source_request_id) BETWEEN 1 AND 160
    AND source_request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  source_trace_id TEXT NOT NULL CHECK (
    length(source_trace_id) BETWEEN 1 AND 160
    AND source_trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  schema_version TEXT NOT NULL CHECK (schema_version='adapter-contract-report-v1'),
  catalog_version TEXT NOT NULL CHECK (
    length(catalog_version) BETWEEN 1 AND 80
    AND catalog_version NOT GLOB '*[^A-Za-z0-9_.-]*'
  ),
  catalog_hash TEXT NOT NULL CHECK (
    length(catalog_hash)=64 AND catalog_hash NOT GLOB '*[^0-9a-f]*'
  ),
  runner_version TEXT NOT NULL CHECK (
    length(runner_version) BETWEEN 5 AND 32
    AND runner_version NOT GLOB '*[^0-9.]*'
  ),
  suite_kind TEXT NOT NULL CHECK (suite_kind IN ('merge_gate','probe')),
  mode TEXT NOT NULL CHECK (mode IN ('mock','gateway_probe')),
  source_revision_hash TEXT NOT NULL CHECK (
    length(source_revision_hash)=64 AND source_revision_hash NOT GLOB '*[^0-9a-f]*'
  ),
  status TEXT NOT NULL CHECK (status IN ('passed','failed')),
  coverage_complete INTEGER NOT NULL CHECK (coverage_complete IN (0,1)),
  total_cases INTEGER NOT NULL CHECK (
    typeof(total_cases)='integer' AND total_cases BETWEEN 1 AND 2048
  ),
  passed_cases INTEGER NOT NULL CHECK (
    typeof(passed_cases)='integer' AND passed_cases BETWEEN 0 AND 2048
  ),
  failed_cases INTEGER NOT NULL CHECK (
    typeof(failed_cases)='integer' AND failed_cases BETWEEN 0 AND 2048
  ),
  failure_codes_json TEXT NOT NULL CHECK (
    json_valid(failure_codes_json)
    AND json_type(failure_codes_json)='array'
    AND json_array_length(failure_codes_json) BETWEEN 0 AND 16
    AND length(CAST(failure_codes_json AS BLOB))<=1024
  ),
  report_hash TEXT NOT NULL CHECK (
    length(report_hash)=64 AND report_hash NOT GLOB '*[^0-9a-f]*'
  ),
  started_at TEXT NOT NULL CHECK (
    length(started_at)=20 AND substr(started_at,11,1)='T' AND substr(started_at,20,1)='Z'
    AND unixepoch(started_at) IS NOT NULL
  ),
  completed_at TEXT NOT NULL CHECK (
    length(completed_at)=20 AND substr(completed_at,11,1)='T' AND substr(completed_at,20,1)='Z'
    AND unixepoch(completed_at) IS NOT NULL
  ),
  created_at TEXT NOT NULL CHECK (
    length(created_at)=20 AND substr(created_at,11,1)='T' AND substr(created_at,20,1)='Z'
    AND unixepoch(created_at) IS NOT NULL
  ),
  updated_at TEXT NOT NULL CHECK (
    length(updated_at)=20 AND substr(updated_at,11,1)='T' AND substr(updated_at,20,1)='Z'
    AND unixepoch(updated_at) IS NOT NULL
  ),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  UNIQUE (tenant_id,adapter_contract_run_id),
  UNIQUE (tenant_id,source_request_id),
  CHECK (total_cases=passed_cases+failed_cases),
  CHECK (
    (status='passed' AND failed_cases=0 AND json_array_length(failure_codes_json)=0)
    OR (status='failed' AND failed_cases>0 AND json_array_length(failure_codes_json)>0)
  ),
  CHECK (
    (suite_kind='merge_gate' AND mode='mock' AND coverage_complete=1)
    OR (suite_kind='probe' AND mode='gateway_probe' AND coverage_complete=0)
  ),
  CHECK (
    unixepoch(completed_at)>=unixepoch(started_at)
    AND unixepoch(completed_at)<=unixepoch(created_at)+1
    AND updated_at=created_at
  )
);

CREATE INDEX idx_adapter_contract_runs_tenant_completed_prd064
  ON adapter_contract_runs(tenant_id,completed_at DESC,adapter_contract_run_id DESC);
CREATE INDEX idx_adapter_contract_runs_tenant_status_prd064
  ON adapter_contract_runs(tenant_id,status,completed_at DESC,adapter_contract_run_id DESC);

-- SQLite 禁止在列 CHECK 中使用子查询，因此数组元素白名单、去重和 canonical 排序必须在
-- BEFORE INSERT trigger 中完成；服务层校验不能替代这里对绕过 API 的直接写入防护。
CREATE TRIGGER trg_adapter_contract_runs_failure_codes_insert_prd064
BEFORE INSERT ON adapter_contract_runs FOR EACH ROW
WHEN CASE
  WHEN json_valid(NEW.failure_codes_json)
    AND json_type(NEW.failure_codes_json)='array'
    AND json_array_length(NEW.failure_codes_json) BETWEEN 0 AND 16
  THEN EXISTS (
      SELECT 1 FROM json_each(NEW.failure_codes_json) item
      WHERE item.type!='text' OR item.value NOT IN (
        'adapter.contract_fixture_missing','adapter.contract_fixture_unexpected',
        'adapter.contract_fixture_invalid','adapter.contract_coverage_mismatch',
        'adapter.contract_request_failed','adapter.contract_non_stream_failed',
        'adapter.contract_stream_failed','adapter.contract_error_mapping_failed',
        'adapter.contract_usage_failed','adapter.contract_probe_failed'
      )
    )
    OR (SELECT COUNT(*) FROM json_each(NEW.failure_codes_json))!=(
      SELECT COUNT(DISTINCT value) FROM json_each(NEW.failure_codes_json)
    )
    OR NEW.failure_codes_json!=(
      SELECT json_group_array(value)
      FROM (
        SELECT value FROM json_each(NEW.failure_codes_json) ORDER BY value
      )
    )
  ELSE 1
END
BEGIN SELECT RAISE(ABORT,'adapter_contract_failure_codes_invalid'); END;

-- 租户目录 owner 在数据库再次校验，阻止绕过 Control 服务写入不存在租户或非 active 目录。
CREATE TRIGGER trg_adapter_contract_runs_owner_insert_prd064
BEFORE INSERT ON adapter_contract_runs FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1
  FROM tenants tenant
  JOIN provider_adapter_catalog_activations activation
    ON activation.tenant_id=tenant.tenant_id AND activation.status='active'
  WHERE tenant.tenant_id=NEW.tenant_id
    AND activation.catalog_version=NEW.catalog_version
    AND activation.catalog_hash=NEW.catalog_hash
)
BEGIN SELECT RAISE(ABORT,'adapter_contract_run_owner_invalid'); END;

-- run 是 CI 完成事实，写入后不允许改计数、状态、目录、链路或时间。
CREATE TRIGGER trg_adapter_contract_runs_immutable_update_prd064
BEFORE UPDATE ON adapter_contract_runs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'adapter_contract_run_immutable'); END;
CREATE TRIGGER trg_adapter_contract_runs_immutable_delete_prd064
BEFORE DELETE ON adapter_contract_runs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'adapter_contract_run_immutable'); END;

CREATE TABLE adapter_contract_cases (
  adapter_contract_case_id TEXT PRIMARY KEY CHECK (
    length(adapter_contract_case_id)=36
    AND substr(adapter_contract_case_id,1,4)='acc_'
    AND substr(adapter_contract_case_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  adapter_contract_run_id TEXT NOT NULL,
  contract_case_key TEXT NOT NULL CHECK (
    length(contract_case_key) BETWEEN 8 AND 240
    AND contract_case_key NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  spec_id TEXT NOT NULL CHECK (
    length(spec_id) BETWEEN 3 AND 96 AND spec_id NOT GLOB '*[^A-Za-z0-9_.-]*'
  ),
  provider_type TEXT NOT NULL CHECK (
    length(provider_type) BETWEEN 2 AND 64 AND provider_type NOT GLOB '*[^a-z0-9_]*'
  ),
  adapter_name TEXT NOT NULL CHECK (
    adapter_name IN ('openai_compatible','generic_http')
  ),
  adapter_version TEXT NOT NULL CHECK (
    length(adapter_version) BETWEEN 5 AND 32 AND adapter_version NOT GLOB '*[^0-9.]*'
  ),
  case_kind TEXT NOT NULL CHECK (
    case_kind IN ('request','non_stream','stream','error','usage')
  ),
  variant TEXT NOT NULL CHECK (
    variant IN (
      'auth','success','auth_401','rate_limit_429','server_5xx','timeout',
      'standard','missing','invalid'
    )
  ),
  auth_type TEXT CHECK (auth_type IS NULL OR auth_type IN ('none','bearer')),
  status TEXT NOT NULL CHECK (status IN ('passed','failed')),
  failure_code TEXT CHECK (
    failure_code IS NULL OR failure_code IN (
      'adapter.contract_request_failed','adapter.contract_non_stream_failed',
      'adapter.contract_stream_failed','adapter.contract_error_mapping_failed',
      'adapter.contract_usage_failed','adapter.contract_probe_failed'
    )
  ),
  duration_ms INTEGER NOT NULL CHECK (
    typeof(duration_ms)='integer' AND duration_ms BETWEEN 0 AND 300000
  ),
  evidence_hash TEXT NOT NULL CHECK (
    length(evidence_hash)=64 AND evidence_hash NOT GLOB '*[^0-9a-f]*'
  ),
  created_at TEXT NOT NULL CHECK (
    length(created_at)=20 AND substr(created_at,11,1)='T' AND substr(created_at,20,1)='Z'
    AND unixepoch(created_at) IS NOT NULL
  ),
  updated_at TEXT NOT NULL CHECK (
    length(updated_at)=20 AND substr(updated_at,11,1)='T' AND substr(updated_at,20,1)='Z'
    AND unixepoch(updated_at) IS NOT NULL
  ),
  UNIQUE (tenant_id,adapter_contract_run_id,contract_case_key),
  FOREIGN KEY (tenant_id,adapter_contract_run_id)
    REFERENCES adapter_contract_runs(tenant_id,adapter_contract_run_id),
  CHECK (
    (case_kind='request' AND variant='auth' AND auth_type IS NOT NULL)
    OR (case_kind='non_stream' AND variant='success' AND auth_type IS NULL)
    OR (case_kind='stream' AND variant='success' AND auth_type IS NULL)
    OR (case_kind='error' AND variant IN ('auth_401','rate_limit_429','server_5xx','timeout')
        AND auth_type IS NULL)
    OR (case_kind='usage' AND variant IN ('standard','missing','invalid') AND auth_type IS NULL)
  ),
  CHECK (
    (status='passed' AND failure_code IS NULL)
    OR (status='failed' AND failure_code IS NOT NULL)
  ),
  CHECK (updated_at=created_at)
);

CREATE INDEX idx_adapter_contract_cases_tenant_run_prd064
  ON adapter_contract_cases(tenant_id,adapter_contract_run_id,provider_type,contract_case_key);

-- case 必须与同租户父 run 共存，失败码还要匹配 kind；probe 可统一使用 probe_failed。
CREATE TRIGGER trg_adapter_contract_cases_owner_insert_prd064
BEFORE INSERT ON adapter_contract_cases FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM adapter_contract_runs run
  WHERE run.tenant_id=NEW.tenant_id
    AND run.adapter_contract_run_id=NEW.adapter_contract_run_id
    AND unixepoch(NEW.created_at)>=unixepoch(run.completed_at)
    AND (
      NEW.status='passed'
      OR NEW.failure_code=CASE NEW.case_kind
        WHEN 'request' THEN 'adapter.contract_request_failed'
        WHEN 'non_stream' THEN 'adapter.contract_non_stream_failed'
        WHEN 'stream' THEN 'adapter.contract_stream_failed'
        WHEN 'error' THEN 'adapter.contract_error_mapping_failed'
        WHEN 'usage' THEN 'adapter.contract_usage_failed'
      END
      OR (run.suite_kind='probe' AND NEW.failure_code='adapter.contract_probe_failed')
    )
)
BEGIN SELECT RAISE(ABORT,'adapter_contract_case_owner_invalid'); END;

CREATE TRIGGER trg_adapter_contract_cases_immutable_update_prd064
BEFORE UPDATE ON adapter_contract_cases FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'adapter_contract_case_immutable'); END;
CREATE TRIGGER trg_adapter_contract_cases_immutable_delete_prd064
BEFORE DELETE ON adapter_contract_cases FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'adapter_contract_case_immutable'); END;

-- Outbox 不复制 payload；worker 每次都从不可变 run 重建相同事件并交给 Webhook dispatcher。
CREATE TABLE adapter_contract_event_outbox (
  event_id TEXT PRIMARY KEY CHECK (
    length(event_id)=40 AND substr(event_id,1,8)='evt_acr_'
    AND substr(event_id,9) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  adapter_contract_run_id TEXT NOT NULL,
  dispatch_status TEXT NOT NULL CHECK (
    dispatch_status IN ('pending','processing','dispatched','failed')
  ),
  dispatch_attempts INTEGER NOT NULL CHECK (
    typeof(dispatch_attempts)='integer' AND dispatch_attempts BETWEEN 0 AND 10
  ),
  next_attempt_at TEXT,
  dispatched_at TEXT,
  claim_token TEXT,
  lease_owner_id TEXT,
  lease_expires_at TEXT,
  last_error_code TEXT CHECK (
    last_error_code IS NULL OR last_error_code IN (
      'webhook.payload_invalid','webhook.delivery_unavailable',
      'adapter.contract_storage_unavailable'
    )
  ),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK (unixepoch(updated_at) IS NOT NULL),
  UNIQUE (tenant_id,event_id),
  UNIQUE (tenant_id,adapter_contract_run_id),
  FOREIGN KEY (tenant_id,adapter_contract_run_id)
    REFERENCES adapter_contract_runs(tenant_id,adapter_contract_run_id),
  CHECK (event_id='evt_'||adapter_contract_run_id),
  CHECK (
    (dispatch_status='pending' AND dispatch_attempts BETWEEN 0 AND 9
      AND next_attempt_at IS NOT NULL AND unixepoch(next_attempt_at) IS NOT NULL
      AND dispatched_at IS NULL AND claim_token IS NULL
      AND lease_owner_id IS NULL AND lease_expires_at IS NULL)
    OR (dispatch_status='processing' AND dispatch_attempts BETWEEN 1 AND 10
      AND next_attempt_at IS NULL AND dispatched_at IS NULL
      AND claim_token IS NOT NULL AND length(claim_token)=32
      AND claim_token NOT GLOB '*[^0-9a-f]*'
      AND lease_owner_id IS NOT NULL AND length(lease_owner_id) BETWEEN 1 AND 128
      AND lease_owner_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
      AND lease_expires_at IS NOT NULL AND unixepoch(lease_expires_at) IS NOT NULL)
    OR (dispatch_status='dispatched' AND dispatch_attempts BETWEEN 1 AND 10
      AND next_attempt_at IS NULL AND dispatched_at IS NOT NULL
      AND unixepoch(dispatched_at) IS NOT NULL AND claim_token IS NULL
      AND lease_owner_id IS NULL AND lease_expires_at IS NULL AND last_error_code IS NULL)
    OR (dispatch_status='failed' AND dispatch_attempts=10
      AND next_attempt_at IS NULL AND dispatched_at IS NULL AND claim_token IS NULL
      AND lease_owner_id IS NULL AND lease_expires_at IS NULL AND last_error_code IS NOT NULL)
  )
);

CREATE INDEX idx_adapter_contract_event_pending_prd064
  ON adapter_contract_event_outbox(dispatch_status,next_attempt_at,lease_expires_at,created_at)
  WHERE dispatch_status IN ('pending','processing');

CREATE TRIGGER trg_adapter_contract_event_validate_insert_prd064
BEFORE INSERT ON adapter_contract_event_outbox FOR EACH ROW
WHEN NOT (
  NEW.dispatch_status='pending' AND NEW.dispatch_attempts=0
  AND NEW.next_attempt_at=NEW.created_at AND NEW.updated_at=NEW.created_at
  AND NEW.dispatched_at IS NULL AND NEW.claim_token IS NULL
  AND NEW.lease_owner_id IS NULL AND NEW.lease_expires_at IS NULL
  AND NEW.last_error_code IS NULL
  AND EXISTS (
    SELECT 1 FROM adapter_contract_runs run
    WHERE run.tenant_id=NEW.tenant_id
      AND run.adapter_contract_run_id=NEW.adapter_contract_run_id
      AND run.status='failed' AND run.failed_cases>0
  )
)
BEGIN SELECT RAISE(ABORT,'adapter_contract_event_invalid'); END;

-- 只允许固定 claim/接管/finish 状态机修改调度字段；run owner 和 source 身份永远不可改。
CREATE TRIGGER trg_adapter_contract_event_state_update_prd064
BEFORE UPDATE ON adapter_contract_event_outbox FOR EACH ROW
WHEN NOT (
  NEW.rowid=OLD.rowid
  AND NEW.event_id=OLD.event_id AND NEW.tenant_id=OLD.tenant_id
  AND NEW.adapter_contract_run_id=OLD.adapter_contract_run_id
  AND NEW.created_at=OLD.created_at
  AND unixepoch(NEW.updated_at)>=unixepoch(OLD.updated_at)
  AND (
    (
      (
        (OLD.dispatch_status='pending' AND unixepoch(OLD.next_attempt_at)<=unixepoch(NEW.updated_at))
        OR (OLD.dispatch_status='processing' AND OLD.dispatch_attempts BETWEEN 1 AND 9
            AND unixepoch(OLD.lease_expires_at)<=unixepoch(NEW.updated_at))
      )
      AND NEW.dispatch_status='processing'
      AND NEW.dispatch_attempts=OLD.dispatch_attempts+1
      AND NEW.next_attempt_at IS NULL AND NEW.dispatched_at IS NULL
      AND NEW.claim_token IS NOT NULL AND length(NEW.claim_token)=32
      AND NEW.claim_token NOT GLOB '*[^0-9a-f]*'
      AND NEW.lease_owner_id IS NOT NULL AND length(NEW.lease_owner_id) BETWEEN 1 AND 128
      AND NEW.lease_owner_id NOT GLOB '*[^A-Za-z0-9._:-]*'
      AND unixepoch(NEW.lease_expires_at)=unixepoch(NEW.updated_at)+30
      AND NEW.last_error_code IS NULL
    )
    OR (
      OLD.dispatch_status='processing'
      AND NEW.dispatch_attempts=OLD.dispatch_attempts
      AND NEW.claim_token IS NULL AND NEW.lease_owner_id IS NULL AND NEW.lease_expires_at IS NULL
      AND (
        (
          unixepoch(OLD.lease_expires_at)>unixepoch(NEW.updated_at)
          AND (
            (NEW.dispatch_status='dispatched' AND NEW.dispatched_at=NEW.updated_at
              AND NEW.next_attempt_at IS NULL AND NEW.last_error_code IS NULL)
            OR (OLD.dispatch_attempts<10 AND NEW.dispatch_status='pending'
              AND NEW.dispatched_at IS NULL AND NEW.last_error_code IS NOT NULL
              AND unixepoch(NEW.next_attempt_at)=unixepoch(NEW.updated_at)+CASE
                WHEN OLD.dispatch_attempts=1 THEN 5
                WHEN OLD.dispatch_attempts=2 THEN 30
                ELSE 300 END)
            OR (OLD.dispatch_attempts=10 AND NEW.dispatch_status='failed'
              AND NEW.next_attempt_at IS NULL AND NEW.dispatched_at IS NULL
              AND NEW.last_error_code IS NOT NULL)
          )
        )
        OR (
          OLD.dispatch_attempts=10
          AND unixepoch(OLD.lease_expires_at)<=unixepoch(NEW.updated_at)
          AND NEW.dispatch_status='failed' AND NEW.next_attempt_at IS NULL
          AND NEW.dispatched_at IS NULL AND NEW.last_error_code IS NOT NULL
        )
      )
    )
  )
)
BEGIN SELECT RAISE(ABORT,'adapter_contract_event_state_invalid'); END;

CREATE TRIGGER trg_adapter_contract_event_immutable_delete_prd064
BEFORE DELETE ON adapter_contract_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'adapter_contract_event_immutable'); END;
