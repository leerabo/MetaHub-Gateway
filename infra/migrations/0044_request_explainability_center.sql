-- 迁移职责：为 PRD-054 建立请求解释访问审计、可恢复查看事件 outbox 与请求级 Webhook 关联索引。
-- 边界：迁移只扩展控制面读模型和索引，不修改 Gateway 热路径事实，也不保存 Prompt、Response、Key 或 URL。
BEGIN IMMEDIATE;

-- RequestExplanation 同时承担一次敏感详情读取的不可变审计事实和稳定 Webhook source outbox。
CREATE TABLE request_explanations (
  request_explanation_id TEXT PRIMARY KEY CHECK (
    length(request_explanation_id)=36
    AND substr(request_explanation_id, 1, 4)='rxe_'
    AND substr(request_explanation_id, 5) NOT GLOB '*[^0-9a-f]*'
  ),
  event_id TEXT NOT NULL UNIQUE CHECK (
    length(event_id)=38
    AND substr(event_id, 1, 6)='rxevt_'
    AND substr(event_id, 7) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  source_request_id TEXT NOT NULL CHECK (
    length(source_request_id) BETWEEN 1 AND 128
    AND source_request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  source_trace_id TEXT NOT NULL CHECK (
    length(source_trace_id) BETWEEN 1 AND 128
    AND source_trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  actor_user_id TEXT NOT NULL,
  actor_session_id TEXT NOT NULL,
  source_status TEXT NOT NULL CHECK (source_status IN ('success', 'failed')),
  status TEXT NOT NULL CHECK (status IN ('complete', 'partial')),
  evidence_version TEXT NOT NULL DEFAULT 'request_explanation_v1'
    CHECK (evidence_version='request_explanation_v1'),
  section_status_json TEXT NOT NULL CHECK (
    json_valid(section_status_json) AND json_type(section_status_json)='object'
  ),
  missing_sections_json TEXT NOT NULL CHECK (
    json_valid(missing_sections_json) AND json_type(missing_sections_json)='array'
  ),
  truncated_sections_json TEXT NOT NULL CHECK (
    json_valid(truncated_sections_json) AND json_type(truncated_sections_json)='array'
  ),
  source_created_at TEXT NOT NULL CHECK (unixepoch(source_created_at) IS NOT NULL),
  source_updated_at TEXT NOT NULL CHECK (
    unixepoch(source_updated_at) IS NOT NULL
    AND unixepoch(source_updated_at)>=unixepoch(source_created_at)
  ),
  operation_request_id TEXT NOT NULL CHECK (
    length(operation_request_id) BETWEEN 1 AND 128
    AND operation_request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  operation_trace_id TEXT NOT NULL CHECK (
    length(operation_trace_id) BETWEEN 1 AND 128
    AND operation_trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  event_dispatch_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (event_dispatch_status IN ('pending', 'processing', 'dispatched', 'failed')),
  event_dispatch_attempts INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(event_dispatch_attempts)='integer'
    AND event_dispatch_attempts BETWEEN 0 AND 10
  ),
  event_next_attempt_at TEXT,
  event_dispatched_at TEXT,
  event_claim_token TEXT CHECK (
    event_claim_token IS NULL
    OR (length(event_claim_token) BETWEEN 16 AND 128 AND instr(event_claim_token, char(0))=0)
  ),
  event_lease_owner_id TEXT CHECK (
    event_lease_owner_id IS NULL
    OR (length(event_lease_owner_id) BETWEEN 1 AND 128 AND instr(event_lease_owner_id, char(0))=0)
  ),
  event_lease_expires_at TEXT,
  event_last_error_code TEXT CHECK (
    event_last_error_code IS NULL OR event_last_error_code IN (
      'webhook.event_type_invalid',
      'webhook.payload_invalid',
      'webhook.payload_sensitive',
      'webhook.delivery_unavailable',
      'request_explanation.event_unavailable'
    )
  ),
  viewed_at TEXT NOT NULL CHECK (unixepoch(viewed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK (unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, source_request_id) REFERENCES request_logs(tenant_id, request_id),
  FOREIGN KEY (tenant_id, actor_user_id) REFERENCES users(tenant_id, user_id),
  FOREIGN KEY (actor_session_id) REFERENCES sessions(session_id),
  UNIQUE (tenant_id, request_explanation_id),
  UNIQUE (tenant_id, event_id),
  CHECK (viewed_at=created_at),
  CHECK (
    (event_dispatch_status='pending'
      AND event_dispatch_attempts BETWEEN 0 AND 9
      AND event_next_attempt_at IS NOT NULL
      AND unixepoch(event_next_attempt_at) IS NOT NULL
      AND event_dispatched_at IS NULL
      AND event_claim_token IS NULL AND event_lease_owner_id IS NULL
      AND event_lease_expires_at IS NULL)
    OR (event_dispatch_status='processing'
      AND event_dispatch_attempts BETWEEN 1 AND 10
      AND event_next_attempt_at IS NULL AND event_dispatched_at IS NULL
      AND event_claim_token IS NOT NULL AND event_lease_owner_id IS NOT NULL
      AND event_lease_expires_at IS NOT NULL
      AND unixepoch(event_lease_expires_at)>unixepoch(updated_at))
    OR (event_dispatch_status='dispatched'
      AND event_dispatched_at IS NOT NULL
      AND unixepoch(event_dispatched_at) IS NOT NULL
      AND event_next_attempt_at IS NULL AND event_claim_token IS NULL
      AND event_lease_owner_id IS NULL AND event_lease_expires_at IS NULL
      AND event_last_error_code IS NULL)
    OR (event_dispatch_status='failed'
      AND event_dispatch_attempts=10 AND event_dispatched_at IS NULL
      AND event_next_attempt_at IS NULL AND event_claim_token IS NULL
      AND event_lease_owner_id IS NULL AND event_lease_expires_at IS NULL
      AND event_last_error_code IS NOT NULL)
  )
);

-- 请求关联表只保存索引化关联，delivery 的可变状态仍以 webhook_deliveries 为唯一权威。
CREATE TABLE request_webhook_delivery_links (
  request_webhook_delivery_link_id TEXT PRIMARY KEY CHECK (
    length(request_webhook_delivery_link_id)=37
    AND substr(request_webhook_delivery_link_id, 1, 5)='rwdl_'
    AND substr(request_webhook_delivery_link_id, 6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  source_request_id TEXT NOT NULL CHECK (
    length(source_request_id) BETWEEN 1 AND 128
    AND source_request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  webhook_delivery_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (
    length(event_type) BETWEEN 1 AND 128 AND instr(event_type, char(0))=0
  ),
  delivery_kind TEXT NOT NULL CHECK (delivery_kind IN ('original', 'replay')),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY (tenant_id, source_request_id) REFERENCES request_logs(tenant_id, request_id),
  FOREIGN KEY (webhook_delivery_id) REFERENCES webhook_deliveries(webhook_delivery_id),
  UNIQUE (tenant_id, webhook_delivery_id)
);

-- 访问审计、outbox 和请求关联均以 tenant 为首列，禁止跨租户后过滤。
CREATE INDEX idx_request_explanations_request_time_prd054
  ON request_explanations(tenant_id, source_request_id, viewed_at DESC, request_explanation_id DESC);
CREATE INDEX idx_request_explanations_actor_time_prd054
  ON request_explanations(tenant_id, actor_user_id, viewed_at DESC, request_explanation_id DESC);
CREATE INDEX idx_request_explanations_pending_event_prd054
  ON request_explanations(
    event_dispatch_status, event_next_attempt_at, event_lease_expires_at, created_at,
    request_explanation_id
  )
  WHERE event_dispatch_status IN ('pending', 'processing');
CREATE INDEX idx_request_webhook_links_request_time_prd054
  ON request_webhook_delivery_links(
    tenant_id, source_request_id, created_at DESC, request_webhook_delivery_link_id DESC
  );
CREATE INDEX idx_budget_scope_events_request_prd054
  ON budget_scope_events(tenant_id, request_id, created_at, budget_scope_event_id);
CREATE INDEX idx_usage_ledger_request_prd054
  ON usage_ledger(tenant_id, request_id, created_at, usage_ledger_id);

-- 插入时从数据库事实复核 source、actor 和 session 归属，并冻结 11 个详情区块的完整状态。
CREATE TRIGGER trg_request_explanations_owner_insert_prd054
BEFORE INSERT ON request_explanations
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM request_logs request
  WHERE request.tenant_id=NEW.tenant_id
    AND request.request_id=NEW.source_request_id
    AND request.trace_id=NEW.source_trace_id
    AND request.status=NEW.source_status
    AND request.created_at=NEW.source_created_at
    AND coalesce(request.updated_at, request.created_at)=NEW.source_updated_at
)
OR NOT EXISTS (
  SELECT 1 FROM users user
  WHERE user.tenant_id=NEW.tenant_id AND user.user_id=NEW.actor_user_id
)
OR NOT EXISTS (
  SELECT 1 FROM sessions session
  WHERE session.tenant_id=NEW.tenant_id
    AND session.user_id=NEW.actor_user_id
    AND session.session_id=NEW.actor_session_id
)
OR (SELECT COUNT(*) FROM json_each(NEW.section_status_json))!=11
OR EXISTS (
  SELECT 1 FROM json_each(NEW.section_status_json) section
  WHERE section.key NOT IN (
    'authentication', 'rate_limit', 'budget', 'dlp', 'policy', 'routing',
    'keypool', 'provider_response', 'usage_and_finops', 'logs', 'webhooks'
  )
  OR section.type!='text'
  OR section.value NOT IN ('complete', 'partial', 'not_recorded', 'not_applicable', 'pending')
)
OR EXISTS (
  SELECT 1 FROM json_each(NEW.missing_sections_json) section
  WHERE section.type!='text' OR section.value NOT IN (
    'authentication', 'rate_limit', 'budget', 'dlp', 'policy', 'routing',
    'keypool', 'provider_response', 'usage_and_finops', 'logs', 'webhooks'
  )
)
OR EXISTS (
  SELECT 1 FROM json_each(NEW.truncated_sections_json) section
  WHERE section.type!='text' OR section.value NOT IN (
    'authentication', 'rate_limit', 'budget', 'dlp', 'policy', 'routing',
    'keypool', 'provider_response', 'usage_and_finops', 'logs', 'webhooks'
  )
)
OR (SELECT COUNT(*) FROM json_each(NEW.missing_sections_json))
  !=(SELECT COUNT(DISTINCT value) FROM json_each(NEW.missing_sections_json))
OR (SELECT COUNT(*) FROM json_each(NEW.truncated_sections_json))
  !=(SELECT COUNT(DISTINCT value) FROM json_each(NEW.truncated_sections_json))
OR EXISTS (
  SELECT 1
  FROM json_each(NEW.missing_sections_json) missing
  JOIN json_each(NEW.truncated_sections_json) truncated ON truncated.value=missing.value
)
OR (
  NEW.status='complete'
  AND (
    json_array_length(NEW.missing_sections_json)!=0
    OR json_array_length(NEW.truncated_sections_json)!=0
  )
)
OR (
  NEW.status='partial'
  AND json_array_length(NEW.missing_sections_json)=0
  AND json_array_length(NEW.truncated_sections_json)=0
)
BEGIN
  SELECT RAISE(ABORT, 'request_explanation_owner_mismatch');
END;

-- 冲突门禁使 INSERT OR REPLACE 和显式 rowid 都不能覆盖已有访问审计或稳定事件。
CREATE TRIGGER trg_request_explanations_conflict_insert_prd054
BEFORE INSERT ON request_explanations
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM request_explanations current
  WHERE current.request_explanation_id=NEW.request_explanation_id
    OR current.event_id=NEW.event_id
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN
  SELECT RAISE(ABORT, 'request_explanation_immutable');
END;
CREATE TRIGGER trg_request_explanations_rowid_insert_prd054
AFTER INSERT ON request_explanations
FOR EACH ROW
WHEN NEW.rowid<0
BEGIN
  SELECT RAISE(ABORT, 'request_explanation_immutable');
END;

-- 访问事实不可改写；唯一允许的更新是带 CAS/lease 的 outbox 状态迁移。
CREATE TRIGGER trg_request_explanations_state_update_prd054
BEFORE UPDATE ON request_explanations
FOR EACH ROW
WHEN NOT (
  NEW.rowid=OLD.rowid
  AND NEW.request_explanation_id=OLD.request_explanation_id
  AND NEW.event_id=OLD.event_id
  AND NEW.tenant_id=OLD.tenant_id
  AND NEW.source_request_id=OLD.source_request_id
  AND NEW.source_trace_id=OLD.source_trace_id
  AND NEW.actor_user_id=OLD.actor_user_id
  AND NEW.actor_session_id=OLD.actor_session_id
  AND NEW.source_status=OLD.source_status
  AND NEW.status=OLD.status
  AND NEW.evidence_version=OLD.evidence_version
  AND NEW.section_status_json=OLD.section_status_json
  AND NEW.missing_sections_json=OLD.missing_sections_json
  AND NEW.truncated_sections_json=OLD.truncated_sections_json
  AND NEW.source_created_at=OLD.source_created_at
  AND NEW.source_updated_at=OLD.source_updated_at
  AND NEW.operation_request_id=OLD.operation_request_id
  AND NEW.operation_trace_id=OLD.operation_trace_id
  AND NEW.viewed_at=OLD.viewed_at
  AND NEW.created_at=OLD.created_at
  AND unixepoch(NEW.updated_at)>=unixepoch(OLD.updated_at)
  AND (
    (OLD.event_dispatch_status='pending' AND NEW.event_dispatch_status='processing'
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts+1
      AND unixepoch(OLD.event_next_attempt_at)<=unixepoch(NEW.updated_at)
      AND NEW.event_next_attempt_at IS NULL
      AND NEW.event_claim_token IS NOT OLD.event_claim_token)
    OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='processing'
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts+1
      AND unixepoch(OLD.event_lease_expires_at)<=unixepoch(NEW.updated_at)
      AND NEW.event_next_attempt_at IS NULL
      AND NEW.event_claim_token IS NOT OLD.event_claim_token)
    OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='pending'
      AND OLD.event_dispatch_attempts<10
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts
      AND NEW.event_next_attempt_at IS NOT NULL
      AND unixepoch(NEW.event_next_attempt_at)>unixepoch(NEW.updated_at)
      AND NEW.event_claim_token IS NULL AND NEW.event_lease_owner_id IS NULL
      AND NEW.event_lease_expires_at IS NULL
      AND NEW.event_last_error_code IS NOT NULL)
    OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='dispatched'
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts
      AND NEW.event_dispatched_at IS NOT NULL
      AND NEW.event_next_attempt_at IS NULL
      AND NEW.event_claim_token IS NULL AND NEW.event_lease_owner_id IS NULL
      AND NEW.event_lease_expires_at IS NULL
      AND NEW.event_last_error_code IS NULL)
    OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='failed'
      AND NEW.event_dispatch_attempts=10
      AND NEW.event_next_attempt_at IS NULL
      AND NEW.event_claim_token IS NULL AND NEW.event_lease_owner_id IS NULL
      AND NEW.event_lease_expires_at IS NULL
      AND NEW.event_last_error_code IS NOT NULL)
  )
)
BEGIN
  SELECT RAISE(ABORT, 'request_explanation_state_invalid');
END;
CREATE TRIGGER trg_request_explanations_immutable_delete_prd054
BEFORE DELETE ON request_explanations
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'request_explanation_immutable');
END;

-- Link 插入必须与 delivery 信封、源请求 tenant 和 original/replay 来源逐项一致。
-- original 只能使用事件白名单对应的字段并复核持久 source；replay 只能继承已证明的 original link。
CREATE TRIGGER trg_request_webhook_links_owner_insert_prd054
BEFORE INSERT ON request_webhook_delivery_links
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1
  FROM webhook_deliveries delivery
  JOIN request_logs request
    ON request.tenant_id=delivery.tenant_id
   AND request.request_id=NEW.source_request_id
  WHERE delivery.webhook_delivery_id=NEW.webhook_delivery_id
    AND delivery.tenant_id=NEW.tenant_id
    AND delivery.event_id=NEW.event_id
    AND delivery.event_type=NEW.event_type
    AND delivery.delivery_kind=NEW.delivery_kind
    AND json_valid(delivery.payload_json)
    AND json_extract(delivery.payload_json, '$.tenant_id')=NEW.tenant_id
    AND json_extract(delivery.payload_json, '$.event_id')=NEW.event_id
    AND json_extract(delivery.payload_json, '$.event_type')=NEW.event_type
    AND (
      (
        delivery.delivery_kind='original'
        AND (
          (
            delivery.event_type='request.explanation_viewed'
            AND json_type(delivery.payload_json, '$.payload.source_request_id')='text'
            AND json_extract(delivery.payload_json, '$.payload.source_request_id')=
                NEW.source_request_id
            AND EXISTS (
              SELECT 1 FROM request_explanations explanation
              WHERE explanation.tenant_id=delivery.tenant_id
                AND explanation.event_id=delivery.event_id
                AND explanation.request_explanation_id=
                    json_extract(delivery.payload_json, '$.object_id')
                AND explanation.request_explanation_id=
                    json_extract(delivery.payload_json, '$.payload.request_explanation_id')
                AND explanation.source_request_id=NEW.source_request_id
            )
          )
          OR (
            delivery.event_type='ledger.entry_created'
            AND json_type(delivery.payload_json, '$.payload.source_request_id')='text'
            AND json_extract(delivery.payload_json, '$.payload.source_request_id')=
                NEW.source_request_id
            AND EXISTS (
              SELECT 1 FROM finops_ledger_entries entry
              WHERE entry.tenant_id=delivery.tenant_id
                AND entry.event_id=delivery.event_id
                AND entry.finops_ledger_entry_id=
                    json_extract(delivery.payload_json, '$.object_id')
                AND entry.finops_ledger_entry_id=
                    json_extract(delivery.payload_json, '$.payload.ledger_entry_id')
                AND entry.source_request_id=NEW.source_request_id
            )
          )
          OR (
            json_type(delivery.payload_json, '$.payload.request_id')='text'
            AND json_extract(delivery.payload_json, '$.payload.request_id')=
                NEW.source_request_id
            AND (
              delivery.event_type IN (
                'gateway.request_failed', 'security.dlp_matched',
                'security.dlp_unavailable', 'usage.normalized', 'stream.timeout',
                'route.fallback_triggered', 'budget.threshold_reached',
                'budget.exhausted', 'budget.scope_exceeded',
                'budget.blocked', 'budget.unblocked'
              )
              OR (
                delivery.event_type IN ('route.policy_selected', 'cache.affinity_selected')
                AND json_extract(delivery.payload_json, '$.payload.trigger')='request_selected'
              )
            )
          )
        )
      )
      OR (
        delivery.delivery_kind='replay'
        AND EXISTS (
          SELECT 1 FROM request_webhook_delivery_links source
          WHERE source.tenant_id=delivery.tenant_id
            AND source.webhook_delivery_id=delivery.replayed_from_delivery_id
            AND source.source_request_id=NEW.source_request_id
            AND source.event_id=NEW.event_id
            AND source.event_type=NEW.event_type
            AND source.delivery_kind='original'
        )
      )
    )
)
BEGIN
  SELECT RAISE(ABORT, 'request_webhook_link_owner_mismatch');
END;
CREATE TRIGGER trg_request_webhook_links_conflict_insert_prd054
BEFORE INSERT ON request_webhook_delivery_links
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM request_webhook_delivery_links current
  WHERE current.request_webhook_delivery_link_id=NEW.request_webhook_delivery_link_id
    OR (current.tenant_id=NEW.tenant_id AND current.webhook_delivery_id=NEW.webhook_delivery_id)
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN
  SELECT RAISE(ABORT, 'request_webhook_link_immutable');
END;
CREATE TRIGGER trg_request_webhook_links_rowid_insert_prd054
AFTER INSERT ON request_webhook_delivery_links
FOR EACH ROW
WHEN NEW.rowid<0
BEGIN
  SELECT RAISE(ABORT, 'request_webhook_link_immutable');
END;
CREATE TRIGGER trg_request_webhook_links_immutable_update_prd054
BEFORE UPDATE ON request_webhook_delivery_links
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'request_webhook_link_immutable');
END;
CREATE TRIGGER trg_request_webhook_links_immutable_delete_prd054
BEFORE DELETE ON request_webhook_delivery_links
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'request_webhook_link_immutable');
END;

