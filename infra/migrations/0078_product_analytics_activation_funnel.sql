-- PRD-085：建立租户归属的产品行为事实、激活漏斗及其可靠投递源发件箱。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 先清理本连接可能残留的临时门禁，确保只校验本次迁移输入。
DROP TABLE IF EXISTS temp.prd085_up_guard;
CREATE TEMP TABLE prd085_up_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd085_up_guard_abort
BEFORE INSERT ON prd085_up_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd085_up_guard_failed'); END;
-- 写入基线偏差计数，非 0077 与 RBAC-19 的数据库会在事务内立即中止。
INSERT INTO prd085_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT version FROM schema_migrations
      WHERE status='applied' ORDER BY version DESC LIMIT 1)='0077' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=88
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=19)
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||description||
          char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code
      ))='f18c61ce9d657a22a0592abb0922407b7bda7cc1130d93fe5eea959ee1dbd3b0'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=232
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code
      ))='0a448a84577e046654ed83d384f6cb59930477fcec78156f478a2aab66a5e239'
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name IN(
      'product_events','product_event_outbox',
      'rbac_permission_templates_prd085_new','permissions_prd085_new'))
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd085')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd085_up_guard_abort;
DROP TABLE prd085_up_guard;

CREATE TABLE product_events(
  product_event_id TEXT PRIMARY KEY CHECK(length(product_event_id)=36
    AND substr(product_event_id,1,4)='pev_'
    AND substr(product_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'tenant.registered','provider.created','virtual_key.created',
    'gateway.first_call_succeeded','budget.configured','teammate.invited',
    'request_logs.viewed')),
  actor_user_id TEXT,
  source_kind TEXT NOT NULL CHECK(source_kind IN(
    'tenant','seed_run','audit_log','request_log','request_log_query')),
  source_id TEXT NOT NULL CHECK(length(source_id) BETWEEN 1 AND 128
    AND instr(source_id,char(0))=0),
  operation_request_id TEXT CHECK(operation_request_id IS NULL OR(
    length(operation_request_id) BETWEEN 1 AND 128
    AND operation_request_id NOT GLOB '*[^A-Za-z0-9_.:-]*')),
  operation_trace_id TEXT CHECK(operation_trace_id IS NULL OR(
    length(operation_trace_id) BETWEEN 1 AND 128
    AND operation_trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*')),
  capture_mode TEXT NOT NULL CHECK(capture_mode IN('backfill','live')),
  status TEXT NOT NULL CHECK(status='recorded'),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(tenant_id,actor_user_id) REFERENCES users(tenant_id,user_id),
  UNIQUE(tenant_id,product_event_id),
  UNIQUE(tenant_id,source_kind,source_id,event_type),
  CHECK(updated_at=created_at)
);
CREATE INDEX idx_product_events_funnel_prd085
  ON product_events(tenant_id,event_type,occurred_at,product_event_id);
CREATE UNIQUE INDEX uq_product_events_once_prd085
  ON product_events(tenant_id,event_type)
  WHERE event_type IN('tenant.registered','gateway.first_call_succeeded');

CREATE TABLE product_event_outbox(
  product_event_outbox_id TEXT PRIMARY KEY CHECK(
    length(product_event_outbox_id)=37 AND substr(product_event_outbox_id,1,5)='pevo_'
    AND substr(product_event_outbox_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  product_event_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('pending','leased','sent','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(
    typeof(attempt_count)='integer' AND attempt_count BETWEEN 0 AND 10),
  generation INTEGER NOT NULL DEFAULT 0 CHECK(
    typeof(generation)='integer' AND generation>=0),
  lease_owner_id TEXT CHECK(lease_owner_id IS NULL OR(
    length(lease_owner_id) BETWEEN 1 AND 128 AND instr(lease_owner_id,char(0))=0)),
  lease_token TEXT CHECK(lease_token IS NULL OR(
    length(lease_token)=64 AND lease_token NOT GLOB '*[^0-9a-f]*')),
  lease_expires_at TEXT CHECK(
    lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  next_attempt_at TEXT CHECK(
    next_attempt_at IS NULL OR unixepoch(next_attempt_at) IS NOT NULL),
  subscription_snapshot_json TEXT CHECK(subscription_snapshot_json IS NULL OR(
    json_valid(subscription_snapshot_json)
    AND json_type(subscription_snapshot_json)='array'
    AND length(CAST(subscription_snapshot_json AS BLOB))<=65536)),
  snapshot_frozen_at TEXT CHECK(
    snapshot_frozen_at IS NULL OR unixepoch(snapshot_frozen_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL OR last_error_code IN(
    'webhook.payload_invalid','webhook.delivery_unavailable',
    'product_analytics.unavailable')),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,product_event_id)
    REFERENCES product_events(tenant_id,product_event_id),
  UNIQUE(tenant_id,product_event_id),
  CHECK(updated_at>=created_at),
  CHECK((subscription_snapshot_json IS NULL AND snapshot_frozen_at IS NULL)
    OR(subscription_snapshot_json IS NOT NULL AND snapshot_frozen_at IS NOT NULL)),
  CHECK(
    (status='pending' AND attempt_count BETWEEN 0 AND 9
      AND lease_owner_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL
      AND next_attempt_at IS NOT NULL AND delivered_at IS NULL)
    OR(status='leased' AND attempt_count BETWEEN 1 AND 10
      AND lease_owner_id IS NOT NULL AND lease_token IS NOT NULL
      AND lease_expires_at IS NOT NULL AND next_attempt_at IS NULL
      AND last_error_code IS NULL AND delivered_at IS NULL)
    OR(status='sent' AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND next_attempt_at IS NULL
      AND last_error_code IS NULL AND delivered_at IS NOT NULL)
    OR(status='failed' AND attempt_count=10 AND lease_owner_id IS NULL
      AND lease_token IS NULL AND lease_expires_at IS NULL
      AND next_attempt_at IS NULL AND last_error_code IS NOT NULL
      AND delivered_at IS NULL))
);
CREATE INDEX idx_product_event_outbox_claim_prd085
  ON product_event_outbox(status,next_attempt_at,tenant_id,created_at,product_event_outbox_id);

-- Freeze the complete expected historical projection before installing live source triggers.
CREATE TEMP TABLE prd085_expected_backfill(
  product_event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  actor_user_id TEXT,
  source_kind TEXT NOT NULL,
  source_id TEXT NOT NULL,
  operation_request_id TEXT,
  operation_trace_id TEXT,
  occurred_at TEXT NOT NULL
);

-- 从租户和首次成功种子记录重建注册事实，每个租户只保留权威首条来源。
INSERT INTO prd085_expected_backfill
SELECT
  'pev_'||substr(metahub_sha256(json_array(
    tenant.tenant_id,'tenant.registered',
    CASE WHEN seed.seed_run_id IS NULL THEN 'tenant' ELSE 'seed_run' END,
    COALESCE(seed.seed_run_id,tenant.tenant_id))),1,32),
  tenant.tenant_id,'tenant.registered',NULL,
  CASE WHEN seed.seed_run_id IS NULL THEN 'tenant' ELSE 'seed_run' END,
  COALESCE(seed.seed_run_id,tenant.tenant_id),NULL,NULL,
  COALESCE(seed.created_at,tenant.created_at)
FROM tenants tenant
LEFT JOIN seed_runs seed ON seed.seed_run_id=(
  SELECT candidate.seed_run_id FROM seed_runs candidate
  WHERE candidate.tenant_id=tenant.tenant_id AND candidate.status='applied'
  ORDER BY candidate.created_at,candidate.seed_run_id LIMIT 1);

-- 从审计日志投影 Provider、虚拟密钥、预算与邀请四类历史激活事实。
INSERT INTO prd085_expected_backfill
SELECT
  'pev_'||substr(metahub_sha256(json_array(
    audit.tenant_id,
    CASE audit.action
      WHEN 'provider.created' THEN 'provider.created'
      WHEN 'virtual_key.created' THEN 'virtual_key.created'
      WHEN 'budget_rule.created' THEN 'budget.configured'
      WHEN 'budget_rule.updated' THEN 'budget.configured'
      WHEN 'user.invited' THEN 'teammate.invited' END,
    'audit_log',audit.audit_log_id)),1,32),
  audit.tenant_id,
  CASE audit.action
    WHEN 'provider.created' THEN 'provider.created'
    WHEN 'virtual_key.created' THEN 'virtual_key.created'
    WHEN 'budget_rule.created' THEN 'budget.configured'
    WHEN 'budget_rule.updated' THEN 'budget.configured'
    WHEN 'user.invited' THEN 'teammate.invited' END,
  audit.actor_user_id,'audit_log',audit.audit_log_id,
  audit.request_id,audit.trace_id,audit.created_at
FROM audit_logs audit
WHERE audit.action IN(
  'provider.created','virtual_key.created','budget_rule.created',
  'budget_rule.updated','user.invited');

-- 配置导入包含 Provider 创建数量时补录对应历史激活事实。
INSERT INTO prd085_expected_backfill
SELECT
  'pev_'||substr(metahub_sha256(json_array(
    audit.tenant_id,'provider.created','audit_log',audit.audit_log_id)),1,32),
  audit.tenant_id,'provider.created',audit.actor_user_id,
  'audit_log',audit.audit_log_id,audit.request_id,audit.trace_id,audit.created_at
FROM audit_logs audit
WHERE audit.action='config.imported'
  AND json_type(audit.after_summary_json,'$.providers_created_count')='integer'
  AND json_extract(audit.after_summary_json,'$.providers_created_count')>0;

-- 配置导入包含虚拟密钥创建数量时补录对应历史激活事实。
INSERT INTO prd085_expected_backfill
SELECT
  'pev_'||substr(metahub_sha256(json_array(
    audit.tenant_id,'virtual_key.created','audit_log',audit.audit_log_id)),1,32),
  audit.tenant_id,'virtual_key.created',audit.actor_user_id,
  'audit_log',audit.audit_log_id,audit.request_id,audit.trace_id,audit.created_at
FROM audit_logs audit
WHERE audit.action='config.imported'
  AND json_type(audit.after_summary_json,'$.virtual_keys_created_count')='integer'
  AND json_extract(audit.after_summary_json,'$.virtual_keys_created_count')>0;

-- 每个租户仅以最早成功请求生成首次网关调用成功事实。
INSERT INTO prd085_expected_backfill
SELECT
  'pev_'||substr(metahub_sha256(json_array(
    request.tenant_id,'gateway.first_call_succeeded','request_log',request.request_id)),1,32),
  request.tenant_id,'gateway.first_call_succeeded',NULL,
  'request_log',request.request_id,request.request_id,request.trace_id,request.created_at
FROM request_logs request
WHERE request.status='success' AND NOT EXISTS(
  SELECT 1 FROM request_logs earlier
  WHERE earlier.tenant_id=request.tenant_id AND earlier.status='success'
    AND(earlier.created_at<request.created_at
      OR(earlier.created_at=request.created_at AND earlier.request_id<request.request_id)));

-- 将请求日志查询审计记录投影为查看日志的历史激活事实。
INSERT INTO prd085_expected_backfill
SELECT
  'pev_'||substr(metahub_sha256(json_array(
    query.tenant_id,'request_logs.viewed','request_log_query',query.query_event_id)),1,32),
  query.tenant_id,'request_logs.viewed',query.actor_user_id,
  'request_log_query',query.query_event_id,
  query.operation_request_id,query.operation_trace_id,query.executed_at
FROM request_log_query_events query;

-- 将冻结后的完整历史投影一次性写入不可变产品事实表。
INSERT INTO product_events(
  product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id,
  operation_request_id,operation_trace_id,capture_mode,status,
  occurred_at,created_at,updated_at)
SELECT product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id,
  operation_request_id,operation_trace_id,'backfill','recorded',occurred_at,
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd085_expected_backfill;

-- Every post-migration insert must be an exact projection of one authoritative source row.
CREATE TRIGGER trg_product_events_owner_insert_prd085
BEFORE INSERT ON product_events FOR EACH ROW
WHEN NEW.product_event_id!='pev_'||substr(metahub_sha256(json_array(
      NEW.tenant_id,NEW.event_type,NEW.source_kind,NEW.source_id)),1,32)
  OR NEW.status!='recorded' OR NEW.updated_at!=NEW.created_at
  OR EXISTS(SELECT 1 FROM product_events current
    WHERE current.product_event_id=NEW.product_event_id
      OR(current.tenant_id=NEW.tenant_id AND current.source_kind=NEW.source_kind
        AND current.source_id=NEW.source_id AND current.event_type=NEW.event_type)
      OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
  OR NOT(
    (NEW.source_kind='tenant' AND NEW.event_type='tenant.registered'
      AND NEW.capture_mode='backfill' AND NEW.actor_user_id IS NULL
      AND NEW.source_id=NEW.tenant_id AND NEW.operation_request_id IS NULL
      AND NEW.operation_trace_id IS NULL
      AND EXISTS(SELECT 1 FROM tenants source
        WHERE source.tenant_id=NEW.tenant_id AND source.created_at=NEW.occurred_at))
    OR(NEW.source_kind='seed_run' AND NEW.event_type='tenant.registered'
      AND NEW.capture_mode='backfill' AND NEW.actor_user_id IS NULL
      AND NEW.operation_request_id IS NULL AND NEW.operation_trace_id IS NULL
      AND EXISTS(SELECT 1 FROM seed_runs source
        WHERE source.seed_run_id=NEW.source_id AND source.tenant_id=NEW.tenant_id
          AND source.status='applied' AND source.created_at=NEW.occurred_at))
    OR(NEW.source_kind='audit_log' AND NEW.capture_mode='live'
      AND EXISTS(SELECT 1 FROM audit_logs source
        WHERE source.audit_log_id=NEW.source_id AND source.tenant_id=NEW.tenant_id
          AND source.actor_user_id=NEW.actor_user_id
          AND source.request_id=NEW.operation_request_id
          AND source.trace_id=NEW.operation_trace_id
          AND source.created_at=NEW.occurred_at
          AND(
            (NEW.event_type='provider.created' AND(
              source.action='provider.created'
              OR(source.action='config.imported'
                AND json_type(source.after_summary_json,'$.providers_created_count')='integer'
                AND json_extract(source.after_summary_json,'$.providers_created_count')>0)))
            OR(NEW.event_type='virtual_key.created' AND(
              source.action='virtual_key.created'
              OR(source.action='config.imported'
                AND json_type(source.after_summary_json,'$.virtual_keys_created_count')='integer'
                AND json_extract(source.after_summary_json,'$.virtual_keys_created_count')>0)))
            OR(NEW.event_type='budget.configured'
              AND source.action IN('budget_rule.created','budget_rule.updated'))
            OR(NEW.event_type='teammate.invited' AND source.action='user.invited'))))
    OR(NEW.source_kind='request_log'
      AND NEW.event_type='gateway.first_call_succeeded' AND NEW.capture_mode='live'
      AND NEW.actor_user_id IS NULL AND NEW.source_id=NEW.operation_request_id
      AND EXISTS(SELECT 1 FROM request_logs source
        WHERE source.tenant_id=NEW.tenant_id AND source.request_id=NEW.source_id
          AND source.status='success' AND source.trace_id=NEW.operation_trace_id
          AND NEW.occurred_at IN(source.created_at,COALESCE(source.updated_at,source.created_at))))
    OR(NEW.source_kind='request_log_query'
      AND NEW.event_type='request_logs.viewed' AND NEW.capture_mode='live'
      AND EXISTS(SELECT 1 FROM request_log_query_events source
        WHERE source.query_event_id=NEW.source_id AND source.tenant_id=NEW.tenant_id
          AND source.actor_user_id=NEW.actor_user_id
          AND source.operation_request_id=NEW.operation_request_id
          AND source.operation_trace_id=NEW.operation_trace_id
          AND source.executed_at=NEW.occurred_at)))
BEGIN SELECT RAISE(ABORT,'product_event_owner_mismatch'); END;

CREATE TRIGGER trg_product_events_update_prd085
BEFORE UPDATE ON product_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'product_event_immutable'); END;
CREATE TRIGGER trg_product_events_delete_prd085
BEFORE DELETE ON product_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'product_event_immutable'); END;

CREATE TRIGGER trg_product_event_outbox_owner_insert_prd085
BEFORE INSERT ON product_event_outbox FOR EACH ROW
WHEN NEW.product_event_outbox_id!='pevo_'||
      substr(metahub_sha256(NEW.product_event_id),1,32)
  OR NEW.status!='pending' OR NEW.attempt_count!=0 OR NEW.generation!=0
  OR NEW.lease_owner_id IS NOT NULL OR NEW.lease_token IS NOT NULL
  OR NEW.lease_expires_at IS NOT NULL OR NEW.last_error_code IS NOT NULL
  OR NEW.delivered_at IS NOT NULL OR NEW.subscription_snapshot_json IS NOT NULL
  OR NEW.snapshot_frozen_at IS NOT NULL OR NEW.next_attempt_at<NEW.created_at
  OR NEW.updated_at!=NEW.created_at
  OR NOT EXISTS(SELECT 1 FROM product_events event
    WHERE event.tenant_id=NEW.tenant_id
      AND event.product_event_id=NEW.product_event_id
      AND event.capture_mode='live')
  OR EXISTS(SELECT 1 FROM product_event_outbox current
    WHERE current.product_event_outbox_id=NEW.product_event_outbox_id
      OR(current.tenant_id=NEW.tenant_id
        AND current.product_event_id=NEW.product_event_id)
      OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'product_event_outbox_owner_mismatch'); END;

CREATE TRIGGER trg_product_event_outbox_transition_prd085
BEFORE UPDATE ON product_event_outbox FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.product_event_outbox_id!=OLD.product_event_outbox_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.product_event_id!=OLD.product_event_id
  OR NEW.created_at!=OLD.created_at OR NEW.updated_at<OLD.updated_at
  OR NOT(
    (NEW.subscription_snapshot_json IS OLD.subscription_snapshot_json
      AND NEW.snapshot_frozen_at IS OLD.snapshot_frozen_at)
    OR(OLD.status='pending' AND NEW.status='leased'
      AND OLD.subscription_snapshot_json IS NULL AND OLD.snapshot_frozen_at IS NULL
      AND NEW.subscription_snapshot_json IS NOT NULL
      AND NEW.snapshot_frozen_at IS NOT NULL))
  OR NOT(
    (OLD.status='pending' AND NEW.status='leased'
      AND NEW.attempt_count=OLD.attempt_count+1
      AND NEW.generation=OLD.generation+1)
    OR(OLD.status='leased' AND NEW.status='leased'
      AND NEW.attempt_count=OLD.attempt_count
      AND NEW.generation=OLD.generation+1)
    OR(OLD.status='leased' AND NEW.status='pending'
      AND NEW.attempt_count=OLD.attempt_count
      AND NEW.generation=OLD.generation+1)
    OR(OLD.status='leased' AND NEW.status IN('sent','failed')
      AND NEW.attempt_count=OLD.attempt_count
      AND NEW.generation=OLD.generation+1))
BEGIN SELECT RAISE(ABORT,'product_event_outbox_transition_invalid'); END;

CREATE TRIGGER trg_product_event_outbox_delete_prd085
BEFORE DELETE ON product_event_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'product_event_outbox_immutable'); END;

CREATE TRIGGER trg_product_events_outbox_insert_prd085
AFTER INSERT ON product_events FOR EACH ROW WHEN NEW.capture_mode='live'
BEGIN
  -- 实时事实必须同步创建待投递源发件箱记录，历史回填不得触发外发。
  INSERT INTO product_event_outbox(
    product_event_outbox_id,tenant_id,product_event_id,status,attempt_count,generation,
    lease_owner_id,lease_token,lease_expires_at,next_attempt_at,
    subscription_snapshot_json,snapshot_frozen_at,last_error_code,delivered_at,
    created_at,updated_at)
  VALUES(
    'pevo_'||substr(metahub_sha256(NEW.product_event_id),1,32),
    NEW.tenant_id,NEW.product_event_id,'pending',0,0,NULL,NULL,NULL,NEW.created_at,
    NULL,NULL,NULL,NULL,NEW.created_at,NEW.created_at);
END;

CREATE TRIGGER trg_seed_runs_product_event_insert_prd085
AFTER INSERT ON seed_runs FOR EACH ROW
WHEN NEW.status='applied' AND NEW.tenant_id IS NOT NULL
BEGIN
  -- 新租户完成种子初始化后补齐唯一注册事实，不为重复种子生成第二条。
  INSERT INTO product_events(
    product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id,
    operation_request_id,operation_trace_id,capture_mode,status,
    occurred_at,created_at,updated_at)
  SELECT
    'pev_'||substr(metahub_sha256(json_array(
      NEW.tenant_id,'tenant.registered','seed_run',NEW.seed_run_id)),1,32),
    NEW.tenant_id,'tenant.registered',NULL,'seed_run',NEW.seed_run_id,NULL,NULL,
    'backfill','recorded',NEW.created_at,
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  WHERE NOT EXISTS(SELECT 1 FROM product_events current
    WHERE current.tenant_id=NEW.tenant_id AND current.event_type='tenant.registered');
END;

CREATE TRIGGER trg_audit_logs_product_event_insert_prd085
AFTER INSERT ON audit_logs FOR EACH ROW
BEGIN
  -- 常规审计动作按白名单映射为当前租户的实时激活事实。
  INSERT INTO product_events(
    product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id,
    operation_request_id,operation_trace_id,capture_mode,status,
    occurred_at,created_at,updated_at)
  SELECT
    'pev_'||substr(metahub_sha256(json_array(
      NEW.tenant_id,
      CASE NEW.action
        WHEN 'provider.created' THEN 'provider.created'
        WHEN 'virtual_key.created' THEN 'virtual_key.created'
        WHEN 'budget_rule.created' THEN 'budget.configured'
        WHEN 'budget_rule.updated' THEN 'budget.configured'
        WHEN 'user.invited' THEN 'teammate.invited' END,
      'audit_log',NEW.audit_log_id)),1,32),
    NEW.tenant_id,
    CASE NEW.action
      WHEN 'provider.created' THEN 'provider.created'
      WHEN 'virtual_key.created' THEN 'virtual_key.created'
      WHEN 'budget_rule.created' THEN 'budget.configured'
      WHEN 'budget_rule.updated' THEN 'budget.configured'
      WHEN 'user.invited' THEN 'teammate.invited' END,
    NEW.actor_user_id,'audit_log',NEW.audit_log_id,NEW.request_id,NEW.trace_id,
    'live','recorded',NEW.created_at,
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  WHERE NEW.action IN(
    'provider.created','virtual_key.created','budget_rule.created',
    'budget_rule.updated','user.invited')
    AND NOT EXISTS(SELECT 1 FROM product_events current
      WHERE current.tenant_id=NEW.tenant_id AND current.source_kind='audit_log'
        AND current.source_id=NEW.audit_log_id
        AND current.event_type=CASE NEW.action
          WHEN 'provider.created' THEN 'provider.created'
          WHEN 'virtual_key.created' THEN 'virtual_key.created'
          WHEN 'budget_rule.created' THEN 'budget.configured'
          WHEN 'budget_rule.updated' THEN 'budget.configured'
          WHEN 'user.invited' THEN 'teammate.invited' END);

  -- 配置导入确实创建 Provider 时生成一条去重后的实时事实。
  INSERT INTO product_events(
    product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id,
    operation_request_id,operation_trace_id,capture_mode,status,
    occurred_at,created_at,updated_at)
  SELECT
    'pev_'||substr(metahub_sha256(json_array(
      NEW.tenant_id,'provider.created','audit_log',NEW.audit_log_id)),1,32),
    NEW.tenant_id,'provider.created',NEW.actor_user_id,'audit_log',NEW.audit_log_id,
    NEW.request_id,NEW.trace_id,'live','recorded',NEW.created_at,
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  WHERE NEW.action='config.imported'
    AND json_type(NEW.after_summary_json,'$.providers_created_count')='integer'
    AND json_extract(NEW.after_summary_json,'$.providers_created_count')>0
    AND NOT EXISTS(SELECT 1 FROM product_events current
      WHERE current.tenant_id=NEW.tenant_id AND current.source_kind='audit_log'
        AND current.source_id=NEW.audit_log_id AND current.event_type='provider.created');

  -- 配置导入确实创建虚拟密钥时生成一条去重后的实时事实。
  INSERT INTO product_events(
    product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id,
    operation_request_id,operation_trace_id,capture_mode,status,
    occurred_at,created_at,updated_at)
  SELECT
    'pev_'||substr(metahub_sha256(json_array(
      NEW.tenant_id,'virtual_key.created','audit_log',NEW.audit_log_id)),1,32),
    NEW.tenant_id,'virtual_key.created',NEW.actor_user_id,'audit_log',NEW.audit_log_id,
    NEW.request_id,NEW.trace_id,'live','recorded',NEW.created_at,
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  WHERE NEW.action='config.imported'
    AND json_type(NEW.after_summary_json,'$.virtual_keys_created_count')='integer'
    AND json_extract(NEW.after_summary_json,'$.virtual_keys_created_count')>0
    AND NOT EXISTS(SELECT 1 FROM product_events current
      WHERE current.tenant_id=NEW.tenant_id AND current.source_kind='audit_log'
        AND current.source_id=NEW.audit_log_id
        AND current.event_type='virtual_key.created');
END;

CREATE TRIGGER trg_request_logs_product_event_insert_prd085
AFTER INSERT ON request_logs FOR EACH ROW WHEN NEW.status='success'
BEGIN
  -- 首条直接写入的成功请求为租户生成唯一首次调用成功事实。
  INSERT INTO product_events(
    product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id,
    operation_request_id,operation_trace_id,capture_mode,status,
    occurred_at,created_at,updated_at)
  SELECT
    'pev_'||substr(metahub_sha256(json_array(
      NEW.tenant_id,'gateway.first_call_succeeded','request_log',NEW.request_id)),1,32),
    NEW.tenant_id,'gateway.first_call_succeeded',NULL,'request_log',NEW.request_id,
    NEW.request_id,NEW.trace_id,'live','recorded',NEW.created_at,
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  WHERE NOT EXISTS(SELECT 1 FROM product_events current
    WHERE current.tenant_id=NEW.tenant_id
      AND current.event_type='gateway.first_call_succeeded');
END;

CREATE TRIGGER trg_request_logs_product_event_update_prd085
AFTER UPDATE OF status ON request_logs FOR EACH ROW
WHEN OLD.status!='success' AND NEW.status='success'
BEGIN
  -- 请求状态后续转为成功时也生成首次调用事实，并继续执行租户级去重。
  INSERT INTO product_events(
    product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id,
    operation_request_id,operation_trace_id,capture_mode,status,
    occurred_at,created_at,updated_at)
  SELECT
    'pev_'||substr(metahub_sha256(json_array(
      NEW.tenant_id,'gateway.first_call_succeeded','request_log',NEW.request_id)),1,32),
    NEW.tenant_id,'gateway.first_call_succeeded',NULL,'request_log',NEW.request_id,
    NEW.request_id,NEW.trace_id,'live','recorded',COALESCE(NEW.updated_at,NEW.created_at),
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  WHERE NOT EXISTS(SELECT 1 FROM product_events current
    WHERE current.tenant_id=NEW.tenant_id
      AND current.event_type='gateway.first_call_succeeded');
END;

CREATE TRIGGER trg_request_log_queries_product_event_insert_prd085
AFTER INSERT ON request_log_query_events FOR EACH ROW
BEGIN
  -- 每次权威查询审计记录生成一条租户内可追踪的日志查看事实。
  INSERT INTO product_events(
    product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id,
    operation_request_id,operation_trace_id,capture_mode,status,
    occurred_at,created_at,updated_at)
  SELECT
    'pev_'||substr(metahub_sha256(json_array(
      NEW.tenant_id,'request_logs.viewed','request_log_query',NEW.query_event_id)),1,32),
    NEW.tenant_id,'request_logs.viewed',NEW.actor_user_id,
    'request_log_query',NEW.query_event_id,
    NEW.operation_request_id,NEW.operation_trace_id,'live','recorded',NEW.executed_at,
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  WHERE NOT EXISTS(SELECT 1 FROM product_events current
    WHERE current.tenant_id=NEW.tenant_id AND current.source_kind='request_log_query'
      AND current.source_id=NEW.query_event_id
      AND current.event_type='request_logs.viewed');
END;

-- Expand only exact 0077 default matrices; customized roles remain unchanged.
CREATE TEMP TABLE prd085_roles_to_expand AS
SELECT role.role_id,role.tenant_id,role.code
FROM roles role
WHERE role.code IN('admin','ops_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '76c109f34e195c8bdce18c7b6bdb9305aac0bc4dc292d1f46a8cd5def631d074'
    WHEN 'ops_admin' THEN 'aae41c1ea441876e4b810ba4f80f0de4ac7b1d8cb2a6386c07e3564ab7e44dba'
  END
  AND NOT EXISTS(
    SELECT 1 FROM rbac_role_permission_templates template
    WHERE template.role_code=role.code AND NOT EXISTS(
      SELECT 1 FROM role_permissions relation JOIN permissions permission
        ON permission.tenant_id=relation.tenant_id
       AND permission.permission_id=relation.permission_id
      WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
        AND permission.code=template.permission_code))
  AND NOT EXISTS(
    SELECT 1 FROM role_permissions relation JOIN permissions permission
      ON permission.tenant_id=relation.tenant_id
     AND permission.permission_id=relation.permission_id
    WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
      AND NOT EXISTS(SELECT 1 FROM rbac_role_permission_templates template
        WHERE template.role_code=role.code AND template.permission_code=permission.code));

DROP TRIGGER trg_tenants_rbac_initialize_prd059;
DROP TRIGGER trg_roles_owner_insert_prd059;
DROP TRIGGER trg_roles_matrix_update_prd059;
DROP TRIGGER trg_role_permissions_owner_insert_prd059;
DROP TRIGGER trg_role_permissions_delete_prd059;
DROP TRIGGER trg_rbac_permission_templates_insert_prd059;
DROP TRIGGER trg_rbac_permission_templates_update_prd059;
DROP TRIGGER trg_rbac_permission_templates_delete_prd059;
DROP TRIGGER trg_rbac_role_templates_insert_prd059;
DROP TRIGGER trg_rbac_role_templates_update_prd059;
DROP TRIGGER trg_rbac_role_templates_delete_prd059;
DROP TRIGGER trg_permissions_owner_insert_prd059;
DROP TRIGGER trg_permissions_update_prd059;
DROP TRIGGER trg_permissions_delete_prd059;
DROP TRIGGER trg_permissions_rowid_insert_prd059;

CREATE TABLE rbac_permission_templates_prd085_new(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=20)
);
-- 将既有权限模板逐行保序复制到修订二十的新目录结构。
INSERT INTO rbac_permission_templates_prd085_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision)
SELECT rowid,permission_code,category,name,description,risk_level,20
FROM rbac_permission_templates;
-- 在权限模板目录中登记只读的产品激活漏斗权限定义。
INSERT INTO rbac_permission_templates_prd085_new(
  permission_code,category,name,description,risk_level,catalog_revision)
VALUES(
  'product_analytics.read','analytics','读取产品激活漏斗',
  '读取当前租户产品使用行为聚合与七阶段激活漏斗。','read',20);
DROP TABLE rbac_permission_templates;
-- 用完成校验与填充的新表原子替换旧权限模板目录。
ALTER TABLE rbac_permission_templates_prd085_new RENAME TO rbac_permission_templates;

-- 仅为管理员与运维管理员登记产品分析权限的默认授予关系。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
VALUES('admin','product_analytics.read',1),('ops_admin','product_analytics.read',1);

CREATE TABLE permissions_prd085_new(
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=20),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),
  UNIQUE(tenant_id,permission_id)
);
-- 将所有租户已有权限实例保序升级到目录修订二十。
INSERT INTO permissions_prd085_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  20,status,created_at,updated_at FROM permissions;
-- 为每个现有租户创建产品分析读取权限实例，保持租户数据完整隔离。
INSERT INTO permissions_prd085_new(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at)
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
  template.category,template.name,template.description,template.risk_level,20,'active',
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant JOIN rbac_permission_templates template
  ON template.permission_code='product_analytics.read';
