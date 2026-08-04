-- PRD-049 调用反馈与采纳率记录：建立不可变反馈、派生模型统计和一对一持久事件。
-- 安全边界：所有事实按 tenant 关联，不保存 Prompt、Response、Token、Key 明文或自由文本标签。
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

-- 前序迁移必须已提供 user/model/Virtual Key 的 tenant-qualified 候选键，禁止悄悄创建同义索引。
CREATE TEMP TABLE prd049_prerequisite_guard (
  missing_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd049_prerequisite_guard
BEFORE INSERT ON prd049_prerequisite_guard
FOR EACH ROW
WHEN NEW.missing_count != 0
BEGIN
  SELECT RAISE(ABORT, 'prd049_prerequisite_missing');
END;
-- 通过 sqlite_master 精确断言三个前序索引，缺一项即在任何正式 DDL 前原子失败。
INSERT INTO prd049_prerequisite_guard(missing_count)
SELECT 3 - COUNT(*)
FROM sqlite_master
WHERE type='index'
  AND name IN (
    'uq_users_tenant_user_prd044',
    'uq_models_tenant_model_prd044',
    'uq_virtual_keys_tenant_key_prd045'
  );
DROP TRIGGER trg_prd049_prerequisite_guard;
DROP TABLE prd049_prerequisite_guard;

-- RequestFeedback 保存一次成功调用的一条不可变评价，并以 deferred FK 要求持久 Event 成对提交。
CREATE TABLE request_feedbacks (
  request_feedback_id TEXT PRIMARY KEY CHECK (
    length(request_feedback_id)=36
    AND substr(request_feedback_id, 1, 4)='rfb_'
    AND substr(request_feedback_id, 5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  source_request_id TEXT NOT NULL CHECK (
    length(source_request_id) BETWEEN 1 AND 128
    AND source_request_id NOT GLOB '*[^A-Za-z0-9._:-]*'
  ),
  model_id TEXT NOT NULL CHECK (length(model_id) BETWEEN 1 AND 128),
  provider_id TEXT NOT NULL CHECK (length(provider_id) BETWEEN 1 AND 128),
  virtual_key_id TEXT CHECK (virtual_key_id IS NULL OR length(virtual_key_id) BETWEEN 1 AND 128),
  project_id TEXT CHECK (project_id IS NULL OR length(project_id) BETWEEN 1 AND 128),
  application_id TEXT CHECK (application_id IS NULL OR length(application_id) BETWEEN 1 AND 128),
  environment_id TEXT CHECK (environment_id IS NULL OR length(environment_id) BETWEEN 1 AND 128),
  rating INTEGER NOT NULL CHECK (typeof(rating)='integer' AND rating BETWEEN 1 AND 5),
  accepted INTEGER NOT NULL CHECK (typeof(accepted)='integer' AND accepted IN (0, 1)),
  tags_json TEXT NOT NULL CHECK (
    json_valid(tags_json)
    AND json_type(tags_json)='array'
    AND json_array_length(tags_json) BETWEEN 0 AND 5
    AND length(tags_json) <= 512
  ),
  actor_type TEXT NOT NULL CHECK (actor_type IN ('session_user', 'virtual_key')),
  actor_user_id TEXT,
  actor_virtual_key_id TEXT,
  operation_request_id TEXT NOT NULL CHECK (
    length(operation_request_id) BETWEEN 1 AND 128
    AND operation_request_id NOT GLOB '*[^A-Za-z0-9._:-]*'
  ),
  request_fingerprint TEXT NOT NULL CHECK (
    length(request_fingerprint)=64
    AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  feedback_event_id TEXT NOT NULL UNIQUE CHECK (
    length(feedback_event_id)=36
    AND substr(feedback_event_id, 1, 4)='fbe_'
    AND substr(feedback_event_id, 5) NOT GLOB '*[^0-9a-f]*'
  ),
  trace_id TEXT NOT NULL CHECK (
    length(trace_id) BETWEEN 1 AND 128
    AND trace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
  ),
  status TEXT NOT NULL CHECK (status='recorded'),
  created_at TEXT NOT NULL CHECK (
    length(created_at)=20 AND created_at GLOB '????-??-??T??:??:??Z'
  ),
  updated_at TEXT NOT NULL CHECK (updated_at=created_at),
  FOREIGN KEY (tenant_id, source_request_id)
    REFERENCES request_logs(tenant_id, request_id),
  FOREIGN KEY (tenant_id, model_id)
    REFERENCES models(tenant_id, model_id),
  FOREIGN KEY (tenant_id, actor_user_id)
    REFERENCES users(tenant_id, user_id),
  FOREIGN KEY (tenant_id, actor_virtual_key_id)
    REFERENCES virtual_keys(tenant_id, virtual_key_id),
  FOREIGN KEY (tenant_id, feedback_event_id)
    REFERENCES request_feedback_events(tenant_id, feedback_event_id)
    DEFERRABLE INITIALLY DEFERRED,
  UNIQUE (tenant_id, request_feedback_id),
  UNIQUE (tenant_id, feedback_event_id),
  UNIQUE (tenant_id, source_request_id),
  CHECK (
    (actor_type='session_user' AND actor_user_id IS NOT NULL AND actor_virtual_key_id IS NULL)
    OR
    (actor_type='virtual_key' AND actor_user_id IS NULL AND actor_virtual_key_id IS NOT NULL)
  )
);

-- all-time 派生表只服务 ModelQuality 联表；时间窗口报表仍必须从不可变 Feedback 原表计算。
CREATE TABLE model_feedback_statistics (
  tenant_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  feedback_count INTEGER NOT NULL CHECK (typeof(feedback_count)='integer' AND feedback_count >= 0),
  rating_sum INTEGER NOT NULL CHECK (typeof(rating_sum)='integer' AND rating_sum >= 0),
  rating_1_count INTEGER NOT NULL CHECK (typeof(rating_1_count)='integer' AND rating_1_count >= 0),
  rating_2_count INTEGER NOT NULL CHECK (typeof(rating_2_count)='integer' AND rating_2_count >= 0),
  rating_3_count INTEGER NOT NULL CHECK (typeof(rating_3_count)='integer' AND rating_3_count >= 0),
  rating_4_count INTEGER NOT NULL CHECK (typeof(rating_4_count)='integer' AND rating_4_count >= 0),
  rating_5_count INTEGER NOT NULL CHECK (typeof(rating_5_count)='integer' AND rating_5_count >= 0),
  accepted_count INTEGER NOT NULL CHECK (
    typeof(accepted_count)='integer'
    AND accepted_count BETWEEN 0 AND feedback_count
  ),
  feedback_statistics_version INTEGER NOT NULL CHECK (
    typeof(feedback_statistics_version)='integer'
    AND feedback_statistics_version=feedback_count
  ),
  updated_at TEXT NOT NULL CHECK (
    length(updated_at)=20 AND updated_at GLOB '????-??-??T??:??:??Z'
  ),
  PRIMARY KEY (tenant_id, model_id),
  FOREIGN KEY (tenant_id, model_id) REFERENCES models(tenant_id, model_id),
  CHECK (
    rating_1_count + rating_2_count + rating_3_count
      + rating_4_count + rating_5_count = feedback_count
  ),
  CHECK (rating_sum BETWEEN feedback_count AND feedback_count * 5)
);

-- 持久事件与 Feedback 一一对应；反向 deferred FK 让缺任一侧的事务在 commit 时失败。
CREATE TABLE request_feedback_events (
  feedback_event_id TEXT PRIMARY KEY CHECK (
    length(feedback_event_id)=36
    AND substr(feedback_event_id, 1, 4)='fbe_'
    AND substr(feedback_event_id, 5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  request_feedback_id TEXT NOT NULL UNIQUE CHECK (
    length(request_feedback_id)=36
    AND substr(request_feedback_id, 1, 4)='rfb_'
    AND substr(request_feedback_id, 5) NOT GLOB '*[^0-9a-f]*'
  ),
  source_request_id TEXT NOT NULL CHECK (
    length(source_request_id) BETWEEN 1 AND 128
    AND source_request_id NOT GLOB '*[^A-Za-z0-9._:-]*'
  ),
  event_type TEXT NOT NULL CHECK (event_type='feedback.created'),
  actor_type TEXT NOT NULL CHECK (actor_type IN ('session_user', 'virtual_key')),
  actor_id TEXT NOT NULL CHECK (length(actor_id) BETWEEN 1 AND 128),
  operation_request_id TEXT NOT NULL CHECK (
    length(operation_request_id) BETWEEN 1 AND 128
    AND operation_request_id NOT GLOB '*[^A-Za-z0-9._:-]*'
  ),
  trace_id TEXT NOT NULL CHECK (
    length(trace_id) BETWEEN 1 AND 128
    AND trace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
  ),
  payload_json TEXT NOT NULL CHECK (
    json_valid(payload_json)
    AND json_type(payload_json)='object'
    AND length(payload_json) <= 4096
  ),
  created_at TEXT NOT NULL CHECK (
    length(created_at)=20 AND created_at GLOB '????-??-??T??:??:??Z'
  ),
  FOREIGN KEY (tenant_id, request_feedback_id)
    REFERENCES request_feedbacks(tenant_id, request_feedback_id)
    DEFERRABLE INITIALLY DEFERRED,
  UNIQUE (tenant_id, feedback_event_id),
  UNIQUE (tenant_id, request_feedback_id)
);

-- Session user 的操作幂等键按 tenant + user 隔离，不能与另一用户或 Key 共享。
CREATE UNIQUE INDEX uq_request_feedback_session_operation_prd049
  ON request_feedbacks(tenant_id, actor_user_id, operation_request_id)
  WHERE actor_type='session_user';
-- Virtual Key 的操作幂等键按 tenant + Key 隔离，不保存或索引 Key 明文。
CREATE UNIQUE INDEX uq_request_feedback_key_operation_prd049
  ON request_feedbacks(tenant_id, actor_virtual_key_id, operation_request_id)
  WHERE actor_type='virtual_key';
-- tenant + time 索引保证无模型筛选的 Summary 先完成租户和窗口裁剪。
CREATE INDEX idx_request_feedback_tenant_time_prd049
  ON request_feedbacks(tenant_id, created_at, model_id);
-- tenant + model + time 索引支撑单模型窗口和 all-time 质量联表核对。
CREATE INDEX idx_request_feedback_model_time_prd049
  ON request_feedbacks(tenant_id, model_id, created_at);
-- 源调用 Key 快照索引支撑同 Key 追踪，NULL 的内部/legacy 调用不占 partial index。
CREATE INDEX idx_request_feedback_key_time_prd049
  ON request_feedbacks(tenant_id, virtual_key_id, created_at)
  WHERE virtual_key_id IS NOT NULL;
-- 持久事件可按租户和创建时间排查投递，payload 本身不参与任何索引。
CREATE INDEX idx_request_feedback_event_time_prd049
  ON request_feedback_events(tenant_id, created_at, feedback_event_id);

-- 标签 trigger 拒绝未知、重复、乱序和正反冲突 code，防止正文借自由标签进入统计/Webhook。
CREATE TRIGGER trg_request_feedback_tags_insert_prd049
BEFORE INSERT ON request_feedbacks
FOR EACH ROW
WHEN
  EXISTS (
    SELECT 1 FROM json_each(NEW.tags_json) item
    WHERE item.type!='text'
       OR item.value NOT IN (
         'accurate', 'relevant', 'complete', 'helpful', 'clear',
         'inaccurate', 'irrelevant', 'incomplete', 'unsafe', 'slow'
       )
  )
  OR (SELECT COUNT(*) FROM json_each(NEW.tags_json))
     != (SELECT COUNT(DISTINCT value) FROM json_each(NEW.tags_json))
  OR NEW.tags_json != COALESCE((
    SELECT json_group_array(value)
    FROM (
      SELECT value
      FROM json_each(NEW.tags_json)
      ORDER BY CASE value
        WHEN 'accurate' THEN 1 WHEN 'relevant' THEN 2 WHEN 'complete' THEN 3
        WHEN 'helpful' THEN 4 WHEN 'clear' THEN 5 WHEN 'inaccurate' THEN 6
        WHEN 'irrelevant' THEN 7 WHEN 'incomplete' THEN 8 WHEN 'unsafe' THEN 9
        WHEN 'slow' THEN 10 ELSE 99 END
    )
  ), '[]')
  OR (
    EXISTS (SELECT 1 FROM json_each(NEW.tags_json) WHERE value='accurate')
    AND EXISTS (SELECT 1 FROM json_each(NEW.tags_json) WHERE value='inaccurate')
  )
  OR (
    EXISTS (SELECT 1 FROM json_each(NEW.tags_json) WHERE value='relevant')
    AND EXISTS (SELECT 1 FROM json_each(NEW.tags_json) WHERE value='irrelevant')
  )
  OR (
    EXISTS (SELECT 1 FROM json_each(NEW.tags_json) WHERE value='complete')
    AND EXISTS (SELECT 1 FROM json_each(NEW.tags_json) WHERE value='incomplete')
  )
BEGIN
  SELECT RAISE(ABORT, 'request_feedback_tags_invalid');
END;

-- graph trigger 在数据库内复核 active actor、同租户/同 Key 源调用、成功终态和最终模型归因。
CREATE TRIGGER trg_request_feedback_graph_insert_prd049
BEFORE INSERT ON request_feedbacks
FOR EACH ROW
WHEN
  NOT EXISTS (
    SELECT 1 FROM tenants tenant
    WHERE tenant.tenant_id=NEW.tenant_id AND tenant.status='active'
  )
  OR (
    NEW.actor_type='session_user'
    AND NOT EXISTS (
      SELECT 1 FROM users user
      WHERE user.tenant_id=NEW.tenant_id
        AND user.user_id=NEW.actor_user_id
        AND user.status='active'
        AND user.role IN ('admin', 'developer')
    )
  )
  OR (
    NEW.actor_type='virtual_key'
    AND NOT EXISTS (
      SELECT 1 FROM virtual_keys key
      WHERE key.tenant_id=NEW.tenant_id
        AND key.virtual_key_id=NEW.actor_virtual_key_id
        AND key.status='active'
    )
  )
  OR NOT EXISTS (
    SELECT 1
    FROM request_logs request
    WHERE request.tenant_id=NEW.tenant_id
      AND request.request_id=NEW.source_request_id
      AND request.status='success'
      AND request.virtual_key_id IS NEW.virtual_key_id
      AND request.project_id IS NEW.project_id
      AND request.application_id IS NEW.application_id
      AND request.environment_id IS NEW.environment_id
      AND (
        (request.stream_session_id IS NULL AND request.stream_status IS NULL)
        OR
        (request.stream_session_id IS NOT NULL AND request.stream_status='completed')
      )
      AND (
        NEW.actor_type='session_user'
        OR (
          request.virtual_key_id IS NOT NULL
          AND request.virtual_key_id=NEW.actor_virtual_key_id
        )
      )
      AND request.model_id=NEW.model_id
      AND request.provider_id=NEW.provider_id
      AND (
        (
          request.routing_attempt_count=0
          AND request.final_routing_attempt_id IS NULL
          AND request.model_id IS NOT NULL
          AND request.provider_id IS NOT NULL
        )
        OR
        (
          request.routing_attempt_count IN (1, 2)
          AND request.final_routing_attempt_id IS NOT NULL
          AND EXISTS (
            SELECT 1 FROM routing_attempts attempt
            WHERE attempt.tenant_id=request.tenant_id
              AND attempt.request_id=request.request_id
              AND attempt.routing_attempt_id=request.final_routing_attempt_id
              AND attempt.status='succeeded'
              AND attempt.model_id=NEW.model_id
              AND attempt.provider_id=NEW.provider_id
          )
        )
      )
  )
  OR NOT EXISTS (
    SELECT 1 FROM models model
    WHERE model.tenant_id=NEW.tenant_id
      AND model.model_id=NEW.model_id
      AND model.provider_id=NEW.provider_id
  )
BEGIN
  SELECT RAISE(ABORT, 'request_feedback_graph_invalid');
END;

-- 新聚合行必须逐字段等于当前不可变 Feedback 重算结果，禁止 direct SQL 制造 ghost count。
CREATE TRIGGER trg_model_feedback_statistics_validate_insert_prd049
BEFORE INSERT ON model_feedback_statistics
FOR EACH ROW
WHEN
  EXISTS (
    SELECT 1 FROM model_feedback_statistics current
    WHERE current.tenant_id=NEW.tenant_id AND current.model_id=NEW.model_id
  )
  OR NEW.feedback_count IS NOT (
    SELECT COUNT(*) FROM request_feedbacks source
    WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id
  )
  OR NEW.rating_sum IS NOT (
    SELECT COALESCE(SUM(rating), 0) FROM request_feedbacks source
    WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id
  )
  OR NEW.rating_1_count IS NOT (
    SELECT COUNT(*) FROM request_feedbacks source
    WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id AND source.rating=1
  )
  OR NEW.rating_2_count IS NOT (
    SELECT COUNT(*) FROM request_feedbacks source
    WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id AND source.rating=2
  )
  OR NEW.rating_3_count IS NOT (
    SELECT COUNT(*) FROM request_feedbacks source
    WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id AND source.rating=3
  )
  OR NEW.rating_4_count IS NOT (
    SELECT COUNT(*) FROM request_feedbacks source
    WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id AND source.rating=4
  )
  OR NEW.rating_5_count IS NOT (
    SELECT COUNT(*) FROM request_feedbacks source
    WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id AND source.rating=5
  )
  OR NEW.accepted_count IS NOT (
    SELECT COALESCE(SUM(accepted), 0) FROM request_feedbacks source
    WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id
  )
  OR NEW.feedback_statistics_version IS NOT NEW.feedback_count
  OR NEW.updated_at IS NOT (
    SELECT MAX(created_at) FROM request_feedbacks source
    WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id
  )
BEGIN
  SELECT RAISE(ABORT, 'model_feedback_statistics_invalid');
END;

-- 聚合更新同样必须逐字段等于原始事实，阻止漏计、错计、倒退或手工覆盖。
CREATE TRIGGER trg_model_feedback_statistics_validate_update_prd049
BEFORE UPDATE ON model_feedback_statistics
FOR EACH ROW
WHEN
  NEW.tenant_id IS NOT OLD.tenant_id
  OR NEW.model_id IS NOT OLD.model_id
  OR NEW.feedback_count IS NOT (
    SELECT COUNT(*) FROM request_feedbacks source
    WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id
  )
  OR NEW.rating_sum IS NOT (
    SELECT COALESCE(SUM(rating), 0) FROM request_feedbacks source
    WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id
  )
  OR NEW.rating_1_count IS NOT (
    SELECT COUNT(*) FROM request_feedbacks source
    WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id AND source.rating=1
  )
  OR NEW.rating_2_count IS NOT (
    SELECT COUNT(*) FROM request_feedbacks source
    WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id AND source.rating=2
  )
  OR NEW.rating_3_count IS NOT (
    SELECT COUNT(*) FROM request_feedbacks source
    WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id AND source.rating=3
  )
  OR NEW.rating_4_count IS NOT (
    SELECT COUNT(*) FROM request_feedbacks source
    WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id AND source.rating=4
  )
  OR NEW.rating_5_count IS NOT (
    SELECT COUNT(*) FROM request_feedbacks source
    WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id AND source.rating=5
  )
  OR NEW.accepted_count IS NOT (
    SELECT COALESCE(SUM(accepted), 0) FROM request_feedbacks source
    WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id
  )
  OR NEW.feedback_statistics_version IS NOT NEW.feedback_count
  OR NEW.updated_at IS NOT (
    SELECT MAX(created_at) FROM request_feedbacks source
    WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id
  )
BEGIN
  SELECT RAISE(ABORT, 'model_feedback_statistics_invalid');
END;

-- Aggregate 是 Feedback 的可重建镜像，但运行期不得直接删除造成 ModelQuality 漏计。
CREATE TRIGGER trg_model_feedback_statistics_delete_prd049
BEFORE DELETE ON model_feedback_statistics
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'model_feedback_statistics_immutable');
END;

-- 每插入一条 Feedback，先更新已有模型聚合；全部值都从同事务原始事实重新计算。
CREATE TRIGGER trg_request_feedback_aggregate_insert_prd049
AFTER INSERT ON request_feedbacks
FOR EACH ROW
BEGIN
  -- 已存在模型聚合按当前 tenant/model 的全部不可变 Feedback 重新计算，避免增量算术漂移。
  UPDATE model_feedback_statistics
  SET
    feedback_count=(
      SELECT COUNT(*) FROM request_feedbacks source
      WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id
    ),
    rating_sum=(
      SELECT COALESCE(SUM(rating), 0) FROM request_feedbacks source
      WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id
    ),
    rating_1_count=(
      SELECT COUNT(*) FROM request_feedbacks source
      WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id AND source.rating=1
    ),
    rating_2_count=(
      SELECT COUNT(*) FROM request_feedbacks source
      WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id AND source.rating=2
    ),
    rating_3_count=(
      SELECT COUNT(*) FROM request_feedbacks source
      WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id AND source.rating=3
    ),
    rating_4_count=(
      SELECT COUNT(*) FROM request_feedbacks source
      WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id AND source.rating=4
    ),
    rating_5_count=(
      SELECT COUNT(*) FROM request_feedbacks source
      WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id AND source.rating=5
    ),
    accepted_count=(
      SELECT COALESCE(SUM(accepted), 0) FROM request_feedbacks source
      WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id
    ),
    feedback_statistics_version=(
      SELECT COUNT(*) FROM request_feedbacks source
      WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id
    ),
    updated_at=(
      SELECT MAX(created_at) FROM request_feedbacks source
      WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id
    )
  WHERE tenant_id=NEW.tenant_id AND model_id=NEW.model_id;

  -- 当前模型尚无聚合时插入首行；existing 行已由上方 UPDATE 处理，避免 REPLACE 绕过删除保护。
  INSERT INTO model_feedback_statistics(
    tenant_id, model_id, feedback_count, rating_sum,
    rating_1_count, rating_2_count, rating_3_count, rating_4_count, rating_5_count,
    accepted_count, feedback_statistics_version, updated_at
  )
  SELECT
    NEW.tenant_id,
    NEW.model_id,
    COUNT(*),
    COALESCE(SUM(rating), 0),
    COUNT(CASE WHEN rating=1 THEN 1 END),
    COUNT(CASE WHEN rating=2 THEN 1 END),
    COUNT(CASE WHEN rating=3 THEN 1 END),
    COUNT(CASE WHEN rating=4 THEN 1 END),
    COUNT(CASE WHEN rating=5 THEN 1 END),
    COALESCE(SUM(accepted), 0),
    COUNT(*),
    MAX(created_at)
  FROM request_feedbacks source
  WHERE source.tenant_id=NEW.tenant_id AND source.model_id=NEW.model_id
  HAVING NOT EXISTS (
    SELECT 1 FROM model_feedback_statistics current
    WHERE current.tenant_id=NEW.tenant_id AND current.model_id=NEW.model_id
  );
END;

-- Event graph 必须与 Feedback 的 tenant、对象、actor、链路和创建时间逐字段一致。
CREATE TRIGGER trg_request_feedback_event_graph_insert_prd049
BEFORE INSERT ON request_feedback_events
FOR EACH ROW
WHEN NOT EXISTS (
  SELECT 1 FROM request_feedbacks feedback
  WHERE feedback.tenant_id=NEW.tenant_id
    AND feedback.request_feedback_id=NEW.request_feedback_id
    AND feedback.feedback_event_id=NEW.feedback_event_id
    AND feedback.source_request_id=NEW.source_request_id
    AND feedback.actor_type=NEW.actor_type
    AND (
      (feedback.actor_type='session_user' AND feedback.actor_user_id=NEW.actor_id)
      OR
      (feedback.actor_type='virtual_key' AND feedback.actor_virtual_key_id=NEW.actor_id)
    )
    AND feedback.operation_request_id=NEW.operation_request_id
    AND feedback.trace_id=NEW.trace_id
    AND feedback.created_at=NEW.created_at
)
BEGIN
  SELECT RAISE(ABORT, 'request_feedback_event_graph_invalid');
END;

-- Event payload 只允许七个安全字段，并与 Feedback 评分、采纳和规范化标签精确绑定。
CREATE TRIGGER trg_request_feedback_event_payload_insert_prd049
BEFORE INSERT ON request_feedback_events
FOR EACH ROW
WHEN
  (SELECT COUNT(*) FROM json_each(NEW.payload_json)) != 7
  OR EXISTS (
    SELECT 1 FROM json_each(NEW.payload_json) item
    WHERE item.key NOT IN (
      'request_feedback_id', 'request_id', 'model_id', 'actor_type',
      'rating', 'accepted', 'tags'
    )
  )
  OR json_type(NEW.payload_json, '$.request_feedback_id')!='text'
  OR json_type(NEW.payload_json, '$.request_id')!='text'
  OR json_type(NEW.payload_json, '$.model_id')!='text'
  OR json_type(NEW.payload_json, '$.actor_type')!='text'
  OR json_type(NEW.payload_json, '$.rating')!='integer'
  OR json_type(NEW.payload_json, '$.accepted') NOT IN ('true', 'false')
  OR json_type(NEW.payload_json, '$.tags')!='array'
  OR NOT EXISTS (
    SELECT 1 FROM request_feedbacks feedback
    WHERE feedback.tenant_id=NEW.tenant_id
      AND feedback.request_feedback_id=NEW.request_feedback_id
      AND json_extract(NEW.payload_json, '$.request_feedback_id')=feedback.request_feedback_id
      AND json_extract(NEW.payload_json, '$.request_id')=feedback.source_request_id
      AND json_extract(NEW.payload_json, '$.model_id')=feedback.model_id
      AND json_extract(NEW.payload_json, '$.actor_type')=feedback.actor_type
      AND json_extract(NEW.payload_json, '$.rating')=feedback.rating
      AND json_extract(NEW.payload_json, '$.accepted')=feedback.accepted
      AND json(json_extract(NEW.payload_json, '$.tags'))=json(feedback.tags_json)
  )
BEGIN
  SELECT RAISE(ABORT, 'request_feedback_event_payload_invalid');
END;

-- Feedback 是不可变评价，不提供 PATCH/DELETE，也不允许内部 SQL 覆盖历史评分。
CREATE TRIGGER trg_request_feedback_immutable_update_prd049
BEFORE UPDATE ON request_feedbacks
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'request_feedback_immutable');
END;
CREATE TRIGGER trg_request_feedback_immutable_delete_prd049
BEFORE DELETE ON request_feedbacks
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'request_feedback_immutable');
END;

-- 持久事件同样不可更新或删除，重派只能复用原 event ID/trace/time。
CREATE TRIGGER trg_request_feedback_event_immutable_update_prd049
BEFORE UPDATE ON request_feedback_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'request_feedback_event_immutable');
END;
CREATE TRIGGER trg_request_feedback_event_immutable_delete_prd049
BEFORE DELETE ON request_feedback_events
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'request_feedback_event_immutable');
END;

-- 已形成 Feedback 后锁定 RequestLog 的租户、actor 归属、终态、路由快照和流状态字段。
CREATE TRIGGER trg_request_log_feedback_parent_update_prd049
BEFORE UPDATE OF
  tenant_id, request_id, virtual_key_id, status, model_id, provider_id,
  project_id, application_id, environment_id, stream_session_id, stream_status,
  final_routing_attempt_id, routing_attempt_count
ON request_logs
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM request_feedbacks feedback
  WHERE feedback.tenant_id=OLD.tenant_id AND feedback.source_request_id=OLD.request_id
)
AND (
  NEW.tenant_id IS NOT OLD.tenant_id
  OR NEW.request_id IS NOT OLD.request_id
  OR NEW.virtual_key_id IS NOT OLD.virtual_key_id
  OR NEW.status IS NOT OLD.status
  OR NEW.model_id IS NOT OLD.model_id
  OR NEW.provider_id IS NOT OLD.provider_id
  OR NEW.project_id IS NOT OLD.project_id
  OR NEW.application_id IS NOT OLD.application_id
  OR NEW.environment_id IS NOT OLD.environment_id
  OR NEW.stream_session_id IS NOT OLD.stream_session_id
  OR NEW.stream_status IS NOT OLD.stream_status
  OR NEW.final_routing_attempt_id IS NOT OLD.final_routing_attempt_id
  OR NEW.routing_attempt_count IS NOT OLD.routing_attempt_count
)
BEGIN
  SELECT RAISE(ABORT, 'request_log_feedback_parent_immutable');
