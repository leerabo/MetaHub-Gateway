-- PRD-065：多角色首页配置与 dashboard.role_viewed 访问事件。
-- 安全边界：配置按 tenant_id + role_code 复合 owner 隔离；事件表只扩展固定枚举，
-- 不保存角色权限快照、Provider Key、Token、Prompt、Response 或上游错误正文。
-- console_page_events 是既有审计事实表，重建必须显式保留 rowid、十二列和三个索引。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- 所有持久 DDL 前先证明数据库精确位于 0056 栈顶。未知列、索引或依赖若继续迁移，
-- 可能在 SQLite 换表时被静默丢弃，因此统一通过 TEMP CHECK 原子失败关闭。
DROP TABLE IF EXISTS temp.prd065_up_guard;
CREATE TEMP TABLE prd065_up_guard (
  issue_count INTEGER NOT NULL
    CONSTRAINT prd065_up_preconditions_ok CHECK (issue_count = 0)
);
-- 执行前置零值断言；任一栈顶、owner 或依赖漂移都在持久 DDL 之前中止。
INSERT INTO prd065_up_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
      WHERE version='0056' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations
     WHERE status='applied' AND version>'0056')
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='table' AND name IN (
        'tenants','roles','console_page_events'
      ))=3 THEN 0 ELSE 1 END
  -- tenants 必须仍是七列根 owner 表，禁止在未知结构上建立租户初始化触发器。
  + CASE WHEN (
      SELECT group_concat(name||':'||upper(type)||':'||"notnull"||':'||pk, ',')
      FROM (
        SELECT name,type,"notnull",pk
        FROM pragma_table_info('tenants') ORDER BY cid
      )
    )='tenant_id:TEXT:0:1,slug:TEXT:1:0,name:TEXT:1:0,service_mode:TEXT:1:0,status:TEXT:1:0,created_at:TEXT:1:0,updated_at:TEXT:1:0'
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_table_info('tenants') WHERE dflt_value IS NOT NULL)
  -- roles 必须保持 PRD-059 的十一列六角色结构及复合唯一 owner；默认值也逐项核验。
  + CASE WHEN (
      SELECT group_concat(name||':'||upper(type)||':'||"notnull"||':'||pk, ',')
      FROM (
        SELECT name,type,"notnull",pk
        FROM pragma_table_info('roles') ORDER BY cid
      )
    )='role_id:TEXT:0:1,tenant_id:TEXT:1:0,code:TEXT:1:0,name:TEXT:1:0,description:TEXT:1:0,status:TEXT:1:0,created_at:TEXT:1:0,updated_at:TEXT:1:0,permission_revision:INTEGER:1:0,matrix_sha256:TEXT:1:0,is_permission_immutable:INTEGER:1:0'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('roles')
      WHERE dflt_value IS NOT NULL)=2
      AND (SELECT dflt_value FROM pragma_table_info('roles')
           WHERE name='description')=quote('')
      AND CAST((SELECT dflt_value FROM pragma_table_info('roles')
                WHERE name='permission_revision') AS INTEGER)=1
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_foreign_key_list('roles'))=1
      AND (SELECT COUNT(*) FROM pragma_foreign_key_list('roles')
           WHERE "table"='tenants' AND "from"='tenant_id' AND "to"='tenant_id'
             AND on_update='NO ACTION' AND on_delete='NO ACTION' AND "match"='NONE')=1
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_index_list('roles'))=4
      AND (SELECT COUNT(*) FROM pragma_index_list('roles')
           WHERE name='idx_roles_tenant_id' AND "unique"=0
             AND origin='c' AND partial=0)=1
      AND (SELECT COUNT(*) FROM pragma_index_list('roles')
           WHERE "unique"=1 AND origin IN ('pk','u') AND partial=0)=3
    THEN 0 ELSE 1 END
  + CASE WHEN (
      SELECT group_concat(name||':'||"desc"||':'||coll, ',') FROM (
        SELECT name,"desc",coll FROM pragma_index_xinfo('idx_roles_tenant_id')
        WHERE "key"=1 ORDER BY seqno
      )
    )='tenant_id:0:BINARY' THEN 0 ELSE 1 END
  + CASE WHEN instr(replace(replace(lower((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='roles')),char(10),''),' ',''),
      'unique(tenant_id,code)')>0
      AND instr(replace(replace(lower((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='roles')),char(10),''),' ',''),
      'unique(tenant_id,role_id)')>0
    THEN 0 ELSE 1 END
  -- 每个 tenant 必须精确拥有六个固定角色；总数相同仍可能掩盖 5/7 分布，故逐租户复核。
  + ABS((SELECT COUNT(*) FROM roles)-(SELECT COUNT(*)*6 FROM tenants))
  + (SELECT COUNT(*) FROM tenants tenant
     WHERE (SELECT COUNT(*) FROM roles role
            WHERE role.tenant_id=tenant.tenant_id)!=6)
  + (SELECT COUNT(*) FROM roles role
     LEFT JOIN tenants tenant ON tenant.tenant_id=role.tenant_id
     WHERE tenant.tenant_id IS NULL
        OR role.code NOT IN (
          'admin','developer','viewer','finance_admin','security_admin','ops_admin'
        ))
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name='trg_tenants_rbac_initialize_prd059'
        AND tbl_name='tenants')=1 THEN 0 ELSE 1 END
  -- console_page_events 必须仍是 0053 的十二列、三外键、五事件结构。
  + CASE WHEN (
      SELECT group_concat(name||':'||upper(type)||':'||"notnull"||':'||pk, ',')
      FROM (
        SELECT name,type,"notnull",pk
        FROM pragma_table_info('console_page_events') ORDER BY cid
      )
    )='event_id:TEXT:0:1,tenant_id:TEXT:1:0,user_id:TEXT:1:0,session_id:TEXT:0:0,event_type:TEXT:1:0,navigation_item_id:TEXT:1:0,target_section:TEXT:1:0,path:TEXT:1:0,request_id:TEXT:1:0,trace_id:TEXT:0:0,payload_json:TEXT:1:0,created_at:TEXT:1:0'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('console_page_events')
      WHERE dflt_value IS NOT NULL)=2
      AND (SELECT dflt_value FROM pragma_table_info('console_page_events')
           WHERE name='event_type')=quote('console.page_viewed')
      AND (SELECT dflt_value FROM pragma_table_info('console_page_events')
           WHERE name='payload_json')=quote('{}')
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_foreign_key_list('console_page_events'))=3
      AND (SELECT COUNT(*) FROM pragma_foreign_key_list('console_page_events')
           WHERE "table"='tenants' AND "from"='tenant_id' AND "to"='tenant_id'
             AND on_update='NO ACTION' AND on_delete='NO ACTION' AND "match"='NONE')=1
      AND (SELECT COUNT(*) FROM pragma_foreign_key_list('console_page_events')
           WHERE "table"='users' AND "from"='user_id' AND "to"='user_id'
             AND on_update='NO ACTION' AND on_delete='NO ACTION' AND "match"='NONE')=1
      AND (SELECT COUNT(*) FROM pragma_foreign_key_list('console_page_events')
           WHERE "table"='sessions' AND "from"='session_id' AND "to"='session_id'
             AND on_update='NO ACTION' AND on_delete='NO ACTION' AND "match"='NONE')=1
    THEN 0 ELSE 1 END
  + CASE WHEN instr(replace(replace(lower((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='console_page_events')),char(10),''),' ',''),
      'event_typein(''console.page_viewed'',''dashboard.viewed'',''migration.guide_viewed'',''help.competitor_viewed'',''status.viewed''))')>0
    THEN 0 ELSE 1 END
  -- 三个普通索引的名称、唯一性、partial 属性、列顺序与 DESC 方向全部冻结。
  + CASE WHEN (SELECT COUNT(*) FROM pragma_index_list('console_page_events'))=4
      AND (SELECT COUNT(*) FROM pragma_index_list('console_page_events')
           WHERE origin='c' AND "unique"=0 AND partial=0
             AND name IN (
               'idx_console_page_events_tenant_time',
               'idx_console_page_events_user_time',
               'idx_console_page_events_item_time'
             ))=3
      AND (SELECT COUNT(*) FROM pragma_index_list('console_page_events')
           WHERE origin='c')=3
    THEN 0 ELSE 1 END
  + CASE WHEN (
      SELECT group_concat(name||':'||"desc"||':'||coll, ',') FROM (
        SELECT name,"desc",coll FROM pragma_index_xinfo('idx_console_page_events_tenant_time')
        WHERE "key"=1 ORDER BY seqno
      )
    )='tenant_id:0:BINARY,created_at:1:BINARY' THEN 0 ELSE 1 END
  + CASE WHEN (
      SELECT group_concat(name||':'||"desc"||':'||coll, ',') FROM (
        SELECT name,"desc",coll FROM pragma_index_xinfo('idx_console_page_events_user_time')
        WHERE "key"=1 ORDER BY seqno
      )
    )='tenant_id:0:BINARY,user_id:0:BINARY,created_at:1:BINARY' THEN 0 ELSE 1 END
  + CASE WHEN (
      SELECT group_concat(name||':'||"desc"||':'||coll, ',') FROM (
        SELECT name,"desc",coll FROM pragma_index_xinfo('idx_console_page_events_item_time')
        WHERE "key"=1 ORDER BY seqno
      )
    )='tenant_id:0:BINARY,navigation_item_id:0:BINARY,created_at:1:BINARY' THEN 0 ELSE 1 END
  -- 除表本身和三个索引外，不允许任何触发器、视图、外键表或其他对象依赖事件表。
  + (SELECT COUNT(*) FROM sqlite_schema object
     WHERE object.sql IS NOT NULL
       AND instr(lower(object.sql),'console_page_events')>0
       AND object.name NOT IN (
         'console_page_events',
         'idx_console_page_events_tenant_time',
         'idx_console_page_events_user_time',
         'idx_console_page_events_item_time'
       ))
  -- 0057 的全部持久对象和两个换表暂存名必须尚未存在；禁止覆盖未知对象。
  + (SELECT COUNT(*) FROM sqlite_schema
     WHERE name IN (
       'role_dashboard_configs',
       'idx_role_dashboard_configs_tenant_status',
       'trg_role_dashboard_configs_insert_prd065',
       'trg_role_dashboard_configs_update_prd065',
       'trg_role_dashboard_configs_delete_prd065',
       'trg_role_dashboard_configs_rowid_insert_prd065',
       'trg_tenants_role_dashboard_initialize_prd065',
       'console_page_events_prd065_new',
       'console_page_events_prd065_old'
     ))
  + (SELECT COUNT(*) FROM sqlite_schema object
     WHERE object.sql IS NOT NULL
       AND instr(lower(object.sql),'role_dashboard_configs')>0)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd065_up_guard;

