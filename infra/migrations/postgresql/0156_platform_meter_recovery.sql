CREATE TABLE platform_meter_recovery_actions (
  recovery_id TEXT PRIMARY KEY CHECK(length(recovery_id)=36 AND substr(recovery_id,1,4)='pmx_'),
  meter_outbox_id TEXT NOT NULL REFERENCES platform_meter_outbox(meter_outbox_id),
  source_fact_id TEXT NOT NULL REFERENCES usage_ledger(usage_ledger_id),
  source_sequence BIGINT NOT NULL REFERENCES usage_export_ledger_sequences(usage_export_sequence),
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
  created_at TIMESTAMPTZ NOT NULL,
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(meter_outbox_id,source_attempt_count),
  UNIQUE(operator_id,idempotency_sha256)
);
CREATE INDEX idx_meter_recovery_workspace ON platform_meter_recovery_actions(workspace_id,created_at,recovery_id);
CREATE INDEX idx_meter_recovery_recent ON platform_meter_recovery_actions(created_at,recovery_id);
CREATE FUNCTION fn_meter_recovery_source_insert() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.action='internal_cost_only' THEN
    LOCK TABLE workspace_subscriptions,entitlement_snapshots IN SHARE MODE;
  END IF;
  IF NOT EXISTS(
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
        AND u.input_tokens BETWEEN 0 AND 1000000000000
        AND u.output_tokens BETWEEN 0 AND 1000000000000
        AND u.total_tokens BETWEEN 0 AND 1000000000000
        AND u.input_tokens+u.output_tokens=u.total_tokens
        AND NOT EXISTS(SELECT 1 FROM workspace_subscriptions sub
          WHERE sub.workspace_id=NEW.workspace_id AND sub.tenant_id=NEW.tenant_id
            AND sub.created_at<=u.created_at)
        AND NOT EXISTS(SELECT 1 FROM entitlement_snapshots e
          JOIN workspace_subscriptions sub ON sub.subscription_id=e.subscription_id
          WHERE sub.workspace_id=NEW.workspace_id AND sub.tenant_id=NEW.tenant_id
            AND e.effective_from<=u.created_at
            AND (e.effective_until IS NULL OR e.effective_until>u.created_at))
      ))
  ) OR EXISTS(SELECT 1 FROM platform_meter_readings WHERE source_fact_id=NEW.source_fact_id)
  THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='meter_recovery_source_invalid'; END IF;
  RETURN NEW;
END $$;
CREATE FUNCTION fn_meter_recovery_immutable() RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='meter_recovery_immutable'; END $$;
CREATE TRIGGER trg_meter_recovery_source_insert BEFORE INSERT ON platform_meter_recovery_actions
FOR EACH ROW EXECUTE FUNCTION fn_meter_recovery_source_insert();
CREATE TRIGGER trg_meter_recovery_update BEFORE UPDATE ON platform_meter_recovery_actions
FOR EACH ROW EXECUTE FUNCTION fn_meter_recovery_immutable();
CREATE TRIGGER trg_meter_recovery_delete BEFORE DELETE ON platform_meter_recovery_actions
FOR EACH ROW EXECUTE FUNCTION fn_meter_recovery_immutable();

DO $$ DECLARE table_name TEXT := 'platform_meter_recovery_actions'; BEGIN
  WITH columns AS (
    SELECT a.attnum,a.attname,format_type(a.atttypid,a.atttypmod) AS type_name,
      a.attnotnull,pg_get_expr(d.adbin,d.adrelid) AS default_sql
    FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
    WHERE a.attrelid=table_name::regclass AND a.attnum>0 AND NOT a.attisdropped
  ), definitions AS (
    SELECT attnum AS ordinal,quote_ident(attname)||' '||type_name
      ||CASE WHEN default_sql IS NULL THEN '' ELSE ' DEFAULT '||default_sql END
      ||CASE WHEN attnotnull THEN ' NOT NULL' ELSE '' END AS sql FROM columns
    UNION ALL
    SELECT 10000+row_number() OVER(ORDER BY conname),
      'CONSTRAINT '||quote_ident(conname)||' '||pg_get_constraintdef(oid)
    FROM pg_constraint WHERE conrelid=table_name::regclass
  ), triggers AS (
    SELECT tgname,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid) AS sql
    FROM pg_trigger WHERE tgrelid=table_name::regclass AND NOT tgisinternal
  )
  INSERT INTO tenant_deletion_table_manifest(
    source_table,disposition,retention_class,table_sql_sha256,column_signature_sha256,
    trigger_set_sha256,catalog_revision,created_at)
  SELECT table_name,'archive_then_delete','billing',
    metahub_sha256('CREATE TABLE '||table_name||' ('
      ||(SELECT string_agg(sql,',' ORDER BY ordinal) FROM definitions)||')'),
    metahub_sha256((SELECT string_agg(attnum::text||chr(31)||attname||chr(31)||type_name
      ||chr(31)||attnotnull::text||chr(31)||coalesce(default_sql,''),chr(30) ORDER BY attnum) FROM columns)),
    metahub_sha256(coalesce((SELECT string_agg(tgname||chr(31)||metahub_sha256(sql),chr(30) ORDER BY tgname) FROM triggers),'')),
    1,CURRENT_TIMESTAMP;
  INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
  SELECT tgname,table_name,pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid),
    metahub_sha256(pg_get_triggerdef(oid)||';'||chr(10)||pg_get_functiondef(tgfoid))
  FROM pg_trigger WHERE tgrelid=table_name::regclass AND NOT tgisinternal;
END $$;