END;
CREATE TRIGGER trg_request_log_feedback_parent_delete_prd049
BEFORE DELETE ON request_logs
FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM request_feedbacks feedback
  WHERE feedback.tenant_id=OLD.tenant_id AND feedback.source_request_id=OLD.request_id
)
BEGIN
  SELECT RAISE(ABORT, 'request_log_feedback_parent_immutable');
END;

-- 仅锁定被 Feedback 源 RequestLog 指定为 final 的 Attempt 归因字段，Usage/延迟结算仍可推进。
CREATE TRIGGER trg_routing_attempt_feedback_parent_update_prd049
BEFORE UPDATE OF routing_attempt_id, tenant_id, request_id, status, model_id, provider_id
ON routing_attempts
FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM request_feedbacks feedback
  JOIN request_logs request
    ON request.tenant_id=feedback.tenant_id
   AND request.request_id=feedback.source_request_id
  WHERE request.final_routing_attempt_id=OLD.routing_attempt_id
    AND OLD.tenant_id=request.tenant_id
    AND OLD.request_id=request.request_id
)
AND (
  NEW.routing_attempt_id IS NOT OLD.routing_attempt_id
  OR NEW.tenant_id IS NOT OLD.tenant_id
  OR NEW.request_id IS NOT OLD.request_id
  OR NEW.status IS NOT OLD.status
  OR NEW.model_id IS NOT OLD.model_id
  OR NEW.provider_id IS NOT OLD.provider_id
)
BEGIN
  SELECT RAISE(ABORT, 'routing_attempt_feedback_parent_immutable');
