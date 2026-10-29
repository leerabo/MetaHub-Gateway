-- 迁移职责：为 PRD-055 增加个人 SavedFilter、不可变查询事件 outbox 和 tenant-first 查询索引。
-- 安全边界：筛选事实不保存 Prompt、Response、密钥、Token 或自由查询正文，所有业务表均显式携带 tenant_id。
BEGIN IMMEDIATE;

CREATE TABLE saved_filters (
  saved_filter_id TEXT PRIMARY KEY CHECK (
    length(saved_filter_id)=35 AND substr(saved_filter_id,1,3)='sf_'
    AND substr(saved_filter_id,4) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  owner_user_id TEXT NOT NULL,
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 64 AND instr(name,char(0))=0),
  name_normalized TEXT NOT NULL CHECK (
    length(name_normalized) BETWEEN 1 AND 64 AND instr(name_normalized,char(0))=0
  ),
  filter_version INTEGER NOT NULL DEFAULT 1 CHECK (filter_version=1),
  filter_json TEXT NOT NULL CHECK (json_valid(filter_json) AND json_type(filter_json)='object'),
  sort_by TEXT NOT NULL CHECK (sort_by IN ('created_at','latency_ms','total_tokens','estimated_cost_amount')),
  sort_direction TEXT NOT NULL CHECK (sort_direction IN ('asc','desc')),
  version INTEGER NOT NULL DEFAULT 1 CHECK (typeof(version)='integer' AND version>=1),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','deleted')),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK (unixepoch(updated_at) IS NOT NULL),
  deleted_at TEXT CHECK (deleted_at IS NULL OR unixepoch(deleted_at) IS NOT NULL),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  -- 复合外键把 owner 的租户归属固化到数据库，避免只凭全局 user_id 形成跨租户关系。
  FOREIGN KEY (tenant_id, owner_user_id) REFERENCES users(tenant_id, user_id),
  CHECK (
    (status='active' AND deleted_at IS NULL)
    OR (status='deleted' AND deleted_at IS NOT NULL AND unixepoch(deleted_at)>=unixepoch(created_at))
  ),
  CHECK (unixepoch(updated_at)>=unixepoch(created_at))
);

CREATE UNIQUE INDEX idx_saved_filters_owner_name_prd055
  ON saved_filters(tenant_id, owner_user_id, name_normalized) WHERE status='active';
CREATE INDEX idx_saved_filters_owner_time_prd055
  ON saved_filters(tenant_id, owner_user_id, status, updated_at DESC, saved_filter_id DESC);

