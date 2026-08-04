-- PRD-047：为 ModelAlias 增加成本优先/可用性优先路由策略，并保存每次请求的强类型候选事实。
-- 安全边界：本迁移只保存模型、Provider、策略、版本和有界原因码，不保存 Prompt、Response、Key、Token 或 Endpoint。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- 新表的复合外键要求父表存在同顺序 UNIQUE；既有 User/Model/VirtualKey 复合索引由 0033/0034 提供。
CREATE UNIQUE INDEX uq_environments_tenant_environment_prd047
  ON environments(tenant_id, environment_id);
CREATE UNIQUE INDEX uq_model_aliases_tenant_id_prd047
  ON model_aliases(tenant_id, model_alias_id);
CREATE UNIQUE INDEX uq_providers_tenant_id_prd047
  ON providers(tenant_id, provider_id);

-- 保留 Provider 前主备最坏总预留，同时允许终态精确覆盖仍未知的上游分项。
-- NULL 沿用 0035 兼容语义；非 NULL 即使已有备用 Usage 也继续计入，避免主失败账单被整笔结清。
ALTER TABLE budget_reservations
  ADD COLUMN outstanding_cost_cny NUMERIC
  CHECK (outstanding_cost_cny IS NULL OR outstanding_cost_cny >= 0);

-- 在任何结构替换前验证存量路由图；发现跨租户关系时整笔迁移失败关闭。
DROP TABLE IF EXISTS prd047_preflight_guard;
CREATE TABLE prd047_preflight_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count = 0)
);
-- 汇总所有存量 owner 图问题，非零计数利用 CHECK 原子中止迁移。
INSERT INTO prd047_preflight_guard(issue_count)
SELECT
  (SELECT COUNT(*)
   FROM models m
   LEFT JOIN providers p
     ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
   WHERE p.provider_id IS NULL)
  +
  (SELECT COUNT(*)
   FROM model_aliases a
   LEFT JOIN environments e
     ON e.tenant_id=a.tenant_id AND e.environment_id=a.environment_id
   LEFT JOIN models dm
     ON dm.tenant_id=a.tenant_id AND dm.model_id=a.default_model_id
   LEFT JOIN models fm
     ON fm.tenant_id=a.tenant_id AND fm.model_id=a.fallback_model_id
   WHERE e.environment_id IS NULL
      OR e.code != a.environment
      OR dm.model_id IS NULL
      OR (a.fallback_model_id IS NOT NULL AND fm.model_id IS NULL)
      OR a.fallback_model_id IS a.default_model_id)
  +
  (SELECT COUNT(*)
   FROM routing_decisions d
   LEFT JOIN environments e
     ON e.tenant_id=d.tenant_id AND e.environment_id=d.environment_id
   LEFT JOIN model_aliases a
     ON a.tenant_id=d.tenant_id AND a.model_alias_id=d.model_alias_id
   LEFT JOIN models m
     ON m.tenant_id=d.tenant_id AND m.model_id=d.selected_model_id
   LEFT JOIN providers p
     ON p.tenant_id=d.tenant_id AND p.provider_id=d.provider_id
   WHERE (d.virtual_key_id IS NOT NULL AND NOT EXISTS (
          SELECT 1
          FROM virtual_keys vk
          WHERE vk.tenant_id=d.tenant_id
            AND vk.virtual_key_id=d.virtual_key_id
            AND (
              (vk.environment_id IS NOT NULL AND vk.environment_id=d.environment_id)
              OR (
                vk.environment_id IS NULL
                AND vk.environment=d.environment
                AND EXISTS (
                  SELECT 1
                  FROM environments ve
                  WHERE ve.tenant_id=d.tenant_id
                    AND ve.environment_id=d.environment_id
                    AND ve.code=d.environment
                )
              )
            )
        ))
      OR (d.environment_id IS NOT NULL AND (e.environment_id IS NULL OR e.code != d.environment))
      OR (d.model_alias_id IS NOT NULL AND a.model_alias_id IS NULL)
      OR (d.model_alias_id IS NOT NULL AND a.environment_id IS NOT d.environment_id)
      OR (d.selected_model_id IS NOT NULL AND m.model_id IS NULL)
      OR (d.provider_id IS NOT NULL AND p.provider_id IS NULL)
      OR ((d.selected_model_id IS NULL) != (d.provider_id IS NULL))
      OR (d.selected_model_id IS NOT NULL AND m.provider_id != d.provider_id));
DROP TABLE prd047_preflight_guard;

