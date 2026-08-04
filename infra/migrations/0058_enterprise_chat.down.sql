-- PRD-066 回滚：只允许从“无任何 Chat 业务事实”的 canonical revision 3 空状态恢复 0057。
-- ChatSession、Message、grant、Webhook 和显式权限变更都是不可伪造的治理证据；任一存在时，
-- 必须在首项持久 DDL 前失败关闭，不能为了降级而删除请求、用量或权限审计线索。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 业务事实使用稳定错误原因，与结构漂移分开，便于迁移器和运维人员判定该失败不可重试。
DROP TABLE IF EXISTS temp.prd066_down_business_guard;
DROP TRIGGER IF EXISTS temp.trg_prd066_down_business_guard;
CREATE TEMP TABLE prd066_down_business_guard (
  invalid_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd066_down_business_guard
BEFORE INSERT ON prd066_down_business_guard FOR EACH ROW
WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd066_down_business_facts_present'); END;
-- 汇总所有不可丢失的 Chat 与权限业务事实，非零时须在持久变更前终止回滚。
INSERT INTO prd066_down_business_guard(invalid_count)
SELECT
  (SELECT COUNT(*) FROM chat_sessions)
  + (SELECT COUNT(*) FROM chat_messages)
  + (SELECT COUNT(*) FROM chat_execution_grants)
  -- 精确 JSON 命中和损坏 JSON 的保守文本命中同时覆盖，不能用畸形订阅绕过回滚门禁。
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
     WHERE (
       json_valid(endpoint.event_types_json)
       AND EXISTS (
         SELECT 1 FROM json_each(endpoint.event_types_json) item
         WHERE item.value='chat.message_sent'
       )
     ) OR lower(endpoint.event_types_json) LIKE '%chat.message_sent%')
  + (SELECT COUNT(*) FROM webhook_deliveries
     WHERE event_type='chat.message_sent')
  + (SELECT COUNT(*) FROM webhook_delivery_attempts attempt
     JOIN webhook_deliveries delivery
       ON delivery.tenant_id=attempt.tenant_id
      AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
     WHERE delivery.event_type='chat.message_sent')
  + (SELECT COUNT(*) FROM webhook_dead_letter_events
     WHERE event_type='chat.message_sent')
  -- 管理员显式授予或撤销 chat.use 后，source event 本身就是必须保留的权限治理事实。
  + (SELECT COUNT(*) FROM role_permission_events event
     WHERE EXISTS (
       SELECT 1 FROM json_each(event.added_permissions_json) item
       WHERE item.value='chat.use'
     ) OR EXISTS (
       SELECT 1 FROM json_each(event.removed_permissions_json) item
       WHERE item.value='chat.use'
     ))
  -- 只允许迁移生成的 canonical Admin/Developer/Viewer 持权；其他持权者属于显式业务配置。
  + (SELECT COUNT(*)
     FROM role_permissions relation
     JOIN permissions permission
       ON permission.tenant_id=relation.tenant_id
      AND permission.permission_id=relation.permission_id
     JOIN roles role
       ON role.tenant_id=relation.tenant_id AND role.role_id=relation.role_id
     WHERE permission.code='chat.use' AND (
       role.code NOT IN ('admin','developer','viewer')
       OR role.matrix_sha256!=CASE role.code
         WHEN 'admin' THEN 'af482b55e38b1b893188dfefc305fd6825307d2f2a78fe887e1e96df4fd1c7ef'
         WHEN 'developer' THEN 'ad81843ccd5f21fa2faef129a033712a52eb110f1ee41c40d4d356814885a4a3'
         WHEN 'viewer' THEN '77030a56fb45f45cbf9325c02a21f751e2f27ab40b79d8b1cd1997e5e4557228'
       END
       OR (role.code IN ('admin','developer')
           AND role.permission_revision NOT IN (1,2,3))
       OR (role.code='viewer' AND role.permission_revision NOT IN (1,2))
       OR EXISTS (
         SELECT 1 FROM rbac_role_permission_templates template
         WHERE template.role_code=role.code AND NOT EXISTS (
           SELECT 1 FROM role_permissions expected_relation
           JOIN permissions expected_permission
             ON expected_permission.tenant_id=expected_relation.tenant_id
            AND expected_permission.permission_id=expected_relation.permission_id
           WHERE expected_relation.tenant_id=role.tenant_id
             AND expected_relation.role_id=role.role_id
             AND expected_permission.code=template.permission_code
         )
       )
       OR EXISTS (
         SELECT 1 FROM role_permissions actual_relation
         JOIN permissions actual_permission
           ON actual_permission.tenant_id=actual_relation.tenant_id
          AND actual_permission.permission_id=actual_relation.permission_id
         WHERE actual_relation.tenant_id=role.tenant_id
           AND actual_relation.role_id=role.role_id
           AND NOT EXISTS (
             SELECT 1 FROM rbac_role_permission_templates template
             WHERE template.role_code=role.code
               AND template.permission_code=actual_permission.code
           )
       )
     ));
