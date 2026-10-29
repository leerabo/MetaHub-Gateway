-- PRD-070 回滚：仅在没有会话风险业务事实、治理历史和后续 schema 依赖时恢复 0061。
-- 所有失败关闭门禁均位于首项持久 DDL 前；回滚绝不以删除审计、Webhook、权限历史或
-- 会话风险证据为代价。SQLite 换表期间暂时关闭即时 FK，提交前统一执行完整性复核。
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

-- 只认领仍精确匹配 revision 7 产品矩阵的 Admin/SecurityAdmin。关系全集和 hash
-- 必须同时相等，避免伪造角色名称或 hash 后借回滚静默移除租户自定义授权。
CREATE TEMP TABLE prd070_roles_to_contract AS
SELECT role.role_id,role.tenant_id,role.code,role.permission_revision
FROM roles role
WHERE role.code IN ('admin','security_admin')
  AND role.matrix_sha256=CASE role.code
    WHEN 'admin' THEN '792e003e6c0a188c124ef71ce7ab31658e7ef43a6dadaf6b83c339dfff2caf7f'
    WHEN 'security_admin' THEN 'de4701bc3ce4305c129704c69212d8a520ea5942a87c6119a36542e01784b78c' END
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

-- 业务门禁以稳定错误码聚合所有不可逆事实；任一计数非零即在首项持久 DDL 前回滚事务。
DROP TABLE IF EXISTS temp.prd070_down_business_guard;
DROP TRIGGER IF EXISTS temp.trg_prd070_down_business_guard;
CREATE TEMP TABLE prd070_down_business_guard (invalid_count INTEGER NOT NULL);
CREATE TEMP TRIGGER trg_prd070_down_business_guard
BEFORE INSERT ON prd070_down_business_guard FOR EACH ROW
WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd070_down_business_facts_present'); END;
-- 把所有不可逆业务事实汇总写入失败触发器，确保首项持久 DDL 前得到稳定错误。
INSERT INTO prd070_down_business_guard(invalid_count)
SELECT
  (SELECT COUNT(*) FROM session_risk_scores)
  + (SELECT COUNT(*) FROM session_risk_observations)
  + (SELECT COUNT(*) FROM session_risk_freezes)
  + (SELECT COUNT(*) FROM session_risk_actions)
  + (SELECT COUNT(*) FROM session_risk_events)
  -- sync_session 结果和事件无法无损投影回 0061 的分类枚举，必须作为业务事实保留。
  + (SELECT COUNT(*) FROM intent_risk_results WHERE classification_mode='sync_session')
  + (SELECT COUNT(*) FROM intent_risk_events WHERE classification_mode='sync_session')
  -- RequestLog 只要使用任一 0062 新枚举或会话摘要，就不能通过删列伪装成 0061 日志。
  + (SELECT COUNT(*) FROM request_logs
     WHERE session_risk_status!='not_applicable'
        OR session_risk_score_id IS NOT NULL OR session_risk_scope_type IS NOT NULL
        OR session_risk_state IS NOT NULL OR session_risk_reason_code IS NOT NULL
        OR intent_risk_schedule_status NOT IN (
          'disabled','queued','skipped','blocked','failed_closed'
        )
        OR (intent_risk_mode IS NOT NULL
            AND intent_risk_mode NOT IN ('async_light','sync_strong'))
        OR (intent_risk_action IS NOT NULL
            AND intent_risk_action NOT IN ('block','none'))
        OR (intent_risk_reason_code IS NOT NULL
            AND intent_risk_reason_code NOT IN (
              'queued','queue_full','worker_unavailable','classifier_failed',
              'result_store_unavailable','explicit_credential_extraction',
              'explicit_system_prompt_request','explicit_policy_bypass',
              'explicit_instruction_override'
            )))
  -- 管理动作审计和关联导出序列都是不可变治理事实；object_type 兜底覆盖损坏动作值。
  + (SELECT COUNT(*) FROM audit_logs
     WHERE action IN (
       'session_risk.freeze_session','session_risk.freeze_virtual_key',
       'session_risk.dismiss','session_risk.release_session',
       'session_risk.release_virtual_key'
     ) OR object_type='session_risk')
  -- Endpoint JSON 是主动订阅事实；JSON 精确匹配与保守文本兜底共同覆盖异常记录。
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
     WHERE (json_valid(endpoint.event_types_json) AND EXISTS (
       SELECT 1 FROM json_each(endpoint.event_types_json) item
       WHERE item.value='security.session_high_risk'
     )) OR lower(endpoint.event_types_json) LIKE '%security.session_high_risk%')
  + (SELECT COUNT(*) FROM webhook_deliveries
     WHERE event_type='security.session_high_risk')
  + (SELECT COUNT(*) FROM webhook_delivery_attempts attempt
     JOIN webhook_deliveries delivery
       ON delivery.tenant_id=attempt.tenant_id
      AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
     WHERE delivery.event_type='security.session_high_risk')
  + (SELECT COUNT(*) FROM webhook_dead_letter_events
     WHERE event_type='security.session_high_risk')
  -- 权限变更历史不可通过删除当前关系掩盖；文本兜底使损坏 JSON 同样失败关闭。
  + (SELECT COUNT(*) FROM role_permission_events event
     WHERE EXISTS (
       SELECT 1 FROM json_each(event.added_permissions_json) item
       WHERE item.value IN ('session_risk.read','session_risk.manage')
     ) OR EXISTS (
       SELECT 1 FROM json_each(event.removed_permissions_json) item
       WHERE item.value IN ('session_risk.read','session_risk.manage')
     ) OR lower(event.added_permissions_json) LIKE '%session_risk.read%'
       OR lower(event.added_permissions_json) LIKE '%session_risk.manage%'
       OR lower(event.removed_permissions_json) LIKE '%session_risk.read%'
       OR lower(event.removed_permissions_json) LIKE '%session_risk.manage%')
  -- 两项新权限只能由可证明 canonical 的角色持有；自定义角色持有时拒绝收缩目录。
  + (SELECT COUNT(*) FROM role_permissions relation
     JOIN permissions permission
       ON permission.tenant_id=relation.tenant_id
      AND permission.permission_id=relation.permission_id
     WHERE permission.code IN ('session_risk.read','session_risk.manage')
       AND NOT EXISTS (
         SELECT 1 FROM prd070_roles_to_contract source
         WHERE source.tenant_id=relation.tenant_id AND source.role_id=relation.role_id
       ));
DROP TRIGGER trg_prd070_down_business_guard;
DROP TABLE prd070_down_business_guard;

-- 结构门禁冻结 0062 栈顶、目录/投影、表形状以及所有未知后续引用。
DROP TABLE IF EXISTS temp.prd070_down_guard;
CREATE TEMP TABLE prd070_down_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd070_down_preflight_ok CHECK (issue_count=0)
);
-- 结构、目录和未知依赖必须同时匹配 0062 栈顶，禁止在后续 schema 上盲目降级。
INSERT INTO prd070_down_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
      WHERE version='0062' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations WHERE status='applied' AND version>'0062')
  + CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=59
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=7)=59
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=155
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*59 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=7)
          =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM roles role
     WHERE role.code='admin' AND NOT EXISTS (
       SELECT 1 FROM prd070_roles_to_contract source
       WHERE source.tenant_id=role.tenant_id AND source.role_id=role.role_id
     ))
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('request_logs'))=66
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='index'
           AND tbl_name='request_logs' AND name NOT LIKE 'sqlite_autoindex%')=24
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
           AND tbl_name='request_logs')=14 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('intent_risk_results'))=22
      AND (SELECT COUNT(*) FROM pragma_table_info('intent_risk_events'))=19
      AND (SELECT COUNT(*) FROM pragma_table_info('session_risk_scores'))=28
      AND (SELECT COUNT(*) FROM pragma_table_info('session_risk_observations'))=27
      AND (SELECT COUNT(*) FROM pragma_table_info('session_risk_freezes'))=19
      AND (SELECT COUNT(*) FROM pragma_table_info('session_risk_actions'))=19
      AND (SELECT COUNT(*) FROM pragma_table_info('session_risk_events'))=23
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('audit_logs'))=17
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='index'
           AND tbl_name='audit_logs' AND name NOT LIKE 'sqlite_autoindex%')=15
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
           AND tbl_name='audit_logs')=8 THEN 0 ELSE 1 END
  -- PRD-070 自有对象允许引用新表/列；其余对象命中关键标识即属于未知后续依赖。
  + (SELECT COUNT(*) FROM sqlite_schema object
     WHERE object.sql IS NOT NULL
       AND (
         instr(lower(object.sql),'session_risk')>0
         OR instr(lower(object.sql),'sync_session')>0
         OR instr(lower(object.sql),'session_risk_status')>0
       )
       AND object.name NOT IN (
         'session_risk_scores','session_risk_observations','session_risk_freezes',
         'session_risk_actions','session_risk_events','request_logs',
         'intent_risk_results','intent_risk_events','audit_logs'
       )
       AND object.tbl_name NOT IN (
         'session_risk_scores','session_risk_observations','session_risk_freezes',
         'session_risk_actions','session_risk_events','request_logs',
         'intent_risk_results','intent_risk_events','audit_logs'
       )
       AND object.name NOT LIKE '%prd070')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd070_down_guard;

-- 首项持久 DDL：先移除 0062 的组合 trigger，业务门禁已证明五个会话摘要列均为空默认值。
DROP TRIGGER trg_request_logs_intent_insert_prd069;
DROP TRIGGER trg_request_logs_intent_update_prd069;
-- 原因摘要最后写入但最先移除，避免剩余组合约束引用已删除的状态字段。
ALTER TABLE request_logs DROP COLUMN session_risk_reason_code;
-- 状态值只属于 0062 会话风险机，业务门禁已证明没有需要保留的非空事实。
ALTER TABLE request_logs DROP COLUMN session_risk_state;
-- scope 类型随会话摘要一起收缩，原始会话身份从未存入 RequestLog。
ALTER TABLE request_logs DROP COLUMN session_risk_scope_type;
-- score 关联只在五张新表为空时移除，不会造成安全事实失联。
ALTER TABLE request_logs DROP COLUMN session_risk_score_id;
-- 最后移除默认 not_applicable 状态列，精确恢复 0061 的 61 列合同。
ALTER TABLE request_logs DROP COLUMN session_risk_status;