-- RoutingPolicy 与 ModelAlias 一对一；候选配置继续以 Alias 的 default/fallback 字段为唯一来源。
CREATE TABLE model_alias_routing_policies (
  routing_policy_id TEXT PRIMARY KEY CHECK (
    length(routing_policy_id)=37
    AND substr(routing_policy_id, 1, 5)='rpol_'
    AND substr(routing_policy_id, 6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  model_alias_id TEXT NOT NULL,
  routing_mode TEXT NOT NULL CHECK (
    routing_mode IN ('fixed', 'cost_first', 'availability_first')
  ),
  status TEXT NOT NULL CHECK (status='enabled'),
  version INTEGER NOT NULL CHECK (typeof(version)='integer' AND version>=1),
  created_by TEXT,
  updated_by TEXT,
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  updated_at TEXT NOT NULL CHECK (length(updated_at) BETWEEN 1 AND 64),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, model_alias_id)
    REFERENCES model_aliases(tenant_id, model_alias_id),
  FOREIGN KEY (tenant_id, created_by) REFERENCES users(tenant_id, user_id),
  FOREIGN KEY (tenant_id, updated_by) REFERENCES users(tenant_id, user_id),
  UNIQUE (tenant_id, model_alias_id),
  UNIQUE (tenant_id, routing_policy_id)
);

-- 存量 Alias 的执行语义必须完全保持 fixed；无可信同租户 actor 时保留 NULL，不能伪造迁移操作者。
INSERT INTO model_alias_routing_policies(
  routing_policy_id, tenant_id, model_alias_id, routing_mode, status, version,
  created_by, updated_by, created_at, updated_at
)
SELECT
  'rpol_' || lower(hex(randomblob(16))),
  a.tenant_id,
  a.model_alias_id,
  'fixed',
  'enabled',
  1,
  CASE WHEN EXISTS (
    SELECT 1 FROM users u
    WHERE u.tenant_id=a.tenant_id AND u.user_id=a.created_by
  ) THEN a.created_by ELSE NULL END,
  CASE WHEN EXISTS (
    SELECT 1 FROM users u
    WHERE u.tenant_id=a.tenant_id AND u.user_id=a.created_by
  ) THEN a.created_by ELSE NULL END,
  a.created_at,
  a.updated_at
FROM model_aliases a;

-- 回填数量和一对一关系必须与 Alias 完全一致，避免部分策略上线后出现不可预测混合语义。
DROP TABLE IF EXISTS prd047_policy_backfill_guard;
CREATE TABLE prd047_policy_backfill_guard (
  alias_count INTEGER NOT NULL,
  policy_count INTEGER NOT NULL,
  non_fixed_count INTEGER NOT NULL,
  CHECK (alias_count=policy_count AND non_fixed_count=0)
);
-- 对比 Alias、策略总数与非 fixed 行，证明回填没有遗漏或改变存量执行语义。
INSERT INTO prd047_policy_backfill_guard(alias_count, policy_count, non_fixed_count)
SELECT
  (SELECT COUNT(*) FROM model_aliases),
  (SELECT COUNT(*) FROM model_alias_routing_policies),
  (SELECT COUNT(*) FROM model_alias_routing_policies
   WHERE routing_mode!='fixed' OR version!=1 OR status!='enabled');
DROP TABLE prd047_policy_backfill_guard;

CREATE INDEX idx_routing_policies_tenant_mode
  ON model_alias_routing_policies(tenant_id, routing_mode, updated_at DESC);

-- Model 的 Provider 归属必须由数据库兜底，服务层 tenant 过滤不能保护直接 SQL 或导入脚本。
CREATE TRIGGER trg_models_prd047_owner_insert
BEFORE INSERT ON models
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM providers p
  WHERE p.tenant_id=NEW.tenant_id AND p.provider_id=NEW.provider_id
)
BEGIN
  SELECT RAISE(ABORT, 'models.provider_owner_invalid');
END;
CREATE TRIGGER trg_models_prd047_owner_update
BEFORE UPDATE OF tenant_id, provider_id ON models
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM providers p
  WHERE p.tenant_id=NEW.tenant_id AND p.provider_id=NEW.provider_id
)
BEGIN
  SELECT RAISE(ABORT, 'models.provider_owner_invalid');
END;

-- Alias owner trigger 同时校验 Environment 兼容代码、主备租户归属和主备不同这一完整路由图。
CREATE TRIGGER trg_model_aliases_prd047_owner_insert
BEFORE INSERT ON model_aliases
FOR EACH ROW
WHEN
  NEW.environment_id IS NULL
  OR NOT EXISTS (
    SELECT 1 FROM environments e
    WHERE e.tenant_id=NEW.tenant_id
      AND e.environment_id=NEW.environment_id
      AND e.code=NEW.environment
  )
  OR NOT EXISTS (
    SELECT 1 FROM models m
    WHERE m.tenant_id=NEW.tenant_id AND m.model_id=NEW.default_model_id
  )
  OR (
    NEW.fallback_model_id IS NOT NULL
    AND NOT EXISTS (
      SELECT 1 FROM models m
      WHERE m.tenant_id=NEW.tenant_id AND m.model_id=NEW.fallback_model_id
    )
  )
  OR NEW.fallback_model_id IS NEW.default_model_id
BEGIN
  SELECT RAISE(ABORT, 'model_aliases.owner_graph_invalid');
END;
CREATE TRIGGER trg_model_aliases_prd047_owner_update
BEFORE UPDATE OF tenant_id, environment_id, environment, default_model_id, fallback_model_id
ON model_aliases
FOR EACH ROW
WHEN
  NEW.environment_id IS NULL
  OR NOT EXISTS (
    SELECT 1 FROM environments e
    WHERE e.tenant_id=NEW.tenant_id
      AND e.environment_id=NEW.environment_id
      AND e.code=NEW.environment
  )
  OR NOT EXISTS (
    SELECT 1 FROM models m
    WHERE m.tenant_id=NEW.tenant_id AND m.model_id=NEW.default_model_id
  )
  OR (
    NEW.fallback_model_id IS NOT NULL
    AND NOT EXISTS (
      SELECT 1 FROM models m
      WHERE m.tenant_id=NEW.tenant_id AND m.model_id=NEW.fallback_model_id
    )
  )
  OR NEW.fallback_model_id IS NEW.default_model_id
BEGIN
  SELECT RAISE(ABORT, 'model_aliases.owner_graph_invalid');
END;

-- Alias ID/tenant 是 RoutingPolicy 一对一身份的一部分，迁移后不允许通过 UPDATE 换绑。
CREATE TRIGGER trg_model_aliases_prd047_identity_update
BEFORE UPDATE OF tenant_id, model_alias_id ON model_aliases
FOR EACH ROW
WHEN NEW.tenant_id IS NOT OLD.tenant_id OR NEW.model_alias_id IS NOT OLD.model_alias_id
BEGIN
  SELECT RAISE(ABORT, 'model_aliases.identity_immutable');
END;

-- 新 Alias 由数据库自动得到 fixed v1；ID、actor 和时间只取本次 NEW 行，服务不得双写第二条策略。
CREATE TRIGGER trg_model_aliases_prd047_policy_insert
AFTER INSERT ON model_aliases
FOR EACH ROW
BEGIN
  -- 新 Alias 的 fixed v1 与业务行同事务生成，ID 与 actor 只采用 NEW 的可信字段。
  INSERT INTO model_alias_routing_policies(
    routing_policy_id, tenant_id, model_alias_id, routing_mode, status, version,
    created_by, updated_by, created_at, updated_at
  )
  VALUES(
    'rpol_' || lower(hex(randomblob(16))), NEW.tenant_id, NEW.model_alias_id,
    'fixed', 'enabled', 1, NEW.created_by, NEW.created_by, NEW.created_at, NEW.updated_at
  );
END;

