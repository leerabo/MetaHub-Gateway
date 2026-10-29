-- PRD-051 FinOps Ledger：把不可变 Usage 事实投影为可追溯、只追加的正式财务分录。
-- 本迁移只保存租户内安全元数据和整数微元，不复制 Prompt、Response、密钥或 Endpoint 明文。
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

-- 前置门禁必须先于任何持久 DDL。TEMP 对象只负责把结构漂移收敛为稳定错误并随事务回滚。
CREATE TEMP TABLE prd051_prerequisite_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd051_prerequisite_guard
BEFORE INSERT ON prd051_prerequisite_guard
FOR EACH ROW
WHEN NEW.issue_count != 0
BEGIN
  SELECT RAISE(ABORT, 'prd051_prerequisite_missing');
END;
-- 只有 0040 与全部依赖对象就绪时才允许继续，避免在未知基线上创建半套账本结构。
INSERT INTO prd051_prerequisite_guard(issue_count)
SELECT
  CASE WHEN (
    SELECT COUNT(*) FROM schema_migrations
    WHERE version='0040' AND status='applied'
  )=1 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='table' AND name IN (
      'tenants', 'users', 'request_logs', 'usage_ledger', 'price_snapshots',
      'audit_logs', 'webhook_endpoints', 'webhook_deliveries',
      'webhook_delivery_attempts', 'usage_export_ledger_sequences'
    )
  )=10 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='trigger' AND name IN (
      'trg_usage_routing_attempt_insert',
      'trg_usage_ledger_export_append_only_update',
      'trg_usage_ledger_export_append_only_delete',
      'trg_usage_ledger_export_sequence_insert',
      'trg_usage_export_ledger_sequences_owner_insert',
      'trg_usage_export_ledger_sequences_immutable_update',
      'trg_usage_export_ledger_sequences_immutable_delete',
      'trg_audit_logs_owner_insert',
      'trg_audit_logs_immutable_update',
      'trg_audit_logs_immutable_delete',
      'trg_audit_logs_prd050_conflict_insert',
      'trg_audit_logs_prd050_rowid_insert',
      'trg_webhook_deliveries_prd042_owner_insert',
      'trg_webhook_deliveries_prd042_owner_immutable'
    )
  )=14 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='index' AND tbl_name='audit_logs' AND sql IS NOT NULL
      AND name IN (
        'idx_audit_logs_tenant_time',
        'idx_audit_logs_tenant_actor_time',
        'idx_audit_logs_tenant_object_time',
        'idx_audit_logs_tenant_action_time',
        'idx_audit_logs_tenant_request',
        'idx_audit_logs_dry_run_request_unique',
        'idx_audit_logs_model_quality_request_unique'
      )
  )=7 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='index' AND tbl_name='audit_logs' AND sql IS NOT NULL
  )=7 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='trigger' AND tbl_name='audit_logs'
  )=5 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE name IN (
       'finops_adjustments', 'finops_ledger_entries',
       'audit_logs_prd051_new', 'audit_logs_prd051_old'
     ))
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE type='view' AND lower(COALESCE(sql, '')) LIKE '%audit_logs%')
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE type='trigger' AND tbl_name!='audit_logs'
       AND lower(COALESCE(sql, '')) LIKE '%audit_logs%')
  + (SELECT COUNT(*) FROM sqlite_master
     WHERE type='table' AND name!='audit_logs'
       AND lower(COALESCE(sql, '')) LIKE '%references%audit_logs%')
  + (SELECT COUNT(*) FROM usage_ledger
     WHERE NOT json_valid(normalized_usage)
        OR json_type(normalized_usage)!='object')
  + (SELECT COUNT(*) FROM price_snapshots
     WHERE NOT json_valid(billing_dimensions)
        OR json_type(billing_dimensions)!='object'
        OR NOT json_valid(provider_raw_mapping)
        OR json_type(provider_raw_mapping)!='object');
DROP TRIGGER trg_prd051_prerequisite_guard;
DROP TABLE prd051_prerequisite_guard;

