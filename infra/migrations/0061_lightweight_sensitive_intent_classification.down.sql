-- PRD-069 回滚：仅在没有意图安全、Webhook、RBAC 历史和后续 schema 依赖时恢复 0060。
-- 安全边界：所有门禁都位于首项持久 DDL 前；任何不可逆业务事实都原子拒绝，禁止静默抹除。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 只冻结仍精确等于 revision 6 产品矩阵的 Admin/SecurityAdmin，供安全删除新增关系。
CREATE TEMP TABLE prd069_roles_to_contract AS
SELECT role.role_id,role.tenant_id,role.code,role.permission_revision
FROM roles role
WHERE role.code IN ('admin','security_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '20d984052969d07a315b032df8a6351fca540117e3574e744f8c721a53c13760'
    WHEN 'security_admin' THEN 'eda14739c9562e962de0093999fc5f2fcea45b0eeca103a440d095cd1c5e6f94'
  END
  AND NOT EXISTS (
    SELECT 1 FROM rbac_role_permission_templates template
    WHERE template.role_code=role.code AND NOT EXISTS (
      SELECT 1 FROM role_permissions relation
      JOIN permissions permission
        ON permission.tenant_id=relation.tenant_id
       AND permission.permission_id=relation.permission_id
      WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
        AND permission.code=template.permission_code
    )
  )
  AND NOT EXISTS (
    SELECT 1 FROM role_permissions relation
    JOIN permissions permission
      ON permission.tenant_id=relation.tenant_id
     AND permission.permission_id=relation.permission_id
    WHERE relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
      AND NOT EXISTS (
        SELECT 1 FROM rbac_role_permission_templates template
        WHERE template.role_code=role.code AND template.permission_code=permission.code
      )
  );

-- 业务门禁使用 TEMP trigger 产生稳定错误码；结果、日志、订阅、投递或授权历史任一存在即拒绝。
DROP TABLE IF EXISTS temp.prd069_down_business_guard;
DROP TRIGGER IF EXISTS temp.trg_prd069_down_business_guard;
CREATE TEMP TABLE prd069_down_business_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd069_down_business_guard
BEFORE INSERT ON prd069_down_business_guard FOR EACH ROW
WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd069_down_business_facts_present'); END;
-- 聚合所有不可逆结果、日志、Webhook 和权限历史，任一事实存在都停止回滚。
INSERT INTO prd069_down_business_guard(invalid_count)
SELECT
  (SELECT COUNT(*) FROM intent_risk_results)
  + (SELECT COUNT(*) FROM intent_risk_events)
  + (SELECT COUNT(*) FROM request_logs
     WHERE intent_risk_schedule_status!='disabled'
        OR intent_risk_result_id IS NOT NULL OR intent_risk_mode IS NOT NULL
        OR intent_risk_type IS NOT NULL OR intent_risk_score IS NOT NULL
        OR intent_risk_action IS NOT NULL OR intent_risk_reason_code IS NOT NULL
        OR intent_risk_detector_version IS NOT NULL OR intent_risk_input_truncated!=0)
  -- Endpoint JSON 是用户主动订阅事实；损坏 JSON 仍以保守文本匹配失败关闭。
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
     WHERE (json_valid(endpoint.event_types_json) AND EXISTS (
       SELECT 1 FROM json_each(endpoint.event_types_json) item
       WHERE item.value='security.intent_detected'
     )) OR lower(endpoint.event_types_json) LIKE '%security.intent_detected%')
  + (SELECT COUNT(*) FROM webhook_deliveries
     WHERE event_type='security.intent_detected')
  + (SELECT COUNT(*) FROM webhook_delivery_attempts attempt
     JOIN webhook_deliveries delivery
       ON delivery.tenant_id=attempt.tenant_id
      AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
     WHERE delivery.event_type='security.intent_detected')
  + (SELECT COUNT(*) FROM webhook_dead_letter_events
     WHERE event_type='security.intent_detected')
  -- 权限变更历史不可通过删除当前关系掩盖；JSON 精确查找和文本兜底共同覆盖损坏记录。
  + (SELECT COUNT(*) FROM role_permission_events event
     WHERE EXISTS (
       SELECT 1 FROM json_each(event.added_permissions_json) item
       WHERE item.value='intent_risk.classify'
     ) OR EXISTS (
       SELECT 1 FROM json_each(event.removed_permissions_json) item
       WHERE item.value='intent_risk.classify'
     ) OR lower(event.added_permissions_json) LIKE '%intent_risk.classify%'
       OR lower(event.removed_permissions_json) LIKE '%intent_risk.classify%')
  -- 新权限只能由仍可证明 canonical 的两个角色持有；自定义角色持有时无法无损收缩目录。
  + (SELECT COUNT(*) FROM role_permissions relation
     JOIN permissions permission
       ON permission.tenant_id=relation.tenant_id
      AND permission.permission_id=relation.permission_id
     WHERE permission.code='intent_risk.classify'
       AND NOT EXISTS (
         SELECT 1 FROM prd069_roles_to_contract source
         WHERE source.tenant_id=relation.tenant_id AND source.role_id=relation.role_id
       ));