-- 一条 Alias UPDATE 即使同时修改多个路由字段也只触发一次严格 +1；同值更新不产生新版本。
CREATE TRIGGER trg_model_aliases_prd047_policy_update
AFTER UPDATE OF alias, environment, environment_id, status, default_model_id, fallback_model_id
ON model_aliases
FOR EACH ROW
WHEN
  NEW.alias IS NOT OLD.alias
  OR NEW.environment IS NOT OLD.environment
  OR NEW.environment_id IS NOT OLD.environment_id
  OR NEW.status IS NOT OLD.status
  OR NEW.default_model_id IS NOT OLD.default_model_id
  OR NEW.fallback_model_id IS NOT OLD.fallback_model_id
BEGIN
  -- 一条 Alias UPDATE 无论改变几个路由字段都只推进一次版本，防止同图多版本。
  UPDATE model_alias_routing_policies
  SET version=version+1, updated_at=NEW.updated_at
  WHERE tenant_id=NEW.tenant_id AND model_alias_id=NEW.model_alias_id;
END;

-- Policy 身份不可换绑；Routing mode/status 变化必须严格 +1，任意版本倒退或跳号也必须拒绝。
CREATE TRIGGER trg_routing_policies_prd047_identity_update
BEFORE UPDATE OF routing_policy_id, tenant_id, model_alias_id
ON model_alias_routing_policies
FOR EACH ROW
WHEN
  NEW.routing_policy_id IS NOT OLD.routing_policy_id
  OR NEW.tenant_id IS NOT OLD.tenant_id
  OR NEW.model_alias_id IS NOT OLD.model_alias_id
BEGIN
  SELECT RAISE(ABORT, 'routing_policy_identity_immutable');
END;
CREATE TRIGGER trg_routing_policies_prd047_version_update
BEFORE UPDATE OF routing_mode, status, version
ON model_alias_routing_policies
FOR EACH ROW
WHEN
  NEW.version < OLD.version
  OR NEW.version > OLD.version + 1
  OR (
    (NEW.routing_mode IS NOT OLD.routing_mode OR NEW.status IS NOT OLD.status)
    AND NEW.version != OLD.version + 1
  )
BEGIN
  SELECT RAISE(ABORT, 'routing_policy_version_invalid');
END;
CREATE TRIGGER trg_routing_policies_prd047_delete
BEFORE DELETE ON model_alias_routing_policies
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM model_aliases a
  WHERE a.tenant_id=OLD.tenant_id AND a.model_alias_id=OLD.model_alias_id
)
BEGIN
  SELECT RAISE(ABORT, 'routing_policy_alias_bound');
END;

-- routing_decisions 需要增加策略冻结字段和 request 唯一合同，因此按 0035 全列保真重建。
DROP TABLE IF EXISTS routing_decisions_prd047_new;
CREATE TABLE routing_decisions_prd047_new (
  routing_decision_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  virtual_key_id TEXT,
  model_alias_id TEXT,
  requested_model TEXT NOT NULL,
  environment TEXT NOT NULL,
  selected_model_id TEXT,
  selected_internal_model_id TEXT,
  provider_id TEXT,
  provider_code TEXT,
  decision_status TEXT NOT NULL CHECK (decision_status IN ('selected', 'failed')),
  decision_reason TEXT NOT NULL,
  candidate_summary TEXT NOT NULL DEFAULT '{}' CHECK (
    json_valid(candidate_summary) AND json_type(candidate_summary)='object'
  ),
  error_code TEXT,
  created_at TEXT NOT NULL,
  environment_id TEXT,
  routing_policy_id TEXT,
  routing_policy_version INTEGER CHECK (
    routing_policy_version IS NULL
    OR (typeof(routing_policy_version)='integer' AND routing_policy_version>=1)
  ),
  routing_mode TEXT CHECK (
    routing_mode IS NULL OR routing_mode IN ('fixed', 'cost_first', 'availability_first')
  ),
  price_as_of TEXT,
  estimated_input_tokens INTEGER CHECK (
    estimated_input_tokens IS NULL
    OR (typeof(estimated_input_tokens)='integer' AND estimated_input_tokens>=0)
  ),
  estimated_output_tokens INTEGER CHECK (
    estimated_output_tokens IS NULL
    OR (typeof(estimated_output_tokens)='integer' AND estimated_output_tokens>=0)
  ),
  token_estimate_truncated INTEGER CHECK (
    token_estimate_truncated IS NULL
    OR (typeof(token_estimate_truncated)='integer' AND token_estimate_truncated IN (0, 1))
  ),
  routing_policy_event_id TEXT UNIQUE CHECK (
    routing_policy_event_id IS NULL
    OR (
      length(routing_policy_event_id)=36
      AND substr(routing_policy_event_id, 1, 4)='rpe_'
      AND substr(routing_policy_event_id, 5) NOT GLOB '*[^0-9a-f]*'
    )
  ),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, virtual_key_id)
    REFERENCES virtual_keys(tenant_id, virtual_key_id),
  FOREIGN KEY (tenant_id, environment_id)
    REFERENCES environments(tenant_id, environment_id),
  FOREIGN KEY (tenant_id, model_alias_id)
    REFERENCES model_aliases(tenant_id, model_alias_id),
  FOREIGN KEY (tenant_id, selected_model_id)
    REFERENCES models(tenant_id, model_id),
  FOREIGN KEY (tenant_id, provider_id)
    REFERENCES providers(tenant_id, provider_id),
  FOREIGN KEY (tenant_id, routing_policy_id)
    REFERENCES model_alias_routing_policies(tenant_id, routing_policy_id),
  UNIQUE (tenant_id, routing_decision_id),
  CHECK (
    (routing_policy_id IS NULL AND routing_policy_version IS NULL AND routing_mode IS NULL)
    OR
    (routing_policy_id IS NOT NULL AND routing_policy_version IS NOT NULL AND routing_mode IS NOT NULL)
  ),
  CHECK (
    routing_policy_event_id IS NULL
    OR (routing_policy_id IS NOT NULL AND decision_status='selected')
  ),
  CHECK (
    (price_as_of IS NULL AND estimated_input_tokens IS NULL
      AND estimated_output_tokens IS NULL AND token_estimate_truncated IS NULL)
    OR
    (routing_mode='cost_first' AND price_as_of IS NOT NULL
      AND estimated_input_tokens IS NOT NULL AND estimated_output_tokens IS NOT NULL
      AND token_estimate_truncated IS NOT NULL)
  )
);

