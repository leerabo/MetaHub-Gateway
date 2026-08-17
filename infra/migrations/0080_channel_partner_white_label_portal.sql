-- PRD-087 创建租户隔离的渠道伙伴、客户映射、分润物化与 EVT-099 恢复事实。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 升级只接受 0079/revision 21 精确基线，避免在未知目录上继续写入伙伴权限。
DROP TABLE IF EXISTS temp.prd087_up_guard;
CREATE TEMP TABLE prd087_up_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd087_up_guard_abort
BEFORE INSERT ON prd087_up_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd087_up_guard_failed'); END;
INSERT INTO prd087_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT version FROM schema_migrations
      WHERE status='applied' ORDER BY version DESC LIMIT 1)='0079' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=90
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
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name IN(
      'partners','partner_customers','commission_records','partner_commission_cursors',
      'partner_customer_outbox','partner_portal_operations',
      'rbac_permission_templates_prd087_new','permissions_prd087_new'))
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd087')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd087_up_guard_abort;
DROP TABLE prd087_up_guard;

-- 一个租户最多一个 Partner；白标只允许纯文本与固定十六进制颜色。
CREATE TABLE partners(
  partner_id TEXT PRIMARY KEY CHECK(length(partner_id)=36
    AND substr(partner_id,1,4)='par_' AND substr(partner_id,5) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL UNIQUE,
  partner_code TEXT NOT NULL CHECK(length(partner_code) BETWEEN 1 AND 80
    AND partner_code=lower(partner_code)
    AND partner_code NOT GLOB '*[^a-z0-9._-]*'),
  display_name TEXT NOT NULL CHECK(length(CAST(display_name AS BLOB)) BETWEEN 1 AND 120
    AND instr(display_name,char(0))=0 AND instr(display_name,char(10))=0
    AND instr(display_name,char(13))=0),
  portal_name TEXT NOT NULL CHECK(length(CAST(portal_name AS BLOB)) BETWEEN 1 AND 80
    AND instr(portal_name,char(0))=0 AND instr(portal_name,char(10))=0
    AND instr(portal_name,char(13))=0),
  brand_primary_color TEXT NOT NULL CHECK(length(brand_primary_color)=7
    AND substr(brand_primary_color,1,1)='#'
    AND substr(brand_primary_color,2) NOT GLOB '*[^0-9A-F]*'),
  support_text TEXT CHECK(support_text IS NULL OR(
    length(CAST(support_text AS BLOB)) BETWEEN 1 AND 160
    AND instr(support_text,char(0))=0 AND instr(support_text,char(10))=0
    AND instr(support_text,char(13))=0)),
  default_commission_rate_bps INTEGER NOT NULL CHECK(default_commission_rate_bps=1000),
  status TEXT NOT NULL CHECK(status='active'),
  revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=1),
  idempotency_key_sha256 TEXT NOT NULL CHECK(length(idempotency_key_sha256)=64
    AND idempotency_key_sha256 NOT GLOB '*[^0-9a-f]*'),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_by_user_id TEXT NOT NULL,
  operation_request_id TEXT NOT NULL CHECK(length(operation_request_id) BETWEEN 1 AND 128),
  operation_trace_id TEXT NOT NULL CHECK(length(operation_trace_id) BETWEEN 1 AND 128),
  created_at TEXT NOT NULL CHECK(length(created_at)=20
    AND created_at GLOB '????-??-??T??:??:??Z' AND unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(length(updated_at)=20
    AND updated_at GLOB '????-??-??T??:??:??Z' AND unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(tenant_id,created_by_user_id) REFERENCES users(tenant_id,user_id),
  UNIQUE(tenant_id,partner_id),
  UNIQUE(tenant_id,partner_code),
  UNIQUE(tenant_id,idempotency_key_sha256),
  CHECK(unixepoch(updated_at)>=unixepoch(created_at))
);

-- PartnerCustomer 只映射当前租户 Project；同一 Project 不可重复计佣。
CREATE TABLE partner_customers(
  partner_customer_id TEXT PRIMARY KEY CHECK(length(partner_customer_id)=36
    AND substr(partner_customer_id,1,4)='pcu_'
    AND substr(partner_customer_id,5) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  partner_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  customer_code TEXT NOT NULL CHECK(length(customer_code) BETWEEN 1 AND 80
    AND customer_code=lower(customer_code)
    AND customer_code NOT GLOB '*[^a-z0-9._-]*'),
  display_name TEXT NOT NULL CHECK(length(CAST(display_name AS BLOB)) BETWEEN 1 AND 120
    AND instr(display_name,char(0))=0 AND instr(display_name,char(10))=0
    AND instr(display_name,char(13))=0),
  status TEXT NOT NULL CHECK(status IN('active','disabled')),
  linked_from_ledger_sequence INTEGER NOT NULL CHECK(
    typeof(linked_from_ledger_sequence)='integer' AND linked_from_ledger_sequence>=0),
  commission_cutoff_sequence INTEGER CHECK(commission_cutoff_sequence IS NULL OR(
    typeof(commission_cutoff_sequence)='integer'
    AND commission_cutoff_sequence>=linked_from_ledger_sequence)),
  commission_rate_bps INTEGER NOT NULL CHECK(commission_rate_bps=1000),
  revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=1),
  idempotency_key_sha256 TEXT NOT NULL CHECK(length(idempotency_key_sha256)=64
    AND idempotency_key_sha256 NOT GLOB '*[^0-9a-f]*'),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_by_user_id TEXT NOT NULL,
  disabled_by_user_id TEXT,
  operation_request_id TEXT NOT NULL CHECK(length(operation_request_id) BETWEEN 1 AND 128),
  operation_trace_id TEXT NOT NULL CHECK(length(operation_trace_id) BETWEEN 1 AND 128),
  disable_request_id TEXT,
  disable_trace_id TEXT,
  created_at TEXT NOT NULL CHECK(length(created_at)=20
    AND created_at GLOB '????-??-??T??:??:??Z' AND unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(length(updated_at)=20
    AND updated_at GLOB '????-??-??T??:??:??Z' AND unixepoch(updated_at) IS NOT NULL),
  disabled_at TEXT,
  FOREIGN KEY(tenant_id,partner_id) REFERENCES partners(tenant_id,partner_id),
  FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,project_id),
  FOREIGN KEY(tenant_id,created_by_user_id) REFERENCES users(tenant_id,user_id),
  FOREIGN KEY(tenant_id,disabled_by_user_id) REFERENCES users(tenant_id,user_id),
  UNIQUE(tenant_id,partner_customer_id),
  UNIQUE(tenant_id,project_id),
  UNIQUE(tenant_id,partner_id,customer_code),
  UNIQUE(tenant_id,partner_id,idempotency_key_sha256),
  CHECK(unixepoch(updated_at)>=unixepoch(created_at)),
  CHECK((status='active' AND commission_cutoff_sequence IS NULL
      AND disabled_by_user_id IS NULL AND disable_request_id IS NULL
      AND disable_trace_id IS NULL AND disabled_at IS NULL)
    OR(status='disabled' AND commission_cutoff_sequence IS NOT NULL
      AND disabled_by_user_id IS NOT NULL AND disable_request_id IS NOT NULL
      AND disable_trace_id IS NOT NULL AND disabled_at IS NOT NULL))
);
CREATE INDEX idx_partner_customers_list_prd087
  ON partner_customers(tenant_id,partner_id,status,created_at,partner_customer_id);

-- 每个客户一条公平扫描游标；完整终止后状态固定 complete。
CREATE TABLE partner_commission_cursors(
  tenant_id TEXT NOT NULL,
  partner_customer_id TEXT NOT NULL,
  partner_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('pending','leased','complete')),
  last_scanned_ledger_sequence INTEGER NOT NULL CHECK(
    typeof(last_scanned_ledger_sequence)='integer' AND last_scanned_ledger_sequence>=0),
  scan_upper_sequence INTEGER CHECK(scan_upper_sequence IS NULL OR(
    typeof(scan_upper_sequence)='integer'
    AND scan_upper_sequence>=last_scanned_ledger_sequence)),
  generation INTEGER NOT NULL DEFAULT 0 CHECK(typeof(generation)='integer' AND generation>=0),
  lease_owner_id TEXT,
  lease_token TEXT,
  lease_expires_at TEXT,
  next_run_at TEXT,
  consecutive_failure_count INTEGER NOT NULL DEFAULT 0 CHECK(
    typeof(consecutive_failure_count)='integer' AND consecutive_failure_count BETWEEN 0 AND 2147483647),
  last_error_code TEXT CHECK(last_error_code IS NULL
    OR last_error_code='partner.commission_unavailable'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(tenant_id,partner_customer_id),
  FOREIGN KEY(tenant_id,partner_customer_id)
    REFERENCES partner_customers(tenant_id,partner_customer_id),
  FOREIGN KEY(tenant_id,partner_id) REFERENCES partners(tenant_id,partner_id),
  FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,project_id),
  CHECK((status='pending' AND scan_upper_sequence IS NULL
      AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND next_run_at IS NOT NULL)
    OR(status='leased' AND lease_owner_id IS NOT NULL AND lease_token IS NOT NULL
      AND lease_expires_at IS NOT NULL AND next_run_at IS NULL
      AND scan_upper_sequence IS NOT NULL AND last_error_code IS NULL)
    OR(status='complete' AND scan_upper_sequence IS NULL
      AND lease_owner_id IS NULL AND lease_token IS NULL
      AND lease_expires_at IS NULL AND next_run_at IS NULL))
);
CREATE INDEX idx_partner_commission_cursor_claim_prd087
  ON partner_commission_cursors(status,next_run_at,tenant_id,partner_customer_id);

-- CommissionRecord 是来源正式账本的不可变按请求分润事实，不表示支付或结算。
CREATE TABLE commission_records(
  commission_record_sequence INTEGER PRIMARY KEY AUTOINCREMENT CHECK(commission_record_sequence>0),
  commission_record_id TEXT NOT NULL UNIQUE CHECK(length(commission_record_id)=36
    AND substr(commission_record_id,1,4)='com_'
    AND substr(commission_record_id,5) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  partner_id TEXT NOT NULL,
  partner_customer_id TEXT NOT NULL,
  source_finops_ledger_entry_id TEXT NOT NULL,
  source_ledger_sequence INTEGER NOT NULL CHECK(source_ledger_sequence>0),
  source_request_id TEXT NOT NULL,
  source_trace_id TEXT NOT NULL,
  customer_code_snapshot TEXT NOT NULL,
  display_name_snapshot TEXT NOT NULL,
  project_id TEXT NOT NULL,
  amount_micros INTEGER NOT NULL CHECK(typeof(amount_micros)='integer'
    AND amount_micros BETWEEN 0 AND 1000000000000000),
  currency TEXT NOT NULL CHECK(currency IN('CNY','USD')),
  commission_rate_bps INTEGER NOT NULL CHECK(commission_rate_bps BETWEEN 0 AND 10000),
  commission_amount_micros INTEGER NOT NULL CHECK(typeof(commission_amount_micros)='integer'
    AND commission_amount_micros BETWEEN 0 AND 1000000000000000),
  input_tokens INTEGER NOT NULL CHECK(input_tokens>=0),
  output_tokens INTEGER NOT NULL CHECK(output_tokens>=0),
  total_tokens INTEGER NOT NULL CHECK(total_tokens>=0),
  cache_tokens INTEGER NOT NULL CHECK(cache_tokens>=0),
  estimated INTEGER NOT NULL CHECK(estimated IN(0,1)),
  status TEXT NOT NULL CHECK(status='accrued'),
  source_created_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id,partner_id) REFERENCES partners(tenant_id,partner_id),
  FOREIGN KEY(tenant_id,partner_customer_id)
    REFERENCES partner_customers(tenant_id,partner_customer_id),
  FOREIGN KEY(tenant_id,source_finops_ledger_entry_id)
    REFERENCES finops_ledger_entries(tenant_id,finops_ledger_entry_id),
  FOREIGN KEY(tenant_id,source_request_id) REFERENCES request_logs(tenant_id,request_id),
  FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,project_id),
  UNIQUE(tenant_id,commission_record_id),
  UNIQUE(tenant_id,partner_customer_id,source_finops_ledger_entry_id),
  CHECK(total_tokens=input_tokens+output_tokens),
  CHECK(created_at=updated_at)
);
CREATE INDEX idx_commission_records_report_prd087
  ON commission_records(tenant_id,partner_id,source_created_at,partner_customer_id,currency);