-- 四个扩展枚举逐列恢复 0061 CHECK。先复制再删除临时列，既保留 rowid，也避免重建
-- RequestLog 时误伤已有 24 个索引和其他 12 个跨域 owner trigger。
ALTER TABLE request_logs RENAME COLUMN intent_risk_reason_code TO intent_risk_reason_code_prd070;
-- 重新创建 0061 原因目录，只接受业务门禁已证明可无损表示的旧值。
ALTER TABLE request_logs ADD COLUMN intent_risk_reason_code TEXT CHECK (
  intent_risk_reason_code IS NULL OR intent_risk_reason_code IN (
    'queued','queue_full','worker_unavailable','classifier_failed','result_store_unavailable',
    'explicit_credential_extraction','explicit_system_prompt_request',
    'explicit_policy_bypass','explicit_instruction_override'
  )
);
-- 逐值复制原因事实，不重新运行分类器或依据当前规则改写历史结果。
UPDATE request_logs SET intent_risk_reason_code=intent_risk_reason_code_prd070;
-- 新旧原因完全一致后删除迁移临时列，恢复单一权威字段。
ALTER TABLE request_logs DROP COLUMN intent_risk_reason_code_prd070;

-- 动作列先改为临时名，再按 0061 的 block/none 枚举精确恢复。
ALTER TABLE request_logs RENAME COLUMN intent_risk_action TO intent_risk_action_prd070;
-- 恢复旧动作 CHECK 前已证明不存在 allow/observe 等 0062 事实。
ALTER TABLE request_logs ADD COLUMN intent_risk_action TEXT
  CHECK (intent_risk_action IS NULL OR intent_risk_action IN ('block','none'));
-- 历史动作逐值复制，不能从分数或原因再次推导而改变审计语义。
UPDATE request_logs SET intent_risk_action=intent_risk_action_prd070;
-- 动作复制完成后删除临时列，保持 RequestLog 字段数精确回到 0061。
ALTER TABLE request_logs DROP COLUMN intent_risk_action_prd070;

-- 模式列安全换位后收缩为 0061 的异步轻量与同步强分类两种路径。
ALTER TABLE request_logs RENAME COLUMN intent_risk_mode TO intent_risk_mode_prd070;
-- 业务门禁已拒绝 sync_session，因此旧 CHECK 可无损接受全部剩余行。
ALTER TABLE request_logs ADD COLUMN intent_risk_mode TEXT
  CHECK (intent_risk_mode IS NULL OR intent_risk_mode IN ('async_light','sync_strong'));
-- 分类模式逐值复制，绝不把同步会话事实伪装成其他旧模式。
UPDATE request_logs SET intent_risk_mode=intent_risk_mode_prd070;
-- 模式复制后删除临时列，避免后续触发器引用迁移私有名称。
ALTER TABLE request_logs DROP COLUMN intent_risk_mode_prd070;

-- 调度状态列改名后恢复 0061 五状态目录，completed 已由业务门禁拒绝。
ALTER TABLE request_logs RENAME COLUMN intent_risk_schedule_status
  TO intent_risk_schedule_status_prd070;
-- 默认值与旧 CHECK 使用 0061 原定义，兼容历史 disabled 行和既有 upsert。
ALTER TABLE request_logs ADD COLUMN intent_risk_schedule_status TEXT NOT NULL DEFAULT 'disabled'
  CHECK (intent_risk_schedule_status IN ('disabled','queued','skipped','blocked','failed_closed'));
-- 原调度状态逐值复制，回滚不会把 queued 或 failed_closed 重置为默认值。
UPDATE request_logs SET intent_risk_schedule_status=intent_risk_schedule_status_prd070;
-- 状态复制完成后删除临时列，随后再重建 0061 组合触发器。
ALTER TABLE request_logs DROP COLUMN intent_risk_schedule_status_prd070;

-- Intent 两表必须恢复 0061 的 CHECK 枚举。先移除本地对象并把 0062 表改为临时名，
-- 再以 0061 原定义创建正式表，逐列复制 rowid 后按 child-to-parent 删除临时表。
DROP TRIGGER trg_intent_risk_events_owner_insert_prd069;
DROP TRIGGER trg_intent_risk_events_update_prd069;
DROP TRIGGER trg_intent_risk_events_delete_prd069;
DROP INDEX idx_intent_risk_events_tenant_time_prd069;
DROP TRIGGER trg_intent_risk_results_owner_insert_prd069;
DROP TRIGGER trg_intent_risk_results_update_prd069;
DROP TRIGGER trg_intent_risk_results_delete_prd069;
DROP INDEX idx_intent_risk_results_status_time_prd069;
DROP INDEX idx_intent_risk_results_type_time_prd069;
-- 先把事件子表移到迁移私有名称，释放正式表名供 0061 schema 重建。
ALTER TABLE intent_risk_events RENAME TO intent_risk_events_prd070_new;
-- 子表改名后再移动结果父表，避免 SQLite 重写出错误的临时外键目标。
ALTER TABLE intent_risk_results RENAME TO intent_risk_results_prd070_new;

CREATE TABLE intent_risk_results (
  intent_risk_result_id TEXT PRIMARY KEY CHECK (
    length(intent_risk_result_id) BETWEEN 5 AND 128 AND intent_risk_result_id GLOB 'irr_*'
  ),
  tenant_id TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK (source_kind IN ('gateway','manual')),
  source_request_id TEXT NOT NULL CHECK (length(source_request_id) BETWEEN 1 AND 128),
  source_trace_id TEXT NOT NULL CHECK (length(source_trace_id) BETWEEN 1 AND 128),
  actor_type TEXT NOT NULL CHECK (actor_type IN ('virtual_key','user')),
  actor_id TEXT NOT NULL CHECK (length(actor_id) BETWEEN 1 AND 128),
  status TEXT NOT NULL CHECK (status IN ('clean','detected','blocked','failed')),
  classification_mode TEXT NOT NULL CHECK (
    classification_mode IN ('sync_strong','async_light','manual_sync')
  ),
  risk_type TEXT NOT NULL CHECK (
    risk_type IN ('none','credential_exfiltration','system_prompt_extraction','policy_bypass','jailbreak')
  ),
  risk_score INTEGER NOT NULL CHECK (
    typeof(risk_score)='integer' AND risk_score BETWEEN 0 AND 10000
  ),
  risk_score_scale INTEGER NOT NULL CHECK (risk_score_scale=10000),
  action TEXT NOT NULL CHECK (action IN ('none','allow','observe','block','review')),
  reason_code TEXT NOT NULL CHECK (reason_code IN (
    'no_risk_detected','explicit_credential_extraction','explicit_system_prompt_request',
    'explicit_policy_bypass','explicit_instruction_override','credential_access_request',
    'system_prompt_access_request','classifier_failed'
  )),
  detector_version TEXT NOT NULL CHECK (
    length(detector_version) BETWEEN 1 AND 128 AND instr(detector_version,char(0))=0
  ),
  ruleset_version TEXT NOT NULL CHECK (
    length(ruleset_version) BETWEEN 1 AND 128 AND instr(ruleset_version,char(0))=0
  ),
  input_code_points INTEGER NOT NULL CHECK (
    typeof(input_code_points)='integer' AND input_code_points BETWEEN 0 AND 20000
  ),
  input_truncated INTEGER NOT NULL CHECK (input_truncated IN (0,1)),
  duration_ms INTEGER NOT NULL CHECK (
    typeof(duration_ms)='integer' AND duration_ms BETWEEN 0 AND 86400000
  ),
  error_code TEXT CHECK (
    error_code IS NULL OR error_code='intent_risk.classifier_failed'
  ),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  completed_at TEXT NOT NULL CHECK (
    unixepoch(completed_at) IS NOT NULL AND unixepoch(completed_at)>=unixepoch(created_at)
  ),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  UNIQUE (tenant_id,intent_risk_result_id),
  UNIQUE (tenant_id,source_kind,source_request_id),
  CHECK (
    (source_kind='gateway' AND actor_type='virtual_key'
      AND classification_mode IN ('sync_strong','async_light'))
    OR (source_kind='manual' AND actor_type='user' AND classification_mode='manual_sync'
      AND input_truncated=0)
  ),
  CHECK (
    (status='clean' AND classification_mode IN ('async_light','manual_sync')
      AND risk_type='none' AND risk_score=0 AND action='allow'
      AND reason_code='no_risk_detected' AND error_code IS NULL)
    OR (status='detected' AND classification_mode='async_light'
      AND risk_type!='none' AND risk_score>0 AND action='observe' AND error_code IS NULL)
    OR (status='detected' AND classification_mode='manual_sync'
      AND risk_type!='none' AND risk_score>0 AND action='review' AND error_code IS NULL)
    OR (status='blocked' AND source_kind='gateway' AND classification_mode='sync_strong'
      AND risk_type!='none' AND risk_score BETWEEN 9500 AND 10000
      AND action='block' AND error_code IS NULL)
    OR (status='failed'
      AND ((source_kind='gateway' AND classification_mode IN ('sync_strong','async_light'))
        OR (source_kind='manual' AND classification_mode='manual_sync'))
      AND risk_type='none' AND risk_score=0 AND action='none'
      AND reason_code='classifier_failed' AND error_code='intent_risk.classifier_failed')
  ),
  -- risk_type 与固定原因码必须一一属于同一 taxonomy，防止伪造不一致安全摘要。
  CHECK (
    (risk_type='none' AND reason_code IN ('no_risk_detected','classifier_failed'))
    OR (risk_type='credential_exfiltration'
      AND reason_code IN ('explicit_credential_extraction','credential_access_request'))
    OR (risk_type='system_prompt_extraction'
      AND reason_code IN ('explicit_system_prompt_request','system_prompt_access_request'))
    OR (risk_type='policy_bypass' AND reason_code='explicit_policy_bypass')
    OR (risk_type='jailbreak' AND reason_code='explicit_instruction_override')
  )
);
-- 0062 已证明不存在 sync_session，剩余结果按 rowid 和 22 列逐值保真复制。
INSERT INTO intent_risk_results(
  rowid,intent_risk_result_id,tenant_id,source_kind,source_request_id,source_trace_id,
  actor_type,actor_id,status,classification_mode,risk_type,risk_score,risk_score_scale,
  action,reason_code,detector_version,ruleset_version,input_code_points,input_truncated,
  duration_ms,error_code,created_at,completed_at
)
SELECT
  rowid,intent_risk_result_id,tenant_id,source_kind,source_request_id,source_trace_id,
  actor_type,actor_id,status,classification_mode,risk_type,risk_score,risk_score_scale,
  action,reason_code,detector_version,ruleset_version,input_code_points,input_truncated,
  duration_ms,error_code,created_at,completed_at
