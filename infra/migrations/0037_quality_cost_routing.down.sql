-- PRD-048 条件回滚：只有不存在不可降级的质量配置、人工评分和 quality_cost 历史时才恢复 0036。
-- 自动统计可由 attempt/session 重建，因此 guard 通过后允许删除 Score 派生累计。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- 自动统计属于可重建派生事实，但只有源终态和 Score 完全一致时才允许删除后重建。
DROP TABLE IF EXISTS temp.prd048_down_statistics_guard;
CREATE TEMP TABLE prd048_down_statistics_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd048_down_statistics_guard
BEFORE INSERT ON prd048_down_statistics_guard
FOR EACH ROW
WHEN NEW.issue_count!=0
BEGIN
  SELECT RAISE(ABORT, 'prd048_down_automatic_statistics_unrebuildable');
END;
-- 先校验所有将纳入重建的源事实；坏归属、坏延迟或非规范流时间不能被静默跳过。
INSERT INTO prd048_down_statistics_guard(issue_count)
SELECT
  (SELECT COUNT(*)
   FROM routing_attempts ra
   LEFT JOIN models m
     ON m.tenant_id=ra.tenant_id AND m.model_id=ra.model_id
   WHERE (
       (ra.status='succeeded' AND ra.usage_status!='not_applicable')
       OR (
         ra.status='failed'
         AND ra.usage_status='unavailable'
         AND (
           ra.error_category IN ('timeout', 'rate_limited', 'upstream_5xx')
           OR ra.error_code='gateway.upstream_transport_failed'
         )
       )
     )
     AND (
       m.model_id IS NULL
       OR ra.latency_ms IS NULL
       OR typeof(ra.latency_ms)!='integer'
       OR ra.latency_ms<0
       OR ra.completed_at IS NULL
       OR typeof(ra.completed_at)!='text'
       OR strftime('%Y-%m-%dT%H:%M:%SZ', ra.completed_at, '+0 seconds') IS NULL
       OR NOT (
         length(strftime(
           '%Y-%m-%dT%H:%M:%SZ', ra.completed_at, '+0 seconds'
         ))=20
         AND strftime(
           '%Y-%m-%dT%H:%M:%SZ', ra.completed_at, '+0 seconds'
         ) GLOB
           '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
         AND CAST(substr(strftime(
           '%Y-%m-%dT%H:%M:%SZ', ra.completed_at, '+0 seconds'
         ), 1, 4) AS INTEGER) BETWEEN 1 AND 9999
       )
     ))
  +
  (SELECT COUNT(*)
   FROM routing_attempts ra
   WHERE ra.status='failed'
     AND ra.usage_status!='unavailable'
     AND (
       ra.error_category IN ('timeout', 'rate_limited', 'upstream_5xx')
       OR ra.error_code='gateway.upstream_transport_failed'
     ))
  +
  (SELECT COUNT(*)
   FROM stream_sessions ss
   LEFT JOIN models m
     ON m.tenant_id=ss.tenant_id AND m.model_id=ss.model_id
   WHERE (
       ss.status IN ('completed', 'timed_out')
       OR (
         ss.status='upstream_failed'
         AND ss.error_code IN (
           'gateway.upstream_rate_limited',
           'gateway.upstream_timeout',
           'gateway.upstream_stream_timeout',
           'gateway.upstream_stream_failed'
         )
       )
     )
     AND (
       m.model_id IS NULL
       OR ss.started_at IS NULL
       OR ss.completed_at IS NULL
       OR typeof(ss.started_at)!='text'
       OR typeof(ss.completed_at)!='text'
       OR NOT (
         (
           (length(ss.started_at)=20 AND ss.started_at GLOB
             '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
           OR
           (length(ss.started_at)=27 AND ss.started_at GLOB
             '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')
         )
         AND CAST(substr(ss.started_at, 1, 4) AS INTEGER) BETWEEN 1 AND 9999
         AND strftime(
           '%Y-%m-%dT%H:%M:%SZ', substr(ss.started_at, 1, 19) || 'Z', '+0 seconds'
         )=substr(ss.started_at, 1, 19) || 'Z'
       )
       OR NOT (
         (
           (length(ss.completed_at)=20 AND ss.completed_at GLOB
             '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z')
           OR
           (length(ss.completed_at)=27 AND ss.completed_at GLOB
             '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z')
         )
         AND CAST(substr(ss.completed_at, 1, 4) AS INTEGER) BETWEEN 1 AND 9999
         AND strftime(
           '%Y-%m-%dT%H:%M:%SZ', substr(ss.completed_at, 1, 19) || 'Z', '+0 seconds'
         )=substr(ss.completed_at, 1, 19) || 'Z'
       )
       OR (
         (
           CAST(strftime('%s', substr(ss.completed_at, 1, 19) || 'Z') AS INTEGER)
           - CAST(strftime('%s', substr(ss.started_at, 1, 19) || 'Z') AS INTEGER)
         ) * 1000000
         + CASE WHEN length(ss.completed_at)=27
             THEN CAST(substr(ss.completed_at, 21, 6) AS INTEGER) ELSE 0 END
         - CASE WHEN length(ss.started_at)=27
             THEN CAST(substr(ss.started_at, 21, 6) AS INTEGER) ELSE 0 END
       )<0
     ));

-- 临时观察表复用正向迁移的固定分类和毫秒截断，绝不依赖可变的运行时代码猜测历史资格。
DROP TABLE IF EXISTS temp.prd048_down_quality_observations;
CREATE TEMP TABLE prd048_down_quality_observations (
  tenant_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  succeeded INTEGER NOT NULL CHECK (succeeded IN (0, 1)),
  latency_ms INTEGER NOT NULL CHECK (
    typeof(latency_ms)='integer' AND latency_ms>=0
  ),
  observed_at TEXT NOT NULL
);
-- 非流成功排除本地 usage；失败必须同时命中可靠上游分类和 unavailable 语义。
INSERT INTO prd048_down_quality_observations(
  tenant_id, model_id, succeeded, latency_ms, observed_at
)
SELECT
  ra.tenant_id,
  ra.model_id,
  CASE WHEN ra.status='succeeded' THEN 1 ELSE 0 END,
  ra.latency_ms,
  strftime('%Y-%m-%dT%H:%M:%SZ', ra.completed_at, '+0 seconds')
FROM routing_attempts ra
WHERE
  (ra.status='succeeded' AND ra.usage_status!='not_applicable')
  OR (
    ra.status='failed'
    AND ra.usage_status='unavailable'
    AND (
      ra.error_category IN ('timeout', 'rate_limited', 'upstream_5xx')
      OR ra.error_code='gateway.upstream_transport_failed'
    )
  );
-- 流观察只接收完成、超时和无歧义错误码，并以整数微秒差向下截断为毫秒。
INSERT INTO prd048_down_quality_observations(
  tenant_id, model_id, succeeded, latency_ms, observed_at
)
SELECT
  ss.tenant_id,
  ss.model_id,
  CASE WHEN ss.status='completed' THEN 1 ELSE 0 END,
  (
    (
      CAST(strftime('%s', substr(ss.completed_at, 1, 19) || 'Z') AS INTEGER)
      - CAST(strftime('%s', substr(ss.started_at, 1, 19) || 'Z') AS INTEGER)
    ) * 1000000
    + CASE WHEN length(ss.completed_at)=27
        THEN CAST(substr(ss.completed_at, 21, 6) AS INTEGER) ELSE 0 END
    - CASE WHEN length(ss.started_at)=27
        THEN CAST(substr(ss.started_at, 21, 6) AS INTEGER) ELSE 0 END
  ) / 1000,
  strftime('%Y-%m-%dT%H:%M:%SZ', ss.completed_at)
FROM stream_sessions ss
WHERE
  ss.status IN ('completed', 'timed_out')
  OR (
    ss.status='upstream_failed'
    AND ss.error_code IN (
      'gateway.upstream_rate_limited',
      'gateway.upstream_timeout',
      'gateway.upstream_stream_timeout',
      'gateway.upstream_stream_failed'
    )
  );

-- TOTAL 先验证每个模型的累计值仍可放入 SQLite signed 64-bit，随后 SUM 才不会抛非固定错误。
INSERT INTO prd048_down_statistics_guard(issue_count)
SELECT COUNT(*)
FROM (
  SELECT tenant_id, model_id
  FROM prd048_down_quality_observations
  GROUP BY tenant_id, model_id
  HAVING TOTAL(latency_ms)>9223372036854775807
     OR COUNT(*)>9223372036854775807
);

-- Manifest 为每个 Model 生成一行确定性期望值，随后与纯自动 Score 逐字段核对。
DROP TABLE IF EXISTS temp.prd048_down_quality_manifest;
CREATE TEMP TABLE prd048_down_quality_manifest AS
SELECT
  m.tenant_id,
  m.model_id,
  'active' AS status,
  COUNT(o.model_id) AS sample_count,
  COALESCE(SUM(o.succeeded), 0) AS success_count,
  COUNT(o.model_id)-COALESCE(SUM(o.succeeded), 0) AS error_count,
  COUNT(o.model_id) AS latency_sample_count,
  COALESCE(SUM(o.latency_ms), 0) AS total_latency_ms,
  COUNT(o.model_id) AS statistics_version,
  MIN(o.observed_at) AS first_observed_at,
  MAX(o.observed_at) AS last_observed_at,
  m.created_at AS created_at,
  MAX(m.created_at, COALESCE(MAX(o.observed_at), m.created_at)) AS updated_at
FROM models m
LEFT JOIN prd048_down_quality_observations o
  ON o.tenant_id=m.tenant_id AND o.model_id=m.model_id
GROUP BY m.tenant_id, m.model_id;

-- 纯自动 Score 的所有可重建字段必须逐项相等；缺行、多行或无源增量均禁止降级。
INSERT INTO prd048_down_statistics_guard(issue_count)
SELECT
  (SELECT COUNT(*)
   FROM prd048_down_quality_manifest expected
   LEFT JOIN model_quality_scores actual
     ON actual.tenant_id=expected.tenant_id AND actual.model_id=expected.model_id
   WHERE actual.model_quality_score_id IS NULL)
  +
  (SELECT COUNT(*)
   FROM model_quality_scores actual
   LEFT JOIN prd048_down_quality_manifest expected
     ON expected.tenant_id=actual.tenant_id AND expected.model_id=actual.model_id
   WHERE expected.model_id IS NULL)
  +
  (SELECT COUNT(*)
   FROM prd048_down_quality_manifest expected
   JOIN model_quality_scores actual
     ON actual.tenant_id=expected.tenant_id AND actual.model_id=expected.model_id
   WHERE actual.manual_quality_bps IS NULL
     AND actual.feedback_version=1
     AND actual.feedback_updated_by IS NULL
     AND actual.feedback_updated_at IS NULL
     AND (
       actual.status IS NOT expected.status
       OR actual.sample_count IS NOT expected.sample_count
       OR actual.success_count IS NOT expected.success_count
       OR actual.error_count IS NOT expected.error_count
       OR actual.latency_sample_count IS NOT expected.latency_sample_count
       OR actual.total_latency_ms IS NOT expected.total_latency_ms
       OR actual.statistics_version IS NOT expected.statistics_version
       OR actual.first_observed_at IS NOT expected.first_observed_at
       OR actual.last_observed_at IS NOT expected.last_observed_at
       OR actual.created_at IS NOT expected.created_at
       OR actual.updated_at IS NOT expected.updated_at
     ));
DROP TRIGGER trg_prd048_down_statistics_guard;
DROP TABLE prd048_down_statistics_guard;
DROP TABLE prd048_down_quality_manifest;
DROP TABLE prd048_down_quality_observations;

-- guard 必须是第一项业务判断；固定 RAISE 文本便于运维区分“有业务事实”与 SQL 故障。
DROP TABLE IF EXISTS temp.prd048_down_guard;
CREATE TEMP TABLE prd048_down_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd048_down_guard
BEFORE INSERT ON prd048_down_guard
FOR EACH ROW
WHEN NEW.issue_count!=0
BEGIN
  SELECT RAISE(ABORT, 'prd048_down_business_facts_present');
END;
-- 在任何正式表变更前汇总不可降级事实，非零时以固定错误原子拒绝回滚。
INSERT INTO prd048_down_guard(issue_count)
SELECT
  (SELECT COUNT(*) FROM model_alias_routing_policies WHERE routing_mode='quality_cost')
  + (SELECT COUNT(*)
     FROM model_alias_routing_policies
     WHERE quality_weight_bps!=5000
        OR cost_weight_bps!=3000
        OR latency_weight_bps!=2000
        OR quality_floor_bps!=6000
        OR minimum_sample_count!=20)
  + (SELECT COUNT(*) FROM routing_decisions WHERE routing_mode='quality_cost')
  + (SELECT COUNT(*)
     FROM routing_candidates c
     JOIN routing_decisions d
       ON d.tenant_id=c.tenant_id AND d.routing_decision_id=c.routing_decision_id
     WHERE d.routing_mode='quality_cost')
  + (SELECT COUNT(*)
     FROM routing_policy_events e
     WHERE e.payload_schema_version=2
       AND (
         json_extract(e.payload_json, '$.routing_mode')='quality_cost'
         OR EXISTS (
           SELECT 1 FROM json_each(e.payload_json, '$.change_kinds')
           WHERE value='quality_cost'
         )
         -- created 只有“旧模式 + 五项默认休眠配置”可降级；历史非默认配置不能被当前行覆盖。
         OR (
           EXISTS (
             SELECT 1 FROM json_each(e.payload_json, '$.change_kinds')
             WHERE value='created'
           )
           AND (
             COALESCE(json_extract(e.payload_json, '$.routing_mode'), '')
               NOT IN ('fixed', 'cost_first', 'availability_first')
             OR json_extract(
               e.payload_json, '$.quality_cost_config.quality_weight_bps'
             ) IS NOT 5000
             OR json_extract(
               e.payload_json, '$.quality_cost_config.cost_weight_bps'
             ) IS NOT 3000
             OR json_extract(
               e.payload_json, '$.quality_cost_config.latency_weight_bps'
             ) IS NOT 2000
             OR json_extract(
               e.payload_json, '$.quality_cost_config.quality_floor_bps'
             ) IS NOT 6000
             OR json_extract(
               e.payload_json, '$.quality_cost_config.minimum_sample_count'
             ) IS NOT 20
           )
         )
         OR (
           EXISTS (
             SELECT 1 FROM json_each(e.payload_json, '$.changed_routing_fields')
             WHERE value='quality_cost_config'
           )
           AND NOT EXISTS (
             SELECT 1 FROM json_each(e.payload_json, '$.change_kinds')
             WHERE value='created'
           )
         )
       ))
  + (SELECT COUNT(*)
     FROM model_quality_scores
     WHERE manual_quality_bps IS NOT NULL
        OR feedback_version!=1
        OR feedback_updated_by IS NOT NULL
        OR feedback_updated_at IS NOT NULL)
  + (SELECT COUNT(*) FROM model_quality_score_events)
  + (SELECT COUNT(*) FROM audit_logs WHERE action='model_quality_score.updated')
  + (SELECT COUNT(*)
     FROM webhook_endpoints endpoint
     WHERE EXISTS (
       SELECT 1 FROM json_each(endpoint.event_types_json)
       WHERE value='model.feedback_recorded'
     ))
  + (SELECT COUNT(*) FROM webhook_deliveries WHERE event_type='model.feedback_recorded')
  + (SELECT COUNT(*)
     FROM webhook_delivery_attempts attempt
     JOIN webhook_deliveries delivery
       ON delivery.tenant_id=attempt.tenant_id
      AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
     WHERE delivery.event_type='model.feedback_recorded');
DROP TRIGGER trg_prd048_down_guard;
DROP TABLE prd048_down_guard;

-- 解除所有 0037 跨表/不可变 trigger，避免父表替换时 SQLite 解析到短暂缺失的对象。
DROP TRIGGER IF EXISTS trg_routing_decisions_prd048_candidate_bound_update;
DROP TRIGGER IF EXISTS trg_routing_candidates_prd048_immutable_delete;
DROP TRIGGER IF EXISTS trg_routing_candidates_prd048_immutable_update;
DROP TRIGGER IF EXISTS trg_routing_candidates_prd048_graph_insert;
DROP TRIGGER IF EXISTS trg_routing_policy_events_prd048_immutable_delete;
DROP TRIGGER IF EXISTS trg_routing_policy_events_prd048_immutable_update;
DROP TRIGGER IF EXISTS trg_routing_policy_events_prd048_graph_insert;
DROP TRIGGER IF EXISTS trg_routing_policy_events_prd048_payload_insert;
DROP TRIGGER IF EXISTS trg_routing_policy_events_prd048_schema_insert;
DROP TRIGGER IF EXISTS trg_routing_decisions_prd048_owner_update;
DROP TRIGGER IF EXISTS trg_routing_decisions_prd048_owner_insert;
DROP TRIGGER IF EXISTS trg_routing_policies_prd048_delete;
DROP TRIGGER IF EXISTS trg_routing_policies_prd048_version_update;
DROP TRIGGER IF EXISTS trg_routing_policies_prd048_identity_update;
DROP TRIGGER IF EXISTS trg_model_aliases_prd047_policy_update;
DROP TRIGGER IF EXISTS trg_model_aliases_prd047_policy_insert;
DROP TRIGGER IF EXISTS trg_audit_logs_immutable_delete;
DROP TRIGGER IF EXISTS trg_audit_logs_immutable_update;
DROP TRIGGER IF EXISTS trg_audit_logs_owner_insert;

-- 先恢复 0036 RoutingPolicy 父表；所有存量 mode 已被 guard 证明属于旧三模式。
DROP TABLE IF EXISTS model_alias_routing_policies_prd047_old;
CREATE TABLE model_alias_routing_policies_prd047_old (
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
-- 旧三模式 Policy 逐列复制，0037 的休眠配置不进入 0036 正式结构。
INSERT INTO model_alias_routing_policies_prd047_old(
  routing_policy_id, tenant_id, model_alias_id, routing_mode, status, version,
  created_by, updated_by, created_at, updated_at
)
SELECT
  routing_policy_id, tenant_id, model_alias_id, routing_mode, status, version,
  created_by, updated_by, created_at, updated_at
FROM model_alias_routing_policies;

-- 删除现表前先建立 Policy 行数守卫，避免回滚因意外过滤静默丢失租户配置。
DROP TABLE IF EXISTS prd048_policy_down_guard;
CREATE TABLE prd048_policy_down_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  CHECK (source_count=copied_count)
);
-- 源表与 0036 目标表必须逐行等量，非零差异通过 CHECK 终止事务。
INSERT INTO prd048_policy_down_guard(source_count, copied_count)
SELECT
  (SELECT COUNT(*) FROM model_alias_routing_policies),
  (SELECT COUNT(*) FROM model_alias_routing_policies_prd047_old);
DROP TABLE prd048_policy_down_guard;
DROP TABLE model_alias_routing_policies;
-- Policy 复制守卫通过后才恢复 0036 正式表名，后续 trigger 始终绑定完整父表。
ALTER TABLE model_alias_routing_policies_prd047_old RENAME TO model_alias_routing_policies;

CREATE INDEX idx_routing_policies_tenant_mode
  ON model_alias_routing_policies(tenant_id, routing_mode, updated_at DESC);

CREATE TRIGGER trg_model_aliases_prd047_policy_insert
AFTER INSERT ON model_aliases
FOR EACH ROW
BEGIN
  -- 回滚后的新 Alias 仍按 0036 合同创建 fixed Policy，不能携带已删除的质量配置。
  INSERT INTO model_alias_routing_policies(
    routing_policy_id, tenant_id, model_alias_id, routing_mode, status, version,
    created_by, updated_by, created_at, updated_at
  )
  VALUES(
    'rpol_' || lower(hex(randomblob(16))), NEW.tenant_id, NEW.model_alias_id,
    'fixed', 'enabled', 1, NEW.created_by, NEW.created_by, NEW.created_at, NEW.updated_at
  );
END;
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
  -- 0036 只因 Alias 图真实变化推进同租户 Policy 版本，继续保留 CAS 可见性。
  UPDATE model_alias_routing_policies
  SET version=version+1, updated_at=NEW.updated_at
  WHERE tenant_id=NEW.tenant_id AND model_alias_id=NEW.model_alias_id;
END;
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
  NEW.version<OLD.version
  OR NEW.version>OLD.version+1
  OR ((NEW.routing_mode IS NOT OLD.routing_mode OR NEW.status IS NOT OLD.status)
      AND NEW.version!=OLD.version+1)
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

-- Decision 恢复 0036 冻结字段；guard 已保证不会裁剪任何 quality_cost 请求事实。
DROP TABLE IF EXISTS routing_decisions_prd047_old;
CREATE TABLE routing_decisions_prd047_old (
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
    OR (typeof(token_estimate_truncated)='integer'
        AND token_estimate_truncated IN (0, 1))
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
    (routing_policy_id IS NOT NULL AND routing_policy_version IS NOT NULL
      AND routing_mode IS NOT NULL)
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
-- guard 已证明不存在 quality_cost 历史后，Decision 只复制 0036 可表达字段且逐值保真。
INSERT INTO routing_decisions_prd047_old(
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
  routing_policy_id, routing_policy_version, routing_mode, price_as_of,
  estimated_input_tokens, estimated_output_tokens, token_estimate_truncated,
  routing_policy_event_id
FROM routing_decisions;

-- Decision 降级换表前建立行数守卫，避免删除质量列时连带丢失旧模式事实。
DROP TABLE IF EXISTS prd048_decision_down_guard;
CREATE TABLE prd048_decision_down_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  CHECK (source_count=copied_count)
);
-- 源表和 0036 目标表行数必须完全一致，否则当前回滚事务失败关闭。
INSERT INTO prd048_decision_down_guard(source_count, copied_count)
SELECT (SELECT COUNT(*) FROM routing_decisions),
       (SELECT COUNT(*) FROM routing_decisions_prd047_old);
DROP TABLE prd048_decision_down_guard;
DROP TABLE routing_decisions;
-- Decision 行数守卫通过后才恢复旧表名，确保 Candidate 外键不会指向半成品。
ALTER TABLE routing_decisions_prd047_old RENAME TO routing_decisions;

CREATE INDEX idx_routing_decisions_request_id
  ON routing_decisions(tenant_id, request_id);
CREATE UNIQUE INDEX idx_routing_decisions_policy_request_unique
  ON routing_decisions(tenant_id, request_id)
  WHERE routing_policy_id IS NOT NULL;
CREATE INDEX idx_routing_decisions_alias_time
  ON routing_decisions(tenant_id, model_alias_id, created_at);
CREATE INDEX idx_routing_decisions_error_code
  ON routing_decisions(tenant_id, error_code);
CREATE INDEX idx_routing_decisions_environment_time
  ON routing_decisions(tenant_id, environment_id, created_at DESC);

-- 恢复 0036 Decision owner 图，继续兼容同租户、同代码的 legacy NULL-environment Key。
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
        OR
        (vk.environment_id IS NULL AND vk.environment=NEW.environment
          AND EXISTS (
            SELECT 1 FROM environments ve
            WHERE ve.tenant_id=NEW.tenant_id
              AND ve.environment_id=NEW.environment_id AND ve.code=NEW.environment
          ))
      )
  ))
  OR (NEW.model_alias_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM model_aliases a
    WHERE a.tenant_id=NEW.tenant_id AND a.model_alias_id=NEW.model_alias_id
      AND a.environment_id IS NEW.environment_id
  ))
  OR ((NEW.selected_model_id IS NULL)!=(NEW.provider_id IS NULL))
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
        OR
        (vk.environment_id IS NULL AND vk.environment=NEW.environment
          AND EXISTS (
            SELECT 1 FROM environments ve
            WHERE ve.tenant_id=NEW.tenant_id
              AND ve.environment_id=NEW.environment_id AND ve.code=NEW.environment
          ))
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
  OR ((NEW.selected_model_id IS NULL)!=(NEW.provider_id IS NULL))
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