-- 将 0035 Decision 的全部旧字段逐列复制，新冻结字段统一留空以保持历史语义。
INSERT INTO routing_decisions_prd047_new(
  routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
  model_alias_id, requested_model, environment, selected_model_id,
  selected_internal_model_id, provider_id, provider_code, decision_status,
  decision_reason, candidate_summary, error_code, created_at, environment_id,
  routing_policy_id, routing_policy_version, routing_mode, price_as_of,
  estimated_input_tokens, estimated_output_tokens, token_estimate_truncated,
  routing_policy_event_id
)
SELECT
  routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
  model_alias_id, requested_model, environment, selected_model_id,
  selected_internal_model_id, provider_id, provider_code, decision_status,
  decision_reason, candidate_summary, error_code, created_at, environment_id,
  NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
FROM routing_decisions;

-- 复制完成后创建临时计数守卫，任何遗漏都会在删除旧表前中止事务。
DROP TABLE IF EXISTS prd047_decision_copy_guard;
CREATE TABLE prd047_decision_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  CHECK (source_count=copied_count)
);
-- 同时读取源表与新表行数写入守卫，避免依赖应用层迁移结果统计。
INSERT INTO prd047_decision_copy_guard(source_count, copied_count)
SELECT
  (SELECT COUNT(*) FROM routing_decisions),
  (SELECT COUNT(*) FROM routing_decisions_prd047_new);
DROP TABLE prd047_decision_copy_guard;

DROP TABLE routing_decisions;
-- 仅在计数守卫通过后切换正式表名，保证下游外键引用完整 Decision 历史。
ALTER TABLE routing_decisions_prd047_new RENAME TO routing_decisions;

CREATE INDEX idx_routing_decisions_request_id
  ON routing_decisions(tenant_id, request_id);
-- 0035 允许同 request 留下多条历史 Decision；只有 0036 Policy 冻结事实必须一请求一条。
CREATE UNIQUE INDEX idx_routing_decisions_policy_request_unique
  ON routing_decisions(tenant_id, request_id)
  WHERE routing_policy_id IS NOT NULL;
CREATE INDEX idx_routing_decisions_alias_time
  ON routing_decisions(tenant_id, model_alias_id, created_at);
CREATE INDEX idx_routing_decisions_error_code
  ON routing_decisions(tenant_id, error_code);
CREATE INDEX idx_routing_decisions_environment_time
  ON routing_decisions(tenant_id, environment_id, created_at DESC);

-- Decision INSERT/关系更新必须形成同租户、同 Environment、同 Policy 版本和同 Model/Provider 的完整图。
CREATE TRIGGER trg_routing_decisions_prd047_owner_insert
BEFORE INSERT ON routing_decisions
FOR EACH ROW
WHEN
  (NEW.virtual_key_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM virtual_keys vk
    WHERE vk.tenant_id=NEW.tenant_id AND vk.virtual_key_id=NEW.virtual_key_id
  ))
  OR (NEW.environment_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM environments e
    WHERE e.tenant_id=NEW.tenant_id AND e.environment_id=NEW.environment_id
      AND e.code=NEW.environment
  ))
  OR (NEW.virtual_key_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM virtual_keys vk
    WHERE vk.tenant_id=NEW.tenant_id AND vk.virtual_key_id=NEW.virtual_key_id
      AND (
        (vk.environment_id IS NOT NULL AND vk.environment_id=NEW.environment_id)
        OR (
          vk.environment_id IS NULL
          AND vk.environment=NEW.environment
          AND EXISTS (
            SELECT 1 FROM environments ve
            WHERE ve.tenant_id=NEW.tenant_id
              AND ve.environment_id=NEW.environment_id
              AND ve.code=NEW.environment
          )
        )
      )
  ))
  OR (NEW.model_alias_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM model_aliases a
    WHERE a.tenant_id=NEW.tenant_id AND a.model_alias_id=NEW.model_alias_id
      AND a.environment_id IS NEW.environment_id
  ))
  OR (NEW.selected_model_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM models m
    WHERE m.tenant_id=NEW.tenant_id AND m.model_id=NEW.selected_model_id
  ))
  OR (NEW.provider_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM providers p
    WHERE p.tenant_id=NEW.tenant_id AND p.provider_id=NEW.provider_id
  ))
  OR ((NEW.selected_model_id IS NULL) != (NEW.provider_id IS NULL))
  OR (NEW.selected_model_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM models m
    WHERE m.tenant_id=NEW.tenant_id AND m.model_id=NEW.selected_model_id
      AND m.provider_id=NEW.provider_id
  ))
  OR (NEW.routing_policy_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM model_alias_routing_policies rp
    WHERE rp.tenant_id=NEW.tenant_id
      AND rp.routing_policy_id=NEW.routing_policy_id
      AND rp.model_alias_id=NEW.model_alias_id
      AND rp.version=NEW.routing_policy_version
      AND rp.routing_mode=NEW.routing_mode
  ))
BEGIN
  SELECT RAISE(ABORT, 'routing_decision_owner_graph_invalid');
END;
CREATE TRIGGER trg_routing_decisions_prd047_owner_update
BEFORE UPDATE OF tenant_id, virtual_key_id, environment_id, environment,
  model_alias_id, selected_model_id, provider_id, routing_policy_id,
  routing_policy_version, routing_mode
ON routing_decisions
FOR EACH ROW
WHEN
  (NEW.virtual_key_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM virtual_keys vk
    WHERE vk.tenant_id=NEW.tenant_id AND vk.virtual_key_id=NEW.virtual_key_id
      AND (
        (vk.environment_id IS NOT NULL AND vk.environment_id=NEW.environment_id)
        OR (
          vk.environment_id IS NULL
          AND vk.environment=NEW.environment
          AND EXISTS (
            SELECT 1 FROM environments ve
            WHERE ve.tenant_id=NEW.tenant_id
              AND ve.environment_id=NEW.environment_id
              AND ve.code=NEW.environment
          )
        )
      )
  ))
  OR (NEW.environment_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM environments e
    WHERE e.tenant_id=NEW.tenant_id AND e.environment_id=NEW.environment_id
      AND e.code=NEW.environment
  ))
  OR (NEW.model_alias_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM model_aliases a
    WHERE a.tenant_id=NEW.tenant_id AND a.model_alias_id=NEW.model_alias_id
      AND a.environment_id IS NEW.environment_id
  ))
  OR ((NEW.selected_model_id IS NULL) != (NEW.provider_id IS NULL))
  OR (NEW.selected_model_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM models m
    WHERE m.tenant_id=NEW.tenant_id AND m.model_id=NEW.selected_model_id
      AND m.provider_id=NEW.provider_id
  ))
  OR (NEW.routing_policy_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM model_alias_routing_policies rp
    WHERE rp.tenant_id=NEW.tenant_id
      AND rp.routing_policy_id=NEW.routing_policy_id
      AND rp.model_alias_id=NEW.model_alias_id
      AND rp.version=NEW.routing_policy_version
      AND rp.routing_mode=NEW.routing_mode
  ))