-- 统一 AFTER INSERT 触发器覆盖 Control、Runtime、original 和 replay，调用方无需维护双写。
CREATE TRIGGER trg_webhook_deliveries_request_link_insert_prd054
AFTER INSERT ON webhook_deliveries
FOR EACH ROW
BEGIN
  -- 原始投递只有在 payload 白名单和同租户请求归属同时成立时才建立可解释性关联。
  INSERT INTO request_webhook_delivery_links(
    request_webhook_delivery_link_id, tenant_id, source_request_id,
    webhook_delivery_id, event_id, event_type, delivery_kind, created_at
  )
  SELECT
    'rwdl_' || lower(hex(randomblob(16))),
    NEW.tenant_id,
    extracted.source_request_id,
    NEW.webhook_delivery_id,
    NEW.event_id,
    NEW.event_type,
    NEW.delivery_kind,
    NEW.created_at
  FROM (
    SELECT CASE
      WHEN json_type(NEW.payload_json, '$.payload.source_request_id')='text'
        AND (
          (
            NEW.event_type='request.explanation_viewed'
            AND EXISTS (
              SELECT 1 FROM request_explanations explanation
              WHERE explanation.tenant_id=NEW.tenant_id
                AND explanation.event_id=NEW.event_id
                AND explanation.request_explanation_id=
                    json_extract(NEW.payload_json, '$.object_id')
                AND explanation.request_explanation_id=
                    json_extract(NEW.payload_json, '$.payload.request_explanation_id')
                AND explanation.source_request_id=
                    json_extract(NEW.payload_json, '$.payload.source_request_id')
            )
          )
          OR (
            NEW.event_type='ledger.entry_created'
            AND EXISTS (
              SELECT 1 FROM finops_ledger_entries entry
              WHERE entry.tenant_id=NEW.tenant_id
                AND entry.event_id=NEW.event_id
                AND entry.finops_ledger_entry_id=
                    json_extract(NEW.payload_json, '$.object_id')
                AND entry.finops_ledger_entry_id=
                    json_extract(NEW.payload_json, '$.payload.ledger_entry_id')
                AND entry.source_request_id=
                    json_extract(NEW.payload_json, '$.payload.source_request_id')
            )
          )
        )
        THEN json_extract(NEW.payload_json, '$.payload.source_request_id')
      WHEN json_type(NEW.payload_json, '$.payload.request_id')='text'
        AND (
          NEW.event_type IN (
            'gateway.request_failed', 'security.dlp_matched', 'security.dlp_unavailable',
            'usage.normalized', 'stream.timeout', 'route.fallback_triggered',
            'budget.threshold_reached', 'budget.exhausted', 'budget.scope_exceeded',
            'budget.blocked', 'budget.unblocked'
          )
          OR (
            NEW.event_type IN ('route.policy_selected', 'cache.affinity_selected')
            AND json_extract(NEW.payload_json, '$.payload.trigger')='request_selected'
          )
        )
        THEN json_extract(NEW.payload_json, '$.payload.request_id')
      ELSE NULL
    END AS source_request_id
  ) extracted
  WHERE NEW.delivery_kind='original'
    AND json_valid(NEW.payload_json)
    AND typeof(extracted.source_request_id)='text'
    AND length(extracted.source_request_id) BETWEEN 1 AND 128
    AND extracted.source_request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
    AND EXISTS (
      SELECT 1 FROM request_logs request
      WHERE request.tenant_id=NEW.tenant_id
        AND request.request_id=extracted.source_request_id
    );

  -- 重放投递只能继承同租户原始关联，禁止重新解析任意 payload 来伪造来源请求。
  INSERT INTO request_webhook_delivery_links(
    request_webhook_delivery_link_id, tenant_id, source_request_id,
    webhook_delivery_id, event_id, event_type, delivery_kind, created_at
  )
  SELECT
    'rwdl_' || lower(hex(randomblob(16))),
    NEW.tenant_id,
    source.source_request_id,
    NEW.webhook_delivery_id,
    NEW.event_id,
    NEW.event_type,
    NEW.delivery_kind,
    NEW.created_at
  FROM request_webhook_delivery_links source
  WHERE NEW.delivery_kind='replay'
    AND source.tenant_id=NEW.tenant_id
    AND source.webhook_delivery_id=NEW.replayed_from_delivery_id;