-- Candidate 恢复 0036 列、原因白名单和价格组约束；质量列只在 guard 允许后裁剪。
DROP TABLE IF EXISTS routing_candidates_prd047_old;
CREATE TABLE routing_candidates_prd047_old (
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
-- Candidate 仅复制 0036 价格与资格事实；质量快照已由全局 guard 证明可安全移除。
INSERT INTO routing_candidates_prd047_old(
  routing_candidate_id, tenant_id, routing_decision_id, routing_policy_id,
  model_alias_id, model_id, provider_id, candidate_role, configured_priority,
  rank, status, reason_code, health_state, health_fresh, keypool_state,
  price_available, price_source, price_schema_version, currency,
  estimated_cost_amount, created_at, updated_at
)
SELECT
  routing_candidate_id, tenant_id, routing_decision_id, routing_policy_id,
  model_alias_id, model_id, provider_id, candidate_role, configured_priority,
  rank, status, reason_code, health_state, health_fresh, keypool_state,
  price_available, price_source, price_schema_version, currency,
  estimated_cost_amount, created_at, updated_at
FROM routing_candidates;

-- Candidate 正式换表前建立行数守卫，防止任何父子解释事实静默漏行。
DROP TABLE IF EXISTS prd048_candidate_down_guard;
CREATE TABLE prd048_candidate_down_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  CHECK (source_count=copied_count)
);
-- 源候选与 0036 目标候选必须等量，差异由 CHECK 原子阻断回滚。
INSERT INTO prd048_candidate_down_guard(source_count, copied_count)
SELECT (SELECT COUNT(*) FROM routing_candidates),
       (SELECT COUNT(*) FROM routing_candidates_prd047_old);