BEGIN
  SELECT RAISE(ABORT, 'routing_decision_owner_graph_invalid');
END;

-- 低频配置事件是提交后重派 Webhook 的权威事实；完整请求正文和描述明文不进入本表。
CREATE TABLE routing_policy_events (
  routing_policy_event_id TEXT PRIMARY KEY CHECK (
    length(routing_policy_event_id)=36
    AND substr(routing_policy_event_id, 1, 4)='rpe_'
    AND substr(routing_policy_event_id, 5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  routing_policy_id TEXT NOT NULL,
  model_alias_id TEXT NOT NULL,
  routing_policy_version INTEGER NOT NULL CHECK (
    typeof(routing_policy_version)='integer' AND routing_policy_version>=1
  ),
  event_type TEXT NOT NULL CHECK (event_type='route.policy_selected'),
  trigger_type TEXT NOT NULL CHECK (trigger_type='configuration_changed'),
  actor_user_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK (length(trace_id) BETWEEN 1 AND 128),
  request_fingerprint TEXT NOT NULL CHECK (
    length(request_fingerprint)=64
    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  payload_json TEXT NOT NULL CHECK (
    json_valid(payload_json)
    AND json_type(payload_json)='object'
    AND length(CAST(payload_json AS BLOB))<=8192
  ),
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, routing_policy_id)
    REFERENCES model_alias_routing_policies(tenant_id, routing_policy_id),
  FOREIGN KEY (tenant_id, model_alias_id)
    REFERENCES model_aliases(tenant_id, model_alias_id),
  FOREIGN KEY (tenant_id, actor_user_id) REFERENCES users(tenant_id, user_id),
  UNIQUE (tenant_id, routing_policy_id, routing_policy_version, trigger_type),
  UNIQUE (tenant_id, actor_user_id, request_id),
  CHECK (
    COALESCE(
      json_remove(
        payload_json,
        '$.trigger', '$.request_id', '$.model_alias_id', '$.routing_policy_id',
        '$.routing_policy_version', '$.change_kinds', '$.changed_routing_fields',
        '$.previous_mode', '$.routing_mode'
      )='{}'
      AND json_extract(payload_json, '$.trigger')=trigger_type
      AND json_extract(payload_json, '$.request_id')=request_id
      AND json_extract(payload_json, '$.model_alias_id')=model_alias_id
      AND json_extract(payload_json, '$.routing_policy_id')=routing_policy_id
      AND json_extract(payload_json, '$.routing_policy_version')=routing_policy_version
      AND json_type(payload_json, '$.change_kinds')='array'
      AND json_array_length(payload_json, '$.change_kinds') BETWEEN 1 AND 4
      AND json_type(payload_json, '$.changed_routing_fields')='array'
      AND json_array_length(payload_json, '$.changed_routing_fields') BETWEEN 1 AND 7
      AND (
        json_type(payload_json, '$.previous_mode')='null'
        OR json_extract(payload_json, '$.previous_mode')
          IN ('fixed', 'cost_first', 'availability_first')
      )
      AND json_extract(payload_json, '$.routing_mode')
        IN ('fixed', 'cost_first', 'availability_first'),
      0
    )
  )
);

CREATE INDEX idx_routing_policy_events_tenant_time
  ON routing_policy_events(tenant_id, created_at DESC, routing_policy_event_id DESC);
CREATE INDEX idx_routing_policy_events_policy_version
  ON routing_policy_events(tenant_id, routing_policy_id, routing_policy_version DESC);

-- 配置事件数组必须复现应用层固定顺序；逐元素校验可阻止未知值、非字符串和重复项绕过长度约束。
CREATE TRIGGER trg_routing_policy_events_prd047_payload_insert
BEFORE INSERT ON routing_policy_events
FOR EACH ROW
WHEN CASE
  WHEN json_valid(NEW.payload_json)!=1 THEN 1
  WHEN json_type(NEW.payload_json) IS NOT 'object' THEN 1
  WHEN json_type(NEW.payload_json, '$.change_kinds') IS NOT 'array' THEN 1
  WHEN json_type(NEW.payload_json, '$.changed_routing_fields') IS NOT 'array' THEN 1
  WHEN json_array_length(NEW.payload_json, '$.change_kinds') NOT BETWEEN 1 AND 4 THEN 1
  WHEN json_array_length(NEW.payload_json, '$.changed_routing_fields') NOT BETWEEN 1 AND 7 THEN 1
  WHEN EXISTS (
    SELECT 1
    FROM json_each(NEW.payload_json, '$.change_kinds') item
    WHERE item.type!='text'
      OR item.value NOT IN ('created', 'mode', 'candidates', 'alias_state')
  ) THEN 1
  WHEN EXISTS (
    SELECT 1
    FROM json_each(NEW.payload_json, '$.change_kinds') item
    WHERE item.value='created'
  ) AND json_array_length(NEW.payload_json, '$.change_kinds')!=1 THEN 1
  WHEN EXISTS (
    SELECT 1
    FROM json_each(NEW.payload_json, '$.change_kinds') earlier
    JOIN json_each(NEW.payload_json, '$.change_kinds') later
      ON CAST(earlier.key AS INTEGER)<CAST(later.key AS INTEGER)
    WHERE
      CASE earlier.value
        WHEN 'created' THEN 0
        WHEN 'mode' THEN 1
        WHEN 'candidates' THEN 2
        WHEN 'alias_state' THEN 3
        ELSE 99
      END
      >=
      CASE later.value
        WHEN 'created' THEN 0
        WHEN 'mode' THEN 1
        WHEN 'candidates' THEN 2
        WHEN 'alias_state' THEN 3
        ELSE 99
      END
  ) THEN 1
  WHEN EXISTS (
    SELECT 1
    FROM json_each(NEW.payload_json, '$.changed_routing_fields') item
    WHERE item.type!='text'
      OR item.value NOT IN (
        'alias', 'environment', 'environment_id', 'status',
        'default_model_id', 'fallback_model_id', 'routing_mode'
      )
  ) THEN 1
  WHEN EXISTS (
    SELECT 1
    FROM json_each(NEW.payload_json, '$.changed_routing_fields') earlier
    JOIN json_each(NEW.payload_json, '$.changed_routing_fields') later
      ON CAST(earlier.key AS INTEGER)<CAST(later.key AS INTEGER)
    WHERE
      CASE earlier.value
        WHEN 'alias' THEN 0
        WHEN 'environment' THEN 1
        WHEN 'environment_id' THEN 2
        WHEN 'status' THEN 3
        WHEN 'default_model_id' THEN 4
        WHEN 'fallback_model_id' THEN 5
        WHEN 'routing_mode' THEN 6
        ELSE 99
      END
      >=
      CASE later.value
        WHEN 'alias' THEN 0
        WHEN 'environment' THEN 1
        WHEN 'environment_id' THEN 2
        WHEN 'status' THEN 3
        WHEN 'default_model_id' THEN 4
        WHEN 'fallback_model_id' THEN 5
        WHEN 'routing_mode' THEN 6
        ELSE 99
      END
  ) THEN 1
  -- created 是独立初始事实：只能是版本 1、没有前态，并完整声明七个初始路由字段。
  WHEN EXISTS (
    SELECT 1
    FROM json_each(NEW.payload_json, '$.change_kinds') item
    WHERE item.value='created'
  ) THEN CASE
    WHEN NEW.routing_policy_version!=1 THEN 1
    WHEN json_type(NEW.payload_json, '$.previous_mode') IS NOT 'null' THEN 1
    WHEN json_array_length(NEW.payload_json, '$.changed_routing_fields')!=7 THEN 1
    ELSE 0
  END
  -- 普通更新必须携带明确前态，不能借 NULL 伪装为创建事件。
  WHEN json_type(NEW.payload_json, '$.previous_mode') IS 'null' THEN 1
  -- change_kinds 必须由 changed fields 唯一推导，禁止事件类别与真实变更范围漂移。
  WHEN (
    EXISTS (
      SELECT 1 FROM json_each(NEW.payload_json, '$.change_kinds') item
      WHERE item.value='mode'
    )
    !=
    EXISTS (
      SELECT 1 FROM json_each(NEW.payload_json, '$.changed_routing_fields') item
      WHERE item.value='routing_mode'
    )
  ) THEN 1
  WHEN (
    EXISTS (
      SELECT 1 FROM json_each(NEW.payload_json, '$.change_kinds') item
      WHERE item.value='candidates'
    )
    !=
    EXISTS (
      SELECT 1 FROM json_each(NEW.payload_json, '$.changed_routing_fields') item
      WHERE item.value IN ('default_model_id', 'fallback_model_id')
    )
  ) THEN 1
  WHEN (
    EXISTS (
      SELECT 1 FROM json_each(NEW.payload_json, '$.change_kinds') item
      WHERE item.value='alias_state'
    )
    !=
    EXISTS (
      SELECT 1 FROM json_each(NEW.payload_json, '$.changed_routing_fields') item
      WHERE item.value IN ('alias', 'environment', 'environment_id', 'status')
    )
  ) THEN 1
  -- mode 更新必须是真实状态跳转；非 mode 更新则必须保持前后模式完全相同。
  WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.payload_json, '$.change_kinds') item
    WHERE item.value='mode'
  ) AND json_extract(NEW.payload_json, '$.previous_mode')
        IS json_extract(NEW.payload_json, '$.routing_mode') THEN 1
  WHEN NOT EXISTS (
    SELECT 1 FROM json_each(NEW.payload_json, '$.change_kinds') item
    WHERE item.value='mode'
  ) AND json_extract(NEW.payload_json, '$.previous_mode')
        IS NOT json_extract(NEW.payload_json, '$.routing_mode') THEN 1
  ELSE 0