END;

-- 回填 original 只使用安全信封白名单和实际 request_logs owner，不对任意 JSON 字段做猜测。
INSERT INTO request_webhook_delivery_links(
  request_webhook_delivery_link_id, tenant_id, source_request_id,
  webhook_delivery_id, event_id, event_type, delivery_kind, created_at
)
SELECT
  'rwdl_' || lower(hex(randomblob(16))),
  delivery.tenant_id,
  extracted.source_request_id,
  delivery.webhook_delivery_id,
  delivery.event_id,
  delivery.event_type,
  delivery.delivery_kind,
  delivery.created_at
FROM webhook_deliveries delivery
JOIN (
  SELECT
    candidate.webhook_delivery_id,
    CASE
      WHEN json_type(candidate.payload_json, '$.payload.source_request_id')='text'
        AND (
          (
            candidate.event_type='request.explanation_viewed'
            AND EXISTS (
              SELECT 1 FROM request_explanations explanation
              WHERE explanation.tenant_id=candidate.tenant_id
                AND explanation.event_id=candidate.event_id
                AND explanation.request_explanation_id=
                    json_extract(candidate.payload_json, '$.object_id')
                AND explanation.request_explanation_id=
                    json_extract(candidate.payload_json, '$.payload.request_explanation_id')
                AND explanation.source_request_id=
                    json_extract(candidate.payload_json, '$.payload.source_request_id')
            )
          )
          OR (
            candidate.event_type='ledger.entry_created'
            AND EXISTS (
              SELECT 1 FROM finops_ledger_entries entry
              WHERE entry.tenant_id=candidate.tenant_id
                AND entry.event_id=candidate.event_id
                AND entry.finops_ledger_entry_id=
                    json_extract(candidate.payload_json, '$.object_id')
                AND entry.finops_ledger_entry_id=
                    json_extract(candidate.payload_json, '$.payload.ledger_entry_id')
                AND entry.source_request_id=
                    json_extract(candidate.payload_json, '$.payload.source_request_id')
            )
          )
        )
        THEN json_extract(candidate.payload_json, '$.payload.source_request_id')
      WHEN json_type(candidate.payload_json, '$.payload.request_id')='text'
        AND (
          candidate.event_type IN (
            'gateway.request_failed', 'security.dlp_matched', 'security.dlp_unavailable',
            'usage.normalized', 'stream.timeout', 'route.fallback_triggered',
            'budget.threshold_reached', 'budget.exhausted', 'budget.scope_exceeded',
            'budget.blocked', 'budget.unblocked'
          )
          OR (
            candidate.event_type IN ('route.policy_selected', 'cache.affinity_selected')
            AND json_extract(candidate.payload_json, '$.payload.trigger')='request_selected'
          )
        )
        THEN json_extract(candidate.payload_json, '$.payload.request_id')
      ELSE NULL
    END AS source_request_id
  FROM webhook_deliveries candidate
  WHERE candidate.delivery_kind='original' AND json_valid(candidate.payload_json)
) extracted ON extracted.webhook_delivery_id=delivery.webhook_delivery_id
JOIN request_logs request
  ON request.tenant_id=delivery.tenant_id
 AND request.request_id=extracted.source_request_id