DROP TRIGGER trg_prd069_down_business_guard;
DROP TABLE prd069_down_business_guard;

-- 结构门禁冻结 0061 栈顶、目录计数、RequestLog 形状、目标表及所有未知后续引用。
DROP TABLE IF EXISTS temp.prd069_down_guard;
CREATE TEMP TABLE prd069_down_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd069_down_preflight_ok CHECK (issue_count=0)
);
-- 在持久对象变化前复核栈顶、精确结构、未知依赖和数据库完整性。
INSERT INTO prd069_down_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
      WHERE version='0061' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations WHERE status='applied' AND version>'0061')
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=57
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=6)=57
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=151
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*57 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=6)
          =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM roles role
     WHERE role.code='admin' AND NOT EXISTS (
       SELECT 1 FROM prd069_roles_to_contract source
       WHERE source.tenant_id=role.tenant_id AND source.role_id=role.role_id
     ))
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('request_logs'))=61
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='index'
           AND tbl_name='request_logs' AND name NOT LIKE 'sqlite_autoindex%')=24
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
           AND tbl_name='request_logs')=14 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('intent_risk_results'))=22
      AND (SELECT COUNT(*) FROM pragma_table_info('intent_risk_events'))=19
    THEN 0 ELSE 1 END
  -- 0061 自有对象允许引用新表/列；任何其他 view/index/trigger 都属于未知后续依赖。
  + (SELECT COUNT(*) FROM sqlite_schema object
     WHERE object.sql IS NOT NULL
       AND (instr(lower(object.sql),'intent_risk_results')>0
         OR instr(lower(object.sql),'intent_risk_events')>0
         OR instr(lower(object.sql),'intent_risk_result_id')>0
         OR instr(lower(object.sql),'intent_risk_schedule_status')>0
         OR instr(lower(object.sql),'intent_risk_mode')>0
         OR instr(lower(object.sql),'intent_risk_type')>0
         OR instr(lower(object.sql),'intent_risk_score')>0
         OR instr(lower(object.sql),'intent_risk_action')>0
         OR instr(lower(object.sql),'intent_risk_reason_code')>0
         OR instr(lower(object.sql),'intent_risk_detector_version')>0
         OR instr(lower(object.sql),'intent_risk_input_truncated')>0)
       AND object.name NOT IN ('intent_risk_results','intent_risk_events','request_logs')
       AND object.name NOT LIKE '%prd069')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd069_down_guard;

