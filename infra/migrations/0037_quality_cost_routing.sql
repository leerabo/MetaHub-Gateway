-- PRD-048 效果-成本路由基础：质量统计、人工评分和 quality_cost 可解释路由事实。
-- 安全边界：迁移只消费固定状态、整数延迟和租户内 ID，不复制 Prompt、Response、URL 或凭据。
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- 历史回填必须先证明每个可计数 attempt/session 均能归属同租户 Model，且时间和延迟可解释。
DROP TABLE IF EXISTS prd048_quality_preflight_guard;
CREATE TABLE prd048_quality_preflight_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count=0)
);
-- 在创建任何新业务表前汇总历史归属、时间和延迟坏事实，非零即由 CHECK 原子失败关闭。
INSERT INTO prd048_quality_preflight_guard(issue_count)
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
       -- Attempt 可兼容合法 offset 输入，但规范化结果必须是 Control API 可解析的 20 位 UTC Z。
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
       -- 只有 Python 运行时可无损解析的两种 UTC Z 文本才能参与整数微秒重建。
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
       -- 与正式回填使用同一个纯整数表达式，避免预检和插入对边界微秒得出不同结论。
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
DROP TABLE prd048_quality_preflight_guard;

-- 使用临时普通表统一两类历史来源；迁移结束前显式删除，不能把回填中间事实留在运行库。
DROP TABLE IF EXISTS prd048_quality_observations;
CREATE TABLE prd048_quality_observations (
  tenant_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  succeeded INTEGER NOT NULL CHECK (succeeded IN (0, 1)),
  latency_ms INTEGER NOT NULL CHECK (
    typeof(latency_ms)='integer' AND latency_ms>=0
  ),
  observed_at TEXT NOT NULL
);

-- 非流成功按 usage 排除本地路径；失败必须同时命中可靠上游分类和 unavailable 语义。
INSERT INTO prd048_quality_observations(
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

-- 流式完整会话耗时使用历史 UTC 时间计算；客户端断开和本地会话审计失败不污染模型质量。
INSERT INTO prd048_quality_observations(
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

-- SQLite 的 SUM(integer) 溢出会使回填口径不可复现；先用 TOTAL 进行显式 64-bit 上界预检。
DROP TABLE IF EXISTS prd048_quality_sum_guard;
CREATE TABLE prd048_quality_sum_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count=0)
);
-- 将所有分组的累计值溢出风险压缩为单一守卫行，避免部分 Model 已回填后才失败。
INSERT INTO prd048_quality_sum_guard(issue_count)
SELECT COUNT(*)
FROM (
  SELECT tenant_id, model_id
  FROM prd048_quality_observations
  GROUP BY tenant_id, model_id
  HAVING TOTAL(latency_ms)>9223372036854775807
     OR COUNT(*)>9223372036854775807
);
DROP TABLE prd048_quality_sum_guard;

-- ModelQualityScore 保存整数累计和人工先验；成功率、错误率与平均延迟由服务层确定性派生。
CREATE TABLE model_quality_scores (
  model_quality_score_id TEXT PRIMARY KEY CHECK (
    length(model_quality_score_id)=36
    AND substr(model_quality_score_id, 1, 4)='mqs_'
    AND substr(model_quality_score_id, 5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status='active'),
  sample_count INTEGER NOT NULL CHECK (
    typeof(sample_count)='integer' AND sample_count>=0
  ),
  success_count INTEGER NOT NULL CHECK (
    typeof(success_count)='integer' AND success_count>=0
  ),
  error_count INTEGER NOT NULL CHECK (
    typeof(error_count)='integer' AND error_count>=0
  ),
  latency_sample_count INTEGER NOT NULL CHECK (
    typeof(latency_sample_count)='integer'
    AND latency_sample_count>=0 AND latency_sample_count<=sample_count
  ),
  total_latency_ms INTEGER NOT NULL CHECK (
    typeof(total_latency_ms)='integer' AND total_latency_ms>=0
  ),
  manual_quality_bps INTEGER CHECK (
    manual_quality_bps IS NULL
    OR (typeof(manual_quality_bps)='integer' AND manual_quality_bps BETWEEN 0 AND 10000)
  ),
  statistics_version INTEGER NOT NULL CHECK (
    typeof(statistics_version)='integer' AND statistics_version>=0
  ),
  feedback_version INTEGER NOT NULL CHECK (
    typeof(feedback_version)='integer' AND feedback_version>=1
  ),
  first_observed_at TEXT,
  last_observed_at TEXT,
  feedback_updated_by TEXT,
  feedback_updated_at TEXT,
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  updated_at TEXT NOT NULL CHECK (length(updated_at) BETWEEN 1 AND 64),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, model_id) REFERENCES models(tenant_id, model_id),
  FOREIGN KEY (tenant_id, feedback_updated_by) REFERENCES users(tenant_id, user_id),
  UNIQUE (tenant_id, model_id),
  UNIQUE (tenant_id, model_quality_score_id),
  CHECK (sample_count=success_count+error_count),
  CHECK (statistics_version=sample_count),
  CHECK (
    (sample_count=0 AND latency_sample_count=0 AND total_latency_ms=0
      AND first_observed_at IS NULL AND last_observed_at IS NULL)
    OR
    (sample_count>0 AND first_observed_at IS NOT NULL AND last_observed_at IS NOT NULL)
  ),
  CHECK (
    (feedback_updated_by IS NULL AND feedback_updated_at IS NULL)
    OR (feedback_updated_by IS NOT NULL AND feedback_updated_at IS NOT NULL)
  )
);

-- 为每个存量 Model 精确回填一行；随机 ID 不参与统计口径，所有累计值来自上方规范观察表。
INSERT INTO model_quality_scores(
  model_quality_score_id, tenant_id, model_id, status,
  sample_count, success_count, error_count, latency_sample_count,
  total_latency_ms, manual_quality_bps, statistics_version, feedback_version,
  first_observed_at, last_observed_at, feedback_updated_by, feedback_updated_at,
  created_at, updated_at
)
SELECT
  'mqs_' || lower(hex(randomblob(16))),
  m.tenant_id,
  m.model_id,
  'active',
  COUNT(o.model_id),
  COALESCE(SUM(o.succeeded), 0),
  COUNT(o.model_id)-COALESCE(SUM(o.succeeded), 0),
  COUNT(o.model_id),
  COALESCE(SUM(o.latency_ms), 0),
  NULL,
  COUNT(o.model_id),
  1,
  MIN(o.observed_at),
  MAX(o.observed_at),
  NULL,
  NULL,
  m.created_at,
  MAX(m.created_at, COALESCE(MAX(o.observed_at), m.created_at))
FROM models m
LEFT JOIN prd048_quality_observations o
  ON o.tenant_id=m.tenant_id AND o.model_id=m.model_id
GROUP BY m.tenant_id, m.model_id;

-- 行数、样本数和延迟和必须同时复现 Model 与观察 manifest，避免 LEFT JOIN 静默漏行。
DROP TABLE IF EXISTS prd048_quality_backfill_guard;
CREATE TABLE prd048_quality_backfill_guard (
  model_count INTEGER NOT NULL,
  score_count INTEGER NOT NULL,
  observation_count INTEGER NOT NULL,
  sample_count INTEGER NOT NULL,
  observation_latency INTEGER NOT NULL,
  score_latency INTEGER NOT NULL,
  CHECK (
    model_count=score_count
    AND observation_count=sample_count
    AND observation_latency=score_latency
  )
);
-- 同时核对模型行数、样本数和延迟总和，任何维度漂移都必须回滚整个迁移事务。
INSERT INTO prd048_quality_backfill_guard(
  model_count, score_count, observation_count, sample_count,
  observation_latency, score_latency
)
SELECT
  (SELECT COUNT(*) FROM models),
  (SELECT COUNT(*) FROM model_quality_scores),
  (SELECT COUNT(*) FROM prd048_quality_observations),
  (SELECT COALESCE(SUM(sample_count), 0) FROM model_quality_scores),
  (SELECT COALESCE(SUM(latency_ms), 0) FROM prd048_quality_observations),
  (SELECT COALESCE(SUM(total_latency_ms), 0) FROM model_quality_scores);