DROP TRIGGER trg_prd066_down_business_guard;
DROP TABLE prd066_down_business_guard;

-- 记录可以安全收缩的 canonical 角色。revision=1 表示 0058 后新租户；revision=2/3
-- 表示从旧默认矩阵升级，回滚时分别恢复为 1/1/2，不能把角色 revision 当 catalog revision。
DROP TABLE IF EXISTS temp.prd066_roles_to_contract;
CREATE TEMP TABLE prd066_roles_to_contract AS
SELECT role.role_id,role.tenant_id,role.code,role.permission_revision
FROM roles role
WHERE role.code IN ('admin','developer','viewer')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN 'af482b55e38b1b893188dfefc305fd6825307d2f2a78fe887e1e96df4fd1c7ef'
    WHEN 'developer' THEN 'ad81843ccd5f21fa2faef129a033712a52eb110f1ee41c40d4d356814885a4a3'
    WHEN 'viewer' THEN '77030a56fb45f45cbf9325c02a21f751e2f27ab40b79d8b1cd1997e5e4557228'
  END
  AND (
    (role.code IN ('admin','developer') AND role.permission_revision IN (1,2,3))
    OR (role.code='viewer' AND role.permission_revision IN (1,2))
  )
  AND NOT EXISTS (
    SELECT 1 FROM rbac_role_permission_templates template
    WHERE template.role_code=role.code AND NOT EXISTS (
      SELECT 1 FROM role_permissions relation
      JOIN permissions permission
        ON permission.tenant_id=relation.tenant_id
       AND permission.permission_id=relation.permission_id
      WHERE relation.tenant_id=role.tenant_id
        AND relation.role_id=role.role_id
        AND permission.code=template.permission_code
    )
  )
  AND NOT EXISTS (
    SELECT 1 FROM role_permissions relation
    JOIN permissions permission
      ON permission.tenant_id=relation.tenant_id
     AND permission.permission_id=relation.permission_id
    WHERE relation.tenant_id=role.tenant_id
      AND relation.role_id=role.role_id
      AND NOT EXISTS (
        SELECT 1 FROM rbac_role_permission_templates template
        WHERE template.role_code=role.code
          AND template.permission_code=permission.code
      )
  );