FROM intent_risk_results_prd070_new;

CREATE TABLE intent_risk_events (
  intent_risk_event_id TEXT PRIMARY KEY CHECK (
    length(intent_risk_event_id) BETWEEN 7 AND 128 AND intent_risk_event_id GLOB 'irevt_*'
  ),
  tenant_id TEXT NOT NULL,
  intent_risk_result_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type='security.intent_detected'),
  source_request_id TEXT NOT NULL,
  source_trace_id TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK (source_kind IN ('gateway','manual')),
  classification_mode TEXT NOT NULL CHECK (
    classification_mode IN ('sync_strong','async_light','manual_sync')
  ),
  risk_type TEXT NOT NULL CHECK (
    risk_type IN ('credential_exfiltration','system_prompt_extraction','policy_bypass','jailbreak')
  ),
  risk_score INTEGER NOT NULL CHECK (
    typeof(risk_score)='integer' AND risk_score BETWEEN 1 AND 10000
  ),
  risk_score_scale INTEGER NOT NULL CHECK (risk_score_scale=10000),
  action TEXT NOT NULL CHECK (action IN ('observe','block','review')),
  reason_code TEXT NOT NULL,
  detector_version TEXT NOT NULL,
  ruleset_version TEXT NOT NULL,
  input_truncated INTEGER NOT NULL CHECK (input_truncated IN (0,1)),
  actor_type TEXT NOT NULL CHECK (actor_type IN ('virtual_key','user')),
  actor_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id,intent_risk_result_id)
    REFERENCES intent_risk_results(tenant_id,intent_risk_result_id),
  UNIQUE (tenant_id,intent_risk_event_id),
  UNIQUE (tenant_id,intent_risk_result_id)
);
-- 父结果复制完成后再恢复事件，保证每条事件复合外键始终有同租户 owner。
INSERT INTO intent_risk_events(
  rowid,intent_risk_event_id,tenant_id,intent_risk_result_id,event_type,
  source_request_id,source_trace_id,source_kind,classification_mode,risk_type,
  risk_score,risk_score_scale,action,reason_code,detector_version,ruleset_version,
  input_truncated,actor_type,actor_id,created_at
)
SELECT
  rowid,intent_risk_event_id,tenant_id,intent_risk_result_id,event_type,
  source_request_id,source_trace_id,source_kind,classification_mode,risk_type,
  risk_score,risk_score_scale,action,reason_code,detector_version,ruleset_version,
  input_truncated,actor_type,actor_id,created_at
FROM intent_risk_events_prd070_new;

DROP TABLE intent_risk_events_prd070_new;
DROP TABLE intent_risk_results_prd070_new;

CREATE INDEX idx_intent_risk_results_status_time_prd069
  ON intent_risk_results(tenant_id,status,completed_at DESC,intent_risk_result_id DESC);
CREATE INDEX idx_intent_risk_results_type_time_prd069
  ON intent_risk_results(tenant_id,risk_type,completed_at DESC);
CREATE INDEX idx_intent_risk_events_tenant_time_prd069
  ON intent_risk_events(tenant_id,created_at DESC,intent_risk_event_id DESC);
CREATE TRIGGER trg_intent_risk_results_owner_insert_prd069
BEFORE INSERT ON intent_risk_results FOR EACH ROW
WHEN (NEW.source_kind='gateway' AND NOT EXISTS (
    SELECT 1 FROM gateway_request_claims claim
    WHERE claim.tenant_id=NEW.tenant_id AND claim.request_id=NEW.source_request_id
      AND claim.trace_id=NEW.source_trace_id AND claim.virtual_key_id IS NEW.actor_id
  ))
  OR (NEW.source_kind='manual' AND NOT EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.actor_id
      AND actor.status='active'
  ))
  OR EXISTS (
    SELECT 1 FROM intent_risk_results current
    WHERE current.intent_risk_result_id=NEW.intent_risk_result_id
       OR (current.tenant_id=NEW.tenant_id AND current.source_kind=NEW.source_kind
           AND current.source_request_id=NEW.source_request_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'intent_risk_result_owner_mismatch'); END;
CREATE TRIGGER trg_intent_risk_results_update_prd069
BEFORE UPDATE ON intent_risk_results FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'intent_risk_result_immutable'); END;
CREATE TRIGGER trg_intent_risk_results_delete_prd069
BEFORE DELETE ON intent_risk_results FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'intent_risk_result_immutable'); END;
CREATE TRIGGER trg_intent_risk_events_owner_insert_prd069
BEFORE INSERT ON intent_risk_events FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM intent_risk_results result
  WHERE result.tenant_id=NEW.tenant_id
    AND result.intent_risk_result_id=NEW.intent_risk_result_id
    AND result.status IN ('detected','blocked')
    AND result.source_request_id=NEW.source_request_id
    AND result.source_trace_id=NEW.source_trace_id
    AND result.source_kind=NEW.source_kind
    AND result.classification_mode=NEW.classification_mode
    AND result.risk_type=NEW.risk_type AND result.risk_score=NEW.risk_score
    AND result.risk_score_scale=NEW.risk_score_scale AND result.action=NEW.action
    AND result.reason_code=NEW.reason_code
    AND result.detector_version=NEW.detector_version
    AND result.ruleset_version=NEW.ruleset_version
    AND result.input_truncated=NEW.input_truncated
    AND result.actor_type=NEW.actor_type AND result.actor_id=NEW.actor_id
    AND result.completed_at=NEW.created_at
)
  OR EXISTS (
    SELECT 1 FROM intent_risk_events current
    WHERE current.intent_risk_event_id=NEW.intent_risk_event_id
       OR (current.tenant_id=NEW.tenant_id
           AND current.intent_risk_result_id=NEW.intent_risk_result_id)
       OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
  )
BEGIN SELECT RAISE(ABORT,'intent_risk_event_owner_mismatch'); END;
CREATE TRIGGER trg_intent_risk_events_update_prd069
BEFORE UPDATE ON intent_risk_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'intent_risk_event_immutable'); END;
CREATE TRIGGER trg_intent_risk_events_delete_prd069
BEFORE DELETE ON intent_risk_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'intent_risk_event_immutable'); END;

-- AuditLog 恢复 0061 的 action/object CHECK，并逐列保留全部 17 列和 rowid。
-- 旧索引与 owner/immutable/export trigger 直接复用 PRD-068 已验证定义。
DROP TABLE IF EXISTS audit_logs_prd070_old;
CREATE TABLE audit_logs_prd070_old (
  audit_log_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  actor_email TEXT NOT NULL,
  actor_role TEXT NOT NULL CHECK (
    actor_role IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  ),
  action TEXT NOT NULL CHECK (
    action IN (
      'provider.created','provider.updated',
      'provider_key.created','provider_key.updated',
      'virtual_key.created','virtual_key.updated',
      'budget_rule.created','budget_rule.updated',
      'dlp_rule.created','dlp_rule.updated',
      'webhook_delivery.replayed','webhook_endpoint.secret_rotated',
      'webhook_dead_letter.archived',
      'usage_export.requested','usage_export.downloaded',
      'policy.created','policy.updated',
      'policy_snapshot.published','policy_snapshot.rolled_back',
      'policy.dry_run_executed',
      'routing_policy.created','routing_policy.updated',
      'model_quality_score.updated',
      'cache_affinity.created','cache_affinity.updated',
      'finops.adjustment_created',
      'cost_anomaly.acknowledged','cost_anomaly.dismissed',
      'cost_anomaly.temporary_rate_limit_applied',
      'cost_anomaly.temporary_rate_limit_released',
      'error_map.created','error_map.updated',
      'audit.exported','role.permission_changed',
      'user.invited','invitation.accepted',
      'config.imported','config.exported',
      'prompt_template.created','prompt_template.updated',
      'prompt_template.version_published',
      'policy_template.applied','policy_template.rolled_back'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider','provider_key','virtual_key','budget_rule','dlp_rule',
      'webhook_delivery','webhook_endpoint','webhook_dead_letter',
      'usage_export_job','policy','policy_snapshot','policy_explanation',
      'routing_policy','model_quality_score','cache_affinity',
      'finops_adjustment','cost_anomaly','provider_error_map','audit_export',
      'role_permission','invitation','config_transfer',
      'prompt_template','prompt_template_version',
      'policy_template_application'
    )
  ),
  object_id TEXT NOT NULL,
  object_name TEXT,
  before_summary_json TEXT NOT NULL CHECK (
    json_valid(before_summary_json) AND json_type(before_summary_json)='object'
  ),
  after_summary_json TEXT NOT NULL CHECK (
    json_valid(after_summary_json) AND json_type(after_summary_json)='object'
  ),
  changed_fields_json TEXT NOT NULL CHECK (
    json_valid(changed_fields_json) AND json_type(changed_fields_json)='array'
  ),
  client_ip TEXT,
  user_agent TEXT,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (tenant_id,audit_log_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);
-- 审计换表逐列复制并保留 rowid，历史 17 个持久列和 NULL 语义不得发生漂移。
INSERT INTO audit_logs_prd070_old(
  rowid,audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
  action,object_type,object_id,object_name,before_summary_json,
  after_summary_json,changed_fields_json,client_ip,user_agent,
  request_id,trace_id,created_at
)
SELECT
  rowid,audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,
  action,object_type,object_id,object_name,before_summary_json,
  after_summary_json,changed_fields_json,client_ip,user_agent,
  request_id,trace_id,created_at
FROM audit_logs;

-- 删除父表前以行数、NULL 分布和含 rowid 双向差集复核复制，不能只比较主键数量。
CREATE TEMP TABLE prd070_down_audit_copy_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count=0)
);
-- 复制后逐值核对行数与 17 列内容，任何差异都在删除旧审计表前失败关闭。
INSERT INTO prd070_down_audit_copy_guard(issue_count)
SELECT
  ABS((SELECT COUNT(*) FROM audit_logs)-(SELECT COUNT(*) FROM audit_logs_prd070_old))
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd070_old WHERE object_name IS NULL)
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd070_old WHERE client_ip IS NULL)
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
      =(SELECT COUNT(*) FROM audit_logs_prd070_old WHERE user_agent IS NULL)
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs
      EXCEPT SELECT rowid,* FROM audit_logs_prd070_old
    ))
  + (SELECT COUNT(*) FROM (
      SELECT rowid,* FROM audit_logs_prd070_old
      EXCEPT SELECT rowid,* FROM audit_logs
    ));