WHERE delivery.delivery_kind='original'
  AND typeof(extracted.source_request_id)='text'
  AND length(extracted.source_request_id) BETWEEN 1 AND 128
  AND extracted.source_request_id NOT GLOB '*[^A-Za-z0-9_.:-]*';

-- replay 只继承已证明的 original link，不重新解释其 payload。
INSERT INTO request_webhook_delivery_links(
  request_webhook_delivery_link_id, tenant_id, source_request_id,
  webhook_delivery_id, event_id, event_type, delivery_kind, created_at
)
SELECT
  'rwdl_' || lower(hex(randomblob(16))),
  replay.tenant_id,
  source.source_request_id,
  replay.webhook_delivery_id,
  replay.event_id,
  replay.event_type,
  replay.delivery_kind,
  replay.created_at
FROM webhook_deliveries replay
JOIN request_webhook_delivery_links source
  ON source.tenant_id=replay.tenant_id
 AND source.webhook_delivery_id=replay.replayed_from_delivery_id
WHERE replay.delivery_kind='replay';

-- 提交前检查目标表、索引、触发器和 SQLite 完整性，半套结构不能登记为 0044。
CREATE TEMP TABLE prd054_up_integrity_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd054_up_integrity_guard
BEFORE INSERT ON prd054_up_integrity_guard
FOR EACH ROW
WHEN NEW.issue_count!=0
BEGIN
  SELECT RAISE(ABORT, 'prd054_up_integrity_failed');