DROP TABLE prd048_candidate_down_guard;
DROP TABLE routing_candidates;
-- 候选复制守卫通过后才恢复旧表名，并在完整数据上重建 rank 唯一约束。
ALTER TABLE routing_candidates_prd047_old RENAME TO routing_candidates;

CREATE UNIQUE INDEX idx_routing_candidates_decision_rank_unique
  ON routing_candidates(tenant_id, routing_decision_id, rank)
  WHERE rank IS NOT NULL;
CREATE INDEX idx_routing_candidates_decision_order
  ON routing_candidates(tenant_id, routing_decision_id, rank, configured_priority);

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
        ON rp.tenant_id=d.tenant_id AND rp.routing_policy_id=d.routing_policy_id
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
        AND d.selected_model_id=NEW.model_id AND d.provider_id=NEW.provider_id
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

-- v1 历史原文直接复制；无质量变化的 v2 事件删除休眠配置字段后恢复为 0036 payload。
DROP TABLE IF EXISTS routing_policy_events_prd047_old;
CREATE TABLE routing_policy_events_prd047_old (
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
    json_valid(payload_json) AND json_type(payload_json)='object'
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

-- v1 事件原文逐字节保留；可降级 v2 仅移除质量配置字段并保持数组原有顺序。
INSERT INTO routing_policy_events_prd047_old(
  routing_policy_event_id, tenant_id, routing_policy_id, model_alias_id,
  routing_policy_version, event_type, trigger_type, actor_user_id,
  request_id, trace_id, request_fingerprint, payload_json, created_at
)
SELECT
  routing_policy_event_id, tenant_id, routing_policy_id, model_alias_id,
  routing_policy_version, event_type, trigger_type, actor_user_id,
  request_id, trace_id, request_fingerprint,
  CASE
    WHEN payload_schema_version=1 THEN payload_json
    ELSE json_set(
      json_remove(payload_json, '$.configuration_schema_version', '$.quality_cost_config'),
      '$.changed_routing_fields',
      json(COALESCE(
        (
          SELECT json_group_array(value)
          FROM (
            SELECT item.value AS value
            FROM json_each(routing_policy_events.payload_json, '$.changed_routing_fields') item
            WHERE item.value!='quality_cost_config'
            ORDER BY CAST(item.key AS INTEGER)
          )
        ),
        '[]'
      ))
    )
  END,
  created_at
FROM routing_policy_events;

-- Event 换表前建立行数守卫，JSON 降级不得造成事件缺失或重复。
DROP TABLE IF EXISTS prd048_policy_event_down_guard;
CREATE TABLE prd048_policy_event_down_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  CHECK (source_count=copied_count)
);
-- 新旧事件表必须等量，任何转换漏行都通过 CHECK 回滚整个 down 事务。
INSERT INTO prd048_policy_event_down_guard(source_count, copied_count)
SELECT (SELECT COUNT(*) FROM routing_policy_events),
       (SELECT COUNT(*) FROM routing_policy_events_prd047_old);