-- 角色首页配置只保存产品允许的固定视图集合，不保存用户偏好或权限快照。
-- 复合外键使用延迟语义：同一 tenant INSERT 上的 PRD-059 与 PRD-065 两个 AFTER trigger
-- 执行顺序不可靠，只要求事务提交前六角色和六配置同时完整。
CREATE TABLE role_dashboard_configs (
  role_dashboard_config_id TEXT PRIMARY KEY CHECK (
    typeof(role_dashboard_config_id)='text'
    AND length(role_dashboard_config_id)=36
    AND substr(role_dashboard_config_id,1,4)='rdc_'
    AND substr(role_dashboard_config_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  role_code TEXT NOT NULL CHECK (
    role_code IN (
      'admin','developer','viewer','finance_admin','security_admin','ops_admin'
    )
  ),
  default_view_code TEXT NOT NULL CHECK (
    default_view_code IN ('executive','finance','security','developer','operations')
  ),
  allowed_view_codes_json TEXT NOT NULL CHECK (
    json_valid(allowed_view_codes_json)
    AND json_type(allowed_view_codes_json)='array'
    AND json_array_length(allowed_view_codes_json) BETWEEN 1 AND 5
    AND length(CAST(allowed_view_codes_json AS BLOB))<=256
  ),
  config_revision INTEGER NOT NULL CHECK (
    typeof(config_revision)='integer' AND config_revision>=1
  ),
  status TEXT NOT NULL CHECK (status IN ('active','disabled')),
  created_at TEXT NOT NULL CHECK (
    length(created_at)=20 AND created_at GLOB '????-??-??T??:??:??Z'
    AND unixepoch(created_at) IS NOT NULL
  ),
  updated_at TEXT NOT NULL CHECK (
    length(updated_at)=20 AND updated_at GLOB '????-??-??T??:??:??Z'
    AND unixepoch(updated_at) IS NOT NULL
  ),
  UNIQUE (tenant_id,role_code),
  FOREIGN KEY (tenant_id,role_code)
    REFERENCES roles(tenant_id,code) DEFERRABLE INITIALLY DEFERRED,
  CHECK (unixepoch(updated_at)>=unixepoch(created_at))
);

CREATE INDEX idx_role_dashboard_configs_tenant_status
  ON role_dashboard_configs(tenant_id,status,role_code);

-- INSERT 仅用于迁移 bootstrap 和新租户固定目录。它故意不查询 roles：否则两个 tenant
-- AFTER trigger 会隐含执行顺序；角色存在性只由延迟复合 FK 在事务提交时确认。
CREATE TRIGGER trg_role_dashboard_configs_insert_prd065
BEFORE INSERT ON role_dashboard_configs FOR EACH ROW
WHEN
  typeof(NEW.role_dashboard_config_id)!='text'
  OR length(NEW.role_dashboard_config_id)!=36
  OR substr(NEW.role_dashboard_config_id,1,4)!='rdc_'
  OR substr(NEW.role_dashboard_config_id,5) GLOB '*[^0-9a-f]*'
  OR NEW.role_code NOT IN (
    'admin','developer','viewer','finance_admin','security_admin','ops_admin'
  )
  OR NEW.default_view_code IS NOT CASE NEW.role_code
    WHEN 'admin' THEN 'executive'
    WHEN 'developer' THEN 'developer'
    WHEN 'viewer' THEN 'executive'
    WHEN 'finance_admin' THEN 'finance'
    WHEN 'security_admin' THEN 'security'
    WHEN 'ops_admin' THEN 'operations'
  END
  OR NEW.allowed_view_codes_json IS NOT CASE NEW.role_code
    WHEN 'admin' THEN '["executive","finance","security","developer","operations"]'
    WHEN 'developer' THEN '["developer","operations","executive"]'
    WHEN 'viewer' THEN '["executive"]'
    WHEN 'finance_admin' THEN '["finance","executive"]'
    WHEN 'security_admin' THEN '["security","executive"]'
    WHEN 'ops_admin' THEN '["operations","executive"]'
  END
  OR typeof(NEW.config_revision)!='integer' OR NEW.config_revision!=1
  OR NEW.status!='active'
  OR NEW.created_at IS NOT NEW.updated_at
  OR EXISTS (
    SELECT 1 FROM role_dashboard_configs current
    WHERE current.role_dashboard_config_id=NEW.role_dashboard_config_id
       OR (current.tenant_id=NEW.tenant_id AND current.role_code=NEW.role_code)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'role_dashboard_config_invalid'); END;

-- 运行期只允许受控配置演进：owner/ID/rowid/创建时间不可变，revision 精确 +1，
-- updated_at 必须前进且至少一项配置真实变化；JSON 固定枚举、去重并包含 default。
CREATE TRIGGER trg_role_dashboard_configs_update_prd065
BEFORE UPDATE ON role_dashboard_configs FOR EACH ROW
WHEN
  NEW.rowid!=OLD.rowid
  OR NEW.role_dashboard_config_id IS NOT OLD.role_dashboard_config_id
  OR NEW.tenant_id IS NOT OLD.tenant_id
  OR NEW.role_code IS NOT OLD.role_code
  OR NEW.created_at IS NOT OLD.created_at
  OR typeof(NEW.config_revision)!='integer'
  OR NEW.config_revision!=OLD.config_revision+1
  OR unixepoch(NEW.updated_at)<=unixepoch(OLD.updated_at)
  OR (
    NEW.default_view_code IS OLD.default_view_code
    AND NEW.allowed_view_codes_json IS OLD.allowed_view_codes_json
    AND NEW.status IS OLD.status
  )
  OR CASE
    WHEN json_valid(NEW.allowed_view_codes_json) THEN CASE
      WHEN json_type(NEW.allowed_view_codes_json)='array'
        AND json_array_length(NEW.allowed_view_codes_json) BETWEEN 1 AND 5
      THEN
        EXISTS (
          SELECT 1 FROM json_each(NEW.allowed_view_codes_json) item
          WHERE item.type!='text'
             OR item.value NOT IN (
               'executive','finance','security','developer','operations'
             )
        )
        OR (SELECT COUNT(*) FROM json_each(NEW.allowed_view_codes_json))
           !=(SELECT COUNT(DISTINCT value)
              FROM json_each(NEW.allowed_view_codes_json))
        OR NOT EXISTS (
          SELECT 1 FROM json_each(NEW.allowed_view_codes_json) item
          WHERE item.type='text' AND item.value=NEW.default_view_code
        )
      ELSE 1
    END
    ELSE 1
  END
BEGIN SELECT RAISE(ABORT,'role_dashboard_config_invalid'); END;

-- 配置不能通过 DELETE 或 INSERT OR REPLACE 绕过 revision 语义；后续迁移必须先显式移除门禁。
CREATE TRIGGER trg_role_dashboard_configs_delete_prd065
BEFORE DELETE ON role_dashboard_configs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'role_dashboard_config_immutable'); END;

CREATE TRIGGER trg_role_dashboard_configs_rowid_insert_prd065
AFTER INSERT ON role_dashboard_configs FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'role_dashboard_config_immutable'); END;