DROP TABLE prd048_quality_backfill_guard;
DROP TABLE prd048_quality_observations;

CREATE INDEX idx_model_quality_scores_tenant_updated
  ON model_quality_scores(tenant_id, updated_at DESC, model_quality_score_id DESC);
CREATE INDEX idx_model_quality_scores_tenant_model
  ON model_quality_scores(tenant_id, model_id);

-- 新 Model 与默认 Score 在同一业务事务生成，防止运行时遇到“存在模型但没有冷启动行”的半图。
CREATE TRIGGER trg_models_prd048_quality_score_insert
AFTER INSERT ON models
FOR EACH ROW
BEGIN
  -- 默认 Score 与 Model 同事务生成，随机 ID 只承担内部身份且不携带任何模型正文。
  INSERT INTO model_quality_scores(
    model_quality_score_id, tenant_id, model_id, status,
    sample_count, success_count, error_count, latency_sample_count,
    total_latency_ms, statistics_version, feedback_version,
    created_at, updated_at
  )
  VALUES(
    'mqs_' || lower(hex(randomblob(16))), NEW.tenant_id, NEW.model_id, 'active',
    0, 0, 0, 0, 0, 0, 1, NEW.created_at, NEW.created_at
  );
END;

-- Score 身份和创建时间不可换绑；运行时统计与人工评分只能修改各自的字段集合。
CREATE TRIGGER trg_model_quality_scores_prd048_identity_update
BEFORE UPDATE OF model_quality_score_id, tenant_id, model_id, status, created_at
ON model_quality_scores
FOR EACH ROW
WHEN
  NEW.model_quality_score_id IS NOT OLD.model_quality_score_id
  OR NEW.tenant_id IS NOT OLD.tenant_id
  OR NEW.model_id IS NOT OLD.model_id
  OR NEW.status IS NOT OLD.status
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
  SELECT RAISE(ABORT, 'model_quality_score_identity_immutable');
END;

-- 每次 UPDATE 必须恰好是一个统计样本或一次人工评分推进；乱序统计只能扩展时间极值，禁止收缩。
CREATE TRIGGER trg_model_quality_scores_prd048_transition_update
BEFORE UPDATE ON model_quality_scores
FOR EACH ROW
WHEN NOT (
  (
    NEW.sample_count=OLD.sample_count+1
    AND NEW.statistics_version=OLD.statistics_version+1
    AND (
      (NEW.success_count=OLD.success_count+1 AND NEW.error_count=OLD.error_count)
      OR
      (NEW.success_count=OLD.success_count AND NEW.error_count=OLD.error_count+1)
    )
    AND (
      (NEW.latency_sample_count=OLD.latency_sample_count
        AND NEW.total_latency_ms=OLD.total_latency_ms)
      OR
      (NEW.latency_sample_count=OLD.latency_sample_count+1
        AND NEW.total_latency_ms>=OLD.total_latency_ms)
    )
    AND NEW.feedback_version=OLD.feedback_version
    AND NEW.manual_quality_bps IS OLD.manual_quality_bps
    AND NEW.feedback_updated_by IS OLD.feedback_updated_by
    AND NEW.feedback_updated_at IS OLD.feedback_updated_at
    AND NEW.first_observed_at IS NOT NULL
    AND NEW.last_observed_at IS NOT NULL
    AND NEW.first_observed_at<=NEW.last_observed_at
    AND (
      (OLD.sample_count=0 AND NEW.first_observed_at=NEW.last_observed_at)
      OR
      (
        OLD.sample_count>0
        AND NEW.first_observed_at<=OLD.first_observed_at
        AND NEW.last_observed_at>=OLD.last_observed_at
        -- 一个观察时刻不可能同时位于旧区间两端，禁止伪造双向扩张。
        AND (
          NEW.first_observed_at=OLD.first_observed_at
          OR NEW.last_observed_at=OLD.last_observed_at
        )
      )
    )
    -- 统计更新时间取旧值和本次观察的 MAX；不得倒退，也不能跳到与 last 无关的任意时间。
    AND NEW.updated_at>=OLD.updated_at
    AND (
      NEW.updated_at=OLD.updated_at
      OR NEW.updated_at=NEW.last_observed_at
    )
  )
  OR
  (
    NEW.sample_count=OLD.sample_count
    AND NEW.success_count=OLD.success_count
    AND NEW.error_count=OLD.error_count
    AND NEW.latency_sample_count=OLD.latency_sample_count
    AND NEW.total_latency_ms=OLD.total_latency_ms
    AND NEW.statistics_version=OLD.statistics_version
    AND NEW.first_observed_at IS OLD.first_observed_at
    AND NEW.last_observed_at IS OLD.last_observed_at
    AND NEW.feedback_version=OLD.feedback_version+1
    AND NEW.manual_quality_bps IS NOT OLD.manual_quality_bps
    AND NEW.feedback_updated_by IS NOT NULL
    AND NEW.feedback_updated_at IS NOT NULL
  )
)
BEGIN
  SELECT RAISE(ABORT, 'model_quality_score_transition_invalid');
END;

-- 最近操作者仍必须属于同租户；单列 users 外键不足以阻止直接 SQL 串租户写入。
CREATE TRIGGER trg_model_quality_scores_prd048_owner_update
BEFORE UPDATE OF feedback_updated_by ON model_quality_scores
FOR EACH ROW
WHEN NEW.feedback_updated_by IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM users u
  WHERE u.tenant_id=NEW.tenant_id AND u.user_id=NEW.feedback_updated_by
)
BEGIN
  SELECT RAISE(ABORT, 'model_quality_score_actor_owner_invalid');
END;

CREATE TRIGGER trg_model_quality_scores_prd048_delete
BEFORE DELETE ON model_quality_scores
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'model_quality_score_delete_forbidden');
END;