END;
-- 迁移结束时统一验证表、索引、触发器与回填数据，异常必须中止整个升级事务。
INSERT INTO prd054_up_integrity_guard(issue_count)
SELECT
  CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='table' AND name IN (
      'request_explanations', 'request_webhook_delivery_links'
    )
  )=2 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='index' AND name IN (
      'idx_request_explanations_request_time_prd054',
      'idx_request_explanations_actor_time_prd054',
      'idx_request_explanations_pending_event_prd054',
      'idx_request_webhook_links_request_time_prd054',
      'idx_budget_scope_events_request_prd054',
      'idx_usage_ledger_request_prd054'
    )
  )=6 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='trigger' AND name IN (
      'trg_request_explanations_owner_insert_prd054',
      'trg_request_explanations_conflict_insert_prd054',
      'trg_request_explanations_rowid_insert_prd054',
      'trg_request_explanations_state_update_prd054',
      'trg_request_explanations_immutable_delete_prd054',
      'trg_request_webhook_links_owner_insert_prd054',
      'trg_request_webhook_links_conflict_insert_prd054',
      'trg_request_webhook_links_rowid_insert_prd054',
      'trg_request_webhook_links_immutable_update_prd054',
      'trg_request_webhook_links_immutable_delete_prd054',
      'trg_webhook_deliveries_request_link_insert_prd054'
    )
  )=11 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + CASE WHEN (SELECT integrity_check FROM pragma_integrity_check)='ok' THEN 0 ELSE 1 END;
DROP TRIGGER trg_prd054_up_integrity_guard;
DROP TABLE prd054_up_integrity_guard;

COMMIT;