-- 为所有存量 tenant 原子生成六条 canonical revision 1 配置。这里从固定目录生成，
-- 不借用 roles 查询决定产品默认值；前置角色计数与最终复合 FK 共同证明 owner 完整。
-- 执行固定目录回填时只把 tenant_id 作为 owner 投影，不读取其他租户角色行。
INSERT INTO role_dashboard_configs(
  role_dashboard_config_id,tenant_id,role_code,default_view_code,
  allowed_view_codes_json,config_revision,status,created_at,updated_at
)
SELECT
  'rdc_'||lower(hex(randomblob(16))),tenant.tenant_id,catalog.role_code,
  catalog.default_view_code,catalog.allowed_view_codes_json,1,'active',
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant
CROSS JOIN (
  SELECT 'admin' AS role_code,'executive' AS default_view_code,
         '["executive","finance","security","developer","operations"]' AS allowed_view_codes_json
  UNION ALL SELECT 'developer','developer','["developer","operations","executive"]'
  UNION ALL SELECT 'viewer','executive','["executive"]'
  UNION ALL SELECT 'finance_admin','finance','["finance","executive"]'
  UNION ALL SELECT 'security_admin','security','["security","executive"]'
  UNION ALL SELECT 'ops_admin','operations','["operations","executive"]'
) catalog;