-- 人工评分事件是低频、不可变、可按 request_id 恢复的提交事实，不记录自动统计的每次增量。
CREATE TABLE model_quality_score_events (
  model_quality_score_event_id TEXT PRIMARY KEY CHECK (
    length(model_quality_score_event_id)=36
    AND substr(model_quality_score_event_id, 1, 4)='mqe_'
    AND substr(model_quality_score_event_id, 5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  model_quality_score_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type='model.feedback_recorded'),
  actor_user_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK (length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK (length(trace_id) BETWEEN 1 AND 128),
  request_fingerprint TEXT NOT NULL CHECK (
    length(request_fingerprint)=64
    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  feedback_version INTEGER NOT NULL CHECK (
    typeof(feedback_version)='integer' AND feedback_version>=2
  ),
  statistics_version INTEGER NOT NULL CHECK (
    typeof(statistics_version)='integer' AND statistics_version>=0
  ),
  sample_count INTEGER NOT NULL CHECK (
    typeof(sample_count)='integer' AND sample_count>=0
  ),
  previous_manual_quality_bps INTEGER CHECK (
    previous_manual_quality_bps IS NULL
    OR (typeof(previous_manual_quality_bps)='integer'
        AND previous_manual_quality_bps BETWEEN 0 AND 10000)
  ),
  manual_quality_bps INTEGER CHECK (
    manual_quality_bps IS NULL
    OR (typeof(manual_quality_bps)='integer' AND manual_quality_bps BETWEEN 0 AND 10000)
  ),
  payload_json TEXT NOT NULL CHECK (
    json_valid(payload_json) AND json_type(payload_json)='object'
    AND length(CAST(payload_json AS BLOB))<=8192
  ),
  created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 64),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, model_quality_score_id)
    REFERENCES model_quality_scores(tenant_id, model_quality_score_id),
  FOREIGN KEY (tenant_id, model_id) REFERENCES models(tenant_id, model_id),
  FOREIGN KEY (tenant_id, actor_user_id) REFERENCES users(tenant_id, user_id),
  UNIQUE (tenant_id, model_quality_score_event_id),
  UNIQUE (tenant_id, actor_user_id, request_id),
  CHECK (statistics_version=sample_count),
  CHECK (previous_manual_quality_bps IS NOT manual_quality_bps),
  CHECK (
    COALESCE(
      json_remove(
        payload_json,
        '$.trigger', '$.request_id', '$.model_quality_score_id', '$.model_id',
        '$.previous_manual_quality_bps', '$.manual_quality_bps',
        '$.feedback_version', '$.statistics_version', '$.sample_count'
      )='{}'
      AND json_extract(payload_json, '$.trigger')='manual_score_updated'
      AND json_extract(payload_json, '$.request_id')=request_id
      AND json_extract(payload_json, '$.model_quality_score_id')=model_quality_score_id
      AND json_extract(payload_json, '$.model_id')=model_id
      AND json_extract(payload_json, '$.previous_manual_quality_bps')
          IS previous_manual_quality_bps
      AND json_extract(payload_json, '$.manual_quality_bps') IS manual_quality_bps
      AND json_extract(payload_json, '$.feedback_version')=feedback_version
      AND json_extract(payload_json, '$.statistics_version')=statistics_version
      AND json_extract(payload_json, '$.sample_count')=sample_count,
      0
    )
  )
);

CREATE INDEX idx_model_quality_score_events_tenant_time
  ON model_quality_score_events(tenant_id, created_at DESC, model_quality_score_event_id DESC);
CREATE INDEX idx_model_quality_score_events_score_version
  ON model_quality_score_events(tenant_id, model_quality_score_id, feedback_version DESC);

-- 事件必须精确绑定更新后的 Score、当前人工评分和同租户 actor，不能伪造陈旧版本。
CREATE TRIGGER trg_model_quality_score_events_prd048_graph_insert
BEFORE INSERT ON model_quality_score_events
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1
  FROM model_quality_scores qs
  JOIN users u
    ON u.tenant_id=qs.tenant_id AND u.user_id=NEW.actor_user_id
  WHERE qs.tenant_id=NEW.tenant_id
    AND qs.model_quality_score_id=NEW.model_quality_score_id
    AND qs.model_id=NEW.model_id
    AND qs.feedback_version=NEW.feedback_version
    AND qs.statistics_version=NEW.statistics_version
    AND qs.sample_count=NEW.sample_count
    AND qs.manual_quality_bps IS NEW.manual_quality_bps
)
BEGIN
  SELECT RAISE(ABORT, 'model_quality_score_event_graph_invalid');
END;

CREATE TRIGGER trg_model_quality_score_events_prd048_immutable_update
BEFORE UPDATE ON model_quality_score_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'model_quality_score_event_immutable');
END;
CREATE TRIGGER trg_model_quality_score_events_prd048_immutable_delete
BEFORE DELETE ON model_quality_score_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'model_quality_score_event_immutable');
END;