-- 首项持久 DDL：先移除 RequestLog 的 0061 组合 trigger，再删除仅由本迁移追加的九列。
-- SQLite 的 DROP COLUMN 内部执行原子换表；原表的 52 列、24 索引、12 trigger 均由最终 guard 逐项复核。
DROP TRIGGER trg_request_logs_intent_insert_prd069;
DROP TRIGGER trg_request_logs_intent_update_prd069;
-- 按追加顺序逆序移除截断标志，避免剩余组合约束引用已删除字段。
ALTER TABLE request_logs DROP COLUMN intent_risk_input_truncated;
-- detector 版本只属于 0061 调度证据，业务门禁已证明所有旧行为空。
ALTER TABLE request_logs DROP COLUMN intent_risk_detector_version;
-- 固定原因码只属于 0061，移除前已证明不存在安全事实。
ALTER TABLE request_logs DROP COLUMN intent_risk_reason_code;
-- 同步动作列只属于 0061，移除不会改写历史 DLP 或 Policy 动作。
ALTER TABLE request_logs DROP COLUMN intent_risk_action;
-- 风险分数列只属于 0061，独立结果表为空后才能安全移除。
ALTER TABLE request_logs DROP COLUMN intent_risk_score;
-- 风险类型列只属于 0061，不能被误当成既有 DLP 类型。
ALTER TABLE request_logs DROP COLUMN intent_risk_type;
-- 分类模式列只属于 0061，回滚后不向 0060 虚构调度语义。
ALTER TABLE request_logs DROP COLUMN intent_risk_mode;
-- 调度状态列最后于 identity 前移除，确保此前每一步仍能由门禁解释。
ALTER TABLE request_logs DROP COLUMN intent_risk_schedule_status;
-- 结果 identity 最后移除，完整恢复 0060 的 52 列 RequestLog 合同。
ALTER TABLE request_logs DROP COLUMN intent_risk_result_id;

-- 业务门禁已证明两表为空，按 child-to-parent 顺序移除 source event 与结果。
DROP TABLE intent_risk_events;
DROP TABLE intent_risk_results;

-- RBAC 收缩期间短暂移除不可变与 owner trigger；删除集合已证明只属于 canonical 产品矩阵。
DROP TRIGGER trg_tenants_rbac_initialize_prd059;
DROP TRIGGER trg_roles_owner_insert_prd059;
DROP TRIGGER trg_roles_matrix_update_prd059;
DROP TRIGGER trg_role_permissions_owner_insert_prd059;
DROP TRIGGER trg_role_permissions_delete_prd059;
DROP TRIGGER trg_rbac_role_templates_insert_prd059;
DROP TRIGGER trg_rbac_role_templates_update_prd059;
DROP TRIGGER trg_rbac_role_templates_delete_prd059;

-- 删除仅属于新权限的 canonical 关系；自定义持有者已被首项业务门禁拒绝。
DELETE FROM role_permissions
WHERE permission_id IN (
  SELECT permission_id FROM permissions WHERE code='intent_risk.classify'
);
-- 产品角色模板同步删除两条默认授权，恢复 0060 的 149 条关系模板。
DELETE FROM rbac_role_permission_templates
WHERE permission_code='intent_risk.classify';