END
BEGIN
  SELECT RAISE(ABORT, 'routing_policy_event_payload_invalid');
END;

CREATE TRIGGER trg_routing_policy_events_prd047_graph_insert
BEFORE INSERT ON routing_policy_events
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM model_alias_routing_policies rp
  WHERE rp.tenant_id=NEW.tenant_id
    AND rp.routing_policy_id=NEW.routing_policy_id
    AND rp.model_alias_id=NEW.model_alias_id
    AND rp.version=NEW.routing_policy_version
)
BEGIN
  SELECT RAISE(ABORT, 'routing_policy_event_graph_invalid');
END;
CREATE TRIGGER trg_routing_policy_events_prd047_immutable_update
BEFORE UPDATE ON routing_policy_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'routing_policy_event_immutable');
END;
CREATE TRIGGER trg_routing_policy_events_prd047_immutable_delete
BEFORE DELETE ON routing_policy_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'routing_policy_event_immutable');
END;

-- Candidate 是每请求不可变评估事实，最多保存 Alias 的 default/fallback 两行，不是第二份配置源。
CREATE TABLE routing_candidates (
  routing_candidate_id TEXT PRIMARY KEY CHECK (
    length(routing_candidate_id)=38
    AND substr(routing_candidate_id, 1, 6)='rcand_'
    AND substr(routing_candidate_id, 7) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  routing_decision_id TEXT NOT NULL,
  routing_policy_id TEXT,
  model_alias_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  provider_id TEXT,
  candidate_role TEXT NOT NULL CHECK (candidate_role IN ('default', 'fallback')),
  configured_priority INTEGER NOT NULL CHECK (
    typeof(configured_priority)='integer' AND configured_priority IN (0, 1)
  ),
  rank INTEGER CHECK (rank IS NULL OR (typeof(rank)='integer' AND rank IN (1, 2))),
  status TEXT NOT NULL CHECK (status IN ('selected', 'standby', 'rejected')),
  reason_code TEXT NOT NULL CHECK (
    reason_code IN (
      'routing.no_available_candidate',
      'routing.fixed_priority',
      'routing.cost_lowest',
      'routing.cost_tie_stable_priority',
      'routing.cost_fixed_fallback.price_missing',
      'routing.cost_fixed_fallback.currency_incomparable',
      'routing.availability_healthy',
      'routing.availability_unknown',
      'routing.single_eligible_candidate',
      'routing.candidate.standby',
      'routing.candidate.model_missing',
      'routing.candidate.model_disabled',
      'routing.candidate.provider_missing',
      'routing.candidate.provider_disabled',
      'routing.candidate.provider_unhealthy',
      'routing.candidate.keypool_unavailable'
    )
  ),
  health_state TEXT NOT NULL CHECK (health_state IN ('healthy', 'unhealthy', 'unknown')),
  health_fresh INTEGER NOT NULL CHECK (
    typeof(health_fresh)='integer' AND health_fresh IN (0, 1)
  ),
  keypool_state TEXT NOT NULL CHECK (
    keypool_state IN ('available', 'unavailable', 'unknown', 'not_evaluated')
  ),
  price_available INTEGER NOT NULL CHECK (
    typeof(price_available)='integer' AND price_available IN (0, 1)
  ),
  price_source TEXT NOT NULL CHECK (
    price_source IN ('price_plan', 'model_catalog', 'missing', 'not_evaluated')
  ),
  price_schema_version TEXT,
  currency TEXT CHECK (currency IS NULL OR currency IN ('CNY', 'USD')),
  estimated_cost_amount TEXT CHECK (
    estimated_cost_amount IS NULL
    OR (
      length(estimated_cost_amount) BETWEEN 17 AND 80
      AND estimated_cost_amount NOT GLOB '*[^0-9.]*'
      AND instr(estimated_cost_amount, '.')>1
      AND instr(substr(estimated_cost_amount, instr(estimated_cost_amount, '.')+1), '.')=0
      AND length(substr(estimated_cost_amount, instr(estimated_cost_amount, '.')+1))=15
    )
  ),
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  updated_at TEXT NOT NULL CHECK (updated_at=created_at),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, routing_decision_id)
    REFERENCES routing_decisions(tenant_id, routing_decision_id),
  FOREIGN KEY (tenant_id, routing_policy_id)
    REFERENCES model_alias_routing_policies(tenant_id, routing_policy_id),
  FOREIGN KEY (tenant_id, model_alias_id)
    REFERENCES model_aliases(tenant_id, model_alias_id),
  FOREIGN KEY (tenant_id, model_id) REFERENCES models(tenant_id, model_id),
  FOREIGN KEY (tenant_id, provider_id) REFERENCES providers(tenant_id, provider_id),
  UNIQUE (tenant_id, routing_decision_id, candidate_role),
  CHECK (
    (candidate_role='default' AND configured_priority=0)
    OR (candidate_role='fallback' AND configured_priority=1)
  ),
  CHECK (
    (status='selected' AND rank=1)
    OR (status='standby' AND rank=2)
    OR (status='rejected' AND rank IS NULL)
  ),
  CHECK (
    (price_source IN ('missing', 'not_evaluated')
      AND price_available=0 AND price_schema_version IS NULL
      AND currency IS NULL AND estimated_cost_amount IS NULL)
    OR
    (price_source IN ('price_plan', 'model_catalog')
      AND price_available=1 AND price_schema_version IS NOT NULL
      AND currency IS NOT NULL AND estimated_cost_amount IS NOT NULL)
  )
);