-- 先把每条历史 Usage 的权威十进制文本、整数微元和目标快照冻结在 TEMP 表中。
-- normalized_usage 缺少 cost_amount 才允许使用一次性 printf fallback；显式 null 或错误类型拒绝。
CREATE TEMP TABLE prd051_usage_backfill_source (
  usage_ledger_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  source_trace_id TEXT NOT NULL,
  amount_text TEXT,
  integer_text TEXT,
  fraction_text TEXT,
  amount_micros INTEGER,
  currency TEXT NOT NULL,
  target_price_snapshot_id TEXT NOT NULL,
  target_price_schema_version TEXT NOT NULL,
  target_price_snapshot_source TEXT NOT NULL,
  price_evidence_status TEXT NOT NULL,
  target_input_price_per_1k NUMERIC NOT NULL,
  target_output_price_per_1k NUMERIC NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  total_tokens INTEGER NOT NULL,
  cache_tokens INTEGER NOT NULL,
  model_id TEXT,
  provider_id TEXT,
  provider_code TEXT,
  virtual_key_id TEXT,
  environment_id TEXT,
  project_id TEXT,
  application_id TEXT,
  estimated INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
WITH canonical_usage AS (
  SELECT
    usage.*,
    request.trace_id AS source_trace_id,
    snapshot.price_snapshot_id AS existing_snapshot_id,
    snapshot.tenant_id AS snapshot_tenant_id,
    snapshot.request_id AS snapshot_request_id,
    snapshot.currency AS snapshot_currency,
    snapshot.price_schema_version AS snapshot_version,
    snapshot.source AS snapshot_source,
    snapshot.input_price_per_1k AS snapshot_input_price,
    snapshot.output_price_per_1k AS snapshot_output_price,
    CASE
      WHEN json_type(usage.normalized_usage, '$.cost_amount')='text'
        THEN json_extract(usage.normalized_usage, '$.cost_amount')
      WHEN json_type(usage.normalized_usage, '$.cost_amount') IS NULL
        THEN printf('%.6f', usage.cost_amount)
      ELSE NULL
    END AS amount_text
  FROM usage_ledger usage
  LEFT JOIN request_logs request
    ON request.tenant_id=usage.tenant_id AND request.request_id=usage.request_id
  LEFT JOIN price_snapshots snapshot
    ON snapshot.price_snapshot_id=usage.price_snapshot_id
), split_amount AS (
  SELECT
    canonical.*,
    CASE WHEN instr(amount_text, '.')=0
      THEN amount_text ELSE substr(amount_text, 1, instr(amount_text, '.')-1) END AS integer_text,
    CASE WHEN instr(amount_text, '.')=0
      THEN '' ELSE substr(amount_text, instr(amount_text, '.')+1) END AS fraction_text,
    CASE
      WHEN existing_snapshot_id IS NOT NULL
       AND snapshot_tenant_id=tenant_id
       AND snapshot_request_id=request_id
       AND snapshot_currency=currency
       AND NULLIF(trim(snapshot_version), '') IS NOT NULL
       AND NULLIF(trim(price_schema_version), '') IS NOT NULL
       AND snapshot_version=price_schema_version
      THEN 1 ELSE 0
    END AS trusted_snapshot
  FROM canonical_usage canonical
)
-- 冻结每条历史 Usage 的原币微元和价格证据，后续 DDL 只消费这份已校验来源。
INSERT INTO prd051_usage_backfill_source(
  usage_ledger_id, tenant_id, request_id, source_trace_id,
  amount_text, integer_text, fraction_text, amount_micros, currency,
  target_price_snapshot_id, target_price_schema_version,
  target_price_snapshot_source, price_evidence_status,
  target_input_price_per_1k, target_output_price_per_1k,
  input_tokens, output_tokens, total_tokens, cache_tokens,
  model_id, provider_id, provider_code, virtual_key_id, environment_id,
  project_id, application_id, estimated, created_at
)
SELECT
  usage_ledger_id, tenant_id, request_id, source_trace_id,
  amount_text, integer_text, fraction_text,
  CASE WHEN amount_text IS NULL THEN NULL ELSE
    CAST(integer_text AS INTEGER) * 1000000
    + CASE WHEN fraction_text=''
        THEN 0 ELSE CAST(substr(fraction_text || '000000', 1, 6) AS INTEGER) END
  END,
  currency,
  CASE WHEN trusted_snapshot=1
    THEN existing_snapshot_id ELSE 'prs_' || lower(hex(randomblob(16))) END,
  CASE WHEN trusted_snapshot=1
    THEN snapshot_version ELSE 'legacy-unversioned-v1' END,
  CASE WHEN trusted_snapshot=1
    THEN snapshot_source ELSE 'finops_legacy_backfill' END,
  CASE
    WHEN trusted_snapshot=0 THEN 'legacy_amount_only'
    WHEN snapshot_source='finops_unpriced' THEN 'unpriced'
    WHEN snapshot_source='finops_legacy_backfill' THEN 'legacy_amount_only'
    ELSE 'priced'
  END,
  CASE WHEN trusted_snapshot=1 THEN snapshot_input_price ELSE 0 END,
  CASE WHEN trusted_snapshot=1 THEN snapshot_output_price ELSE 0 END,
  input_tokens, output_tokens, total_tokens, cache_tokens,
  model_id, provider_id, provider_code, virtual_key_id, environment_id,
  project_id, application_id, estimated, created_at
FROM split_amount;

-- 数据门禁逐行验证金额可逆、租户 owner、版本和币种；失败时尚未发生任何持久结构变化。
CREATE TEMP TABLE prd051_usage_preflight_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd051_usage_preflight_guard
BEFORE INSERT ON prd051_usage_preflight_guard
FOR EACH ROW
WHEN NEW.issue_count != 0
BEGIN
  SELECT RAISE(ABORT, 'prd051_usage_preflight_failed');
END;
-- 汇总所有逐行金额、owner、快照和版本异常，任一问题都在持久 DDL 前终止迁移。
INSERT INTO prd051_usage_preflight_guard(issue_count)
SELECT
  (SELECT COUNT(*) FROM prd051_usage_backfill_source source
   WHERE source.source_trace_id IS NULL
      OR source.currency NOT IN ('CNY', 'USD')
      OR source.amount_text IS NULL
      OR typeof(source.amount_text)!='text'
      OR source.amount_text=''
      OR source.amount_text GLOB '*[^0-9.]*'
      OR source.integer_text=''
      OR source.integer_text GLOB '*[^0-9]*'
      OR length(source.integer_text) NOT BETWEEN 1 AND 10
      OR (length(source.integer_text)>1 AND substr(source.integer_text, 1, 1)='0')
      OR (
        instr(source.amount_text, '.')>0
        AND (
          source.fraction_text=''
          OR source.fraction_text GLOB '*[^0-9]*'
          OR length(source.fraction_text) NOT BETWEEN 1 AND 6
        )
      )
      OR instr(substr(source.amount_text, instr(source.amount_text, '.')+1), '.')>0
      OR typeof(source.amount_micros)!='integer'
      OR source.amount_micros NOT BETWEEN 0 AND 1000000000000000
      OR printf(
           '%d.%06d',
           CAST(source.amount_micros / 1000000 AS INTEGER),
           source.amount_micros % 1000000
         ) != source.integer_text || '.' || substr(source.fraction_text || '000000', 1, 6))
  + (SELECT COUNT(*)
     FROM usage_ledger usage
     JOIN prd051_usage_backfill_source source
       ON source.usage_ledger_id=usage.usage_ledger_id
     WHERE (
       json_type(usage.normalized_usage, '$.cost_amount') IS NULL
       AND (
         typeof(usage.cost_amount) NOT IN ('integer', 'real')
         OR usage.cost_amount<0 OR usage.cost_amount>1000000000
         OR printf('%.6f', usage.cost_amount) NOT GLOB '[0-9]*.[0-9][0-9][0-9][0-9][0-9][0-9]'
       )
     )
     OR (
       json_type(usage.normalized_usage, '$.cost_amount')='text'
       AND printf('%.6f', usage.cost_amount) != printf(
         '%d.%06d',
         CAST(source.amount_micros / 1000000 AS INTEGER),
         source.amount_micros % 1000000
       )
     )
     OR json_type(usage.normalized_usage, '$.cost_amount') NOT IN ('text')
        AND json_type(usage.normalized_usage, '$.cost_amount') IS NOT NULL
     OR (
       usage.currency='CNY'
       AND json_type(usage.normalized_usage, '$.cost_cny')='text'
       AND json_extract(usage.normalized_usage, '$.cost_cny') != source.amount_text
     )
     OR (
       usage.currency='CNY'
       AND json_type(usage.normalized_usage, '$.cost_cny') NOT IN ('text')
       AND json_type(usage.normalized_usage, '$.cost_cny') IS NOT NULL
     )
     OR (
       json_type(usage.normalized_usage, '$.currency') IS NOT NULL
       AND (
         json_type(usage.normalized_usage, '$.currency')!='text'
         OR json_extract(usage.normalized_usage, '$.currency')!=usage.currency
       )
     ))
  + (SELECT COUNT(*)
     FROM usage_ledger usage
     LEFT JOIN request_logs request
       ON request.tenant_id=usage.tenant_id AND request.request_id=usage.request_id
     LEFT JOIN price_snapshots snapshot
       ON snapshot.price_snapshot_id=usage.price_snapshot_id
     WHERE request.request_id IS NULL
        OR (snapshot.price_snapshot_id IS NOT NULL AND (
          snapshot.tenant_id!=usage.tenant_id
          OR snapshot.request_id IS NOT usage.request_id
          OR snapshot.currency!=usage.currency
          OR (
            NULLIF(trim(snapshot.price_schema_version), '') IS NOT NULL
            AND NULLIF(trim(usage.price_schema_version), '') IS NOT NULL
            AND snapshot.price_schema_version!=usage.price_schema_version
          )
        ))
        OR (usage.model_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM models model
          WHERE model.model_id=usage.model_id AND model.tenant_id=usage.tenant_id
        ))
        OR (usage.provider_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM providers provider
          WHERE provider.provider_id=usage.provider_id AND provider.tenant_id=usage.tenant_id
        ))
        OR (usage.virtual_key_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM virtual_keys key
          WHERE key.virtual_key_id=usage.virtual_key_id AND key.tenant_id=usage.tenant_id
        ))
        OR (usage.environment_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM environments environment
          WHERE environment.environment_id=usage.environment_id
            AND environment.tenant_id=usage.tenant_id
        ))
        OR (usage.project_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM projects project
          WHERE project.project_id=usage.project_id AND project.tenant_id=usage.tenant_id
        ))
        OR (usage.application_id IS NOT NULL AND NOT EXISTS (
          SELECT 1 FROM applications application
          WHERE application.application_id=usage.application_id
            AND application.tenant_id=usage.tenant_id
        ))
        OR typeof(usage.input_tokens)!='integer' OR usage.input_tokens<0
        OR typeof(usage.output_tokens)!='integer' OR usage.output_tokens<0
        OR typeof(usage.total_tokens)!='integer' OR usage.total_tokens<0
        OR typeof(usage.cache_tokens)!='integer' OR usage.cache_tokens<0
        OR usage.estimated NOT IN (0, 1));
DROP TRIGGER trg_prd051_usage_preflight_guard;
DROP TABLE prd051_usage_preflight_guard;