DROP TABLE permissions;
-- 用已补齐新权限实例的表原子替换租户权限旧表。
ALTER TABLE permissions_prd085_new RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);

-- 只向迁移前仍保持默认矩阵的管理员和运维管理员授予新权限。
INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
  permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd085_roles_to_expand role JOIN permissions permission
  ON permission.tenant_id=role.tenant_id
 AND permission.code='product_analytics.read';
-- 同步递增被扩展角色的修订号，并写入审定后的新矩阵哈希。
UPDATE roles SET permission_revision=permission_revision+1,
  matrix_sha256=CASE code
    WHEN 'admin' THEN 'e9fc70ecbb0aee117f913bad242eb62d23ef0374a905497ee391caae31951272'
    WHEN 'ops_admin' THEN 'c86c645b5c090c3ad8b1152ed783335ba82c788fc9690c4d803dd76f14eba8e7'
  END
WHERE role_id IN(SELECT role_id FROM prd085_roles_to_expand);

CREATE TRIGGER trg_rbac_permission_templates_insert_prd059
BEFORE INSERT ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_permission_templates_update_prd059
BEFORE UPDATE ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_permission_templates_delete_prd059
BEFORE DELETE ON rbac_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_insert_prd059
BEFORE INSERT ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_update_prd059
BEFORE UPDATE ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_delete_prd059
BEFORE DELETE ON rbac_role_permission_templates FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_permissions_owner_insert_prd059
BEFORE INSERT ON permissions FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM rbac_permission_templates template
  WHERE template.permission_code=NEW.code AND template.category=NEW.category
    AND template.name=NEW.name AND template.description=NEW.description
    AND template.risk_level=NEW.risk_level
    AND template.catalog_revision=NEW.catalog_revision)
  OR NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id)
  OR NEW.status!='active'
  OR EXISTS(SELECT 1 FROM permissions current
    WHERE current.permission_id=NEW.permission_id
      OR(current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
      OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_update_prd059
BEFORE UPDATE ON permissions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_delete_prd059
BEFORE DELETE ON permissions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_rowid_insert_prd059
AFTER INSERT ON permissions FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_roles_owner_insert_prd059
BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id)
  OR NEW.code NOT IN('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN 'e9fc70ecbb0aee117f913bad242eb62d23ef0374a905497ee391caae31951272'
    WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1'
    WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
    WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
    WHEN 'security_admin' THEN 'aeb4d6b081dce8ed36c6513b567aba4be395816c5a5ad2f1d939527a50ad14df'
    WHEN 'ops_admin' THEN 'c86c645b5c090c3ad8b1152ed783335ba82c788fc9690c4d803dd76f14eba8e7'
  END
  OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END
  OR EXISTS(SELECT 1 FROM roles current WHERE current.role_id=NEW.role_id
    OR(current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
    OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_matrix_update_prd059
BEFORE UPDATE ON roles FOR EACH ROW
WHEN OLD.code='admin' OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code OR NEW.name!=OLD.name
  OR NEW.description!=OLD.description OR NEW.status!=OLD.status
  OR NEW.created_at!=OLD.created_at
  OR NEW.is_permission_immutable!=OLD.is_permission_immutable
  OR NEW.permission_revision!=OLD.permission_revision+1
  OR NEW.matrix_sha256=OLD.matrix_sha256 OR length(NEW.matrix_sha256)!=64
  OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*'
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_owner_insert_prd059
BEFORE INSERT ON role_permissions FOR EACH ROW
WHEN NOT EXISTS(
  SELECT 1 FROM roles role JOIN permissions permission
    ON permission.tenant_id=role.tenant_id
  JOIN rbac_role_permission_templates template ON template.role_code=role.code
    AND template.permission_code=permission.code
  WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id
    AND role.status='active' AND permission.permission_id=NEW.permission_id
    AND permission.status='active')
  OR EXISTS(SELECT 1 FROM role_permissions current
    WHERE current.role_permission_id=NEW.role_permission_id
      OR(current.tenant_id=NEW.tenant_id AND current.role_id=NEW.role_id
        AND current.permission_id=NEW.permission_id)
      OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_owner_mismatch'); END;
CREATE TRIGGER trg_role_permissions_delete_prd059
BEFORE DELETE ON role_permissions FOR EACH ROW
WHEN EXISTS(SELECT 1 FROM roles role WHERE role.tenant_id=OLD.tenant_id
  AND role.role_id=OLD.role_id AND role.code='admin')
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;

CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 新租户初始化六个内置角色，并绑定修订二十的冻结权限矩阵哈希。
  INSERT INTO roles(role_id,tenant_id,code,name,description,status,created_at,updated_at,
    permission_revision,matrix_sha256,is_permission_immutable)
  SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,catalog.role_code,
    CASE catalog.role_code WHEN 'admin' THEN 'Admin' WHEN 'developer' THEN 'Developer'
      WHEN 'viewer' THEN 'Viewer' WHEN 'finance_admin' THEN 'FinanceAdmin'
      WHEN 'security_admin' THEN 'SecurityAdmin' WHEN 'ops_admin' THEN 'OpsAdmin' END,
    CASE catalog.role_code
      WHEN 'admin' THEN '租户最高管理员，拥有完整权限且矩阵不可修改。'
      WHEN 'developer' THEN '负责模型接入、Key、调试和请求排障，不读取正式账单。'
      WHEN 'viewer' THEN '仅查看基础概览和公开控制台信息。'
      WHEN 'finance_admin' THEN '负责用量、账本、预算、价格、成本异常、套餐额度和账单争议。'
      WHEN 'security_admin' THEN '负责审计、DLP、策略、错误归一和安全事件。'
      WHEN 'ops_admin' THEN '负责 Provider、环境、项目、Key、模型、路由、批量任务和在线实验运维。'
    END,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,
    CASE catalog.role_code
      WHEN 'admin' THEN 'e9fc70ecbb0aee117f913bad242eb62d23ef0374a905497ee391caae31951272'
      WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1'
      WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
      WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
      WHEN 'security_admin' THEN 'aeb4d6b081dce8ed36c6513b567aba4be395816c5a5ad2f1d939527a50ad14df'
      WHEN 'ops_admin' THEN 'c86c645b5c090c3ad8b1152ed783335ba82c788fc9690c4d803dd76f14eba8e7'
    END,CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;
  -- 为新租户实例化修订二十目录中的全部权限，租户标识由触发源提供。
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,20,'active',
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;
  -- 按内置角色模板写入租户内角色权限关系，不允许跨租户连接。
  INSERT INTO role_permissions(
    role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,role.role_id,
    permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM roles role JOIN rbac_role_permission_templates template
    ON template.role_code=role.code
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
    AND permission.code=template.permission_code
  WHERE role.tenant_id=NEW.tenant_id;
END;

-- 清理本连接可能残留的迁移终态门禁，避免旧临时对象影响本次校验。
DROP TABLE IF EXISTS temp.prd085_up_final_guard;
CREATE TEMP TABLE prd085_up_final_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd085_up_final_guard_abort
BEFORE INSERT ON prd085_up_final_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd085_up_final_invalid'); END;
-- 聚合校验目录、矩阵、历史投影、触发器、外键与数据库完整性后再提交。
INSERT INTO prd085_up_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=89
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=20)
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||description||
          char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code
      ))='5f77eaca2da3b37026d9891ee7fab0f4aa121fbd2ac775bcc2b21d740aacca14'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=234
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code
      ))='4602777ad3937aa0351a4b9949826756904a4ee3b485d65984cdbecdce127711'
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM permissions permission LEFT JOIN rbac_permission_templates template
        ON template.permission_code=permission.code
      WHERE template.permission_code IS NULL OR permission.catalog_revision!=20
        OR permission.category!=template.category OR permission.name!=template.name
        OR permission.description!=template.description
        OR permission.risk_level!=template.risk_level)
      AND NOT EXISTS(SELECT 1 FROM tenants tenant
        WHERE(SELECT COUNT(*) FROM permissions permission
          WHERE permission.tenant_id=tenant.tenant_id)!=89)
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(SELECT 1 FROM prd085_roles_to_expand selected
      JOIN roles role ON role.tenant_id=selected.tenant_id AND role.role_id=selected.role_id
      WHERE role.matrix_sha256!=CASE role.code
        WHEN 'admin' THEN 'e9fc70ecbb0aee117f913bad242eb62d23ef0374a905497ee391caae31951272'
        WHEN 'ops_admin' THEN 'c86c645b5c090c3ad8b1152ed783335ba82c788fc9690c4d803dd76f14eba8e7'
      END OR NOT EXISTS(
        SELECT 1 FROM role_permissions relation JOIN permissions permission
          ON permission.tenant_id=relation.tenant_id
         AND permission.permission_id=relation.permission_id
        WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
          AND permission.code='product_analytics.read'))
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id,
        operation_request_id,operation_trace_id,occurred_at
      FROM product_events WHERE capture_mode='backfill'
      EXCEPT
      SELECT product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id,
        operation_request_id,operation_trace_id,occurred_at
      FROM prd085_expected_backfill)
      AND NOT EXISTS(
      SELECT product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id,
        operation_request_id,operation_trace_id,occurred_at
      FROM prd085_expected_backfill
      EXCEPT
      SELECT product_event_id,tenant_id,event_type,actor_user_id,source_kind,source_id,
        operation_request_id,operation_trace_id,occurred_at
      FROM product_events WHERE capture_mode='backfill')
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM product_events WHERE capture_mode!='backfill')
  + (SELECT COUNT(*) FROM product_event_outbox)
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table'
      AND name IN('product_events','product_event_outbox'))=2 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name LIKE '%_prd085')=12 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd085_up_final_guard_abort;
DROP TABLE prd085_up_final_guard;
DROP TABLE prd085_expected_backfill;
DROP TABLE prd085_roles_to_expand;

COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
