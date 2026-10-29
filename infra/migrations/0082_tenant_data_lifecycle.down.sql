-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- A lifecycle fact is irreversible.  Refuse rollback rather than pretending a deleted
-- or encrypted tenant record can be restored by DDL alone.
CREATE TEMP TABLE prd089_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd089_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied' ORDER BY version DESC LIMIT 1)='0082' THEN 0 ELSE 1 END
 + (SELECT COUNT(*) FROM tenant_export_jobs)+(SELECT COUNT(*) FROM deletion_requests)
 + (SELECT COUNT(*) FROM tenant_retention_archives)+(SELECT COUNT(*) FROM tenant_lifecycle_events)
 + (SELECT COUNT(*) FROM tenant_lifecycle_event_outbox)
 + (SELECT COUNT(*) FROM tenant_data_states WHERE lifecycle_status!='active' OR revision!=1 OR cache_generation!=0 OR active_deletion_request_id IS NOT NULL)
 + (SELECT COUNT(*) FROM role_permissions relation JOIN permissions permission ON permission.tenant_id=relation.tenant_id AND permission.permission_id=relation.permission_id JOIN roles role ON role.tenant_id=relation.tenant_id AND role.role_id=relation.role_id WHERE permission.code LIKE 'tenant_data.%' AND role.matrix_sha256 NOT IN('d64ef0db03b06138b5a5b5d4652ccdfcad2c79b420b3ce04fd5772eeebbf0ab1','f0f642cea1be202d82016ac045332c729acd04060be8e5de68b32a597d57716b'))
 + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=102 AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=259 THEN 0 ELSE 1 END
 + (SELECT COUNT(*) FROM pragma_foreign_key_check)+(SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd089_down_guard;

DROP TRIGGER trg_tenants_tenant_data_initialize_prd089;
DROP TRIGGER trg_tenant_export_jobs_owner_prd089;
DROP TRIGGER trg_tenant_export_jobs_transition_prd089;
DROP TRIGGER trg_tenant_export_jobs_delete_prd089;
DROP TRIGGER trg_deletion_requests_owner_prd089;
DROP TRIGGER trg_deletion_requests_transition_prd089;
DROP TRIGGER trg_deletion_requests_delete_prd089;
DROP TRIGGER trg_tenant_data_states_transition_prd089;
DROP TRIGGER trg_tenant_data_states_delete_prd089;
DROP TRIGGER trg_tenant_retention_archives_update_prd089;
DROP TRIGGER trg_tenant_retention_archives_delete_prd089;
DROP TRIGGER trg_tenant_lifecycle_events_update_prd089;
DROP TRIGGER trg_tenant_lifecycle_events_delete_prd089;
DROP TRIGGER trg_tenant_lifecycle_outbox_transition_prd089;
DROP TRIGGER trg_tenant_lifecycle_outbox_delete_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;

-- Contract audit enums before rebuilding the RBAC projection.
PRAGMA writable_schema=ON;
UPDATE sqlite_master SET sql=replace(replace(sql,
  '''branding.updated'',''tenant.export_requested'',''tenant.exported'',''tenant.deletion_requested'',''tenant.deleted''',
  '''branding.updated'''),
  '''branding_config'',''tenant_export_job'',''deletion_request''','''branding_config''')
WHERE type='table' AND name='audit_logs';
PRAGMA schema_version=1;
PRAGMA writable_schema=OFF;

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

DELETE FROM role_permissions WHERE permission_id IN(SELECT permission_id FROM permissions WHERE code LIKE 'tenant_data.%');
DELETE FROM permissions WHERE code LIKE 'tenant_data.%';
DELETE FROM rbac_role_permission_templates WHERE permission_code LIKE 'tenant_data.%';
CREATE TABLE rbac_permission_templates_prd089_old(
  permission_code TEXT PRIMARY KEY CHECK(length(permission_code) BETWEEN 3 AND 80 AND permission_code=lower(permission_code) AND permission_code NOT GLOB '*[^a-z0-9._]*'),
  category TEXT NOT NULL CHECK(length(category) BETWEEN 2 AND 40), name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK(length(description) BETWEEN 1 AND 300), risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=23));
INSERT INTO rbac_permission_templates_prd089_old(rowid,permission_code,category,name,description,risk_level,catalog_revision)
SELECT rowid,permission_code,category,name,description,risk_level,23 FROM rbac_permission_templates WHERE permission_code NOT LIKE 'tenant_data.%';
DROP TABLE rbac_permission_templates;
ALTER TABLE rbac_permission_templates_prd089_old RENAME TO rbac_permission_templates;
CREATE TABLE permissions_prd089_old(
  permission_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, code TEXT NOT NULL, category TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK(risk_level IN('read','write','critical')), catalog_revision INTEGER NOT NULL CHECK(catalog_revision=23), status TEXT NOT NULL CHECK(status='active'),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id), FOREIGN KEY(code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE(tenant_id,code), UNIQUE(tenant_id,permission_id));
INSERT INTO permissions_prd089_old(rowid,permission_id,tenant_id,code,category,name,description,risk_level,catalog_revision,status,created_at,updated_at)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,23,status,created_at,updated_at FROM permissions;
DROP TABLE permissions;
ALTER TABLE permissions_prd089_old RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);
UPDATE roles SET permission_revision=permission_revision+1,matrix_sha256=CASE code
  WHEN 'admin' THEN 'b9036b7e02f9f5d7dc282bb87bdd5f6312eab239d4159aca53ae34e82e90ff5e'
  WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686'
  WHEN 'viewer' THEN 'db7c917b4e7232d2cc24af672ffeb80ac375bc72ee9efa4eecb544c08139f94e'
  WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc'
  WHEN 'security_admin' THEN 'eba36b2011e994eda4d1f30368b5c53c580b025f28c3a3dee25e564a4a586377'
  WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END