-- Audit 保真重建仅扩展 FinOps 调整动作；rowid、NULL 和全部 17 列必须逐值不变。
CREATE TABLE audit_logs_prd051_new (
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
      'finops.adjustment_created'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'usage_export_job', 'policy',
      'policy_snapshot', 'policy_explanation', 'routing_policy',
      'model_quality_score', 'cache_affinity', 'finops_adjustment'
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
-- 复制旧 Audit 时显式保留 rowid 与全部可空字段，避免重建枚举时改写历史事实。
INSERT INTO audit_logs_prd051_new(
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
CREATE TEMP TABLE prd051_audit_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  null_mismatch_count INTEGER NOT NULL,
  row_difference_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd051_audit_copy_guard
BEFORE INSERT ON prd051_audit_copy_guard
FOR EACH ROW
WHEN NEW.source_count!=NEW.copied_count
  OR NEW.null_mismatch_count!=0
  OR NEW.row_difference_count!=0
BEGIN
  SELECT RAISE(ABORT, 'prd051_audit_copy_failed');
END;
-- 对比源表与新表的计数、NULL 分布和双向行差，确认保真后才允许切表。
INSERT INTO prd051_audit_copy_guard(
  source_count, copied_count, null_mismatch_count, row_difference_count
)
SELECT
  (SELECT COUNT(*) FROM audit_logs),
  (SELECT COUNT(*) FROM audit_logs_prd051_new),
  CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd051_new WHERE object_name IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd051_new WHERE client_ip IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd051_new WHERE user_agent IS NULL) THEN 0 ELSE 1 END,
  (SELECT COUNT(*) FROM (
    SELECT rowid, * FROM audit_logs
    EXCEPT SELECT rowid, * FROM audit_logs_prd051_new
  ))
  + (SELECT COUNT(*) FROM (
    SELECT rowid, * FROM audit_logs_prd051_new
    EXCEPT SELECT rowid, * FROM audit_logs
  ));
DROP TRIGGER trg_prd051_audit_copy_guard;
DROP TABLE prd051_audit_copy_guard;
DROP TABLE audit_logs;
-- 只有保真门禁通过后才把新表切换为正式名称，避免暴露不完整审计表。
ALTER TABLE audit_logs_prd051_new RENAME TO audit_logs;

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

-- 复合唯一索引是 SQLite 复合外键的父键，也让所有账本引用在数据库层携带 tenant_id。
CREATE UNIQUE INDEX uq_users_tenant_user_prd051
  ON users(tenant_id, user_id);
CREATE UNIQUE INDEX uq_usage_ledger_tenant_usage_prd051
  ON usage_ledger(tenant_id, usage_ledger_id);
CREATE UNIQUE INDEX uq_price_snapshots_tenant_snapshot_prd051
  ON price_snapshots(tenant_id, price_snapshot_id);
CREATE UNIQUE INDEX uq_models_tenant_model_prd051
  ON models(tenant_id, model_id);
CREATE UNIQUE INDEX uq_providers_tenant_provider_prd051
  ON providers(tenant_id, provider_id);
CREATE UNIQUE INDEX uq_virtual_keys_tenant_key_prd051
  ON virtual_keys(tenant_id, virtual_key_id);
CREATE UNIQUE INDEX uq_environments_tenant_environment_prd051
  ON environments(tenant_id, environment_id);
CREATE UNIQUE INDEX uq_projects_tenant_project_prd051
  ON projects(tenant_id, project_id);
CREATE UNIQUE INDEX uq_applications_tenant_application_prd051
  ON applications(tenant_id, application_id);
CREATE UNIQUE INDEX uq_price_snapshots_finops_usage_prd051
  ON price_snapshots(
    tenant_id,
    json_extract(billing_dimensions, '$.finops_usage_ledger_id')
  )
  WHERE source IN ('finops_unpriced', 'finops_legacy_backfill');

-- Adjustment 保存操作者输入的不可变财务事实；金额方向和展示值由类型推导而非客户端自报。
CREATE TABLE finops_adjustments (
  finops_adjustment_id TEXT PRIMARY KEY CHECK (
    length(finops_adjustment_id)=37
    AND substr(finops_adjustment_id, 1, 5)='fadj_'
    AND substr(finops_adjustment_id, 6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  adjustment_type TEXT NOT NULL CHECK (
    adjustment_type IN (
      'manual_debit', 'manual_credit', 'reversal', 'gift_credit', 'trial_credit'
    )
  ),
  amount_micros INTEGER NOT NULL CHECK (
    typeof(amount_micros)='integer' AND amount_micros BETWEEN 1 AND 1000000000000000
  ),
  currency TEXT NOT NULL CHECK (currency IN ('CNY', 'USD')),
  reversal_of_entry_id TEXT,
  related_request_id TEXT,
  reason TEXT NOT NULL CHECK (
    typeof(reason)='text'
    AND reason=trim(reason)
    AND length(CAST(reason AS BLOB)) BETWEEN 1 AND 500
    AND instr(reason, char(0))=0
    AND instr(reason, char(9))=0
    AND instr(reason, char(10))=0
    AND instr(reason, char(13))=0
  ),
  reason_sha256 TEXT NOT NULL CHECK (
    length(reason_sha256)=64 AND reason_sha256 NOT GLOB '*[^0-9a-f]*'
  ),
  actor_user_id TEXT NOT NULL,
  operation_request_id TEXT NOT NULL CHECK (length(operation_request_id) BETWEEN 1 AND 128),
  operation_trace_id TEXT NOT NULL CHECK (length(operation_trace_id) BETWEEN 1 AND 128),
  idempotency_key_sha256 TEXT NOT NULL CHECK (
    length(idempotency_key_sha256)=64
    AND idempotency_key_sha256 NOT GLOB '*[^0-9a-f]*'
  ),
  payload_sha256 TEXT NOT NULL CHECK (
    length(payload_sha256)=64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
  ),
  status TEXT NOT NULL DEFAULT 'posted' CHECK (status='posted'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, actor_user_id) REFERENCES users(tenant_id, user_id),
  FOREIGN KEY (tenant_id, related_request_id) REFERENCES request_logs(tenant_id, request_id),
  FOREIGN KEY (tenant_id, reversal_of_entry_id)
    REFERENCES finops_ledger_entries(tenant_id, finops_ledger_entry_id),
  UNIQUE (tenant_id, finops_adjustment_id),
  UNIQUE (tenant_id, idempotency_key_sha256),
  CHECK (created_at=updated_at),
  CHECK (
    (adjustment_type='reversal' AND reversal_of_entry_id IS NOT NULL)
    OR (adjustment_type!='reversal' AND reversal_of_entry_id IS NULL AND currency='CNY')
  )
);

-- Formal Entry 冻结原币微元、价格证据及安全归属；任何汇总都从整数微元计算。
CREATE TABLE finops_ledger_entries (
  ledger_sequence INTEGER PRIMARY KEY AUTOINCREMENT CHECK (ledger_sequence>0),
  finops_ledger_entry_id TEXT NOT NULL UNIQUE CHECK (
    length(finops_ledger_entry_id)=36
    AND substr(finops_ledger_entry_id, 1, 4)='fle_'
    AND substr(finops_ledger_entry_id, 5) NOT GLOB '*[^0-9a-f]*'
  ),
  event_id TEXT NOT NULL UNIQUE CHECK (
    length(event_id)=38
    AND substr(event_id, 1, 6)='flevt_'
    AND substr(event_id, 7) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  entry_type TEXT NOT NULL CHECK (
    entry_type IN (
      'usage_charge', 'manual_debit', 'manual_credit', 'reversal',
      'gift_credit', 'trial_credit'
    )
  ),
  direction TEXT NOT NULL CHECK (direction IN ('debit', 'credit')),
  amount_micros INTEGER NOT NULL CHECK (
    typeof(amount_micros)='integer'
    AND amount_micros BETWEEN 0 AND 1000000000000000
  ),
  currency TEXT NOT NULL CHECK (currency IN ('CNY', 'USD')),
  normalized_cny_micros INTEGER CHECK (
    normalized_cny_micros IS NULL
    OR (
      typeof(normalized_cny_micros)='integer'
      AND normalized_cny_micros BETWEEN 0 AND 1000000000000000
    )
  ),
  cny_normalization_status TEXT NOT NULL CHECK (
    cny_normalization_status IN ('normalized', 'unavailable')
  ),
  price_evidence_status TEXT NOT NULL CHECK (
    price_evidence_status IN ('priced', 'unpriced', 'legacy_amount_only', 'not_applicable')
  ),
  price_snapshot_source TEXT,
  posting_source TEXT NOT NULL CHECK (
    posting_source IN ('direct', 'spool_replay', 'legacy_backfill')
  ),
  quota_type TEXT NOT NULL CHECK (quota_type IN ('paid', 'free', 'trial', 'gift')),
  usage_ledger_id TEXT,
  finops_adjustment_id TEXT,
  source_request_id TEXT,
  source_trace_id TEXT,
  operation_request_id TEXT NOT NULL CHECK (length(operation_request_id) BETWEEN 1 AND 128),
  operation_trace_id TEXT NOT NULL CHECK (length(operation_trace_id) BETWEEN 1 AND 128),
  price_snapshot_id TEXT,
  price_schema_version TEXT,
  reversal_of_entry_id TEXT,
  actor_user_id TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(input_tokens)='integer' AND input_tokens>=0
  ),
  output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(output_tokens)='integer' AND output_tokens>=0
  ),
  total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(total_tokens)='integer' AND total_tokens>=0
  ),
  cache_tokens INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(cache_tokens)='integer' AND cache_tokens>=0
  ),
  model_id TEXT,
  provider_id TEXT,
  virtual_key_id TEXT,
  environment_id TEXT,
  project_id TEXT,
  application_id TEXT,
  estimated INTEGER NOT NULL DEFAULT 0 CHECK (estimated IN (0, 1)),
  status TEXT NOT NULL DEFAULT 'posted' CHECK (status='posted'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, usage_ledger_id)
    REFERENCES usage_ledger(tenant_id, usage_ledger_id),
  FOREIGN KEY (tenant_id, finops_adjustment_id)
    REFERENCES finops_adjustments(tenant_id, finops_adjustment_id),
  FOREIGN KEY (tenant_id, source_request_id)
    REFERENCES request_logs(tenant_id, request_id),
  FOREIGN KEY (tenant_id, price_snapshot_id)
    REFERENCES price_snapshots(tenant_id, price_snapshot_id),
  FOREIGN KEY (tenant_id, reversal_of_entry_id)
    REFERENCES finops_ledger_entries(tenant_id, finops_ledger_entry_id),
  FOREIGN KEY (tenant_id, actor_user_id) REFERENCES users(tenant_id, user_id),
  FOREIGN KEY (tenant_id, model_id) REFERENCES models(tenant_id, model_id),
  FOREIGN KEY (tenant_id, provider_id) REFERENCES providers(tenant_id, provider_id),
  FOREIGN KEY (tenant_id, virtual_key_id) REFERENCES virtual_keys(tenant_id, virtual_key_id),
  FOREIGN KEY (tenant_id, environment_id)
    REFERENCES environments(tenant_id, environment_id),
  FOREIGN KEY (tenant_id, project_id) REFERENCES projects(tenant_id, project_id),
  FOREIGN KEY (tenant_id, application_id)
    REFERENCES applications(tenant_id, application_id),
  UNIQUE (tenant_id, finops_ledger_entry_id),
  UNIQUE (tenant_id, event_id),
  UNIQUE (tenant_id, usage_ledger_id),
  UNIQUE (tenant_id, finops_adjustment_id),
  CHECK (created_at=updated_at),
  CHECK (entry_type='usage_charge' OR amount_micros>0),
  CHECK (
    (entry_type IN ('usage_charge', 'manual_debit') AND direction='debit')
    OR (entry_type IN ('manual_credit', 'gift_credit', 'trial_credit') AND direction='credit')
    OR entry_type='reversal'
  ),
  CHECK (
    (currency='CNY' AND cny_normalization_status='normalized'
      AND normalized_cny_micros=amount_micros)
    OR (currency='USD' AND cny_normalization_status='unavailable'
      AND normalized_cny_micros IS NULL)
  ),
  CHECK (
    (entry_type='usage_charge' AND usage_ledger_id IS NOT NULL
      AND finops_adjustment_id IS NULL AND actor_user_id IS NULL
      AND source_request_id IS NOT NULL AND source_trace_id IS NOT NULL)
    OR (entry_type!='usage_charge' AND usage_ledger_id IS NULL
      AND finops_adjustment_id IS NOT NULL AND actor_user_id IS NOT NULL)
  ),
  CHECK (
    (entry_type='reversal' AND reversal_of_entry_id IS NOT NULL)
    OR (entry_type!='reversal' AND reversal_of_entry_id IS NULL)
  ),
  CHECK (
    (entry_type='usage_charge' AND quota_type='paid')
    OR (entry_type IN ('manual_debit', 'manual_credit') AND quota_type='paid')
    OR (entry_type='gift_credit' AND quota_type='gift')
    OR (entry_type='trial_credit' AND quota_type='trial')
    OR entry_type='reversal'
  ),
  CHECK (
    (price_evidence_status='not_applicable'
      AND price_snapshot_id IS NULL AND price_schema_version IS NULL
      AND price_snapshot_source IS NULL)
    OR (price_evidence_status='unpriced'
      AND price_snapshot_id IS NOT NULL AND price_schema_version='unpriced-v1'
      AND price_snapshot_source='finops_unpriced')
    OR (price_evidence_status='legacy_amount_only'
      AND price_snapshot_id IS NOT NULL
      AND price_schema_version='legacy-unversioned-v1'
      AND price_snapshot_source='finops_legacy_backfill')
    OR (price_evidence_status='priced'
      AND price_snapshot_id IS NOT NULL
      AND NULLIF(trim(price_schema_version), '') IS NOT NULL
      AND NULLIF(trim(price_snapshot_source), '') IS NOT NULL
      AND price_snapshot_source NOT IN ('finops_unpriced', 'finops_legacy_backfill'))
  ),
  CHECK (entry_type!='usage_charge' OR price_evidence_status!='not_applicable'),
  CHECK (
    entry_type='usage_charge'
    OR entry_type='reversal'
    OR price_evidence_status='not_applicable'
  ),
  CHECK (
    entry_type='usage_charge'
    OR (
      input_tokens=0 AND output_tokens=0 AND total_tokens=0
      AND cache_tokens=0 AND estimated=0
    )
  )
);