-- RoutingPolicy 增加第四模式与休眠质量配置；存量 mode/version/actor/time 必须逐值保留。
DROP TRIGGER IF EXISTS trg_model_aliases_prd047_policy_update;
DROP TRIGGER IF EXISTS trg_model_aliases_prd047_policy_insert;
-- SQLite 在删除父表时会重新解析引用它的 trigger；先撤销跨表 trigger，待新结构就位后统一重建。
DROP TRIGGER IF EXISTS trg_routing_decisions_prd047_owner_update;
DROP TRIGGER IF EXISTS trg_routing_decisions_prd047_owner_insert;
DROP TRIGGER IF EXISTS trg_routing_candidates_prd047_graph_insert;
DROP TRIGGER IF EXISTS trg_routing_policy_events_prd047_graph_insert;
-- 临时目标表必须从空名字开始，防止上次失败残留被误当成可信复制目标。
DROP TABLE IF EXISTS model_alias_routing_policies_prd048_new;
CREATE TABLE model_alias_routing_policies_prd048_new (
  routing_policy_id TEXT PRIMARY KEY CHECK (
    length(routing_policy_id)=37
    AND substr(routing_policy_id, 1, 5)='rpol_'
    AND substr(routing_policy_id, 6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  model_alias_id TEXT NOT NULL,
  routing_mode TEXT NOT NULL CHECK (
    routing_mode IN ('fixed', 'cost_first', 'availability_first', 'quality_cost')
  ),
  quality_weight_bps INTEGER NOT NULL DEFAULT 5000 CHECK (
    typeof(quality_weight_bps)='integer' AND quality_weight_bps BETWEEN 1 AND 10000
  ),
  cost_weight_bps INTEGER NOT NULL DEFAULT 3000 CHECK (
    typeof(cost_weight_bps)='integer' AND cost_weight_bps BETWEEN 0 AND 10000
  ),
  latency_weight_bps INTEGER NOT NULL DEFAULT 2000 CHECK (
    typeof(latency_weight_bps)='integer' AND latency_weight_bps BETWEEN 0 AND 10000
  ),
  quality_floor_bps INTEGER NOT NULL DEFAULT 6000 CHECK (
    typeof(quality_floor_bps)='integer' AND quality_floor_bps BETWEEN 1 AND 10000
  ),
  minimum_sample_count INTEGER NOT NULL DEFAULT 20 CHECK (
    typeof(minimum_sample_count)='integer' AND minimum_sample_count BETWEEN 1 AND 10000
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
  UNIQUE (tenant_id, routing_policy_id),
  CHECK (quality_weight_bps+cost_weight_bps+latency_weight_bps=10000)
);

-- 存量 Policy 逐列复制并只填充休眠默认配置，routing_mode、version 和 owner 事实保持原值。
INSERT INTO model_alias_routing_policies_prd048_new(
  routing_policy_id, tenant_id, model_alias_id, routing_mode,
  quality_weight_bps, cost_weight_bps, latency_weight_bps,
  quality_floor_bps, minimum_sample_count,
  status, version, created_by, updated_by, created_at, updated_at
)
SELECT
  routing_policy_id, tenant_id, model_alias_id, routing_mode,
  5000, 3000, 2000, 6000, 20,
  status, version, created_by, updated_by, created_at, updated_at
FROM model_alias_routing_policies;

-- 复制完成后再建立一次性 manifest 守卫，旧父表在守卫通过前绝不删除。
DROP TABLE IF EXISTS prd048_policy_copy_guard;
CREATE TABLE prd048_policy_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  changed_count INTEGER NOT NULL,
  CHECK (source_count=copied_count AND changed_count=0)
);
-- 对行数和每个历史业务字段做双重比对，默认配置不参与“历史被改写”判定。
INSERT INTO prd048_policy_copy_guard(source_count, copied_count, changed_count)
SELECT
  (SELECT COUNT(*) FROM model_alias_routing_policies),
  (SELECT COUNT(*) FROM model_alias_routing_policies_prd048_new),
  (SELECT COUNT(*)
   FROM model_alias_routing_policies old
   LEFT JOIN model_alias_routing_policies_prd048_new new
     ON new.tenant_id=old.tenant_id AND new.routing_policy_id=old.routing_policy_id
   WHERE new.routing_policy_id IS NULL
      OR new.model_alias_id!=old.model_alias_id
      OR new.routing_mode!=old.routing_mode
      OR new.status!=old.status
      OR new.version!=old.version
      OR new.created_by IS NOT old.created_by
      OR new.updated_by IS NOT old.updated_by
      OR new.created_at!=old.created_at
      OR new.updated_at!=old.updated_at)
;
DROP TABLE prd048_policy_copy_guard;

DROP TABLE model_alias_routing_policies;
-- 只有复制守卫通过后才切换正式表名，使外键和 trigger 后续统一绑定新结构。
ALTER TABLE model_alias_routing_policies_prd048_new RENAME TO model_alias_routing_policies;

CREATE INDEX idx_routing_policies_tenant_mode
  ON model_alias_routing_policies(tenant_id, routing_mode, updated_at DESC);

-- 新 Alias 仍默认 fixed；休眠质量配置只提供未来显式启用，不改变存量或新建流量。
CREATE TRIGGER trg_model_aliases_prd047_policy_insert
AFTER INSERT ON model_aliases
FOR EACH ROW
BEGIN
  -- 新 Alias 仍创建 fixed Policy，质量配置只是休眠值，不能未经管理员显式启用影响流量。
  INSERT INTO model_alias_routing_policies(
    routing_policy_id, tenant_id, model_alias_id, routing_mode,
    quality_weight_bps, cost_weight_bps, latency_weight_bps,
    quality_floor_bps, minimum_sample_count,
    status, version, created_by, updated_by, created_at, updated_at
  )
  VALUES(
    'rpol_' || lower(hex(randomblob(16))), NEW.tenant_id, NEW.model_alias_id,
    'fixed', 5000, 3000, 2000, 6000, 20,
    'enabled', 1, NEW.created_by, NEW.created_by, NEW.created_at, NEW.updated_at
  );
END;

-- Alias 图字段仍可执行合法 version-only +1；0037 不能因 Policy 新字段而破坏 PRD-047 合同。
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
  -- Alias 图发生真实变化时只推进同租户 Policy 一次版本，保留既有 CAS 可见性合同。
  UPDATE model_alias_routing_policies
  SET version=version+1, updated_at=NEW.updated_at
  WHERE tenant_id=NEW.tenant_id AND model_alias_id=NEW.model_alias_id;
END;

CREATE TRIGGER trg_routing_policies_prd048_identity_update
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

-- Policy 自身执行字段变化必须严格 +1；无字段变化的 +1 仅为 Alias 父图 trigger 保留。
CREATE TRIGGER trg_routing_policies_prd048_version_update
BEFORE UPDATE OF
  routing_mode, quality_weight_bps, cost_weight_bps, latency_weight_bps,
  quality_floor_bps, minimum_sample_count, status, version
ON model_alias_routing_policies
FOR EACH ROW
WHEN
  NEW.version<OLD.version
  OR NEW.version>OLD.version+1
  OR (
    (
      NEW.routing_mode IS NOT OLD.routing_mode
      OR NEW.quality_weight_bps IS NOT OLD.quality_weight_bps
      OR NEW.cost_weight_bps IS NOT OLD.cost_weight_bps
      OR NEW.latency_weight_bps IS NOT OLD.latency_weight_bps
      OR NEW.quality_floor_bps IS NOT OLD.quality_floor_bps
      OR NEW.minimum_sample_count IS NOT OLD.minimum_sample_count
      OR NEW.status IS NOT OLD.status
    )
    AND NEW.version!=OLD.version+1
  )
BEGIN
  SELECT RAISE(ABORT, 'routing_policy_version_invalid');
END;

CREATE TRIGGER trg_routing_policies_prd048_delete
BEFORE DELETE ON model_alias_routing_policies
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM model_aliases a
  WHERE a.tenant_id=OLD.tenant_id AND a.model_alias_id=OLD.model_alias_id
)
BEGIN
  SELECT RAISE(ABORT, 'routing_policy_alias_bound');
END;

-- Decision 扩展质量配置与公式快照；0036 历史新字段统一为空/false，原字段逐值保留。
DROP TABLE IF EXISTS routing_decisions_prd048_new;
CREATE TABLE routing_decisions_prd048_new (
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
    routing_mode IS NULL
    OR routing_mode IN ('fixed', 'cost_first', 'availability_first', 'quality_cost')
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
  quality_weight_bps INTEGER CHECK (
    quality_weight_bps IS NULL
    OR (typeof(quality_weight_bps)='integer' AND quality_weight_bps BETWEEN 1 AND 10000)
  ),
  cost_weight_bps INTEGER CHECK (
    cost_weight_bps IS NULL
    OR (typeof(cost_weight_bps)='integer' AND cost_weight_bps BETWEEN 0 AND 10000)
  ),
  latency_weight_bps INTEGER CHECK (
    latency_weight_bps IS NULL
    OR (typeof(latency_weight_bps)='integer' AND latency_weight_bps BETWEEN 0 AND 10000)
  ),
  quality_floor_bps INTEGER CHECK (
    quality_floor_bps IS NULL
    OR (typeof(quality_floor_bps)='integer' AND quality_floor_bps BETWEEN 1 AND 10000)
  ),
  minimum_sample_count INTEGER CHECK (
    minimum_sample_count IS NULL
    OR (typeof(minimum_sample_count)='integer' AND minimum_sample_count BETWEEN 1 AND 10000)
  ),
  quality_as_of TEXT,
  quality_formula_version TEXT CHECK (
    quality_formula_version IS NULL OR quality_formula_version='quality_cost_v1'
  ),
  quality_scoring_performed INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(quality_scoring_performed)='integer'
    AND quality_scoring_performed IN (0, 1)
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
  -- 价格/Token 只允许 cost_first 或实际进入质量成本价格阶段的 quality_cost 决策保存。
  CHECK (
    (price_as_of IS NULL AND estimated_input_tokens IS NULL
      AND estimated_output_tokens IS NULL AND token_estimate_truncated IS NULL)
    OR
    (routing_mode IN ('cost_first', 'quality_cost') AND price_as_of IS NOT NULL
      AND estimated_input_tokens IS NOT NULL AND estimated_output_tokens IS NOT NULL
      AND token_estimate_truncated IS NOT NULL)
  ),
  -- 旧模式和未进入质量阶段的失败允许质量组全空；非空组必须完整且只属于 quality_cost。
  CHECK (
    (
      quality_weight_bps IS NULL AND cost_weight_bps IS NULL
      AND latency_weight_bps IS NULL AND quality_floor_bps IS NULL
      AND minimum_sample_count IS NULL AND quality_as_of IS NULL
      AND quality_formula_version IS NULL AND quality_scoring_performed=0
    )
    OR
    (
      routing_mode='quality_cost'
      AND quality_weight_bps IS NOT NULL AND cost_weight_bps IS NOT NULL
      AND latency_weight_bps IS NOT NULL AND quality_floor_bps IS NOT NULL
      AND minimum_sample_count IS NOT NULL AND quality_as_of IS NOT NULL
      AND quality_formula_version='quality_cost_v1'
      AND quality_weight_bps+cost_weight_bps+latency_weight_bps=10000
    )
  )
);

-- 旧 Decision 逐列保真复制，新增质量快照列统一为空且 performed=0，禁止伪造历史评分。
INSERT INTO routing_decisions_prd048_new(
  routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
  model_alias_id, requested_model, environment, selected_model_id,
  selected_internal_model_id, provider_id, provider_code, decision_status,
  decision_reason, candidate_summary, error_code, created_at, environment_id,
  routing_policy_id, routing_policy_version, routing_mode, price_as_of,
  estimated_input_tokens, estimated_output_tokens, token_estimate_truncated,
  routing_policy_event_id,
  quality_weight_bps, cost_weight_bps, latency_weight_bps,
  quality_floor_bps, minimum_sample_count, quality_as_of,
  quality_formula_version, quality_scoring_performed
)
SELECT
  routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
  model_alias_id, requested_model, environment, selected_model_id,
  selected_internal_model_id, provider_id, provider_code, decision_status,
  decision_reason, candidate_summary, error_code, created_at, environment_id,
  routing_policy_id, routing_policy_version, routing_mode, price_as_of,
  estimated_input_tokens, estimated_output_tokens, token_estimate_truncated,
  routing_policy_event_id,
  NULL, NULL, NULL, NULL, NULL, NULL, NULL, 0
FROM routing_decisions;

-- 正式换表前创建 Decision manifest，确保不存在漏行或把旧模式误标为质量评分的情况。
DROP TABLE IF EXISTS prd048_decision_copy_guard;
CREATE TABLE prd048_decision_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  quality_leak_count INTEGER NOT NULL,
  CHECK (source_count=copied_count AND quality_leak_count=0)
);
-- 行数与新增列泄漏同时为零才允许继续，任一异常通过 CHECK 终止当前事务。
INSERT INTO prd048_decision_copy_guard(source_count, copied_count, quality_leak_count)
SELECT
  (SELECT COUNT(*) FROM routing_decisions),
  (SELECT COUNT(*) FROM routing_decisions_prd048_new),
  (SELECT COUNT(*) FROM routing_decisions_prd048_new
   WHERE quality_weight_bps IS NOT NULL OR cost_weight_bps IS NOT NULL
      OR latency_weight_bps IS NOT NULL OR quality_floor_bps IS NOT NULL
      OR minimum_sample_count IS NOT NULL OR quality_as_of IS NOT NULL
      OR quality_formula_version IS NOT NULL OR quality_scoring_performed!=0);