DROP TABLE prd048_policy_event_down_guard;
DROP TABLE routing_policy_events;
-- 事件复制守卫通过后才恢复 0036 表名，并重新启用旧版 payload trigger。
ALTER TABLE routing_policy_events_prd047_old RENAME TO routing_policy_events;

CREATE INDEX idx_routing_policy_events_tenant_time
  ON routing_policy_events(tenant_id, created_at DESC, routing_policy_event_id DESC);
CREATE INDEX idx_routing_policy_events_policy_version
  ON routing_policy_events(tenant_id, routing_policy_id, routing_policy_version DESC);

-- 恢复 0036 数组白名单、固定顺序、创建/更新分类和模式跳转约束。
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
    SELECT 1 FROM json_each(NEW.payload_json, '$.change_kinds') item
    WHERE item.type!='text'
      OR item.value NOT IN ('created', 'mode', 'candidates', 'alias_state')
  ) THEN 1
  WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.payload_json, '$.changed_routing_fields') item
    WHERE item.type!='text'
      OR item.value NOT IN (
        'alias', 'environment', 'environment_id', 'status',
        'default_model_id', 'fallback_model_id', 'routing_mode'
      )
  ) THEN 1
  WHEN EXISTS (
    SELECT 1
    FROM json_each(NEW.payload_json, '$.change_kinds') earlier
    JOIN json_each(NEW.payload_json, '$.change_kinds') later
      ON CAST(earlier.key AS INTEGER)<CAST(later.key AS INTEGER)
    WHERE CASE earlier.value
      WHEN 'created' THEN 0 WHEN 'mode' THEN 1
      WHEN 'candidates' THEN 2 WHEN 'alias_state' THEN 3 ELSE 99 END
      >= CASE later.value
      WHEN 'created' THEN 0 WHEN 'mode' THEN 1
      WHEN 'candidates' THEN 2 WHEN 'alias_state' THEN 3 ELSE 99 END
  ) THEN 1
  WHEN EXISTS (
    SELECT 1
    FROM json_each(NEW.payload_json, '$.changed_routing_fields') earlier
    JOIN json_each(NEW.payload_json, '$.changed_routing_fields') later
      ON CAST(earlier.key AS INTEGER)<CAST(later.key AS INTEGER)
    WHERE CASE earlier.value
      WHEN 'alias' THEN 0 WHEN 'environment' THEN 1 WHEN 'environment_id' THEN 2
      WHEN 'status' THEN 3 WHEN 'default_model_id' THEN 4
      WHEN 'fallback_model_id' THEN 5 WHEN 'routing_mode' THEN 6 ELSE 99 END
      >= CASE later.value
      WHEN 'alias' THEN 0 WHEN 'environment' THEN 1 WHEN 'environment_id' THEN 2
      WHEN 'status' THEN 3 WHEN 'default_model_id' THEN 4
      WHEN 'fallback_model_id' THEN 5 WHEN 'routing_mode' THEN 6 ELSE 99 END
  ) THEN 1
  WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.payload_json, '$.change_kinds') WHERE value='created'
  ) THEN CASE
    WHEN NEW.routing_policy_version!=1 THEN 1
    WHEN json_type(NEW.payload_json, '$.previous_mode') IS NOT 'null' THEN 1
    WHEN json_array_length(NEW.payload_json, '$.change_kinds')!=1 THEN 1
    WHEN json_array_length(NEW.payload_json, '$.changed_routing_fields')!=7 THEN 1
    ELSE 0 END
  WHEN json_type(NEW.payload_json, '$.previous_mode') IS 'null' THEN 1
  WHEN (
    EXISTS (SELECT 1 FROM json_each(NEW.payload_json, '$.change_kinds') WHERE value='mode')
    !=
    EXISTS (SELECT 1 FROM json_each(NEW.payload_json, '$.changed_routing_fields')
            WHERE value='routing_mode')
  ) THEN 1
  WHEN (
    EXISTS (SELECT 1 FROM json_each(NEW.payload_json, '$.change_kinds')
            WHERE value='candidates')
    !=
    EXISTS (SELECT 1 FROM json_each(NEW.payload_json, '$.changed_routing_fields')
            WHERE value IN ('default_model_id', 'fallback_model_id'))
  ) THEN 1
  WHEN (
    EXISTS (SELECT 1 FROM json_each(NEW.payload_json, '$.change_kinds')
            WHERE value='alias_state')
    !=
    EXISTS (SELECT 1 FROM json_each(NEW.payload_json, '$.changed_routing_fields')
            WHERE value IN ('alias', 'environment', 'environment_id', 'status'))
  ) THEN 1
  WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.payload_json, '$.change_kinds') WHERE value='mode'
  ) AND json_extract(NEW.payload_json, '$.previous_mode')
        IS json_extract(NEW.payload_json, '$.routing_mode') THEN 1
  WHEN NOT EXISTS (
    SELECT 1 FROM json_each(NEW.payload_json, '$.change_kinds') WHERE value='mode'
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

-- Audit 恢复 0036 白名单；guard 已证明不存在会被裁剪的人工评分审计。
DROP TABLE IF EXISTS audit_logs_prd047_old;
CREATE TABLE audit_logs_prd047_old (
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
-- 全局 guard 已排除质量审计后，其余 Audit 按 0036 列集合逐值保真复制。
INSERT INTO audit_logs_prd047_old(
  audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
  action, object_type, object_id, object_name, before_summary_json,
  after_summary_json, changed_fields_json, client_ip, user_agent,
  request_id, trace_id, created_at
)
SELECT
  audit_log_id, tenant_id, actor_user_id, actor_email, actor_role,
  action, object_type, object_id, object_name, before_summary_json,
  after_summary_json, changed_fields_json, client_ip, user_agent,
  request_id, trace_id, created_at
FROM audit_logs;

-- 审计降级换表前建立行数守卫，不可变事实不能因枚举收窄被静默过滤。
DROP TABLE IF EXISTS prd048_audit_down_guard;
CREATE TABLE prd048_audit_down_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  CHECK (source_count=copied_count)
);
-- 源审计和 0036 目标审计必须等量，差异通过 CHECK 中止回滚。
INSERT INTO prd048_audit_down_guard(source_count, copied_count)
SELECT (SELECT COUNT(*) FROM audit_logs), (SELECT COUNT(*) FROM audit_logs_prd047_old);
DROP TABLE prd048_audit_down_guard;
DROP TABLE audit_logs;
-- 审计复制守卫通过后才恢复正式表名，并在完整历史上重建不可变 trigger。
ALTER TABLE audit_logs_prd047_old RENAME TO audit_logs;

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

-- 人工事件已由 guard 证明为空；先删子表与 trigger，再删除可重建的 Score 派生统计。
DROP TRIGGER IF EXISTS trg_model_quality_score_events_prd048_immutable_delete;
DROP TRIGGER IF EXISTS trg_model_quality_score_events_prd048_immutable_update;
DROP TRIGGER IF EXISTS trg_model_quality_score_events_prd048_graph_insert;
DROP TABLE model_quality_score_events;

DROP TRIGGER IF EXISTS trg_model_quality_scores_prd048_delete;
DROP TRIGGER IF EXISTS trg_model_quality_scores_prd048_owner_update;
DROP TRIGGER IF EXISTS trg_model_quality_scores_prd048_transition_update;
DROP TRIGGER IF EXISTS trg_model_quality_scores_prd048_identity_update;
DROP TRIGGER IF EXISTS trg_models_prd048_quality_score_insert;
DROP TABLE model_quality_scores;

-- 恢复后的 0036 不得留下任何 PRD-048 表、列、索引或 trigger，且完整性必须为零/ok。
DROP TABLE IF EXISTS prd048_down_integrity_guard;
CREATE TABLE prd048_down_integrity_guard (
  foreign_key_issue_count INTEGER NOT NULL CHECK (foreign_key_issue_count=0),
  integrity_issue_count INTEGER NOT NULL CHECK (integrity_issue_count=0)
);
-- 最终双零守卫证明恢复后的 0036 外键图和 SQLite 结构均完整，否则回滚 down 本身。
INSERT INTO prd048_down_integrity_guard(foreign_key_issue_count, integrity_issue_count)
SELECT
  (SELECT COUNT(*) FROM pragma_foreign_key_check),
  (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd048_down_integrity_guard;

COMMIT;
PRAGMA foreign_keys = ON;