CREATE INDEX idx_finops_ledger_tenant_sequence
  ON finops_ledger_entries(tenant_id, ledger_sequence DESC);
CREATE INDEX idx_finops_ledger_tenant_time
  ON finops_ledger_entries(tenant_id, created_at DESC, ledger_sequence DESC);
CREATE INDEX idx_finops_ledger_source_request
  ON finops_ledger_entries(tenant_id, source_request_id, ledger_sequence DESC);
CREATE INDEX idx_finops_ledger_price_snapshot
  ON finops_ledger_entries(tenant_id, price_snapshot_id, ledger_sequence DESC);
CREATE INDEX idx_finops_ledger_reversal_target
  ON finops_ledger_entries(tenant_id, reversal_of_entry_id, ledger_sequence DESC)
  WHERE reversal_of_entry_id IS NOT NULL;
CREATE INDEX idx_finops_ledger_type_direction
  ON finops_ledger_entries(tenant_id, entry_type, direction, ledger_sequence DESC);
CREATE INDEX idx_finops_adjustments_tenant_time
  ON finops_adjustments(tenant_id, created_at DESC, finops_adjustment_id DESC);
CREATE INDEX idx_finops_adjustments_reversal_target
  ON finops_adjustments(tenant_id, reversal_of_entry_id)
  WHERE reversal_of_entry_id IS NOT NULL;

-- 仅对缺失可信版本证据的历史 Usage 创建独享 ledger-only 快照；不更新原 Usage 或旧快照。
INSERT INTO price_snapshots(
  price_snapshot_id, tenant_id, request_id, model_id, provider_id, provider_code,
  currency, input_price_per_1k, output_price_per_1k, price_schema_version,
  billing_dimensions, provider_raw_mapping, source, status,
  created_at, updated_at, price_plan_id, effective_from
)
SELECT
  source.target_price_snapshot_id,
  source.tenant_id,
  source.request_id,
  source.model_id,
  source.provider_id,
  source.provider_code,
  source.currency,
  source.target_input_price_per_1k,
  source.target_output_price_per_1k,
  'legacy-unversioned-v1',
  json_object(
    'finops_usage_ledger_id', source.usage_ledger_id,
    'price_evidence_status', 'legacy_amount_only'
  ),
  '{}',
  'finops_legacy_backfill',
  'active',
  source.created_at,
  source.created_at,
  NULL,
  source.created_at
FROM prd051_usage_backfill_source source
WHERE source.price_evidence_status='legacy_amount_only';

-- 历史正式分录按稳定业务顺序追加，金额直接来自上面的可逆整数微元，不按当前价格重算。
INSERT INTO finops_ledger_entries(
  finops_ledger_entry_id, event_id, tenant_id, entry_type, direction,
  amount_micros, currency, normalized_cny_micros, cny_normalization_status,
  price_evidence_status, price_snapshot_source, posting_source, quota_type,
  usage_ledger_id, finops_adjustment_id, source_request_id, source_trace_id,
  operation_request_id, operation_trace_id, price_snapshot_id,
  price_schema_version, reversal_of_entry_id, actor_user_id,
  input_tokens, output_tokens, total_tokens, cache_tokens,
  model_id, provider_id, virtual_key_id, environment_id, project_id,
  application_id, estimated, status, created_at, updated_at
)
SELECT
  'fle_' || lower(hex(randomblob(16))),
  'flevt_' || lower(hex(randomblob(16))),
  source.tenant_id,
  'usage_charge',
  'debit',
  source.amount_micros,
  source.currency,
  CASE WHEN source.currency='CNY' THEN source.amount_micros ELSE NULL END,
  CASE WHEN source.currency='CNY' THEN 'normalized' ELSE 'unavailable' END,
  source.price_evidence_status,
  source.target_price_snapshot_source,
  'legacy_backfill',
  'paid',
  source.usage_ledger_id,
  NULL,
  source.request_id,
  source.source_trace_id,
  source.request_id,
  source.source_trace_id,
  source.target_price_snapshot_id,
  source.target_price_schema_version,
  NULL,
  NULL,
  source.input_tokens,
  source.output_tokens,
  source.total_tokens,
  source.cache_tokens,
  source.model_id,
  source.provider_id,
  source.virtual_key_id,
  source.environment_id,
  source.project_id,
  source.application_id,
  source.estimated,
  'posted',
  source.created_at,
  source.created_at
FROM prd051_usage_backfill_source source
ORDER BY source.created_at, source.usage_ledger_id;

-- Audit owner trigger 同时校验操作者与 FinOps 对象租户，避免服务遗漏 tenant 条件时横向挂接。
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
-- REPLACE 在 recursive_triggers=OFF 时可能绕过 DELETE trigger，因此覆盖全部唯一冲突目标。
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

