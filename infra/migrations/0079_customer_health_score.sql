-- PRD-086 创建租户隔离的确定性客户健康快照、调度租约与变更事件 outbox。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 升级前守卫必须从干净临时表开始，并原子核对 0078 与 revision 20 基线。
DROP TABLE IF EXISTS temp.prd086_up_guard;
CREATE TEMP TABLE prd086_up_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd086_up_guard_abort
BEFORE INSERT ON prd086_up_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd086_up_guard_failed'); END;
-- 汇总版本、RBAC 哈希、遗留对象、外键与完整性问题，非零时原子阻断升级。
INSERT INTO prd086_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT version FROM schema_migrations
      WHERE status='applied' ORDER BY version DESC LIMIT 1)='0078' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=89
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
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name IN(
      'customer_health_scores','customer_health_score_outbox',
      'customer_health_scheduler_tenants','rbac_permission_templates_prd086_new',
      'permissions_prd086_new'))
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd086')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd086_up_guard_abort;
DROP TABLE prd086_up_guard;

CREATE TABLE customer_health_scheduler_tenants(
  tenant_id TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK(status IN('pending','leased')),
  next_evaluation_at TEXT NOT NULL CHECK(length(next_evaluation_at)=20
    AND next_evaluation_at GLOB '????-??-??T??:??:??Z'
    AND unixepoch(next_evaluation_at) IS NOT NULL),
  consecutive_failure_count INTEGER NOT NULL DEFAULT 0 CHECK(
    typeof(consecutive_failure_count)='integer'
    AND consecutive_failure_count BETWEEN 0 AND 2147483647),
  generation INTEGER NOT NULL DEFAULT 0 CHECK(
    typeof(generation)='integer' AND generation>=0),
  lease_owner_id TEXT CHECK(lease_owner_id IS NULL OR(
    length(lease_owner_id) BETWEEN 1 AND 128 AND instr(lease_owner_id,char(0))=0)),
  lease_token TEXT CHECK(lease_token IS NULL OR(
    length(lease_token)=64 AND lease_token NOT GLOB '*[^0-9a-f]*')),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR(
    length(lease_expires_at)=20 AND lease_expires_at GLOB '????-??-??T??:??:??Z'
    AND unixepoch(lease_expires_at) IS NOT NULL)),
  last_completed_window_end TEXT CHECK(last_completed_window_end IS NULL OR(
    length(last_completed_window_end)=20
    AND last_completed_window_end GLOB '????-??-??T??:00:00Z'
    AND unixepoch(last_completed_window_end) IS NOT NULL)),
  last_error_code TEXT CHECK(last_error_code IS NULL
    OR last_error_code='customer_health.unavailable'),
  created_at TEXT NOT NULL CHECK(length(created_at)=20
    AND created_at GLOB '????-??-??T??:??:??Z' AND unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(length(updated_at)=20
    AND updated_at GLOB '????-??-??T??:??:??Z' AND unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  CHECK(unixepoch(updated_at)>=unixepoch(created_at)),
  CHECK((status='pending' AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL)
    OR(status='leased' AND lease_owner_id IS NOT NULL AND lease_token IS NOT NULL
      AND lease_expires_at IS NOT NULL AND last_error_code IS NULL))
);
CREATE INDEX idx_customer_health_scheduler_claim_prd086
  ON customer_health_scheduler_tenants(status,next_evaluation_at,tenant_id);

CREATE TABLE customer_health_scores(
  customer_health_score_sequence INTEGER PRIMARY KEY AUTOINCREMENT CHECK(
    typeof(customer_health_score_sequence)='integer' AND customer_health_score_sequence>0),
  customer_health_score_id TEXT NOT NULL UNIQUE CHECK(
    length(customer_health_score_id)=36 AND substr(customer_health_score_id,1,4)='chs_'
    AND substr(customer_health_score_id,5) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  score_version TEXT NOT NULL CHECK(score_version='customer-health-v1'),
  window_start TEXT NOT NULL CHECK(length(window_start)=20
    AND window_start GLOB '????-??-??T??:00:00Z' AND unixepoch(window_start) IS NOT NULL),
  window_end TEXT NOT NULL CHECK(length(window_end)=20
    AND window_end GLOB '????-??-??T??:00:00Z' AND unixepoch(window_end) IS NOT NULL),
  comparison_start TEXT NOT NULL CHECK(length(comparison_start)=20
    AND comparison_start GLOB '????-??-??T??:00:00Z'
    AND unixepoch(comparison_start) IS NOT NULL),
  comparison_end TEXT NOT NULL CHECK(length(comparison_end)=20
    AND comparison_end GLOB '????-??-??T??:00:00Z'
    AND unixepoch(comparison_end) IS NOT NULL),
  source_fingerprint TEXT NOT NULL CHECK(length(source_fingerprint)=64
    AND source_fingerprint NOT GLOB '*[^0-9a-f]*'),
  health_projection_sha256 TEXT NOT NULL CHECK(length(health_projection_sha256)=64
    AND health_projection_sha256 NOT GLOB '*[^0-9a-f]*'),
  current_request_count INTEGER NOT NULL CHECK(typeof(current_request_count)='integer'
    AND current_request_count BETWEEN 0 AND 922337203685477),
  previous_request_count INTEGER NOT NULL CHECK(typeof(previous_request_count)='integer'
    AND previous_request_count BETWEEN 0 AND 922337203685477),
  successful_request_count INTEGER NOT NULL CHECK(typeof(successful_request_count)='integer'
    AND successful_request_count BETWEEN 0 AND 922337203685477),
  failed_request_count INTEGER NOT NULL CHECK(typeof(failed_request_count)='integer'
    AND failed_request_count BETWEEN 0 AND 922337203685477),
  unresolved_cost_anomaly_count INTEGER NOT NULL CHECK(
    typeof(unresolved_cost_anomaly_count)='integer'
    AND unresolved_cost_anomaly_count BETWEEN 0 AND 922337203685477),
  critical_cost_anomaly_count INTEGER NOT NULL CHECK(
    typeof(critical_cost_anomaly_count)='integer'
    AND critical_cost_anomaly_count BETWEEN 0 AND 922337203685477),
  active_budget_rule_count INTEGER NOT NULL CHECK(typeof(active_budget_rule_count)='integer'
    AND active_budget_rule_count BETWEEN 0 AND 922337203685477),
  eligible_user_count INTEGER NOT NULL CHECK(typeof(eligible_user_count)='integer'
    AND eligible_user_count BETWEEN 0 AND 922337203685477),
  recently_active_user_count INTEGER NOT NULL CHECK(typeof(recently_active_user_count)='integer'
    AND recently_active_user_count BETWEEN 0 AND 922337203685477),
  ticket_source_status TEXT NOT NULL CHECK(ticket_source_status='unavailable'),
  activation_stage_count INTEGER NOT NULL CHECK(typeof(activation_stage_count)='integer'
    AND activation_stage_count BETWEEN 0 AND 7),
  usage_trend_score_bps INTEGER NOT NULL CHECK(typeof(usage_trend_score_bps)='integer'
    AND usage_trend_score_bps BETWEEN 0 AND 10000),
  usage_trend_contribution_bps INTEGER NOT NULL CHECK(
    typeof(usage_trend_contribution_bps)='integer'
    AND usage_trend_contribution_bps BETWEEN 0 AND 10000),
  reliability_score_bps INTEGER NOT NULL CHECK(typeof(reliability_score_bps)='integer'
    AND reliability_score_bps BETWEEN 0 AND 10000),
  reliability_contribution_bps INTEGER NOT NULL CHECK(
    typeof(reliability_contribution_bps)='integer'
    AND reliability_contribution_bps BETWEEN 0 AND 10000),
  cost_anomaly_health_score_bps INTEGER NOT NULL CHECK(
    typeof(cost_anomaly_health_score_bps)='integer'
    AND cost_anomaly_health_score_bps BETWEEN 0 AND 10000),
  cost_anomaly_health_contribution_bps INTEGER NOT NULL CHECK(
    typeof(cost_anomaly_health_contribution_bps)='integer'
    AND cost_anomaly_health_contribution_bps BETWEEN 0 AND 10000),
  budget_readiness_score_bps INTEGER NOT NULL CHECK(
    typeof(budget_readiness_score_bps)='integer'
    AND budget_readiness_score_bps BETWEEN 0 AND 10000),
  budget_readiness_contribution_bps INTEGER NOT NULL CHECK(
    typeof(budget_readiness_contribution_bps)='integer'
    AND budget_readiness_contribution_bps BETWEEN 0 AND 10000),
  team_activity_score_bps INTEGER NOT NULL CHECK(typeof(team_activity_score_bps)='integer'
    AND team_activity_score_bps BETWEEN 0 AND 10000),
  team_activity_contribution_bps INTEGER NOT NULL CHECK(
    typeof(team_activity_contribution_bps)='integer'
    AND team_activity_contribution_bps BETWEEN 0 AND 10000),
  ticket_health_score_bps INTEGER CHECK(ticket_health_score_bps IS NULL),
  ticket_health_contribution_bps INTEGER NOT NULL CHECK(ticket_health_contribution_bps=0),
  effective_weight_bps INTEGER NOT NULL CHECK(effective_weight_bps=9000),
  score_bps INTEGER NOT NULL CHECK(typeof(score_bps)='integer' AND score_bps BETWEEN 0 AND 10000),
  risk_level TEXT NOT NULL CHECK(risk_level IN('healthy','watch','at_risk','critical')),
  usage_declining INTEGER NOT NULL CHECK(usage_declining IN(0,1)),
  high_error_rate INTEGER NOT NULL CHECK(high_error_rate IN(0,1)),
  cost_anomaly_open INTEGER NOT NULL CHECK(cost_anomaly_open IN(0,1)),
  budget_missing INTEGER NOT NULL CHECK(budget_missing IN(0,1)),
  team_inactive INTEGER NOT NULL CHECK(team_inactive IN(0,1)),
  operation_request_id TEXT NOT NULL CHECK(length(operation_request_id)=40
    AND substr(operation_request_id,1,8)='req_chs_'
    AND substr(operation_request_id,9) NOT GLOB '*[^0-9a-f]*'),
  operation_trace_id TEXT NOT NULL CHECK(length(operation_trace_id)=40
    AND substr(operation_trace_id,1,8)='trc_chs_'
    AND substr(operation_trace_id,9) NOT GLOB '*[^0-9a-f]*'),
  evaluated_at TEXT NOT NULL CHECK(length(evaluated_at)=20
    AND evaluated_at GLOB '????-??-??T??:??:??Z' AND unixepoch(evaluated_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(length(created_at)=20
    AND created_at GLOB '????-??-??T??:??:??Z' AND unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(length(updated_at)=20
    AND updated_at GLOB '????-??-??T??:??:??Z' AND unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  UNIQUE(tenant_id,customer_health_score_id),
  UNIQUE(tenant_id,window_end,source_fingerprint),
  CHECK(unixepoch(window_end)-unixepoch(window_start)=604800),
  CHECK(comparison_end=window_start
    AND unixepoch(comparison_end)-unixepoch(comparison_start)=604800),
  CHECK(successful_request_count+failed_request_count=current_request_count),
  CHECK(critical_cost_anomaly_count<=unresolved_cost_anomaly_count),
  CHECK(recently_active_user_count<=eligible_user_count),
  CHECK(evaluated_at=created_at AND updated_at=created_at
    AND unixepoch(evaluated_at)>=unixepoch(window_end)
    AND unixepoch(evaluated_at)<unixepoch(window_end)+3600)
);
CREATE INDEX idx_customer_health_scores_latest_prd086
  ON customer_health_scores(tenant_id,customer_health_score_sequence DESC);

CREATE TABLE customer_health_score_outbox(
  customer_health_score_outbox_id TEXT PRIMARY KEY CHECK(
    length(customer_health_score_outbox_id)=37
    AND substr(customer_health_score_outbox_id,1,5)='chso_'
    AND substr(customer_health_score_outbox_id,6) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  customer_health_score_id TEXT NOT NULL,
  webhook_event_id TEXT NOT NULL CHECK(length(webhook_event_id)=40
    AND substr(webhook_event_id,1,8)='evt_chs_'
    AND substr(webhook_event_id,9) NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('pending','leased','sent','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(
    typeof(attempt_count)='integer' AND attempt_count BETWEEN 0 AND 10),
  generation INTEGER NOT NULL DEFAULT 0 CHECK(typeof(generation)='integer' AND generation>=0),
  lease_owner_id TEXT CHECK(lease_owner_id IS NULL OR(
    length(lease_owner_id) BETWEEN 1 AND 128 AND instr(lease_owner_id,char(0))=0)),
  lease_token TEXT CHECK(lease_token IS NULL OR(
    length(lease_token)=64 AND lease_token NOT GLOB '*[^0-9a-f]*')),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR(
    length(lease_expires_at)=20 AND lease_expires_at GLOB '????-??-??T??:??:??Z'
    AND unixepoch(lease_expires_at) IS NOT NULL)),
  next_attempt_at TEXT CHECK(next_attempt_at IS NULL OR(
    length(next_attempt_at)=20 AND next_attempt_at GLOB '????-??-??T??:??:??Z'
    AND unixepoch(next_attempt_at) IS NOT NULL)),
  subscription_snapshot_json TEXT CHECK(subscription_snapshot_json IS NULL OR(
    json_valid(subscription_snapshot_json) AND json_type(subscription_snapshot_json)='array'
    AND length(CAST(subscription_snapshot_json AS BLOB))<=65536)),
  snapshot_frozen_at TEXT CHECK(snapshot_frozen_at IS NULL OR(
    length(snapshot_frozen_at)=20 AND snapshot_frozen_at GLOB '????-??-??T??:??:??Z'
    AND unixepoch(snapshot_frozen_at) IS NOT NULL)),
  last_error_code TEXT CHECK(last_error_code IS NULL OR last_error_code IN(
    'webhook.payload_invalid','webhook.delivery_unavailable','customer_health.unavailable')),
  delivered_at TEXT CHECK(delivered_at IS NULL OR(
    length(delivered_at)=20 AND delivered_at GLOB '????-??-??T??:??:??Z'
    AND unixepoch(delivered_at) IS NOT NULL)),
  created_at TEXT NOT NULL CHECK(length(created_at)=20
    AND created_at GLOB '????-??-??T??:??:??Z' AND unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(length(updated_at)=20
    AND updated_at GLOB '????-??-??T??:??:??Z' AND unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id,customer_health_score_id)
    REFERENCES customer_health_scores(tenant_id,customer_health_score_id),
  UNIQUE(tenant_id,customer_health_score_id),
  UNIQUE(tenant_id,webhook_event_id),
  CHECK(unixepoch(updated_at)>=unixepoch(created_at)),
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
    OR(status='sent' AND attempt_count BETWEEN 1 AND 10
      AND lease_owner_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL
      AND next_attempt_at IS NULL AND last_error_code IS NULL AND delivered_at IS NOT NULL)
    OR(status='failed' AND attempt_count=10
      AND lease_owner_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL
      AND next_attempt_at IS NULL AND last_error_code IS NOT NULL AND delivered_at IS NULL))
);
CREATE INDEX idx_customer_health_outbox_claim_prd086
  ON customer_health_score_outbox(status,next_attempt_at,tenant_id,created_at,
    customer_health_score_outbox_id);

CREATE TRIGGER trg_customer_health_scheduler_owner_insert_prd086
BEFORE INSERT ON customer_health_scheduler_tenants FOR EACH ROW
WHEN NEW.status!='pending' OR NEW.next_evaluation_at!=NEW.created_at
  OR NEW.consecutive_failure_count!=0 OR NEW.generation!=0
  OR NEW.lease_owner_id IS NOT NULL OR NEW.lease_token IS NOT NULL
  OR NEW.lease_expires_at IS NOT NULL OR NEW.last_completed_window_end IS NOT NULL
  OR NEW.last_error_code IS NOT NULL OR NEW.updated_at!=NEW.created_at
  OR NOT EXISTS(SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR EXISTS(SELECT 1 FROM customer_health_scheduler_tenants current
    WHERE current.tenant_id=NEW.tenant_id OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'customer_health_scheduler_owner_mismatch'); END;

CREATE TRIGGER trg_customer_health_scheduler_transition_prd086
BEFORE UPDATE ON customer_health_scheduler_tenants FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.tenant_id!=OLD.tenant_id OR NEW.created_at!=OLD.created_at
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR NOT(
    (OLD.status='pending' AND NEW.status='leased'
      AND NEW.generation=OLD.generation+1
      AND NEW.consecutive_failure_count=OLD.consecutive_failure_count
      AND NEW.next_evaluation_at=OLD.next_evaluation_at
      AND NEW.last_completed_window_end IS OLD.last_completed_window_end
      AND NEW.last_error_code IS NULL
      AND unixepoch(NEW.lease_expires_at)>unixepoch(NEW.updated_at))
    OR(OLD.status='leased' AND NEW.status='leased'
      AND unixepoch(OLD.lease_expires_at)<=unixepoch(NEW.updated_at)
      AND NEW.generation=OLD.generation+1
      AND NEW.consecutive_failure_count=OLD.consecutive_failure_count
      AND NEW.next_evaluation_at=OLD.next_evaluation_at
      AND NEW.last_completed_window_end IS OLD.last_completed_window_end
      AND NEW.last_error_code IS NULL
      AND NEW.lease_token!=OLD.lease_token
      AND unixepoch(NEW.lease_expires_at)>unixepoch(NEW.updated_at))
    OR(OLD.status='leased' AND NEW.status='leased'
      AND unixepoch(OLD.lease_expires_at)>unixepoch(NEW.updated_at)
      AND NEW.generation=OLD.generation+1
      AND NEW.consecutive_failure_count=OLD.consecutive_failure_count
      AND NEW.next_evaluation_at=OLD.next_evaluation_at
      AND NEW.last_completed_window_end IS OLD.last_completed_window_end
      AND NEW.last_error_code IS NULL
      AND NEW.lease_owner_id=OLD.lease_owner_id
      AND NEW.lease_token!=OLD.lease_token
      AND unixepoch(NEW.lease_expires_at)>unixepoch(OLD.lease_expires_at))
    OR(OLD.status='leased' AND NEW.status='pending'
      AND NEW.generation=OLD.generation+1 AND NEW.consecutive_failure_count=0
      AND NEW.last_error_code IS NULL
      AND unixepoch(OLD.lease_expires_at)>unixepoch(NEW.updated_at)
      AND NEW.last_completed_window_end IS NOT NULL
      AND(OLD.last_completed_window_end IS NULL
        OR unixepoch(NEW.last_completed_window_end)>=unixepoch(OLD.last_completed_window_end))
      AND unixepoch(NEW.next_evaluation_at)>unixepoch(NEW.updated_at))
    OR(OLD.status='leased' AND NEW.status='pending'
      AND NEW.generation=OLD.generation+1
      AND NEW.consecutive_failure_count=OLD.consecutive_failure_count+1
      AND NEW.last_completed_window_end IS OLD.last_completed_window_end
      AND NEW.last_error_code='customer_health.unavailable'
      AND unixepoch(OLD.lease_expires_at)>unixepoch(NEW.updated_at)
      AND unixepoch(NEW.next_evaluation_at)>unixepoch(NEW.updated_at)))
BEGIN SELECT RAISE(ABORT,'customer_health_scheduler_transition_invalid'); END;

CREATE TRIGGER trg_customer_health_scheduler_delete_prd086
BEFORE DELETE ON customer_health_scheduler_tenants FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'customer_health_scheduler_immutable'); END;

CREATE TRIGGER trg_customer_health_scores_owner_insert_prd086
BEFORE INSERT ON customer_health_scores FOR EACH ROW
WHEN NEW.customer_health_score_id!='chs_'||substr(metahub_sha256(json_array(
      'customer-health-score-v1',NEW.tenant_id,NEW.score_version,
      NEW.window_end,NEW.source_fingerprint)),1,32)
  OR NEW.source_fingerprint!=metahub_sha256(json_array(
      'customer-health-source-v1',NEW.tenant_id,NEW.score_version,
      NEW.window_start,NEW.window_end,NEW.comparison_start,NEW.comparison_end,
      NEW.current_request_count,NEW.previous_request_count,
      NEW.successful_request_count,NEW.failed_request_count,
      NEW.unresolved_cost_anomaly_count,NEW.critical_cost_anomaly_count,
      NEW.active_budget_rule_count,NEW.eligible_user_count,
      NEW.recently_active_user_count,NEW.ticket_source_status,
      NEW.activation_stage_count))
  OR NEW.operation_request_id!='req_chs_'||substr(metahub_sha256(json_array(
      'customer-health-operation-v1',NEW.tenant_id,NEW.window_end,
      NEW.source_fingerprint)),1,32)
  OR NEW.operation_trace_id!='trc_chs_'||substr(metahub_sha256(json_array(
      'customer-health-operation-v1',NEW.tenant_id,NEW.window_end,
      NEW.source_fingerprint)),1,32)
  OR NEW.usage_trend_score_bps!=CASE
      WHEN NEW.current_request_count=0 THEN 0
      WHEN NEW.previous_request_count=0 THEN 10000
      WHEN (NEW.current_request_count*10000)/NEW.previous_request_count>10000 THEN 10000
      ELSE (NEW.current_request_count*10000)/NEW.previous_request_count END
  OR NEW.reliability_score_bps!=CASE WHEN NEW.current_request_count=0 THEN 5000
      ELSE (NEW.successful_request_count*10000)/NEW.current_request_count
        +CASE WHEN ((NEW.successful_request_count*10000)%NEW.current_request_count)*2
          >=NEW.current_request_count THEN 1 ELSE 0 END END
  OR NEW.cost_anomaly_health_score_bps!=CASE
      WHEN NEW.critical_cost_anomaly_count>0 THEN 0
      WHEN NEW.unresolved_cost_anomaly_count>0 THEN 4000 ELSE 10000 END
  OR NEW.budget_readiness_score_bps!=CASE
      WHEN NEW.active_budget_rule_count>0 THEN 10000 ELSE 3000 END
  OR NEW.team_activity_score_bps!=CASE WHEN NEW.eligible_user_count=0 THEN 0
      ELSE (NEW.recently_active_user_count*10000)/NEW.eligible_user_count
        +CASE WHEN ((NEW.recently_active_user_count*10000)%NEW.eligible_user_count)*2
          >=NEW.eligible_user_count THEN 1 ELSE 0 END END
  OR NEW.score_bps!=(
      NEW.usage_trend_score_bps*2500+NEW.reliability_score_bps*2500+
      NEW.cost_anomaly_health_score_bps*1500+NEW.budget_readiness_score_bps*1000+
      NEW.team_activity_score_bps*1500+4500)/9000
  OR EXISTS(
    WITH components(ordinal,score,weight,contribution) AS(VALUES
      (1,NEW.usage_trend_score_bps,2500,NEW.usage_trend_contribution_bps),
      (2,NEW.reliability_score_bps,2500,NEW.reliability_contribution_bps),
      (3,NEW.cost_anomaly_health_score_bps,1500,NEW.cost_anomaly_health_contribution_bps),
      (4,NEW.budget_readiness_score_bps,1000,NEW.budget_readiness_contribution_bps),
      (5,NEW.team_activity_score_bps,1500,NEW.team_activity_contribution_bps)),
    calculated AS(SELECT ordinal,contribution,(score*weight)/9000 AS base,
      (score*weight)%9000 AS remainder FROM components),
    totals AS(SELECT NEW.score_bps-SUM(base) AS extras FROM calculated)
    SELECT 1 FROM calculated candidate,totals
    WHERE candidate.contribution!=candidate.base+CASE WHEN(
      SELECT COUNT(*) FROM calculated ahead
      WHERE ahead.remainder>candidate.remainder
        OR(ahead.remainder=candidate.remainder AND ahead.ordinal<candidate.ordinal)
    )<totals.extras THEN 1 ELSE 0 END)
  OR NEW.usage_declining!=CASE WHEN NEW.current_request_count=0
      OR(NEW.previous_request_count>0
        AND (NEW.current_request_count*10000)/NEW.previous_request_count<5000)
      THEN 1 ELSE 0 END
  OR NEW.high_error_rate!=CASE WHEN NEW.current_request_count>=5
      AND 10000-NEW.reliability_score_bps>=500 THEN 1 ELSE 0 END
  OR NEW.cost_anomaly_open!=CASE WHEN NEW.unresolved_cost_anomaly_count>0 THEN 1 ELSE 0 END
  OR NEW.budget_missing!=CASE WHEN NEW.active_budget_rule_count=0 THEN 1 ELSE 0 END
  OR NEW.team_inactive!=CASE WHEN NEW.recently_active_user_count=0
      OR(NEW.eligible_user_count>=2 AND NEW.team_activity_score_bps<5000)
      THEN 1 ELSE 0 END
  OR NEW.risk_level!=CASE
      WHEN NEW.score_bps<3000 OR NEW.critical_cost_anomaly_count>0 THEN 'critical'
      WHEN NEW.score_bps<6000 OR NEW.current_request_count=0
        OR(NEW.current_request_count>=5 AND 10000-NEW.reliability_score_bps>=1000)
        THEN 'at_risk'
      WHEN NEW.score_bps<8000 OR NEW.unresolved_cost_anomaly_count>0
        OR(NEW.current_request_count>=5 AND 10000-NEW.reliability_score_bps>=500)
        THEN 'watch' ELSE 'healthy' END
  OR NEW.health_projection_sha256!=metahub_sha256(json_array(
      'customer-health-projection-v1',NEW.score_version,
      NEW.usage_trend_score_bps,NEW.usage_trend_contribution_bps,
      CASE WHEN NEW.current_request_count=0 AND NEW.previous_request_count=0
        THEN 'no_samples' ELSE 'available' END,
      CASE WHEN NEW.usage_trend_score_bps>=8000 THEN 'healthy'
        WHEN NEW.usage_trend_score_bps>=6000 THEN 'watch'
        WHEN NEW.usage_trend_score_bps>=3000 THEN 'at_risk' ELSE 'critical' END,
      NEW.reliability_score_bps,NEW.reliability_contribution_bps,
      CASE WHEN NEW.current_request_count=0 THEN 'no_samples' ELSE 'available' END,
      CASE WHEN NEW.current_request_count=0 THEN 'unknown'
        WHEN NEW.reliability_score_bps>=8000 THEN 'healthy'
        WHEN NEW.reliability_score_bps>=6000 THEN 'watch'
        WHEN NEW.reliability_score_bps>=3000 THEN 'at_risk' ELSE 'critical' END,
      NEW.cost_anomaly_health_score_bps,NEW.cost_anomaly_health_contribution_bps,'available',
      CASE WHEN NEW.cost_anomaly_health_score_bps>=8000 THEN 'healthy'
        WHEN NEW.cost_anomaly_health_score_bps>=6000 THEN 'watch'
        WHEN NEW.cost_anomaly_health_score_bps>=3000 THEN 'at_risk' ELSE 'critical' END,
      NEW.budget_readiness_score_bps,NEW.budget_readiness_contribution_bps,'available',
      CASE WHEN NEW.budget_readiness_score_bps>=8000 THEN 'healthy'
        WHEN NEW.budget_readiness_score_bps>=6000 THEN 'watch'
        WHEN NEW.budget_readiness_score_bps>=3000 THEN 'at_risk' ELSE 'critical' END,
      NEW.team_activity_score_bps,NEW.team_activity_contribution_bps,
      CASE WHEN NEW.eligible_user_count=0 THEN 'no_samples' ELSE 'available' END,
      CASE WHEN NEW.team_activity_score_bps>=8000 THEN 'healthy'
        WHEN NEW.team_activity_score_bps>=6000 THEN 'watch'
        WHEN NEW.team_activity_score_bps>=3000 THEN 'at_risk' ELSE 'critical' END,
      NULL,0,'unavailable','unknown',NEW.effective_weight_bps,NEW.score_bps,NEW.risk_level,
      NEW.usage_declining,NEW.high_error_rate,NEW.cost_anomaly_open,
      NEW.budget_missing,NEW.team_inactive,'ticket_health'))
  OR NOT EXISTS(SELECT 1 FROM customer_health_scheduler_tenants scheduler
    WHERE scheduler.tenant_id=NEW.tenant_id AND scheduler.status='leased'
      AND unixepoch(scheduler.lease_expires_at)>unixepoch(NEW.evaluated_at))
  OR EXISTS(SELECT 1 FROM customer_health_scores current
    WHERE current.customer_health_score_id=NEW.customer_health_score_id
      OR(current.tenant_id=NEW.tenant_id AND current.window_end=NEW.window_end
        AND current.source_fingerprint=NEW.source_fingerprint)
      OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'customer_health_score_owner_mismatch'); END;

CREATE TRIGGER trg_customer_health_scores_update_prd086
BEFORE UPDATE ON customer_health_scores FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'customer_health_score_immutable'); END;
CREATE TRIGGER trg_customer_health_scores_delete_prd086
BEFORE DELETE ON customer_health_scores FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'customer_health_score_immutable'); END;

CREATE TRIGGER trg_customer_health_outbox_owner_insert_prd086
BEFORE INSERT ON customer_health_score_outbox FOR EACH ROW
WHEN NEW.customer_health_score_outbox_id!='chso_'||
      substr(metahub_sha256(NEW.customer_health_score_id),1,32)
  OR NEW.webhook_event_id!='evt_chs_'||
      substr(metahub_sha256(NEW.customer_health_score_id),1,32)
  OR NEW.status!='pending' OR NEW.attempt_count!=0 OR NEW.generation!=0
  OR NEW.lease_owner_id IS NOT NULL OR NEW.lease_token IS NOT NULL
  OR NEW.lease_expires_at IS NOT NULL OR NEW.last_error_code IS NOT NULL
  OR NEW.delivered_at IS NOT NULL OR NEW.subscription_snapshot_json IS NOT NULL
  OR NEW.snapshot_frozen_at IS NOT NULL OR NEW.next_attempt_at!=NEW.created_at
  OR NEW.updated_at!=NEW.created_at
  OR NOT EXISTS(SELECT 1 FROM customer_health_scores score
    WHERE score.tenant_id=NEW.tenant_id
      AND score.customer_health_score_id=NEW.customer_health_score_id
      AND score.created_at=NEW.created_at)
  OR NOT EXISTS(SELECT 1 FROM customer_health_scores score
    WHERE score.tenant_id=NEW.tenant_id
      AND score.customer_health_score_id=NEW.customer_health_score_id
      AND(NOT EXISTS(SELECT 1 FROM customer_health_scores previous
          WHERE previous.tenant_id=score.tenant_id
            AND previous.customer_health_score_sequence<score.customer_health_score_sequence)
        OR score.health_projection_sha256!=(
          SELECT previous.health_projection_sha256 FROM customer_health_scores previous
          WHERE previous.tenant_id=score.tenant_id
            AND previous.customer_health_score_sequence<score.customer_health_score_sequence
          ORDER BY previous.customer_health_score_sequence DESC LIMIT 1)))
  OR EXISTS(SELECT 1 FROM customer_health_score_outbox current
    WHERE current.customer_health_score_outbox_id=NEW.customer_health_score_outbox_id
      OR(current.tenant_id=NEW.tenant_id
        AND current.customer_health_score_id=NEW.customer_health_score_id)
      OR(current.tenant_id=NEW.tenant_id AND current.webhook_event_id=NEW.webhook_event_id)
      OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'customer_health_outbox_owner_mismatch'); END;

CREATE TRIGGER trg_customer_health_outbox_transition_prd086
BEFORE UPDATE ON customer_health_score_outbox FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.customer_health_score_outbox_id!=OLD.customer_health_score_outbox_id
  OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.customer_health_score_id!=OLD.customer_health_score_id
  OR NEW.webhook_event_id!=OLD.webhook_event_id
  OR NEW.created_at!=OLD.created_at OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR NOT((NEW.subscription_snapshot_json IS OLD.subscription_snapshot_json
      AND NEW.snapshot_frozen_at IS OLD.snapshot_frozen_at)
    OR(OLD.status='pending' AND NEW.status='leased'
      AND OLD.subscription_snapshot_json IS NULL AND OLD.snapshot_frozen_at IS NULL
      AND NEW.subscription_snapshot_json IS NOT NULL
      AND NEW.snapshot_frozen_at IS NOT NULL))
  OR NOT(
    (OLD.status='pending' AND NEW.status='leased'
      AND NEW.attempt_count=OLD.attempt_count+1 AND NEW.generation=OLD.generation+1
      AND unixepoch(NEW.lease_expires_at)>unixepoch(NEW.updated_at))
    OR(OLD.status='leased' AND NEW.status='leased'
      AND NEW.attempt_count=OLD.attempt_count AND NEW.generation=OLD.generation+1
      AND unixepoch(OLD.lease_expires_at)<=unixepoch(NEW.updated_at)
      AND NEW.lease_token!=OLD.lease_token
      AND unixepoch(NEW.lease_expires_at)>unixepoch(NEW.updated_at))
    OR(OLD.status='leased' AND NEW.status='leased'
      AND NEW.attempt_count=OLD.attempt_count AND NEW.generation=OLD.generation+1
      AND unixepoch(OLD.lease_expires_at)>unixepoch(NEW.updated_at)
      AND NEW.lease_owner_id=OLD.lease_owner_id
      AND NEW.lease_token!=OLD.lease_token
      AND unixepoch(NEW.lease_expires_at)>unixepoch(OLD.lease_expires_at))
    OR(OLD.status='leased' AND NEW.status='pending'
      AND NEW.attempt_count=OLD.attempt_count AND NEW.generation=OLD.generation+1
      AND unixepoch(OLD.lease_expires_at)>unixepoch(NEW.updated_at))
    OR(OLD.status='leased' AND NEW.status IN('sent','failed')
      AND NEW.attempt_count=OLD.attempt_count AND NEW.generation=OLD.generation+1
      AND unixepoch(OLD.lease_expires_at)>unixepoch(NEW.updated_at)))
BEGIN SELECT RAISE(ABORT,'customer_health_outbox_transition_invalid'); END;

CREATE TRIGGER trg_customer_health_outbox_delete_prd086
BEFORE DELETE ON customer_health_score_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'customer_health_outbox_immutable'); END;

-- 为全部既有租户创建立即到期的干净 Scheduler 游标，所有状态均显式携带 tenant_id。
INSERT INTO customer_health_scheduler_tenants(
  tenant_id,status,next_evaluation_at,consecutive_failure_count,generation,
  lease_owner_id,lease_token,lease_expires_at,last_completed_window_end,last_error_code,
  created_at,updated_at)
SELECT tenant_id,'pending',strftime('%Y-%m-%dT%H:%M:%SZ','now'),0,0,
  NULL,NULL,NULL,NULL,NULL,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants;

CREATE TRIGGER trg_tenants_customer_health_scheduler_insert_prd086
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 新租户创建时同步补齐客户健康 Scheduler 游标，避免租户缺失后台评估能力。
  INSERT INTO customer_health_scheduler_tenants(
    tenant_id,status,next_evaluation_at,consecutive_failure_count,generation,
    lease_owner_id,lease_token,lease_expires_at,last_completed_window_end,last_error_code,
    created_at,updated_at)
  VALUES(NEW.tenant_id,'pending',strftime('%Y-%m-%dT%H:%M:%SZ','now'),0,0,
    NULL,NULL,NULL,NULL,NULL,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now'));
END;

-- Expand only exact revision-20 default admin and ops matrices.
CREATE TEMP TABLE prd086_roles_to_expand AS
SELECT role.role_id,role.tenant_id,role.code
FROM roles role
WHERE role.code IN('admin','ops_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN 'e9fc70ecbb0aee117f913bad242eb62d23ef0374a905497ee391caae31951272'
    WHEN 'ops_admin' THEN 'c86c645b5c090c3ad8b1152ed783335ba82c788fc9690c4d803dd76f14eba8e7'
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

CREATE TABLE rbac_permission_templates_prd086_new(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=21)
);
-- 将既有权限模板原序复制到 revision 21 新表，保留 rowid 与规范顺序。
INSERT INTO rbac_permission_templates_prd086_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision)
SELECT rowid,permission_code,category,name,description,risk_level,21
FROM rbac_permission_templates;
-- 在 revision 21 目录中新增客户健康读取权限，并冻结风险级别与中文说明。
INSERT INTO rbac_permission_templates_prd086_new(
  permission_code,category,name,description,risk_level,catalog_revision)
VALUES('customer_health.read','analytics','读取客户健康评分',
  '读取当前租户确定性客户健康评分、聚合信号与可解释建议。','read',21);
DROP TABLE rbac_permission_templates;
-- 原子切换新的 revision 21 权限模板表，后续触发器继续保护不可变目录。
ALTER TABLE rbac_permission_templates_prd086_new RENAME TO rbac_permission_templates;

-- 默认只向 admin 与 ops_admin 模板授予客户健康读取权限。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
VALUES('admin','customer_health.read',1),('ops_admin','customer_health.read',1);

CREATE TABLE permissions_prd086_new(
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=21),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),
  UNIQUE(tenant_id,permission_id)
);
-- 将各租户既有权限实例原序复制到 revision 21 表并保留稳定标识。
INSERT INTO permissions_prd086_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  21,status,created_at,updated_at FROM permissions;
-- 为每个既有租户创建 customer_health.read 权限实例，所有权由 tenant_id 约束。
INSERT INTO permissions_prd086_new(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at)
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
  template.category,template.name,template.description,template.risk_level,21,'active',
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant JOIN rbac_permission_templates template
  ON template.permission_code='customer_health.read';
DROP TABLE permissions;
-- 原子切换租户权限实例表，并在切换后恢复既有索引与保护触发器。
ALTER TABLE permissions_prd086_new RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);

-- 仅为升级前矩阵精确匹配的 admin 与 ops_admin 增加新权限关系。
INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
  permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd086_roles_to_expand role JOIN permissions permission
  ON permission.tenant_id=role.tenant_id AND permission.code='customer_health.read';
-- 对已扩展角色递增权限修订并写入 revision 21 精确矩阵摘要。
UPDATE roles SET permission_revision=permission_revision+1,
  matrix_sha256=CASE code
    WHEN 'admin' THEN '49c3be24e085e091cc3c3bf3b8411ed6a783486050d484edec112ed63bee8de5'
    WHEN 'ops_admin' THEN '1f8c90dfb94af73874a8ef3e5470991d36bd7533940adbdbfcbb0e9c6a878783'
  END
WHERE role_id IN(SELECT role_id FROM prd086_roles_to_expand);

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
    WHEN 'admin' THEN '49c3be24e085e091cc3c3bf3b8411ed6a783486050d484edec112ed63bee8de5'
    WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1'
    WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
    WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
    WHEN 'security_admin' THEN 'aeb4d6b081dce8ed36c6513b567aba4be395816c5a5ad2f1d939527a50ad14df'
    WHEN 'ops_admin' THEN '1f8c90dfb94af73874a8ef3e5470991d36bd7533940adbdbfcbb0e9c6a878783'
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
  OR NEW.created_at!=OLD.created_at OR NEW.is_permission_immutable!=OLD.is_permission_immutable
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
  -- 新租户先按 revision 21 角色模板初始化六个固定角色及其矩阵摘要。
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
      WHEN 'admin' THEN '49c3be24e085e091cc3c3bf3b8411ed6a783486050d484edec112ed63bee8de5'
      WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1'
      WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
      WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
      WHEN 'security_admin' THEN 'aeb4d6b081dce8ed36c6513b567aba4be395816c5a5ad2f1d939527a50ad14df'
      WHEN 'ops_admin' THEN '1f8c90dfb94af73874a8ef3e5470991d36bd7533940adbdbfcbb0e9c6a878783'
    END,CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;
  -- 新租户按 revision 21 目录初始化全部权限实例，tenant_id 与模板代码共同限定归属。
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,21,'active',
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;
  -- 新租户按默认角色模板建立权限关系，所有连接均同时约束 tenant_id。
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

-- 最终守卫从干净临时表开始，复核 0079 全部对象、哈希、外键与完整性。
DROP TABLE IF EXISTS temp.prd086_up_final_guard;
CREATE TEMP TABLE prd086_up_final_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd086_up_final_guard_abort
BEFORE INSERT ON prd086_up_final_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd086_up_final_invalid'); END;
-- 汇总最终 revision 21、角色矩阵、调度游标与 schema 问题，非零时回滚整个升级。
INSERT INTO prd086_up_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=90
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=21)
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||description||
          char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code
      ))='03776958f56b3ac3d3d0355d29b4ae2ea3520be09607295f4fa15a549bbfe28b'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=236
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code
      ))='c737744b0a9f8aa577c49f40bd7cdf2457f69ed93bb0dedf8d01365a5855aef0'
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM permissions permission LEFT JOIN rbac_permission_templates template
        ON template.permission_code=permission.code
      WHERE template.permission_code IS NULL OR permission.catalog_revision!=21
        OR permission.category!=template.category OR permission.name!=template.name
        OR permission.description!=template.description
        OR permission.risk_level!=template.risk_level)
      AND NOT EXISTS(SELECT 1 FROM tenants tenant WHERE(
        SELECT COUNT(*) FROM permissions permission
        WHERE permission.tenant_id=tenant.tenant_id)!=90)
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(SELECT 1 FROM prd086_roles_to_expand selected
      JOIN roles role ON role.tenant_id=selected.tenant_id AND role.role_id=selected.role_id
      WHERE role.matrix_sha256!=CASE role.code
        WHEN 'admin' THEN '49c3be24e085e091cc3c3bf3b8411ed6a783486050d484edec112ed63bee8de5'
        WHEN 'ops_admin' THEN '1f8c90dfb94af73874a8ef3e5470991d36bd7533940adbdbfcbb0e9c6a878783'
      END OR NOT EXISTS(
        SELECT 1 FROM role_permissions relation JOIN permissions permission
          ON permission.tenant_id=relation.tenant_id
         AND permission.permission_id=relation.permission_id
        WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
          AND permission.code='customer_health.read'))
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM customer_health_scheduler_tenants)=
      (SELECT COUNT(*) FROM tenants)
      AND NOT EXISTS(SELECT 1 FROM customer_health_scheduler_tenants
        WHERE status!='pending' OR next_evaluation_at!=created_at
          OR consecutive_failure_count!=0 OR generation!=0
          OR lease_owner_id IS NOT NULL OR lease_token IS NOT NULL
          OR lease_expires_at IS NOT NULL OR last_completed_window_end IS NOT NULL
          OR last_error_code IS NOT NULL OR updated_at!=created_at)
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'customer_health_scores','customer_health_score_outbox',
      'customer_health_scheduler_tenants'))=3 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
      AND name LIKE '%_prd086')=10 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='index'
      AND name LIKE '%_prd086')=3 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd086_up_final_guard_abort;
DROP TABLE prd086_up_final_guard;
DROP TABLE prd086_roles_to_expand;

COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
