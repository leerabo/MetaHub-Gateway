-- 迁移职责：为 PRD-057 建立 Webhook 死信、持久 replay lease、逻辑归档和固定过期策略。
-- 安全边界：死信只保存已经过严格校验的事件引用与固定失败摘要，不新增 Endpoint URL、签名、
-- Secret、Authorization、Prompt、Response、Token、Key 或接收端响应正文。
BEGIN IMMEDIATE;

-- 复合 owner 外键需要同列顺序的唯一键；既有全局主键语义保持不变。
CREATE UNIQUE INDEX idx_webhook_deliveries_tenant_delivery_prd057
  ON webhook_deliveries(tenant_id, webhook_delivery_id);
CREATE UNIQUE INDEX idx_webhook_endpoints_tenant_endpoint_prd057
  ON webhook_endpoints(tenant_id, webhook_endpoint_id);

-- DeadLetterEvent 是 delivery 传输状态之外的治理生命周期；failed 仍保留以兼容 PRD-025/042。
CREATE TABLE webhook_dead_letter_events (
  dead_letter_event_id TEXT PRIMARY KEY CHECK (
    length(dead_letter_event_id)=37
    AND substr(dead_letter_event_id,1,5)='whdl_'
    AND substr(dead_letter_event_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  webhook_delivery_id TEXT NOT NULL,
  webhook_endpoint_id TEXT NOT NULL,
  event_id TEXT NOT NULL CHECK (
    length(event_id) BETWEEN 8 AND 128 AND instr(event_id,char(0))=0
  ),
  event_type TEXT NOT NULL CHECK (
    length(event_type) BETWEEN 3 AND 120 AND instr(event_type,char(0))=0
  ),
  status TEXT NOT NULL CHECK (
    status IN ('dead_letter','replay_pending','replayed','archived','expired')
  ),
  failure_error_code TEXT CHECK (
    failure_error_code IS NULL OR (
      length(failure_error_code) BETWEEN 3 AND 120 AND instr(failure_error_code,char(0))=0
    )
  ),
  failure_http_status INTEGER CHECK (
    failure_http_status IS NULL OR (
      typeof(failure_http_status)='integer' AND failure_http_status BETWEEN 100 AND 599
    )
  ),
  failed_at TEXT NOT NULL CHECK (unixepoch(failed_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK (
    unixepoch(expires_at) IS NOT NULL AND unixepoch(expires_at)>unixepoch(failed_at)
  ),
  current_replay_delivery_id TEXT,
  replay_requested_by TEXT,
  replay_requested_at TEXT CHECK (
    replay_requested_at IS NULL OR unixepoch(replay_requested_at) IS NOT NULL
  ),
  replay_dispatch_attempts INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(replay_dispatch_attempts)='integer' AND replay_dispatch_attempts BETWEEN 0 AND 1000000
  ),
  replay_next_attempt_at TEXT CHECK (
    replay_next_attempt_at IS NULL OR unixepoch(replay_next_attempt_at) IS NOT NULL
  ),
  replay_claim_token TEXT CHECK (
    replay_claim_token IS NULL OR (
      length(replay_claim_token) BETWEEN 16 AND 128 AND instr(replay_claim_token,char(0))=0
    )
  ),
  replay_lease_owner_id TEXT CHECK (
    replay_lease_owner_id IS NULL OR (
      length(replay_lease_owner_id) BETWEEN 1 AND 128 AND instr(replay_lease_owner_id,char(0))=0
    )
  ),
  replay_lease_expires_at TEXT CHECK (
    replay_lease_expires_at IS NULL OR unixepoch(replay_lease_expires_at) IS NOT NULL
  ),
  last_replay_error_code TEXT CHECK (
    last_replay_error_code IS NULL OR (
      length(last_replay_error_code) BETWEEN 3 AND 120 AND instr(last_replay_error_code,char(0))=0
    )
  ),
  replayed_at TEXT CHECK (replayed_at IS NULL OR unixepoch(replayed_at) IS NOT NULL),
  archived_by TEXT,
  archived_at TEXT CHECK (archived_at IS NULL OR unixepoch(archived_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK (
    unixepoch(updated_at) IS NOT NULL AND unixepoch(updated_at)>=unixepoch(created_at)
  ),
  UNIQUE (tenant_id, webhook_delivery_id),
  UNIQUE (tenant_id, dead_letter_event_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, webhook_delivery_id)
    REFERENCES webhook_deliveries(tenant_id, webhook_delivery_id),
  FOREIGN KEY (tenant_id, webhook_endpoint_id)
    REFERENCES webhook_endpoints(tenant_id, webhook_endpoint_id),
  FOREIGN KEY (current_replay_delivery_id) REFERENCES webhook_deliveries(webhook_delivery_id),
  FOREIGN KEY (replay_requested_by) REFERENCES users(user_id),
  FOREIGN KEY (archived_by) REFERENCES users(user_id),
  CHECK (
    (replay_claim_token IS NULL AND replay_lease_owner_id IS NULL AND replay_lease_expires_at IS NULL)
    OR
    (status='replay_pending' AND replay_claim_token IS NOT NULL
      AND replay_lease_owner_id IS NOT NULL AND replay_lease_expires_at IS NOT NULL)
  ),
  CHECK (
    (current_replay_delivery_id IS NULL AND replay_requested_by IS NULL AND replay_requested_at IS NULL
      AND replayed_at IS NULL)
    OR
    (current_replay_delivery_id IS NOT NULL AND replay_requested_by IS NOT NULL
      AND replay_requested_at IS NOT NULL)
  ),
  CHECK (
    (status='replayed' AND current_replay_delivery_id IS NOT NULL AND replayed_at IS NOT NULL
      AND archived_by IS NULL AND archived_at IS NULL)
    OR
    (status='archived' AND archived_by IS NOT NULL AND archived_at IS NOT NULL)
    OR
    (status IN ('dead_letter','replay_pending')
      AND replayed_at IS NULL AND archived_by IS NULL AND archived_at IS NULL)
    OR
    (status='expired' AND archived_by IS NULL AND archived_at IS NULL)
  )
);

CREATE INDEX idx_webhook_dead_letters_tenant_status_time_prd057
  ON webhook_dead_letter_events(tenant_id, status, failed_at DESC, dead_letter_event_id DESC);
CREATE INDEX idx_webhook_dead_letters_tenant_endpoint_status_prd057
  ON webhook_dead_letter_events(tenant_id, webhook_endpoint_id, status, failed_at DESC);
CREATE INDEX idx_webhook_dead_letters_tenant_event_prd057
  ON webhook_dead_letter_events(tenant_id, event_type, failed_at DESC);
CREATE INDEX idx_webhook_dead_letters_replay_due_prd057
  ON webhook_dead_letter_events(status, replay_next_attempt_at, replay_lease_expires_at, failed_at)
  WHERE status='replay_pending';
CREATE INDEX idx_webhook_dead_letters_expiry_prd057
  ON webhook_dead_letter_events(status, expires_at)
  WHERE status IN ('dead_letter','replayed');

-- 0046 及更早的失败 original 必须成为可治理死信；最新历史 replay 决定恢复后的治理状态。
-- 这里使用稳定的 created_at + delivery_id 排序，避免升级时把已成功 replay 误标为待重放，
-- 也避免遗漏 PRD-042 已提交但进程在 HTTP 前退出的 pending replay intent。
INSERT INTO webhook_dead_letter_events(
  dead_letter_event_id,tenant_id,webhook_delivery_id,webhook_endpoint_id,event_id,event_type,
  status,failure_error_code,failure_http_status,failed_at,expires_at,
  current_replay_delivery_id,replay_requested_by,replay_requested_at,
  replay_next_attempt_at,last_replay_error_code,replayed_at,created_at,updated_at
)
SELECT
  'whdl_' || substr(delivery.webhook_delivery_id,5),
  delivery.tenant_id,
  delivery.webhook_delivery_id,
  delivery.webhook_endpoint_id,
  delivery.event_id,
  delivery.event_type,
  CASE latest_replay.status
    WHEN 'pending' THEN 'replay_pending'
    WHEN 'success' THEN 'replayed'
    ELSE 'dead_letter'
  END,
  delivery.error_code,
  delivery.http_status,
  delivery.updated_at,
  strftime('%Y-%m-%dT%H:%M:%SZ',delivery.updated_at,'+30 days'),
  latest_replay.webhook_delivery_id,
  latest_replay.replayed_by,
  latest_replay.replayed_at,
  CASE WHEN latest_replay.status='pending' THEN latest_replay.updated_at END,
  CASE WHEN latest_replay.status='failed' THEN latest_replay.error_code END,
  CASE WHEN latest_replay.status='success' THEN latest_replay.updated_at END,
  delivery.updated_at,
  COALESCE(latest_replay.updated_at,delivery.updated_at)
FROM webhook_deliveries delivery
LEFT JOIN webhook_deliveries latest_replay
  ON latest_replay.webhook_delivery_id=(
    SELECT replay.webhook_delivery_id
    FROM webhook_deliveries replay
    WHERE replay.tenant_id=delivery.tenant_id
      AND replay.webhook_endpoint_id=delivery.webhook_endpoint_id
      AND replay.delivery_kind='replay'
      AND replay.replayed_from_delivery_id=delivery.webhook_delivery_id
    ORDER BY replay.created_at DESC,replay.webhook_delivery_id DESC
    LIMIT 1
  )
WHERE delivery.status='failed' AND delivery.delivery_kind='original';

-- 任一发送实现把 delivery 原子终结为 failed 时，同一事务自动形成唯一 DeadLetterEvent。
CREATE TRIGGER trg_webhook_dead_letters_delivery_failed_update_prd057
AFTER UPDATE OF status ON webhook_deliveries FOR EACH ROW
WHEN NEW.status='failed' AND NEW.delivery_kind='original' AND OLD.status IS NOT NEW.status
BEGIN
  -- 更新终结路径只复制安全失败摘要和租户引用，不复制 Endpoint URL、Secret 或响应正文。
  INSERT INTO webhook_dead_letter_events(
    dead_letter_event_id,tenant_id,webhook_delivery_id,webhook_endpoint_id,event_id,event_type,
    status,failure_error_code,failure_http_status,failed_at,expires_at,created_at,updated_at
  ) VALUES(
    'whdl_' || substr(NEW.webhook_delivery_id,5),NEW.tenant_id,NEW.webhook_delivery_id,
    NEW.webhook_endpoint_id,NEW.event_id,NEW.event_type,'dead_letter',NEW.error_code,NEW.http_status,
    NEW.updated_at,strftime('%Y-%m-%dT%H:%M:%SZ',NEW.updated_at,'+30 days'),NEW.updated_at,NEW.updated_at
  );
END;

CREATE TRIGGER trg_webhook_dead_letters_delivery_failed_insert_prd057
AFTER INSERT ON webhook_deliveries FOR EACH ROW
WHEN NEW.status='failed' AND NEW.delivery_kind='original'
BEGIN
  -- 直接插入最终失败 delivery 时同样生成唯一根死信，保持两个写入入口事务语义一致。
  INSERT INTO webhook_dead_letter_events(
    dead_letter_event_id,tenant_id,webhook_delivery_id,webhook_endpoint_id,event_id,event_type,
    status,failure_error_code,failure_http_status,failed_at,expires_at,created_at,updated_at
  ) VALUES(
    'whdl_' || substr(NEW.webhook_delivery_id,5),NEW.tenant_id,NEW.webhook_delivery_id,
    NEW.webhook_endpoint_id,NEW.event_id,NEW.event_type,'dead_letter',NEW.error_code,NEW.http_status,
    NEW.updated_at,strftime('%Y-%m-%dT%H:%M:%SZ',NEW.updated_at,'+30 days'),NEW.updated_at,NEW.updated_at
  );
END;

-- 插入只能引用同租户 failed delivery；当前 replay 与操作者也必须属于同一 tenant/Endpoint。
CREATE TRIGGER trg_webhook_dead_letters_owner_insert_prd057
BEFORE INSERT ON webhook_dead_letter_events FOR EACH ROW
WHEN NEW.dead_letter_event_id!='whdl_' || substr(NEW.webhook_delivery_id,5)
  OR NOT EXISTS (
    SELECT 1 FROM webhook_deliveries source
    WHERE source.tenant_id=NEW.tenant_id
      AND source.webhook_delivery_id=NEW.webhook_delivery_id
      AND source.webhook_endpoint_id=NEW.webhook_endpoint_id
      AND source.event_id=NEW.event_id AND source.event_type=NEW.event_type
      AND source.status='failed' AND source.delivery_kind='original'
  )
  OR (
    NEW.current_replay_delivery_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM webhook_deliveries replay
      WHERE replay.tenant_id=NEW.tenant_id
        AND replay.webhook_endpoint_id=NEW.webhook_endpoint_id
        AND replay.webhook_delivery_id=NEW.current_replay_delivery_id
        AND replay.delivery_kind='replay'
        AND replay.replayed_from_delivery_id=NEW.webhook_delivery_id
        AND replay.event_id=NEW.event_id AND replay.event_type=NEW.event_type
        AND replay.replayed_by=NEW.replay_requested_by
        AND replay.replayed_at=NEW.replay_requested_at
    )
  )
  OR (
    NEW.replay_requested_by IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM users actor
      WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.replay_requested_by
    )
  )
  OR (
    NEW.archived_by IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM users actor
      WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.archived_by
    )
  )
BEGIN SELECT RAISE(ABORT,'webhook_dead_letter_owner_mismatch'); END;

-- 业务身份和首次失败证据不可改；只有受控生命周期/lease 字段允许 UPDATE。
CREATE TRIGGER trg_webhook_dead_letters_identity_immutable_prd057
BEFORE UPDATE ON webhook_dead_letter_events FOR EACH ROW
WHEN NEW.dead_letter_event_id IS NOT OLD.dead_letter_event_id
  OR NEW.tenant_id IS NOT OLD.tenant_id
  OR NEW.webhook_delivery_id IS NOT OLD.webhook_delivery_id
  OR NEW.webhook_endpoint_id IS NOT OLD.webhook_endpoint_id
  OR NEW.event_id IS NOT OLD.event_id OR NEW.event_type IS NOT OLD.event_type
  OR NEW.failure_error_code IS NOT OLD.failure_error_code
  OR NEW.failure_http_status IS NOT OLD.failure_http_status
  OR NEW.failed_at IS NOT OLD.failed_at OR NEW.expires_at IS NOT OLD.expires_at
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'webhook_dead_letter_identity_immutable'); END;

-- 状态只允许沿设计状态机前进或在 replay 终态失败时回到 dead_letter。
CREATE TRIGGER trg_webhook_dead_letters_state_transition_prd057
BEFORE UPDATE OF status ON webhook_dead_letter_events FOR EACH ROW
WHEN NEW.status IS NOT OLD.status AND NOT (
  (OLD.status='dead_letter' AND NEW.status IN ('replay_pending','archived','expired'))
  OR (OLD.status='replay_pending' AND NEW.status IN ('replayed','dead_letter'))
  OR (OLD.status='replayed' AND NEW.status IN ('archived','expired'))
)
BEGIN SELECT RAISE(ABORT,'webhook_dead_letter_state_invalid'); END;

-- UPDATE 后重新校验 replay/actor owner，防止合法来源行被挂到其他租户资源。
CREATE TRIGGER trg_webhook_dead_letters_owner_update_prd057
BEFORE UPDATE ON webhook_dead_letter_events FOR EACH ROW
WHEN (
    NEW.current_replay_delivery_id IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM webhook_deliveries replay
      WHERE replay.tenant_id=NEW.tenant_id
        AND replay.webhook_endpoint_id=NEW.webhook_endpoint_id
        AND replay.webhook_delivery_id=NEW.current_replay_delivery_id
        AND replay.delivery_kind='replay'
        AND replay.replayed_from_delivery_id=NEW.webhook_delivery_id
        AND replay.event_id=NEW.event_id AND replay.event_type=NEW.event_type
        AND replay.replayed_by=NEW.replay_requested_by
        AND replay.replayed_at=NEW.replay_requested_at
    )
  )
  OR (
    NEW.replay_requested_by IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM users actor
      WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.replay_requested_by
    )
  )
  OR (
    NEW.archived_by IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM users actor
      WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.archived_by
    )
  )