DROP TABLE prd048_decision_copy_guard;

DROP TABLE routing_decisions;
-- Decision 复制验证通过后才将新表提升为正式表，避免留下半升级父子图。
ALTER TABLE routing_decisions_prd048_new RENAME TO routing_decisions;

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

-- Decision owner trigger 保持 0036 的 legacy Key/Environment 兼容，并校验新增质量配置来自同一 Policy。
CREATE TRIGGER trg_routing_decisions_prd048_owner_insert
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
      AND (
        NEW.quality_weight_bps IS NULL
        OR (
          NEW.routing_mode='quality_cost'
          AND rp.quality_weight_bps=NEW.quality_weight_bps
          AND rp.cost_weight_bps=NEW.cost_weight_bps
          AND rp.latency_weight_bps=NEW.latency_weight_bps
          AND rp.quality_floor_bps=NEW.quality_floor_bps
          AND rp.minimum_sample_count=NEW.minimum_sample_count
        )
      )
  ))
BEGIN
  SELECT RAISE(ABORT, 'routing_decision_owner_graph_invalid');
END;

CREATE TRIGGER trg_routing_decisions_prd048_owner_update
BEFORE UPDATE OF tenant_id, virtual_key_id, environment_id, environment,
  model_alias_id, selected_model_id, provider_id, routing_policy_id,
  routing_policy_version, routing_mode,
  quality_weight_bps, cost_weight_bps, latency_weight_bps,
  quality_floor_bps, minimum_sample_count
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
      AND (
        NEW.quality_weight_bps IS NULL
        OR (
          NEW.routing_mode='quality_cost'
          AND rp.quality_weight_bps=NEW.quality_weight_bps
          AND rp.cost_weight_bps=NEW.cost_weight_bps
          AND rp.latency_weight_bps=NEW.latency_weight_bps
          AND rp.quality_floor_bps=NEW.quality_floor_bps
          AND rp.minimum_sample_count=NEW.minimum_sample_count
        )
      )
  ))
BEGIN
  SELECT RAISE(ABORT, 'routing_decision_owner_graph_invalid');
END;

-- Candidate 是每请求冻结事实；0036 行增加 not_evaluated 状态，原排序、价格和原因逐值保留。
DROP TABLE IF EXISTS routing_candidates_prd048_new;
CREATE TABLE routing_candidates_prd048_new (
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
      'routing.candidate.keypool_unavailable',
      'routing.quality_cost_highest_score',
      'routing.quality_cost_score_tie_stable_priority',
      'routing.quality_cost_cold_start_priority',
      'routing.quality_cost_evidence_priority',
      'routing.quality_cost_fixed_fallback.price_missing',
      'routing.quality_cost_fixed_fallback.currency_incomparable',
      'routing.quality_cost_fixed_fallback.latency_missing',
      'routing.quality_cost_single_eligible_candidate',
      'routing.candidate.quality_below_floor',
      'routing.candidate.quality_cold_start_deferred'
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
  model_quality_score_id TEXT,
  quality_statistics_version INTEGER CHECK (
    quality_statistics_version IS NULL
    OR (typeof(quality_statistics_version)='integer' AND quality_statistics_version>=0)
  ),
  quality_feedback_version INTEGER CHECK (
    quality_feedback_version IS NULL
    OR (typeof(quality_feedback_version)='integer' AND quality_feedback_version>=1)
  ),
  quality_evidence_state TEXT NOT NULL DEFAULT 'not_evaluated' CHECK (
    quality_evidence_state IN ('ready', 'cold', 'not_evaluated')
  ),
  quality_sample_count INTEGER CHECK (
    quality_sample_count IS NULL
    OR (typeof(quality_sample_count)='integer' AND quality_sample_count>=0)
  ),
  quality_success_count INTEGER CHECK (
    quality_success_count IS NULL
    OR (typeof(quality_success_count)='integer' AND quality_success_count>=0)
  ),
  quality_error_count INTEGER CHECK (
    quality_error_count IS NULL
    OR (typeof(quality_error_count)='integer' AND quality_error_count>=0)
  ),
  manual_quality_bps INTEGER CHECK (
    manual_quality_bps IS NULL
    OR (typeof(manual_quality_bps)='integer' AND manual_quality_bps BETWEEN 0 AND 10000)
  ),
  average_latency_ms TEXT CHECK (
    average_latency_ms IS NULL
    OR (
      length(average_latency_ms) BETWEEN 5 AND 80
      AND average_latency_ms NOT GLOB '*[^0-9.]*'
      AND instr(average_latency_ms, '.')>0
      AND instr(substr(average_latency_ms, instr(average_latency_ms, '.')+1), '.')=0
      AND length(substr(average_latency_ms, instr(average_latency_ms, '.')+1))=3
    )
  ),
  quality_component_bps INTEGER CHECK (
    quality_component_bps IS NULL
    OR (typeof(quality_component_bps)='integer' AND quality_component_bps BETWEEN 0 AND 10000)
  ),
  cost_component_bps INTEGER CHECK (
    cost_component_bps IS NULL
    OR (typeof(cost_component_bps)='integer' AND cost_component_bps BETWEEN 0 AND 10000)
  ),
  latency_component_bps INTEGER CHECK (
    latency_component_bps IS NULL
    OR (typeof(latency_component_bps)='integer' AND latency_component_bps BETWEEN 0 AND 10000)
  ),
  weighted_score_bps INTEGER CHECK (
    weighted_score_bps IS NULL
    OR (typeof(weighted_score_bps)='integer' AND weighted_score_bps BETWEEN 0 AND 10000)
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
  FOREIGN KEY (tenant_id, model_quality_score_id)
    REFERENCES model_quality_scores(tenant_id, model_quality_score_id),
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
  ),
  -- 未评估行不能伪造半组质量事实；ready/cold 行必须携带可复现计数和质量分。
  CHECK (
    (
      quality_evidence_state='not_evaluated'
      AND model_quality_score_id IS NULL
      AND quality_statistics_version IS NULL AND quality_feedback_version IS NULL
      AND quality_sample_count IS NULL AND quality_success_count IS NULL
      AND quality_error_count IS NULL AND manual_quality_bps IS NULL
      AND average_latency_ms IS NULL AND quality_component_bps IS NULL
      AND cost_component_bps IS NULL AND latency_component_bps IS NULL
      AND weighted_score_bps IS NULL
    )
    OR
    (
      quality_evidence_state IN ('ready', 'cold')
      AND model_quality_score_id IS NOT NULL
      AND quality_statistics_version IS NOT NULL AND quality_feedback_version IS NOT NULL
      AND quality_sample_count IS NOT NULL AND quality_success_count IS NOT NULL
      AND quality_error_count IS NOT NULL AND quality_component_bps IS NOT NULL
      AND quality_sample_count=quality_success_count+quality_error_count
      AND quality_statistics_version=quality_sample_count
    )
  )
);