DROP TABLE prd070_down_audit_copy_guard;

-- legacy_alter_table 保持四个已核验的外部 owner trigger 原样指向正式表名；旧表本地
-- trigger/index 随 DROP 移除，重命名后按冻结定义逐项恢复。
DROP TABLE audit_logs;
-- 仅在逐值复制门禁通过后替换正式审计表，确保历史治理证据完整保真。
ALTER TABLE audit_logs_prd070_old RENAME TO audit_logs;
CREATE INDEX idx_audit_logs_tenant_time
  ON audit_logs(tenant_id,created_at DESC,audit_log_id DESC);
CREATE INDEX idx_audit_logs_tenant_actor_time
  ON audit_logs(tenant_id,actor_user_id,created_at DESC);
CREATE INDEX idx_audit_logs_tenant_object_time
  ON audit_logs(tenant_id,object_type,object_id,created_at DESC);
CREATE INDEX idx_audit_logs_tenant_action_time
  ON audit_logs(tenant_id,action,created_at DESC);
CREATE INDEX idx_audit_logs_tenant_request
  ON audit_logs(tenant_id,request_id);
CREATE UNIQUE INDEX idx_audit_logs_dry_run_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='policy.dry_run_executed';
CREATE UNIQUE INDEX idx_audit_logs_model_quality_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='model_quality_score.updated';
CREATE UNIQUE INDEX idx_audit_logs_finops_adjustment_unique
  ON audit_logs(tenant_id,object_id)
  WHERE action='finops.adjustment_created';
CREATE UNIQUE INDEX idx_audit_logs_role_permission_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action='role.permission_changed';
CREATE UNIQUE INDEX idx_audit_logs_invitation_request_unique
  ON audit_logs(tenant_id,actor_user_id,request_id)
  WHERE action IN ('user.invited','invitation.accepted');
CREATE UNIQUE INDEX idx_audit_logs_config_transfer_request_unique
  ON audit_logs(tenant_id,actor_user_id,action,request_id)
  WHERE action IN ('config.imported','config.exported');
-- v1 的 created 与 version_published 共用 request_id，因此 action 是唯一键的一部分。
CREATE UNIQUE INDEX idx_audit_logs_prompt_template_request_unique
  ON audit_logs(tenant_id,request_id,action)
  WHERE action IN (
    'prompt_template.created','prompt_template.updated',
    'prompt_template.version_published'
  );

-- tenant owner 候选键在 Audit 换表后必须显式恢复，供 application 的 deferred 环边使用。
CREATE UNIQUE INDEX uq_audit_logs_owner_prd068
  ON audit_logs(tenant_id,audit_log_id);
CREATE UNIQUE INDEX idx_audit_logs_policy_template_request_unique
  ON audit_logs(tenant_id,request_id,action)
  WHERE action IN ('policy_template.applied','policy_template.rolled_back');