-- 调整 owner/语义门禁在写 Adjustment 时锁定同租户用户、关联请求和可冲正余额。
CREATE TRIGGER trg_finops_adjustments_owner_insert_prd051
BEFORE INSERT ON finops_adjustments
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM users user
  WHERE user.user_id=NEW.actor_user_id AND user.tenant_id=NEW.tenant_id
)
OR (
  NEW.related_request_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM finops_ledger_entries entry
    WHERE entry.tenant_id=NEW.tenant_id
      AND entry.entry_type='usage_charge'
      AND entry.source_request_id=NEW.related_request_id
  )
)
OR (
  NEW.adjustment_type='reversal'
  AND NOT EXISTS (
    SELECT 1
    FROM finops_ledger_entries target
    WHERE target.tenant_id=NEW.tenant_id
      AND target.finops_ledger_entry_id=NEW.reversal_of_entry_id
      AND target.entry_type!='reversal'
      AND target.amount_micros>0
      AND target.currency=NEW.currency
      AND NEW.amount_micros <= target.amount_micros - COALESCE((
        SELECT SUM(reversal.amount_micros)
        FROM finops_ledger_entries reversal
        WHERE reversal.tenant_id=target.tenant_id
          AND reversal.entry_type='reversal'
          AND reversal.reversal_of_entry_id=target.finops_ledger_entry_id
      ), 0)
  )
)
BEGIN
  SELECT RAISE(ABORT, 'finops_adjustment_owner_mismatch');
END;
CREATE TRIGGER trg_finops_adjustments_conflict_insert_prd051
BEFORE INSERT ON finops_adjustments
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM finops_adjustments current
  WHERE current.finops_adjustment_id=NEW.finops_adjustment_id
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
     OR (
       current.tenant_id=NEW.tenant_id
       AND current.idempotency_key_sha256=NEW.idempotency_key_sha256
     )
)
BEGIN
  SELECT RAISE(ABORT, 'finops_adjustment_append_only');
END;
CREATE TRIGGER trg_finops_adjustments_rowid_insert_prd051
AFTER INSERT ON finops_adjustments
FOR EACH ROW
WHEN NEW.rowid<0
BEGIN
  SELECT RAISE(ABORT, 'finops_adjustment_append_only');
END;
CREATE TRIGGER trg_finops_adjustments_immutable_update_prd051
BEFORE UPDATE ON finops_adjustments
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'finops_adjustment_append_only');
END;
CREATE TRIGGER trg_finops_adjustments_immutable_delete_prd051
BEFORE DELETE ON finops_adjustments
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'finops_adjustment_append_only');
END;

-- Ledger owner trigger 逐字段核验 Usage/Adjustment 来源，防止伪造金额、租户、快照或安全维度。
CREATE TRIGGER trg_finops_ledger_owner_insert_prd051
BEFORE INSERT ON finops_ledger_entries
FOR EACH ROW
WHEN (
  NEW.entry_type='usage_charge'
  AND NOT EXISTS (
    SELECT 1
    FROM usage_ledger usage
    JOIN request_logs request
      ON request.tenant_id=usage.tenant_id AND request.request_id=usage.request_id
    JOIN price_snapshots snapshot
      ON snapshot.tenant_id=usage.tenant_id
     AND snapshot.price_snapshot_id=NEW.price_snapshot_id
     AND snapshot.request_id=usage.request_id
    WHERE usage.tenant_id=NEW.tenant_id
      AND usage.usage_ledger_id=NEW.usage_ledger_id
      AND usage.request_id=NEW.source_request_id
      AND request.trace_id=NEW.source_trace_id
      AND NEW.operation_request_id=NEW.source_request_id
      AND NEW.operation_trace_id=NEW.source_trace_id
      AND snapshot.currency=NEW.currency
      AND snapshot.price_schema_version=NEW.price_schema_version
      AND snapshot.source=NEW.price_snapshot_source
      AND (
        usage.price_snapshot_id=snapshot.price_snapshot_id
        OR (
          snapshot.source IN ('finops_unpriced', 'finops_legacy_backfill')
          AND json_extract(
            snapshot.billing_dimensions, '$.finops_usage_ledger_id'
          )=usage.usage_ledger_id
        )
      )
      AND NEW.amount_micros=(
        SELECT
          CAST(parts.integer_text AS INTEGER) * 1000000
          + CASE WHEN parts.fraction_text=''
              THEN 0
              ELSE CAST(substr(parts.fraction_text || '000000', 1, 6) AS INTEGER)
            END
        FROM (
          SELECT
            CASE WHEN instr(value.amount_text, '.')=0
              THEN value.amount_text
              ELSE substr(value.amount_text, 1, instr(value.amount_text, '.')-1)
            END AS integer_text,
            CASE WHEN instr(value.amount_text, '.')=0
              THEN '' ELSE substr(value.amount_text, instr(value.amount_text, '.')+1)
            END AS fraction_text
          FROM (
            SELECT CASE
              WHEN json_type(usage.normalized_usage, '$.cost_amount')='text'
                THEN json_extract(usage.normalized_usage, '$.cost_amount')
              ELSE printf('%.6f', usage.cost_amount)
            END AS amount_text
          ) value
        ) parts
      )
      AND NEW.input_tokens=usage.input_tokens
      AND NEW.output_tokens=usage.output_tokens
      AND NEW.total_tokens=usage.total_tokens
      AND NEW.cache_tokens=usage.cache_tokens
      AND NEW.model_id IS usage.model_id
      AND NEW.provider_id IS usage.provider_id
      AND NEW.virtual_key_id IS usage.virtual_key_id
      AND NEW.environment_id IS usage.environment_id
      AND NEW.project_id IS usage.project_id
      AND NEW.application_id IS usage.application_id
      AND NEW.estimated=usage.estimated
      AND NEW.price_evidence_status=CASE
        WHEN snapshot.source='finops_unpriced' THEN 'unpriced'
        WHEN snapshot.source='finops_legacy_backfill' THEN 'legacy_amount_only'
        ELSE 'priced'
      END
  )
)
OR (
  NEW.entry_type!='usage_charge'
  AND NOT EXISTS (
    SELECT 1
    FROM finops_adjustments adjustment
    LEFT JOIN request_logs request
      ON request.tenant_id=adjustment.tenant_id
     AND request.request_id=adjustment.related_request_id
    LEFT JOIN finops_ledger_entries target
      ON target.tenant_id=adjustment.tenant_id
     AND target.finops_ledger_entry_id=adjustment.reversal_of_entry_id
    LEFT JOIN finops_ledger_entries related_usage
      ON related_usage.tenant_id=adjustment.tenant_id
     AND related_usage.entry_type='usage_charge'
     AND related_usage.source_request_id=adjustment.related_request_id
    WHERE adjustment.tenant_id=NEW.tenant_id
      AND adjustment.finops_adjustment_id=NEW.finops_adjustment_id
      AND adjustment.adjustment_type=NEW.entry_type
      AND adjustment.amount_micros=NEW.amount_micros
      AND adjustment.currency=NEW.currency
      AND adjustment.actor_user_id=NEW.actor_user_id
      AND adjustment.operation_request_id=NEW.operation_request_id
      AND adjustment.operation_trace_id=NEW.operation_trace_id
      AND adjustment.reversal_of_entry_id IS NEW.reversal_of_entry_id
      AND (
        (
          adjustment.adjustment_type='reversal'
          AND target.source_request_id IS NEW.source_request_id
          AND target.source_trace_id IS NEW.source_trace_id
        )
        OR (
          adjustment.adjustment_type!='reversal'
          AND adjustment.related_request_id IS NEW.source_request_id
          AND request.trace_id IS NEW.source_trace_id
        )
      )
      AND NEW.posting_source='direct'
      AND (
        (adjustment.adjustment_type='manual_debit' AND NEW.direction='debit')
        OR (adjustment.adjustment_type IN (
          'manual_credit', 'gift_credit', 'trial_credit'
        ) AND NEW.direction='credit')
        OR (
          adjustment.adjustment_type='reversal'
          AND target.entry_type!='reversal'
          AND NEW.direction=CASE target.direction WHEN 'debit' THEN 'credit' ELSE 'debit' END
          AND NEW.quota_type=target.quota_type
          AND NEW.price_evidence_status=target.price_evidence_status
          AND NEW.price_snapshot_source IS target.price_snapshot_source
          AND NEW.price_snapshot_id IS target.price_snapshot_id
          AND NEW.price_schema_version IS target.price_schema_version
          AND NEW.model_id IS target.model_id
          AND NEW.provider_id IS target.provider_id
          AND NEW.virtual_key_id IS target.virtual_key_id
          AND NEW.environment_id IS target.environment_id
          AND NEW.project_id IS target.project_id
          AND NEW.application_id IS target.application_id
        )
      )
      AND (
        adjustment.adjustment_type='reversal'
        OR (
          NEW.price_evidence_status='not_applicable'
          AND (
            (adjustment.related_request_id IS NULL
              AND NEW.model_id IS NULL AND NEW.provider_id IS NULL
              AND NEW.virtual_key_id IS NULL AND NEW.environment_id IS NULL
              AND NEW.project_id IS NULL AND NEW.application_id IS NULL)
            OR (
              adjustment.related_request_id IS NOT NULL
              AND NEW.model_id IS related_usage.model_id
              AND NEW.provider_id IS related_usage.provider_id
              AND NEW.virtual_key_id IS related_usage.virtual_key_id
              AND NEW.environment_id IS related_usage.environment_id
              AND NEW.project_id IS related_usage.project_id
              AND NEW.application_id IS related_usage.application_id
            )
          )
        )
      )
  )
)
BEGIN
  SELECT RAISE(ABORT, 'finops_ledger_owner_mismatch');
