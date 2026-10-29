-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 只允许在没有 PRD-081 业务事实、CEF 配置、非默认限流或标准事件订阅时回退。
DROP TABLE IF EXISTS temp.prd081_down_guard;
CREATE TEMP TABLE prd081_down_guard(issue_count INTEGER NOT NULL);
CREATE TEMP TRIGGER prd081_down_guard_abort
BEFORE INSERT ON prd081_down_guard FOR EACH ROW WHEN NEW.issue_count!=0
BEGIN SELECT RAISE(ABORT,'prd081_down_guard_failed'); END;
INSERT INTO prd081_down_guard(issue_count)
SELECT
  CASE WHEN (SELECT version FROM schema_migrations
      WHERE status='applied' ORDER BY version DESC LIMIT 1)='0074'
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM provider_key_anomaly_events)
  + (SELECT COUNT(*) FROM security_event_sources)
  + (SELECT COUNT(*) FROM security_event_aggregate_members)
  + (SELECT COUNT(*) FROM security_event_aggregates)
  + (SELECT COUNT(*) FROM security_event_outbox)
  + (SELECT COUNT(*) FROM webhook_security_rate_claims)
  + (SELECT COUNT(*) FROM webhook_endpoints
      WHERE payload_format!='json' OR security_event_rate_limit_per_minute!=60
        OR instr(event_types_json,'"security.event_pushed"')>0)
  + (SELECT COUNT(*) FROM webhook_deliveries
      WHERE payload_format!='json' OR wire_body_text!=payload_json
        OR security_event_rate_limit_per_minute!=60
        OR event_type='security.event_pushed')
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('webhook_endpoints')
      WHERE name IN('payload_format','security_event_rate_limit_per_minute'))=2
      AND (SELECT COUNT(*) FROM pragma_table_info('webhook_deliveries')
      WHERE name IN('payload_format','wire_body_text',
        'security_event_rate_limit_per_minute'))=3
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
        'provider_key_anomaly_events','security_event_sources',
        'security_event_aggregates','security_event_aggregate_members',
        'security_event_outbox','webhook_security_rate_claims'))=6
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER prd081_down_guard_abort;
DROP TABLE prd081_down_guard;

-- 先移除所有引用新增表/列的 producer 与不可变门禁，再按外键依赖逆序删除事实表。
DROP TRIGGER trg_webhook_security_rate_claim_delete_prd081;
DROP TRIGGER trg_webhook_security_rate_claim_update_prd081;
DROP TRIGGER trg_webhook_security_rate_claim_owner_prd081;
DROP TRIGGER trg_provider_key_anomaly_security_source_prd081;
DROP TRIGGER trg_provider_error_facts_security_source_prd081;
DROP TRIGGER trg_session_risk_events_security_source_prd081;
DROP TRIGGER trg_dlp_matches_security_source_prd081;
DROP TRIGGER trg_security_event_sources_aggregate_prd081;
DROP TRIGGER trg_security_event_outbox_delete_prd081;
DROP TRIGGER trg_security_event_outbox_identity_update_prd081;
DROP TRIGGER trg_security_event_outbox_owner_insert_prd081;
DROP TRIGGER trg_security_event_aggregates_delete_prd081;
DROP TRIGGER trg_security_event_aggregates_recompute_prd081;
DROP TRIGGER trg_security_event_members_delete_prd081;
DROP TRIGGER trg_security_event_members_update_prd081;
DROP TRIGGER trg_security_event_members_owner_insert_prd081;
DROP TRIGGER trg_security_event_aggregates_owner_insert_prd081;
DROP TRIGGER trg_security_event_sources_delete_prd081;
DROP TRIGGER trg_security_event_sources_update_prd081;
DROP TRIGGER trg_security_event_sources_owner_insert_prd081;
DROP TRIGGER trg_provider_key_anomaly_delete_prd081;
DROP TRIGGER trg_provider_key_anomaly_update_prd081;
DROP TRIGGER trg_provider_key_anomaly_owner_insert_prd081;
DROP TRIGGER trg_webhook_deliveries_siem_immutable_prd081;
DROP TRIGGER trg_webhook_deliveries_siem_legacy_fill_prd081;
DROP TRIGGER trg_webhook_deliveries_siem_insert_prd081;
DROP TRIGGER trg_webhook_endpoints_siem_update_prd081;
DROP TRIGGER trg_webhook_endpoints_siem_insert_prd081;

DROP TABLE webhook_security_rate_claims;
DROP TABLE security_event_outbox;
DROP TABLE security_event_aggregate_members;
DROP TABLE security_event_aggregates;
DROP TABLE security_event_sources;
DROP TABLE provider_key_anomaly_events;

-- 所有历史 delivery 已验证为 JSON 且 wire 等于逻辑正文，因此移除增量列不会改变旧事实。
ALTER TABLE webhook_deliveries DROP COLUMN security_event_rate_limit_per_minute;
ALTER TABLE webhook_deliveries DROP COLUMN wire_body_text;
ALTER TABLE webhook_deliveries DROP COLUMN payload_format;
ALTER TABLE webhook_endpoints DROP COLUMN security_event_rate_limit_per_minute;
ALTER TABLE webhook_endpoints DROP COLUMN payload_format;

COMMIT;
PRAGMA foreign_keys = ON;