-- owner trigger 保留 0052 的全部旧领域约束，并为两类配置迁移动作增加同租户 source fact、
-- actor、request/trace、时间与精确摘要的双向绑定。任一不一致都回滚业务与审计事务。
CREATE TRIGGER trg_audit_logs_owner_insert
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM users user
  WHERE user.user_id=NEW.actor_user_id AND user.tenant_id=NEW.tenant_id
)
OR (
  NEW.action='finops.adjustment_created'
  AND (
    NEW.object_type!='finops_adjustment'
    OR NOT EXISTS (
      SELECT 1 FROM finops_adjustments adjustment
      WHERE adjustment.finops_adjustment_id=NEW.object_id
        AND adjustment.tenant_id=NEW.tenant_id
        AND adjustment.actor_user_id=NEW.actor_user_id
    )
  )
)
OR (NEW.object_type='finops_adjustment' AND NEW.action!='finops.adjustment_created')
OR (
  NEW.action IN (
    'cost_anomaly.acknowledged','cost_anomaly.dismissed',
    'cost_anomaly.temporary_rate_limit_applied',
    'cost_anomaly.temporary_rate_limit_released'
  )
  AND (
    NEW.object_type!='cost_anomaly'
    OR NOT EXISTS (
      SELECT 1 FROM cost_anomaly_actions action
      JOIN cost_anomalies anomaly
        ON anomaly.tenant_id=action.tenant_id
       AND anomaly.cost_anomaly_id=action.cost_anomaly_id
      WHERE action.tenant_id=NEW.tenant_id
        AND action.cost_anomaly_id=NEW.object_id
        AND action.actor_user_id=NEW.actor_user_id
        AND action.operation_request_id=NEW.request_id
        AND action.operation_trace_id=NEW.trace_id
        AND CASE action.action_type
          WHEN 'acknowledge' THEN 'cost_anomaly.acknowledged'
          WHEN 'dismiss' THEN 'cost_anomaly.dismissed'
          WHEN 'apply_temporary_rate_limit' THEN 'cost_anomaly.temporary_rate_limit_applied'
          WHEN 'release_temporary_rate_limit' THEN 'cost_anomaly.temporary_rate_limit_released'
        END=NEW.action
    )
  )
)
OR (
  NEW.object_type='cost_anomaly'
  AND NEW.action NOT IN (
    'cost_anomaly.acknowledged','cost_anomaly.dismissed',
    'cost_anomaly.temporary_rate_limit_applied',
    'cost_anomaly.temporary_rate_limit_released'
  )
)
OR (
  NEW.action IN ('error_map.created','error_map.updated')
  AND (
    NEW.object_type!='provider_error_map'
    OR NOT EXISTS (
      SELECT 1 FROM provider_error_map_events event
      JOIN provider_error_map_versions version
        ON version.tenant_id=event.tenant_id
       AND version.provider_error_map_version_id=event.provider_error_map_version_id
      JOIN provider_error_maps map
        ON map.tenant_id=event.tenant_id
       AND map.provider_error_map_id=event.provider_error_map_id
       AND map.current_version=event.rule_version
      WHERE event.tenant_id=NEW.tenant_id
        AND event.provider_error_map_id=NEW.object_id
        AND event.actor_user_id=NEW.actor_user_id
        AND event.request_id=NEW.request_id
        AND event.trace_id=NEW.trace_id
        AND (
          (NEW.action='error_map.created' AND event.operation='create')
          OR (NEW.action='error_map.updated' AND event.operation IN ('update','enable','disable'))
        )
    )
  )
)
OR (
  NEW.object_type='provider_error_map'
  AND NEW.action NOT IN ('error_map.created','error_map.updated')
)
OR (
  NEW.action='webhook_dead_letter.archived'
  AND (
    NEW.object_type!='webhook_dead_letter'
    OR NOT EXISTS (
      SELECT 1 FROM webhook_dead_letter_events dead_letter
      WHERE dead_letter.tenant_id=NEW.tenant_id
        AND dead_letter.dead_letter_event_id=NEW.object_id
        AND dead_letter.status='archived'
        AND dead_letter.archived_by=NEW.actor_user_id
    )
  )
)
OR (
  NEW.object_type='webhook_dead_letter'
  AND NEW.action!='webhook_dead_letter.archived'
)
OR (
  NEW.action='audit.exported'
  AND (
    NEW.object_type!='audit_export'
    OR NOT EXISTS (
      SELECT 1 FROM audit_exports export
      WHERE export.tenant_id=NEW.tenant_id
        AND export.audit_export_id=NEW.object_id
        AND export.audit_log_id=NEW.audit_log_id
        AND export.requested_by=NEW.actor_user_id
        AND export.request_id=NEW.request_id
        AND export.trace_id=NEW.trace_id
        AND export.created_at=NEW.created_at
    )
  )
)
OR (NEW.object_type='audit_export' AND NEW.action!='audit.exported')
OR (
  EXISTS (SELECT 1 FROM audit_exports export WHERE export.audit_log_id=NEW.audit_log_id)
  AND NOT (
    NEW.action='audit.exported' AND NEW.object_type='audit_export'
    AND EXISTS (
      SELECT 1 FROM audit_exports export
      WHERE export.tenant_id=NEW.tenant_id
        AND export.audit_export_id=NEW.object_id
        AND export.audit_log_id=NEW.audit_log_id
        AND export.requested_by=NEW.actor_user_id
        AND export.request_id=NEW.request_id
        AND export.trace_id=NEW.trace_id
        AND export.created_at=NEW.created_at
    )
  )
)
OR (
  NEW.action='role.permission_changed'
  AND (
    NEW.object_type!='role_permission'
    OR NOT EXISTS (
      SELECT 1 FROM roles role
      WHERE role.tenant_id=NEW.tenant_id AND role.role_id=NEW.object_id
        AND role.code!='admin' AND role.status='active'
        AND role.permission_revision=json_extract(NEW.after_summary_json,'$.permission_revision')
        AND role.matrix_sha256=json_extract(NEW.after_summary_json,'$.matrix_sha256')
        AND json_extract(NEW.before_summary_json,'$.permission_revision')+1=role.permission_revision
    )
  )
)
OR (NEW.object_type='role_permission' AND NEW.action!='role.permission_changed')
OR (
  NEW.action='config.imported'
  AND (
    NEW.object_type!='config_transfer' OR NEW.object_name IS NOT NULL
    OR NOT EXISTS (
      SELECT 1
      FROM config_imports import_fact
      JOIN users actor
        ON actor.tenant_id=import_fact.tenant_id
       AND actor.user_id=import_fact.actor_user_id
      WHERE import_fact.tenant_id=NEW.tenant_id
        AND import_fact.config_import_id=NEW.object_id
        AND import_fact.actor_user_id=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active'
        AND import_fact.request_id=NEW.request_id
        AND import_fact.trace_id=NEW.trace_id
        AND import_fact.created_at=NEW.created_at
        AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=0
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=12
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN (
            'schema_version','manifest_sha256',
            'providers_created_count','providers_unchanged_count',
            'models_created_count','models_unchanged_count',
            'aliases_created_count','aliases_unchanged_count',
            'virtual_keys_created_count','virtual_keys_unchanged_count',
            'generated_virtual_key_count','disabled_virtual_key_count'
          )
        )
        AND json_extract(NEW.after_summary_json,'$.schema_version')=import_fact.schema_version
        AND json_extract(NEW.after_summary_json,'$.manifest_sha256')=import_fact.manifest_sha256
        AND json_extract(NEW.after_summary_json,'$.providers_created_count')
          =import_fact.providers_created_count
        AND json_extract(NEW.after_summary_json,'$.providers_unchanged_count')
          =import_fact.providers_unchanged_count
        AND json_extract(NEW.after_summary_json,'$.models_created_count')
          =import_fact.models_created_count
        AND json_extract(NEW.after_summary_json,'$.models_unchanged_count')
          =import_fact.models_unchanged_count
        AND json_extract(NEW.after_summary_json,'$.aliases_created_count')
          =import_fact.aliases_created_count
        AND json_extract(NEW.after_summary_json,'$.aliases_unchanged_count')
          =import_fact.aliases_unchanged_count
        AND json_extract(NEW.after_summary_json,'$.virtual_keys_created_count')
          =import_fact.virtual_keys_created_count
        AND json_extract(NEW.after_summary_json,'$.virtual_keys_unchanged_count')
          =import_fact.virtual_keys_unchanged_count
        AND json_extract(NEW.after_summary_json,'$.generated_virtual_key_count')
          =import_fact.generated_virtual_key_count
        AND json_extract(NEW.after_summary_json,'$.disabled_virtual_key_count')
          =import_fact.disabled_virtual_key_count
        AND json(NEW.changed_fields_json)=json(
          '["aliases_created_count","aliases_unchanged_count",' ||
          '"disabled_virtual_key_count","generated_virtual_key_count",' ||
          '"manifest_sha256","models_created_count","models_unchanged_count",' ||
          '"providers_created_count","providers_unchanged_count","schema_version",' ||
          '"virtual_keys_created_count","virtual_keys_unchanged_count"]'
        )
        AND (SELECT COUNT(*) FROM config_import_rows row_fact
             WHERE row_fact.tenant_id=import_fact.tenant_id
               AND row_fact.config_import_id=import_fact.config_import_id)
          =import_fact.providers_created_count+import_fact.providers_unchanged_count
           +import_fact.models_created_count+import_fact.models_unchanged_count
           +import_fact.aliases_created_count+import_fact.aliases_unchanged_count
           +import_fact.virtual_keys_created_count+import_fact.virtual_keys_unchanged_count
    )
  )
)
OR (
  NEW.action='config.exported'
  AND (
    NEW.object_type!='config_transfer' OR NEW.object_name IS NOT NULL
    OR NOT EXISTS (
      SELECT 1
      FROM config_exports export_fact
      JOIN users actor
        ON actor.tenant_id=export_fact.tenant_id
       AND actor.user_id=export_fact.actor_user_id
      WHERE export_fact.tenant_id=NEW.tenant_id
        AND export_fact.config_export_id=NEW.object_id
        AND export_fact.actor_user_id=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active'
        AND export_fact.request_id=NEW.request_id
        AND export_fact.trace_id=NEW.trace_id
        AND export_fact.created_at=NEW.created_at
        AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=0
        AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=6
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN (
            'schema_version','manifest_sha256','providers_count','models_count',
            'aliases_count','virtual_keys_count'
          )
        )
        AND json_extract(NEW.after_summary_json,'$.schema_version')=export_fact.schema_version
        AND json_extract(NEW.after_summary_json,'$.manifest_sha256')=export_fact.manifest_sha256
        AND json_extract(NEW.after_summary_json,'$.providers_count')=export_fact.providers_count
        AND json_extract(NEW.after_summary_json,'$.models_count')=export_fact.models_count
        AND json_extract(NEW.after_summary_json,'$.aliases_count')=export_fact.aliases_count
        AND json_extract(NEW.after_summary_json,'$.virtual_keys_count')
          =export_fact.virtual_keys_count
        AND json(NEW.changed_fields_json)=json(
          '["aliases_count","manifest_sha256","models_count","providers_count",' ||
          '"schema_version","virtual_keys_count"]'
        )
    )
  )
)
OR (
  NEW.object_type='config_transfer'
  AND NEW.action NOT IN ('config.imported','config.exported')
)
OR (
  NEW.action IN ('user.invited','invitation.accepted')
  AND (
    NEW.object_type!='invitation' OR NEW.object_name IS NOT NULL
    OR NOT EXISTS (
      SELECT 1
      FROM invitations invitation
      JOIN tenants tenant
        ON tenant.tenant_id=invitation.tenant_id
      JOIN roles target_role
        ON target_role.tenant_id=invitation.tenant_id
       AND target_role.code=invitation.role_code
      JOIN users actor
        ON actor.tenant_id=invitation.tenant_id AND actor.user_id=NEW.actor_user_id
      WHERE invitation.tenant_id=NEW.tenant_id
        AND invitation.invitation_id=NEW.object_id
        AND tenant.status='active' AND target_role.status='active'
        AND actor.role=NEW.actor_role AND actor.status='active'
        AND (
          (
            NEW.action='user.invited'
            AND NEW.actor_email=actor.email
            AND invitation.status='pending'
            AND invitation.invited_by=NEW.actor_user_id
            AND invitation.created_at=NEW.created_at
            AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=0
            AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=5
            AND NOT EXISTS (
              SELECT 1 FROM json_each(NEW.after_summary_json) field
              WHERE field.key NOT IN (
                'email_fingerprint','role_code','status','expires_at','accepted_user_id'
              )
            )
            AND json_extract(NEW.after_summary_json,'$.email_fingerprint')
              =invitation.email_fingerprint
            AND json_extract(NEW.after_summary_json,'$.role_code')=invitation.role_code
            AND json_extract(NEW.after_summary_json,'$.status')='pending'
            AND json_extract(NEW.after_summary_json,'$.expires_at')=invitation.expires_at
            AND json_type(NEW.after_summary_json,'$.accepted_user_id')='null'
            AND json(NEW.changed_fields_json)
              =json('["email_fingerprint","expires_at","role_code","status"]')
          )
          OR
          (
            NEW.action='invitation.accepted'
            AND NEW.actor_email='invitation-acceptor@redacted.invalid'
            AND invitation.status='accepted'
            AND invitation.accepted_user_id=NEW.actor_user_id
            AND actor.role=invitation.role_code
            AND invitation.email=actor.email
            AND invitation.accepted_at=NEW.created_at
            AND (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=5
            AND (SELECT COUNT(*) FROM json_each(NEW.after_summary_json))=5
            AND NOT EXISTS (
              SELECT 1 FROM json_each(NEW.before_summary_json) field
              WHERE field.key NOT IN (
                'email_fingerprint','role_code','status','expires_at','accepted_user_id'
              )
            )
            AND NOT EXISTS (
              SELECT 1 FROM json_each(NEW.after_summary_json) field
              WHERE field.key NOT IN (
                'email_fingerprint','role_code','status','expires_at','accepted_user_id'
              )
            )
            AND json_extract(NEW.before_summary_json,'$.email_fingerprint')
              =invitation.email_fingerprint
            AND json_extract(NEW.before_summary_json,'$.role_code')=invitation.role_code
            AND json_extract(NEW.before_summary_json,'$.status')='pending'
            AND json_extract(NEW.before_summary_json,'$.expires_at')=invitation.expires_at
            AND json_type(NEW.before_summary_json,'$.accepted_user_id')='null'
            AND json_extract(NEW.after_summary_json,'$.email_fingerprint')
              =invitation.email_fingerprint
            AND json_extract(NEW.after_summary_json,'$.role_code')=invitation.role_code
            AND json_extract(NEW.after_summary_json,'$.status')='accepted'
            AND json_extract(NEW.after_summary_json,'$.expires_at')=invitation.expires_at
            AND json_extract(NEW.after_summary_json,'$.accepted_user_id')
              =invitation.accepted_user_id
            AND json(NEW.changed_fields_json)=json('["accepted_user_id","status"]')
          )
        )
    )
  )
)
OR (
  NEW.object_type='invitation'
  AND NEW.action NOT IN ('user.invited','invitation.accepted')
)
-- Template create/update 必须绑定当前同租户 head、actor 和安全摘要白名单。
OR (
  NEW.action IN ('prompt_template.created','prompt_template.updated')
  AND (
    NEW.object_type!='prompt_template'
    OR NOT EXISTS (
      SELECT 1
      FROM prompt_templates template
      JOIN prompt_template_versions version
        ON version.tenant_id=template.tenant_id
       AND version.prompt_template_id=template.prompt_template_id
       AND version.prompt_template_version_id=template.current_version_id
       AND version.state='published'
      JOIN users actor
        ON actor.tenant_id=template.tenant_id AND actor.user_id=NEW.actor_user_id
      WHERE template.tenant_id=NEW.tenant_id
        AND template.prompt_template_id=NEW.object_id
        AND template.updated_by=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active' AND template.name=NEW.object_name
        AND json_extract(NEW.after_summary_json,'$.status')=template.status
        AND json_extract(NEW.after_summary_json,'$.revision')=template.revision
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.before_summary_json) field
          WHERE field.key NOT IN (
            'status','revision','version_number','project_scope','project_count',
            'recommended_model_count','definition_size_bytes','variable_count',
            'change_summary_hmac'
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN (
            'status','revision','version_number','project_scope','project_count',
            'recommended_model_count','definition_size_bytes','variable_count',
            'change_summary_hmac'
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.changed_fields_json) field
          WHERE field.value NOT IN (
            'status','revision','version_number','project_scope','project_count',
            'recommended_model_count','definition_size_bytes','variable_count',
            'change_summary_hmac'
          )
        )
        AND (
          NEW.action!='prompt_template.created'
          OR (
            (SELECT COUNT(*) FROM json_each(NEW.before_summary_json))=0
            AND json_extract(NEW.after_summary_json,'$.version_number')
              =version.version_number
            AND json_extract(NEW.after_summary_json,'$.project_scope')
              =version.project_scope
            AND json_extract(NEW.after_summary_json,'$.project_count')=(
              SELECT COUNT(*) FROM prompt_template_version_projects binding
              WHERE binding.tenant_id=version.tenant_id
                AND binding.prompt_template_version_id=version.prompt_template_version_id
            )
            AND json_extract(NEW.after_summary_json,'$.recommended_model_count')
              =version.recommended_model_count
            AND json_extract(NEW.after_summary_json,'$.definition_size_bytes')
              =version.definition_size_bytes
            AND json_extract(NEW.after_summary_json,'$.variable_count')
              =version.variable_count
            AND json_extract(NEW.after_summary_json,'$.change_summary_hmac')
              =version.change_summary_hmac
          )
        )
        AND (
          NEW.action!='prompt_template.updated'
          OR json_extract(NEW.before_summary_json,'$.revision')=template.revision-1
        )
    )
  )
)
-- Version publication Audit 绑定已封印版本、当前 head、publisher 和全部安全计数。
OR (
  NEW.action='prompt_template.version_published'
  AND (
    NEW.object_type!='prompt_template_version'
    OR NOT EXISTS (
      SELECT 1
      FROM prompt_template_versions version
      JOIN prompt_templates template
        ON template.tenant_id=version.tenant_id
       AND template.prompt_template_id=version.prompt_template_id
       AND template.current_version_id=version.prompt_template_version_id
      JOIN users actor
        ON actor.tenant_id=version.tenant_id AND actor.user_id=NEW.actor_user_id
      WHERE version.tenant_id=NEW.tenant_id
        AND version.prompt_template_version_id=NEW.object_id
        AND version.state='published' AND version.created_by=NEW.actor_user_id
        AND actor.email=NEW.actor_email AND actor.role=NEW.actor_role
        AND actor.status='active' AND template.name=NEW.object_name
        AND NEW.created_at=version.published_at
        AND json_extract(NEW.after_summary_json,'$.status')=template.status
        AND json_extract(NEW.after_summary_json,'$.revision')=template.revision
        AND json_extract(NEW.after_summary_json,'$.version_number')=version.version_number
        AND json_extract(NEW.after_summary_json,'$.project_scope')=version.project_scope
        AND json_extract(NEW.after_summary_json,'$.project_count')=(
          SELECT COUNT(*) FROM prompt_template_version_projects binding
          WHERE binding.tenant_id=version.tenant_id
            AND binding.prompt_template_version_id=version.prompt_template_version_id
        )
        AND json_extract(NEW.after_summary_json,'$.recommended_model_count')
          =version.recommended_model_count
        AND json_extract(NEW.after_summary_json,'$.definition_size_bytes')
          =version.definition_size_bytes
        AND json_extract(NEW.after_summary_json,'$.variable_count')=version.variable_count
        AND json_extract(NEW.after_summary_json,'$.change_summary_hmac')
          =version.change_summary_hmac
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.before_summary_json) field
          WHERE field.key NOT IN (
            'status','revision','version_number','project_scope','project_count',
            'recommended_model_count','definition_size_bytes','variable_count',
            'change_summary_hmac'
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN (
            'status','revision','version_number','project_scope','project_count',
            'recommended_model_count','definition_size_bytes','variable_count',
            'change_summary_hmac'
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.changed_fields_json) field
          WHERE field.value NOT IN (
            'status','revision','version_number','project_scope','project_count',
            'recommended_model_count','definition_size_bytes','variable_count',
            'change_summary_hmac'
          )
        )
    )
  )
)
OR (
  NEW.object_type='prompt_template'
  AND NEW.action NOT IN ('prompt_template.created','prompt_template.updated')
)
OR (
  NEW.object_type='prompt_template_version'
  AND NEW.action!='prompt_template.version_published'
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;

-- Template Audit 必须反向绑定同 tenant application、actor、request/trace 与预生成 fact ID。
-- 摘要只允许稳定治理字段，明确排除绑定值、价格、预算余额、URL、Key 和请求正文。
CREATE TRIGGER trg_audit_logs_policy_template_owner_insert_prd068
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN (
  NEW.action IN ('policy_template.applied','policy_template.rolled_back')
  AND (
    NEW.object_type!='policy_template_application'
    OR NEW.object_name IS NOT NULL
    OR NOT EXISTS (
      SELECT 1
      FROM policy_template_applications application
      JOIN policy_templates template
        ON template.tenant_id=application.tenant_id
       AND template.policy_template_id=application.policy_template_id
      WHERE application.tenant_id=NEW.tenant_id
        AND application.application_id=NEW.object_id
        AND (
          (NEW.action='policy_template.applied'
            AND application.status IN ('applied','rolled_back')
            AND application.apply_audit_log_id=NEW.audit_log_id
            AND application.actor_user_id=NEW.actor_user_id
            AND application.request_id=NEW.request_id
            AND application.trace_id=NEW.trace_id
            AND application.applied_as_of=NEW.created_at)
          OR
          (NEW.action='policy_template.rolled_back'
            AND application.status='rolled_back'
            AND application.rollback_apply_audit_log_id=NEW.audit_log_id
            AND application.rollback_actor_user_id=NEW.actor_user_id
            AND application.rollback_request_id=NEW.request_id
            AND application.rollback_trace_id=NEW.trace_id
            AND application.rolled_back_at=NEW.created_at)
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.before_summary_json) field
          WHERE field.key NOT IN (
            'status','template_code','template_version','application_id',
            'generated_policy_id','previous_snapshot_version','result_snapshot_version',
            'rollback_snapshot_version','impact_hash','bindings_hash','changed_resource_types'
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.after_summary_json) field
          WHERE field.key NOT IN (
            'status','template_code','template_version','application_id',
            'generated_policy_id','previous_snapshot_version','result_snapshot_version',
            'rollback_snapshot_version','impact_hash','bindings_hash','changed_resource_types'
          )
        )
        AND NOT EXISTS (
          SELECT 1 FROM json_each(NEW.changed_fields_json) field
          WHERE field.value NOT IN (
            'status','template_code','template_version','application_id',
            'generated_policy_id','previous_snapshot_version','result_snapshot_version',
            'rollback_snapshot_version','impact_hash','bindings_hash','changed_resource_types'
          )
        )
        AND json_extract(NEW.after_summary_json,'$.template_code')=template.template_code
        AND json_extract(NEW.after_summary_json,'$.template_version')=application.template_version
        AND json_extract(NEW.after_summary_json,'$.application_id')=application.application_id
        AND json_extract(NEW.after_summary_json,'$.generated_policy_id')=application.generated_policy_id
        AND json_extract(NEW.after_summary_json,'$.previous_snapshot_version')
          =application.previous_snapshot_version
        AND json_extract(NEW.after_summary_json,'$.result_snapshot_version')
          =application.result_snapshot_version
        AND json_extract(NEW.after_summary_json,'$.impact_hash')=application.impact_hash
        AND json_extract(NEW.after_summary_json,'$.bindings_hash')=application.bindings_hash
        AND json_extract(NEW.after_summary_json,'$.changed_resource_types')
          ='policy,policy_snapshot'
        AND (
          (
            NEW.action='policy_template.applied'
            AND NEW.before_summary_json='{}'
            AND json_extract(NEW.after_summary_json,'$.status')='applied'
            AND json_type(NEW.after_summary_json,'$.rollback_snapshot_version') IS NULL
          )
          OR
          (
            NEW.action='policy_template.rolled_back'
            AND json_extract(NEW.before_summary_json,'$.status')='applied'
            AND json_extract(NEW.before_summary_json,'$.template_code')=template.template_code
            AND json_extract(NEW.before_summary_json,'$.template_version')
              =application.template_version
            AND json_extract(NEW.before_summary_json,'$.application_id')
              =application.application_id
            AND json_extract(NEW.before_summary_json,'$.generated_policy_id')
              =application.generated_policy_id
            AND json_extract(NEW.before_summary_json,'$.previous_snapshot_version')
              =application.previous_snapshot_version
            AND json_extract(NEW.before_summary_json,'$.result_snapshot_version')
              =application.result_snapshot_version
            AND json_extract(NEW.before_summary_json,'$.impact_hash')=application.impact_hash
            AND json_extract(NEW.before_summary_json,'$.bindings_hash')=application.bindings_hash
            AND json_extract(NEW.before_summary_json,'$.changed_resource_types')
              ='policy,policy_snapshot'
            AND json_type(NEW.before_summary_json,'$.rollback_snapshot_version') IS NULL
            AND json_extract(NEW.after_summary_json,'$.status')='rolled_back'
            AND json_extract(NEW.after_summary_json,'$.rollback_snapshot_version')
              =application.rollback_snapshot_version
          )
        )
    )
  )
)
OR (
  NEW.object_type='policy_template_application'
  AND NEW.action NOT IN ('policy_template.applied','policy_template.rolled_back')
)
BEGIN SELECT RAISE(ABORT,'audit_log_owner_mismatch'); END;

