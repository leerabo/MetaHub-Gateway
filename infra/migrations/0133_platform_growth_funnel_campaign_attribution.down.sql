-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd141_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd141_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
      ORDER BY version DESC LIMIT 1)='0133' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM platform_growth_event_eligibility)
  + (SELECT COUNT(*) FROM platform_growth_legacy_reconciliations)
  + (SELECT COUNT(*) FROM platform_growth_legacy_mappings)
  + (SELECT COUNT(*) FROM platform_acquisition_sources)
  + (SELECT COUNT(*) FROM platform_growth_funnel_snapshots)
  + (SELECT COUNT(*) FROM platform_growth_cohort_snapshots)
  + (SELECT COUNT(*) FROM platform_growth_campaigns)
  + (SELECT COUNT(*) FROM platform_growth_experiments)
  + (SELECT COUNT(*) FROM platform_growth_experiment_assignments)
  + (SELECT COUNT(*) FROM platform_conversion_attributions)
  + (SELECT COUNT(*) FROM platform_growth_exports)
  + (SELECT COUNT(*) FROM platform_growth_export_subjects)
  + (SELECT COUNT(*) FROM platform_growth_export_approvals)
  + (SELECT COUNT(*) FROM platform_growth_export_receipts)
  + (SELECT COUNT(*) FROM platform_growth_commands)
  + (SELECT COUNT(*) FROM platform_growth_events)
  + (SELECT COUNT(*) FROM platform_growth_event_outbox)
  + CASE WHEN (SELECT COUNT(*) FROM platform_growth_migration_cursors)=5
      AND NOT EXISTS(SELECT 1 FROM platform_growth_migration_cursors
        WHERE source_row_count!=0 OR target_row_count!=0 OR pending_count!=0
          OR leased_count!=0 OR sent_count!=0 OR failed_count!=0
          OR source_watermark IS NOT NULL OR source_sha256!=metahub_sha256('')
          OR generation!=0)
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_growth_scheduler_state)=9
      AND NOT EXISTS(SELECT 1 FROM platform_growth_scheduler_state
        WHERE status!='idle' OR generation!=0 OR lease_owner_id IS NOT NULL
          OR lease_token_sha256 IS NOT NULL OR lease_expires_at IS NOT NULL
          OR cursor_json IS NOT NULL OR source_watermark IS NOT NULL
          OR consecutive_failure_count!=0 OR last_error_code IS NOT NULL)
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_growth_permission_extensions)=8
      AND (SELECT COUNT(*) FROM platform_growth_role_permissions)=17
      AND (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd141)=6
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd141_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd141_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd141_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd141_delete;
CREATE TEMP TABLE prd141_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd141;
DROP TABLE tenant_deletion_manifest_extensions_prd141;
DELETE FROM tenant_deletion_trigger_manifest;
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd141_manifest_sources);
DROP TABLE prd141_manifest_sources;

DROP TABLE platform_growth_event_outbox;
DROP TABLE platform_growth_events;
DROP TABLE platform_growth_export_receipts;
DROP TABLE platform_growth_export_approvals;
DROP TABLE platform_growth_export_subjects;
DROP TABLE platform_growth_exports;
DROP TABLE platform_growth_commands;
DROP TABLE platform_conversion_attributions;
DROP TABLE platform_growth_experiment_assignments;
DROP TABLE platform_growth_experiments;
DROP TABLE platform_growth_campaigns;
DROP TABLE platform_growth_cohort_snapshots;
DROP TABLE platform_growth_funnel_snapshots;
DROP TABLE platform_acquisition_sources;
DROP TABLE platform_growth_legacy_mappings;
DROP TABLE platform_growth_legacy_reconciliations;
DROP TABLE platform_growth_event_eligibility;
DROP TABLE platform_growth_scheduler_state;
DROP TABLE platform_growth_migration_cursors;
DROP TABLE platform_growth_role_permissions;
DROP TABLE platform_growth_permission_extensions;

-- Restore the exact PRD-085 Workspace contract for a schema-only development rollback.
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
INSERT INTO rbac_permission_templates(
  permission_code,category,name,description,risk_level,catalog_revision)
VALUES('product_analytics.read','analytics','读取产品激活漏斗',
  '读取当前租户产品使用行为聚合与七阶段激活漏斗。','read',27);