-- 领域操作表记录安全摘要和链路，不复制展示名、支持文本或附件正文。
CREATE TABLE partner_portal_operations(
  partner_operation_id TEXT PRIMARY KEY CHECK(length(partner_operation_id)=36
    AND substr(partner_operation_id,1,4)='pop_'
    AND substr(partner_operation_id,5) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  partner_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN('partner.created','partner.branding_updated',
    'partner.customer_created','partner.customer_disabled',
    'partner.commission_exported','partner.delivery_downloaded')),
  object_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  summary_json TEXT NOT NULL CHECK(json_valid(summary_json)
    AND json_type(summary_json)='object' AND length(CAST(summary_json AS BLOB))<=4096),
  summary_sha256 TEXT NOT NULL CHECK(length(summary_sha256)=64
    AND summary_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id,partner_id) REFERENCES partners(tenant_id,partner_id),
  FOREIGN KEY(tenant_id,actor_user_id) REFERENCES users(tenant_id,user_id),
  UNIQUE(tenant_id,partner_operation_id),
  UNIQUE(tenant_id,action,object_id,request_id)
);
CREATE INDEX idx_partner_operations_time_prd087
  ON partner_portal_operations(tenant_id,partner_id,created_at DESC,partner_operation_id);

-- EVT-099 source outbox 保存原始操作者与稳定链路，worker 必须回读 PartnerCustomer 重建 payload。
CREATE TABLE partner_customer_outbox(
  partner_customer_outbox_id TEXT PRIMARY KEY CHECK(length(partner_customer_outbox_id)=36
    AND substr(partner_customer_outbox_id,1,4)='pco_'
    AND substr(partner_customer_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  tenant_id TEXT NOT NULL,
  partner_id TEXT NOT NULL,
  partner_customer_id TEXT NOT NULL,
  webhook_event_id TEXT NOT NULL CHECK(length(webhook_event_id)=40
    AND substr(webhook_event_id,1,8)='evt_pcu_'
    AND substr(webhook_event_id,9) NOT GLOB '*[^0-9a-f]*'),
  actor_user_id TEXT NOT NULL,
  source_request_id TEXT NOT NULL,
  source_trace_id TEXT NOT NULL,
  event_timestamp TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)
    AND json_type(payload_json)='object' AND length(CAST(payload_json AS BLOB))<=4096),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('pending','leased','sent','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 10),
  generation INTEGER NOT NULL DEFAULT 0 CHECK(generation>=0),
  lease_owner_id TEXT,
  lease_token TEXT,
  lease_expires_at TEXT,
  next_attempt_at TEXT,
  subscription_snapshot_json TEXT CHECK(subscription_snapshot_json IS NULL OR(
    json_valid(subscription_snapshot_json) AND json_type(subscription_snapshot_json)='array'
    AND length(CAST(subscription_snapshot_json AS BLOB))<=65536)),
  snapshot_frozen_at TEXT,
  last_error_code TEXT CHECK(last_error_code IS NULL OR last_error_code IN(
    'webhook.payload_invalid','webhook.delivery_unavailable','partner.event_unavailable')),
  delivered_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id,partner_id) REFERENCES partners(tenant_id,partner_id),
  FOREIGN KEY(tenant_id,partner_customer_id)
    REFERENCES partner_customers(tenant_id,partner_customer_id),
  FOREIGN KEY(tenant_id,actor_user_id) REFERENCES users(tenant_id,user_id),
  UNIQUE(tenant_id,partner_customer_id),
  UNIQUE(tenant_id,webhook_event_id),
  CHECK((status='pending' AND attempt_count BETWEEN 0 AND 9
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
CREATE INDEX idx_partner_outbox_claim_prd087
  ON partner_customer_outbox(status,next_attempt_at,tenant_id,created_at,partner_customer_outbox_id);

-- Domain owner 和不可变触发器防止 rowid/replace 绕过服务层状态机。
CREATE TRIGGER trg_partners_owner_insert_prd087
BEFORE INSERT ON partners FOR EACH ROW
WHEN NEW.status!='active' OR NEW.default_commission_rate_bps!=1000 OR NEW.revision!=1
  OR NOT EXISTS(SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR NOT EXISTS(SELECT 1 FROM users actor WHERE actor.tenant_id=NEW.tenant_id
    AND actor.user_id=NEW.created_by_user_id AND actor.status='active')
  OR EXISTS(SELECT 1 FROM partners current WHERE current.tenant_id=NEW.tenant_id
    OR current.partner_id=NEW.partner_id OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'partner_owner_mismatch'); END;
CREATE TRIGGER trg_partners_update_prd087
BEFORE UPDATE ON partners FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.partner_id!=OLD.partner_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.partner_code!=OLD.partner_code OR NEW.display_name!=OLD.display_name
  OR NEW.default_commission_rate_bps!=OLD.default_commission_rate_bps
  OR NEW.status!=OLD.status OR NEW.idempotency_key_sha256!=OLD.idempotency_key_sha256
  OR NEW.payload_sha256!=OLD.payload_sha256 OR NEW.created_by_user_id!=OLD.created_by_user_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR(NEW.portal_name=OLD.portal_name AND NEW.brand_primary_color=OLD.brand_primary_color
    AND NEW.support_text IS OLD.support_text)
BEGIN SELECT RAISE(ABORT,'partner_transition_invalid'); END;
CREATE TRIGGER trg_partners_delete_prd087 BEFORE DELETE ON partners FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'partner_immutable'); END;

CREATE TRIGGER trg_partner_customers_owner_insert_prd087
BEFORE INSERT ON partner_customers FOR EACH ROW
WHEN NEW.status!='active' OR NEW.revision!=1 OR NEW.commission_rate_bps!=1000
  OR NEW.commission_cutoff_sequence IS NOT NULL OR NEW.disabled_at IS NOT NULL
  OR NEW.linked_from_ledger_sequence!=(
    SELECT COALESCE(MAX(entry.ledger_sequence),0) FROM finops_ledger_entries entry
    WHERE entry.tenant_id=NEW.tenant_id)
  OR NOT EXISTS(SELECT 1 FROM partners partner WHERE partner.tenant_id=NEW.tenant_id
    AND partner.partner_id=NEW.partner_id AND partner.status='active'
    AND partner.default_commission_rate_bps=NEW.commission_rate_bps)
  OR NOT EXISTS(SELECT 1 FROM projects project WHERE project.tenant_id=NEW.tenant_id
    AND project.project_id=NEW.project_id AND project.status='active')
  OR EXISTS(SELECT 1 FROM partner_customers current
    WHERE current.partner_customer_id=NEW.partner_customer_id
      OR(current.tenant_id=NEW.tenant_id AND current.project_id=NEW.project_id)
      OR(current.tenant_id=NEW.tenant_id AND current.partner_id=NEW.partner_id
        AND current.customer_code=NEW.customer_code)
      OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'partner_customer_owner_mismatch'); END;
CREATE TRIGGER trg_partner_customers_update_prd087
BEFORE UPDATE ON partner_customers FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.partner_customer_id!=OLD.partner_customer_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.partner_id!=OLD.partner_id
  OR NEW.project_id!=OLD.project_id OR NEW.customer_code!=OLD.customer_code
  OR NEW.linked_from_ledger_sequence!=OLD.linked_from_ledger_sequence
  OR NEW.commission_rate_bps!=OLD.commission_rate_bps
  OR NEW.idempotency_key_sha256!=OLD.idempotency_key_sha256
  OR NEW.payload_sha256!=OLD.payload_sha256 OR NEW.created_by_user_id!=OLD.created_by_user_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR OLD.status!='active' OR NEW.status NOT IN('active','disabled')
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR(NEW.status='active' AND NEW.display_name=OLD.display_name)
  OR(NEW.status='disabled' AND(
    NEW.display_name!=OLD.display_name OR NEW.commission_cutoff_sequence IS NULL
    OR NEW.commission_cutoff_sequence!=(
      SELECT COALESCE(MAX(entry.ledger_sequence),0) FROM finops_ledger_entries entry
      WHERE entry.tenant_id=NEW.tenant_id)
    OR NEW.disabled_by_user_id IS NULL OR NEW.disable_request_id IS NULL
    OR NEW.disable_trace_id IS NULL OR NEW.disabled_at IS NULL))
BEGIN SELECT RAISE(ABORT,'partner_customer_transition_invalid'); END;
CREATE TRIGGER trg_partner_customers_delete_prd087 BEFORE DELETE ON partner_customers FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'partner_customer_immutable'); END;

CREATE TRIGGER trg_commission_records_owner_insert_prd087
BEFORE INSERT ON commission_records FOR EACH ROW
WHEN NEW.commission_record_id!='com_'||substr(metahub_sha256(json_array(
      'partner-commission-v1',NEW.tenant_id,NEW.partner_customer_id,
      NEW.source_finops_ledger_entry_id)),1,32)
  OR NOT EXISTS(
  SELECT 1 FROM partner_customers customer
  JOIN finops_ledger_entries entry ON entry.tenant_id=customer.tenant_id
    AND entry.finops_ledger_entry_id=NEW.source_finops_ledger_entry_id
  WHERE customer.tenant_id=NEW.tenant_id
    AND customer.partner_customer_id=NEW.partner_customer_id
    AND customer.partner_id=NEW.partner_id AND customer.project_id=NEW.project_id
    AND entry.ledger_sequence=NEW.source_ledger_sequence
    AND entry.entry_type='usage_charge' AND entry.project_id=customer.project_id
    AND entry.source_request_id=NEW.source_request_id
    AND entry.source_trace_id=NEW.source_trace_id
    AND entry.amount_micros=NEW.amount_micros AND entry.currency=NEW.currency
    AND entry.input_tokens=NEW.input_tokens AND entry.output_tokens=NEW.output_tokens
    AND entry.total_tokens=NEW.total_tokens AND entry.cache_tokens=NEW.cache_tokens
    AND entry.estimated=NEW.estimated AND entry.created_at=NEW.source_created_at
    AND entry.ledger_sequence>customer.linked_from_ledger_sequence
    AND(customer.commission_cutoff_sequence IS NULL
      OR entry.ledger_sequence<=customer.commission_cutoff_sequence)
    AND NEW.commission_rate_bps=customer.commission_rate_bps
    AND NEW.commission_amount_micros=
      (NEW.amount_micros/10000)*NEW.commission_rate_bps
      +(((NEW.amount_micros%10000)*NEW.commission_rate_bps+5000)/10000)
    AND NEW.customer_code_snapshot=customer.customer_code
    AND NEW.display_name_snapshot=customer.display_name)
  OR EXISTS(SELECT 1 FROM commission_records current
    WHERE current.commission_record_id=NEW.commission_record_id
      OR(current.tenant_id=NEW.tenant_id
        AND current.partner_customer_id=NEW.partner_customer_id
        AND current.source_finops_ledger_entry_id=NEW.source_finops_ledger_entry_id)
      OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'commission_record_owner_mismatch'); END;
CREATE TRIGGER trg_commission_records_update_prd087 BEFORE UPDATE ON commission_records FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'commission_record_immutable'); END;
CREATE TRIGGER trg_commission_records_delete_prd087 BEFORE DELETE ON commission_records FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'commission_record_immutable'); END;

