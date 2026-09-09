BEGIN IMMEDIATE;
CREATE TABLE platform_meter_recovery_actions (
  recovery_id TEXT PRIMARY KEY CHECK(length(recovery_id)=36 AND substr(recovery_id,1,4)='pmx_'),
  meter_outbox_id TEXT NOT NULL REFERENCES platform_meter_outbox(meter_outbox_id),
  source_fact_id TEXT NOT NULL REFERENCES usage_ledger(usage_ledger_id),
  source_sequence INTEGER NOT NULL REFERENCES usage_export_ledger_sequences(usage_export_sequence),
  tenant_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  operator_id TEXT NOT NULL REFERENCES platform_operators(operator_id),
  action TEXT NOT NULL CHECK(action IN('retry','internal_cost_only')),
  source_attempt_count INTEGER NOT NULL CHECK(source_attempt_count>0),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(meter_outbox_id,source_attempt_count),
  UNIQUE(operator_id,idempotency_sha256)
);
CREATE INDEX idx_meter_recovery_workspace ON platform_meter_recovery_actions(workspace_id,created_at,recovery_id);
CREATE INDEX idx_meter_recovery_recent ON platform_meter_recovery_actions(created_at,recovery_id);
CREATE TRIGGER trg_meter_recovery_source_insert BEFORE INSERT ON platform_meter_recovery_actions
WHEN NOT EXISTS(
  SELECT 1 FROM platform_meter_outbox o
  JOIN usage_ledger u ON u.usage_ledger_id=o.source_usage_ledger_id
  JOIN usage_export_ledger_sequences s ON s.usage_ledger_id=u.usage_ledger_id
  JOIN platform_meter_checkpoints c ON c.projection_code='usage_ledger_v1'
  WHERE o.meter_outbox_id=NEW.meter_outbox_id AND o.status='failed'
    AND o.attempt_count=NEW.source_attempt_count AND u.usage_ledger_id=NEW.source_fact_id
    AND u.tenant_id=NEW.tenant_id AND s.usage_export_sequence=NEW.source_sequence
    AND c.source_sequence=NEW.source_sequence-1
    AND (SELECT COUNT(*) FROM workspace_tenant_bindings b WHERE b.tenant_id=u.tenant_id)=1
    AND (NEW.action='retry' OR (
      o.error_code='platform_metering.owner_invalid'
      AND typeof(u.input_tokens)='integer' AND typeof(u.output_tokens)='integer'
      AND typeof(u.total_tokens)='integer'
      AND u.input_tokens BETWEEN 0 AND 1000000000000
      AND u.output_tokens BETWEEN 0 AND 1000000000000
      AND u.total_tokens BETWEEN 0 AND 1000000000000
      AND u.input_tokens+u.output_tokens=u.total_tokens
      AND julianday(u.created_at) IS NOT NULL
      AND NOT EXISTS(SELECT 1 FROM workspace_subscriptions sub
        WHERE sub.workspace_id=NEW.workspace_id AND sub.tenant_id=NEW.tenant_id
          AND (julianday(sub.created_at) IS NULL OR julianday(sub.created_at)<=julianday(u.created_at)))
      AND NOT EXISTS(SELECT 1 FROM entitlement_snapshots e
        JOIN workspace_subscriptions sub ON sub.subscription_id=e.subscription_id
        WHERE sub.workspace_id=NEW.workspace_id AND sub.tenant_id=NEW.tenant_id
          AND (julianday(e.effective_from) IS NULL
            OR (e.effective_until IS NOT NULL AND julianday(e.effective_until) IS NULL)
            OR (julianday(e.effective_from)<=julianday(u.created_at)
              AND (e.effective_until IS NULL OR julianday(e.effective_until)>=julianday(u.created_at)))))
    ))
)
OR EXISTS(SELECT 1 FROM platform_meter_readings WHERE source_fact_id=NEW.source_fact_id)
BEGIN SELECT RAISE(ABORT,'meter_recovery_source_invalid'); END;
CREATE TRIGGER trg_meter_recovery_update BEFORE UPDATE ON platform_meter_recovery_actions
BEGIN SELECT RAISE(ABORT,'meter_recovery_immutable'); END;
CREATE TRIGGER trg_meter_recovery_delete BEFORE DELETE ON platform_meter_recovery_actions
BEGIN SELECT RAISE(ABORT,'meter_recovery_immutable'); END;

INSERT INTO tenant_deletion_table_manifest(
  source_table,disposition,retention_class,table_sql_sha256,column_signature_sha256,
  trigger_set_sha256,catalog_revision,created_at)
SELECT t.name,'archive_then_delete','billing',metahub_sha256(t.sql),
  metahub_sha256((SELECT group_concat(record,char(30)) FROM(
    SELECT cid||char(31)||name||char(31)||type||char(31)||"notnull"||char(31)
      ||coalesce(dflt_value,'')||char(31)||pk AS record
    FROM pragma_table_info(t.name) ORDER BY cid))),
  metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
    SELECT name||char(31)||metahub_sha256(sql) AS record FROM sqlite_schema
    WHERE type='trigger' AND tbl_name=t.name ORDER BY name)),'')),
  1,strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM sqlite_schema t WHERE type='table' AND name='platform_meter_recovery_actions';
INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
SELECT name,tbl_name,sql,metahub_sha256(sql) FROM sqlite_schema
WHERE type='trigger' AND tbl_name='platform_meter_recovery_actions';
COMMIT;
