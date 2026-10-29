-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd142_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd142_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0134' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM channel_brands)
  + (SELECT COUNT(*) FROM deployment_brands)
  + (SELECT COUNT(*) FROM platform_channel_partner_scopes)
  + (SELECT COUNT(*) FROM brand_assets)
  + (SELECT COUNT(*) FROM brand_verified_domains)
  + (SELECT COUNT(*) FROM brand_domain_observations)
  + CASE WHEN (SELECT COUNT(*) FROM brand_releases)=1
      AND (SELECT COUNT(*) FROM brand_releases release
        JOIN platform_brands brand ON brand.platform_brand_id=release.platform_brand_id
        WHERE release.owner_kind='platform' AND release.generation=1
          AND release.status='released' AND release.channel_brand_id IS NULL
          AND release.deployment_brand_id IS NULL AND release.logo_asset_id IS NULL
          AND release.favicon_asset_id IS NULL AND release.verified_domain_id IS NULL
          AND release.product_name=brand.product_name
          AND release.primary_color=brand.primary_color
          AND release.secondary_color=brand.secondary_color
          AND release.login_title=brand.login_title
          AND release.login_message=brand.login_message)=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM brand_public_heads)
  + (SELECT COUNT(*) FROM deployment_brand_bundles)
  + (SELECT COUNT(*) FROM deployment_brand_acks)
  + (SELECT COUNT(*) FROM brand_commands)
  + (SELECT COUNT(*) FROM brand_events)
  + (SELECT COUNT(*) FROM brand_event_outbox)
  + (SELECT COUNT(*) FROM brand_legacy_object_mappings)
  + (SELECT COUNT(*) FROM brand_migration_cursors)
  + CASE WHEN (SELECT COUNT(*) FROM platform_brands
      WHERE singleton_id=1 AND status='released' AND product_name='MetaHub'
        AND primary_color='#176B87' AND secondary_color='#F59E0B'
        AND login_title='企业 AI Gateway' AND login_message='安全、可追踪的企业模型网关'
        AND draft_logo_asset_id IS NULL AND draft_favicon_asset_id IS NULL
        AND draft_domain_id IS NULL AND published_generation=1 AND lkg_generation=1
        AND revision=1)=1 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM brand_scheduler_state
      WHERE cursor_value IS NULL AND fence_generation=1 AND lease_owner_id IS NULL
        AND lease_token_hmac IS NULL AND lease_expires_at IS NULL)=7 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd142_down_guard;

DROP TRIGGER trg_brand_legacy_mapping_delete_prd142;
DROP TRIGGER trg_brand_legacy_mapping_update_prd142;
DROP TRIGGER trg_brand_commands_delete_prd142;
DROP TRIGGER trg_brand_commands_update_prd142;
DROP TRIGGER trg_brand_events_delete_prd142;
DROP TRIGGER trg_brand_events_update_prd142;
DROP TRIGGER trg_brand_releases_delete_prd142;
DROP TRIGGER trg_brand_releases_update_prd142;
DROP TRIGGER trg_brand_verified_domains_delete_prd142;
DROP TRIGGER trg_brand_verified_domains_owner_prd142;
DROP TRIGGER trg_brand_assets_delete_prd142;
DROP TRIGGER trg_brand_assets_owner_prd142;
DROP TRIGGER trg_deployment_brands_delete_prd142;
DROP TRIGGER trg_deployment_brands_owner_prd142;
DROP TRIGGER trg_channel_brands_delete_prd142;
DROP TRIGGER trg_channel_brands_owner_prd142;
DROP TRIGGER trg_platform_brands_delete_prd142;
DROP TRIGGER trg_platform_brands_owner_prd142;
DROP TRIGGER trg_branding_configs_cutover_delete_prd142;
DROP TRIGGER trg_branding_configs_cutover_update_prd142;
DROP TRIGGER trg_branding_configs_cutover_prd142;

DROP TABLE brand_scheduler_state;
DROP TABLE brand_migration_cursors;
DROP TABLE brand_legacy_object_mappings;
DROP TABLE brand_event_outbox;
DROP TABLE brand_events;
DROP TABLE brand_commands;
DROP TABLE deployment_brand_acks;
DROP TABLE deployment_brand_bundles;
DROP TABLE brand_public_heads;
DROP TABLE brand_releases;
DROP TABLE brand_domain_observations;
DROP TABLE brand_verified_domains;
DROP TABLE brand_assets;
DROP TABLE platform_channel_partner_scopes;
DROP TABLE deployment_brands;
DROP TABLE channel_brands;
DROP TABLE platform_brands;