CREATE TRIGGER trg_partner_operations_owner_insert_prd087
BEFORE INSERT ON partner_portal_operations FOR EACH ROW
WHEN NEW.summary_sha256!=metahub_sha256(NEW.summary_json)
  OR NOT EXISTS(SELECT 1 FROM partners partner
    WHERE partner.tenant_id=NEW.tenant_id AND partner.partner_id=NEW.partner_id)
  OR NOT EXISTS(SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.actor_user_id)
  OR NOT(
    (NEW.action IN('partner.created','partner.branding_updated',
        'partner.commission_exported','partner.delivery_downloaded')
      AND NEW.object_id=NEW.partner_id)
    OR(NEW.action IN('partner.customer_created','partner.customer_disabled')
      AND EXISTS(SELECT 1 FROM partner_customers customer
        WHERE customer.tenant_id=NEW.tenant_id
          AND customer.partner_id=NEW.partner_id
          AND customer.partner_customer_id=NEW.object_id)))
  OR EXISTS(SELECT 1 FROM json_tree(NEW.summary_json)
    WHERE key IN('provider_key','virtual_key','token','prompt','response','endpoint',
      'authorization','cookie','password','secret','support_text','display_name'))
  OR EXISTS(SELECT 1 FROM partner_portal_operations current
    WHERE current.partner_operation_id=NEW.partner_operation_id
      OR(current.tenant_id=NEW.tenant_id AND current.action=NEW.action
        AND current.object_id=NEW.object_id AND current.request_id=NEW.request_id)
      OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'partner_operation_owner_mismatch'); END;