-- 旧 Candidate 逐列复制，质量证据和组件列保持未评估，不能以默认零值重写历史解释。
INSERT INTO routing_candidates_prd048_new(
  routing_candidate_id, tenant_id, routing_decision_id, routing_policy_id,
  model_alias_id, model_id, provider_id, candidate_role, configured_priority,
  rank, status, reason_code, health_state, health_fresh, keypool_state,
  price_available, price_source, price_schema_version, currency,
  estimated_cost_amount,
  model_quality_score_id, quality_statistics_version, quality_feedback_version,
  quality_evidence_state, quality_sample_count, quality_success_count,
  quality_error_count, manual_quality_bps, average_latency_ms,
  quality_component_bps, cost_component_bps, latency_component_bps,
  weighted_score_bps, created_at, updated_at
)
SELECT
  routing_candidate_id, tenant_id, routing_decision_id, routing_policy_id,
  model_alias_id, model_id, provider_id, candidate_role, configured_priority,
  rank, status, reason_code, health_state, health_fresh, keypool_state,
  price_available, price_source, price_schema_version, currency,
  estimated_cost_amount,
  NULL, NULL, NULL, 'not_evaluated', NULL, NULL, NULL, NULL, NULL,
  NULL, NULL, NULL, NULL, created_at, updated_at
FROM routing_candidates;

-- Candidate 正式换表前先建立行数与误评估双守卫，防止父子决策图出现静默缺口。
DROP TABLE IF EXISTS prd048_candidate_copy_guard;
CREATE TABLE prd048_candidate_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  evaluated_count INTEGER NOT NULL,
  CHECK (source_count=copied_count AND evaluated_count=0)
);
-- 只有全部旧候选复制且没有生成质量证据时才允许删除旧表。
INSERT INTO prd048_candidate_copy_guard(source_count, copied_count, evaluated_count)
SELECT
  (SELECT COUNT(*) FROM routing_candidates),
  (SELECT COUNT(*) FROM routing_candidates_prd048_new),
  (SELECT COUNT(*) FROM routing_candidates_prd048_new
   WHERE quality_evidence_state!='not_evaluated'
      OR model_quality_score_id IS NOT NULL OR weighted_score_bps IS NOT NULL);
DROP TABLE prd048_candidate_copy_guard;

DROP TABLE routing_candidates;
-- 守卫通过后原子切换 Candidate 表名，随后重建唯一索引和租户归属 trigger。
ALTER TABLE routing_candidates_prd048_new RENAME TO routing_candidates;

CREATE UNIQUE INDEX idx_routing_candidates_decision_rank_unique
  ON routing_candidates(tenant_id, routing_decision_id, rank)
  WHERE rank IS NOT NULL;
CREATE INDEX idx_routing_candidates_decision_order
  ON routing_candidates(tenant_id, routing_decision_id, rank, configured_priority);
CREATE INDEX idx_routing_candidates_quality_score
  ON routing_candidates(tenant_id, model_quality_score_id, created_at DESC)
  WHERE model_quality_score_id IS NOT NULL;

-- Candidate graph 同时绑定 Decision、Policy、Alias 角色、Model/Provider 和当前 Score 版本。
CREATE TRIGGER trg_routing_candidates_prd048_graph_insert
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
  OR (
    NEW.quality_evidence_state!='not_evaluated' AND NOT EXISTS (
      SELECT 1
      FROM model_quality_scores qs
      JOIN routing_decisions d
        ON d.tenant_id=NEW.tenant_id AND d.routing_decision_id=NEW.routing_decision_id
      WHERE qs.tenant_id=NEW.tenant_id
        AND qs.model_quality_score_id=NEW.model_quality_score_id
        AND qs.model_id=NEW.model_id
        AND qs.statistics_version=NEW.quality_statistics_version
        AND qs.feedback_version=NEW.quality_feedback_version
        AND qs.sample_count=NEW.quality_sample_count
        AND qs.success_count=NEW.quality_success_count
        AND qs.error_count=NEW.quality_error_count
        AND qs.manual_quality_bps IS NEW.manual_quality_bps
        AND d.routing_mode='quality_cost'
        AND (
          (NEW.quality_evidence_state='ready'
            AND (qs.manual_quality_bps IS NOT NULL
                 OR qs.sample_count>=d.minimum_sample_count))
          OR
          (NEW.quality_evidence_state='cold'
            AND qs.manual_quality_bps IS NULL
            AND qs.sample_count<d.minimum_sample_count)
        )
    )
  )
  OR (
    NEW.quality_evidence_state='not_evaluated'
    AND EXISTS (
      SELECT 1 FROM routing_decisions d
      WHERE d.tenant_id=NEW.tenant_id
        AND d.routing_decision_id=NEW.routing_decision_id
        AND d.quality_scoring_performed=1
    )
  )
  OR (
    NEW.quality_evidence_state!='not_evaluated'
    AND EXISTS (
      SELECT 1 FROM routing_decisions d
      WHERE d.tenant_id=NEW.tenant_id
        AND d.routing_decision_id=NEW.routing_decision_id
        AND (
          (d.quality_scoring_performed=1 AND (
            NEW.weighted_score_bps IS NULL
            OR (d.cost_weight_bps>0 AND NEW.cost_component_bps IS NULL)
            OR (d.cost_weight_bps=0 AND NEW.cost_component_bps IS NOT NULL)
            OR (d.latency_weight_bps>0 AND NEW.latency_component_bps IS NULL)
            OR (d.latency_weight_bps=0 AND NEW.latency_component_bps IS NOT NULL)
          ))
          OR
          (d.quality_scoring_performed=0 AND (
            NEW.weighted_score_bps IS NOT NULL
            OR NEW.cost_component_bps IS NOT NULL
            OR NEW.latency_component_bps IS NOT NULL
          ))
        )
    )
  )