END;
CREATE TRIGGER trg_routing_attempt_feedback_parent_delete_prd049
BEFORE DELETE ON routing_attempts
FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM request_feedbacks feedback
  JOIN request_logs request
    ON request.tenant_id=feedback.tenant_id
   AND request.request_id=feedback.source_request_id
  WHERE request.final_routing_attempt_id=OLD.routing_attempt_id
    AND OLD.tenant_id=request.tenant_id
    AND OLD.request_id=request.request_id
)
BEGIN
  SELECT RAISE(ABORT, 'routing_attempt_feedback_parent_immutable');
END;

-- 最终用 TEMP trigger 把 FK/SQLite 完整性问题转换为固定迁移错误，任一问题都回滚全部 0038 DDL。
CREATE TEMP TABLE prd049_up_integrity_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd049_up_integrity_guard
BEFORE INSERT ON prd049_up_integrity_guard
FOR EACH ROW
WHEN NEW.issue_count != 0
BEGIN
  SELECT RAISE(ABORT, 'prd049_up_integrity_failed');
END;
-- 外键图与 SQLite 内部结构必须同时为零问题，循环 deferred FK 也在这里真实解析。
INSERT INTO prd049_up_integrity_guard(issue_count)
SELECT
  (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TRIGGER trg_prd049_up_integrity_guard;
DROP TABLE prd049_up_integrity_guard;

COMMIT;