-- 产品 Permission 模板恢复 revision 5 的 56 项，保留旧 rowid 和全部展示元数据。
CREATE TABLE rbac_permission_templates_prd069_old (
  permission_code TEXT PRIMARY KEY CHECK (
    length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'
  ),
  category TEXT NOT NULL CHECK (length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=5)
);
-- 旧 56 项保留 rowid 与展示元数据，仅把 catalog revision 恢复为 5。
INSERT INTO rbac_permission_templates_prd069_old(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,5
FROM rbac_permission_templates WHERE permission_code!='intent_risk.classify';
DROP TABLE rbac_permission_templates;
-- 完整复制后恢复正式目录名，再重新启用产品元数据不可变 trigger。
ALTER TABLE rbac_permission_templates_prd069_old RENAME TO rbac_permission_templates;
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

-- 租户 Permission 投影恢复 revision 5；除新权限外的实体 ID、rowid 和时间全部保真。
CREATE TABLE permissions_prd069_old (
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=5),
  status TEXT NOT NULL CHECK (status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE (tenant_id,code),
  UNIQUE (tenant_id,permission_id)
);
-- 每租户旧 Permission 保留稳定 ID、rowid 和时间，只排除 0061 新权限。
INSERT INTO permissions_prd069_old(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       5,status,created_at,updated_at
FROM permissions WHERE code!='intent_risk.classify';
DROP TABLE permissions;
-- 旧投影复制完整后恢复正式表名，并重建同 tenant owner 约束。
ALTER TABLE permissions_prd069_old RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059 ON permissions(tenant_id,category,code);
CREATE TRIGGER trg_permissions_owner_insert_prd059
BEFORE INSERT ON permissions FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM rbac_permission_templates template
  WHERE template.permission_code=NEW.code AND template.category=NEW.category
    AND template.name=NEW.name AND template.description=NEW.description
    AND template.risk_level=NEW.risk_level AND template.catalog_revision=NEW.catalog_revision
)
  OR NOT EXISTS (SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR NEW.status!='active'
  OR EXISTS (
    SELECT 1 FROM permissions current
    WHERE current.permission_id=NEW.permission_id
       OR (current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
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

-- 只回退冻结角色；revision 1 的新租户角色保持下限 1，绝不能产生 revision 0。
UPDATE roles
SET permission_revision=CASE WHEN permission_revision>1 THEN permission_revision-1 ELSE 1 END,
    matrix_sha256=CASE code
      WHEN 'admin' THEN '64bd61c22d9a190d17bdccb13f1205d9175955c0107d9783a250f11137a4c655'
      WHEN 'security_admin' THEN 'b0c10e9673a23ed0ebdf783e64d3929797a4e1ecdd4bf39b33c1987fd48b0160'
    END
WHERE role_id IN (SELECT role_id FROM prd069_roles_to_contract);

CREATE TRIGGER trg_roles_owner_insert_prd059
BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS (SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR NEW.code NOT IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN '64bd61c22d9a190d17bdccb13f1205d9175955c0107d9783a250f11137a4c655'
    WHEN 'developer' THEN '2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c'
    WHEN 'viewer' THEN '3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086'
    WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
    WHEN 'security_admin' THEN 'b0c10e9673a23ed0ebdf783e64d3929797a4e1ecdd4bf39b33c1987fd48b0160'
    WHEN 'ops_admin' THEN '6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b'
  END
  OR NEW.is_permission_immutable!=CASE WHEN NEW.code='admin' THEN 1 ELSE 0 END
  OR EXISTS (
    SELECT 1 FROM roles current
    WHERE current.role_id=NEW.role_id
       OR (current.tenant_id=NEW.tenant_id AND current.code=NEW.code)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_roles_matrix_update_prd059
BEFORE UPDATE ON roles FOR EACH ROW
WHEN OLD.code='admin'
  OR NEW.rowid!=OLD.rowid OR NEW.role_id!=OLD.role_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.code!=OLD.code OR NEW.name!=OLD.name OR NEW.description!=OLD.description
  OR NEW.status!=OLD.status OR NEW.created_at!=OLD.created_at
  OR NEW.is_permission_immutable!=OLD.is_permission_immutable
  OR NEW.permission_revision!=OLD.permission_revision+1
  OR NEW.matrix_sha256=OLD.matrix_sha256
  OR length(NEW.matrix_sha256)!=64 OR NEW.matrix_sha256 GLOB '*[^0-9a-f]*'
BEGIN SELECT RAISE(ABORT,'rbac_role_immutable'); END;
CREATE TRIGGER trg_role_permissions_owner_insert_prd059
BEFORE INSERT ON role_permissions FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM roles role
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
  JOIN rbac_role_permission_templates template
    ON template.role_code=role.code AND template.permission_code=permission.code
  WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id AND role.status='active'
    AND permission.tenant_id=NEW.tenant_id AND permission.permission_id=NEW.permission_id
    AND permission.status='active'
)
  OR EXISTS (
    SELECT 1 FROM role_permissions current
    WHERE current.role_permission_id=NEW.role_permission_id
       OR (current.tenant_id=NEW.tenant_id AND current.role_id=NEW.role_id
           AND current.permission_id=NEW.permission_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_owner_mismatch'); END;
CREATE TRIGGER trg_role_permissions_delete_prd059
BEFORE DELETE ON role_permissions FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM roles role
  WHERE role.tenant_id=OLD.tenant_id AND role.role_id=OLD.role_id AND role.code='admin'
)
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;

-- 降级后新 tenant 继续使用 revision 5 目录和 hash，角色 revision 仍从 1 起步。
CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 降级后的新 tenant 先创建 revision 5 hash 对应的六个 revision 1 角色。
  INSERT INTO roles(
    role_id,tenant_id,code,name,description,status,created_at,updated_at,
    permission_revision,matrix_sha256,is_permission_immutable
  )
  SELECT 'role_'||lower(hex(randomblob(16))),NEW.tenant_id,catalog.role_code,
    CASE catalog.role_code WHEN 'admin' THEN 'Admin' WHEN 'developer' THEN 'Developer'
      WHEN 'viewer' THEN 'Viewer' WHEN 'finance_admin' THEN 'FinanceAdmin'
      WHEN 'security_admin' THEN 'SecurityAdmin' WHEN 'ops_admin' THEN 'OpsAdmin' END,
    CASE catalog.role_code
      WHEN 'admin' THEN '租户最高管理员，拥有完整权限且矩阵不可修改。'
      WHEN 'developer' THEN '负责模型接入、Key、调试和请求排障，不读取正式账单。'
      WHEN 'viewer' THEN '仅查看基础概览和公开控制台信息。'
      WHEN 'finance_admin' THEN '负责用量、账本、预算、价格和成本异常。'
      WHEN 'security_admin' THEN '负责审计、DLP、策略、错误归一和安全事件。'
      WHEN 'ops_admin' THEN '负责 Provider、环境、项目、Key、模型和路由运维。' END,
    'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,
    CASE catalog.role_code
      WHEN 'admin' THEN '64bd61c22d9a190d17bdccb13f1205d9175955c0107d9783a250f11137a4c655'
      WHEN 'developer' THEN '2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c'
      WHEN 'viewer' THEN '3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086'
      WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
      WHEN 'security_admin' THEN 'b0c10e9673a23ed0ebdf783e64d3929797a4e1ecdd4bf39b33c1987fd48b0160'
      WHEN 'ops_admin' THEN '6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b' END,
    CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;

  -- 再从 revision 5 产品目录生成 56 项同 tenant Permission 投影。
  INSERT INTO permissions(
    permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at
  )
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,
    template.catalog_revision,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;

  -- 最后建立 149 项产品模板约束下的默认关系，禁止跨租户拼接。
  INSERT INTO role_permissions(
    role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
  )
  SELECT 'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,role.role_id,
    permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM roles role
  JOIN rbac_role_permission_templates template ON template.role_code=role.code
  JOIN permissions permission
    ON permission.tenant_id=role.tenant_id AND permission.code=template.permission_code
  WHERE role.tenant_id=NEW.tenant_id;
END;

-- 最终门禁证明精确恢复 0060 的 56/149、每租户 56、52 列和 12 个 RequestLog trigger。
DROP TABLE IF EXISTS temp.prd069_down_final_guard;
CREATE TEMP TABLE prd069_down_final_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd069_down_final_ok CHECK (issue_count=0)
);
-- 提交前证明目录、租户投影、RequestLog 和数据库完整性均精确恢复 0060。
INSERT INTO prd069_down_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=56
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=5)=56
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=149
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*56 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=5)
          =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('request_logs'))=52
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='index'
           AND tbl_name='request_logs' AND name NOT LIKE 'sqlite_autoindex%')=24
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
           AND tbl_name='request_logs')=12 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%prd069'
     AND name NOT IN ('prd069_roles_to_contract','prd069_down_final_guard'))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd069_down_final_guard;
DROP TABLE prd069_roles_to_contract;

COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