BEGIN
  SELECT RAISE(ABORT, 'routing_candidate_graph_invalid');
END;

CREATE TRIGGER trg_routing_candidates_prd048_immutable_update
BEFORE UPDATE ON routing_candidates
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'routing_candidate_immutable');
END;
CREATE TRIGGER trg_routing_candidates_prd048_immutable_delete
BEFORE DELETE ON routing_candidates
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'routing_candidate_immutable');
END;

-- 首条 Candidate 形成不可变解释后，Decision 的任何字段都不得再被改写。
CREATE TRIGGER trg_routing_decisions_prd048_candidate_bound_update
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

-- RoutingPolicy 配置事件保留 0036 v1 原文；0037 新写事件必须使用带质量快照的 schema v2。
DROP TABLE IF EXISTS routing_policy_events_prd048_new;
CREATE TABLE routing_policy_events_prd048_new (
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
  payload_schema_version INTEGER NOT NULL CHECK (
    typeof(payload_schema_version)='integer' AND payload_schema_version IN (1, 2)
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
    -- v1 完整复用 0036 精确字段集合和三模式枚举，历史 JSON 字节不重写。
    (
      payload_schema_version=1
      AND COALESCE(
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
    OR
    -- v2 固定携带当前休眠配置；change arrays 只描述本次真实变化，不携带前后模型值。
    (
      payload_schema_version=2
      AND COALESCE(
        json_remove(
          payload_json,
          '$.trigger', '$.request_id', '$.model_alias_id', '$.routing_policy_id',
          '$.routing_policy_version', '$.configuration_schema_version',
          '$.change_kinds', '$.changed_routing_fields', '$.previous_mode',
          '$.routing_mode', '$.quality_cost_config'
        )='{}'
        AND json_extract(payload_json, '$.trigger')=trigger_type
        AND json_extract(payload_json, '$.request_id')=request_id
        AND json_extract(payload_json, '$.model_alias_id')=model_alias_id
        AND json_extract(payload_json, '$.routing_policy_id')=routing_policy_id
        AND json_extract(payload_json, '$.routing_policy_version')=routing_policy_version
        AND json_extract(payload_json, '$.configuration_schema_version')=2
        AND json_type(payload_json, '$.change_kinds')='array'
        AND json_array_length(payload_json, '$.change_kinds') BETWEEN 1 AND 5
        AND json_type(payload_json, '$.changed_routing_fields')='array'
        AND json_array_length(payload_json, '$.changed_routing_fields') BETWEEN 1 AND 8
        AND (
          json_type(payload_json, '$.previous_mode')='null'
          OR json_extract(payload_json, '$.previous_mode')
            IN ('fixed', 'cost_first', 'availability_first', 'quality_cost')
        )
        AND json_extract(payload_json, '$.routing_mode')
          IN ('fixed', 'cost_first', 'availability_first', 'quality_cost')
        AND json_type(payload_json, '$.quality_cost_config')='object'
        AND json_remove(
          json_extract(payload_json, '$.quality_cost_config'),
          '$.quality_weight_bps', '$.cost_weight_bps', '$.latency_weight_bps',
          '$.quality_floor_bps', '$.minimum_sample_count'
        )='{}'
        AND json_type(payload_json, '$.quality_cost_config.quality_weight_bps')='integer'
        AND json_extract(payload_json, '$.quality_cost_config.quality_weight_bps')
          BETWEEN 1 AND 10000
        AND json_type(payload_json, '$.quality_cost_config.cost_weight_bps')='integer'
        AND json_extract(payload_json, '$.quality_cost_config.cost_weight_bps')
          BETWEEN 0 AND 10000
        AND json_type(payload_json, '$.quality_cost_config.latency_weight_bps')='integer'
        AND json_extract(payload_json, '$.quality_cost_config.latency_weight_bps')
          BETWEEN 0 AND 10000
        AND json_type(payload_json, '$.quality_cost_config.quality_floor_bps')='integer'
        AND json_extract(payload_json, '$.quality_cost_config.quality_floor_bps')
          BETWEEN 1 AND 10000
        AND json_type(payload_json, '$.quality_cost_config.minimum_sample_count')='integer'
        AND json_extract(payload_json, '$.quality_cost_config.minimum_sample_count')
          BETWEEN 1 AND 10000
        AND json_extract(payload_json, '$.quality_cost_config.quality_weight_bps')
          + json_extract(payload_json, '$.quality_cost_config.cost_weight_bps')
          + json_extract(payload_json, '$.quality_cost_config.latency_weight_bps')=10000,
        0
      )
    )
  )
);

-- 历史 v1 配置事件按原始 payload 字节复制，新增 schema 列只标记版本而不改写正文。
INSERT INTO routing_policy_events_prd048_new(
  routing_policy_event_id, tenant_id, routing_policy_id, model_alias_id,
  routing_policy_version, event_type, trigger_type, actor_user_id,
  request_id, trace_id, request_fingerprint, payload_schema_version,
  payload_json, created_at
)
SELECT
  routing_policy_event_id, tenant_id, routing_policy_id, model_alias_id,
  routing_policy_version, event_type, trigger_type, actor_user_id,
  request_id, trace_id, request_fingerprint, 1, payload_json, created_at
FROM routing_policy_events;

-- Event 换表前建立字节级复制守卫，任何 v1 正文变化都必须阻断迁移。
DROP TABLE IF EXISTS prd048_policy_event_copy_guard;
CREATE TABLE prd048_policy_event_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  rewritten_count INTEGER NOT NULL,
  CHECK (source_count=copied_count AND rewritten_count=0)
);
-- 同时核对总行数与 v1 payload 原文，避免迁移把旧事件升级伪装成 v2。
INSERT INTO prd048_policy_event_copy_guard(source_count, copied_count, rewritten_count)
SELECT
  (SELECT COUNT(*) FROM routing_policy_events),
  (SELECT COUNT(*) FROM routing_policy_events_prd048_new),
  (SELECT COUNT(*)
   FROM routing_policy_events old
   JOIN routing_policy_events_prd048_new new
     ON new.routing_policy_event_id=old.routing_policy_event_id
   WHERE new.payload_schema_version!=1 OR new.payload_json!=old.payload_json);
DROP TABLE prd048_policy_event_copy_guard;

DROP TABLE routing_policy_events;
-- 事件复制守卫通过后才切换正式表名，并由后续 trigger 约束所有新写 v2 事件。
ALTER TABLE routing_policy_events_prd048_new RENAME TO routing_policy_events;

CREATE INDEX idx_routing_policy_events_tenant_time
  ON routing_policy_events(tenant_id, created_at DESC, routing_policy_event_id DESC);
CREATE INDEX idx_routing_policy_events_policy_version
  ON routing_policy_events(tenant_id, routing_policy_id, routing_policy_version DESC);

-- 迁移完成后禁止服务写入旧 schema；v1 只允许作为逐字节保留的历史事实存在。
CREATE TRIGGER trg_routing_policy_events_prd048_schema_insert
BEFORE INSERT ON routing_policy_events
FOR EACH ROW
WHEN NEW.payload_schema_version!=2
BEGIN
  SELECT RAISE(ABORT, 'routing_policy_event_schema_version_invalid');
END;

-- v2 数组必须使用设计固定顺序，并让 change kind 与 changed field 一一对应。
CREATE TRIGGER trg_routing_policy_events_prd048_payload_insert
BEFORE INSERT ON routing_policy_events
FOR EACH ROW
WHEN NEW.payload_schema_version=2 AND CASE
  WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.payload_json, '$.change_kinds') item
    WHERE item.type!='text'
      OR item.value NOT IN ('created', 'mode', 'quality_cost', 'candidates', 'alias_state')
  ) THEN 1
  WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.payload_json, '$.changed_routing_fields') item
    WHERE item.type!='text'
      OR item.value NOT IN (
        'alias', 'environment', 'environment_id', 'status',
        'default_model_id', 'fallback_model_id', 'routing_mode', 'quality_cost_config'
      )
  ) THEN 1
  WHEN EXISTS (
    SELECT 1
    FROM json_each(NEW.payload_json, '$.change_kinds') earlier
    JOIN json_each(NEW.payload_json, '$.change_kinds') later
      ON CAST(earlier.key AS INTEGER)<CAST(later.key AS INTEGER)
    WHERE CASE earlier.value
      WHEN 'created' THEN 0 WHEN 'mode' THEN 1 WHEN 'quality_cost' THEN 2
      WHEN 'candidates' THEN 3 WHEN 'alias_state' THEN 4 ELSE 99 END
      >= CASE later.value
      WHEN 'created' THEN 0 WHEN 'mode' THEN 1 WHEN 'quality_cost' THEN 2
      WHEN 'candidates' THEN 3 WHEN 'alias_state' THEN 4 ELSE 99 END
  ) THEN 1
  WHEN EXISTS (
    SELECT 1
    FROM json_each(NEW.payload_json, '$.changed_routing_fields') earlier
    JOIN json_each(NEW.payload_json, '$.changed_routing_fields') later
      ON CAST(earlier.key AS INTEGER)<CAST(later.key AS INTEGER)
    WHERE CASE earlier.value
      WHEN 'alias' THEN 0 WHEN 'environment' THEN 1 WHEN 'environment_id' THEN 2
      WHEN 'status' THEN 3 WHEN 'default_model_id' THEN 4
      WHEN 'fallback_model_id' THEN 5 WHEN 'routing_mode' THEN 6
      WHEN 'quality_cost_config' THEN 7 ELSE 99 END
      >= CASE later.value
      WHEN 'alias' THEN 0 WHEN 'environment' THEN 1 WHEN 'environment_id' THEN 2
      WHEN 'status' THEN 3 WHEN 'default_model_id' THEN 4
      WHEN 'fallback_model_id' THEN 5 WHEN 'routing_mode' THEN 6
      WHEN 'quality_cost_config' THEN 7 ELSE 99 END
  ) THEN 1
  WHEN EXISTS (
    SELECT 1 FROM json_each(NEW.payload_json, '$.change_kinds') WHERE value='created'
  ) THEN CASE
    WHEN NEW.routing_policy_version!=1 THEN 1
    WHEN json_type(NEW.payload_json, '$.previous_mode') IS NOT 'null' THEN 1
    WHEN json_array_length(NEW.payload_json, '$.change_kinds')!=1 THEN 1
    WHEN json_array_length(NEW.payload_json, '$.changed_routing_fields')!=8 THEN 1
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
            WHERE value='quality_cost')
    !=
    EXISTS (SELECT 1 FROM json_each(NEW.payload_json, '$.changed_routing_fields')
            WHERE value='quality_cost_config')
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