CREATE TRIGGER trg_audit_logs_immutable_update
BEFORE UPDATE ON audit_logs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_immutable_delete
BEFORE DELETE ON audit_logs FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_prd050_conflict_insert
BEFORE INSERT ON audit_logs FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM audit_logs current
  WHERE current.audit_log_id=NEW.audit_log_id
    OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
    OR (
      NEW.action IN ('policy.dry_run_executed','model_quality_score.updated')
      AND current.action=NEW.action AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
    OR (
      NEW.action='finops.adjustment_created'
      AND current.action='finops.adjustment_created'
      AND current.tenant_id=NEW.tenant_id AND current.object_id=NEW.object_id
    )
    OR (
      NEW.action IN (
        'cost_anomaly.acknowledged','cost_anomaly.dismissed',
        'cost_anomaly.temporary_rate_limit_applied',
        'cost_anomaly.temporary_rate_limit_released'
      )
      AND current.action=NEW.action AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
    OR (
      NEW.action IN ('error_map.created','error_map.updated')
      AND current.action=NEW.action AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
    OR (
      NEW.action IN ('user.invited','invitation.accepted')
      AND current.action IN ('user.invited','invitation.accepted')
      AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
    OR (
      NEW.action IN ('config.imported','config.exported')
      AND current.action=NEW.action AND current.tenant_id=NEW.tenant_id
      AND current.actor_user_id=NEW.actor_user_id AND current.request_id=NEW.request_id
    )
    OR (
      NEW.action IN (
        'prompt_template.created','prompt_template.updated',
        'prompt_template.version_published'
      )
      AND current.action=NEW.action AND current.tenant_id=NEW.tenant_id
      AND current.request_id=NEW.request_id
    )
)
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_prd050_rowid_insert
AFTER INSERT ON audit_logs FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_export_sequence_insert_prd058
AFTER INSERT ON audit_logs FOR EACH ROW
BEGIN
  -- 每条新审计事实都登记导出序列，配置迁移审计不得绕过既有增量导出链。
  INSERT INTO audit_log_export_sequences(tenant_id,audit_log_id)
  VALUES(NEW.tenant_id,NEW.audit_log_id);
END;

-- 业务门禁已证明五张会话风险表为空，按 child-to-parent 顺序移除 0062 私有对象。
DROP TABLE session_risk_events;
DROP TABLE session_risk_observations;
DROP TABLE session_risk_actions;
DROP TABLE session_risk_freezes;
DROP TABLE session_risk_scores;

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
  SELECT permission_id FROM permissions WHERE code IN ('session_risk.read','session_risk.manage')
);
-- 产品角色模板同步删除两项权限对应的四条默认授权，恢复 0061 的 151 条关系模板。
DELETE FROM rbac_role_permission_templates
WHERE permission_code IN ('session_risk.read','session_risk.manage');

-- 产品 Permission 模板恢复 revision 6 的 57 项，保留旧 rowid 和全部展示元数据。
CREATE TABLE rbac_permission_templates_prd070_old (
  permission_code TEXT PRIMARY KEY CHECK (
    length(permission_code) BETWEEN 3 AND 80
    AND permission_code=lower(permission_code)
    AND permission_code NOT GLOB '*[^a-z0-9._]*'
  ),
  category TEXT NOT NULL CHECK (length(category) BETWEEN 2 AND 40),
  name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
  description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 300),
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=6)
);
-- 旧 57 项保留 rowid 与展示元数据，仅把 catalog revision 恢复为 6。
INSERT INTO rbac_permission_templates_prd070_old(
  rowid,permission_code,category,name,description,risk_level,catalog_revision
)
SELECT rowid,permission_code,category,name,description,risk_level,6
FROM rbac_permission_templates WHERE permission_code NOT IN ('session_risk.read','session_risk.manage');
DROP TABLE rbac_permission_templates;
-- 完整复制后恢复正式目录名，再重新启用产品元数据不可变 trigger。
ALTER TABLE rbac_permission_templates_prd070_old RENAME TO rbac_permission_templates;
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