CREATE TRIGGER trg_partner_operations_update_prd087 BEFORE UPDATE ON partner_portal_operations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'partner_operation_immutable'); END;
CREATE TRIGGER trg_partner_operations_delete_prd087 BEFORE DELETE ON partner_portal_operations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'partner_operation_immutable'); END;

CREATE TRIGGER trg_partner_commission_cursor_owner_insert_prd087
BEFORE INSERT ON partner_commission_cursors FOR EACH ROW
WHEN NEW.status!='pending' OR NEW.generation!=0
  OR NEW.scan_upper_sequence IS NOT NULL
  OR NEW.lease_owner_id IS NOT NULL OR NEW.lease_token IS NOT NULL
  OR NEW.lease_expires_at IS NOT NULL OR NEW.last_error_code IS NOT NULL
  OR NEW.consecutive_failure_count!=0 OR NEW.next_run_at!=NEW.created_at
  OR NEW.updated_at!=NEW.created_at
  OR NOT EXISTS(SELECT 1 FROM partner_customers customer
    WHERE customer.tenant_id=NEW.tenant_id
      AND customer.partner_customer_id=NEW.partner_customer_id
      AND customer.partner_id=NEW.partner_id AND customer.project_id=NEW.project_id
      AND customer.status='active'
      AND customer.linked_from_ledger_sequence=NEW.last_scanned_ledger_sequence)
  OR EXISTS(SELECT 1 FROM partner_commission_cursors current
    WHERE(current.tenant_id=NEW.tenant_id
        AND current.partner_customer_id=NEW.partner_customer_id)
      OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'partner_commission_cursor_owner_mismatch'); END;