-- 结构门禁验证 0058 栈顶、canonical RBAC、最终 Chat 对象清单和未知依赖。
DROP TABLE IF EXISTS temp.prd066_down_guard;
CREATE TEMP TABLE prd066_down_guard (
  issue_count INTEGER NOT NULL
    CONSTRAINT prd066_down_preflight_ok CHECK (issue_count = 0)
);
-- 汇总结构与 canonical RBAC 偏差，只有完全匹配 0058 空状态才进入持久回滚。
INSERT INTO prd066_down_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
      WHERE version='0058' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations
     WHERE status='applied' AND version>'0058')
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='table' AND name IN (
        'tenants','sessions','roles','permissions','role_permissions',
        'rbac_permission_templates','rbac_role_permission_templates',
        'role_permission_events','webhook_endpoints','webhook_deliveries',
        'webhook_delivery_attempts','webhook_dead_letter_events',
        'chat_sessions','chat_messages','chat_execution_grants'
      ))=15 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM chat_sessions)
  + (SELECT COUNT(*) FROM chat_messages)
  + (SELECT COUNT(*) FROM chat_execution_grants)
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=51
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE catalog_revision=3)=51
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE permission_code='chat.use' AND category='chat'
             AND risk_level='write')=1
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=134
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates
           WHERE permission_code='chat.use'
             AND role_code IN ('admin','developer','viewer'))=3
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates
           WHERE permission_code='chat.use')=3
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)
      =(SELECT COUNT(*)*51 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=3)
      =(SELECT COUNT(*) FROM permissions)
      AND (SELECT COUNT(*) FROM permissions WHERE code='chat.use')
      =(SELECT COUNT(*) FROM tenants)
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*)
     FROM permissions permission
     LEFT JOIN rbac_permission_templates template
       ON template.permission_code=permission.code
      AND template.category=permission.category
      AND template.name=permission.name
      AND template.description=permission.description
      AND template.risk_level=permission.risk_level
      AND template.catalog_revision=permission.catalog_revision
     WHERE template.permission_code IS NULL OR permission.status!='active')
  -- Admin 必须全部进入收缩集合；任意新规范 hash 未进入集合都表示关系或 revision 漂移。
  + (SELECT COUNT(*) FROM roles role
     WHERE role.code='admin' AND NOT EXISTS (
       SELECT 1 FROM prd066_roles_to_contract source
       WHERE source.tenant_id=role.tenant_id AND source.role_id=role.role_id
     ))
  + (SELECT COUNT(*) FROM roles role
     WHERE role.matrix_sha256 IN (
       'af482b55e38b1b893188dfefc305fd6825307d2f2a78fe887e1e96df4fd1c7ef',
       'ad81843ccd5f21fa2faef129a033712a52eb110f1ee41c40d4d356814885a4a3',
       '77030a56fb45f45cbf9325c02a21f751e2f27ab40b79d8b1cd1997e5e4557228'
     ) AND NOT EXISTS (
       SELECT 1 FROM prd066_roles_to_contract source
       WHERE source.tenant_id=role.tenant_id AND source.role_id=role.role_id
     ))
  + (SELECT COUNT(*)
     FROM role_permissions relation
     JOIN permissions permission
       ON permission.tenant_id=relation.tenant_id
      AND permission.permission_id=relation.permission_id
     WHERE permission.code='chat.use'
       AND NOT EXISTS (
         SELECT 1 FROM prd066_roles_to_contract source
         WHERE source.tenant_id=relation.tenant_id
           AND source.role_id=relation.role_id
       ))
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name LIKE 'trg_chat_%_prd066')=9
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='index' AND name IN (
        'uq_sessions_tenant_session_prd066',
        'uq_chat_sessions_tenant_session_prd066',
        'idx_chat_sessions_tenant_owner_status_prd066',
        'idx_chat_sessions_tenant_expiry_prd066',
        'uq_chat_messages_tenant_message_prd066',
        'uq_chat_messages_tenant_session_client_prd066',
        'uq_chat_messages_tenant_gateway_request_prd066',
        'uq_chat_messages_tenant_session_inflight_prd066',
        'idx_chat_messages_tenant_session_sequence_prd066',
        'uq_chat_execution_grants_hash_prd066',
        'uq_chat_execution_grants_tenant_message_prd066',
        'idx_chat_execution_grants_tenant_session_status_prd066',
        'idx_chat_execution_grants_tenant_expiry_prd066'
      ))=13 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_foreign_key_list('chat_sessions'))=13
      AND (SELECT COUNT(*) FROM pragma_foreign_key_list('chat_messages'))=5
      AND (SELECT COUNT(*) FROM pragma_foreign_key_list('chat_execution_grants'))=17
    THEN 0 ELSE 1 END
  -- 除本迁移创建的表、索引和 trigger 外，任何引用 Chat 表的 schema 对象都是未知依赖。
  + (SELECT COUNT(*) FROM sqlite_schema object
     WHERE object.sql IS NOT NULL
       AND (
         instr(lower(object.sql),'chat_sessions')>0
         OR instr(lower(object.sql),'chat_messages')>0
         OR instr(lower(object.sql),'chat_execution_grants')>0
         OR instr(lower(object.sql),'''chat.use''')>0
       )
       AND object.name NOT IN (
         'chat_sessions','chat_messages','chat_execution_grants',
         'uq_chat_sessions_tenant_session_prd066',
         'idx_chat_sessions_tenant_owner_status_prd066',
         'idx_chat_sessions_tenant_expiry_prd066',
         'trg_chat_sessions_owner_insert_prd066',
         'trg_chat_sessions_state_update_prd066',
         'trg_chat_sessions_delete_prd066',
         'uq_chat_messages_tenant_message_prd066',
         'uq_chat_messages_tenant_session_client_prd066',
         'uq_chat_messages_tenant_gateway_request_prd066',
         'uq_chat_messages_tenant_session_inflight_prd066',
         'idx_chat_messages_tenant_session_sequence_prd066',
         'trg_chat_messages_owner_insert_prd066',
         'trg_chat_messages_state_update_prd066',
         'trg_chat_messages_delete_prd066',
         'uq_chat_execution_grants_hash_prd066',
         'uq_chat_execution_grants_tenant_message_prd066',
         'idx_chat_execution_grants_tenant_session_status_prd066',
         'idx_chat_execution_grants_tenant_expiry_prd066',
         'trg_chat_execution_grants_owner_insert_prd066',
         'trg_chat_execution_grants_state_update_prd066',
         'trg_chat_execution_grants_delete_prd066'
       ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd066_down_guard;

-- 先移除 Chat 子表，再移除登录 Session 的复合父键；表级事实已由业务门禁证明为空。
DROP TRIGGER trg_chat_execution_grants_delete_prd066;
DROP TRIGGER trg_chat_execution_grants_state_update_prd066;
DROP TRIGGER trg_chat_execution_grants_owner_insert_prd066;
DROP INDEX idx_chat_execution_grants_tenant_expiry_prd066;
DROP INDEX idx_chat_execution_grants_tenant_session_status_prd066;
DROP INDEX uq_chat_execution_grants_tenant_message_prd066;
DROP INDEX uq_chat_execution_grants_hash_prd066;
DROP TABLE chat_execution_grants;

DROP TRIGGER trg_chat_messages_delete_prd066;
DROP TRIGGER trg_chat_messages_state_update_prd066;
DROP TRIGGER trg_chat_messages_owner_insert_prd066;
DROP INDEX uq_chat_messages_tenant_session_inflight_prd066;
DROP INDEX idx_chat_messages_tenant_session_sequence_prd066;
DROP INDEX uq_chat_messages_tenant_gateway_request_prd066;
DROP INDEX uq_chat_messages_tenant_session_client_prd066;
DROP INDEX uq_chat_messages_tenant_message_prd066;
DROP TABLE chat_messages;

DROP TRIGGER trg_chat_sessions_delete_prd066;
DROP TRIGGER trg_chat_sessions_state_update_prd066;
DROP TRIGGER trg_chat_sessions_owner_insert_prd066;
DROP INDEX idx_chat_sessions_tenant_expiry_prd066;
DROP INDEX idx_chat_sessions_tenant_owner_status_prd066;
DROP INDEX uq_chat_sessions_tenant_session_prd066;
DROP TABLE chat_sessions;
DROP INDEX uq_sessions_tenant_session_prd066;

-- RBAC 收缩期间短暂移除不可变/owner trigger；所有删除集合已经被前置门禁冻结。
DROP TRIGGER trg_tenants_rbac_initialize_prd059;
DROP TRIGGER trg_roles_owner_insert_prd059;
DROP TRIGGER trg_roles_matrix_update_prd059;
DROP TRIGGER trg_role_permissions_owner_insert_prd059;
DROP TRIGGER trg_role_permissions_delete_prd059;
DROP TRIGGER trg_rbac_role_templates_insert_prd059;
DROP TRIGGER trg_rbac_role_templates_update_prd059;
DROP TRIGGER trg_rbac_role_templates_delete_prd059;

-- 先移除租户侧 chat.use 关系，避免删除权限投影时遗留复合外键引用。
DELETE FROM role_permissions
WHERE permission_id IN (
  SELECT permission_id FROM permissions WHERE code='chat.use'
);
-- 再移除产品角色模板的 chat.use 关系，使默认矩阵恢复到 revision 2。
DELETE FROM rbac_role_permission_templates
WHERE permission_code='chat.use';

-- 产品模板恢复 revision 2 的 50 项，旧 rowid 和展示元数据全部保真。
CREATE TABLE rbac_permission_templates_prd066_old (
  permission_code TEXT PRIMARY KEY CHECK (
    length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'
  ),
  category TEXT NOT NULL CHECK (length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=2)
);
-- 复制既有模板的 rowid 与展示元数据，仅收缩 chat.use 并回退目录 revision。
INSERT INTO rbac_permission_templates_prd066_old(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,2
FROM rbac_permission_templates
WHERE permission_code!='chat.use';
DROP TABLE rbac_permission_templates;
-- 原子替换产品权限目录，让原表名重新指向已验证的 revision 2 结构。
ALTER TABLE rbac_permission_templates_prd066_old RENAME TO rbac_permission_templates;

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

-- 租户 Permission 删除 chat.use 并恢复 revision 2；其余稳定 ID、rowid 和时间不变。
CREATE TABLE permissions_prd066_old (
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=2),
  status TEXT NOT NULL CHECK (status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE (tenant_id,code),
  UNIQUE (tenant_id,permission_id)
);
-- 逐租户复制稳定权限标识与 rowid，只删除本迁移新增的 chat.use 投影。
INSERT INTO permissions_prd066_old(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       2,status,created_at,updated_at
FROM permissions
WHERE code!='chat.use';
DROP TABLE permissions;
-- 原子恢复租户权限表名称，再重建 revision 2 的索引与不可变约束触发器。
ALTER TABLE permissions_prd066_old RENAME TO permissions;
CREATE INDEX idx_permissions_tenant_category_prd059
  ON permissions(tenant_id,category,code);
CREATE TRIGGER trg_permissions_owner_insert_prd059
BEFORE INSERT ON permissions FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM rbac_permission_templates template
  WHERE template.permission_code=NEW.code
    AND template.category=NEW.category AND template.name=NEW.name
    AND template.description=NEW.description AND template.risk_level=NEW.risk_level
    AND template.catalog_revision=NEW.catalog_revision
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

-- 只收缩前置集合中的 canonical 角色；新租户 revision=1 保持 1，升级角色回退一次。
UPDATE roles
SET permission_revision=CASE
      WHEN permission_revision>1 THEN permission_revision-1 ELSE 1 END,
    matrix_sha256=CASE code
      WHEN 'admin' THEN '1826b819ad25042037eefae9c3372a9fbcb13e6327dfda409f8da7795bac9f73'
      WHEN 'developer' THEN '220bc91e6e661c7dd81385de8e87256bfc913d9bb7f5c777505b1464c0a8037f'
      WHEN 'viewer' THEN 'b6c1f328a8a1f31973d73f21a574e47fec76701c5a515cca31eead9a1b74068a'
    END
WHERE role_id IN (SELECT role_id FROM prd066_roles_to_contract);

CREATE TRIGGER trg_roles_owner_insert_prd059
BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS (SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR NEW.code NOT IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN '1826b819ad25042037eefae9c3372a9fbcb13e6327dfda409f8da7795bac9f73'
    WHEN 'developer' THEN '220bc91e6e661c7dd81385de8e87256bfc913d9bb7f5c777505b1464c0a8037f'
    WHEN 'viewer' THEN 'b6c1f328a8a1f31973d73f21a574e47fec76701c5a515cca31eead9a1b74068a'
    WHEN 'finance_admin' THEN '181b77fb00bb0a43e939cd1995043ac1c61401480d6f9c4034eac61d6e2240be'
    WHEN 'security_admin' THEN 'ee8c11c2e087dc08098b4f80f13c14629bd56f229a6af6a7f57bbe9ec1f87a1c'
    WHEN 'ops_admin' THEN 'a5dbc9448a68be2c7a5b27b372d497a6739e4b240cfeb93eaff137dc96e1f8d8'
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
  SELECT 1
  FROM roles role
  JOIN permissions permission ON permission.tenant_id=role.tenant_id
  JOIN rbac_role_permission_templates template
    ON template.role_code=role.code AND template.permission_code=permission.code
  WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.role_id
    AND role.status='active'
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
  WHERE role.tenant_id=OLD.tenant_id AND role.role_id=OLD.role_id
    AND role.code='admin'
)
BEGIN SELECT RAISE(ABORT,'rbac_role_permission_immutable'); END;

-- 降级后新 tenant 仍按 revision 2 产品目录初始化六角色和 50 项 tenant 投影。
CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 先创建六个默认角色，后续权限关系才能通过同租户复合外键与模板约束。
  INSERT INTO roles(
    role_id,tenant_id,code,name,description,status,created_at,updated_at,
    permission_revision,matrix_sha256,is_permission_immutable
  )
  SELECT
    'role_'||lower(hex(randomblob(16))),NEW.tenant_id,catalog.role_code,
    CASE catalog.role_code
      WHEN 'admin' THEN 'Admin'
      WHEN 'developer' THEN 'Developer'
      WHEN 'viewer' THEN 'Viewer'
      WHEN 'finance_admin' THEN 'FinanceAdmin'
      WHEN 'security_admin' THEN 'SecurityAdmin'
      WHEN 'ops_admin' THEN 'OpsAdmin'
    END,
    CASE catalog.role_code
      WHEN 'admin' THEN '租户最高管理员，拥有完整权限且矩阵不可修改。'
      WHEN 'developer' THEN '负责模型接入、Key、调试和请求排障，不读取正式账单。'
      WHEN 'viewer' THEN '仅查看基础概览和公开控制台信息。'
      WHEN 'finance_admin' THEN '负责用量、账本、预算、价格和成本异常。'
      WHEN 'security_admin' THEN '负责审计、DLP、策略、错误归一和安全事件。'
      WHEN 'ops_admin' THEN '负责 Provider、环境、项目、Key、模型和路由运维。'
    END,
    'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),1,
    CASE catalog.role_code
      WHEN 'admin' THEN '1826b819ad25042037eefae9c3372a9fbcb13e6327dfda409f8da7795bac9f73'
      WHEN 'developer' THEN '220bc91e6e661c7dd81385de8e87256bfc913d9bb7f5c777505b1464c0a8037f'
      WHEN 'viewer' THEN 'b6c1f328a8a1f31973d73f21a574e47fec76701c5a515cca31eead9a1b74068a'
      WHEN 'finance_admin' THEN '181b77fb00bb0a43e939cd1995043ac1c61401480d6f9c4034eac61d6e2240be'
      WHEN 'security_admin' THEN 'ee8c11c2e087dc08098b4f80f13c14629bd56f229a6af6a7f57bbe9ec1f87a1c'
      WHEN 'ops_admin' THEN 'a5dbc9448a68be2c7a5b27b372d497a6739e4b240cfeb93eaff137dc96e1f8d8'
    END,
    CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;

  -- 再按 revision 2 产品目录生成租户权限投影，禁止沿用已删除的 chat.use 权限。
  INSERT INTO permissions(
    permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at
  )
  SELECT
    'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,
    template.catalog_revision,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;

  -- 最后依据产品角色模板建立关系，确保每条授权都由同租户角色和权限共同拥有。
  INSERT INTO role_permissions(
    role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
  )
  SELECT
    'rperm_'||lower(hex(randomblob(16))),NEW.tenant_id,role.role_id,
    permission.permission_id,strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM roles role
  JOIN rbac_role_permission_templates template ON template.role_code=role.code
  JOIN permissions permission
    ON permission.tenant_id=role.tenant_id AND permission.code=template.permission_code
  WHERE role.tenant_id=NEW.tenant_id;
END;

-- 提交前证明 revision 2、旧默认矩阵、对象清理和数据库完整性均已恢复。
DROP TABLE IF EXISTS temp.prd066_down_final_guard;
CREATE TEMP TABLE prd066_down_final_guard (
  issue_count INTEGER NOT NULL
    CONSTRAINT prd066_down_final_state_ok CHECK (issue_count = 0)
);
-- 提交前集中核验目录规模、角色矩阵、对象清理和外键状态，任一偏差都回滚事务。
INSERT INTO prd066_down_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=50
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE catalog_revision=2)=50
      AND (SELECT COUNT(*) FROM rbac_permission_templates
           WHERE permission_code='chat.use')=0
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM rbac_role_permission_templates)=131
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates
           WHERE permission_code='chat.use')=0
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)
      =(SELECT COUNT(*)*50 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=2)
      =(SELECT COUNT(*) FROM permissions)
      AND (SELECT COUNT(*) FROM permissions WHERE code='chat.use')=0
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM prd066_roles_to_contract source
     JOIN roles role
       ON role.tenant_id=source.tenant_id AND role.role_id=source.role_id
     WHERE role.permission_revision!=CASE
             WHEN source.permission_revision>1
             THEN source.permission_revision-1 ELSE 1 END
        OR role.matrix_sha256!=CASE role.code
          WHEN 'admin' THEN '1826b819ad25042037eefae9c3372a9fbcb13e6327dfda409f8da7795bac9f73'
          WHEN 'developer' THEN '220bc91e6e661c7dd81385de8e87256bfc913d9bb7f5c777505b1464c0a8037f'
          WHEN 'viewer' THEN 'b6c1f328a8a1f31973d73f21a574e47fec76701c5a515cca31eead9a1b74068a'
        END)
  + (SELECT COUNT(*) FROM sqlite_schema
     WHERE name IN (
       'chat_sessions','chat_messages','chat_execution_grants',
       'uq_sessions_tenant_session_prd066',
       'uq_chat_sessions_tenant_session_prd066',
       'idx_chat_sessions_tenant_owner_status_prd066',
       'idx_chat_sessions_tenant_expiry_prd066',
       'uq_chat_messages_tenant_message_prd066',
       'uq_chat_messages_tenant_session_client_prd066',
       'uq_chat_messages_tenant_gateway_request_prd066',
       'uq_chat_messages_tenant_session_inflight_prd066',
       'idx_chat_messages_tenant_session_sequence_prd066',
       'uq_chat_execution_grants_hash_prd066',
       'uq_chat_execution_grants_tenant_message_prd066',
       'idx_chat_execution_grants_tenant_session_status_prd066',
       'idx_chat_execution_grants_tenant_expiry_prd066'
     ) OR name LIKE 'trg_chat_%_prd066')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd066_down_final_guard;
DROP TABLE prd066_roles_to_contract;

COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
