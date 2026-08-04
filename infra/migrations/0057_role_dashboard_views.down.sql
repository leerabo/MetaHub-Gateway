-- PRD-065 down：移除角色首页配置，并把 dashboard.role_viewed 降级为 dashboard.viewed。
-- role_dashboard_configs 是未来可演进的租户配置；任何 revision、状态、视图或时间戳已变化时，
-- 回滚都必须在首项持久 DDL 前失败，不能静默删除用户或运维产生的配置事实。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- 清理仅属于当前连接的旧临时守卫，不删除或覆盖任何持久业务对象。
DROP TABLE IF EXISTS temp.prd065_down_guard;
CREATE TEMP TABLE prd065_down_guard (
  issue_count INTEGER NOT NULL
    CONSTRAINT prd065_down_preconditions_ok CHECK (issue_count = 0)
);
-- 栈顶、配置 canonical 状态、对象签名、未知依赖与数据库完整性一次性核验。
INSERT INTO prd065_down_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
      WHERE version='0057' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations
     WHERE status='applied' AND version>'0057')
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='table' AND name IN (
        'tenants','roles','role_dashboard_configs','console_page_events'
      ))=4 THEN 0 ELSE 1 END
  -- 配置表必须保持九列、复合延迟外键和一个普通检索索引。
  + CASE WHEN (
      SELECT group_concat(name||':'||upper(type)||':'||"notnull"||':'||pk, ',')
      FROM (
        SELECT name,type,"notnull",pk
        FROM pragma_table_info('role_dashboard_configs') ORDER BY cid
      )
    )='role_dashboard_config_id:TEXT:0:1,tenant_id:TEXT:1:0,role_code:TEXT:1:0,default_view_code:TEXT:1:0,allowed_view_codes_json:TEXT:1:0,config_revision:INTEGER:1:0,status:TEXT:1:0,created_at:TEXT:1:0,updated_at:TEXT:1:0'
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_table_info('role_dashboard_configs')
     WHERE dflt_value IS NOT NULL)
  + CASE WHEN (SELECT COUNT(*) FROM pragma_foreign_key_list('role_dashboard_configs'))=2
      AND (SELECT COUNT(*) FROM pragma_foreign_key_list('role_dashboard_configs')
           WHERE "table"='roles' AND seq=0 AND "from"='tenant_id' AND "to"='tenant_id'
             AND on_update='NO ACTION' AND on_delete='NO ACTION' AND "match"='NONE')=1
      AND (SELECT COUNT(*) FROM pragma_foreign_key_list('role_dashboard_configs')
           WHERE "table"='roles' AND seq=1 AND "from"='role_code' AND "to"='code'
             AND on_update='NO ACTION' AND on_delete='NO ACTION' AND "match"='NONE')=1
      AND instr(replace(replace(lower((SELECT sql FROM sqlite_schema
          WHERE type='table' AND name='role_dashboard_configs')),char(10),''),' ',''),
          'deferrableinitiallydeferred')>0
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
  -- 四个表级门禁与一个 tenant 初始化触发器必须精确存在；其他依赖会在换表/删表前阻断。
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND tbl_name='role_dashboard_configs'
        AND name IN (
          'trg_role_dashboard_configs_insert_prd065',
          'trg_role_dashboard_configs_update_prd065',
          'trg_role_dashboard_configs_delete_prd065',
          'trg_role_dashboard_configs_rowid_insert_prd065'
        ))=4
      AND (SELECT COUNT(*) FROM sqlite_schema
           WHERE type='trigger' AND tbl_name='role_dashboard_configs')=4
      AND (SELECT COUNT(*) FROM sqlite_schema
           WHERE type='trigger'
             AND name='trg_tenants_role_dashboard_initialize_prd065'
             AND tbl_name='tenants')=1
    THEN 0 ELSE 1 END
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
  -- 每个 tenant 必须仍是六条初始配置；任何受控更新都会改变 revision/updated_at 并阻断 down。
  + ABS((SELECT COUNT(*) FROM role_dashboard_configs)
      -(SELECT COUNT(*)*6 FROM tenants))
  + (SELECT COUNT(*) FROM tenants tenant
     WHERE (SELECT COUNT(*) FROM role_dashboard_configs config
            WHERE config.tenant_id=tenant.tenant_id)!=6)
  + (SELECT COUNT(*) FROM role_dashboard_configs config
     WHERE typeof(config.role_dashboard_config_id)!='text'
        OR length(config.role_dashboard_config_id)!=36
        OR substr(config.role_dashboard_config_id,1,4)!='rdc_'
        OR substr(config.role_dashboard_config_id,5) GLOB '*[^0-9a-f]*'
        OR typeof(config.config_revision)!='integer'
        OR config.config_revision!=1
        OR config.status!='active'
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
  -- 事件表必须仍为十二列、三外键、六事件和三个普通索引。
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
             AND on_update='NO ACTION' AND on_delete='NO ACTION' AND match='NONE')=1
      AND (SELECT COUNT(*) FROM pragma_foreign_key_list('console_page_events')
           WHERE "table"='users' AND "from"='user_id' AND "to"='user_id'
             AND on_update='NO ACTION' AND on_delete='NO ACTION' AND match='NONE')=1
      AND (SELECT COUNT(*) FROM pragma_foreign_key_list('console_page_events')
           WHERE "table"='sessions' AND "from"='session_id' AND "to"='session_id'
             AND on_update='NO ACTION' AND on_delete='NO ACTION' AND match='NONE')=1
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
  + (SELECT COUNT(*) FROM sqlite_schema object
     WHERE object.sql IS NOT NULL
       AND instr(lower(object.sql),'console_page_events')>0
       AND object.name NOT IN (
         'console_page_events',
         'idx_console_page_events_tenant_time',
         'idx_console_page_events_user_time',
         'idx_console_page_events_item_time'
       ))
  -- 两个换表暂存名不得被未知对象占用。
  + (SELECT COUNT(*) FROM sqlite_schema
     WHERE name IN (
       'console_page_events_prd065_new','console_page_events_prd065_old'
     ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd065_down_guard;

-- 旧结构保留 0053 的五种事件。dashboard.role_viewed 不删除，而是降级为语义最接近的
-- dashboard.viewed；payload_json、request_id、trace_id 和其余字段全部原样保留。
CREATE TABLE console_page_events_prd065_old (
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
      'status.viewed'
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

-- 执行带单一事件类型映射的逐列复制，其他审计字段和 rowid 必须保持原值。
INSERT INTO console_page_events_prd065_old(
  rowid,event_id,tenant_id,user_id,session_id,event_type,navigation_item_id,
  target_section,path,request_id,trace_id,payload_json,created_at
)
SELECT
  rowid,event_id,tenant_id,user_id,session_id,
  CASE WHEN event_type='dashboard.role_viewed'
       THEN 'dashboard.viewed' ELSE event_type END,
  navigation_item_id,target_section,path,request_id,trace_id,payload_json,created_at
FROM console_page_events;

-- 降级只允许 event_type 发生上述单一映射；逐列比较使用 IS NOT 正确覆盖 NULL。
DROP TABLE IF EXISTS temp.prd065_event_down_copy_guard;
CREATE TEMP TABLE prd065_event_down_copy_guard (
  issue_count INTEGER NOT NULL
    CONSTRAINT prd065_event_down_copy_ok CHECK (issue_count = 0)
);
-- 执行逐行映射断言，避免降级时误改 payload、链路 ID 或 tenant owner。
INSERT INTO prd065_event_down_copy_guard(issue_count)
SELECT
  ABS((SELECT COUNT(*) FROM console_page_events)
      -(SELECT COUNT(*) FROM console_page_events_prd065_old))
  + (SELECT COUNT(*)
     FROM console_page_events source
     LEFT JOIN console_page_events_prd065_old target ON target.rowid=source.rowid
     WHERE target.rowid IS NULL
        OR target.event_id IS NOT source.event_id
        OR target.tenant_id IS NOT source.tenant_id
        OR target.user_id IS NOT source.user_id
        OR target.session_id IS NOT source.session_id
        OR target.event_type IS NOT CASE
             WHEN source.event_type='dashboard.role_viewed'
             THEN 'dashboard.viewed' ELSE source.event_type END
        OR target.navigation_item_id IS NOT source.navigation_item_id
        OR target.target_section IS NOT source.target_section
        OR target.path IS NOT source.path
        OR target.request_id IS NOT source.request_id
        OR target.trace_id IS NOT source.trace_id
        OR target.payload_json IS NOT source.payload_json
        OR target.created_at IS NOT source.created_at);
DROP TABLE prd065_event_down_copy_guard;

DROP TABLE console_page_events;
-- 复制断言通过后才恢复正式事件表名，失败事务不会暴露半成品结构。
ALTER TABLE console_page_events_prd065_old RENAME TO console_page_events;
CREATE INDEX idx_console_page_events_tenant_time
  ON console_page_events(tenant_id,created_at DESC);
CREATE INDEX idx_console_page_events_user_time
  ON console_page_events(tenant_id,user_id,created_at DESC);
CREATE INDEX idx_console_page_events_item_time
  ON console_page_events(tenant_id,navigation_item_id,created_at DESC);

-- 先移除跨表 tenant trigger，再显式移除配置表门禁和索引；不依赖 DROP TABLE 的隐式清理。
DROP TRIGGER trg_tenants_role_dashboard_initialize_prd065;
DROP TRIGGER trg_role_dashboard_configs_rowid_insert_prd065;
DROP TRIGGER trg_role_dashboard_configs_delete_prd065;
DROP TRIGGER trg_role_dashboard_configs_update_prd065;
DROP TRIGGER trg_role_dashboard_configs_insert_prd065;
DROP INDEX idx_role_dashboard_configs_tenant_status;
DROP TABLE role_dashboard_configs;

-- 提交前证明 PRD-065 对象全部消失、事件枚举恢复为五种且数据库仍完整。
DROP TABLE IF EXISTS temp.prd065_down_final_guard;
CREATE TEMP TABLE prd065_down_final_guard (
  issue_count INTEGER NOT NULL
    CONSTRAINT prd065_down_final_state_ok CHECK (issue_count = 0)
);
-- 提交前执行降级终态断言，确认 PRD-065 对象消失且旧事件合同完整恢复。
INSERT INTO prd065_down_final_guard(issue_count)
SELECT
  (SELECT COUNT(*) FROM sqlite_schema
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
      'event_typein(''console.page_viewed'',''dashboard.viewed'',''migration.guide_viewed'',''help.competitor_viewed'',''status.viewed''))')>0
      AND instr(lower((SELECT sql FROM sqlite_schema
          WHERE type='table' AND name='console_page_events')),
          'dashboard.role_viewed')=0
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
DROP TABLE prd065_down_final_guard;

COMMIT;
PRAGMA foreign_keys = ON;