CREATE TRIGGER trg_partner_commission_cursor_transition_prd087
BEFORE UPDATE ON partner_commission_cursors FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.partner_customer_id!=OLD.partner_customer_id
  OR NEW.partner_id!=OLD.partner_id OR NEW.project_id!=OLD.project_id
  OR NEW.created_at!=OLD.created_at OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
  OR NOT EXISTS(SELECT 1 FROM partner_customers customer
    WHERE customer.tenant_id=NEW.tenant_id
      AND customer.partner_customer_id=NEW.partner_customer_id
      AND customer.partner_id=NEW.partner_id AND customer.project_id=NEW.project_id)
  OR NOT(
    (OLD.status='pending' AND NEW.status='leased'
      AND NEW.generation=OLD.generation+1
      AND NEW.last_scanned_ledger_sequence=OLD.last_scanned_ledger_sequence
      AND NEW.consecutive_failure_count=OLD.consecutive_failure_count
      AND NEW.scan_upper_sequence>=OLD.last_scanned_ledger_sequence
      AND NEW.scan_upper_sequence=(SELECT CASE
        WHEN customer.commission_cutoff_sequence IS NOT NULL
          AND customer.commission_cutoff_sequence<(
            SELECT COALESCE(MAX(entry.ledger_sequence),0) FROM finops_ledger_entries entry
            WHERE entry.tenant_id=NEW.tenant_id)
          THEN customer.commission_cutoff_sequence
        ELSE(SELECT COALESCE(MAX(entry.ledger_sequence),0)
          FROM finops_ledger_entries entry WHERE entry.tenant_id=NEW.tenant_id) END
        FROM partner_customers customer
        WHERE customer.tenant_id=NEW.tenant_id
          AND customer.partner_customer_id=NEW.partner_customer_id)
      AND unixepoch(NEW.lease_expires_at)>unixepoch(NEW.updated_at))
    OR(OLD.status='leased' AND NEW.status='leased'
      AND NEW.generation=OLD.generation+1
      AND NEW.last_scanned_ledger_sequence=OLD.last_scanned_ledger_sequence
      AND NEW.scan_upper_sequence=OLD.scan_upper_sequence
      AND NEW.consecutive_failure_count=OLD.consecutive_failure_count
      AND NEW.lease_token!=OLD.lease_token
      AND unixepoch(NEW.lease_expires_at)>unixepoch(NEW.updated_at)
      AND((unixepoch(OLD.lease_expires_at)<=unixepoch(NEW.updated_at))
        OR(NEW.lease_owner_id=OLD.lease_owner_id
          AND unixepoch(NEW.lease_expires_at)>unixepoch(OLD.lease_expires_at))))
    OR(OLD.status='leased' AND NEW.status='pending'
      AND NEW.generation=OLD.generation+1
      AND unixepoch(OLD.lease_expires_at)>unixepoch(NEW.updated_at)
      AND NEW.last_scanned_ledger_sequence BETWEEN OLD.last_scanned_ledger_sequence
        AND OLD.scan_upper_sequence
      AND((NEW.last_error_code IS NULL AND NEW.consecutive_failure_count=0)
        OR(NEW.last_error_code='partner.commission_unavailable'
          AND NEW.last_scanned_ledger_sequence=OLD.last_scanned_ledger_sequence
          AND NEW.consecutive_failure_count=OLD.consecutive_failure_count+1)))
    OR(OLD.status='leased' AND NEW.status='complete'
      AND NEW.generation=OLD.generation+1
      AND unixepoch(OLD.lease_expires_at)>unixepoch(NEW.updated_at)
      AND NEW.last_error_code IS NULL AND NEW.consecutive_failure_count=0
      AND NEW.last_scanned_ledger_sequence=OLD.scan_upper_sequence
      AND EXISTS(SELECT 1 FROM partner_customers customer
        WHERE customer.tenant_id=NEW.tenant_id
          AND customer.partner_customer_id=NEW.partner_customer_id
          AND customer.status='disabled'
          AND customer.commission_cutoff_sequence=NEW.last_scanned_ledger_sequence)))