CREATE UNIQUE INDEX idx_routing_candidates_decision_rank_unique
  ON routing_candidates(tenant_id, routing_decision_id, rank)
  WHERE rank IS NOT NULL;
CREATE INDEX idx_routing_candidates_decision_order
  ON routing_candidates(tenant_id, routing_decision_id, rank, configured_priority);

-- Candidate graph trigger 固定 role/priority 对应的 Alias 目标，并核对 Policy、Model/Provider 与最终选择。
CREATE TRIGGER trg_routing_candidates_prd047_graph_insert
BEFORE INSERT ON routing_candidates
FOR EACH ROW
WHEN
  NOT EXISTS (
    SELECT 1 FROM routing_decisions d
    WHERE d.tenant_id=NEW.tenant_id
      AND d.routing_decision_id=NEW.routing_decision_id
      AND d.model_alias_id=NEW.model_alias_id
      AND d.routing_policy_id IS NEW.routing_policy_id
  )
  OR (
    NEW.routing_policy_id IS NOT NULL AND NOT EXISTS (
      SELECT 1
      FROM routing_decisions d
      JOIN model_alias_routing_policies rp
        ON rp.tenant_id=d.tenant_id
       AND rp.routing_policy_id=d.routing_policy_id
      WHERE d.tenant_id=NEW.tenant_id
        AND d.routing_decision_id=NEW.routing_decision_id
        AND rp.model_alias_id=d.model_alias_id
        AND rp.version=d.routing_policy_version
        AND rp.routing_mode=d.routing_mode
    )
  )
  OR NOT EXISTS (
    SELECT 1 FROM model_aliases a
    WHERE a.tenant_id=NEW.tenant_id AND a.model_alias_id=NEW.model_alias_id
      AND (
        (NEW.candidate_role='default' AND a.default_model_id=NEW.model_id)
        OR
        (NEW.candidate_role='fallback' AND a.fallback_model_id IS NOT NULL
          AND a.fallback_model_id=NEW.model_id)
      )
  )
  OR NOT EXISTS (
    SELECT 1 FROM models m
    WHERE m.tenant_id=NEW.tenant_id AND m.model_id=NEW.model_id
      AND m.provider_id=NEW.provider_id
  )
  OR (
    NEW.status='selected' AND NOT EXISTS (
      SELECT 1 FROM routing_decisions d
      WHERE d.tenant_id=NEW.tenant_id
        AND d.routing_decision_id=NEW.routing_decision_id
        AND d.decision_status='selected'
        AND d.selected_model_id=NEW.model_id
        AND d.provider_id=NEW.provider_id
    )
  )
  OR (
    EXISTS (
      SELECT 1 FROM routing_decisions d
      WHERE d.tenant_id=NEW.tenant_id
        AND d.routing_decision_id=NEW.routing_decision_id
        AND d.decision_status='failed'
    )
    AND NEW.status!='rejected'
  )