-- 新 tenant 与六条首页配置同语句事务初始化。该触发器同样不读取 roles，避免依赖
-- PRD-059 RBAC trigger 与本 trigger 的创建顺序或执行顺序。
CREATE TRIGGER trg_tenants_role_dashboard_initialize_prd065
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 新租户只投影固定六角色配置，延迟复合 FK 在事务提交时确认对应角色已初始化。
  INSERT INTO role_dashboard_configs(
    role_dashboard_config_id,tenant_id,role_code,default_view_code,
    allowed_view_codes_json,config_revision,status,created_at,updated_at
  )
  SELECT
    'rdc_'||lower(hex(randomblob(16))),NEW.tenant_id,catalog.role_code,
    catalog.default_view_code,catalog.allowed_view_codes_json,1,'active',
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM (
    SELECT 'admin' AS role_code,'executive' AS default_view_code,
           '["executive","finance","security","developer","operations"]' AS allowed_view_codes_json
    UNION ALL SELECT 'developer','developer','["developer","operations","executive"]'
    UNION ALL SELECT 'viewer','executive','["executive"]'
    UNION ALL SELECT 'finance_admin','finance','["finance","executive"]'
    UNION ALL SELECT 'security_admin','security','["security","executive"]'
    UNION ALL SELECT 'ops_admin','operations','["operations","executive"]'
  ) catalog;