BEGIN SELECT RAISE(ABORT,'partner_commission_cursor_transition_invalid'); END;

CREATE TRIGGER trg_partner_commission_cursor_delete_prd087
BEFORE DELETE ON partner_commission_cursors FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'partner_commission_cursor_immutable'); END;

CREATE TRIGGER trg_partner_customer_outbox_owner_insert_prd087
BEFORE INSERT ON partner_customer_outbox FOR EACH ROW
WHEN NEW.partner_customer_outbox_id!='pco_'||substr(metahub_sha256(
      NEW.partner_customer_id),1,32)
  OR NEW.webhook_event_id!='evt_pcu_'||substr(metahub_sha256(
      NEW.partner_customer_id),1,32)
  OR NEW.status!='pending' OR NEW.attempt_count!=0 OR NEW.generation!=0
  OR NEW.lease_owner_id IS NOT NULL OR NEW.lease_token IS NOT NULL
  OR NEW.lease_expires_at IS NOT NULL OR NEW.last_error_code IS NOT NULL
  OR NEW.delivered_at IS NOT NULL OR NEW.subscription_snapshot_json IS NOT NULL
  OR NEW.snapshot_frozen_at IS NOT NULL OR NEW.next_attempt_at!=NEW.created_at
  OR NEW.updated_at!=NEW.created_at OR NEW.event_timestamp!=NEW.created_at
  OR NEW.payload_sha256!=metahub_sha256(NEW.payload_json)
  OR(SELECT COUNT(*) FROM json_each(NEW.payload_json))!=7
  OR EXISTS(SELECT 1 FROM json_each(NEW.payload_json)
    WHERE key NOT IN('schema_version','partner_id','partner_customer_id','customer_code',
      'project_id','status','commission_rate_bps'))
  OR NOT EXISTS(SELECT 1 FROM partner_customers customer
    WHERE customer.tenant_id=NEW.tenant_id
      AND customer.partner_id=NEW.partner_id
      AND customer.partner_customer_id=NEW.partner_customer_id
      AND customer.created_by_user_id=NEW.actor_user_id
      AND customer.operation_request_id=NEW.source_request_id
      AND customer.operation_trace_id=NEW.source_trace_id
      AND customer.created_at=NEW.created_at AND customer.status='active'
      AND json_extract(NEW.payload_json,'$.schema_version')='1.0'
      AND json_extract(NEW.payload_json,'$.partner_id')=customer.partner_id
      AND json_extract(NEW.payload_json,'$.partner_customer_id')=customer.partner_customer_id
      AND json_extract(NEW.payload_json,'$.customer_code')=customer.customer_code
      AND json_extract(NEW.payload_json,'$.project_id')=customer.project_id
      AND json_extract(NEW.payload_json,'$.status')='active'
      AND json_extract(NEW.payload_json,'$.commission_rate_bps')=customer.commission_rate_bps)
  OR EXISTS(SELECT 1 FROM partner_customer_outbox current
    WHERE current.partner_customer_outbox_id=NEW.partner_customer_outbox_id
      OR(current.tenant_id=NEW.tenant_id
        AND current.partner_customer_id=NEW.partner_customer_id)
      OR(current.tenant_id=NEW.tenant_id AND current.webhook_event_id=NEW.webhook_event_id)
      OR(NEW.rowid!=-1 AND current.rowid=NEW.rowid))
BEGIN SELECT RAISE(ABORT,'partner_customer_outbox_owner_mismatch'); END;

