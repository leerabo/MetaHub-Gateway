-- 迁移职责：为 PRD-081 增加 JSON/CEF Webhook wire、安全事件聚合 outbox 与持久限流证据。
-- 安全边界：标准事件只保存 DLP、会话风险和 Provider 凭据异常的固定安全投影；不得复制
-- Prompt、Response、命中文本、规则 pattern、Provider Key、Credential、Endpoint URL 或 Secret。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- Endpoint 冻结格式与每分钟安全事件上限；历史配置保持 JSON 和默认 60/min。
ALTER TABLE webhook_endpoints ADD COLUMN payload_format TEXT NOT NULL DEFAULT 'json'
  CHECK(payload_format IN('json','cef'));
ALTER TABLE webhook_endpoints ADD COLUMN security_event_rate_limit_per_minute INTEGER NOT NULL DEFAULT 60
  CHECK(typeof(security_event_rate_limit_per_minute)='integer'
    AND security_event_rate_limit_per_minute BETWEEN 1 AND 600);

-- Delivery 同时保存逻辑 JSON 与实际 UTF-8 wire；先回填历史正文，再安装写入门禁。
ALTER TABLE webhook_deliveries ADD COLUMN payload_format TEXT NOT NULL DEFAULT 'json'
  CHECK(payload_format IN('json','cef'));
ALTER TABLE webhook_deliveries ADD COLUMN wire_body_text TEXT NOT NULL DEFAULT '';
ALTER TABLE webhook_deliveries ADD COLUMN security_event_rate_limit_per_minute INTEGER NOT NULL DEFAULT 60
  CHECK(typeof(security_event_rate_limit_per_minute)='integer'
    AND security_event_rate_limit_per_minute BETWEEN 1 AND 600);
UPDATE webhook_deliveries SET wire_body_text=payload_json;

-- CEF Endpoint 只能订阅标准安全事件；未知格式、非整数限流和 JSON 漂移都在数据库层失败关闭。
CREATE TRIGGER trg_webhook_endpoints_siem_insert_prd081
BEFORE INSERT ON webhook_endpoints FOR EACH ROW
WHEN NEW.payload_format NOT IN('json','cef')
  OR typeof(NEW.security_event_rate_limit_per_minute)!='integer'
  OR NEW.security_event_rate_limit_per_minute NOT BETWEEN 1 AND 600
  OR NOT json_valid(NEW.event_types_json)
  OR json_type(NEW.event_types_json)!='array'
  OR (NEW.payload_format='cef' AND (
    json_array_length(NEW.event_types_json)!=1
    OR json_extract(NEW.event_types_json,'$[0]')!='security.event_pushed'
  ))
BEGIN SELECT RAISE(ABORT,'webhook_format_invalid'); END;

CREATE TRIGGER trg_webhook_endpoints_siem_update_prd081
BEFORE UPDATE OF payload_format,event_types_json,security_event_rate_limit_per_minute
ON webhook_endpoints FOR EACH ROW
WHEN NEW.payload_format NOT IN('json','cef')
  OR typeof(NEW.security_event_rate_limit_per_minute)!='integer'
  OR NEW.security_event_rate_limit_per_minute NOT BETWEEN 1 AND 600
  OR NOT json_valid(NEW.event_types_json)
  OR json_type(NEW.event_types_json)!='array'
  OR (NEW.payload_format='cef' AND (
    json_array_length(NEW.event_types_json)!=1
    OR json_extract(NEW.event_types_json,'$[0]')!='security.event_pushed'
  ))
BEGIN SELECT RAISE(ABORT,'webhook_format_invalid'); END;

-- Original 必须冻结 Endpoint 当前 format/rate；replay 必须逐字复用来源的事件、JSON、wire 与 rate。
CREATE TRIGGER trg_webhook_deliveries_siem_insert_prd081
BEFORE INSERT ON webhook_deliveries FOR EACH ROW
WHEN NOT json_valid(NEW.payload_json) OR json_type(NEW.payload_json)!='object'
  OR (NEW.payload_format='json'
    AND NEW.wire_body_text NOT IN(NEW.payload_json,''))
  OR (NEW.payload_format='cef' AND (
    NEW.event_type NOT IN('security.event_pushed','webhook.test')
    OR length(CAST(NEW.wire_body_text AS BLOB)) NOT BETWEEN 1 AND 16384
    OR instr(NEW.wire_body_text,char(10))!=0 OR instr(NEW.wire_body_text,char(13))!=0
    OR substr(NEW.wire_body_text,1,length('CEF:0|MetaHub|AI Gateway|1|'))
      !='CEF:0|MetaHub|AI Gateway|1|'
  ))
  OR (NEW.delivery_kind='original' AND NOT EXISTS(
    SELECT 1 FROM webhook_endpoints endpoint
    WHERE endpoint.tenant_id=NEW.tenant_id
      AND endpoint.webhook_endpoint_id=NEW.webhook_endpoint_id
      AND endpoint.payload_format=NEW.payload_format
      AND endpoint.security_event_rate_limit_per_minute=
          NEW.security_event_rate_limit_per_minute
  ))
  OR (NEW.delivery_kind='replay' AND NOT EXISTS(
    SELECT 1 FROM webhook_deliveries source
    WHERE source.tenant_id=NEW.tenant_id
      AND source.webhook_endpoint_id=NEW.webhook_endpoint_id
      AND source.webhook_delivery_id=NEW.replayed_from_delivery_id
      AND source.event_id=NEW.event_id AND source.event_type=NEW.event_type
      AND source.event_timestamp=NEW.event_timestamp
      AND source.payload_json=NEW.payload_json
      AND source.payload_format=NEW.payload_format
      AND source.wire_body_text=NEW.wire_body_text
      AND source.security_event_rate_limit_per_minute=
          NEW.security_event_rate_limit_per_minute
  ))
BEGIN SELECT RAISE(ABORT,'webhook_delivery_wire_invalid'); END;

-- 兼容 0074 前已发布的内部 producer：省略新列时仅允许 JSON，并在同一事务补齐 canonical wire。
CREATE TRIGGER trg_webhook_deliveries_siem_legacy_fill_prd081
AFTER INSERT ON webhook_deliveries FOR EACH ROW
WHEN NEW.payload_format='json' AND NEW.wire_body_text=''
BEGIN
  UPDATE webhook_deliveries SET wire_body_text=NEW.payload_json
  WHERE webhook_delivery_id=NEW.webhook_delivery_id AND wire_body_text='';
END;

-- 事件坐标、逻辑正文和实际 wire 在 intent 创建后永久冻结；Secret 版本仍按既有协议可受控刷新。
CREATE TRIGGER trg_webhook_deliveries_siem_immutable_prd081
BEFORE UPDATE OF event_id,event_type,event_timestamp,payload_json,payload_format,
  wire_body_text,security_event_rate_limit_per_minute