END;

-- 事件表只增加 dashboard.role_viewed；显式复制 rowid 与全部十二列，避免审计事实重排。
CREATE TABLE console_page_events_prd065_new (
  event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  session_id TEXT,
  event_type TEXT NOT NULL DEFAULT 'console.page_viewed' CHECK (
    event_type IN (
      'console.page_viewed',
      'dashboard.viewed',
      'migration.guide_viewed',
      'help.competitor_viewed',
      'status.viewed',
      'dashboard.role_viewed'
    )
  ),
  navigation_item_id TEXT NOT NULL,
  target_section TEXT NOT NULL,
  path TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (user_id) REFERENCES users(user_id),
  FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

-- 执行十二列和 rowid 的显式复制，避免 INSERT 默认值改变历史审计事实。
INSERT INTO console_page_events_prd065_new(
  rowid,event_id,tenant_id,user_id,session_id,event_type,navigation_item_id,
  target_section,path,request_id,trace_id,payload_json,created_at
)
SELECT
  rowid,event_id,tenant_id,user_id,session_id,event_type,navigation_item_id,
  target_section,path,request_id,trace_id,payload_json,created_at
FROM console_page_events;

-- 行数与双向 EXCEPT 同时为零，才能证明 rowid 和十二列没有任何静默转换。
DROP TABLE IF EXISTS temp.prd065_event_copy_guard;
CREATE TEMP TABLE prd065_event_copy_guard (
  issue_count INTEGER NOT NULL
    CONSTRAINT prd065_event_copy_ok CHECK (issue_count = 0)
);
-- 执行行数与双向差集断言，通过后才允许替换正式事件表。
INSERT INTO prd065_event_copy_guard(issue_count)
SELECT
  ABS((SELECT COUNT(*) FROM console_page_events)
      -(SELECT COUNT(*) FROM console_page_events_prd065_new))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,event_id,tenant_id,user_id,session_id,event_type,
             navigation_item_id,target_section,path,request_id,trace_id,
             payload_json,created_at
      FROM console_page_events
      EXCEPT
      SELECT rowid,event_id,tenant_id,user_id,session_id,event_type,
             navigation_item_id,target_section,path,request_id,trace_id,
             payload_json,created_at
      FROM console_page_events_prd065_new
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,event_id,tenant_id,user_id,session_id,event_type,
             navigation_item_id,target_section,path,request_id,trace_id,
             payload_json,created_at
      FROM console_page_events_prd065_new
      EXCEPT
      SELECT rowid,event_id,tenant_id,user_id,session_id,event_type,
             navigation_item_id,target_section,path,request_id,trace_id,
             payload_json,created_at
      FROM console_page_events
    ));