CREATE TRIGGER trg_partner_customer_outbox_transition_prd087
BEFORE UPDATE ON partner_customer_outbox FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.partner_customer_outbox_id!=OLD.partner_customer_outbox_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.partner_id!=OLD.partner_id
  OR NEW.partner_customer_id!=OLD.partner_customer_id
  OR NEW.webhook_event_id!=OLD.webhook_event_id OR NEW.actor_user_id!=OLD.actor_user_id
  OR NEW.source_request_id!=OLD.source_request_id
  OR NEW.source_trace_id!=OLD.source_trace_id
  OR NEW.event_timestamp!=OLD.event_timestamp OR NEW.payload_json!=OLD.payload_json
  OR NEW.payload_sha256!=OLD.payload_sha256 OR NEW.created_at!=OLD.created_at
  OR unixepoch(NEW.updated_at)<unixepoch(OLD.updated_at)
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
      AND NEW.lease_token!=OLD.lease_token
      AND unixepoch(NEW.lease_expires_at)>unixepoch(NEW.updated_at)
      AND((unixepoch(OLD.lease_expires_at)<=unixepoch(NEW.updated_at))
        OR(NEW.lease_owner_id=OLD.lease_owner_id
          AND unixepoch(NEW.lease_expires_at)>unixepoch(OLD.lease_expires_at))))
    OR(OLD.status='leased' AND NEW.status='pending'
      AND NEW.attempt_count=OLD.attempt_count AND NEW.generation=OLD.generation+1
      AND unixepoch(OLD.lease_expires_at)>unixepoch(NEW.updated_at))
    OR(OLD.status='leased' AND NEW.status IN('sent','failed')
      AND NEW.attempt_count=OLD.attempt_count AND NEW.generation=OLD.generation+1
      AND unixepoch(OLD.lease_expires_at)>unixepoch(NEW.updated_at)))
BEGIN SELECT RAISE(ABORT,'partner_customer_outbox_transition_invalid'); END;

CREATE TRIGGER trg_partner_customer_outbox_delete_prd087
BEFORE DELETE ON partner_customer_outbox FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'partner_customer_outbox_immutable'); END;

-- 仅选择仍为 revision-21 旧默认矩阵的 admin/viewer，避免擅自扩大租户自定义 viewer。
CREATE TEMP TABLE prd087_roles_to_expand AS
SELECT role.role_id,role.tenant_id,role.code
FROM roles role
WHERE role.code IN('admin','viewer')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '49c3be24e085e091cc3c3bf3b8411ed6a783486050d484edec112ed63bee8de5'
    WHEN 'viewer' THEN '33a1c60b27b9536355aa1e93807771d840b8609d74db52349137a94965ae42f9'
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

-- 临时移除 RBAC 不可变触发器，完成 revision 22 的原子目录切换后逐一恢复。
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

CREATE TABLE rbac_permission_templates_prd087_new(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=22)
);
INSERT INTO rbac_permission_templates_prd087_new(
  rowid,permission_code,category,name,description,risk_level,catalog_revision)
SELECT rowid,permission_code,category,name,description,risk_level,22
FROM rbac_permission_templates;
-- 既有八项目录元数据曾在 Shared 合同中收紧但未回写数据库；revision 22 原子收敛到权威定义。
UPDATE rbac_permission_templates_prd087_new SET
  name='读取文件处理记录',
  description='读取当前租户文件名称、解析状态、DLP 摘要和 Token 预估。'
WHERE permission_code='file.read';
UPDATE rbac_permission_templates_prd087_new SET
  name='上传并解析文件',
  description='上传当前租户 PDF、DOCX 或 TXT，并执行解析、DLP 预扫描和加密存储。'
WHERE permission_code='file.write';
UPDATE rbac_permission_templates_prd087_new SET
  description='读取当前租户本地模型 Endpoint 的脱敏聚合与目录摘要。'
WHERE permission_code='local_endpoint.read';
UPDATE rbac_permission_templates_prd087_new SET
  description='原子注册当前租户本地 Endpoint、初始模型和可选凭据。'
WHERE permission_code='local_endpoint.manage';
UPDATE rbac_permission_templates_prd087_new SET
  description='执行当前租户本地 Endpoint 的受限健康与目录探测。'
WHERE permission_code='local_endpoint.test';
UPDATE rbac_permission_templates_prd087_new SET
  name='管理 MCP 网络策略'
WHERE permission_code='mcp.network.manage';
UPDATE rbac_permission_templates_prd087_new SET
  description='读取当前租户模板、不可变版本历史和授权解密后的定义。'
WHERE permission_code='prompt_template.read';
UPDATE rbac_permission_templates_prd087_new SET
  description='创建、启停、修改元数据并发布当前租户的不可变模板版本。'
WHERE permission_code='prompt_template.write';
UPDATE rbac_permission_templates_prd087_new SET
  description='在企业 Chat 中调用当前已发布模板，并接收本次内存渲染结果。'
WHERE permission_code='prompt_template.use';
INSERT INTO rbac_permission_templates_prd087_new(
  permission_code,category,name,description,risk_level,catalog_revision)
VALUES
  ('partner_portal.read','partner','读取伙伴门户',
    '读取当前租户伙伴资料、客户安全聚合与分润摘要。','read',22),
  ('partner_portal.configure','partner','配置伙伴门户',
    '首次创建当前租户唯一伙伴资料与冻结分润基线。','critical',22),
  ('partner_customer.manage','partner','管理伙伴客户',
    '创建或终止当前伙伴租户内的 Project 客户映射。','critical',22),
  ('partner_branding.manage','partner','管理伙伴白标',
    '更新当前伙伴门户的纯文本品牌名称、颜色和支持信息。','write',22),
  ('partner_commission.export','partner','导出伙伴分润',
    '导出当前伙伴按币种汇总的非结算分润报表。','critical',22),
  ('partner_delivery.download','partner','下载伙伴交付包',
    '下载当前伙伴不含凭据和内部地址的安全交付清单。','critical',22);
DROP TABLE rbac_permission_templates;
ALTER TABLE rbac_permission_templates_prd087_new RENAME TO rbac_permission_templates;

-- admin 模板新增六项，viewer 模板新增除首次配置外的五项。
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
VALUES
  ('admin','partner_portal.read',1),('admin','partner_portal.configure',1),
  ('admin','partner_customer.manage',1),('admin','partner_branding.manage',1),
  ('admin','partner_commission.export',1),('admin','partner_delivery.download',1),
  ('viewer','partner_portal.read',1),('viewer','partner_customer.manage',1),
  ('viewer','partner_branding.manage',1),('viewer','partner_commission.export',1),
  ('viewer','partner_delivery.download',1);