END;
CREATE TRIGGER trg_finops_ledger_conflict_insert_prd051
BEFORE INSERT ON finops_ledger_entries
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM finops_ledger_entries current
  WHERE current.finops_ledger_entry_id=NEW.finops_ledger_entry_id
     OR current.event_id=NEW.event_id
     OR (NEW.ledger_sequence!=-1 AND current.ledger_sequence=NEW.ledger_sequence)
     OR (
       NEW.usage_ledger_id IS NOT NULL
       AND current.tenant_id=NEW.tenant_id
       AND current.usage_ledger_id=NEW.usage_ledger_id
     )
     OR (
       NEW.finops_adjustment_id IS NOT NULL
       AND current.tenant_id=NEW.tenant_id
       AND current.finops_adjustment_id=NEW.finops_adjustment_id
     )
)
BEGIN
  SELECT RAISE(ABORT, 'finops_ledger_append_only');
END;
CREATE TRIGGER trg_finops_ledger_immutable_update_prd051
BEFORE UPDATE ON finops_ledger_entries
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'finops_ledger_append_only');
END;
CREATE TRIGGER trg_finops_ledger_immutable_delete_prd051
BEFORE DELETE ON finops_ledger_entries
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'finops_ledger_append_only');
END;

-- PriceSnapshot 从 0041 起是不可变证据；owner trigger 同时校验请求和安全维度的 tenant。
CREATE TRIGGER trg_price_snapshots_owner_insert_prd051
BEFORE INSERT ON price_snapshots
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id
)
OR (
  NEW.request_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM request_logs request
    WHERE request.tenant_id=NEW.tenant_id AND request.request_id=NEW.request_id
  )
)
OR (
  NEW.model_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM models model
    WHERE model.tenant_id=NEW.tenant_id AND model.model_id=NEW.model_id
  )
)
OR (
  NEW.provider_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM providers provider
    WHERE provider.tenant_id=NEW.tenant_id AND provider.provider_id=NEW.provider_id
  )
)
OR (
  NEW.price_plan_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM model_price_plans plan
    WHERE plan.tenant_id=NEW.tenant_id AND plan.price_plan_id=NEW.price_plan_id
  )
)
OR NOT json_valid(NEW.billing_dimensions)
OR json_type(NEW.billing_dimensions)!='object'
OR NOT json_valid(NEW.provider_raw_mapping)
OR json_type(NEW.provider_raw_mapping)!='object'
OR (
  NEW.source='finops_unpriced'
  AND (
    NEW.request_id IS NULL
    OR NEW.price_schema_version!='unpriced-v1'
    OR NEW.input_price_per_1k!=0 OR NEW.output_price_per_1k!=0
    OR NULLIF(json_extract(
      NEW.billing_dimensions, '$.finops_usage_ledger_id'
    ), '') IS NULL
  )
)
OR (
  NEW.source='finops_legacy_backfill'
  AND (
    NEW.request_id IS NULL
    OR NEW.price_schema_version!='legacy-unversioned-v1'
    OR NULLIF(json_extract(
      NEW.billing_dimensions, '$.finops_usage_ledger_id'
    ), '') IS NULL
  )
)
OR (
  NEW.source NOT IN ('finops_unpriced', 'finops_legacy_backfill')
  AND NULLIF(trim(NEW.price_schema_version), '') IS NULL
)
BEGIN
  SELECT RAISE(ABORT, 'price_snapshot_owner_mismatch');
END;
CREATE TRIGGER trg_price_snapshots_conflict_insert_prd051
BEFORE INSERT ON price_snapshots
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM price_snapshots current
  WHERE current.price_snapshot_id=NEW.price_snapshot_id
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
     OR (
       NEW.source IN ('finops_unpriced', 'finops_legacy_backfill')
       AND current.source IN ('finops_unpriced', 'finops_legacy_backfill')
       AND current.tenant_id=NEW.tenant_id
       AND json_extract(
         current.billing_dimensions, '$.finops_usage_ledger_id'
       )=json_extract(NEW.billing_dimensions, '$.finops_usage_ledger_id')
     )
)
BEGIN
  SELECT RAISE(ABORT, 'price_snapshot_append_only');
END;
CREATE TRIGGER trg_price_snapshots_rowid_insert_prd051
AFTER INSERT ON price_snapshots
FOR EACH ROW
WHEN NEW.rowid<0
BEGIN
  SELECT RAISE(ABORT, 'price_snapshot_append_only');
END;
CREATE TRIGGER trg_price_snapshots_immutable_update_prd051
BEFORE UPDATE ON price_snapshots
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'price_snapshot_append_only');
END;
CREATE TRIGGER trg_price_snapshots_immutable_delete_prd051
BEFORE DELETE ON price_snapshots
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'price_snapshot_append_only');
END;

-- Usage owner 门禁补上旧表缺少的 tenant-qualified 维度校验；低层写入也不能跨租户挂接。
CREATE TRIGGER trg_usage_ledger_finops_owner_insert_prd051
BEFORE INSERT ON usage_ledger
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM request_logs request
  WHERE request.tenant_id=NEW.tenant_id AND request.request_id=NEW.request_id
)
OR (
  NEW.price_snapshot_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM price_snapshots snapshot
    WHERE snapshot.price_snapshot_id=NEW.price_snapshot_id
      AND snapshot.tenant_id=NEW.tenant_id
      AND snapshot.request_id=NEW.request_id
      AND snapshot.currency=NEW.currency
  )
)
OR (
  NEW.model_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM models model
    WHERE model.model_id=NEW.model_id AND model.tenant_id=NEW.tenant_id
  )
)
OR (
  NEW.provider_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM providers provider
    WHERE provider.provider_id=NEW.provider_id AND provider.tenant_id=NEW.tenant_id
  )
)
OR (
  NEW.virtual_key_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM virtual_keys key
    WHERE key.virtual_key_id=NEW.virtual_key_id AND key.tenant_id=NEW.tenant_id
  )
)
OR (
  NEW.environment_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM environments environment
    WHERE environment.environment_id=NEW.environment_id
      AND environment.tenant_id=NEW.tenant_id
  )
)
OR (
  NEW.project_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM projects project
    WHERE project.project_id=NEW.project_id AND project.tenant_id=NEW.tenant_id
  )
)
OR (
  NEW.application_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM applications application
    WHERE application.application_id=NEW.application_id
      AND application.tenant_id=NEW.tenant_id
      AND (NEW.project_id IS NULL OR application.project_id=NEW.project_id)
  )
)
BEGIN
  SELECT RAISE(ABORT, 'usage_finops_owner_mismatch');
END;