BEGIN SELECT RAISE(ABORT,'webhook_dead_letter_owner_mismatch'); END;

CREATE TRIGGER trg_webhook_dead_letters_delete_prd057
BEFORE DELETE ON webhook_dead_letter_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'webhook_dead_letter_immutable'); END;

CREATE TRIGGER trg_webhook_dead_letters_conflict_insert_prd057
BEFORE INSERT ON webhook_dead_letter_events FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM webhook_dead_letter_events current
  WHERE current.dead_letter_event_id=NEW.dead_letter_event_id
     OR (current.tenant_id=NEW.tenant_id AND current.webhook_delivery_id=NEW.webhook_delivery_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'webhook_dead_letter_immutable'); END;

CREATE TRIGGER trg_webhook_dead_letters_rowid_insert_prd057
AFTER INSERT ON webhook_dead_letter_events FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'webhook_dead_letter_immutable'); END;

-- Audit 以保真重建扩展归档 action/object；旧证据的 rowid、NULL 和全部列必须原样保留。
CREATE TABLE audit_logs_prd057_new (
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
      'webhook_dead_letter.archived',
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
      'cost_anomaly.temporary_rate_limit_released',
      'error_map.created', 'error_map.updated'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'webhook_dead_letter',
      'usage_export_job', 'policy', 'policy_snapshot', 'policy_explanation',
      'routing_policy', 'model_quality_score', 'cache_affinity',
      'finops_adjustment', 'cost_anomaly', 'provider_error_map'
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

-- 按原 rowid 和全部公开字段复制审计事实，新增约束不能改变历史记录顺序或空值语义。
INSERT INTO audit_logs_prd057_new(
  rowid,audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
  action,object_type,object_id,object_name,before_summary_json,
  after_summary_json,changed_fields_json,client_ip,user_agent,
  request_id,trace_id,created_at
)
SELECT
  rowid,audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
  action,object_type,object_id,object_name,before_summary_json,
  after_summary_json,changed_fields_json,client_ip,user_agent,
  request_id,trace_id,created_at
FROM audit_logs;

CREATE TEMP TABLE prd057_audit_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  null_mismatch_count INTEGER NOT NULL,
  row_difference_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd057_audit_copy_guard
BEFORE INSERT ON prd057_audit_copy_guard FOR EACH ROW
WHEN NEW.source_count!=NEW.copied_count
  OR NEW.null_mismatch_count!=0 OR NEW.row_difference_count!=0
BEGIN SELECT RAISE(ABORT,'prd057_audit_copy_failed'); END;
-- 在删除旧表前原子核对总数、NULL 分布与双向差集，任何不一致都中止迁移。
INSERT INTO prd057_audit_copy_guard(
  source_count,copied_count,null_mismatch_count,row_difference_count
)
SELECT
  (SELECT COUNT(*) FROM audit_logs),
  (SELECT COUNT(*) FROM audit_logs_prd057_new),
  CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd057_new WHERE object_name IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd057_new WHERE client_ip IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd057_new WHERE user_agent IS NULL) THEN 0 ELSE 1 END,
  (SELECT COUNT(*) FROM (
    SELECT rowid,* FROM audit_logs
    EXCEPT SELECT rowid,* FROM audit_logs_prd057_new
  ))
  + (SELECT COUNT(*) FROM (
    SELECT rowid,* FROM audit_logs_prd057_new
    EXCEPT SELECT rowid,* FROM audit_logs
  ));