-- v2 payload 的模式和质量配置必须等于当前 Policy 同版本快照。
CREATE TRIGGER trg_routing_policy_events_prd048_graph_insert
BEFORE INSERT ON routing_policy_events
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM model_alias_routing_policies rp
  WHERE rp.tenant_id=NEW.tenant_id
    AND rp.routing_policy_id=NEW.routing_policy_id
    AND rp.model_alias_id=NEW.model_alias_id
    AND rp.version=NEW.routing_policy_version
    AND rp.routing_mode=json_extract(NEW.payload_json, '$.routing_mode')
    AND rp.quality_weight_bps=
      json_extract(NEW.payload_json, '$.quality_cost_config.quality_weight_bps')
    AND rp.cost_weight_bps=
      json_extract(NEW.payload_json, '$.quality_cost_config.cost_weight_bps')
    AND rp.latency_weight_bps=
      json_extract(NEW.payload_json, '$.quality_cost_config.latency_weight_bps')
    AND rp.quality_floor_bps=
      json_extract(NEW.payload_json, '$.quality_cost_config.quality_floor_bps')
    AND rp.minimum_sample_count=
      json_extract(NEW.payload_json, '$.quality_cost_config.minimum_sample_count')
)
BEGIN
  SELECT RAISE(ABORT, 'routing_policy_event_graph_invalid');
END;

CREATE TRIGGER trg_routing_policy_events_prd048_immutable_update
BEFORE UPDATE ON routing_policy_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'routing_policy_event_immutable');
END;
CREATE TRIGGER trg_routing_policy_events_prd048_immutable_delete
BEFORE DELETE ON routing_policy_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'routing_policy_event_immutable');
END;

-- Audit 只增加人工评分动作/对象；0036 的全部历史行和安全摘要逐列保留。
DROP TABLE IF EXISTS audit_logs_prd048_new;
CREATE TABLE audit_logs_prd048_new (
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
      'routing_policy.created', 'routing_policy.updated',
      'model_quality_score.updated'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'usage_export_job', 'policy',
      'policy_snapshot', 'policy_explanation', 'routing_policy',
      'model_quality_score'
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

-- Audit 逐列复制，仅扩展允许的 action/object 枚举，不得改写既有审计摘要或 request_id。
INSERT INTO audit_logs_prd048_new(
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

-- 审计换表前用 manifest 证明没有漏行；旧不可变 trigger 在验证后才重新绑定。
DROP TABLE IF EXISTS prd048_audit_copy_guard;
CREATE TABLE prd048_audit_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  CHECK (source_count=copied_count)
);
-- 源表与目标表行数必须精确一致，否则 CHECK 使审计结构替换整体回滚。
INSERT INTO prd048_audit_copy_guard(source_count, copied_count)
SELECT (SELECT COUNT(*) FROM audit_logs), (SELECT COUNT(*) FROM audit_logs_prd048_new);
DROP TABLE prd048_audit_copy_guard;

DROP TABLE audit_logs;
-- 复制守卫通过后才把扩展后的审计表提升为正式表，保证不可变链不出现空窗数据。
ALTER TABLE audit_logs_prd048_new RENAME TO audit_logs;

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
CREATE UNIQUE INDEX idx_audit_logs_model_quality_request_unique
  ON audit_logs(tenant_id, actor_user_id, request_id)
  WHERE action='model_quality_score.updated';

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

-- 最终校验所有复合 owner 图和 SQLite 内部结构；任何问题通过 CHECK 中止整个 0037。
DROP TABLE IF EXISTS prd048_integrity_guard;
CREATE TABLE prd048_integrity_guard (
  foreign_key_issue_count INTEGER NOT NULL CHECK (foreign_key_issue_count=0),
  integrity_issue_count INTEGER NOT NULL CHECK (integrity_issue_count=0)
);
-- 把外键和完整性检查写入双零守卫；失败时连同全部结构修改一起回滚。
INSERT INTO prd048_integrity_guard(foreign_key_issue_count, integrity_issue_count)
SELECT
  (SELECT COUNT(*) FROM pragma_foreign_key_check),
  (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd048_integrity_guard;

COMMIT;
PRAGMA foreign_keys = ON;