-- 金额门禁只对 normalized JSON 文本做整数拆分；禁止 float 乘法、指数和七位小数。
CREATE TRIGGER trg_usage_ledger_finops_amount_insert_prd051
BEFORE INSERT ON usage_ledger
FOR EACH ROW
BEGIN
  SELECT CASE WHEN NOT json_valid(NEW.normalized_usage)
    OR json_type(NEW.normalized_usage)!='object'
    THEN RAISE(ABORT, 'usage_finops_amount_invalid') END;
  SELECT CASE WHEN json_type(NEW.normalized_usage, '$.cost_amount') NOT IN ('text')
      AND json_type(NEW.normalized_usage, '$.cost_amount') IS NOT NULL
    THEN RAISE(ABORT, 'usage_finops_amount_invalid') END;
  SELECT CASE WHEN EXISTS (
    SELECT 1
    FROM (
      SELECT
        value.amount_text,
        CASE WHEN instr(value.amount_text, '.')=0
          THEN value.amount_text
          ELSE substr(value.amount_text, 1, instr(value.amount_text, '.')-1)
        END AS integer_text,
        CASE WHEN instr(value.amount_text, '.')=0
          THEN '' ELSE substr(value.amount_text, instr(value.amount_text, '.')+1)
        END AS fraction_text
      FROM (
        SELECT CASE
          WHEN json_type(NEW.normalized_usage, '$.cost_amount')='text'
            THEN json_extract(NEW.normalized_usage, '$.cost_amount')
          ELSE printf('%.6f', NEW.cost_amount)
        END AS amount_text
      ) value
    ) parts
    WHERE parts.amount_text=''
       OR parts.amount_text GLOB '*[^0-9.]*'
       OR parts.integer_text=''
       OR parts.integer_text GLOB '*[^0-9]*'
       OR length(parts.integer_text) NOT BETWEEN 1 AND 10
       OR (length(parts.integer_text)>1 AND substr(parts.integer_text, 1, 1)='0')
       OR (
         instr(parts.amount_text, '.')>0
         AND (
           parts.fraction_text=''
           OR parts.fraction_text GLOB '*[^0-9]*'
           OR length(parts.fraction_text) NOT BETWEEN 1 AND 6
         )
       )
       OR instr(substr(parts.amount_text, instr(parts.amount_text, '.')+1), '.')>0
       OR CAST(parts.integer_text AS INTEGER) * 1000000
          + CASE WHEN parts.fraction_text=''
              THEN 0
              ELSE CAST(substr(parts.fraction_text || '000000', 1, 6) AS INTEGER)
            END > 1000000000000000
  ) THEN RAISE(ABORT, 'usage_finops_amount_invalid') END;
  SELECT CASE WHEN json_type(NEW.normalized_usage, '$.cost_amount')='text'
    AND printf('%.6f', NEW.cost_amount) != (
      SELECT parts.integer_text || '.' || substr(parts.fraction_text || '000000', 1, 6)
      FROM (
        SELECT
          CASE WHEN instr(value.amount_text, '.')=0
            THEN value.amount_text
            ELSE substr(value.amount_text, 1, instr(value.amount_text, '.')-1)
          END AS integer_text,
          CASE WHEN instr(value.amount_text, '.')=0
            THEN '' ELSE substr(value.amount_text, instr(value.amount_text, '.')+1)
          END AS fraction_text
        FROM (
          SELECT json_extract(NEW.normalized_usage, '$.cost_amount') AS amount_text
        ) value
      ) parts
    ) THEN RAISE(ABORT, 'usage_finops_amount_mismatch') END;
  SELECT CASE WHEN NEW.currency NOT IN ('CNY', 'USD')
    THEN RAISE(ABORT, 'usage_finops_currency_invalid') END;
  SELECT CASE WHEN json_type(NEW.normalized_usage, '$.currency') IS NOT NULL
    AND (
      json_type(NEW.normalized_usage, '$.currency')!='text'
      OR json_extract(NEW.normalized_usage, '$.currency')!=NEW.currency
    ) THEN RAISE(ABORT, 'usage_finops_currency_invalid') END;
  SELECT CASE WHEN NEW.currency='CNY'
    AND json_type(NEW.normalized_usage, '$.cost_amount')='text'
    AND (
      json_type(NEW.normalized_usage, '$.cost_cny')!='text'
      OR json_extract(NEW.normalized_usage, '$.cost_cny')
        !=json_extract(NEW.normalized_usage, '$.cost_amount')
    ) THEN RAISE(ABORT, 'usage_finops_cny_mismatch') END;
  SELECT CASE WHEN NEW.currency='USD'
    AND json_type(NEW.normalized_usage, '$.cost_cny') NOT IN ('null')
    AND json_type(NEW.normalized_usage, '$.cost_cny') IS NOT NULL
    THEN RAISE(ABORT, 'usage_finops_cny_unavailable') END;
  SELECT CASE WHEN json_type(NEW.normalized_usage, '$.posting_source') IS NOT NULL
    AND (
      json_type(NEW.normalized_usage, '$.posting_source')!='text'
      OR json_extract(NEW.normalized_usage, '$.posting_source')
        NOT IN ('direct', 'spool_replay')
    ) THEN RAISE(ABORT, 'usage_finops_posting_source_invalid') END;
  SELECT CASE WHEN json_type(NEW.normalized_usage, '$.price_snapshot_id')='text'
    AND json_extract(NEW.normalized_usage, '$.price_snapshot_id') IS NOT NEW.price_snapshot_id
    THEN RAISE(ABORT, 'usage_finops_snapshot_mismatch') END;
  SELECT CASE WHEN json_type(NEW.normalized_usage, '$.price_schema_version')='text'
    AND json_extract(NEW.normalized_usage, '$.price_schema_version') IS NOT NEW.price_schema_version
    THEN RAISE(ABORT, 'usage_finops_snapshot_mismatch') END;
  -- 新 canonical Usage 的非零金额必须已经有可信版本快照；零价才允许自动补 unpriced-v1。
  SELECT CASE WHEN json_type(NEW.normalized_usage, '$.cost_amount')='text'
    AND (
      SELECT CAST(parts.integer_text AS INTEGER) * 1000000
        + CASE WHEN parts.fraction_text=''
            THEN 0 ELSE CAST(substr(parts.fraction_text || '000000', 1, 6) AS INTEGER) END
      FROM (
        SELECT
          CASE WHEN instr(value.amount_text, '.')=0
            THEN value.amount_text
            ELSE substr(value.amount_text, 1, instr(value.amount_text, '.')-1) END AS integer_text,
          CASE WHEN instr(value.amount_text, '.')=0
            THEN '' ELSE substr(value.amount_text, instr(value.amount_text, '.')+1) END AS fraction_text
        FROM (
          SELECT json_extract(NEW.normalized_usage, '$.cost_amount') AS amount_text
        ) value
      ) parts
    )>0
    AND NOT EXISTS (
      SELECT 1 FROM price_snapshots snapshot
      WHERE snapshot.price_snapshot_id=NEW.price_snapshot_id
        AND snapshot.tenant_id=NEW.tenant_id
        AND snapshot.request_id=NEW.request_id
        AND snapshot.currency=NEW.currency
        AND NULLIF(trim(snapshot.price_schema_version), '') IS NOT NULL
        AND snapshot.price_schema_version=NEW.price_schema_version
    ) THEN RAISE(ABORT, 'usage_finops_price_evidence_missing') END;
END;

-- Usage 本身也需阻断 REPLACE；否则隐式删除会让导出水位和正式分录成为孤儿。
CREATE TRIGGER trg_usage_ledger_finops_conflict_insert_prd051
BEFORE INSERT ON usage_ledger
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM usage_ledger current
  WHERE current.usage_ledger_id=NEW.usage_ledger_id
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN
  SELECT RAISE(ABORT, 'usage_ledger_append_only');
END;