CREATE TABLE permissions_prd087_new(
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=22),
  status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code),
  UNIQUE(tenant_id,permission_id)
);
INSERT INTO permissions_prd087_new(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at)
SELECT permission.rowid,permission.permission_id,permission.tenant_id,permission.code,
  template.category,template.name,template.description,template.risk_level,
  22,permission.status,permission.created_at,permission.updated_at
FROM permissions permission JOIN rbac_permission_templates template
  ON template.permission_code=permission.code;
INSERT INTO permissions_prd087_new(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at)
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
  template.category,template.name,template.description,template.risk_level,22,'active',
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant JOIN rbac_permission_templates template
  ON template.permission_code LIKE 'partner_%';
DROP TABLE permissions;
ALTER TABLE permissions_prd087_new RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);

INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
  permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM prd087_roles_to_expand role JOIN permissions permission
  ON permission.tenant_id=role.tenant_id
WHERE permission.code LIKE 'partner_%'
  AND(role.code='admin' OR permission.code!='partner_portal.configure');
UPDATE roles SET permission_revision=permission_revision+1,
  matrix_sha256=CASE code
    WHEN 'admin' THEN '0d70d1adb89f3809dddbb13e41e25b851049eb656d10d13cd2dffe83f045a57a'
    WHEN 'viewer' THEN '7b4af42e2f92112bffbed7c466aa4cbd4f41df89eb9c40ee0da8be91f07169e3'
  END
WHERE role_id IN(SELECT role_id FROM prd087_roles_to_expand);

-- 恢复 RBAC 目录、实例和矩阵不可变触发器。
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
CREATE TRIGGER trg_permissions_update_prd059 BEFORE UPDATE ON permissions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_delete_prd059 BEFORE DELETE ON permissions FOR EACH ROW
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
    WHEN 'admin' THEN '0d70d1adb89f3809dddbb13e41e25b851049eb656d10d13cd2dffe83f045a57a'
    WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1'
    WHEN 'viewer' THEN '7b4af42e2f92112bffbed7c466aa4cbd4f41df89eb9c40ee0da8be91f07169e3'
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
      WHEN 'admin' THEN '0d70d1adb89f3809dddbb13e41e25b851049eb656d10d13cd2dffe83f045a57a'
      WHEN 'developer' THEN 'ee1bc616db2e67a2ddf1e984b938c92033ed3d486ea0b9f52300c2579dc943b1'
      WHEN 'viewer' THEN '7b4af42e2f92112bffbed7c466aa4cbd4f41df89eb9c40ee0da8be91f07169e3'
      WHEN 'finance_admin' THEN '86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c'
      WHEN 'security_admin' THEN 'aeb4d6b081dce8ed36c6513b567aba4be395816c5a5ad2f1d939527a50ad14df'
      WHEN 'ops_admin' THEN '1f8c90dfb94af73874a8ef3e5470991d36bd7533940adbdbfcbb0e9c6a878783'
    END,CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,22,'active',
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;
  INSERT INTO role_permissions(
    role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,role.role_id,
    permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM roles role JOIN rbac_role_permission_templates template ON template.role_code=role.code
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
    AND permission.code=template.permission_code
  WHERE role.tenant_id=NEW.tenant_id;
END;

-- 最终复核 revision 22、角色模板、租户权限投影、业务表与数据库完整性。
DROP TABLE IF EXISTS temp.prd087_up_final_guard;
CREATE TEMP TABLE prd087_up_final_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd087_up_final_guard_abort
BEFORE INSERT ON prd087_up_final_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd087_up_final_invalid'); END;
INSERT INTO prd087_up_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=96
      AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=22)
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT permission_code||char(31)||category||char(31)||name||char(31)||description||
          char(31)||risk_level||char(31)||catalog_revision AS record
        FROM rbac_permission_templates ORDER BY permission_code
      ))='b44391603c39067c55f6d32742e2cb3daa9833721ab2ed4657c2c50db4dca125'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=247
      AND (SELECT metahub_sha256(group_concat(record,char(30))) FROM(
        SELECT role_code||char(31)||permission_code||char(31)||default_granted AS record
        FROM rbac_role_permission_templates ORDER BY role_code,permission_code
      ))='0ebf053173c2a5b1acc10143f092753ef2dafe1d63f2a4ba8398ff49599b4379'
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM permissions permission LEFT JOIN rbac_permission_templates template
        ON template.permission_code=permission.code
      WHERE template.permission_code IS NULL OR permission.catalog_revision!=22
        OR permission.category!=template.category OR permission.name!=template.name
        OR permission.description!=template.description
        OR permission.risk_level!=template.risk_level)
      AND NOT EXISTS(SELECT 1 FROM tenants tenant WHERE(
        SELECT COUNT(*) FROM permissions permission
        WHERE permission.tenant_id=tenant.tenant_id)!=96)
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'partners','partner_customers','commission_records','partner_commission_cursors',
      'partner_customer_outbox','partner_portal_operations'))=6 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
      AND name LIKE '%_prd087')=18 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='index'
      AND name LIKE '%_prd087')=5 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(SELECT 1 FROM prd087_roles_to_expand selected
      JOIN roles role ON role.role_id=selected.role_id
      WHERE role.matrix_sha256!=CASE role.code
        WHEN 'admin' THEN '0d70d1adb89f3809dddbb13e41e25b851049eb656d10d13cd2dffe83f045a57a'
        WHEN 'viewer' THEN '7b4af42e2f92112bffbed7c466aa4cbd4f41df89eb9c40ee0da8be91f07169e3' END)
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd087_up_final_guard_abort;
DROP TABLE prd087_up_final_guard;
DROP TABLE prd087_roles_to_expand;

COMMIT;
PRAGMA foreign_keys = ON;