WHERE matrix_sha256 IN('d64ef0db03b06138b5a5b5d4652ccdfcad2c79b420b3ce04fd5772eeebbf0ab1','888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686','db7c917b4e7232d2cc24af672ffeb80ac375bc72ee9efa4eecb544c08139f94e','843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc','f0f642cea1be202d82016ac045332c729acd04060be8e5de68b32a597d57716b','aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1');

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
CREATE TRIGGER trg_roles_owner_insert_prd059 BEFORE INSERT ON roles FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id) OR NEW.code NOT IN('admin','developer','viewer','finance_admin','security_admin','ops_admin') OR NEW.permission_revision!=1 OR NEW.matrix_sha256!=CASE NEW.code WHEN 'admin' THEN 'b9036b7e02f9f5d7dc282bb87bdd5f6312eab239d4159aca53ae34e82e90ff5e' WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686' WHEN 'viewer' THEN 'db7c917b4e7232d2cc24af672ffeb80ac375bc72ee9efa4eecb544c08139f94e' WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc' WHEN 'security_admin' THEN 'eba36b2011e994eda4d1f30368b5c53c580b025f28c3a3dee25e564a4a586377' WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_matrix_update_prd059 BEFORE UPDATE ON roles FOR EACH ROW WHEN OLD.code='admin' OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code OR NEW.name!=OLD.name OR NEW.description!=OLD.description OR NEW.status!=OLD.status OR NEW.created_at!=OLD.created_at OR NEW.is_permission_immutable!=OLD.is_permission_immutable OR NEW.permission_revision!=OLD.permission_revision+1 OR NEW.matrix_sha256=OLD.matrix_sha256 OR length(NEW.matrix_sha256)!=64 OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*' BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_owner_insert_prd059 BEFORE INSERT ON role_permissions FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM roles r JOIN permissions p ON p.tenant_id=r.tenant_id JOIN rbac_role_permission_templates t ON t.role_code=r.code AND t.permission_code=p.code WHERE r.tenant_id=NEW.tenant_id AND r.role_id=NEW.role_id AND r.status='active' AND p.permission_id=NEW.permission_id AND p.status='active') OR EXISTS(SELECT 1 FROM role_permissions x WHERE x.role_permission_id=NEW.role_permission_id OR(x.tenant_id=NEW.tenant_id AND x.role_id=NEW.role_id AND x.permission_id=NEW.permission_id) OR(NEW.rowid!=-1 AND x.rowid=NEW.rowid)) BEGIN SELECT RAISE(ABORT,'rbac_role_permission_owner_mismatch'); END;
CREATE TRIGGER trg_role_permissions_delete_prd059 BEFORE DELETE ON role_permissions FOR EACH ROW WHEN EXISTS(SELECT 1 FROM roles r WHERE r.tenant_id=OLD.tenant_id AND r.role_id=OLD.role_id AND r.code='admin') BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;
CREATE TRIGGER trg_tenants_rbac_initialize_prd059 AFTER INSERT ON tenants FOR EACH ROW BEGIN
  INSERT INTO roles(role_id,tenant_id,code,name,description,status,created_at,updated_at,permission_revision,matrix_sha256,is_permission_immutable)
  SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,c.role_code,c.role_code,'fixed role','active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,CASE c.role_code WHEN 'admin' THEN 'b9036b7e02f9f5d7dc282bb87bdd5f6312eab239d4159aca53ae34e82e90ff5e' WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686' WHEN 'viewer' THEN 'db7c917b4e7232d2cc24af672ffeb80ac375bc72ee9efa4eecb544c08139f94e' WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc' WHEN 'security_admin' THEN 'eba36b2011e994eda4d1f30368b5c53c580b025f28c3a3dee25e564a4a586377' WHEN 'ops_admin' THEN 'aec5571b1da9a790aefe5c68eec85af47d40707672baedf58baa7588a28a54e1' END,CASE WHEN c.role_code='admin' THEN 1 ELSE 0 END FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates)c;
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,t.permission_code,t.category,t.name,t.description,t.risk_level,23,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM rbac_permission_templates t;
  INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,r.role_id,p.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM roles r JOIN rbac_role_permission_templates t ON t.role_code=r.code JOIN permissions p ON p.tenant_id=r.tenant_id AND p.code=t.permission_code WHERE r.tenant_id=NEW.tenant_id;
END;

DROP TABLE tenant_lifecycle_event_outbox;
DROP TABLE tenant_lifecycle_events;
DROP TABLE tenant_retention_archives;
DROP TABLE tenant_data_states;
DROP TABLE deletion_requests;
DROP TABLE tenant_export_jobs;
DROP TABLE tenant_deletion_trigger_manifest;
DROP TABLE tenant_deletion_table_manifest;

CREATE TEMP TABLE prd089_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd089_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=98 AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=254
    AND NOT EXISTS(SELECT 1 FROM rbac_permission_templates WHERE catalog_revision!=23)
    AND instr((SELECT sql FROM sqlite_schema WHERE type='table' AND name='audit_logs'),'''tenant.deletion_requested''')=0
    AND (SELECT COUNT(*) FROM pragma_foreign_key_check)=0
    AND (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check='ok')=1 THEN 0 ELSE 1 END;
DROP TABLE prd089_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