-- 租户 Permission 投影恢复 revision 6；除新权限外的实体 ID、rowid 和时间全部保真。
CREATE TABLE permissions_prd070_old (
  permission_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  code TEXT NOT NULL,
  category TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level TEXT NOT NULL CHECK (risk_level IN ('read','write','critical')),
  catalog_revision INTEGER NOT NULL CHECK (catalog_revision=6),
  status TEXT NOT NULL CHECK (status='active'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (code) REFERENCES rbac_permission_templates(permission_code),
  UNIQUE (tenant_id,code),
  UNIQUE (tenant_id,permission_id)
);
-- 每租户旧 Permission 保留稳定 ID、rowid 和时间，只排除 0062 新权限。
INSERT INTO permissions_prd070_old(
  rowid,permission_id,tenant_id,code,category,name,description,risk_level,
  catalog_revision,status,created_at,updated_at
)
SELECT rowid,permission_id,tenant_id,code,category,name,description,risk_level,
       6,status,created_at,updated_at
FROM permissions WHERE code NOT IN ('session_risk.read','session_risk.manage');
DROP TABLE permissions;
-- 旧投影复制完整后恢复正式表名，并重建同 tenant owner 约束。
ALTER TABLE permissions_prd070_old RENAME TO permissions;
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
      WHEN 'admin' THEN '20d984052969d07a315b032df8a6351fca540117e3574e744f8c721a53c13760'
      WHEN 'security_admin' THEN 'eda14739c9562e962de0093999fc5f2fcea45b0eeca103a440d095cd1c5e6f94'
    END
WHERE role_id IN (SELECT role_id FROM prd070_roles_to_contract);

CREATE TRIGGER trg_roles_owner_insert_prd059
BEFORE INSERT ON roles FOR EACH ROW
WHEN NOT EXISTS (SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR NEW.code NOT IN ('admin','developer','viewer','finance_admin','security_admin','ops_admin')
  OR NEW.permission_revision!=1
  OR NEW.matrix_sha256!=CASE NEW.code
    WHEN 'admin' THEN '20d984052969d07a315b032df8a6351fca540117e3574e744f8c721a53c13760'
    WHEN 'developer' THEN '2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c'
    WHEN 'viewer' THEN '3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086'
    WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
    WHEN 'security_admin' THEN 'eda14739c9562e962de0093999fc5f2fcea45b0eeca103a440d095cd1c5e6f94'
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

-- 降级后新 tenant 继续使用 revision 6 目录和 hash，角色 revision 仍从 1 起步。
CREATE TRIGGER trg_tenants_rbac_initialize_prd059
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 降级后的新 tenant 先创建 revision 6 hash 对应的六个 revision 1 角色。
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
      WHEN 'admin' THEN '20d984052969d07a315b032df8a6351fca540117e3574e744f8c721a53c13760'
      WHEN 'developer' THEN '2f89388b743145e7fded2d95cc1342c7d9acb289371bf8c70cf0accddda1a05c'
      WHEN 'viewer' THEN '3339f0ea3c2ff63e251ea82a0436dc79b4e604cce4535337d1a013ee05622086'
      WHEN 'finance_admin' THEN '96b0c1f87a2e3d47e11b9d54b878d282814af589ddf7672a9d2a68cc1da97114'
      WHEN 'security_admin' THEN 'eda14739c9562e962de0093999fc5f2fcea45b0eeca103a440d095cd1c5e6f94'
      WHEN 'ops_admin' THEN '6f1fddb0cb1774760bc3efc88193968f364add4e91df1a2490ff6a4104fc154b' END,
    CASE WHEN catalog.role_code='admin' THEN 1 ELSE 0 END
  FROM (SELECT DISTINCT role_code FROM rbac_role_permission_templates) catalog;

  -- 再从 revision 6 产品目录生成 57 项同 tenant Permission 投影。
  INSERT INTO permissions(
    permission_id,tenant_id,code,category,name,description,risk_level,
    catalog_revision,status,created_at,updated_at
  )
  SELECT 'perm_'||lower(hex(randomblob(16))),NEW.tenant_id,template.permission_code,
    template.category,template.name,template.description,template.risk_level,
    template.catalog_revision,'active',strftime('%Y-%m-%dT%H:%M:%SZ','now'),
    strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM rbac_permission_templates template;

  -- 最后建立 151 项产品模板约束下的默认关系，禁止跨租户拼接。
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

-- 组合 trigger 是 RequestLog 安全摘要的最终防线；blocked/可选 failed 必须命中同 tenant/result/request。
CREATE TRIGGER trg_request_logs_intent_insert_prd069
BEFORE INSERT ON request_logs FOR EACH ROW
WHEN (
  (NEW.intent_risk_schedule_status='disabled'
    AND NEW.intent_risk_result_id IS NULL AND NEW.intent_risk_mode IS NULL
    AND NEW.intent_risk_type IS NULL AND NEW.intent_risk_score IS NULL
    AND NEW.intent_risk_action IS NULL AND NEW.intent_risk_reason_code IS NULL
    AND NEW.intent_risk_detector_version IS NULL AND NEW.intent_risk_input_truncated=0)
  OR (NEW.intent_risk_schedule_status='queued'
    AND NEW.intent_risk_result_id IS NOT NULL AND NEW.intent_risk_mode='async_light'
    AND NEW.intent_risk_type IS NULL AND NEW.intent_risk_score IS NULL
    AND NEW.intent_risk_action IS NULL AND NEW.intent_risk_reason_code='queued'
    AND NEW.intent_risk_detector_version IS NOT NULL)
  OR (NEW.intent_risk_schedule_status='skipped'
    AND NEW.intent_risk_result_id IS NOT NULL AND NEW.intent_risk_mode='async_light'
    AND NEW.intent_risk_type IS NULL AND NEW.intent_risk_score IS NULL
    AND NEW.intent_risk_action IS NULL
    AND NEW.intent_risk_reason_code IN ('queue_full','worker_unavailable')
    AND NEW.intent_risk_detector_version IS NOT NULL)
  OR (NEW.intent_risk_schedule_status='blocked'
    AND NEW.intent_risk_result_id IS NOT NULL AND NEW.intent_risk_mode='sync_strong'
    AND NEW.intent_risk_type IS NOT NULL AND NEW.intent_risk_score BETWEEN 9500 AND 10000
    AND NEW.intent_risk_action='block' AND NEW.intent_risk_detector_version IS NOT NULL
    AND EXISTS (
      SELECT 1 FROM intent_risk_results result
      WHERE result.tenant_id=NEW.tenant_id AND result.source_kind='gateway'
        AND result.source_request_id=NEW.request_id
        AND result.intent_risk_result_id=NEW.intent_risk_result_id
        AND result.status='blocked' AND result.classification_mode=NEW.intent_risk_mode
        AND result.risk_type=NEW.intent_risk_type AND result.risk_score=NEW.intent_risk_score
        AND result.action=NEW.intent_risk_action AND result.reason_code=NEW.intent_risk_reason_code
        AND result.detector_version=NEW.intent_risk_detector_version
        AND result.input_truncated=NEW.intent_risk_input_truncated
    ))
  OR (NEW.intent_risk_schedule_status='failed_closed'
    AND NEW.intent_risk_mode='sync_strong' AND NEW.intent_risk_type IS NULL
    AND NEW.intent_risk_score IS NULL AND NEW.intent_risk_action='none'
    AND NEW.intent_risk_reason_code IN ('classifier_failed','result_store_unavailable')
    AND NEW.intent_risk_detector_version IS NOT NULL
    AND (
      (NEW.intent_risk_reason_code='result_store_unavailable'
        AND NEW.intent_risk_result_id IS NULL)
      OR (NEW.intent_risk_reason_code='classifier_failed'
        AND (NEW.intent_risk_result_id IS NULL OR EXISTS (
          SELECT 1 FROM intent_risk_results result
          WHERE result.tenant_id=NEW.tenant_id AND result.source_kind='gateway'
            AND result.source_request_id=NEW.request_id
            AND result.intent_risk_result_id=NEW.intent_risk_result_id
            AND result.status='failed' AND result.classification_mode='sync_strong'
            AND result.detector_version=NEW.intent_risk_detector_version
            AND result.input_truncated=NEW.intent_risk_input_truncated
        )))
    ))
) IS NOT TRUE
BEGIN SELECT RAISE(ABORT,'request_log_intent_state_invalid'); END;

CREATE TRIGGER trg_request_logs_intent_update_prd069
BEFORE UPDATE OF
  tenant_id,request_id,intent_risk_result_id,intent_risk_schedule_status,intent_risk_mode,
  intent_risk_type,intent_risk_score,intent_risk_action,intent_risk_reason_code,
  intent_risk_detector_version,intent_risk_input_truncated
ON request_logs FOR EACH ROW
WHEN
  -- 已形成的安全事实不得被普通 upsert 覆盖、回退或切换 detector/result identity。
  (OLD.intent_risk_result_id IS NOT NULL AND NEW.intent_risk_result_id IS NOT OLD.intent_risk_result_id)
  OR (OLD.intent_risk_schedule_status!='disabled'
      AND NEW.intent_risk_schedule_status IS NOT OLD.intent_risk_schedule_status)
  OR (OLD.intent_risk_mode IS NOT NULL AND NEW.intent_risk_mode IS NOT OLD.intent_risk_mode)
  OR (OLD.intent_risk_detector_version IS NOT NULL
      AND NEW.intent_risk_detector_version IS NOT OLD.intent_risk_detector_version)
  OR (
    (NEW.intent_risk_schedule_status='disabled'
      AND NEW.intent_risk_result_id IS NULL AND NEW.intent_risk_mode IS NULL
      AND NEW.intent_risk_type IS NULL AND NEW.intent_risk_score IS NULL
      AND NEW.intent_risk_action IS NULL AND NEW.intent_risk_reason_code IS NULL
      AND NEW.intent_risk_detector_version IS NULL AND NEW.intent_risk_input_truncated=0)
    OR (NEW.intent_risk_schedule_status='queued'
      AND NEW.intent_risk_result_id IS NOT NULL AND NEW.intent_risk_mode='async_light'
      AND NEW.intent_risk_type IS NULL AND NEW.intent_risk_score IS NULL
      AND NEW.intent_risk_action IS NULL AND NEW.intent_risk_reason_code='queued'
      AND NEW.intent_risk_detector_version IS NOT NULL)
    OR (NEW.intent_risk_schedule_status='skipped'
      AND NEW.intent_risk_result_id IS NOT NULL AND NEW.intent_risk_mode='async_light'
      AND NEW.intent_risk_type IS NULL AND NEW.intent_risk_score IS NULL
      AND NEW.intent_risk_action IS NULL
      AND NEW.intent_risk_reason_code IN ('queue_full','worker_unavailable')
      AND NEW.intent_risk_detector_version IS NOT NULL)
    OR (NEW.intent_risk_schedule_status='blocked'
      AND NEW.intent_risk_result_id IS NOT NULL AND NEW.intent_risk_mode='sync_strong'
      AND NEW.intent_risk_type IS NOT NULL AND NEW.intent_risk_score BETWEEN 9500 AND 10000
      AND NEW.intent_risk_action='block' AND NEW.intent_risk_detector_version IS NOT NULL
      AND EXISTS (
        SELECT 1 FROM intent_risk_results result
        WHERE result.tenant_id=NEW.tenant_id AND result.source_kind='gateway'
          AND result.source_request_id=NEW.request_id
          AND result.intent_risk_result_id=NEW.intent_risk_result_id
          AND result.status='blocked' AND result.classification_mode=NEW.intent_risk_mode
          AND result.risk_type=NEW.intent_risk_type AND result.risk_score=NEW.intent_risk_score
          AND result.action=NEW.intent_risk_action AND result.reason_code=NEW.intent_risk_reason_code
          AND result.detector_version=NEW.intent_risk_detector_version
          AND result.input_truncated=NEW.intent_risk_input_truncated
      ))
    OR (NEW.intent_risk_schedule_status='failed_closed'
      AND NEW.intent_risk_mode='sync_strong' AND NEW.intent_risk_type IS NULL
      AND NEW.intent_risk_score IS NULL AND NEW.intent_risk_action='none'
      AND NEW.intent_risk_reason_code IN ('classifier_failed','result_store_unavailable')
      AND NEW.intent_risk_detector_version IS NOT NULL
      AND (
        (NEW.intent_risk_reason_code='result_store_unavailable'
          AND NEW.intent_risk_result_id IS NULL)
        OR (NEW.intent_risk_reason_code='classifier_failed'
          AND (NEW.intent_risk_result_id IS NULL OR EXISTS (
            SELECT 1 FROM intent_risk_results result
            WHERE result.tenant_id=NEW.tenant_id AND result.source_kind='gateway'
              AND result.source_request_id=NEW.request_id
              AND result.intent_risk_result_id=NEW.intent_risk_result_id
              AND result.status='failed' AND result.classification_mode='sync_strong'
              AND result.detector_version=NEW.intent_risk_detector_version
              AND result.input_truncated=NEW.intent_risk_input_truncated
          )))
      ))
  ) IS NOT TRUE
BEGIN SELECT RAISE(ABORT,'request_log_intent_state_invalid'); END;

-- 提交前证明精确恢复 0061；任何半完成换表、缺失 owner trigger、角色未收缩或
-- 0062 对象残留都会使本事务整体回滚，不向迁移执行器暴露不可验收的中间状态。
DROP TABLE IF EXISTS temp.prd070_down_final_guard;
CREATE TEMP TABLE prd070_down_final_guard (
  issue_count INTEGER NOT NULL CONSTRAINT prd070_down_final_ok CHECK (issue_count=0)
);
-- 提交前汇总 0061 目录、表形状、对象残留和完整性，任何偏差都会回滚全部 DDL。
INSERT INTO prd070_down_final_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM rbac_permission_templates)=57
      AND (SELECT COUNT(*) FROM rbac_permission_templates WHERE catalog_revision=6)=57
      AND (SELECT COUNT(*) FROM rbac_role_permission_templates)=151
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM permissions)=(SELECT COUNT(*)*57 FROM tenants)
      AND (SELECT COUNT(*) FROM permissions WHERE catalog_revision=6)
          =(SELECT COUNT(*) FROM permissions)
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM prd070_roles_to_contract source
     JOIN roles role ON role.tenant_id=source.tenant_id AND role.role_id=source.role_id
     WHERE role.permission_revision!=CASE
             WHEN source.permission_revision>1 THEN source.permission_revision-1 ELSE 1 END
        OR role.matrix_sha256!=CASE role.code
          WHEN 'admin' THEN '20d984052969d07a315b032df8a6351fca540117e3574e744f8c721a53c13760'
          WHEN 'security_admin' THEN 'eda14739c9562e962de0093999fc5f2fcea45b0eeca103a440d095cd1c5e6f94' END)
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('request_logs'))=61
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='index'
           AND tbl_name='request_logs' AND name NOT LIKE 'sqlite_autoindex%')=24
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
           AND tbl_name='request_logs')=14 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('intent_risk_results'))=22
      AND (SELECT COUNT(*) FROM pragma_table_info('intent_risk_events'))=19
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='index'
           AND tbl_name IN ('intent_risk_results','intent_risk_events')
           AND name NOT LIKE 'sqlite_autoindex%')=3
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
           AND tbl_name IN ('intent_risk_results','intent_risk_events'))=6
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM pragma_table_info('audit_logs'))=17
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='index'
           AND tbl_name='audit_logs' AND name NOT LIKE 'sqlite_autoindex%')=14
      AND (SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger'
           AND tbl_name='audit_logs')=7
      AND NOT EXISTS (
        SELECT 1 FROM sqlite_schema object
        WHERE object.type='table' AND object.name='audit_logs'
          AND (instr(object.sql,"'session_risk.freeze_session'")>0
            OR instr(object.sql,"'session_risk.freeze_virtual_key'")>0
            OR instr(object.sql,"'session_risk.dismiss'")>0
            OR instr(object.sql,"'session_risk.release_session'")>0
            OR instr(object.sql,"'session_risk.release_virtual_key'")>0
            OR instr(object.sql,"'session_risk'")>0)
      )
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema object
     WHERE object.name IN (
       'session_risk_scores','session_risk_observations','session_risk_freezes',
       'session_risk_actions','session_risk_events',
       'idx_audit_logs_session_risk_action_unique_prd070',
       'trg_audit_logs_session_risk_owner_insert_prd070'
     ) OR object.name LIKE '%prd070_old' OR object.name LIKE '%prd070_new')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd070_down_final_guard;
DROP TABLE prd070_roles_to_contract;

COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