CREATE TABLE request_log_query_events (
  query_event_id TEXT PRIMARY KEY CHECK (
    length(query_event_id)=36 AND substr(query_event_id,1,4)='lqe_'
    AND substr(query_event_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  event_id TEXT NOT NULL UNIQUE CHECK (
    length(event_id)=38 AND substr(event_id,1,6)='lqevt_'
    AND substr(event_id,7) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  actor_session_id TEXT NOT NULL,
  filter_version INTEGER NOT NULL CHECK (filter_version=1),
  filter_fingerprint TEXT NOT NULL CHECK (
    length(filter_fingerprint)=64 AND filter_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  filter_keys_json TEXT NOT NULL CHECK (
    json_valid(filter_keys_json) AND json_type(filter_keys_json)='array'
  ),
  saved_filter_id TEXT,
  sort_by TEXT NOT NULL CHECK (sort_by IN ('created_at','latency_ms','total_tokens','estimated_cost_amount')),
  sort_direction TEXT NOT NULL CHECK (sort_direction IN ('asc','desc')),
  query_limit INTEGER NOT NULL CHECK (typeof(query_limit)='integer' AND query_limit BETWEEN 1 AND 100),
  query_offset INTEGER NOT NULL CHECK (typeof(query_offset)='integer' AND query_offset BETWEEN 0 AND 10000),
  matched_count INTEGER NOT NULL CHECK (typeof(matched_count)='integer' AND matched_count>=0),
  returned_count INTEGER NOT NULL CHECK (typeof(returned_count)='integer' AND returned_count BETWEEN 0 AND 100),
  has_more INTEGER NOT NULL CHECK (has_more IN (0,1)),
  query_duration_ms INTEGER NOT NULL CHECK (typeof(query_duration_ms)='integer' AND query_duration_ms>=0),
  operation_request_id TEXT NOT NULL CHECK (
    length(operation_request_id) BETWEEN 1 AND 128 AND operation_request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  operation_trace_id TEXT NOT NULL CHECK (
    length(operation_trace_id) BETWEEN 1 AND 128 AND operation_trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  event_dispatch_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (event_dispatch_status IN ('pending','processing','dispatched','failed')),
  event_dispatch_attempts INTEGER NOT NULL DEFAULT 0
    CHECK (typeof(event_dispatch_attempts)='integer' AND event_dispatch_attempts BETWEEN 0 AND 10),
  event_next_attempt_at TEXT,
  event_dispatched_at TEXT,
  event_claim_token TEXT,
  event_lease_owner_id TEXT,
  event_lease_expires_at TEXT,
  event_last_error_code TEXT CHECK (
    event_last_error_code IS NULL OR event_last_error_code IN (
      'webhook.event_type_invalid','webhook.payload_invalid','webhook.payload_sensitive',
      'webhook.delivery_unavailable','request_log.query_event_unavailable'
    )
  ),
  executed_at TEXT NOT NULL CHECK (unixepoch(executed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK (unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (actor_user_id) REFERENCES users(user_id),
  FOREIGN KEY (actor_session_id) REFERENCES sessions(session_id),
  FOREIGN KEY (saved_filter_id) REFERENCES saved_filters(saved_filter_id),
  CHECK (executed_at=created_at),
  CHECK (returned_count<=matched_count AND returned_count<=query_limit),
  -- has_more 是结果事实而非调用方输入，必须与分页位置和总命中数严格一致。
  CHECK (
    has_more=CASE
      WHEN query_offset+returned_count<matched_count THEN 1
      ELSE 0
    END
  ),
  CHECK (
    (event_dispatch_status='pending' AND event_dispatch_attempts BETWEEN 0 AND 9
      AND event_next_attempt_at IS NOT NULL AND unixepoch(event_next_attempt_at) IS NOT NULL
      AND event_dispatched_at IS NULL AND event_claim_token IS NULL
      AND event_lease_owner_id IS NULL AND event_lease_expires_at IS NULL)
    OR (event_dispatch_status='processing' AND event_dispatch_attempts BETWEEN 1 AND 10
      AND event_next_attempt_at IS NULL AND event_dispatched_at IS NULL
      AND event_claim_token IS NOT NULL AND event_lease_owner_id IS NOT NULL
      AND event_lease_expires_at IS NOT NULL)
    OR (event_dispatch_status='dispatched' AND event_dispatched_at IS NOT NULL
      AND event_next_attempt_at IS NULL AND event_claim_token IS NULL
      AND event_lease_owner_id IS NULL AND event_lease_expires_at IS NULL
      AND event_last_error_code IS NULL)
    OR (event_dispatch_status='failed' AND event_dispatch_attempts=10
      AND event_next_attempt_at IS NULL AND event_dispatched_at IS NULL
      AND event_claim_token IS NULL AND event_lease_owner_id IS NULL
      AND event_lease_expires_at IS NULL AND event_last_error_code IS NOT NULL)
  )
);

CREATE INDEX idx_request_log_query_events_actor_time_prd055
  ON request_log_query_events(tenant_id, actor_user_id, executed_at DESC, query_event_id DESC);
CREATE INDEX idx_request_log_query_events_pending_prd055
  ON request_log_query_events(event_dispatch_status, event_next_attempt_at, event_lease_expires_at, created_at)
  WHERE event_dispatch_status IN ('pending','processing');

-- 常用筛选与可选排序均以 tenant_id 为首列；其余条件只在租户子集内过滤。
CREATE INDEX idx_request_logs_trace_time_prd055
  ON request_logs(tenant_id, trace_id, created_at DESC, request_id DESC);
CREATE INDEX idx_request_logs_virtual_key_time_prd055
  ON request_logs(tenant_id, virtual_key_id, created_at DESC, request_id DESC);
CREATE INDEX idx_request_logs_provider_time_prd055
  ON request_logs(tenant_id, provider_code, created_at DESC, request_id DESC);
CREATE INDEX idx_request_logs_status_time_prd055
  ON request_logs(tenant_id, status, created_at DESC, request_id DESC);
CREATE INDEX idx_request_logs_latency_time_prd055
  ON request_logs(tenant_id, latency_ms, created_at DESC, request_id DESC);
CREATE INDEX idx_request_logs_tokens_time_prd055
  ON request_logs(tenant_id, total_tokens, created_at DESC, request_id DESC);
CREATE INDEX idx_request_logs_cost_time_prd055
  ON request_logs(tenant_id, estimated_cost_amount, created_at DESC, request_id DESC);
-- terminal_stage_v1 只表达可由冻结终态证明的完成/终止阶段；unknown 绝不伪造成已通过阶段。
CREATE INDEX idx_request_logs_terminal_stage_time_prd055
  ON request_logs(
    tenant_id,
    CASE
      WHEN status='success' THEN 'completed'
      WHEN error_code IN ('gateway.auth_missing','gateway.auth_invalid') THEN 'authentication'
      WHEN error_code IN ('gateway.rate_limit_exceeded','gateway.rate_limit_unavailable') THEN 'rate_limit'
      WHEN error_code IN ('gateway.budget_exhausted','gateway.budget_unavailable') THEN 'budget'
      WHEN error_code IN ('gateway.dlp_blocked','gateway.dlp_unavailable') THEN 'dlp'
      WHEN error_code LIKE 'gateway.policy_%' THEN 'policy'
      WHEN error_code IN ('gateway.cache_affinity_unavailable') THEN 'keypool'
      WHEN error_code LIKE 'gateway.upstream_%'
        OR error_code IN (
          'gateway.execution_interrupted','gateway.stream_not_supported',
          'gateway.stream_session_unavailable','gateway.client_disconnected',
          'gateway.stream_processing_unavailable'
        ) THEN 'provider'
      WHEN error_code IN (
        'gateway.model_missing','gateway.messages_missing','gateway.input_missing',
        'gateway.model_not_found','gateway.model_alias_disabled','gateway.model_disabled',
        'gateway.provider_not_found','gateway.provider_unhealthy','gateway.no_route_available',
        'gateway.routing_policy_invalid','gateway.routing_snapshot_unavailable',
        'gateway.routing_snapshot_invalid','gateway.routing_decision_unavailable',
        'gateway.routing_attempt_unavailable','gateway.fallback_unavailable'
      ) THEN 'routing'
      WHEN error_code='gateway.request_log_unavailable' THEN 'logs_and_webhooks'
      ELSE 'unknown'
    END,
    created_at DESC,
    request_id DESC
  );

-- 数据库再次核对 user/session/SavedFilter 归属，阻止服务层遗漏 tenant/owner 条件。
CREATE TRIGGER trg_saved_filters_owner_insert_prd055
BEFORE INSERT ON saved_filters FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM users u WHERE u.user_id=NEW.owner_user_id AND u.tenant_id=NEW.tenant_id
)
OR (SELECT COUNT(*) FROM saved_filters f
    WHERE f.tenant_id=NEW.tenant_id AND f.owner_user_id=NEW.owner_user_id AND f.status='active')>=50
BEGIN SELECT RAISE(ABORT,'saved_filter_owner_or_limit_invalid'); END;

-- 新记录只能从 active/version=1 初态进入；禁止伪造 deleted 历史或高版本起点。
CREATE TRIGGER trg_saved_filters_state_insert_prd055
BEFORE INSERT ON saved_filters FOR EACH ROW
WHEN NOT (
  NEW.filter_version=1 AND NEW.version=1 AND NEW.status='active'
  AND NEW.deleted_at IS NULL AND NEW.created_at=NEW.updated_at
)
BEGIN SELECT RAISE(ABORT,'saved_filter_state_invalid'); END;

-- 服务层会生成 canonical JSON；数据库仍独立拒绝重复键、未知字段和嵌套/浮点值，
-- 防止运维 SQL 或未来写路径绕过 v1 查询合同。
CREATE TRIGGER trg_saved_filters_payload_insert_prd055
BEFORE INSERT ON saved_filters FOR EACH ROW
WHEN NOT (
  json_valid(NEW.filter_json)
  AND CASE WHEN json_valid(NEW.filter_json) THEN json_type(NEW.filter_json) END='object'
  AND (SELECT COUNT(*) FROM json_each(
    CASE WHEN json_valid(NEW.filter_json) THEN NEW.filter_json ELSE '{}' END
  )) BETWEEN 1 AND 24
  AND (
    SELECT COUNT(*) FROM json_each(
      CASE WHEN json_valid(NEW.filter_json) THEN NEW.filter_json ELSE '{}' END
    ) WHERE key='v' AND type='integer' AND atom=1
  )=1
  AND (
    SELECT COUNT(*) FROM json_each(
      CASE WHEN json_valid(NEW.filter_json) THEN NEW.filter_json ELSE '{}' END
    )
  )=(
    SELECT COUNT(DISTINCT key) FROM json_each(
      CASE WHEN json_valid(NEW.filter_json) THEN NEW.filter_json ELSE '{}' END
    )
  )
  AND NOT EXISTS (
    SELECT 1 FROM json_each(
      CASE WHEN json_valid(NEW.filter_json) THEN NEW.filter_json ELSE '{}' END
    ) item
    WHERE item.key NOT IN (
      'v','request_id','trace_id','status','request_model','resolved_model',
      'provider_code','virtual_key_id','environment_id','project_id','application_id',
      'snapshot_version','error_code','created_from','created_to','latency_ms_min',
      'latency_ms_max','total_tokens_min','total_tokens_max','cost_amount_min',
      'cost_amount_max','currency','terminal_stage'
    )
    OR item.type NOT IN ('text','integer')
    OR (
      item.key IN (
        'v','snapshot_version','latency_ms_min','latency_ms_max',
        'total_tokens_min','total_tokens_max'
      ) AND item.type!='integer'
    )
    OR (
      item.key NOT IN (
        'v','snapshot_version','latency_ms_min','latency_ms_max',
        'total_tokens_min','total_tokens_max'
      ) AND item.type!='text'
    )
    OR (item.key='snapshot_version' AND item.atom<1)
    OR (
      item.key IN ('latency_ms_min','latency_ms_max','total_tokens_min','total_tokens_max')
      AND item.atom<0
    )
  )
)
BEGIN SELECT RAISE(ABORT,'saved_filter_payload_invalid'); END;

CREATE TRIGGER trg_saved_filters_conflict_insert_prd055
BEFORE INSERT ON saved_filters FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM saved_filters current
  WHERE current.saved_filter_id=NEW.saved_filter_id
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
    OR (
      NEW.status='active' AND current.status='active'
      AND current.tenant_id=NEW.tenant_id
      AND current.owner_user_id=NEW.owner_user_id
      AND current.name_normalized=NEW.name_normalized
    )
)
BEGIN SELECT RAISE(ABORT,'saved_filter_immutable'); END;

CREATE TRIGGER trg_saved_filters_rowid_insert_prd055
AFTER INSERT ON saved_filters FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'saved_filter_immutable'); END;

CREATE TRIGGER trg_saved_filters_state_update_prd055
BEFORE UPDATE ON saved_filters FOR EACH ROW
WHEN NOT (
  NEW.rowid=OLD.rowid AND NEW.saved_filter_id=OLD.saved_filter_id
  AND NEW.tenant_id=OLD.tenant_id AND NEW.owner_user_id=OLD.owner_user_id
  AND NEW.filter_version=OLD.filter_version AND NEW.created_at=OLD.created_at
  AND NEW.version=OLD.version+1
  AND unixepoch(NEW.updated_at)>unixepoch(OLD.updated_at)
  AND OLD.status='active'
  AND NOT EXISTS (
    SELECT 1 FROM saved_filters other
    WHERE other.status='active' AND other.saved_filter_id!=OLD.saved_filter_id
      AND other.tenant_id=NEW.tenant_id AND other.owner_user_id=NEW.owner_user_id
      AND other.name_normalized=NEW.name_normalized
  )
  AND (
    (NEW.status='active' AND NEW.deleted_at IS NULL)
    OR (
      NEW.status='deleted' AND NEW.deleted_at=NEW.updated_at
      -- 删除操作只推进状态、版本和时间；名称、筛选与排序保持最后一个 active 事实。
      AND NEW.name=OLD.name AND NEW.name_normalized=OLD.name_normalized
      AND NEW.filter_json=OLD.filter_json AND NEW.sort_by=OLD.sort_by
      AND NEW.sort_direction=OLD.sort_direction
    )
  )
)
BEGIN SELECT RAISE(ABORT,'saved_filter_state_invalid'); END;

-- 更新路径复用与插入相同的冻结白名单；软删除时 JSON 虽不变，仍要防御历史损坏。
CREATE TRIGGER trg_saved_filters_payload_update_prd055
BEFORE UPDATE ON saved_filters FOR EACH ROW
WHEN NOT (
  json_valid(NEW.filter_json)
  AND CASE WHEN json_valid(NEW.filter_json) THEN json_type(NEW.filter_json) END='object'
  AND (SELECT COUNT(*) FROM json_each(
    CASE WHEN json_valid(NEW.filter_json) THEN NEW.filter_json ELSE '{}' END
  )) BETWEEN 1 AND 24
  AND (
    SELECT COUNT(*) FROM json_each(
      CASE WHEN json_valid(NEW.filter_json) THEN NEW.filter_json ELSE '{}' END
    ) WHERE key='v' AND type='integer' AND atom=1
  )=1
  AND (
    SELECT COUNT(*) FROM json_each(
      CASE WHEN json_valid(NEW.filter_json) THEN NEW.filter_json ELSE '{}' END
    )
  )=(
    SELECT COUNT(DISTINCT key) FROM json_each(
      CASE WHEN json_valid(NEW.filter_json) THEN NEW.filter_json ELSE '{}' END
    )
  )
  AND NOT EXISTS (
    SELECT 1 FROM json_each(
      CASE WHEN json_valid(NEW.filter_json) THEN NEW.filter_json ELSE '{}' END
    ) item
    WHERE item.key NOT IN (
      'v','request_id','trace_id','status','request_model','resolved_model',
      'provider_code','virtual_key_id','environment_id','project_id','application_id',
      'snapshot_version','error_code','created_from','created_to','latency_ms_min',
      'latency_ms_max','total_tokens_min','total_tokens_max','cost_amount_min',
      'cost_amount_max','currency','terminal_stage'
    )
    OR item.type NOT IN ('text','integer')
    OR (
      item.key IN (
        'v','snapshot_version','latency_ms_min','latency_ms_max',
        'total_tokens_min','total_tokens_max'
      ) AND item.type!='integer'
    )
    OR (
      item.key NOT IN (
        'v','snapshot_version','latency_ms_min','latency_ms_max',
        'total_tokens_min','total_tokens_max'
      ) AND item.type!='text'
    )
    OR (item.key='snapshot_version' AND item.atom<1)
    OR (
      item.key IN ('latency_ms_min','latency_ms_max','total_tokens_min','total_tokens_max')
      AND item.atom<0
    )
  )
)
BEGIN SELECT RAISE(ABORT,'saved_filter_payload_invalid'); END;

CREATE TRIGGER trg_saved_filters_immutable_delete_prd055
BEFORE DELETE ON saved_filters FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'saved_filter_immutable'); END;

CREATE TRIGGER trg_request_log_query_events_owner_insert_prd055
BEFORE INSERT ON request_log_query_events FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM users u WHERE u.user_id=NEW.actor_user_id AND u.tenant_id=NEW.tenant_id
)
OR NOT EXISTS (
  SELECT 1 FROM sessions s WHERE s.session_id=NEW.actor_session_id
    AND s.user_id=NEW.actor_user_id AND s.tenant_id=NEW.tenant_id
)
OR (NEW.saved_filter_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM saved_filters f WHERE f.saved_filter_id=NEW.saved_filter_id
    AND f.tenant_id=NEW.tenant_id AND f.owner_user_id=NEW.actor_user_id AND f.status='active'
))
BEGIN SELECT RAISE(ABORT,'request_log_query_event_owner_invalid'); END;

-- 查询事件只能以待派发零尝试初态写入，禁止绕过 worker 直接伪造处理中或已投递。
CREATE TRIGGER trg_request_log_query_events_state_insert_prd055
BEFORE INSERT ON request_log_query_events FOR EACH ROW
WHEN NOT (
  NEW.event_dispatch_status='pending' AND NEW.event_dispatch_attempts=0
  AND NEW.event_next_attempt_at=NEW.created_at
  AND NEW.event_dispatched_at IS NULL AND NEW.event_claim_token IS NULL
  AND NEW.event_lease_owner_id IS NULL AND NEW.event_lease_expires_at IS NULL
  AND NEW.event_last_error_code IS NULL
  AND NEW.executed_at=NEW.created_at AND NEW.updated_at=NEW.created_at
)
BEGIN SELECT RAISE(ABORT,'request_log_query_event_state_invalid'); END;

-- 查询事件只保存排序后的字段名；legacy model 显式列入兼容白名单，筛选值不得进入源事实。
CREATE TRIGGER trg_request_log_query_events_keys_insert_prd055
BEFORE INSERT ON request_log_query_events FOR EACH ROW
WHEN NOT (
  json_valid(NEW.filter_keys_json)
  AND CASE WHEN json_valid(NEW.filter_keys_json)
    THEN json_type(NEW.filter_keys_json) END='array'
  AND (SELECT COUNT(*) FROM json_each(
    CASE WHEN json_valid(NEW.filter_keys_json) THEN NEW.filter_keys_json ELSE '[]' END
  ))<=24
  AND NOT EXISTS (
    SELECT 1 FROM json_each(
      CASE WHEN json_valid(NEW.filter_keys_json) THEN NEW.filter_keys_json ELSE '[]' END
    ) item
    WHERE item.type!='text' OR item.value NOT IN (
      'request_id','trace_id','status','model','request_model','resolved_model',
      'provider_code','virtual_key_id','environment_id','project_id','application_id',
      'snapshot_version','error_code','created_from','created_to','latency_ms_min',
      'latency_ms_max','total_tokens_min','total_tokens_max','cost_amount_min',
      'cost_amount_max','currency','terminal_stage'
    )
  )
  AND NOT EXISTS (
    SELECT 1
    FROM json_each(
      CASE WHEN json_valid(NEW.filter_keys_json) THEN NEW.filter_keys_json ELSE '[]' END
    ) earlier
    JOIN json_each(
      CASE WHEN json_valid(NEW.filter_keys_json) THEN NEW.filter_keys_json ELSE '[]' END
    ) later ON CAST(earlier.key AS INTEGER)<CAST(later.key AS INTEGER)
    WHERE CAST(earlier.value AS TEXT)>=CAST(later.value AS TEXT)
  )
)
BEGIN SELECT RAISE(ABORT,'request_log_query_event_keys_invalid'); END;

CREATE TRIGGER trg_request_log_query_events_conflict_insert_prd055
BEFORE INSERT ON request_log_query_events FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM request_log_query_events current
  WHERE current.query_event_id=NEW.query_event_id OR current.event_id=NEW.event_id
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'request_log_query_event_immutable'); END;

CREATE TRIGGER trg_request_log_query_events_rowid_insert_prd055
AFTER INSERT ON request_log_query_events FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'request_log_query_event_immutable'); END;

CREATE TRIGGER trg_request_log_query_events_state_update_prd055
BEFORE UPDATE ON request_log_query_events FOR EACH ROW
WHEN NOT (
  NEW.rowid=OLD.rowid AND NEW.query_event_id=OLD.query_event_id AND NEW.event_id=OLD.event_id
  AND NEW.tenant_id=OLD.tenant_id AND NEW.actor_user_id=OLD.actor_user_id
  AND NEW.actor_session_id=OLD.actor_session_id AND NEW.filter_version=OLD.filter_version
  AND NEW.filter_fingerprint=OLD.filter_fingerprint AND NEW.filter_keys_json=OLD.filter_keys_json
  AND NEW.saved_filter_id IS OLD.saved_filter_id AND NEW.sort_by=OLD.sort_by
  AND NEW.sort_direction=OLD.sort_direction AND NEW.query_limit=OLD.query_limit
  AND NEW.query_offset=OLD.query_offset AND NEW.matched_count=OLD.matched_count
  AND NEW.returned_count=OLD.returned_count AND NEW.has_more=OLD.has_more
  AND NEW.query_duration_ms=OLD.query_duration_ms
  AND NEW.operation_request_id=OLD.operation_request_id AND NEW.operation_trace_id=OLD.operation_trace_id
  AND NEW.executed_at=OLD.executed_at AND NEW.created_at=OLD.created_at
  AND unixepoch(NEW.updated_at)>unixepoch(OLD.updated_at)
  AND (
    (OLD.event_dispatch_status='pending' AND NEW.event_dispatch_status='processing'
      AND OLD.event_dispatch_attempts BETWEEN 0 AND 9
      AND unixepoch(OLD.event_next_attempt_at)<=unixepoch(NEW.updated_at)
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts+1
      AND NEW.event_next_attempt_at IS NULL AND NEW.event_dispatched_at IS NULL
      AND length(NEW.event_claim_token) BETWEEN 16 AND 128
      AND length(NEW.event_lease_owner_id) BETWEEN 1 AND 128
      AND unixepoch(NEW.event_lease_expires_at)>unixepoch(NEW.updated_at)
      AND NEW.event_last_error_code IS NULL)
    OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='processing'
      AND OLD.event_dispatch_attempts BETWEEN 1 AND 9
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts+1
      AND unixepoch(OLD.event_lease_expires_at)<=unixepoch(NEW.updated_at)
      AND NEW.event_next_attempt_at IS NULL AND NEW.event_dispatched_at IS NULL
      AND length(NEW.event_claim_token) BETWEEN 16 AND 128
      AND length(NEW.event_lease_owner_id) BETWEEN 1 AND 128
      AND unixepoch(NEW.event_lease_expires_at)>unixepoch(NEW.updated_at)
      AND NEW.event_last_error_code IS NULL)
    OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='dispatched'
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts
      AND unixepoch(OLD.event_lease_expires_at)>unixepoch(NEW.updated_at)
      AND NEW.event_dispatched_at=NEW.updated_at AND NEW.event_next_attempt_at IS NULL
      AND NEW.event_claim_token IS NULL AND NEW.event_lease_owner_id IS NULL
      AND NEW.event_lease_expires_at IS NULL AND NEW.event_last_error_code IS NULL)
    OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='pending'
      AND OLD.event_dispatch_attempts BETWEEN 1 AND 9
      AND NEW.event_dispatch_attempts=OLD.event_dispatch_attempts
      AND unixepoch(OLD.event_lease_expires_at)>unixepoch(NEW.updated_at)
      AND unixepoch(NEW.event_next_attempt_at)>unixepoch(NEW.updated_at)
      AND NEW.event_dispatched_at IS NULL AND NEW.event_claim_token IS NULL
      AND NEW.event_lease_owner_id IS NULL AND NEW.event_lease_expires_at IS NULL
      AND NEW.event_last_error_code IS NOT NULL)
    OR (OLD.event_dispatch_status='processing' AND NEW.event_dispatch_status='failed'
      AND OLD.event_dispatch_attempts=10 AND NEW.event_dispatch_attempts=10
      AND NEW.event_next_attempt_at IS NULL AND NEW.event_dispatched_at IS NULL
      AND NEW.event_claim_token IS NULL AND NEW.event_lease_owner_id IS NULL
      AND NEW.event_lease_expires_at IS NULL AND NEW.event_last_error_code IS NOT NULL)
  )
)
BEGIN SELECT RAISE(ABORT,'request_log_query_event_immutable'); END;

CREATE TRIGGER trg_request_log_query_events_immutable_delete_prd055
BEFORE DELETE ON request_log_query_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'request_log_query_event_immutable'); END;

COMMIT;