INSERT INTO rbac_role_permission_templates(role_code,permission_code,default_granted)
VALUES('admin','product_analytics.read',1),('ops_admin','product_analytics.read',1);
INSERT INTO permissions(
  permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at)
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
  template.category,template.name,template.description,template.risk_level,27,'active',
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant JOIN rbac_permission_templates template
  ON template.permission_code='product_analytics.read';
INSERT INTO role_permissions(
  role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,
  permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
  strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM roles role JOIN permissions permission ON permission.tenant_id=role.tenant_id
WHERE role.code IN('admin','ops_admin') AND permission.code='product_analytics.read';
UPDATE roles
SET permission_revision=permission_revision+1,
    matrix_sha256=metahub_sha256(coalesce((
      SELECT json_group_array(code) FROM(
        SELECT permission.code AS code
        FROM role_permissions role_permission
        JOIN permissions permission ON permission.permission_id=role_permission.permission_id
        WHERE role_permission.tenant_id=roles.tenant_id
          AND role_permission.role_id=roles.role_id
        ORDER BY permission.code)), '[]'))
WHERE code IN('admin','ops_admin');

CREATE TRIGGER trg_rbac_permission_templates_insert_prd059 BEFORE INSERT ON rbac_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_permission_templates_update_prd059 BEFORE UPDATE ON rbac_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_permission_templates_delete_prd059 BEFORE DELETE ON rbac_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_insert_prd059 BEFORE INSERT ON rbac_role_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_update_prd059 BEFORE UPDATE ON rbac_role_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_rbac_role_templates_delete_prd059 BEFORE DELETE ON rbac_role_permission_templates FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_template_immutable'); END;
CREATE TRIGGER trg_permissions_owner_insert_prd059 BEFORE INSERT ON permissions FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM rbac_permission_templates t WHERE t.permission_code=NEW.code AND t.category=NEW.category AND t.name=NEW.name AND t.description=NEW.description AND t.risk_level=NEW.risk_level AND t.catalog_revision=NEW.catalog_revision) OR NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id) OR NEW.status!='active' OR EXISTS(SELECT 1 FROM permissions p WHERE p.permission_id=NEW.permission_id OR(p.tenant_id=NEW.tenant_id AND p.code=NEW.code) OR(NEW.rowid!=-1 AND p.rowid=NEW.rowid)) BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_update_prd059 BEFORE UPDATE ON permissions FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_delete_prd059 BEFORE DELETE ON permissions FOR EACH ROW BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_permissions_rowid_insert_prd059 AFTER INSERT ON permissions FOR EACH ROW WHEN NEW.rowid<0 BEGIN SELECT RAISE(ABORT,'rbac_permission_immutable'); END;
CREATE TRIGGER trg_roles_owner_insert_prd059 BEFORE INSERT ON roles FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id) OR NEW.code NOT IN('admin','developer','viewer','finance_admin','security_admin','ops_admin') OR NEW.permission_revision!=1 OR NEW.matrix_sha256!=CASE NEW.code WHEN 'admin' THEN '9af89117d6e3eb365c9c90f61778d0fe7408d03a50a6cd32a0d6eb4e61e772fb' WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686' WHEN 'viewer' THEN 'be55018029cd884b139e7e919a0f7052a94c8ca1ae5120fcb499e1b8754837af' WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc' WHEN 'security_admin' THEN '7bece4007f6bb8affcfa72d1db73ddc12b71d4bb85f874ad440736e63e596dbb' WHEN 'ops_admin' THEN '71c49cf7d47d0a5092eb50c3727cb93a1409f2f1fe3359d47e477c2f6eefdc6b' END OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_matrix_update_prd059 BEFORE UPDATE ON roles FOR EACH ROW WHEN OLD.code='admin' OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code OR NEW.name!=OLD.name OR NEW.description!=OLD.description OR NEW.status!=OLD.status OR NEW.created_at!=OLD.created_at OR NEW.is_permission_immutable!=OLD.is_permission_immutable OR NEW.permission_revision!=OLD.permission_revision+1 OR NEW.matrix_sha256=OLD.matrix_sha256 OR length(NEW.matrix_sha256)!=64 OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*' BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_owner_insert_prd059 BEFORE INSERT ON role_permissions FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM roles r JOIN permissions p ON p.tenant_id=r.tenant_id JOIN rbac_role_permission_templates t ON t.role_code=r.code AND t.permission_code=p.code WHERE r.tenant_id=NEW.tenant_id AND r.role_id=NEW.role_id AND r.status='active' AND p.permission_id=NEW.permission_id AND p.status='active') OR EXISTS(SELECT 1 FROM role_permissions x WHERE x.role_permission_id=NEW.role_permission_id OR(x.tenant_id=NEW.tenant_id AND x.role_id=NEW.role_id AND x.permission_id=NEW.permission_id) OR(NEW.rowid!=-1 AND x.rowid=NEW.rowid)) BEGIN SELECT RAISE(ABORT,'rbac_role_permission_owner_mismatch'); END;
CREATE TRIGGER trg_role_permissions_delete_prd059 BEFORE DELETE ON role_permissions FOR EACH ROW WHEN EXISTS(SELECT 1 FROM roles r WHERE r.tenant_id=OLD.tenant_id AND r.role_id=OLD.role_id AND r.code='admin') BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;
CREATE TRIGGER trg_tenants_rbac_initialize_prd059 AFTER INSERT ON tenants FOR EACH ROW BEGIN
  INSERT INTO roles(role_id,tenant_id,code,name,description,status,created_at,updated_at,permission_revision,matrix_sha256,is_permission_immutable)
  SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,c.role_code,c.role_code,'PRD-140 fixed role','active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,CASE c.role_code WHEN 'admin' THEN '9af89117d6e3eb365c9c90f61778d0fe7408d03a50a6cd32a0d6eb4e61e772fb' WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686' WHEN 'viewer' THEN 'be55018029cd884b139e7e919a0f7052a94c8ca1ae5120fcb499e1b8754837af' WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc' WHEN 'security_admin' THEN '7bece4007f6bb8affcfa72d1db73ddc12b71d4bb85f874ad440736e63e596dbb' WHEN 'ops_admin' THEN '71c49cf7d47d0a5092eb50c3727cb93a1409f2f1fe3359d47e477c2f6eefdc6b' END,CASE WHEN c.role_code='admin' THEN 1 ELSE 0 END FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates)c;
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,t.permission_code,t.category,t.name,t.description,t.risk_level,27,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM rbac_permission_templates t;
  INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,r.role_id,p.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM roles r JOIN rbac_role_permission_templates t ON t.role_code=r.code JOIN permissions p ON p.tenant_id=r.tenant_id AND p.code=t.permission_code WHERE r.tenant_id=NEW.tenant_id;
