-- PRD-063 down：只有全部租户仍仅有 0055 bootstrap 且没有 Adapter 订阅/投递事实时允许回滚。
-- 任一真实发布、同版本幂等收据、结构漂移或后续迁移都会在 DDL 前阻断，保留全部证据。

CREATE TEMP TABLE prd063_down_business_guard (
  invalid_count INTEGER NOT NULL CHECK (invalid_count=0)
);
CREATE TEMP TRIGGER trg_prd063_down_business_guard
BEFORE INSERT ON prd063_down_business_guard FOR EACH ROW WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd063_down_business_facts_present'); END;
-- 聚合真实发布、幂等收据、订阅和投递事实；任一存在都必须在 DDL 前阻断回滚。
INSERT INTO prd063_down_business_guard(invalid_count)
SELECT
  (SELECT COUNT(*) FROM provider_adapter_catalog_activations activation
   WHERE activation.catalog_version!='adapter-spec-v1'
      OR activation.catalog_hash!='9757f206d4bd0ec625d39173d4c5e5bd1f6d36132b3117008ffb1e5e7ba69870'
      OR activation.spec_count!=9 OR activation.status!='active' OR activation.changed!=0
      OR activation.activated_by!='adapter-spec-bootstrap'
      OR activation.superseded_at IS NOT NULL)
  + ABS(
      (SELECT COUNT(*) FROM provider_adapter_catalog_activations)
      - (SELECT COUNT(*) FROM tenants)
    )
  + (SELECT COUNT(*) FROM tenants tenant
     WHERE (SELECT COUNT(*) FROM provider_adapter_catalog_activations activation
            WHERE activation.tenant_id=tenant.tenant_id)!=1)
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
     WHERE json_valid(endpoint.event_types_json)
       AND EXISTS (SELECT 1 FROM json_each(endpoint.event_types_json) item
                   WHERE item.value='adapter.spec_updated'))
  + (SELECT COUNT(*) FROM webhook_deliveries WHERE event_type='adapter.spec_updated')
  + (SELECT COUNT(*) FROM webhook_delivery_attempts attempt
     JOIN webhook_deliveries delivery
       ON delivery.tenant_id=attempt.tenant_id
      AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
     WHERE delivery.event_type='adapter.spec_updated')
  + (SELECT COUNT(*) FROM webhook_dead_letter_events WHERE event_type='adapter.spec_updated');
DROP TRIGGER trg_prd063_down_business_guard;
DROP TABLE prd063_down_business_guard;

CREATE TEMP TABLE prd063_down_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count=0)
);
-- 核对迁移栈顶及 0055 全部对象，结构漂移时不得尝试删除部分数据库对象。
INSERT INTO prd063_down_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
    WHERE version='0055' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations WHERE status='applied' AND version>'0055')
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='table' AND name='provider_adapter_catalog_activations')=1 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='index' AND name IN (
        'idx_provider_adapter_activations_active_prd063',
        'idx_provider_adapter_activations_catalog_prd063'
      ))=2 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name IN (
        'trg_provider_adapter_activations_owner_insert_prd063',
        'trg_provider_adapter_activations_immutable_update_prd063',
        'trg_provider_adapter_activations_immutable_delete_prd063',
        'trg_tenants_adapter_spec_initialize_prd063'
      ))=4 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema object
     WHERE object.sql IS NOT NULL
       AND instr(lower(object.sql),'provider_adapter_catalog_activations')>0
       AND object.name NOT IN (
         'provider_adapter_catalog_activations',
         'idx_provider_adapter_activations_active_prd063',
         'idx_provider_adapter_activations_catalog_prd063',
         'trg_provider_adapter_activations_owner_insert_prd063',
         'trg_provider_adapter_activations_immutable_update_prd063',
         'trg_provider_adapter_activations_immutable_delete_prd063',
         'trg_tenants_adapter_spec_initialize_prd063'
       ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd063_down_guard;

DROP TRIGGER trg_tenants_adapter_spec_initialize_prd063;
DROP TRIGGER trg_provider_adapter_activations_immutable_delete_prd063;
DROP TRIGGER trg_provider_adapter_activations_immutable_update_prd063;
DROP TRIGGER trg_provider_adapter_activations_owner_insert_prd063;
DROP INDEX idx_provider_adapter_activations_catalog_prd063;
DROP INDEX idx_provider_adapter_activations_active_prd063;
DROP TABLE provider_adapter_catalog_activations;