DROP TRIGGER trg_prd057_audit_copy_guard;
DROP TABLE prd057_audit_copy_guard;
DROP TABLE audit_logs;
-- 只有复制守卫通过后才切换新表名，避免半迁移 schema 对外可见。
ALTER TABLE audit_logs_prd057_new RENAME TO audit_logs;

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

-- Owner trigger 保留既有 FinOps/成本异常/error-map 约束，并绑定死信归档 actor 与 tenant。
CREATE TRIGGER trg_audit_logs_owner_insert
BEFORE INSERT ON audit_logs FOR EACH ROW
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
    'cost_anomaly.acknowledged','cost_anomaly.dismissed',
    'cost_anomaly.temporary_rate_limit_applied',
    'cost_anomaly.temporary_rate_limit_released'
  )
  AND (
    NEW.object_type!='cost_anomaly'
    OR NOT EXISTS (
      SELECT 1 FROM cost_anomaly_actions action
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
          WHEN 'apply_temporary_rate_limit' THEN 'cost_anomaly.temporary_rate_limit_applied'
          WHEN 'release_temporary_rate_limit' THEN 'cost_anomaly.temporary_rate_limit_released'
        END=NEW.action
    )
  )
)
OR (
  NEW.object_type='cost_anomaly'
  AND NEW.action NOT IN (
    'cost_anomaly.acknowledged','cost_anomaly.dismissed',
    'cost_anomaly.temporary_rate_limit_applied',
    'cost_anomaly.temporary_rate_limit_released'
  )
)
OR (
  NEW.action IN ('error_map.created','error_map.updated')
  AND (
    NEW.object_type!='provider_error_map'
    OR NOT EXISTS (
      SELECT 1 FROM provider_error_map_events event
      JOIN provider_error_map_versions version
        ON version.tenant_id=event.tenant_id
       AND version.provider_error_map_version_id=event.provider_error_map_version_id
      JOIN provider_error_maps map
        ON map.tenant_id=event.tenant_id
       AND map.provider_error_map_id=event.provider_error_map_id
       AND map.current_version=event.rule_version
      WHERE event.tenant_id=NEW.tenant_id
        AND event.provider_error_map_id=NEW.object_id
        AND event.actor_user_id=NEW.actor_user_id
        AND event.request_id=NEW.request_id
        AND event.trace_id=NEW.trace_id
        AND (
          (NEW.action='error_map.created' AND event.operation='create')
          OR (NEW.action='error_map.updated' AND event.operation IN ('update','enable','disable'))
        )
    )
  )
)
OR (
  NEW.object_type='provider_error_map'
  AND NEW.action NOT IN ('error_map.created','error_map.updated')
)
OR (
  NEW.action='webhook_dead_letter.archived'
  AND (
    NEW.object_type!='webhook_dead_letter'
    OR NOT EXISTS (
      SELECT 1 FROM webhook_dead_letter_events dead_letter
      WHERE dead_letter.tenant_id=NEW.tenant_id
        AND dead_letter.dead_letter_event_id=NEW.object_id
        AND dead_letter.status='archived'
        AND dead_letter.archived_by=NEW.actor_user_id
    )
  )
)
OR (
  NEW.object_type='webhook_dead_letter'
  AND NEW.action!='webhook_dead_letter.archived'
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;

CREATE TRIGGER trg_audit_logs_immutable_update
BEFORE UPDATE ON audit_logs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_immutable_delete
BEFORE DELETE ON audit_logs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_prd050_conflict_insert
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM audit_logs current
  WHERE current.audit_log_id=NEW.audit_log_id
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
    OR (
      NEW.action IN ('policy.dry_run_executed','model_quality_score.updated')
      AND current.action=NEW.action AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
    OR (
      NEW.action='finops.adjustment_created'
      AND current.action='finops.adjustment_created'
      AND current.tenant_id=NEW.tenant_id AND current.object_id=NEW.object_id
    )
    OR (
      NEW.action IN (
        'cost_anomaly.acknowledged','cost_anomaly.dismissed',
        'cost_anomaly.temporary_rate_limit_applied',
        'cost_anomaly.temporary_rate_limit_released'
      )
      AND current.action=NEW.action AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
    OR (
      NEW.action IN ('error_map.created','error_map.updated')
      AND current.action=NEW.action AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
)
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_prd050_rowid_insert
AFTER INSERT ON audit_logs FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;

COMMIT;