DROP TRIGGER trg_workspace_brand_roles_delete_prd142;
DROP TRIGGER trg_workspace_brand_roles_update_prd142;
DROP TRIGGER trg_workspace_brand_roles_insert_prd142;
DROP TRIGGER trg_workspace_brand_permissions_delete_prd142;
DROP TRIGGER trg_workspace_brand_permissions_update_prd142;
DROP TRIGGER trg_workspace_brand_permissions_insert_prd142;
DROP TRIGGER trg_platform_brand_roles_delete_prd142;
DROP TRIGGER trg_platform_brand_roles_update_prd142;
DROP TRIGGER trg_platform_brand_roles_insert_prd142;
DROP TRIGGER trg_platform_brand_permissions_delete_prd142;
DROP TRIGGER trg_platform_brand_permissions_update_prd142;
DROP TRIGGER trg_platform_brand_permissions_insert_prd142;
DROP TABLE workspace_brand_role_permissions;
DROP TABLE workspace_brand_permission_extensions;
DROP TABLE platform_brand_role_permissions;
DROP TABLE platform_brand_permission_extensions;

-- Restore the PRD-088 catalog for an empty 0133 rollback only.
CREATE TEMP TABLE prd142_roles_to_expand(role_id TEXT PRIMARY KEY);
INSERT INTO prd142_roles_to_expand SELECT role_id FROM roles;
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
INSERT INTO rbac_permission_templates VALUES
  ('branding.read','branding','读取白标配置','读取当前租户控制台与登录页的安全白标配置和受限 Logo。','read',27),
  ('branding.manage','branding','管理白标配置','修改当前租户控制台与登录页的 Logo、名称、配色和文案。','critical',27);
INSERT INTO rbac_role_permission_templates
SELECT role_code,'branding.read',1 FROM (SELECT 'admin' role_code UNION ALL SELECT 'developer'
  UNION ALL SELECT 'viewer' UNION ALL SELECT 'finance_admin'
  UNION ALL SELECT 'security_admin' UNION ALL SELECT 'ops_admin');
INSERT INTO rbac_role_permission_templates VALUES('admin','branding.manage',1);
INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at)
SELECT 'perm_'||lower(hex(randomblob(16))),tenant.tenant_id,template.permission_code,
  template.category,template.name,template.description,template.risk_level,27,'active',
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant CROSS JOIN rbac_permission_templates template
WHERE template.permission_code IN('branding.read','branding.manage');
INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
SELECT 'rperm_'||lower(hex(randomblob(16))),role.tenant_id,role.role_id,permission.permission_id,
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM roles role
JOIN rbac_role_permission_templates template ON template.role_code=role.code
JOIN permissions permission ON permission.tenant_id=role.tenant_id
  AND permission.code=template.permission_code
WHERE template.permission_code IN('branding.read','branding.manage');
UPDATE roles
SET permission_revision=permission_revision+1,
    matrix_sha256=metahub_sha256(coalesce((
      SELECT json_group_array(code) FROM(
        SELECT permission.code AS code FROM role_permissions assignment
        JOIN permissions permission ON permission.permission_id=assignment.permission_id
        WHERE assignment.tenant_id=roles.tenant_id AND assignment.role_id=roles.role_id
        ORDER BY permission.code)), '[]'))