END;

INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
SELECT trigger_record.name,trigger_record.tbl_name,trigger_record.sql,
  metahub_sha256(trigger_record.sql)
FROM sqlite_schema trigger_record
JOIN tenant_deletion_table_manifest manifest ON manifest.source_table=trigger_record.tbl_name
WHERE trigger_record.type='trigger' ORDER BY trigger_record.tbl_name,trigger_record.name;
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_table_manifest FOR EACH ROW BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_table_manifest_delete_prd089 BEFORE DELETE ON tenant_deletion_table_manifest FOR EACH ROW BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089 BEFORE UPDATE ON tenant_deletion_trigger_manifest FOR EACH ROW BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089 BEFORE DELETE ON tenant_deletion_trigger_manifest FOR EACH ROW BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd141_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd141_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE name LIKE '%_prd141'
        OR name IN('platform_growth_permission_extensions','platform_growth_role_permissions',
          'platform_growth_event_eligibility','platform_growth_legacy_reconciliations',
          'platform_growth_legacy_mappings','platform_acquisition_sources',
          'platform_growth_funnel_snapshots','platform_growth_cohort_snapshots',
          'platform_growth_campaigns','platform_growth_experiments',
          'platform_growth_experiment_assignments','platform_conversion_attributions',
          'platform_growth_exports','platform_growth_export_subjects',
          'platform_growth_export_approvals','platform_growth_export_receipts',
          'platform_growth_commands','platform_growth_events',
          'platform_growth_event_outbox','platform_growth_migration_cursors',
          'platform_growth_scheduler_state'))=0 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates
        WHERE permission_code='product_analytics.read')=1
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates
        WHERE permission_code='product_analytics.read')=2
      AND (SELECT COUNT(*) FROM permissions WHERE code='product_analytics.read')=
        (SELECT COUNT(*) FROM tenants)
    THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd141_down_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