BEGIN
  SELECT RAISE(ABORT, 'routing_candidate_graph_invalid');
END;
CREATE TRIGGER trg_routing_candidates_prd047_immutable_update
BEFORE UPDATE ON routing_candidates
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'routing_candidate_immutable');
END;
CREATE TRIGGER trg_routing_candidates_prd047_immutable_delete
BEFORE DELETE ON routing_candidates
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'routing_candidate_immutable');
END;

-- Decision 与 Candidate 共同组成一次冻结解释；首条 Candidate 落库后禁止改写任何 Decision 字段。
CREATE TRIGGER trg_routing_decisions_prd047_candidate_bound_update
BEFORE UPDATE ON routing_decisions
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM routing_candidates c
  WHERE c.tenant_id=OLD.tenant_id
    AND c.routing_decision_id=OLD.routing_decision_id
)
BEGIN
  SELECT RAISE(ABORT, 'routing_decision_candidate_bound');
END;

-- audit_logs 仅扩展 RoutingPolicy 动作/对象；0035 的 17 列与全部历史事实逐字段复制。
DROP TABLE IF EXISTS audit_logs_prd047_new;
CREATE TABLE audit_logs_prd047_new (
  audit_log_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  actor_email TEXT NOT NULL,
  actor_role TEXT NOT NULL CHECK (actor_role IN ('admin', 'developer', 'viewer')),
  action TEXT NOT NULL CHECK (
    action IN (
      'provider.created', 'provider.updated',
      'provider_key.created', 'provider_key.updated',
      'virtual_key.created', 'virtual_key.updated',
      'budget_rule.created', 'budget_rule.updated',
      'dlp_rule.created', 'dlp_rule.updated',
      'webhook_delivery.replayed', 'webhook_endpoint.secret_rotated',
      'usage_export.requested', 'usage_export.downloaded',
      'policy.created', 'policy.updated',
      'policy_snapshot.published', 'policy_snapshot.rolled_back',
      'policy.dry_run_executed',
      'routing_policy.created', 'routing_policy.updated'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'usage_export_job', 'policy',
      'policy_snapshot', 'policy_explanation', 'routing_policy'
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
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);

-- 审计历史保留全部行和链路字段；Provider 旧摘要中的可逆 base_url 是安全例外，必须在升级时移除。
INSERT INTO audit_logs_prd047_new(
  audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
  action, object_type, object_id, object_name, before_summary_json,
  after_summary_json, changed_fields_json, client_ip, user_agent,
  request_id, trace_id, created_at
)
SELECT
  audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
  action, object_type, object_id, object_name,
  CASE WHEN object_type='provider'
    THEN json_remove(before_summary_json, '$.base_url')
    ELSE before_summary_json END,
  CASE WHEN object_type='provider'
    THEN json_remove(after_summary_json, '$.base_url')
    ELSE after_summary_json END,
  changed_fields_json, client_ip, user_agent,
  request_id, trace_id, created_at
FROM audit_logs;

-- 审计复制使用独立临时守卫，防止 CHECK 白名单调整导致静默丢行。
DROP TABLE IF EXISTS prd047_audit_copy_guard;
CREATE TABLE prd047_audit_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  CHECK (source_count=copied_count)
);
-- 把新旧审计行数写入守卫，任一历史动作遗漏都会使完整迁移回滚。
INSERT INTO prd047_audit_copy_guard(source_count, copied_count)
SELECT
  (SELECT COUNT(*) FROM audit_logs),
  (SELECT COUNT(*) FROM audit_logs_prd047_new);
DROP TABLE prd047_audit_copy_guard;

DROP TABLE audit_logs;
-- 只有逐列复制和计数都通过后才用新白名单表替换原审计表。
ALTER TABLE audit_logs_prd047_new RENAME TO audit_logs;

CREATE INDEX idx_audit_logs_tenant_time
  ON audit_logs(tenant_id, created_at DESC, audit_log_id DESC);
CREATE INDEX idx_audit_logs_tenant_actor_time
  ON audit_logs(tenant_id, actor_user_id, created_at DESC);
CREATE INDEX idx_audit_logs_tenant_object_time
  ON audit_logs(tenant_id, object_type, object_id, created_at DESC);
CREATE INDEX idx_audit_logs_tenant_action_time
  ON audit_logs(tenant_id, action, created_at DESC);
CREATE INDEX idx_audit_logs_tenant_request
  ON audit_logs(tenant_id, request_id);
CREATE UNIQUE INDEX idx_audit_logs_dry_run_request_unique
  ON audit_logs(tenant_id, actor_user_id, request_id)
  WHERE action = 'policy.dry_run_executed';

CREATE TRIGGER trg_audit_logs_owner_insert
BEFORE INSERT ON audit_logs
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM users u
  WHERE u.user_id=NEW.actor_user_id AND u.tenant_id=NEW.tenant_id
)
BEGIN
  SELECT RAISE(ABORT, 'audit_log_owner_mismatch');
END;
CREATE TRIGGER trg_audit_logs_immutable_update
BEFORE UPDATE ON audit_logs
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'audit_log_immutable');
END;
CREATE TRIGGER trg_audit_logs_immutable_delete
BEFORE DELETE ON audit_logs
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'audit_log_immutable');
END;

-- 最终把 FK/integrity 结果写入零值守卫；任何悬空复合关系都阻止本次事务提交。
DROP TABLE IF EXISTS prd047_integrity_guard;
CREATE TABLE prd047_integrity_guard (
  foreign_key_issue_count INTEGER NOT NULL CHECK (foreign_key_issue_count=0),
  integrity_issue_count INTEGER NOT NULL CHECK (integrity_issue_count=0)
);
-- 外键与完整性问题数必须同时为零，任何非零结果都通过 CHECK 回滚整次升级。
INSERT INTO prd047_integrity_guard(foreign_key_issue_count, integrity_issue_count)
SELECT
  (SELECT COUNT(*) FROM pragma_foreign_key_check),
  (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd047_integrity_guard;

COMMIT;
PRAGMA foreign_keys = ON;