WHERE role_id IN(SELECT role_id FROM prd142_roles_to_expand);
DROP TABLE prd142_roles_to_expand;

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
CREATE TRIGGER trg_roles_owner_insert_prd059 BEFORE INSERT ON roles FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM tenants WHERE tenant_id=NEW.tenant_id) OR NEW.code NOT IN('admin','developer','viewer','finance_admin','security_admin','ops_admin') OR NEW.permission_revision!=1 OR NEW.matrix_sha256!=CASE NEW.code WHEN 'admin' THEN 'eb48d3c3fddfa76477ae5af3943449253af0581da445e3d5695f893258dba6b8' WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686' WHEN 'viewer' THEN 'be55018029cd884b139e7e919a0f7052a94c8ca1ae5120fcb499e1b8754837af' WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc' WHEN 'security_admin' THEN '7bece4007f6bb8affcfa72d1db73ddc12b71d4bb85f874ad440736e63e596dbb' WHEN 'ops_admin' THEN 'bdab5bcae4741701f7171d1f08624f71306963dea15e99ccbde55028df2e6426' END OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_matrix_update_prd059 BEFORE UPDATE ON roles FOR EACH ROW WHEN OLD.code='admin' OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id OR NEW.tenant_id!=OLD.tenant_id OR NEW.code!=OLD.code OR NEW.name!=OLD.name OR NEW.description!=OLD.description OR NEW.status!=OLD.status OR NEW.created_at!=OLD.created_at OR NEW.is_permission_immutable!=OLD.is_permission_immutable OR NEW.permission_revision!=OLD.permission_revision+1 OR NEW.matrix_sha256=OLD.matrix_sha256 OR length(NEW.matrix_sha256)!=64 OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*' BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_owner_insert_prd059 BEFORE INSERT ON role_permissions FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM roles r JOIN permissions p ON p.tenant_id=r.tenant_id JOIN rbac_role_permission_templates t ON t.role_code=r.code AND t.permission_code=p.code WHERE r.tenant_id=NEW.tenant_id AND r.role_id=NEW.role_id AND r.status='active' AND p.permission_id=NEW.permission_id AND p.status='active') OR EXISTS(SELECT 1 FROM role_permissions x WHERE x.role_permission_id=NEW.role_permission_id OR(x.tenant_id=NEW.tenant_id AND x.role_id=NEW.role_id AND x.permission_id=NEW.permission_id) OR(NEW.rowid!=-1 AND x.rowid=NEW.rowid)) BEGIN SELECT RAISE(ABORT,'rbac_role_permission_owner_mismatch'); END;
CREATE TRIGGER trg_role_permissions_delete_prd059 BEFORE DELETE ON role_permissions FOR EACH ROW WHEN EXISTS(SELECT 1 FROM roles r WHERE r.tenant_id=OLD.tenant_id AND r.role_id=OLD.role_id AND r.code='admin') BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;
CREATE TRIGGER trg_tenants_rbac_initialize_prd059 AFTER INSERT ON tenants FOR EACH ROW BEGIN
  INSERT INTO roles(role_id,tenant_id,code,name,description,status,created_at,updated_at,permission_revision,matrix_sha256,is_permission_immutable)
  SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,c.role_code,c.role_code,'PRD-141 fixed role','active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,CASE c.role_code WHEN 'admin' THEN 'eb48d3c3fddfa76477ae5af3943449253af0581da445e3d5695f893258dba6b8' WHEN 'developer' THEN '888796e8f30b282353452e012ba5132476a59ffb2509eded4cb44d8e8de5b686' WHEN 'viewer' THEN 'be55018029cd884b139e7e919a0f7052a94c8ca1ae5120fcb499e1b8754837af' WHEN 'finance_admin' THEN '843de9cd51726438ebf668ab413a126b31c429a26b7ee306a394bca4435764fc' WHEN 'security_admin' THEN '7bece4007f6bb8affcfa72d1db73ddc12b71d4bb85f874ad440736e63e596dbb' WHEN 'ops_admin' THEN 'bdab5bcae4741701f7171d1f08624f71306963dea15e99ccbde55028df2e6426' END,CASE WHEN c.role_code='admin' THEN 1 ELSE 0 END FROM(SELECT DISTINCT role_code FROM rbac_role_permission_templates)c;
  INSERT INTO permissions(permission_id,tenant_id,code,category,name,description,risk_level,catalog_revision,status,created_at,updated_at)
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,t.permission_code,t.category,t.name,t.description,t.risk_level,27,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM rbac_permission_templates t;
  INSERT INTO role_permissions(role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at)
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,r.role_id,p.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now') FROM roles r JOIN rbac_role_permission_templates t ON t.role_code=r.code JOIN permissions p ON p.tenant_id=r.tenant_id AND p.code=t.permission_code WHERE r.tenant_id=NEW.tenant_id;
END;

CREATE TRIGGER trg_tenants_branding_initialize_prd088
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  INSERT INTO branding_configs(
    branding_config_id,tenant_id,status,product_name,primary_color,secondary_color,
    login_title,login_message,custom_domain,custom_domain_status,logo_png,logo_sha256,
    logo_width,logo_height,revision,created_at,updated_at)
  VALUES('brc_'||lower(hex(randomblob(16))),NEW.tenant_id,'active','MetaHub','#176B87','#F59E0B',
    '企业 AI Gateway','安全、可追踪的企业模型网关',NULL,'not_configured',NULL,NULL,NULL,NULL,1,
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'));
END;

CREATE TEMP TABLE prd142_down_post_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd142_down_post_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok')
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates
      WHERE permission_code IN('branding.read','branding.manage'))=2 THEN 0 ELSE 1 END;
DROP TABLE prd142_down_post_guard;

COMMIT;
PRAGMA foreign_keys = ON;