-- AFTER trigger 与 Usage、导出水位、Attempt 和预算共用调用方事务；任一失败全部回滚。
CREATE TRIGGER trg_usage_ledger_finops_projection_insert_prd051
AFTER INSERT ON usage_ledger
FOR EACH ROW
BEGIN
  -- 缺失或不可信的价格快照只创建当前 Usage 专属证据，禁止借用当前模型价格回算历史金额。
  INSERT INTO price_snapshots(
    price_snapshot_id, tenant_id, request_id, model_id, provider_id, provider_code,
    currency, input_price_per_1k, output_price_per_1k, price_schema_version,
    billing_dimensions, provider_raw_mapping, source, status,
    created_at, updated_at, price_plan_id, effective_from
  )
  SELECT
    'prs_' || lower(hex(randomblob(16))),
    NEW.tenant_id,
    NEW.request_id,
    NEW.model_id,
    NEW.provider_id,
    NEW.provider_code,
    NEW.currency,
    CASE WHEN json_type(NEW.normalized_usage, '$.cost_amount') IS NULL
      THEN COALESCE((
        SELECT snapshot.input_price_per_1k FROM price_snapshots snapshot
        WHERE snapshot.price_snapshot_id=NEW.price_snapshot_id
      ), 0) ELSE 0 END,
    CASE WHEN json_type(NEW.normalized_usage, '$.cost_amount') IS NULL
      THEN COALESCE((
        SELECT snapshot.output_price_per_1k FROM price_snapshots snapshot
        WHERE snapshot.price_snapshot_id=NEW.price_snapshot_id
      ), 0) ELSE 0 END,
    CASE WHEN json_type(NEW.normalized_usage, '$.cost_amount') IS NULL
      THEN 'legacy-unversioned-v1' ELSE 'unpriced-v1' END,
    json_object(
      'finops_usage_ledger_id', NEW.usage_ledger_id,
      'price_evidence_status', CASE
        WHEN json_type(NEW.normalized_usage, '$.cost_amount') IS NULL
          THEN 'legacy_amount_only' ELSE 'unpriced' END
    ),
    '{}',
    CASE WHEN json_type(NEW.normalized_usage, '$.cost_amount') IS NULL
      THEN 'finops_legacy_backfill' ELSE 'finops_unpriced' END,
    'active',
    NEW.created_at,
    NEW.created_at,
    NULL,
    NEW.created_at
  WHERE NOT EXISTS (
    SELECT 1 FROM price_snapshots snapshot
    WHERE snapshot.price_snapshot_id=NEW.price_snapshot_id
      AND snapshot.tenant_id=NEW.tenant_id
      AND snapshot.request_id=NEW.request_id
      AND snapshot.currency=NEW.currency
      AND NULLIF(trim(snapshot.price_schema_version), '') IS NOT NULL
      AND snapshot.price_schema_version=NEW.price_schema_version
  );

  -- 正式分录与新 Usage 在同一事务产生，唯一约束保证 tenant+Usage 只投影一次。
  INSERT INTO finops_ledger_entries(
    finops_ledger_entry_id, event_id, tenant_id, entry_type, direction,
    amount_micros, currency, normalized_cny_micros, cny_normalization_status,
    price_evidence_status, price_snapshot_source, posting_source, quota_type,
    usage_ledger_id, finops_adjustment_id, source_request_id, source_trace_id,
    operation_request_id, operation_trace_id, price_snapshot_id,
    price_schema_version, reversal_of_entry_id, actor_user_id,
    input_tokens, output_tokens, total_tokens, cache_tokens,
    model_id, provider_id, virtual_key_id, environment_id, project_id,
    application_id, estimated, status, created_at, updated_at
  )
  SELECT
    'fle_' || lower(hex(randomblob(16))),
    'flevt_' || lower(hex(randomblob(16))),
    NEW.tenant_id,
    'usage_charge',
    'debit',
    amount.amount_micros,
    NEW.currency,
    CASE WHEN NEW.currency='CNY' THEN amount.amount_micros ELSE NULL END,
    CASE WHEN NEW.currency='CNY' THEN 'normalized' ELSE 'unavailable' END,
    CASE
      WHEN snapshot.source='finops_unpriced' THEN 'unpriced'
      WHEN snapshot.source='finops_legacy_backfill' THEN 'legacy_amount_only'
      ELSE 'priced'
    END,
    snapshot.source,
    CASE WHEN json_extract(NEW.normalized_usage, '$.posting_source')='spool_replay'
      THEN 'spool_replay' ELSE 'direct' END,
    'paid',
    NEW.usage_ledger_id,
    NULL,
    NEW.request_id,
    request.trace_id,
    NEW.request_id,
    request.trace_id,
    snapshot.price_snapshot_id,
    snapshot.price_schema_version,
    NULL,
    NULL,
    NEW.input_tokens,
    NEW.output_tokens,
    NEW.total_tokens,
    NEW.cache_tokens,
    NEW.model_id,
    NEW.provider_id,
    NEW.virtual_key_id,
    NEW.environment_id,
    NEW.project_id,
    NEW.application_id,
    NEW.estimated,
    'posted',
    NEW.created_at,
    NEW.created_at
  FROM (
    SELECT
      CAST(parts.integer_text AS INTEGER) * 1000000
      + CASE WHEN parts.fraction_text=''
          THEN 0 ELSE CAST(substr(parts.fraction_text || '000000', 1, 6) AS INTEGER) END
        AS amount_micros
    FROM (
      SELECT
        CASE WHEN instr(value.amount_text, '.')=0
          THEN value.amount_text
          ELSE substr(value.amount_text, 1, instr(value.amount_text, '.')-1) END AS integer_text,
        CASE WHEN instr(value.amount_text, '.')=0
          THEN '' ELSE substr(value.amount_text, instr(value.amount_text, '.')+1) END AS fraction_text
      FROM (
        SELECT CASE
          WHEN json_type(NEW.normalized_usage, '$.cost_amount')='text'
            THEN json_extract(NEW.normalized_usage, '$.cost_amount')
          ELSE printf('%.6f', NEW.cost_amount)
        END AS amount_text
      ) value
    ) parts
  ) amount
  JOIN request_logs request
    ON request.tenant_id=NEW.tenant_id AND request.request_id=NEW.request_id
  JOIN price_snapshots snapshot
    ON snapshot.tenant_id=NEW.tenant_id
   AND snapshot.price_snapshot_id=COALESCE(
     (
       SELECT trusted.price_snapshot_id FROM price_snapshots trusted
       WHERE trusted.price_snapshot_id=NEW.price_snapshot_id
         AND trusted.tenant_id=NEW.tenant_id
         AND trusted.request_id=NEW.request_id
         AND trusted.currency=NEW.currency
         AND NULLIF(trim(trusted.price_schema_version), '') IS NOT NULL
         AND trusted.price_schema_version=NEW.price_schema_version
     ),
     (
       SELECT synthetic.price_snapshot_id FROM price_snapshots synthetic
       WHERE synthetic.tenant_id=NEW.tenant_id
         AND synthetic.source IN ('finops_unpriced', 'finops_legacy_backfill')
         AND json_extract(
           synthetic.billing_dimensions, '$.finops_usage_ledger_id'
         )=NEW.usage_ledger_id
     )
   );
END;

-- 最终门禁验证一一投影、价格证据、对象全集和 SQLite 内部完整性，失败会回滚整个 0041。
CREATE TEMP TABLE prd051_up_integrity_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd051_up_integrity_guard
BEFORE INSERT ON prd051_up_integrity_guard
FOR EACH ROW
WHEN NEW.issue_count != 0
BEGIN
  SELECT RAISE(ABORT, 'prd051_up_integrity_failed');
END;
-- 最终汇总一一投影、价格 owner、对象残留和 SQLite 完整性，失败时回滚整个 0041。
INSERT INTO prd051_up_integrity_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM usage_ledger)
    =(SELECT COUNT(*) FROM finops_ledger_entries WHERE entry_type='usage_charge')
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*)
     FROM usage_ledger usage
     LEFT JOIN finops_ledger_entries entry
       ON entry.tenant_id=usage.tenant_id
      AND entry.usage_ledger_id=usage.usage_ledger_id
      AND entry.entry_type='usage_charge'
     WHERE entry.finops_ledger_entry_id IS NULL
        OR entry.source_request_id!=usage.request_id
        OR entry.price_snapshot_id IS NULL
        OR entry.price_schema_version IS NULL)
  + (SELECT COUNT(*)
     FROM finops_ledger_entries entry
     LEFT JOIN price_snapshots snapshot
       ON snapshot.tenant_id=entry.tenant_id
      AND snapshot.price_snapshot_id=entry.price_snapshot_id
     WHERE entry.entry_type='usage_charge'
       AND (
         snapshot.price_snapshot_id IS NULL
         OR snapshot.request_id!=entry.source_request_id
         OR snapshot.currency!=entry.currency
         OR snapshot.price_schema_version!=entry.price_schema_version
         OR snapshot.source!=entry.price_snapshot_source
       ))
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='table' AND name IN ('finops_adjustments', 'finops_ledger_entries')
  )=2 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='index' AND name IN (
      'idx_audit_logs_finops_adjustment_unique',
      'idx_finops_ledger_tenant_sequence', 'idx_finops_ledger_tenant_time',
      'idx_finops_ledger_source_request', 'idx_finops_ledger_price_snapshot',
      'idx_finops_ledger_reversal_target', 'idx_finops_ledger_type_direction',
      'idx_finops_adjustments_tenant_time', 'idx_finops_adjustments_reversal_target',
      'uq_users_tenant_user_prd051', 'uq_usage_ledger_tenant_usage_prd051',
      'uq_price_snapshots_tenant_snapshot_prd051',
      'uq_models_tenant_model_prd051', 'uq_providers_tenant_provider_prd051',
      'uq_virtual_keys_tenant_key_prd051',
      'uq_environments_tenant_environment_prd051',
      'uq_projects_tenant_project_prd051',
      'uq_applications_tenant_application_prd051',
      'uq_price_snapshots_finops_usage_prd051'
    )
  )=19 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='trigger' AND name IN (
      'trg_audit_logs_owner_insert', 'trg_audit_logs_immutable_update',
      'trg_audit_logs_immutable_delete', 'trg_audit_logs_prd050_conflict_insert',
      'trg_audit_logs_prd050_rowid_insert',
      'trg_finops_adjustments_owner_insert_prd051',
      'trg_finops_adjustments_conflict_insert_prd051',
      'trg_finops_adjustments_rowid_insert_prd051',
      'trg_finops_adjustments_immutable_update_prd051',
      'trg_finops_adjustments_immutable_delete_prd051',
      'trg_finops_ledger_owner_insert_prd051',
      'trg_finops_ledger_conflict_insert_prd051',
      'trg_finops_ledger_immutable_update_prd051',
      'trg_finops_ledger_immutable_delete_prd051',
      'trg_price_snapshots_owner_insert_prd051',
      'trg_price_snapshots_conflict_insert_prd051',
      'trg_price_snapshots_rowid_insert_prd051',
      'trg_price_snapshots_immutable_update_prd051',
      'trg_price_snapshots_immutable_delete_prd051',
      'trg_usage_ledger_finops_owner_insert_prd051',
      'trg_usage_ledger_finops_amount_insert_prd051',
      'trg_usage_ledger_finops_conflict_insert_prd051',
      'trg_usage_ledger_finops_projection_insert_prd051'
    )
  )=23 THEN 0 ELSE 1 END
  + CASE WHEN EXISTS (
    SELECT 1 FROM sqlite_master
    WHERE type='table' AND name='audit_logs'
      AND sql LIKE '%finops.adjustment_created%'
      AND sql LIKE '%finops_adjustment%'
  ) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER trg_prd051_up_integrity_guard;
DROP TABLE prd051_up_integrity_guard;
DROP TABLE prd051_usage_backfill_source;

COMMIT;
PRAGMA foreign_keys = ON;