ON webhook_deliveries FOR EACH ROW
WHEN NEW.event_id IS NOT OLD.event_id OR NEW.event_type IS NOT OLD.event_type
  OR NEW.event_timestamp IS NOT OLD.event_timestamp
  OR NEW.payload_json IS NOT OLD.payload_json OR NEW.payload_format IS NOT OLD.payload_format
  OR (NEW.wire_body_text IS NOT OLD.wire_body_text
    AND NOT (OLD.payload_format='json' AND OLD.wire_body_text=''
      AND NEW.wire_body_text=OLD.payload_json))
  OR NEW.security_event_rate_limit_per_minute IS NOT OLD.security_event_rate_limit_per_minute
BEGIN SELECT RAISE(ABORT,'webhook_delivery_wire_immutable'); END;

-- 本地 Key 解密异常只保存 Provider/模型、可信请求坐标、候选计数和时间。
CREATE TABLE provider_key_anomaly_events (
  provider_key_anomaly_event_id TEXT PRIMARY KEY CHECK(
    length(provider_key_anomaly_event_id)=37
    AND substr(provider_key_anomaly_event_id,1,5)='pkae_'
    AND substr(provider_key_anomaly_event_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  anomaly_type TEXT NOT NULL CHECK(anomaly_type='decrypt_failed'),
  request_id TEXT NOT NULL CHECK(
    length(request_id) BETWEEN 1 AND 128 AND request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  trace_id TEXT NOT NULL CHECK(
    length(trace_id) BETWEEN 1 AND 128 AND trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  candidate_count INTEGER NOT NULL CHECK(
    typeof(candidate_count)='integer' AND candidate_count BETWEEN 1 AND 1000
  ),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(created_at=occurred_at),
  UNIQUE(tenant_id,provider_key_anomaly_event_id),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(tenant_id,provider_id) REFERENCES providers(tenant_id,provider_id),
  FOREIGN KEY(tenant_id,model_id) REFERENCES models(tenant_id,model_id)
);
CREATE INDEX idx_provider_key_anomaly_tenant_time_prd081
  ON provider_key_anomaly_events(tenant_id,occurred_at DESC,provider_key_anomaly_event_id DESC);
CREATE TRIGGER trg_provider_key_anomaly_owner_insert_prd081
BEFORE INSERT ON provider_key_anomaly_events FOR EACH ROW
WHEN NOT EXISTS(
    SELECT 1 FROM providers provider JOIN models model
      ON model.tenant_id=provider.tenant_id AND model.provider_id=provider.provider_id
    WHERE provider.tenant_id=NEW.tenant_id AND provider.provider_id=NEW.provider_id
      AND model.model_id=NEW.model_id
  )
  OR EXISTS(SELECT 1 FROM provider_key_anomaly_events current
    WHERE current.provider_key_anomaly_event_id=NEW.provider_key_anomaly_event_id
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'provider_key_anomaly_owner_mismatch'); END;
CREATE TRIGGER trg_provider_key_anomaly_update_prd081
BEFORE UPDATE ON provider_key_anomaly_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_key_anomaly_immutable'); END;
CREATE TRIGGER trg_provider_key_anomaly_delete_prd081
BEFORE DELETE ON provider_key_anomaly_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_key_anomaly_immutable'); END;

-- 四类权威事实投影到同一 append-only source；表中没有正文、规则名、Credential 或错误原文列。
CREATE TABLE security_event_sources (
  security_event_source_id TEXT PRIMARY KEY CHECK(
    length(security_event_source_id)=38 AND substr(security_event_source_id,1,6)='sesrc_'
    AND substr(security_event_source_id,7) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  source_event_type TEXT NOT NULL CHECK(source_event_type IN(
    'security.dlp_matched','security.session_high_risk','security.provider_key_anomaly'
  )),
  source_object_id TEXT NOT NULL CHECK(length(source_object_id) BETWEEN 36 AND 38),
  category TEXT NOT NULL CHECK(category IN(
    'data_loss_prevention','session_risk','provider_credential'
  )),
  severity TEXT NOT NULL CHECK(severity IN('low','medium','high','critical')),
  severity_score INTEGER NOT NULL CHECK(
    typeof(severity_score)='integer' AND severity_score BETWEEN 0 AND 10
  ),
  action TEXT NOT NULL CHECK(action IN(
    'allow','log_only','block','high_risk','freeze_session','freeze_virtual_key',
    'dismiss','release_session','release_virtual_key','expire',
    'authentication_failed','decrypt_failed'
  )),
  outcome TEXT NOT NULL CHECK(outcome IN(
    'observed','blocked','detected','frozen','released','resolved','expired','rejected'
  )),
  resource_type TEXT NOT NULL CHECK(resource_type IN(
    'dlp_rule','session_risk_score','provider'
  )),
  resource_id TEXT NOT NULL CHECK(
    length(resource_id) BETWEEN 1 AND 160 AND resource_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  source_request_id TEXT NOT NULL CHECK(
    length(source_request_id) BETWEEN 1 AND 128
    AND source_request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  source_trace_id TEXT NOT NULL CHECK(
    length(source_trace_id) BETWEEN 1 AND 128
    AND source_trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  occurrence_count INTEGER NOT NULL CHECK(
    typeof(occurrence_count)='integer' AND occurrence_count BETWEEN 1 AND 1000
  ),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(tenant_id,security_event_source_id),
  UNIQUE(tenant_id,source_event_type,source_object_id),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id)
);
CREATE INDEX idx_security_event_sources_tenant_time_prd081
  ON security_event_sources(tenant_id,occurred_at,security_event_source_id);

-- Aggregate 保存固定五秒窗口和可从 member 重算的首尾/计数事实。
CREATE TABLE security_event_aggregates (
  security_event_id TEXT PRIMARY KEY CHECK(
    length(security_event_id)=37 AND substr(security_event_id,1,5)='sevt_'
    AND substr(security_event_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  source_event_type TEXT NOT NULL,
  category TEXT NOT NULL,
  severity TEXT NOT NULL,
  severity_score INTEGER NOT NULL,
  action TEXT NOT NULL,
  outcome TEXT NOT NULL,
  resource_type TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  window_started_at TEXT NOT NULL CHECK(unixepoch(window_started_at) IS NOT NULL),
  window_ended_at TEXT NOT NULL CHECK(
    unixepoch(window_ended_at)=unixepoch(window_started_at)+5
  ),
  generation INTEGER NOT NULL CHECK(typeof(generation)='integer' AND generation>=0),
  event_count INTEGER NOT NULL CHECK(event_count BETWEEN 0 AND 1000000),
  source_event_count INTEGER NOT NULL CHECK(source_event_count BETWEEN 0 AND 1000),
  first_seen_at TEXT NOT NULL CHECK(unixepoch(first_seen_at) IS NOT NULL),
  last_seen_at TEXT NOT NULL CHECK(unixepoch(last_seen_at) IS NOT NULL),
  first_request_id TEXT NOT NULL,
  last_request_id TEXT NOT NULL,
  first_trace_id TEXT NOT NULL,
  last_trace_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  UNIQUE(tenant_id,security_event_id),
  UNIQUE(tenant_id,source_event_type,category,severity,severity_score,action,outcome,
    resource_type,resource_id,window_started_at,generation),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  CHECK(first_seen_at>=window_started_at AND last_seen_at<window_ended_at
    AND first_seen_at<=last_seen_at),
  CHECK(length(first_request_id) BETWEEN 1 AND 128
    AND first_request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'),
  CHECK(length(last_request_id) BETWEEN 1 AND 128
    AND last_request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'),
  CHECK(length(first_trace_id) BETWEEN 1 AND 128
    AND first_trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'),
  CHECK(length(last_trace_id) BETWEEN 1 AND 128
    AND last_trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*')
);

CREATE TABLE security_event_aggregate_members (
  security_event_aggregate_member_id TEXT PRIMARY KEY CHECK(
    length(security_event_aggregate_member_id)=38
    AND substr(security_event_aggregate_member_id,1,6)='sembr_'
    AND substr(security_event_aggregate_member_id,7) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  security_event_id TEXT NOT NULL,
  security_event_source_id TEXT NOT NULL,
  joined_at TEXT NOT NULL CHECK(unixepoch(joined_at) IS NOT NULL),
  UNIQUE(tenant_id,security_event_aggregate_member_id),
  UNIQUE(tenant_id,security_event_source_id),
  FOREIGN KEY(tenant_id,security_event_id)
    REFERENCES security_event_aggregates(tenant_id,security_event_id),
  FOREIGN KEY(tenant_id,security_event_source_id)
    REFERENCES security_event_sources(tenant_id,security_event_source_id)
);

-- Outbox 的 lease 与重试状态独立于 delivery 网络 attempt；零订阅同样会持久收敛。
CREATE TABLE security_event_outbox (
  security_event_outbox_id TEXT PRIMARY KEY CHECK(
    length(security_event_outbox_id)=37 AND substr(security_event_outbox_id,1,5)='seob_'
    AND substr(security_event_outbox_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  security_event_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('pending','processing','succeeded','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 10),
  lease_generation INTEGER NOT NULL DEFAULT 0 CHECK(lease_generation>=0),
  lease_token TEXT,
  lease_owner_id TEXT,
  lease_expires_at TEXT,
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  snapshot_source_count INTEGER,
  snapshot_endpoint_count INTEGER,
  last_error_code TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  succeeded_at TEXT,
  failed_at TEXT,
  UNIQUE(tenant_id,security_event_outbox_id),
  UNIQUE(tenant_id,security_event_id),
  FOREIGN KEY(tenant_id,security_event_id)
    REFERENCES security_event_aggregates(tenant_id,security_event_id),
  CHECK((lease_token IS NULL AND lease_owner_id IS NULL AND lease_expires_at IS NULL)
    OR (status='processing' AND lease_token IS NOT NULL AND lease_owner_id IS NOT NULL
      AND lease_expires_at IS NOT NULL AND unixepoch(lease_expires_at) IS NOT NULL)),
  CHECK(snapshot_source_count IS NULL OR snapshot_source_count BETWEEN 1 AND 1000),
  CHECK(snapshot_endpoint_count IS NULL OR snapshot_endpoint_count BETWEEN 0 AND 10000),
  CHECK(last_error_code IS NULL OR (length(last_error_code) BETWEEN 3 AND 120
    AND last_error_code NOT GLOB '*[^A-Za-z0-9_.:-]*')),
  CHECK((status='succeeded' AND succeeded_at IS NOT NULL AND failed_at IS NULL)
    OR (status='failed' AND failed_at IS NOT NULL AND succeeded_at IS NULL)
    OR (status IN('pending','processing') AND succeeded_at IS NULL AND failed_at IS NULL))
);
CREATE INDEX idx_security_event_outbox_due_prd081
  ON security_event_outbox(status,next_attempt_at,lease_expires_at,security_event_id);

-- 每条 security delivery 永久领取一个分钟 slot；正文、URL 和凭据均不进入限流证据。
CREATE TABLE webhook_security_rate_claims (
  webhook_security_rate_claim_id TEXT PRIMARY KEY CHECK(
    length(webhook_security_rate_claim_id)=37
    AND substr(webhook_security_rate_claim_id,1,5)='wsrc_'
    AND substr(webhook_security_rate_claim_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  webhook_endpoint_id TEXT NOT NULL,
  webhook_delivery_id TEXT NOT NULL,
  bucket_started_at TEXT NOT NULL CHECK(unixepoch(bucket_started_at) IS NOT NULL),
  bucket_ended_at TEXT NOT NULL CHECK(
    unixepoch(bucket_ended_at)=unixepoch(bucket_started_at)+60
  ),
  slot_number INTEGER NOT NULL CHECK(typeof(slot_number)='integer' AND slot_number BETWEEN 1 AND 600),
  rate_limit_per_minute INTEGER NOT NULL CHECK(
    typeof(rate_limit_per_minute)='integer' AND rate_limit_per_minute BETWEEN 1 AND 600
  ),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at)=unixepoch(bucket_ended_at)+2592000),
  UNIQUE(tenant_id,webhook_security_rate_claim_id),
  UNIQUE(tenant_id,webhook_delivery_id),
  UNIQUE(tenant_id,webhook_endpoint_id,bucket_started_at,slot_number),
  FOREIGN KEY(tenant_id,webhook_endpoint_id)
    REFERENCES webhook_endpoints(tenant_id,webhook_endpoint_id),
  FOREIGN KEY(tenant_id,webhook_delivery_id)
    REFERENCES webhook_deliveries(tenant_id,webhook_delivery_id),
  CHECK(slot_number<=rate_limit_per_minute)
);
CREATE INDEX idx_webhook_security_rate_expiry_prd081
  ON webhook_security_rate_claims(expires_at,webhook_delivery_id);

-- Source 只能逐字段重建自权威表；知道 source ID 不能伪造 tenant、动作、计数或资源。
CREATE TRIGGER trg_security_event_sources_owner_insert_prd081
BEFORE INSERT ON security_event_sources FOR EACH ROW
WHEN NOT (
  (NEW.source_event_type='security.dlp_matched' AND EXISTS(
    SELECT 1 FROM dlp_matches source
    WHERE source.tenant_id=NEW.tenant_id AND source.dlp_match_id=NEW.source_object_id
      AND length(source.dlp_match_id)=37 AND substr(source.dlp_match_id,1,5)='dlpm_'
      AND substr(source.dlp_match_id,6) NOT GLOB '*[^0-9a-f]*'
      AND NEW.security_event_source_id='sesrc_'||substr(source.dlp_match_id,-32)
      AND NEW.category='data_loss_prevention'
      AND NEW.severity=CASE source.action
        WHEN 'allow' THEN 'low' WHEN 'log_only' THEN 'medium' ELSE 'high' END
      AND NEW.severity_score=CASE source.action
        WHEN 'allow' THEN 3 WHEN 'log_only' THEN 5 ELSE 8 END
      AND NEW.action=source.action
      AND NEW.outcome=CASE WHEN source.action='block' THEN 'blocked' ELSE 'observed' END
      AND NEW.resource_type='dlp_rule' AND NEW.resource_id=source.dlp_rule_id
      AND NEW.source_request_id=source.request_id
      AND NEW.source_trace_id=COALESCE(source.trace_id,source.request_id)
      AND NEW.occurrence_count=1 AND NEW.occurred_at=source.created_at
      AND NEW.created_at=source.created_at
  ))
  OR (NEW.source_event_type='security.session_high_risk' AND EXISTS(
    SELECT 1 FROM session_risk_events source JOIN session_risk_actions action
      ON action.tenant_id=source.tenant_id
     AND action.session_risk_action_id=source.session_risk_action_id
    WHERE source.tenant_id=NEW.tenant_id
      AND source.session_risk_event_id=NEW.source_object_id
      AND length(source.session_risk_event_id)=38
      AND substr(source.session_risk_event_id,1,6)='srevt_'
      AND substr(source.session_risk_event_id,7) NOT GLOB '*[^0-9a-f]*'
      AND NEW.security_event_source_id='sesrc_'||substr(source.session_risk_event_id,-32)
      AND NEW.category='session_risk'
      AND NEW.severity=CASE WHEN action.action IN('freeze_session','freeze_virtual_key')
        THEN 'critical' WHEN action.action='high_risk' THEN 'high' ELSE 'medium' END
      AND NEW.severity_score=CASE WHEN action.action IN('freeze_session','freeze_virtual_key')
        THEN 10 WHEN action.action='high_risk' THEN 8 ELSE 4 END
      AND NEW.action=action.action
      AND NEW.outcome=CASE action.action
        WHEN 'high_risk' THEN 'detected'
        WHEN 'freeze_session' THEN 'frozen' WHEN 'freeze_virtual_key' THEN 'frozen'
        WHEN 'dismiss' THEN 'resolved'
        WHEN 'release_session' THEN 'released' WHEN 'release_virtual_key' THEN 'released'
        ELSE 'expired' END
      AND NEW.resource_type='session_risk_score'
      AND NEW.resource_id=source.session_risk_score_id
      AND NEW.source_request_id=source.source_request_id
      AND NEW.source_trace_id=source.source_trace_id
      AND NEW.occurrence_count=source.risky_turn_count
      AND NEW.occurred_at=source.occurred_at AND NEW.created_at=source.occurred_at
  ))
  OR (NEW.source_event_type='security.provider_key_anomaly'
    AND NEW.action='authentication_failed' AND EXISTS(
    SELECT 1 FROM provider_error_facts source
    WHERE source.tenant_id=NEW.tenant_id
      AND source.provider_error_fact_id=NEW.source_object_id
      AND source.normalized_output_error_code='gateway.upstream_auth_failed'
      AND length(source.provider_error_fact_id)=36
      AND substr(source.provider_error_fact_id,1,4)='pef_'
      AND substr(source.provider_error_fact_id,5) NOT GLOB '*[^0-9a-f]*'
      AND NEW.security_event_source_id='sesrc_'||substr(source.provider_error_fact_id,-32)
      AND NEW.category='provider_credential' AND NEW.severity='critical'
      AND NEW.severity_score=9 AND NEW.outcome='rejected'
      AND NEW.resource_type='provider' AND NEW.resource_id=source.provider_id
      AND NEW.source_request_id=source.request_id AND NEW.source_trace_id=source.trace_id
      AND NEW.occurrence_count=1 AND NEW.occurred_at=source.created_at
      AND NEW.created_at=source.created_at
  ))
  OR (NEW.source_event_type='security.provider_key_anomaly'
    AND NEW.action='decrypt_failed' AND EXISTS(
    SELECT 1 FROM provider_key_anomaly_events source
    WHERE source.tenant_id=NEW.tenant_id
      AND source.provider_key_anomaly_event_id=NEW.source_object_id
      AND NEW.security_event_source_id=
          'sesrc_'||substr(source.provider_key_anomaly_event_id,-32)
      AND NEW.category='provider_credential' AND NEW.severity='critical'
      AND NEW.severity_score=9 AND NEW.outcome='rejected'
      AND NEW.resource_type='provider' AND NEW.resource_id=source.provider_id
      AND NEW.source_request_id=source.request_id AND NEW.source_trace_id=source.trace_id
      AND NEW.occurrence_count=source.candidate_count
      AND NEW.occurred_at=source.occurred_at AND NEW.created_at=source.created_at
  ))
)
  OR EXISTS(SELECT 1 FROM security_event_sources current
    WHERE current.security_event_source_id=NEW.security_event_source_id
       OR (current.tenant_id=NEW.tenant_id
         AND current.source_event_type=NEW.source_event_type
         AND current.source_object_id=NEW.source_object_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'security_event_source_owner_mismatch'); END;

CREATE TRIGGER trg_security_event_sources_update_prd081
BEFORE UPDATE ON security_event_sources FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'security_event_source_immutable'); END;
CREATE TRIGGER trg_security_event_sources_delete_prd081
BEFORE DELETE ON security_event_sources FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'security_event_source_immutable'); END;

-- Aggregate 初始行必须由尚未归组的同租户 source 派生，generation 必须连续递增。
CREATE TRIGGER trg_security_event_aggregates_owner_insert_prd081
BEFORE INSERT ON security_event_aggregates FOR EACH ROW
WHEN NOT EXISTS(
    SELECT 1 FROM security_event_sources source
    WHERE NEW.security_event_id='sevt_'||substr(source.source_object_id,-32)
      AND source.tenant_id=NEW.tenant_id
      AND source.source_event_type=NEW.source_event_type AND source.category=NEW.category
      AND source.severity=NEW.severity AND source.severity_score=NEW.severity_score
      AND source.action=NEW.action AND source.outcome=NEW.outcome
      AND source.resource_type=NEW.resource_type AND source.resource_id=NEW.resource_id
      AND NEW.window_started_at=
        strftime('%Y-%m-%dT%H:%M:',source.occurred_at)
          ||printf('%02d',(CAST(strftime('%S',source.occurred_at) AS INTEGER)/5)*5)||'Z'
      AND NEW.window_ended_at=strftime('%Y-%m-%dT%H:%M:%SZ',NEW.window_started_at,'+5 seconds')
      AND NEW.event_count=0 AND NEW.source_event_count=0
      AND NEW.first_seen_at=source.occurred_at AND NEW.last_seen_at=source.occurred_at
      AND NEW.first_request_id=source.source_request_id
      AND NEW.last_request_id=source.source_request_id
      AND NEW.first_trace_id=source.source_trace_id AND NEW.last_trace_id=source.source_trace_id
      AND NEW.created_at=source.created_at AND NEW.updated_at=source.created_at
      AND NOT EXISTS(SELECT 1 FROM security_event_aggregate_members member
        WHERE member.tenant_id=source.tenant_id
          AND member.security_event_source_id=source.security_event_source_id)
  )
  OR NEW.generation!=(SELECT COALESCE(MAX(current.generation),-1)+1
    FROM security_event_aggregates current
    WHERE current.tenant_id=NEW.tenant_id
      AND current.source_event_type=NEW.source_event_type AND current.category=NEW.category
      AND current.severity=NEW.severity AND current.severity_score=NEW.severity_score
      AND current.action=NEW.action AND current.outcome=NEW.outcome
      AND current.resource_type=NEW.resource_type AND current.resource_id=NEW.resource_id
      AND current.window_started_at=NEW.window_started_at)
  OR EXISTS(SELECT 1 FROM security_event_aggregates current
    WHERE current.security_event_id=NEW.security_event_id
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'security_event_aggregate_owner_mismatch'); END;

-- Member 只能把同组 source 纳入仍为 pending 且未达到 1000 条的 aggregate。
CREATE TRIGGER trg_security_event_members_owner_insert_prd081
BEFORE INSERT ON security_event_aggregate_members FOR EACH ROW
WHEN NEW.security_event_aggregate_member_id!='sembr_'||substr(NEW.security_event_source_id,-32)
  OR NOT EXISTS(
    SELECT 1 FROM security_event_sources source JOIN security_event_aggregates aggregate
      ON aggregate.tenant_id=source.tenant_id
     AND aggregate.security_event_id=NEW.security_event_id
    JOIN security_event_outbox outbox
      ON outbox.tenant_id=aggregate.tenant_id
     AND outbox.security_event_id=aggregate.security_event_id
    WHERE source.tenant_id=NEW.tenant_id
      AND source.security_event_source_id=NEW.security_event_source_id
      AND source.source_event_type=aggregate.source_event_type
      AND source.category=aggregate.category AND source.severity=aggregate.severity
      AND source.severity_score=aggregate.severity_score AND source.action=aggregate.action
      AND source.outcome=aggregate.outcome AND source.resource_type=aggregate.resource_type
      AND source.resource_id=aggregate.resource_id
      AND source.occurred_at>=aggregate.window_started_at
      AND source.occurred_at<aggregate.window_ended_at
      AND outbox.status='pending' AND aggregate.source_event_count<1000
      AND NEW.joined_at=source.created_at
  )
  OR EXISTS(SELECT 1 FROM security_event_aggregate_members current
    WHERE current.security_event_aggregate_member_id=NEW.security_event_aggregate_member_id
       OR (current.tenant_id=NEW.tenant_id
         AND current.security_event_source_id=NEW.security_event_source_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'security_event_member_owner_mismatch'); END;

CREATE TRIGGER trg_security_event_members_update_prd081
BEFORE UPDATE ON security_event_aggregate_members FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'security_event_member_immutable'); END;
CREATE TRIGGER trg_security_event_members_delete_prd081
BEFORE DELETE ON security_event_aggregate_members FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'security_event_member_immutable'); END;

-- Aggregate UPDATE 只接受 pending generation 的 member 精确重算，身份和窗口不可改写。
CREATE TRIGGER trg_security_event_aggregates_recompute_prd081
BEFORE UPDATE ON security_event_aggregates FOR EACH ROW
WHEN NEW.security_event_id!=OLD.security_event_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.source_event_type!=OLD.source_event_type OR NEW.category!=OLD.category
  OR NEW.severity!=OLD.severity OR NEW.severity_score!=OLD.severity_score
  OR NEW.action!=OLD.action OR NEW.outcome!=OLD.outcome
  OR NEW.resource_type!=OLD.resource_type OR NEW.resource_id!=OLD.resource_id
  OR NEW.window_started_at!=OLD.window_started_at OR NEW.window_ended_at!=OLD.window_ended_at
  OR NEW.generation!=OLD.generation OR NEW.created_at!=OLD.created_at
  OR NOT EXISTS(SELECT 1 FROM security_event_outbox outbox
    WHERE outbox.tenant_id=OLD.tenant_id AND outbox.security_event_id=OLD.security_event_id
      AND outbox.status='pending')
  OR NEW.source_event_count!=(SELECT COUNT(*)
    FROM security_event_aggregate_members member
    WHERE member.tenant_id=OLD.tenant_id AND member.security_event_id=OLD.security_event_id)
  OR NEW.event_count!=(SELECT COALESCE(SUM(source.occurrence_count),0)
    FROM security_event_aggregate_members member JOIN security_event_sources source
      ON source.tenant_id=member.tenant_id
     AND source.security_event_source_id=member.security_event_source_id
    WHERE member.tenant_id=OLD.tenant_id AND member.security_event_id=OLD.security_event_id)
  OR NEW.first_seen_at!=(SELECT source.occurred_at
    FROM security_event_aggregate_members member JOIN security_event_sources source
      ON source.tenant_id=member.tenant_id
     AND source.security_event_source_id=member.security_event_source_id
    WHERE member.tenant_id=OLD.tenant_id AND member.security_event_id=OLD.security_event_id
    ORDER BY source.occurred_at,source.security_event_source_id LIMIT 1)
  OR NEW.last_seen_at!=(SELECT source.occurred_at
    FROM security_event_aggregate_members member JOIN security_event_sources source
      ON source.tenant_id=member.tenant_id
     AND source.security_event_source_id=member.security_event_source_id
    WHERE member.tenant_id=OLD.tenant_id AND member.security_event_id=OLD.security_event_id
    ORDER BY source.occurred_at DESC,source.security_event_source_id DESC LIMIT 1)
  OR NEW.first_request_id!=(SELECT source.source_request_id
    FROM security_event_aggregate_members member JOIN security_event_sources source
      ON source.tenant_id=member.tenant_id
     AND source.security_event_source_id=member.security_event_source_id
    WHERE member.tenant_id=OLD.tenant_id AND member.security_event_id=OLD.security_event_id
    ORDER BY source.occurred_at,source.security_event_source_id LIMIT 1)
  OR NEW.last_request_id!=(SELECT source.source_request_id
    FROM security_event_aggregate_members member JOIN security_event_sources source
      ON source.tenant_id=member.tenant_id
     AND source.security_event_source_id=member.security_event_source_id
    WHERE member.tenant_id=OLD.tenant_id AND member.security_event_id=OLD.security_event_id
    ORDER BY source.occurred_at DESC,source.security_event_source_id DESC LIMIT 1)
  OR NEW.first_trace_id!=(SELECT source.source_trace_id
    FROM security_event_aggregate_members member JOIN security_event_sources source
      ON source.tenant_id=member.tenant_id
     AND source.security_event_source_id=member.security_event_source_id
    WHERE member.tenant_id=OLD.tenant_id AND member.security_event_id=OLD.security_event_id
    ORDER BY source.occurred_at,source.security_event_source_id LIMIT 1)
  OR NEW.last_trace_id!=(SELECT source.source_trace_id
    FROM security_event_aggregate_members member JOIN security_event_sources source
      ON source.tenant_id=member.tenant_id
     AND source.security_event_source_id=member.security_event_source_id
    WHERE member.tenant_id=OLD.tenant_id AND member.security_event_id=OLD.security_event_id
    ORDER BY source.occurred_at DESC,source.security_event_source_id DESC LIMIT 1)
  OR NEW.updated_at!=(SELECT MAX(source.created_at)
    FROM security_event_aggregate_members member JOIN security_event_sources source
      ON source.tenant_id=member.tenant_id
     AND source.security_event_source_id=member.security_event_source_id
    WHERE member.tenant_id=OLD.tenant_id AND member.security_event_id=OLD.security_event_id)
BEGIN SELECT RAISE(ABORT,'security_event_aggregate_recompute_invalid'); END;

CREATE TRIGGER trg_security_event_aggregates_delete_prd081
BEFORE DELETE ON security_event_aggregates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'security_event_aggregate_immutable'); END;

-- Outbox 创建时必须一对一绑定 aggregate，并从窗口结束时刻开始可领取。
CREATE TRIGGER trg_security_event_outbox_owner_insert_prd081
BEFORE INSERT ON security_event_outbox FOR EACH ROW
WHEN NEW.security_event_outbox_id!='seob_'||substr(NEW.security_event_id,-32)
  OR NOT EXISTS(SELECT 1 FROM security_event_aggregates aggregate
    WHERE aggregate.tenant_id=NEW.tenant_id
      AND aggregate.security_event_id=NEW.security_event_id
      AND NEW.status='pending' AND NEW.attempt_count=0 AND NEW.lease_generation=0
      AND NEW.lease_token IS NULL AND NEW.lease_owner_id IS NULL
      AND NEW.lease_expires_at IS NULL
      AND NEW.next_attempt_at=aggregate.window_ended_at
      AND NEW.snapshot_source_count IS NULL AND NEW.snapshot_endpoint_count IS NULL
      AND NEW.last_error_code IS NULL AND NEW.created_at=aggregate.created_at
      AND NEW.updated_at=aggregate.created_at
      AND NEW.succeeded_at IS NULL AND NEW.failed_at IS NULL)
  OR EXISTS(SELECT 1 FROM security_event_outbox current
    WHERE current.security_event_outbox_id=NEW.security_event_outbox_id
       OR (current.tenant_id=NEW.tenant_id AND current.security_event_id=NEW.security_event_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'security_event_outbox_owner_mismatch'); END;

CREATE TRIGGER trg_security_event_outbox_identity_update_prd081
BEFORE UPDATE ON security_event_outbox FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.security_event_outbox_id!=OLD.security_event_outbox_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.security_event_id!=OLD.security_event_id
  OR NEW.created_at!=OLD.created_at OR NEW.attempt_count<OLD.attempt_count
  OR NEW.lease_generation<OLD.lease_generation
  OR NOT ((OLD.status='pending' AND NEW.status IN('pending','processing'))
    OR (OLD.status='processing' AND NEW.status IN('processing','pending','succeeded','failed'))
    OR (OLD.status IN('succeeded','failed') AND NEW.status=OLD.status))
BEGIN SELECT RAISE(ABORT,'security_event_outbox_state_invalid'); END;
CREATE TRIGGER trg_security_event_outbox_delete_prd081
BEFORE DELETE ON security_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'security_event_outbox_immutable'); END;

-- 插入 source 后在同一事务选择 pending generation；已领取或满 1000 条时创建下一代。
CREATE TRIGGER trg_security_event_sources_aggregate_prd081
AFTER INSERT ON security_event_sources FOR EACH ROW
BEGIN
  INSERT INTO security_event_aggregates(
    security_event_id,tenant_id,source_event_type,category,severity,severity_score,
    action,outcome,resource_type,resource_id,window_started_at,window_ended_at,generation,
    event_count,source_event_count,first_seen_at,last_seen_at,first_request_id,last_request_id,
    first_trace_id,last_trace_id,created_at,updated_at
  )
  SELECT
    'sevt_'||substr(NEW.source_object_id,-32),NEW.tenant_id,NEW.source_event_type,
    NEW.category,NEW.severity,NEW.severity_score,NEW.action,NEW.outcome,
    NEW.resource_type,NEW.resource_id,
    strftime('%Y-%m-%dT%H:%M:',NEW.occurred_at)
      ||printf('%02d',(CAST(strftime('%S',NEW.occurred_at) AS INTEGER)/5)*5)||'Z',
    strftime('%Y-%m-%dT%H:%M:%SZ',
      strftime('%Y-%m-%dT%H:%M:',NEW.occurred_at)
        ||printf('%02d',(CAST(strftime('%S',NEW.occurred_at) AS INTEGER)/5)*5)||'Z',
      '+5 seconds'),
    (SELECT COALESCE(MAX(current.generation),-1)+1 FROM security_event_aggregates current
      WHERE current.tenant_id=NEW.tenant_id
        AND current.source_event_type=NEW.source_event_type AND current.category=NEW.category
        AND current.severity=NEW.severity AND current.severity_score=NEW.severity_score
        AND current.action=NEW.action AND current.outcome=NEW.outcome
        AND current.resource_type=NEW.resource_type AND current.resource_id=NEW.resource_id
        AND current.window_started_at=
          strftime('%Y-%m-%dT%H:%M:',NEW.occurred_at)
            ||printf('%02d',(CAST(strftime('%S',NEW.occurred_at) AS INTEGER)/5)*5)||'Z'),
    0,0,NEW.occurred_at,NEW.occurred_at,NEW.source_request_id,NEW.source_request_id,
    NEW.source_trace_id,NEW.source_trace_id,NEW.created_at,NEW.created_at
  WHERE NOT EXISTS(
    SELECT 1 FROM security_event_aggregates aggregate JOIN security_event_outbox outbox
      ON outbox.tenant_id=aggregate.tenant_id
     AND outbox.security_event_id=aggregate.security_event_id
    WHERE aggregate.tenant_id=NEW.tenant_id
      AND aggregate.source_event_type=NEW.source_event_type AND aggregate.category=NEW.category
      AND aggregate.severity=NEW.severity AND aggregate.severity_score=NEW.severity_score
      AND aggregate.action=NEW.action AND aggregate.outcome=NEW.outcome
      AND aggregate.resource_type=NEW.resource_type AND aggregate.resource_id=NEW.resource_id
      AND aggregate.window_started_at=
        strftime('%Y-%m-%dT%H:%M:',NEW.occurred_at)
          ||printf('%02d',(CAST(strftime('%S',NEW.occurred_at) AS INTEGER)/5)*5)||'Z'
      AND outbox.status='pending' AND aggregate.source_event_count<1000
  );

  INSERT INTO security_event_outbox(
    security_event_outbox_id,tenant_id,security_event_id,status,attempt_count,
    lease_generation,lease_token,lease_owner_id,lease_expires_at,next_attempt_at,
    snapshot_source_count,snapshot_endpoint_count,last_error_code,created_at,updated_at,
    succeeded_at,failed_at
  )
  SELECT 'seob_'||substr(aggregate.security_event_id,-32),aggregate.tenant_id,
    aggregate.security_event_id,'pending',0,0,NULL,NULL,NULL,aggregate.window_ended_at,
    NULL,NULL,NULL,aggregate.created_at,aggregate.created_at,NULL,NULL
  FROM security_event_aggregates aggregate
  WHERE aggregate.tenant_id=NEW.tenant_id
    AND aggregate.source_event_type=NEW.source_event_type AND aggregate.category=NEW.category
    AND aggregate.severity=NEW.severity AND aggregate.severity_score=NEW.severity_score
    AND aggregate.action=NEW.action AND aggregate.outcome=NEW.outcome
    AND aggregate.resource_type=NEW.resource_type AND aggregate.resource_id=NEW.resource_id
    AND aggregate.window_started_at=
      strftime('%Y-%m-%dT%H:%M:',NEW.occurred_at)
        ||printf('%02d',(CAST(strftime('%S',NEW.occurred_at) AS INTEGER)/5)*5)||'Z'
    AND NOT EXISTS(SELECT 1 FROM security_event_outbox outbox
      WHERE outbox.tenant_id=aggregate.tenant_id
        AND outbox.security_event_id=aggregate.security_event_id);

  INSERT INTO security_event_aggregate_members(
    security_event_aggregate_member_id,tenant_id,security_event_id,
    security_event_source_id,joined_at
  )
  SELECT 'sembr_'||substr(NEW.security_event_source_id,-32),NEW.tenant_id,
    aggregate.security_event_id,NEW.security_event_source_id,NEW.created_at
  FROM security_event_aggregates aggregate JOIN security_event_outbox outbox
    ON outbox.tenant_id=aggregate.tenant_id
   AND outbox.security_event_id=aggregate.security_event_id
  WHERE aggregate.tenant_id=NEW.tenant_id
    AND aggregate.source_event_type=NEW.source_event_type AND aggregate.category=NEW.category
    AND aggregate.severity=NEW.severity AND aggregate.severity_score=NEW.severity_score
    AND aggregate.action=NEW.action AND aggregate.outcome=NEW.outcome
    AND aggregate.resource_type=NEW.resource_type AND aggregate.resource_id=NEW.resource_id
    AND aggregate.window_started_at=
      strftime('%Y-%m-%dT%H:%M:',NEW.occurred_at)
        ||printf('%02d',(CAST(strftime('%S',NEW.occurred_at) AS INTEGER)/5)*5)||'Z'
    AND outbox.status='pending' AND aggregate.source_event_count<1000
  ORDER BY aggregate.generation DESC LIMIT 1;

  UPDATE security_event_aggregates SET
    event_count=(SELECT SUM(source.occurrence_count)
      FROM security_event_aggregate_members member JOIN security_event_sources source
        ON source.tenant_id=member.tenant_id
       AND source.security_event_source_id=member.security_event_source_id
      WHERE member.tenant_id=NEW.tenant_id
        AND member.security_event_id=security_event_aggregates.security_event_id),
    source_event_count=(SELECT COUNT(*) FROM security_event_aggregate_members member
      WHERE member.tenant_id=NEW.tenant_id
        AND member.security_event_id=security_event_aggregates.security_event_id),
    first_seen_at=(SELECT source.occurred_at
      FROM security_event_aggregate_members member JOIN security_event_sources source
        ON source.tenant_id=member.tenant_id
       AND source.security_event_source_id=member.security_event_source_id
      WHERE member.tenant_id=NEW.tenant_id
        AND member.security_event_id=security_event_aggregates.security_event_id
      ORDER BY source.occurred_at,source.security_event_source_id LIMIT 1),
    last_seen_at=(SELECT source.occurred_at
      FROM security_event_aggregate_members member JOIN security_event_sources source
        ON source.tenant_id=member.tenant_id
       AND source.security_event_source_id=member.security_event_source_id
      WHERE member.tenant_id=NEW.tenant_id
        AND member.security_event_id=security_event_aggregates.security_event_id
      ORDER BY source.occurred_at DESC,source.security_event_source_id DESC LIMIT 1),
    first_request_id=(SELECT source.source_request_id
      FROM security_event_aggregate_members member JOIN security_event_sources source
        ON source.tenant_id=member.tenant_id
       AND source.security_event_source_id=member.security_event_source_id
      WHERE member.tenant_id=NEW.tenant_id
        AND member.security_event_id=security_event_aggregates.security_event_id
      ORDER BY source.occurred_at,source.security_event_source_id LIMIT 1),
    last_request_id=(SELECT source.source_request_id
      FROM security_event_aggregate_members member JOIN security_event_sources source
        ON source.tenant_id=member.tenant_id
       AND source.security_event_source_id=member.security_event_source_id
      WHERE member.tenant_id=NEW.tenant_id
        AND member.security_event_id=security_event_aggregates.security_event_id
      ORDER BY source.occurred_at DESC,source.security_event_source_id DESC LIMIT 1),
    first_trace_id=(SELECT source.source_trace_id
      FROM security_event_aggregate_members member JOIN security_event_sources source
        ON source.tenant_id=member.tenant_id
       AND source.security_event_source_id=member.security_event_source_id
      WHERE member.tenant_id=NEW.tenant_id
        AND member.security_event_id=security_event_aggregates.security_event_id
      ORDER BY source.occurred_at,source.security_event_source_id LIMIT 1),
    last_trace_id=(SELECT source.source_trace_id
      FROM security_event_aggregate_members member JOIN security_event_sources source
        ON source.tenant_id=member.tenant_id
       AND source.security_event_source_id=member.security_event_source_id
      WHERE member.tenant_id=NEW.tenant_id
        AND member.security_event_id=security_event_aggregates.security_event_id
      ORDER BY source.occurred_at DESC,source.security_event_source_id DESC LIMIT 1),
    updated_at=(SELECT MAX(source.created_at)
      FROM security_event_aggregate_members member JOIN security_event_sources source
        ON source.tenant_id=member.tenant_id
       AND source.security_event_source_id=member.security_event_source_id
      WHERE member.tenant_id=NEW.tenant_id
        AND member.security_event_id=security_event_aggregates.security_event_id)
  WHERE tenant_id=NEW.tenant_id AND security_event_id=(
    SELECT member.security_event_id FROM security_event_aggregate_members member
    WHERE member.tenant_id=NEW.tenant_id
      AND member.security_event_source_id=NEW.security_event_source_id
  );
END;

-- DLP nullable trace 只在 request_id 合法时回退；非法旧式直接 SQL 不产生可外发 source。
CREATE TRIGGER trg_dlp_matches_security_source_prd081
AFTER INSERT ON dlp_matches FOR EACH ROW
WHEN length(NEW.dlp_match_id)=37 AND substr(NEW.dlp_match_id,1,5)='dlpm_'
  AND substr(NEW.dlp_match_id,6) NOT GLOB '*[^0-9a-f]*'
  AND length(NEW.request_id) BETWEEN 1 AND 128
  AND NEW.request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  AND (NEW.trace_id IS NULL OR (length(NEW.trace_id) BETWEEN 1 AND 128
    AND NEW.trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'))
  AND length(NEW.dlp_rule_id) BETWEEN 1 AND 160
  AND NEW.dlp_rule_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  AND unixepoch(NEW.created_at) IS NOT NULL
BEGIN
  INSERT INTO security_event_sources(
    security_event_source_id,tenant_id,source_event_type,source_object_id,category,
    severity,severity_score,action,outcome,resource_type,resource_id,
    source_request_id,source_trace_id,occurrence_count,occurred_at,created_at
  ) VALUES(
    'sesrc_'||substr(NEW.dlp_match_id,-32),NEW.tenant_id,'security.dlp_matched',
    NEW.dlp_match_id,'data_loss_prevention',
    CASE NEW.action WHEN 'allow' THEN 'low' WHEN 'log_only' THEN 'medium' ELSE 'high' END,
    CASE NEW.action WHEN 'allow' THEN 3 WHEN 'log_only' THEN 5 ELSE 8 END,
    NEW.action,CASE WHEN NEW.action='block' THEN 'blocked' ELSE 'observed' END,
    'dlp_rule',NEW.dlp_rule_id,NEW.request_id,COALESCE(NEW.trace_id,NEW.request_id),
    1,NEW.created_at,NEW.created_at
  );
END;

CREATE TRIGGER trg_session_risk_events_security_source_prd081
AFTER INSERT ON session_risk_events FOR EACH ROW
WHEN length(NEW.session_risk_event_id)=38
  AND substr(NEW.session_risk_event_id,1,6)='srevt_'
  AND substr(NEW.session_risk_event_id,7) NOT GLOB '*[^0-9a-f]*'
  AND NEW.risky_turn_count BETWEEN 1 AND 1000
  AND NEW.source_request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  AND NEW.source_trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  AND NEW.session_risk_score_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
BEGIN
  INSERT INTO security_event_sources(
    security_event_source_id,tenant_id,source_event_type,source_object_id,category,
    severity,severity_score,action,outcome,resource_type,resource_id,
    source_request_id,source_trace_id,occurrence_count,occurred_at,created_at
  )
  SELECT 'sesrc_'||substr(NEW.session_risk_event_id,-32),NEW.tenant_id,
    'security.session_high_risk',NEW.session_risk_event_id,'session_risk',
    CASE WHEN action.action IN('freeze_session','freeze_virtual_key') THEN 'critical'
      WHEN action.action='high_risk' THEN 'high' ELSE 'medium' END,
    CASE WHEN action.action IN('freeze_session','freeze_virtual_key') THEN 10
      WHEN action.action='high_risk' THEN 8 ELSE 4 END,
    action.action,CASE action.action WHEN 'high_risk' THEN 'detected'
      WHEN 'freeze_session' THEN 'frozen' WHEN 'freeze_virtual_key' THEN 'frozen'
      WHEN 'dismiss' THEN 'resolved' WHEN 'release_session' THEN 'released'
      WHEN 'release_virtual_key' THEN 'released' ELSE 'expired' END,
    'session_risk_score',NEW.session_risk_score_id,NEW.source_request_id,
    NEW.source_trace_id,NEW.risky_turn_count,NEW.occurred_at,NEW.occurred_at
  FROM session_risk_actions action
  WHERE action.tenant_id=NEW.tenant_id
    AND action.session_risk_action_id=NEW.session_risk_action_id;
END;

CREATE TRIGGER trg_provider_error_facts_security_source_prd081
AFTER INSERT ON provider_error_facts FOR EACH ROW
WHEN NEW.normalized_output_error_code='gateway.upstream_auth_failed'
  AND length(NEW.provider_error_fact_id)=36
  AND substr(NEW.provider_error_fact_id,1,4)='pef_'
  AND substr(NEW.provider_error_fact_id,5) NOT GLOB '*[^0-9a-f]*'
  AND NEW.request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  AND NEW.trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  AND length(NEW.provider_id) BETWEEN 1 AND 160
  AND NEW.provider_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
BEGIN
  INSERT INTO security_event_sources(
    security_event_source_id,tenant_id,source_event_type,source_object_id,category,
    severity,severity_score,action,outcome,resource_type,resource_id,
    source_request_id,source_trace_id,occurrence_count,occurred_at,created_at
  ) VALUES(
    'sesrc_'||substr(NEW.provider_error_fact_id,-32),NEW.tenant_id,
    'security.provider_key_anomaly',NEW.provider_error_fact_id,'provider_credential',
    'critical',9,'authentication_failed','rejected','provider',NEW.provider_id,
    NEW.request_id,NEW.trace_id,1,NEW.created_at,NEW.created_at
  );
END;

CREATE TRIGGER trg_provider_key_anomaly_security_source_prd081
AFTER INSERT ON provider_key_anomaly_events FOR EACH ROW
BEGIN
  INSERT INTO security_event_sources(
    security_event_source_id,tenant_id,source_event_type,source_object_id,category,
    severity,severity_score,action,outcome,resource_type,resource_id,
    source_request_id,source_trace_id,occurrence_count,occurred_at,created_at
  ) VALUES(
    'sesrc_'||substr(NEW.provider_key_anomaly_event_id,-32),NEW.tenant_id,
    'security.provider_key_anomaly',NEW.provider_key_anomaly_event_id,'provider_credential',
    'critical',9,'decrypt_failed','rejected','provider',NEW.provider_id,
    NEW.request_id,NEW.trace_id,NEW.candidate_count,NEW.occurred_at,NEW.created_at
  );
END;

-- Rate claim 必须绑定同租户 pending 标准事件并连续领取 slot，不能伪造更高限额。
CREATE TRIGGER trg_webhook_security_rate_claim_owner_prd081
BEFORE INSERT ON webhook_security_rate_claims FOR EACH ROW
WHEN NEW.webhook_security_rate_claim_id!='wsrc_'||substr(NEW.webhook_delivery_id,-32)
  OR strftime('%Y-%m-%dT%H:%M:00Z',NEW.created_at)!=NEW.bucket_started_at
  OR NEW.bucket_ended_at!=strftime('%Y-%m-%dT%H:%M:%SZ',NEW.bucket_started_at,'+1 minute')
  OR NEW.expires_at!=strftime('%Y-%m-%dT%H:%M:%SZ',NEW.bucket_ended_at,'+30 days')
  OR NOT EXISTS(SELECT 1 FROM webhook_deliveries delivery
    WHERE delivery.tenant_id=NEW.tenant_id
      AND delivery.webhook_endpoint_id=NEW.webhook_endpoint_id
      AND delivery.webhook_delivery_id=NEW.webhook_delivery_id
      AND delivery.event_type='security.event_pushed' AND delivery.status='pending'
      AND delivery.security_event_rate_limit_per_minute=NEW.rate_limit_per_minute)
  OR NEW.slot_number!=(SELECT COALESCE(MAX(current.slot_number),0)+1
    FROM webhook_security_rate_claims current
    WHERE current.tenant_id=NEW.tenant_id
      AND current.webhook_endpoint_id=NEW.webhook_endpoint_id
      AND current.bucket_started_at=NEW.bucket_started_at)
  OR EXISTS(SELECT 1 FROM webhook_security_rate_claims current
    WHERE current.webhook_security_rate_claim_id=NEW.webhook_security_rate_claim_id
       OR (current.tenant_id=NEW.tenant_id
         AND current.webhook_delivery_id=NEW.webhook_delivery_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'webhook_security_rate_claim_invalid'); END;
CREATE TRIGGER trg_webhook_security_rate_claim_update_prd081
BEFORE UPDATE ON webhook_security_rate_claims FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'webhook_security_rate_claim_immutable'); END;
CREATE TRIGGER trg_webhook_security_rate_claim_delete_prd081
BEFORE DELETE ON webhook_security_rate_claims FOR EACH ROW
WHEN unixepoch(OLD.expires_at)>unixepoch('now') OR EXISTS(
  SELECT 1 FROM webhook_deliveries delivery
  WHERE delivery.tenant_id=OLD.tenant_id
    AND delivery.webhook_delivery_id=OLD.webhook_delivery_id
    AND delivery.status='pending'
)
BEGIN SELECT RAISE(ABORT,'webhook_security_rate_claim_immutable'); END;

COMMIT;
PRAGMA foreign_keys = ON;