DROP TABLE prd065_event_copy_guard;

DROP TABLE console_page_events;
-- 复制守卫已经通过，此时才恢复稳定表名供应用继续访问。
ALTER TABLE console_page_events_prd065_new RENAME TO console_page_events;
CREATE INDEX idx_console_page_events_tenant_time
  ON console_page_events(tenant_id,created_at DESC);
CREATE INDEX idx_console_page_events_user_time
  ON console_page_events(tenant_id,user_id,created_at DESC);
CREATE INDEX idx_console_page_events_item_time
  ON console_page_events(tenant_id,navigation_item_id,created_at DESC);

-- 提交前再次验证 canonical 配置、对象集合、事件结构和全库完整性。
DROP TABLE IF EXISTS temp.prd065_up_final_guard;
CREATE TEMP TABLE prd065_up_final_guard (
  issue_count INTEGER NOT NULL
    CONSTRAINT prd065_up_final_state_ok CHECK (issue_count = 0)
);
-- 提交前执行最终零值断言，把配置、事件、索引和外键异常整体回滚。
INSERT INTO prd065_up_final_guard(issue_count)
SELECT
  CASE WHEN (
      SELECT group_concat(name||':'||upper(type)||':'||"notnull"||':'||pk, ',')
      FROM (
        SELECT name,type,"notnull",pk
        FROM pragma_table_info('role_dashboard_configs') ORDER BY cid
      )
    )='role_dashboard_config_id:TEXT:0:1,tenant_id:TEXT:1:0,role_code:TEXT:1:0,default_view_code:TEXT:1:0,allowed_view_codes_json:TEXT:1:0,config_revision:INTEGER:1:0,status:TEXT:1:0,created_at:TEXT:1:0,updated_at:TEXT:1:0'
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_index_list('role_dashboard_configs'))=3
      AND (SELECT COUNT(*) FROM pragma_index_list('role_dashboard_configs')
           WHERE name='idx_role_dashboard_configs_tenant_status'
             AND "unique"=0 AND origin='c' AND partial=0)=1
      AND (SELECT COUNT(*) FROM pragma_index_list('role_dashboard_configs')
           WHERE "unique"=1 AND origin IN ('pk','u') AND partial=0)=2
    THEN 0 ELSE 1 END
  + CASE WHEN (
      SELECT group_concat(name||':'||"desc"||':'||coll, ',') FROM (
        SELECT name,"desc",coll
        FROM pragma_index_xinfo('idx_role_dashboard_configs_tenant_status')
        WHERE "key"=1 ORDER BY seqno
      )
    )='tenant_id:0:BINARY,status:0:BINARY,role_code:0:BINARY' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_foreign_key_list('role_dashboard_configs'))=2
      AND (SELECT COUNT(*) FROM pragma_foreign_key_list('role_dashboard_configs')
           WHERE "table"='roles' AND seq=0 AND "from"='tenant_id' AND "to"='tenant_id')=1
      AND (SELECT COUNT(*) FROM pragma_foreign_key_list('role_dashboard_configs')
           WHERE "table"='roles' AND seq=1 AND "from"='role_code' AND "to"='code')=1
      AND instr(replace(replace(lower((SELECT sql FROM sqlite_schema
          WHERE type='table' AND name='role_dashboard_configs')),char(10),''),' ',''),
          'deferrableinitiallydeferred')>0
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name IN (
        'trg_role_dashboard_configs_insert_prd065',
        'trg_role_dashboard_configs_update_prd065',
        'trg_role_dashboard_configs_delete_prd065',
        'trg_role_dashboard_configs_rowid_insert_prd065',
        'trg_tenants_role_dashboard_initialize_prd065'
      ))=5 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema object
     WHERE object.sql IS NOT NULL
       AND instr(lower(object.sql),'role_dashboard_configs')>0
       AND object.name NOT IN (
         'role_dashboard_configs',
         'idx_role_dashboard_configs_tenant_status',
         'trg_role_dashboard_configs_insert_prd065',
         'trg_role_dashboard_configs_update_prd065',
         'trg_role_dashboard_configs_delete_prd065',
         'trg_role_dashboard_configs_rowid_insert_prd065',
         'trg_tenants_role_dashboard_initialize_prd065'
       ))
  + ABS((SELECT COUNT(*) FROM role_dashboard_configs)
      -(SELECT COUNT(*)*6 FROM tenants))
  + (SELECT COUNT(*) FROM tenants tenant
     WHERE (SELECT COUNT(*) FROM role_dashboard_configs config
            WHERE config.tenant_id=tenant.tenant_id)!=6)
  + (SELECT COUNT(*) FROM role_dashboard_configs config
     WHERE config.config_revision!=1 OR config.status!='active'
        OR config.created_at IS NOT config.updated_at
        OR config.default_view_code IS NOT CASE config.role_code
          WHEN 'admin' THEN 'executive'
          WHEN 'developer' THEN 'developer'
          WHEN 'viewer' THEN 'executive'
          WHEN 'finance_admin' THEN 'finance'
          WHEN 'security_admin' THEN 'security'
          WHEN 'ops_admin' THEN 'operations'
        END
        OR config.allowed_view_codes_json IS NOT CASE config.role_code
          WHEN 'admin' THEN '["executive","finance","security","developer","operations"]'
          WHEN 'developer' THEN '["developer","operations","executive"]'
          WHEN 'viewer' THEN '["executive"]'
          WHEN 'finance_admin' THEN '["finance","executive"]'
          WHEN 'security_admin' THEN '["security","executive"]'
          WHEN 'ops_admin' THEN '["operations","executive"]'
        END)
  + CASE WHEN (
      SELECT group_concat(name||':'||upper(type)||':'||"notnull"||':'||pk, ',')
      FROM (
        SELECT name,type,"notnull",pk
        FROM pragma_table_info('console_page_events') ORDER BY cid
      )
    )='event_id:TEXT:0:1,tenant_id:TEXT:1:0,user_id:TEXT:1:0,session_id:TEXT:0:0,event_type:TEXT:1:0,navigation_item_id:TEXT:1:0,target_section:TEXT:1:0,path:TEXT:1:0,request_id:TEXT:1:0,trace_id:TEXT:0:0,payload_json:TEXT:1:0,created_at:TEXT:1:0'
    THEN 0 ELSE 1 END
  + CASE WHEN instr(replace(replace(lower((SELECT sql FROM sqlite_schema
      WHERE type='table' AND name='console_page_events')),char(10),''),' ',''),
      'event_typein(''console.page_viewed'',''dashboard.viewed'',''migration.guide_viewed'',''help.competitor_viewed'',''status.viewed'',''dashboard.role_viewed''))')>0
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_index_list('console_page_events'))=4
      AND (SELECT COUNT(*) FROM pragma_index_list('console_page_events')
           WHERE origin='c' AND "unique"=0 AND partial=0
             AND name IN (
               'idx_console_page_events_tenant_time',
               'idx_console_page_events_user_time',
               'idx_console_page_events_item_time'
             ))=3
      AND (SELECT COUNT(*) FROM pragma_index_list('console_page_events')
           WHERE origin='c')=3
    THEN 0 ELSE 1 END
  + CASE WHEN (
      SELECT group_concat(name||':'||"desc"||':'||coll, ',') FROM (
        SELECT name,"desc",coll FROM pragma_index_xinfo('idx_console_page_events_tenant_time')
        WHERE "key"=1 ORDER BY seqno
      )
    )='tenant_id:0:BINARY,created_at:1:BINARY' THEN 0 ELSE 1 END
  + CASE WHEN (
      SELECT group_concat(name||':'||"desc"||':'||coll, ',') FROM (
        SELECT name,"desc",coll FROM pragma_index_xinfo('idx_console_page_events_user_time')
        WHERE "key"=1 ORDER BY seqno
      )
    )='tenant_id:0:BINARY,user_id:0:BINARY,created_at:1:BINARY' THEN 0 ELSE 1 END
  + CASE WHEN (
      SELECT group_concat(name||':'||"desc"||':'||coll, ',') FROM (
        SELECT name,"desc",coll FROM pragma_index_xinfo('idx_console_page_events_item_time')
        WHERE "key"=1 ORDER BY seqno
      )
    )='tenant_id:0:BINARY,navigation_item_id:0:BINARY,created_at:1:BINARY' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd065_up_final_guard;

COMMIT;
PRAGMA foreign_keys = ON;
