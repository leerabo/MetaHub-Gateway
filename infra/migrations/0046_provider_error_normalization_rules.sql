-- 迁移职责：为 PRD-056 建立 Provider 错误归一化规则、不可变快照历史、Source Outbox 与冻结事实。
-- 安全边界：除两张全局 builtin registry 外，所有业务事实都显式携带 tenant_id；任何表都不保存
-- Provider Key、Authorization、Token、Prompt、Response、Endpoint、Header 或上游原始 message/body。
BEGIN IMMEDIATE;

-- 复合外键必须引用同列顺序的唯一键。三个索引只补足 tenant-qualified owner，不改变既有全局主键。
CREATE UNIQUE INDEX idx_users_tenant_user_owner_prd056
  ON users(tenant_id, user_id);
CREATE UNIQUE INDEX idx_providers_tenant_provider_owner_prd056
  ON providers(tenant_id, provider_id);
CREATE UNIQUE INDEX idx_models_tenant_model_owner_prd056
  ON models(tenant_id, model_id);

-- 两类失败 owner 都增加 tenant-qualified 唯一键，Fact 不能只依赖全局 ID 推断租户。
CREATE UNIQUE INDEX idx_routing_attempts_tenant_owner_prd056
  ON routing_attempts(tenant_id, routing_attempt_id);
CREATE UNIQUE INDEX idx_stream_sessions_tenant_owner_prd056
  ON stream_sessions(tenant_id, stream_session_id);

-- 流式握手失败此前没有冻结上游 HTTP 与 retryable；新增列只保存归一化标量，不保存上游正文。
ALTER TABLE stream_sessions
  ADD COLUMN upstream_http_status INTEGER
  CHECK (upstream_http_status IS NULL OR upstream_http_status BETWEEN 100 AND 599);
-- retryable 与 HTTP 状态分列冻结，流式 owner 不得在查询时按当前规则重新推导重试语义。
ALTER TABLE stream_sessions
  ADD COLUMN retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0,1));

-- builtin catalog 是不可变系统元数据，因此是本 PRD 唯一允许不带 tenant_id 的表族。
CREATE TABLE provider_error_builtin_catalog_versions (
  builtin_catalog_version TEXT PRIMARY KEY CHECK (
    length(builtin_catalog_version) BETWEEN 1 AND 64
    AND builtin_catalog_version GLOB '[a-z0-9]*'
    AND builtin_catalog_version NOT GLOB '*[^a-z0-9._-]*'
  ),
  catalog_hash TEXT NOT NULL CHECK (
    length(catalog_hash)=64 AND catalog_hash NOT GLOB '*[^0-9a-f]*'
  ),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL)
);

CREATE TABLE provider_error_builtin_rule_versions (
  builtin_catalog_version TEXT NOT NULL,
  error_map_id TEXT NOT NULL CHECK (
    (substr(error_map_id,1,6)='guard_' OR substr(error_map_id,1,8)='builtin_')
    AND length(error_map_id) BETWEEN 7 AND 96
    AND error_map_id NOT GLOB '*[^a-z0-9_]*'
  ),
  source TEXT NOT NULL CHECK (source IN ('guard','builtin')),
  ordinal INTEGER NOT NULL CHECK (typeof(ordinal)='integer' AND ordinal BETWEEN 1 AND 1000000),
  pattern_hash TEXT NOT NULL CHECK (
    length(pattern_hash)=64 AND pattern_hash NOT GLOB '*[^0-9a-f]*'
  ),
  output_error_code TEXT NOT NULL CHECK (output_error_code IN (
    'gateway.upstream_auth_failed','gateway.upstream_rate_limited',
    'gateway.upstream_quota_exceeded','gateway.upstream_model_not_found',
    'gateway.upstream_resource_not_found','gateway.upstream_timeout',
    'gateway.upstream_request_invalid','gateway.upstream_unavailable','gateway.upstream_error'
  )),
  rule_metadata_json TEXT NOT NULL CHECK (
    json_valid(rule_metadata_json) AND json_type(rule_metadata_json)='object'
    AND length(CAST(rule_metadata_json AS BLOB))<=4096
  ),
  PRIMARY KEY (builtin_catalog_version, error_map_id),
  UNIQUE (builtin_catalog_version, ordinal),
  UNIQUE (builtin_catalog_version, error_map_id, pattern_hash, output_error_code),
  FOREIGN KEY (builtin_catalog_version)
    REFERENCES provider_error_builtin_catalog_versions(builtin_catalog_version),
  CHECK (
    (source='guard' AND substr(error_map_id,1,6)='guard_')
    OR (source='builtin' AND substr(error_map_id,1,8)='builtin_')
  )
);

-- BEFORE INSERT 冲突门禁必须早于 seed，且专门阻断 INSERT OR REPLACE 对不可变 registry 的改写。
CREATE TRIGGER trg_provider_error_builtin_catalog_conflict_insert_prd056
BEFORE INSERT ON provider_error_builtin_catalog_versions FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM provider_error_builtin_catalog_versions current
  WHERE current.builtin_catalog_version=NEW.builtin_catalog_version
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'provider_error_builtin_catalog_immutable'); END;
CREATE TRIGGER trg_provider_error_builtin_catalog_rowid_insert_prd056
AFTER INSERT ON provider_error_builtin_catalog_versions FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'provider_error_builtin_catalog_immutable'); END;
CREATE TRIGGER trg_provider_error_builtin_catalog_update_prd056
BEFORE UPDATE ON provider_error_builtin_catalog_versions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_builtin_catalog_immutable'); END;
CREATE TRIGGER trg_provider_error_builtin_catalog_delete_prd056
BEFORE DELETE ON provider_error_builtin_catalog_versions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_builtin_catalog_immutable'); END;

CREATE TRIGGER trg_provider_error_builtin_rule_conflict_insert_prd056
BEFORE INSERT ON provider_error_builtin_rule_versions FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM provider_error_builtin_rule_versions current
  WHERE (current.builtin_catalog_version=NEW.builtin_catalog_version
         AND current.error_map_id=NEW.error_map_id)
     OR (current.builtin_catalog_version=NEW.builtin_catalog_version
         AND current.ordinal=NEW.ordinal)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'provider_error_builtin_rule_immutable'); END;
CREATE TRIGGER trg_provider_error_builtin_rule_rowid_insert_prd056
AFTER INSERT ON provider_error_builtin_rule_versions FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'provider_error_builtin_rule_immutable'); END;
CREATE TRIGGER trg_provider_error_builtin_rule_update_prd056
BEFORE UPDATE ON provider_error_builtin_rule_versions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_builtin_rule_immutable'); END;
CREATE TRIGGER trg_provider_error_builtin_rule_delete_prd056
BEFORE DELETE ON provider_error_builtin_rule_versions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_builtin_rule_immutable'); END;

-- 首版 builtin catalog 先登记不可变版本哈希，后续规则行只能引用这条受触发器保护的目录。
INSERT INTO provider_error_builtin_catalog_versions(
  builtin_catalog_version, catalog_hash, created_at
) VALUES(
  'provider-error-builtin-v1',
  'a209e9d5e86e5b08d6fe97a1d0475ad8533704f6f782fa20dd732fc4dafdc507',
  '2026-07-29T00:00:00Z'
);

-- 13 行 metadata 与 Shared canonical registry 逐字节一致；signal 中文文本以 JSON ASCII 转义保存。
INSERT INTO provider_error_builtin_rule_versions(
  builtin_catalog_version,error_map_id,source,ordinal,pattern_hash,output_error_code,rule_metadata_json
) VALUES
('provider-error-builtin-v1','guard_transport_timeout','guard',1,'c15efa5c0b31d8ecf70121837879782e82b4b5fd6c94b54e4db9f87a2446e93d','gateway.upstream_timeout','{"error_map_id":"guard_transport_timeout","ordinal":1,"output_error_code":"gateway.upstream_timeout","transport_kinds":["timeout"]}'),
('provider-error-builtin-v1','guard_transport_unavailable','guard',2,'5574bcf6bb22bcb7965772cef322f9308e266375e9b3dd4a3a421217520b8d02','gateway.upstream_unavailable','{"error_map_id":"guard_transport_unavailable","ordinal":2,"output_error_code":"gateway.upstream_unavailable","transport_kinds":["local_configuration","network","other"]}'),
('provider-error-builtin-v1','guard_http_auth','guard',3,'0fc51f7cdbef87250993cdfd9cfe132331fed351efb0095027d0755d9bd6d72f','gateway.upstream_auth_failed','{"error_map_id":"guard_http_auth","http_statuses":[401,403],"ordinal":3,"output_error_code":"gateway.upstream_auth_failed"}'),
('provider-error-builtin-v1','guard_http_timeout','guard',4,'aab4f9bc0628a3e858558dc55b34d4428f6ab976242f9965be20464a86b3d264','gateway.upstream_timeout','{"error_map_id":"guard_http_timeout","http_statuses":[408,504],"ordinal":4,"output_error_code":"gateway.upstream_timeout"}'),
('provider-error-builtin-v1','guard_http_5xx','guard',5,'b48d775bae1c2b946ea20be6667565ae752a491d5c376f2a3ffbfc48b552c470','gateway.upstream_error','{"error_map_id":"guard_http_5xx","excluded_http_statuses":[504],"http_status_class":"5xx","ordinal":5,"output_error_code":"gateway.upstream_error"}'),
('provider-error-builtin-v1','builtin_quota_signal','builtin',10,'00e514ee453f23a290ff8a07b4caa8f54b62fa2f8fc0ae7ef197d4147a78bc8a','gateway.upstream_quota_exceeded','{"rule":{"error_map_id":"builtin_quota_signal","excluded_http_statuses":[401,403,404,408],"http_status_class":"4xx","ordinal":10,"output_error_code":"gateway.upstream_quota_exceeded","signal":"quota"},"signal_definition":{"field_equals":["balance_not_enough","billing_hard_limit_reached","credit_exhausted","insufficient_balance","insufficient_quota","quota_exceeded"],"message_contains":["balance not enough","credit exhausted","insufficient balance","insufficient quota","\u4f59\u989d\u4e0d\u8db3","\u4f59\u989d\u5df2\u7528\u5c3d","\u989d\u5ea6\u4e0d\u8db3","\u914d\u989d\u4e0d\u8db3"]}}'),
('provider-error-builtin-v1','builtin_model_not_found_signal','builtin',20,'e6e8ba16126e5790d950cdb057bfd3b89977e2824f4a07f9811ad97437867df0','gateway.upstream_model_not_found','{"rule":{"error_map_id":"builtin_model_not_found_signal","http_statuses":[400,404,422],"ordinal":20,"output_error_code":"gateway.upstream_model_not_found","signal":"model_not_found"},"signal_definition":{"field_equals":["invalid_model","model_not_exist","model_not_found","unknown_model"],"message_contains":["model does not exist","model not found","unknown model","\u672a\u627e\u5230\u6a21\u578b","\u6a21\u578b\u4e0d\u5b58\u5728"]}}'),
('provider-error-builtin-v1','builtin_http_402','builtin',30,'50942b9dbda70a6f5d5539061dd0cc91b0b01f615ac8e1943ba2672892073880','gateway.upstream_quota_exceeded','{"error_map_id":"builtin_http_402","http_statuses":[402],"ordinal":30,"output_error_code":"gateway.upstream_quota_exceeded"}'),
('provider-error-builtin-v1','builtin_http_404','builtin',40,'e6f05c0c9938f75fd4b8135acfc7fef2986d2147034545ead7f51fdc9747a142','gateway.upstream_resource_not_found','{"error_map_id":"builtin_http_404","http_statuses":[404],"ordinal":40,"output_error_code":"gateway.upstream_resource_not_found"}'),
('provider-error-builtin-v1','builtin_http_429','builtin',50,'65202bb23bfe8a2a79cb6914c4bf2560252acc1f86e80b9458343aecd0d883ed','gateway.upstream_rate_limited','{"error_map_id":"builtin_http_429","http_statuses":[429],"ordinal":50,"output_error_code":"gateway.upstream_rate_limited"}'),
('provider-error-builtin-v1','builtin_http_400_422','builtin',60,'560747225636514b84b9b7aea2499017ab11c534ddbb4ea22dfbf193eb3bbe73','gateway.upstream_request_invalid','{"error_map_id":"builtin_http_400_422","http_statuses":[400,422],"ordinal":60,"output_error_code":"gateway.upstream_request_invalid"}'),
('provider-error-builtin-v1','builtin_http_other_4xx','builtin',70,'43af6da05d717c6d14eb0fb084405e38262a5029dc66879f35ac247d2f67ce74','gateway.upstream_request_invalid','{"error_map_id":"builtin_http_other_4xx","excluded_http_statuses":[401,402,403,404,408,422,429],"http_status_class":"4xx","ordinal":70,"output_error_code":"gateway.upstream_request_invalid"}'),
('provider-error-builtin-v1','builtin_response_default','builtin',80,'afac5b684210c3df752f7c97eca47c2246866b41dcfe9324e1fea25544e2ae96','gateway.upstream_error','{"error_map_id":"builtin_response_default","ordinal":80,"output_error_code":"gateway.upstream_error","response_default":true}');

-- 稳定资源只保存身份和 current_version；复合外键延迟到事务提交，以支持“version 先写”的冻结顺序。
CREATE TABLE provider_error_maps (
  provider_error_map_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  current_version INTEGER NOT NULL CHECK (typeof(current_version)='integer' AND current_version>=1),
  created_by TEXT NOT NULL,
  updated_by TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK (unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY (provider_error_map_id),
  UNIQUE (tenant_id, provider_error_map_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, created_by) REFERENCES users(tenant_id, user_id),
  FOREIGN KEY (tenant_id, updated_by) REFERENCES users(tenant_id, user_id),
  FOREIGN KEY (tenant_id, provider_error_map_id, current_version)
    REFERENCES provider_error_map_versions(tenant_id, provider_error_map_id, version)
    DEFERRABLE INITIALLY DEFERRED,
  CHECK (
    length(provider_error_map_id)=36 AND substr(provider_error_map_id,1,4)='pem_'
    AND substr(provider_error_map_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  CHECK (unixepoch(updated_at)>=unixepoch(created_at))
);

CREATE TABLE provider_error_map_versions (
  provider_error_map_version_id TEXT PRIMARY KEY CHECK (
    length(provider_error_map_version_id)=37
    AND substr(provider_error_map_version_id,1,5)='pemv_'
    AND substr(provider_error_map_version_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  provider_error_map_id TEXT NOT NULL,
  version INTEGER NOT NULL CHECK (typeof(version)='integer' AND version>=1),
  provider_id TEXT,
  priority INTEGER NOT NULL CHECK (typeof(priority)='integer' AND priority BETWEEN 1 AND 1000),
  http_statuses_json TEXT NOT NULL CHECK (
    json_valid(http_statuses_json) AND json_type(http_statuses_json)='array'
    AND length(CAST(http_statuses_json AS BLOB))<=65
  ),
  http_status_class TEXT CHECK (http_status_class IS NULL OR http_status_class='4xx'),
  error_field TEXT CHECK (
    error_field IS NULL OR error_field IN ('error.code','error.type','code','type')
  ),
  field_operator TEXT CHECK (field_operator IS NULL OR field_operator IN ('equals','contains')),
  field_pattern TEXT CHECK (
    field_pattern IS NULL OR (length(field_pattern) BETWEEN 1 AND 256 AND instr(field_pattern,char(0))=0)
  ),
  message_operator TEXT CHECK (message_operator IS NULL OR message_operator='contains'),
  message_pattern TEXT CHECK (
    message_pattern IS NULL OR (length(message_pattern) BETWEEN 1 AND 256 AND instr(message_pattern,char(0))=0)
  ),
  output_error_code TEXT NOT NULL CHECK (output_error_code IN (
    'gateway.upstream_rate_limited','gateway.upstream_quota_exceeded',
    'gateway.upstream_model_not_found','gateway.upstream_resource_not_found',
    'gateway.upstream_request_invalid'
  )),
  status TEXT NOT NULL CHECK (status IN ('active','disabled')),
  pattern_hash TEXT NOT NULL CHECK (
    length(pattern_hash)=64 AND pattern_hash NOT GLOB '*[^0-9a-f]*'
  ),
  condition_hash TEXT NOT NULL CHECK (
    length(condition_hash)=64 AND condition_hash NOT GLOB '*[^0-9a-f]*'
  ),
  head_revision INTEGER NOT NULL CHECK (
    typeof(head_revision)='integer' AND head_revision BETWEEN 1 AND 2147483647
  ),
  created_by TEXT NOT NULL,
  updated_by TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  UNIQUE (tenant_id, provider_error_map_id, version),
  UNIQUE (tenant_id, provider_error_map_version_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, provider_error_map_id)
    REFERENCES provider_error_maps(tenant_id, provider_error_map_id)
    DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (tenant_id, provider_id) REFERENCES providers(tenant_id, provider_id),
  FOREIGN KEY (tenant_id, created_by) REFERENCES users(tenant_id, user_id),
  FOREIGN KEY (tenant_id, updated_by) REFERENCES users(tenant_id, user_id),
  CHECK ((error_field IS NULL)=(field_operator IS NULL)),
  CHECK ((error_field IS NULL)=(field_pattern IS NULL)),
  CHECK ((message_operator IS NULL)=(message_pattern IS NULL)),
  CHECK (http_status_class IS NULL OR http_statuses_json='[]'),
  CHECK (
    http_statuses_json!='[]' OR http_status_class IS NOT NULL
    OR error_field IS NOT NULL OR message_operator IS NOT NULL
  ),
  CHECK (
    http_statuses_json!='[]' OR http_status_class IS NOT NULL OR error_field IS NOT NULL
    OR provider_id IS NOT NULL
  )
);

CREATE INDEX idx_provider_error_map_versions_tenant_map_prd056
  ON provider_error_map_versions(tenant_id, provider_error_map_id, version DESC);
CREATE INDEX idx_provider_error_map_versions_tenant_provider_prd056
  ON provider_error_map_versions(tenant_id, provider_id, status, priority DESC);

-- JSON1 trigger 逐项证明 canonical、升序、去重和 HTTP/output 矩阵，避免服务层遗漏后保存伪有效规则。
CREATE TRIGGER trg_provider_error_map_versions_validate_insert_prd056
BEFORE INSERT ON provider_error_map_versions FOR EACH ROW
WHEN NOT (
  json_valid(NEW.http_statuses_json)
  AND CASE WHEN json_valid(NEW.http_statuses_json)
    THEN json_type(NEW.http_statuses_json) END='array'
  AND (SELECT COUNT(*) FROM json_each(
    CASE WHEN json_valid(NEW.http_statuses_json) THEN NEW.http_statuses_json ELSE '[]' END
  )) BETWEEN 0 AND 16
  AND NOT EXISTS (
    SELECT 1 FROM json_each(
      CASE WHEN json_valid(NEW.http_statuses_json) THEN NEW.http_statuses_json ELSE '[]' END
    ) item
    WHERE item.type!='integer' OR item.atom NOT BETWEEN 400 AND 499
       OR item.atom IN (401,403,408)
  )
  AND NOT EXISTS (
    SELECT 1 FROM json_each(
      CASE WHEN json_valid(NEW.http_statuses_json) THEN NEW.http_statuses_json ELSE '[]' END
    ) item GROUP BY item.atom HAVING COUNT(*)>1
  )
  AND NEW.http_statuses_json=(
    SELECT CASE WHEN COUNT(*)=0 THEN '[]'
      ELSE '['||group_concat(CAST(atom AS TEXT),',')||']' END
    FROM (
      SELECT atom FROM json_each(
        CASE WHEN json_valid(NEW.http_statuses_json) THEN NEW.http_statuses_json ELSE '[]' END
      ) ORDER BY atom
    )
  )
  AND NOT EXISTS (
    SELECT 1 FROM json_each(
      CASE WHEN json_valid(NEW.http_statuses_json) THEN NEW.http_statuses_json ELSE '[]' END
    ) item
    WHERE CASE
      WHEN item.atom=402 THEN NEW.output_error_code!='gateway.upstream_quota_exceeded'
      WHEN item.atom=404 THEN NEW.output_error_code NOT IN (
        'gateway.upstream_model_not_found','gateway.upstream_resource_not_found'
      )
      WHEN item.atom=429 THEN NEW.output_error_code NOT IN (
        'gateway.upstream_rate_limited','gateway.upstream_quota_exceeded'
      )
      WHEN item.atom IN (400,422) THEN NEW.output_error_code NOT IN (
        'gateway.upstream_request_invalid','gateway.upstream_model_not_found',
        'gateway.upstream_quota_exceeded'
      )
      ELSE NEW.output_error_code NOT IN (
        'gateway.upstream_request_invalid','gateway.upstream_quota_exceeded'
      )
    END
  )
  AND (NEW.http_status_class IS NULL OR NEW.output_error_code IN (
    'gateway.upstream_request_invalid','gateway.upstream_quota_exceeded'
  ))
  AND (NEW.http_statuses_json!='[]' OR NEW.http_status_class IS NOT NULL
       OR NEW.output_error_code IN (
         'gateway.upstream_quota_exceeded','gateway.upstream_model_not_found',
         'gateway.upstream_request_invalid','gateway.upstream_resource_not_found'
       ))
  AND EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.created_by
  )
  AND EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.updated_by
  )
  AND (NEW.provider_id IS NULL OR EXISTS (
    SELECT 1 FROM providers provider
    WHERE provider.tenant_id=NEW.tenant_id AND provider.provider_id=NEW.provider_id
  ))
  AND NEW.head_revision=COALESCE((
    SELECT head.revision FROM provider_error_map_heads head WHERE head.tenant_id=NEW.tenant_id
  ),0)+1
  AND (
    (NEW.version=1 AND NOT EXISTS (
      SELECT 1 FROM provider_error_map_versions prior
      WHERE prior.tenant_id=NEW.tenant_id
        AND prior.provider_error_map_id=NEW.provider_error_map_id
    ))
    OR
    (NEW.version>1 AND EXISTS (
      SELECT 1 FROM provider_error_maps current
      WHERE current.tenant_id=NEW.tenant_id
        AND current.provider_error_map_id=NEW.provider_error_map_id
        AND current.current_version=NEW.version-1
        AND current.created_by=NEW.created_by
    ))
  )
)
BEGIN SELECT RAISE(ABORT,'provider_error_map_version_invalid'); END;

CREATE TRIGGER trg_provider_error_map_versions_conflict_insert_prd056
BEFORE INSERT ON provider_error_map_versions FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM provider_error_map_versions current
  WHERE current.provider_error_map_version_id=NEW.provider_error_map_version_id
     OR (current.tenant_id=NEW.tenant_id
         AND current.provider_error_map_id=NEW.provider_error_map_id
         AND current.version=NEW.version)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'provider_error_map_version_immutable'); END;
CREATE TRIGGER trg_provider_error_map_versions_rowid_insert_prd056
AFTER INSERT ON provider_error_map_versions FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'provider_error_map_version_immutable'); END;
CREATE TRIGGER trg_provider_error_map_versions_update_prd056
BEFORE UPDATE ON provider_error_map_versions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_map_version_immutable'); END;
CREATE TRIGGER trg_provider_error_map_versions_delete_prd056
BEFORE DELETE ON provider_error_map_versions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_map_version_immutable'); END;

-- map 写路径只接受 version 已经完整落库的单步推进；active 冲突在数据库内按 tenant/scope/priority/hash 拒绝。
CREATE TRIGGER trg_provider_error_maps_insert_prd056
BEFORE INSERT ON provider_error_maps FOR EACH ROW
WHEN NOT (
  NEW.current_version=1 AND NEW.created_by=NEW.updated_by AND NEW.created_at=NEW.updated_at
  AND (SELECT COUNT(*) FROM provider_error_maps existing WHERE existing.tenant_id=NEW.tenant_id)<512
  AND EXISTS (
    SELECT 1 FROM provider_error_map_versions version
    WHERE version.tenant_id=NEW.tenant_id
      AND version.provider_error_map_id=NEW.provider_error_map_id
      AND version.version=1
      AND version.created_by=NEW.created_by AND version.updated_by=NEW.updated_by
      AND version.created_at=NEW.created_at
  )
  AND (
    (SELECT status FROM provider_error_map_versions
      WHERE tenant_id=NEW.tenant_id AND provider_error_map_id=NEW.provider_error_map_id AND version=1
    )='disabled'
    OR (
      (SELECT COUNT(*)
       FROM provider_error_maps map
       JOIN provider_error_map_versions current
         ON current.tenant_id=map.tenant_id
        AND current.provider_error_map_id=map.provider_error_map_id
        AND current.version=map.current_version
       WHERE map.tenant_id=NEW.tenant_id AND current.status='active')<128
      AND NOT EXISTS (
        SELECT 1
        FROM provider_error_map_versions candidate
        JOIN provider_error_maps other_map ON other_map.tenant_id=candidate.tenant_id
        JOIN provider_error_map_versions other
          ON other.tenant_id=other_map.tenant_id
         AND other.provider_error_map_id=other_map.provider_error_map_id
         AND other.version=other_map.current_version
        WHERE candidate.tenant_id=NEW.tenant_id
          AND candidate.provider_error_map_id=NEW.provider_error_map_id
          AND candidate.version=1 AND candidate.status='active'
          AND other.status='active'
          AND (other.provider_id IS NULL OR candidate.provider_id IS NULL OR other.provider_id=candidate.provider_id)
          AND other.priority=candidate.priority
          AND other.condition_hash=candidate.condition_hash
      )
    )
  )
)
BEGIN SELECT RAISE(ABORT,'provider_error_map_state_invalid'); END;

CREATE TRIGGER trg_provider_error_maps_update_prd056
BEFORE UPDATE ON provider_error_maps FOR EACH ROW
WHEN NOT (
  NEW.rowid=OLD.rowid
  AND NEW.provider_error_map_id=OLD.provider_error_map_id
  AND NEW.tenant_id=OLD.tenant_id
  AND NEW.created_by=OLD.created_by AND NEW.created_at=OLD.created_at
  AND NEW.current_version=OLD.current_version+1
  AND unixepoch(NEW.updated_at)>unixepoch(OLD.updated_at)
  AND EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.updated_by
  )
  AND EXISTS (
    SELECT 1 FROM provider_error_map_versions version
    WHERE version.tenant_id=NEW.tenant_id
      AND version.provider_error_map_id=NEW.provider_error_map_id
      AND version.version=NEW.current_version
      AND version.created_by=NEW.created_by AND version.updated_by=NEW.updated_by
      AND version.created_at=NEW.updated_at
  )
  AND (
    (SELECT status FROM provider_error_map_versions
      WHERE tenant_id=NEW.tenant_id AND provider_error_map_id=NEW.provider_error_map_id
        AND version=NEW.current_version)='disabled'
    OR (
      (SELECT COUNT(*)
       FROM provider_error_maps map
       JOIN provider_error_map_versions current
         ON current.tenant_id=map.tenant_id
        AND current.provider_error_map_id=map.provider_error_map_id
        AND current.version=map.current_version
       WHERE map.tenant_id=NEW.tenant_id AND current.status='active'
         AND map.provider_error_map_id!=NEW.provider_error_map_id)<128
      AND NOT EXISTS (
        SELECT 1
        FROM provider_error_map_versions candidate
        JOIN provider_error_maps other_map
          ON other_map.tenant_id=candidate.tenant_id
         AND other_map.provider_error_map_id!=candidate.provider_error_map_id
        JOIN provider_error_map_versions other
          ON other.tenant_id=other_map.tenant_id
         AND other.provider_error_map_id=other_map.provider_error_map_id
         AND other.version=other_map.current_version
        WHERE candidate.tenant_id=NEW.tenant_id
          AND candidate.provider_error_map_id=NEW.provider_error_map_id
          AND candidate.version=NEW.current_version AND candidate.status='active'
          AND other.status='active'
          AND (other.provider_id IS NULL OR candidate.provider_id IS NULL OR other.provider_id=candidate.provider_id)
          AND other.priority=candidate.priority
          AND other.condition_hash=candidate.condition_hash
      )
    )
  )
)
BEGIN SELECT RAISE(ABORT,'provider_error_map_state_invalid'); END;

CREATE TRIGGER trg_provider_error_maps_conflict_insert_prd056
BEFORE INSERT ON provider_error_maps FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM provider_error_maps current
  WHERE current.provider_error_map_id=NEW.provider_error_map_id
     OR (current.tenant_id=NEW.tenant_id AND current.provider_error_map_id=NEW.provider_error_map_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'provider_error_map_immutable'); END;
CREATE TRIGGER trg_provider_error_maps_rowid_insert_prd056
AFTER INSERT ON provider_error_maps FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'provider_error_map_immutable'); END;
CREATE TRIGGER trg_provider_error_maps_delete_prd056
BEFORE DELETE ON provider_error_maps FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_map_immutable'); END;

-- 每个 head 版本保存 active 规则引用；pattern 永不进入 refs，历史可供迟到 v1/LKG Fact 验证。
CREATE TABLE provider_error_map_head_versions (
  provider_error_map_head_version_id TEXT PRIMARY KEY CHECK (
    length(provider_error_map_head_version_id)=38
    AND substr(provider_error_map_head_version_id,1,6)='pemhv_'
    AND substr(provider_error_map_head_version_id,7) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK (
    typeof(revision)='integer' AND revision BETWEEN 1 AND 2147483647
  ),
  content_hash TEXT NOT NULL CHECK (
    length(content_hash)=64 AND content_hash NOT GLOB '*[^0-9a-f]*'
  ),
  builtin_catalog_version TEXT NOT NULL,
  active_rule_refs_json TEXT NOT NULL CHECK (
    json_valid(active_rule_refs_json) AND json_type(active_rule_refs_json)='array'
    AND length(CAST(active_rule_refs_json AS BLOB))<=65536
  ),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  UNIQUE (tenant_id, revision, content_hash, builtin_catalog_version),
  UNIQUE (tenant_id, provider_error_map_head_version_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (builtin_catalog_version)
    REFERENCES provider_error_builtin_catalog_versions(builtin_catalog_version)
);

CREATE INDEX idx_provider_error_map_head_versions_tenant_revision_prd056
  ON provider_error_map_head_versions(tenant_id, revision DESC, builtin_catalog_version);

CREATE TRIGGER trg_provider_error_map_head_versions_validate_insert_prd056
BEFORE INSERT ON provider_error_map_head_versions FOR EACH ROW
WHEN NOT (
  json_valid(NEW.active_rule_refs_json)
  AND CASE WHEN json_valid(NEW.active_rule_refs_json)
    THEN json_type(NEW.active_rule_refs_json) END='array'
  AND json_array_length(CASE WHEN json_valid(NEW.active_rule_refs_json)
    THEN NEW.active_rule_refs_json ELSE '[]' END)<=128
  AND NOT EXISTS (
    SELECT 1
    FROM json_each(CASE WHEN json_valid(NEW.active_rule_refs_json)
      THEN NEW.active_rule_refs_json ELSE '[]' END) ref
    WHERE ref.type!='object'
       OR (SELECT COUNT(*) FROM json_each(ref.value))!=2
       OR (SELECT COUNT(DISTINCT key) FROM json_each(ref.value))!=2
       OR EXISTS (SELECT 1 FROM json_each(ref.value) field
                  WHERE field.key NOT IN ('error_map_id','version'))
       OR json_type(ref.value,'$.error_map_id')!='text'
       OR json_type(ref.value,'$.version')!='integer'
       OR length(json_extract(ref.value,'$.error_map_id'))!=36
       OR substr(json_extract(ref.value,'$.error_map_id'),1,4)!='pem_'
       OR substr(json_extract(ref.value,'$.error_map_id'),5) GLOB '*[^0-9a-f]*'
       OR json_extract(ref.value,'$.version')<1
       OR NOT EXISTS (
         SELECT 1 FROM provider_error_map_versions version
         WHERE version.tenant_id=NEW.tenant_id
           AND version.provider_error_map_id=json_extract(ref.value,'$.error_map_id')
           AND version.version=json_extract(ref.value,'$.version')
           AND version.status='active'
       )
  )
  AND NOT EXISTS (
    SELECT 1
    FROM json_each(CASE WHEN json_valid(NEW.active_rule_refs_json)
      THEN NEW.active_rule_refs_json ELSE '[]' END) left_ref
    JOIN json_each(CASE WHEN json_valid(NEW.active_rule_refs_json)
      THEN NEW.active_rule_refs_json ELSE '[]' END) right_ref
      ON CAST(left_ref.key AS INTEGER)<CAST(right_ref.key AS INTEGER)
    WHERE json_extract(left_ref.value,'$.error_map_id')
          >=json_extract(right_ref.value,'$.error_map_id')
  )
  AND NEW.active_rule_refs_json=(
    SELECT CASE WHEN COUNT(*)=0 THEN '[]'
      ELSE '['||group_concat(
        '{"error_map_id":'||json_quote(error_map_id)||',"version":'||CAST(version AS TEXT)||'}',','
      )||']' END
    FROM (
      SELECT json_extract(value,'$.error_map_id') AS error_map_id,
             json_extract(value,'$.version') AS version
      FROM json_each(CASE WHEN json_valid(NEW.active_rule_refs_json)
        THEN NEW.active_rule_refs_json ELSE '[]' END)
      ORDER BY CAST(key AS INTEGER)
    )
  )
  AND (
    EXISTS (
      SELECT 1 FROM provider_error_map_head_versions prior
      WHERE prior.tenant_id=NEW.tenant_id AND prior.revision=NEW.revision
        AND prior.content_hash=NEW.content_hash
    )
    OR NEW.revision=COALESCE((
      SELECT head.revision FROM provider_error_map_heads head WHERE head.tenant_id=NEW.tenant_id
    ),0)+1
  )
)
BEGIN SELECT RAISE(ABORT,'provider_error_map_head_version_invalid'); END;

CREATE TRIGGER trg_provider_error_map_head_versions_conflict_insert_prd056
BEFORE INSERT ON provider_error_map_head_versions FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM provider_error_map_head_versions current
  WHERE current.provider_error_map_head_version_id=NEW.provider_error_map_head_version_id
     OR (current.tenant_id=NEW.tenant_id AND current.revision=NEW.revision
         AND current.content_hash=NEW.content_hash
         AND current.builtin_catalog_version=NEW.builtin_catalog_version)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'provider_error_map_head_version_immutable'); END;
CREATE TRIGGER trg_provider_error_map_head_versions_rowid_insert_prd056
AFTER INSERT ON provider_error_map_head_versions FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'provider_error_map_head_version_immutable'); END;
CREATE TRIGGER trg_provider_error_map_head_versions_update_prd056
BEFORE UPDATE ON provider_error_map_head_versions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_map_head_version_immutable'); END;
CREATE TRIGGER trg_provider_error_map_head_versions_delete_prd056
BEFORE DELETE ON provider_error_map_head_versions FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_map_head_version_immutable'); END;

CREATE TABLE provider_error_map_heads (
  tenant_id TEXT PRIMARY KEY,
  revision INTEGER NOT NULL CHECK (
    typeof(revision)='integer' AND revision BETWEEN 1 AND 2147483647
  ),
  content_hash TEXT NOT NULL CHECK (
    length(content_hash)=64 AND content_hash NOT GLOB '*[^0-9a-f]*'
  ),
  updated_at TEXT NOT NULL CHECK (unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);

-- current head 只有在 active map 集合与某条 immutable history 精确一致时才能写入。
CREATE TRIGGER trg_provider_error_map_heads_insert_prd056
BEFORE INSERT ON provider_error_map_heads FOR EACH ROW
WHEN NOT (
  NEW.revision=1
  AND EXISTS (
    SELECT 1 FROM provider_error_map_head_versions history
    WHERE history.tenant_id=NEW.tenant_id AND history.revision=NEW.revision
      AND history.content_hash=NEW.content_hash AND history.created_at=NEW.updated_at
      AND json_array_length(history.active_rule_refs_json)=(
        SELECT COUNT(*) FROM provider_error_maps map
        JOIN provider_error_map_versions version
          ON version.tenant_id=map.tenant_id
         AND version.provider_error_map_id=map.provider_error_map_id
         AND version.version=map.current_version
        WHERE map.tenant_id=NEW.tenant_id AND version.status='active'
      )
      AND NOT EXISTS (
        SELECT 1 FROM provider_error_maps map
        JOIN provider_error_map_versions version
          ON version.tenant_id=map.tenant_id
         AND version.provider_error_map_id=map.provider_error_map_id
         AND version.version=map.current_version
        WHERE map.tenant_id=NEW.tenant_id AND version.status='active'
          AND NOT EXISTS (
            SELECT 1 FROM json_each(history.active_rule_refs_json) ref
            WHERE json_extract(ref.value,'$.error_map_id')=map.provider_error_map_id
              AND json_extract(ref.value,'$.version')=map.current_version
          )
      )
  )
)
BEGIN SELECT RAISE(ABORT,'provider_error_map_head_invalid'); END;

CREATE TRIGGER trg_provider_error_map_heads_update_prd056
BEFORE UPDATE ON provider_error_map_heads FOR EACH ROW
WHEN NOT (
  NEW.rowid=OLD.rowid AND NEW.tenant_id=OLD.tenant_id
  AND NEW.revision=OLD.revision+1 AND NEW.revision<=2147483647
  AND unixepoch(NEW.updated_at)>unixepoch(OLD.updated_at)
  AND EXISTS (
    SELECT 1 FROM provider_error_map_head_versions history
    WHERE history.tenant_id=NEW.tenant_id AND history.revision=NEW.revision
      AND history.content_hash=NEW.content_hash AND history.created_at=NEW.updated_at
      AND json_array_length(history.active_rule_refs_json)=(
        SELECT COUNT(*) FROM provider_error_maps map
        JOIN provider_error_map_versions version
          ON version.tenant_id=map.tenant_id
         AND version.provider_error_map_id=map.provider_error_map_id
         AND version.version=map.current_version
        WHERE map.tenant_id=NEW.tenant_id AND version.status='active'
      )
      AND NOT EXISTS (
        SELECT 1 FROM provider_error_maps map
        JOIN provider_error_map_versions version
          ON version.tenant_id=map.tenant_id
         AND version.provider_error_map_id=map.provider_error_map_id
         AND version.version=map.current_version
        WHERE map.tenant_id=NEW.tenant_id AND version.status='active'
          AND NOT EXISTS (
            SELECT 1 FROM json_each(history.active_rule_refs_json) ref
            WHERE json_extract(ref.value,'$.error_map_id')=map.provider_error_map_id
              AND json_extract(ref.value,'$.version')=map.current_version
          )
      )
  )
)
BEGIN SELECT RAISE(ABORT,'provider_error_map_head_invalid'); END;

CREATE TRIGGER trg_provider_error_map_heads_conflict_insert_prd056
BEFORE INSERT ON provider_error_map_heads FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM provider_error_map_heads current
  WHERE current.tenant_id=NEW.tenant_id OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'provider_error_map_head_immutable'); END;
CREATE TRIGGER trg_provider_error_map_heads_rowid_insert_prd056
AFTER INSERT ON provider_error_map_heads FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'provider_error_map_head_immutable'); END;
CREATE TRIGGER trg_provider_error_map_heads_delete_prd056
BEFORE DELETE ON provider_error_map_heads FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_map_head_immutable'); END;
-- 不可变业务事件同时充当 error_map.updated Source Outbox；payload 只允许固定安全标量。
CREATE TABLE provider_error_map_events (
  provider_error_map_event_id TEXT PRIMARY KEY CHECK (
    length(provider_error_map_event_id)=39
    AND substr(provider_error_map_event_id,1,7)='pemevt_'
    AND substr(provider_error_map_event_id,8) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  provider_error_map_id TEXT NOT NULL,
  operation TEXT NOT NULL CHECK (operation IN ('create','update','enable','disable')),
  rule_version INTEGER NOT NULL CHECK (typeof(rule_version)='integer' AND rule_version>=1),
  provider_error_map_version_id TEXT NOT NULL,
  provider_error_map_head_version_id TEXT NOT NULL,
  head_revision INTEGER NOT NULL CHECK (
    typeof(head_revision)='integer' AND head_revision BETWEEN 1 AND 2147483647
  ),
  overlay_content_hash TEXT NOT NULL CHECK (
    length(overlay_content_hash)=64 AND overlay_content_hash NOT GLOB '*[^0-9a-f]*'
  ),
  builtin_catalog_version TEXT NOT NULL,
  pattern_hash TEXT NOT NULL CHECK (
    length(pattern_hash)=64 AND pattern_hash NOT GLOB '*[^0-9a-f]*'
  ),
  status TEXT NOT NULL CHECK (status IN ('active','disabled')),
  provider_id TEXT,
  actor_user_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK (
    length(request_id) BETWEEN 1 AND 128 AND request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  trace_id TEXT NOT NULL CHECK (
    length(trace_id) BETWEEN 1 AND 128 AND trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  request_fingerprint TEXT NOT NULL CHECK (
    length(request_fingerprint)=64 AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  payload_json TEXT NOT NULL CHECK (
    json_valid(payload_json) AND json_type(payload_json)='object'
    AND length(CAST(payload_json AS BLOB))<=4096
  ),
  dispatch_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (dispatch_status IN ('pending','processing','dispatched','failed')),
  dispatch_attempts INTEGER NOT NULL DEFAULT 0
    CHECK (typeof(dispatch_attempts)='integer' AND dispatch_attempts BETWEEN 0 AND 10),
  next_attempt_at TEXT,
  dispatched_at TEXT,
  claim_token TEXT,
  lease_owner_id TEXT,
  lease_expires_at TEXT,
  last_error_code TEXT CHECK (
    last_error_code IS NULL OR last_error_code IN (
      'webhook.event_type_invalid','webhook.payload_invalid','webhook.payload_sensitive',
      'webhook.delivery_unavailable','error_map.event_unavailable'
    )
  ),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK (unixepoch(updated_at) IS NOT NULL),
  UNIQUE (tenant_id, actor_user_id, request_id),
  UNIQUE (tenant_id, provider_error_map_event_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, actor_user_id) REFERENCES users(tenant_id, user_id),
  FOREIGN KEY (tenant_id, provider_error_map_id)
    REFERENCES provider_error_maps(tenant_id, provider_error_map_id),
  FOREIGN KEY (tenant_id, provider_error_map_version_id)
    REFERENCES provider_error_map_versions(tenant_id, provider_error_map_version_id),
  FOREIGN KEY (tenant_id, provider_error_map_id, rule_version)
    REFERENCES provider_error_map_versions(tenant_id, provider_error_map_id, version),
  FOREIGN KEY (tenant_id, provider_error_map_head_version_id)
    REFERENCES provider_error_map_head_versions(tenant_id, provider_error_map_head_version_id),
  FOREIGN KEY (tenant_id, provider_id) REFERENCES providers(tenant_id, provider_id),
  FOREIGN KEY (builtin_catalog_version)
    REFERENCES provider_error_builtin_catalog_versions(builtin_catalog_version),
  CHECK (
    (dispatch_status='pending' AND dispatch_attempts BETWEEN 0 AND 9
      AND next_attempt_at IS NOT NULL AND unixepoch(next_attempt_at) IS NOT NULL
      AND dispatched_at IS NULL AND claim_token IS NULL
      AND lease_owner_id IS NULL AND lease_expires_at IS NULL)
    OR (dispatch_status='processing' AND dispatch_attempts BETWEEN 1 AND 10
      AND next_attempt_at IS NULL AND dispatched_at IS NULL
      AND claim_token IS NOT NULL AND length(claim_token)=32
      AND claim_token NOT GLOB '*[^0-9a-f]*'
      AND lease_owner_id IS NOT NULL AND length(lease_owner_id) BETWEEN 1 AND 128
      AND lease_owner_id NOT GLOB '*[^A-Za-z0-9._:-]*'
      AND lease_expires_at IS NOT NULL AND unixepoch(lease_expires_at) IS NOT NULL)
    OR (dispatch_status='dispatched' AND dispatch_attempts BETWEEN 1 AND 10
      AND next_attempt_at IS NULL AND dispatched_at IS NOT NULL
      AND unixepoch(dispatched_at) IS NOT NULL AND claim_token IS NULL
      AND lease_owner_id IS NULL AND lease_expires_at IS NULL AND last_error_code IS NULL)
    OR (dispatch_status='failed' AND dispatch_attempts=10
      AND next_attempt_at IS NULL AND dispatched_at IS NULL AND claim_token IS NULL
      AND lease_owner_id IS NULL AND lease_expires_at IS NULL AND last_error_code IS NOT NULL)
  )
);

CREATE INDEX idx_provider_error_map_events_tenant_map_prd056
  ON provider_error_map_events(tenant_id, provider_error_map_id, created_at DESC);
CREATE INDEX idx_provider_error_map_events_pending_prd056
  ON provider_error_map_events(dispatch_status, next_attempt_at, lease_expires_at, created_at)
  WHERE dispatch_status IN ('pending','processing');

CREATE TRIGGER trg_provider_error_map_events_validate_insert_prd056
BEFORE INSERT ON provider_error_map_events FOR EACH ROW
WHEN NOT (
  NEW.dispatch_status='pending' AND NEW.dispatch_attempts=0
  AND NEW.next_attempt_at=NEW.created_at AND NEW.updated_at=NEW.created_at
  AND NEW.dispatched_at IS NULL AND NEW.claim_token IS NULL
  AND NEW.lease_owner_id IS NULL AND NEW.lease_expires_at IS NULL
  AND NEW.last_error_code IS NULL
  AND EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.actor_user_id
  )
  AND EXISTS (
    SELECT 1
    FROM provider_error_maps map
    JOIN provider_error_map_versions version
      ON version.tenant_id=map.tenant_id
     AND version.provider_error_map_id=map.provider_error_map_id
     AND version.version=map.current_version
    WHERE map.tenant_id=NEW.tenant_id
      AND map.provider_error_map_id=NEW.provider_error_map_id
      AND map.current_version=NEW.rule_version
      AND version.provider_error_map_version_id=NEW.provider_error_map_version_id
      AND version.pattern_hash=NEW.pattern_hash
      AND version.status=NEW.status
      AND version.provider_id IS NEW.provider_id
      AND version.head_revision=NEW.head_revision
      AND version.updated_by=NEW.actor_user_id
  )
  AND EXISTS (
    SELECT 1 FROM provider_error_map_head_versions history
    WHERE history.tenant_id=NEW.tenant_id
      AND history.provider_error_map_head_version_id=NEW.provider_error_map_head_version_id
      AND history.revision=NEW.head_revision
      AND history.content_hash=NEW.overlay_content_hash
      AND history.builtin_catalog_version=NEW.builtin_catalog_version
      AND (EXISTS (
        SELECT 1 FROM json_each(history.active_rule_refs_json) ref
        WHERE json_extract(ref.value,'$.error_map_id')=NEW.provider_error_map_id
          AND json_extract(ref.value,'$.version')=NEW.rule_version
      ))=CASE WHEN NEW.status='active' THEN 1 ELSE 0 END
  )
  AND (
    (NEW.operation='create' AND NEW.rule_version=1)
    OR
    (NEW.rule_version>1 AND EXISTS (
      SELECT 1 FROM provider_error_map_versions prior
      WHERE prior.tenant_id=NEW.tenant_id
        AND prior.provider_error_map_id=NEW.provider_error_map_id
        AND prior.version=NEW.rule_version-1
        AND (
          (NEW.operation='enable' AND prior.status='disabled' AND NEW.status='active')
          OR (NEW.operation='disable' AND prior.status='active' AND NEW.status='disabled')
          OR (NEW.operation='update' AND prior.status=NEW.status)
        )
    ))
  )
  AND json_valid(NEW.payload_json)
  AND CASE WHEN json_valid(NEW.payload_json) THEN json_type(NEW.payload_json) END='object'
  AND (SELECT COUNT(*) FROM json_each(
    CASE WHEN json_valid(NEW.payload_json) THEN NEW.payload_json ELSE '{}' END
  ))=11
  AND (SELECT COUNT(DISTINCT key) FROM json_each(
    CASE WHEN json_valid(NEW.payload_json) THEN NEW.payload_json ELSE '{}' END
  ))=11
  AND NOT EXISTS (
    SELECT 1 FROM json_each(
      CASE WHEN json_valid(NEW.payload_json) THEN NEW.payload_json ELSE '{}' END
    ) field
    WHERE field.key NOT IN (
      'schema_version','operation','provider_error_map_id','rule_version','head_revision',
      'provider_scope','status','priority','output_error_code','pattern_hash','request_id'
    ) OR field.type NOT IN ('text','integer')
  )
  AND json_type(NEW.payload_json,'$.schema_version')='integer'
  AND json_extract(NEW.payload_json,'$.schema_version')=1
  AND json_extract(NEW.payload_json,'$.operation')=NEW.operation
  AND json_extract(NEW.payload_json,'$.provider_error_map_id')=NEW.provider_error_map_id
  AND json_extract(NEW.payload_json,'$.rule_version')=NEW.rule_version
  AND json_extract(NEW.payload_json,'$.head_revision')=NEW.head_revision
  AND json_extract(NEW.payload_json,'$.provider_scope')=COALESCE(NEW.provider_id,'all')
  AND json_extract(NEW.payload_json,'$.status')=NEW.status
  AND json_extract(NEW.payload_json,'$.priority')=(
    SELECT priority FROM provider_error_map_versions
    WHERE tenant_id=NEW.tenant_id AND provider_error_map_version_id=NEW.provider_error_map_version_id
  )
  AND json_extract(NEW.payload_json,'$.output_error_code')=(
    SELECT output_error_code FROM provider_error_map_versions
    WHERE tenant_id=NEW.tenant_id AND provider_error_map_version_id=NEW.provider_error_map_version_id
  )
  AND json_extract(NEW.payload_json,'$.pattern_hash')=NEW.pattern_hash
  AND json_extract(NEW.payload_json,'$.request_id')=NEW.request_id
)
BEGIN SELECT RAISE(ABORT,'provider_error_map_event_invalid'); END;
-- Outbox 仅允许固定 claim/接管/finish 状态机；所有业务载荷与 owner 标量永远不可改。
CREATE TRIGGER trg_provider_error_map_events_state_update_prd056
BEFORE UPDATE ON provider_error_map_events FOR EACH ROW
WHEN NOT (
  (
    NEW.rowid=OLD.rowid
    AND NEW.provider_error_map_event_id=OLD.provider_error_map_event_id
    AND NEW.tenant_id=OLD.tenant_id
    AND NEW.provider_error_map_id=OLD.provider_error_map_id
    AND NEW.operation=OLD.operation AND NEW.rule_version=OLD.rule_version
    AND NEW.provider_error_map_version_id=OLD.provider_error_map_version_id
    AND NEW.provider_error_map_head_version_id=OLD.provider_error_map_head_version_id
    AND NEW.head_revision=OLD.head_revision
    AND NEW.overlay_content_hash=OLD.overlay_content_hash
    AND NEW.builtin_catalog_version=OLD.builtin_catalog_version
    AND NEW.pattern_hash=OLD.pattern_hash AND NEW.status=OLD.status
    AND NEW.provider_id IS OLD.provider_id AND NEW.actor_user_id=OLD.actor_user_id
    AND NEW.request_id=OLD.request_id AND NEW.trace_id=OLD.trace_id
    AND NEW.request_fingerprint=OLD.request_fingerprint
    AND NEW.payload_json=OLD.payload_json AND NEW.created_at=OLD.created_at
    AND unixepoch(NEW.updated_at)>=unixepoch(OLD.updated_at)
  )
  AND (
    (
      (
        OLD.dispatch_status='pending'
        AND unixepoch(OLD.next_attempt_at)<=unixepoch(NEW.updated_at)
      )
      OR (
        OLD.dispatch_status='processing' AND OLD.dispatch_attempts BETWEEN 1 AND 9
        AND unixepoch(OLD.lease_expires_at)<=unixepoch(NEW.updated_at)
      )
    )
    AND NEW.dispatch_status='processing'
    AND NEW.dispatch_attempts=OLD.dispatch_attempts+1
    AND NEW.next_attempt_at IS NULL AND NEW.dispatched_at IS NULL
    AND NEW.claim_token IS NOT NULL AND length(NEW.claim_token)=32
    AND NEW.claim_token NOT GLOB '*[^0-9a-f]*'
    AND NEW.lease_owner_id IS NOT NULL AND length(NEW.lease_owner_id) BETWEEN 1 AND 128
    AND NEW.lease_owner_id NOT GLOB '*[^A-Za-z0-9._:-]*'
    AND unixepoch(NEW.lease_expires_at)=unixepoch(NEW.updated_at)+30
    AND NEW.last_error_code IS NULL
  )
  OR (
    (
      NEW.rowid=OLD.rowid
      AND NEW.provider_error_map_event_id=OLD.provider_error_map_event_id
      AND NEW.tenant_id=OLD.tenant_id
      AND NEW.provider_error_map_id=OLD.provider_error_map_id
      AND NEW.operation=OLD.operation AND NEW.rule_version=OLD.rule_version
      AND NEW.provider_error_map_version_id=OLD.provider_error_map_version_id
      AND NEW.provider_error_map_head_version_id=OLD.provider_error_map_head_version_id
      AND NEW.head_revision=OLD.head_revision
      AND NEW.overlay_content_hash=OLD.overlay_content_hash
      AND NEW.builtin_catalog_version=OLD.builtin_catalog_version
      AND NEW.pattern_hash=OLD.pattern_hash AND NEW.status=OLD.status
      AND NEW.provider_id IS OLD.provider_id AND NEW.actor_user_id=OLD.actor_user_id
      AND NEW.request_id=OLD.request_id AND NEW.trace_id=OLD.trace_id
      AND NEW.request_fingerprint=OLD.request_fingerprint
      AND NEW.payload_json=OLD.payload_json AND NEW.created_at=OLD.created_at
      AND unixepoch(NEW.updated_at)>=unixepoch(OLD.updated_at)
    )
    AND OLD.dispatch_status='processing'
    AND NEW.dispatch_attempts=OLD.dispatch_attempts
    AND NEW.claim_token IS NULL AND NEW.lease_owner_id IS NULL AND NEW.lease_expires_at IS NULL
    AND (
      (
        unixepoch(OLD.lease_expires_at)>unixepoch(NEW.updated_at)
        AND (
          (NEW.dispatch_status='dispatched' AND NEW.dispatched_at=NEW.updated_at
            AND NEW.next_attempt_at IS NULL AND NEW.last_error_code IS NULL)
          OR
          (OLD.dispatch_attempts<10 AND NEW.dispatch_status='pending'
            AND NEW.dispatched_at IS NULL AND NEW.last_error_code IS NOT NULL
            AND unixepoch(NEW.next_attempt_at)=unixepoch(NEW.updated_at)+CASE
              WHEN OLD.dispatch_attempts=1 THEN 5
              WHEN OLD.dispatch_attempts=2 THEN 30
              ELSE 300 END)
          OR
          (OLD.dispatch_attempts=10 AND NEW.dispatch_status='failed'
            AND NEW.next_attempt_at IS NULL AND NEW.dispatched_at IS NULL
            AND NEW.last_error_code IS NOT NULL)
        )
      )
      OR
      (OLD.dispatch_attempts=10
        AND unixepoch(OLD.lease_expires_at)<=unixepoch(NEW.updated_at)
        AND NEW.dispatch_status='failed'
        AND NEW.next_attempt_at IS NULL AND NEW.dispatched_at IS NULL
        AND NEW.last_error_code IS NOT NULL)
    )
  )
)
BEGIN SELECT RAISE(ABORT,'provider_error_map_event_state_invalid'); END;

CREATE TRIGGER trg_provider_error_map_events_conflict_insert_prd056
BEFORE INSERT ON provider_error_map_events FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM provider_error_map_events current
  WHERE current.provider_error_map_event_id=NEW.provider_error_map_event_id
     OR (current.tenant_id=NEW.tenant_id AND current.actor_user_id=NEW.actor_user_id
         AND current.request_id=NEW.request_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'provider_error_map_event_immutable'); END;
CREATE TRIGGER trg_provider_error_map_events_rowid_insert_prd056
AFTER INSERT ON provider_error_map_events FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'provider_error_map_event_immutable'); END;
CREATE TRIGGER trg_provider_error_map_events_delete_prd056
BEFORE DELETE ON provider_error_map_events FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_map_event_immutable'); END;
-- 所有通过权限、wire 与 fingerprint 门禁的 mutation 都写入不可变幂等账本。
CREATE TABLE provider_error_map_operations (
  provider_error_map_operation_id TEXT PRIMARY KEY CHECK (
    length(provider_error_map_operation_id)=37
    AND substr(provider_error_map_operation_id,1,5)='pemo_'
    AND substr(provider_error_map_operation_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  actor_user_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK (
    length(request_id) BETWEEN 1 AND 128 AND request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  request_fingerprint TEXT NOT NULL CHECK (
    length(request_fingerprint)=64 AND request_fingerprint NOT GLOB '*[^0-9a-f]*'
  ),
  operation TEXT NOT NULL CHECK (operation IN ('create','update','enable','disable')),
  target_provider_error_map_id TEXT,
  outcome TEXT NOT NULL CHECK (outcome IN ('success','noop','business_error')),
  http_status INTEGER NOT NULL CHECK (http_status IN (200,201,404,409,422)),
  error_code TEXT CHECK (
    error_code IS NULL OR error_code IN (
      'error_map.not_found','error_map.input_invalid','error_map.update_empty',
      'error_map.output_code_invalid','error_map.pattern_invalid','error_map.pattern_sensitive',
      'error_map.conflict','error_map.version_conflict','error_map.request_conflict',
      'error_map.limit_exceeded','error_map.unavailable','error_map.event_unavailable'
    )
  ),
  changed INTEGER NOT NULL CHECK (changed IN (0,1)),
  result_version INTEGER CHECK (result_version IS NULL OR result_version>=1),
  result_head_revision INTEGER CHECK (
    result_head_revision IS NULL OR result_head_revision BETWEEN 1 AND 2147483647
  ),
  source_event_id TEXT,
  response_snapshot_json TEXT NOT NULL CHECK (
    json_valid(response_snapshot_json) AND json_type(response_snapshot_json)='object'
    AND length(CAST(response_snapshot_json AS BLOB))<=16384
  ),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  UNIQUE (tenant_id, actor_user_id, request_id),
  UNIQUE (tenant_id, provider_error_map_operation_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, actor_user_id) REFERENCES users(tenant_id, user_id),
  FOREIGN KEY (tenant_id, target_provider_error_map_id)
    REFERENCES provider_error_maps(tenant_id, provider_error_map_id)
    DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (tenant_id, source_event_id)
    REFERENCES provider_error_map_events(tenant_id, provider_error_map_event_id),
  CHECK (
    (outcome='success' AND changed=1 AND http_status IN (200,201)
      AND error_code IS NULL AND target_provider_error_map_id IS NOT NULL
      AND result_version IS NOT NULL AND result_head_revision IS NOT NULL
      AND source_event_id IS NOT NULL)
    OR (outcome='noop' AND changed=0 AND http_status=200 AND error_code IS NULL
      AND target_provider_error_map_id IS NOT NULL
      AND result_version IS NOT NULL AND result_head_revision IS NOT NULL
      AND source_event_id IS NULL)
    OR (outcome='business_error' AND changed=0 AND http_status IN (404,409,422)
      AND error_code IS NOT NULL AND result_version IS NULL
      AND result_head_revision IS NULL AND source_event_id IS NULL)
  )
);

CREATE INDEX idx_provider_error_map_operations_tenant_time_prd056
  ON provider_error_map_operations(tenant_id, actor_user_id, created_at DESC);

CREATE TRIGGER trg_provider_error_map_operations_validate_insert_prd056
BEFORE INSERT ON provider_error_map_operations FOR EACH ROW
WHEN NOT (
  EXISTS (
    SELECT 1 FROM users actor
    WHERE actor.tenant_id=NEW.tenant_id AND actor.user_id=NEW.actor_user_id
  )
  AND (
    NEW.outcome='business_error'
    OR EXISTS (
      SELECT 1
      FROM provider_error_maps map
      JOIN provider_error_map_versions version
        ON version.tenant_id=map.tenant_id
       AND version.provider_error_map_id=map.provider_error_map_id
       AND version.version=map.current_version
      JOIN provider_error_map_heads head ON head.tenant_id=map.tenant_id
      WHERE map.tenant_id=NEW.tenant_id
        AND map.provider_error_map_id=NEW.target_provider_error_map_id
        AND map.current_version=NEW.result_version
        AND head.revision=NEW.result_head_revision
    )
  )
  AND (
    NEW.outcome!='success'
    OR EXISTS (
      SELECT 1 FROM provider_error_map_events event
      WHERE event.tenant_id=NEW.tenant_id
        AND event.provider_error_map_event_id=NEW.source_event_id
        AND event.actor_user_id=NEW.actor_user_id
        AND event.request_id=NEW.request_id
        AND event.request_fingerprint=NEW.request_fingerprint
        AND event.operation=NEW.operation
        AND event.provider_error_map_id=NEW.target_provider_error_map_id
        AND event.rule_version=NEW.result_version
        AND event.head_revision=NEW.result_head_revision
    )
  )
)
BEGIN SELECT RAISE(ABORT,'provider_error_map_operation_invalid'); END;

CREATE TRIGGER trg_provider_error_map_operations_conflict_insert_prd056
BEFORE INSERT ON provider_error_map_operations FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM provider_error_map_operations current
  WHERE current.provider_error_map_operation_id=NEW.provider_error_map_operation_id
     OR (current.tenant_id=NEW.tenant_id AND current.actor_user_id=NEW.actor_user_id
         AND current.request_id=NEW.request_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'provider_error_map_operation_immutable'); END;
CREATE TRIGGER trg_provider_error_map_operations_rowid_insert_prd056
AFTER INSERT ON provider_error_map_operations FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'provider_error_map_operation_immutable'); END;
CREATE TRIGGER trg_provider_error_map_operations_update_prd056
BEFORE UPDATE ON provider_error_map_operations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_map_operation_immutable'); END;
CREATE TRIGGER trg_provider_error_map_operations_delete_prd056
BEFORE DELETE ON provider_error_map_operations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_map_operation_immutable'); END;

-- 冻结事实只保存脱敏投影和规则身份；routing_attempt/stream_session 必须精确二选一。
CREATE TABLE provider_error_facts (
  provider_error_fact_id TEXT PRIMARY KEY CHECK (
    length(provider_error_fact_id)=36 AND substr(provider_error_fact_id,1,4)='pef_'
    AND substr(provider_error_fact_id,5) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK (
    length(request_id) BETWEEN 1 AND 128 AND request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  trace_id TEXT NOT NULL CHECK (
    length(trace_id) BETWEEN 1 AND 128 AND trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  routing_attempt_id TEXT,
  stream_session_id TEXT,
  provider_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  provider_code TEXT NOT NULL CHECK (
    length(provider_code) BETWEEN 1 AND 128 AND instr(provider_code,char(0))=0
  ),
  upstream_http_status INTEGER CHECK (
    upstream_http_status IS NULL OR upstream_http_status BETWEEN 100 AND 599
  ),
  transport_kind TEXT NOT NULL CHECK (
    transport_kind IN ('none','local_configuration','network','timeout','other')
  ),
  provider_error_code_class TEXT CHECK (
    provider_error_code_class IS NULL OR provider_error_code_class IN (
      'auth_error','rate_limit_error','quota_error','model_not_found_error',
      'resource_not_found_error','timeout_error','invalid_request_error',
      'server_error','[REDACTED]'
    )
  ),
  provider_error_type_class TEXT CHECK (
    provider_error_type_class IS NULL OR provider_error_type_class IN (
      'auth_error','rate_limit_error','quota_error','model_not_found_error',
      'resource_not_found_error','timeout_error','invalid_request_error',
      'server_error','[REDACTED]'
    )
  ),
  provider_error_code_present INTEGER NOT NULL CHECK (provider_error_code_present IN (0,1)),
  provider_error_type_present INTEGER NOT NULL CHECK (provider_error_type_present IN (0,1)),
  message_present INTEGER NOT NULL CHECK (message_present IN (0,1)),
  redacted_projection_json TEXT NOT NULL CHECK (
    json_valid(redacted_projection_json) AND json_type(redacted_projection_json)='object'
    AND length(CAST(redacted_projection_json AS BLOB))<=2048
  ),
  projection_hash TEXT NOT NULL CHECK (
    length(projection_hash)=64 AND projection_hash NOT GLOB '*[^0-9a-f]*'
  ),
  body_length_bucket TEXT NOT NULL CHECK (body_length_bucket IN (
    'unknown','0','1_255','256_1023','1024_4095','4096_16383','16384_65535','over_65535'
  )),
  body_truncated INTEGER NOT NULL CHECK (body_truncated IN (0,1)),
  is_json INTEGER NOT NULL CHECK (is_json IN (0,1)),
  matched_error_map_id TEXT NOT NULL CHECK (
    length(matched_error_map_id) BETWEEN 7 AND 96
    AND matched_error_map_id NOT GLOB '*[^a-z0-9_]*'
  ),
  matched_rule_version INTEGER CHECK (
    matched_rule_version IS NULL OR (typeof(matched_rule_version)='integer' AND matched_rule_version>=1)
  ),
  matched_pattern_hash TEXT NOT NULL CHECK (
    length(matched_pattern_hash)=64 AND matched_pattern_hash NOT GLOB '*[^0-9a-f]*'
  ),
  matched_rule_source TEXT NOT NULL CHECK (matched_rule_source IN ('guard','tenant','builtin')),
  error_map_revision INTEGER NOT NULL CHECK (
    typeof(error_map_revision)='integer' AND error_map_revision BETWEEN 0 AND 2147483647
  ),
  overlay_content_hash TEXT NOT NULL CHECK (
    length(overlay_content_hash)=64 AND overlay_content_hash NOT GLOB '*[^0-9a-f]*'
  ),
  builtin_catalog_version TEXT NOT NULL,
  error_map_snapshot_source TEXT NOT NULL CHECK (
    error_map_snapshot_source IN ('fresh','lkg','builtin_fallback')
  ),
  match_basis TEXT NOT NULL CHECK (match_basis IN (
    'transport_timeout','transport_error','http_status','provider_error_code',
    'provider_error_type','message','compound','default'
  )),
  normalized_output_error_code TEXT NOT NULL CHECK (normalized_output_error_code IN (
    'gateway.upstream_auth_failed','gateway.upstream_rate_limited',
    'gateway.upstream_quota_exceeded','gateway.upstream_model_not_found',
    'gateway.upstream_resource_not_found','gateway.upstream_timeout',
    'gateway.upstream_request_invalid','gateway.upstream_unavailable','gateway.upstream_error'
  )),
  owner_terminal_error_code TEXT NOT NULL CHECK (owner_terminal_error_code IN (
    'gateway.upstream_auth_failed','gateway.upstream_rate_limited',
    'gateway.upstream_quota_exceeded','gateway.upstream_model_not_found',
    'gateway.upstream_resource_not_found','gateway.upstream_timeout',
    'gateway.upstream_request_invalid','gateway.upstream_unavailable','gateway.upstream_error',
    'gateway.upstream_transport_failed','gateway.upstream_stream_failed'
  )),
  created_at TEXT NOT NULL CHECK (unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, provider_id) REFERENCES providers(tenant_id, provider_id),
  FOREIGN KEY (tenant_id, model_id) REFERENCES models(tenant_id, model_id),
  FOREIGN KEY (tenant_id, routing_attempt_id)
    REFERENCES routing_attempts(tenant_id, routing_attempt_id),
  FOREIGN KEY (tenant_id, stream_session_id)
    REFERENCES stream_sessions(tenant_id, stream_session_id),
  FOREIGN KEY (builtin_catalog_version)
    REFERENCES provider_error_builtin_catalog_versions(builtin_catalog_version),
  CHECK ((routing_attempt_id IS NULL)<>(stream_session_id IS NULL)),
  CHECK (
    (provider_error_code_present=0 AND provider_error_code_class IS NULL)
    OR (provider_error_code_present=1 AND provider_error_code_class IS NOT NULL)
  ),
  CHECK (
    (provider_error_type_present=0 AND provider_error_type_class IS NULL)
    OR (provider_error_type_present=1 AND provider_error_type_class IS NOT NULL)
  ),
  CHECK (
    (transport_kind='none' AND upstream_http_status IS NOT NULL)
    OR (transport_kind!='none' AND upstream_http_status IS NULL)
  ),
  CHECK ((body_length_bucket='unknown')=body_truncated OR body_length_bucket!='unknown'),
  CHECK (body_length_bucket!='0' OR body_truncated=0),
  CHECK (
    (matched_rule_source='tenant'
      AND length(matched_error_map_id)=36 AND substr(matched_error_map_id,1,4)='pem_'
      AND substr(matched_error_map_id,5) NOT GLOB '*[^0-9a-f]*'
      AND matched_rule_version IS NOT NULL)
    OR (matched_rule_source='guard' AND substr(matched_error_map_id,1,6)='guard_'
      AND matched_rule_version IS NULL)
    OR (matched_rule_source='builtin' AND substr(matched_error_map_id,1,8)='builtin_'
      AND matched_rule_version IS NULL)
  )
);

CREATE UNIQUE INDEX idx_provider_error_facts_attempt_prd056
  ON provider_error_facts(tenant_id, routing_attempt_id)
  WHERE routing_attempt_id IS NOT NULL;
CREATE UNIQUE INDEX idx_provider_error_facts_stream_prd056
  ON provider_error_facts(tenant_id, stream_session_id)
  WHERE stream_session_id IS NOT NULL;
CREATE INDEX idx_provider_error_facts_request_prd056
  ON provider_error_facts(tenant_id, request_id, created_at DESC);
CREATE INDEX idx_provider_error_facts_rule_prd056
  ON provider_error_facts(tenant_id, matched_rule_source, matched_error_map_id, created_at DESC);

-- 插入门禁同时证明 owner、目录/租户版本、revision 分支和脱敏投影 exact schema。
CREATE TRIGGER trg_provider_error_facts_validate_insert_prd056
BEFORE INSERT ON provider_error_facts FOR EACH ROW
WHEN NOT (
  json_valid(NEW.redacted_projection_json)
  AND CASE WHEN json_valid(NEW.redacted_projection_json)
    THEN json_type(NEW.redacted_projection_json) END='object'
  AND (SELECT COUNT(*) FROM json_each(
    CASE WHEN json_valid(NEW.redacted_projection_json)
      THEN NEW.redacted_projection_json ELSE '{}' END
  ))=9
  AND (SELECT COUNT(DISTINCT key) FROM json_each(
    CASE WHEN json_valid(NEW.redacted_projection_json)
      THEN NEW.redacted_projection_json ELSE '{}' END
  ))=9
  AND NOT EXISTS (
    SELECT 1 FROM json_each(
      CASE WHEN json_valid(NEW.redacted_projection_json)
        THEN NEW.redacted_projection_json ELSE '{}' END
    ) field
    WHERE field.key NOT IN (
      'body_length_bucket','body_truncated','is_json','message','message_present',
      'provider_error_code_class','provider_error_code_present',
      'provider_error_type_class','provider_error_type_present'
    )
  )
  AND json_extract(NEW.redacted_projection_json,'$.body_length_bucket')=NEW.body_length_bucket
  AND json_extract(NEW.redacted_projection_json,'$.body_truncated')=NEW.body_truncated
  AND json_extract(NEW.redacted_projection_json,'$.is_json')=NEW.is_json
  AND json_extract(NEW.redacted_projection_json,'$.message_present')=NEW.message_present
  AND (
    (NEW.message_present=0 AND json_type(NEW.redacted_projection_json,'$.message')='null')
    OR (NEW.message_present=1
      AND json_extract(NEW.redacted_projection_json,'$.message')='[REDACTED]')
  )
  AND json_extract(NEW.redacted_projection_json,'$.provider_error_code_class')
      IS NEW.provider_error_code_class
  AND json_extract(NEW.redacted_projection_json,'$.provider_error_type_class')
      IS NEW.provider_error_type_class
  AND json_extract(NEW.redacted_projection_json,'$.provider_error_code_present')
      =NEW.provider_error_code_present
  AND json_extract(NEW.redacted_projection_json,'$.provider_error_type_present')
      =NEW.provider_error_type_present
  AND (
    (NEW.routing_attempt_id IS NOT NULL AND EXISTS (
      SELECT 1 FROM routing_attempts owner
      WHERE owner.tenant_id=NEW.tenant_id
        AND owner.routing_attempt_id=NEW.routing_attempt_id
        AND owner.request_id=NEW.request_id AND owner.trace_id=NEW.trace_id
        AND owner.provider_id=NEW.provider_id AND owner.model_id=NEW.model_id
        AND owner.provider_code=NEW.provider_code AND owner.status='failed'
        AND owner.error_code=NEW.owner_terminal_error_code
        AND owner.upstream_http_status IS NEW.upstream_http_status
        AND owner.retryable=CASE WHEN NEW.normalized_output_error_code IN (
          'gateway.upstream_rate_limited','gateway.upstream_timeout',
          'gateway.upstream_unavailable','gateway.upstream_error'
        ) THEN 1 ELSE 0 END
    ))
    OR
    (NEW.stream_session_id IS NOT NULL AND EXISTS (
      SELECT 1 FROM stream_sessions owner
      WHERE owner.tenant_id=NEW.tenant_id
        AND owner.stream_session_id=NEW.stream_session_id
        AND owner.request_id=NEW.request_id AND owner.trace_id=NEW.trace_id
        AND owner.provider_id=NEW.provider_id AND owner.model_id=NEW.model_id
        AND owner.provider_code=NEW.provider_code AND owner.status='upstream_failed'
        AND owner.error_code=NEW.owner_terminal_error_code
        AND owner.upstream_http_status IS NEW.upstream_http_status
        AND owner.retryable=CASE WHEN NEW.normalized_output_error_code IN (
          'gateway.upstream_rate_limited','gateway.upstream_timeout',
          'gateway.upstream_unavailable','gateway.upstream_error'
        ) THEN 1 ELSE 0 END
    ))
  )
  AND NEW.owner_terminal_error_code=CASE
    WHEN NEW.stream_session_id IS NOT NULL AND (
      NEW.normalized_output_error_code='gateway.upstream_timeout'
      OR (NEW.upstream_http_status=429
        AND NEW.normalized_output_error_code='gateway.upstream_rate_limited')
      OR NEW.upstream_http_status BETWEEN 500 AND 599
      OR NEW.transport_kind IN ('network','other')
    ) THEN 'gateway.upstream_stream_failed'
    WHEN NEW.routing_attempt_id IS NOT NULL
      AND NEW.normalized_output_error_code='gateway.upstream_unavailable'
      AND NEW.transport_kind IN ('network','other')
      THEN 'gateway.upstream_transport_failed'
    ELSE NEW.normalized_output_error_code END
  AND (
    (NEW.error_map_revision=0
      AND NEW.overlay_content_hash='994a863c694e05021d65d0f8b862f24a28da461080286b7b35d1695ee0775fdc'
      AND NEW.matched_rule_source IN ('guard','builtin'))
    OR
    (NEW.error_map_revision>0
      AND NEW.error_map_snapshot_source!='builtin_fallback'
      AND EXISTS (
        SELECT 1 FROM provider_error_map_head_versions history
        WHERE history.tenant_id=NEW.tenant_id
          AND history.revision=NEW.error_map_revision
          AND history.content_hash=NEW.overlay_content_hash
          AND history.builtin_catalog_version=NEW.builtin_catalog_version
          AND (
            NEW.matched_rule_source!='tenant'
            OR EXISTS (
              SELECT 1 FROM json_each(history.active_rule_refs_json) ref
              WHERE json_extract(ref.value,'$.error_map_id')=NEW.matched_error_map_id
                AND json_extract(ref.value,'$.version')=NEW.matched_rule_version
            )
          )
      ))
  )
  AND (
    (NEW.matched_rule_source IN ('guard','builtin') AND EXISTS (
      SELECT 1 FROM provider_error_builtin_rule_versions builtin
      WHERE builtin.builtin_catalog_version=NEW.builtin_catalog_version
        AND builtin.error_map_id=NEW.matched_error_map_id
        AND builtin.source=NEW.matched_rule_source
        AND builtin.pattern_hash=NEW.matched_pattern_hash
        AND builtin.output_error_code=NEW.normalized_output_error_code
    ))
    OR
    (NEW.matched_rule_source='tenant' AND NEW.error_map_revision>0
      AND EXISTS (
        SELECT 1 FROM provider_error_map_versions version
        WHERE version.tenant_id=NEW.tenant_id
          AND version.provider_error_map_id=NEW.matched_error_map_id
          AND version.version=NEW.matched_rule_version
          AND version.status='active'
          AND version.pattern_hash=NEW.matched_pattern_hash
          AND version.output_error_code=NEW.normalized_output_error_code
          AND (version.provider_id IS NULL OR version.provider_id=NEW.provider_id)
      ))
  )
)
BEGIN SELECT RAISE(ABORT,'provider_error_fact_invalid'); END;

CREATE TRIGGER trg_provider_error_facts_conflict_insert_prd056
BEFORE INSERT ON provider_error_facts FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM provider_error_facts current
  WHERE current.provider_error_fact_id=NEW.provider_error_fact_id
     OR (NEW.routing_attempt_id IS NOT NULL
         AND current.tenant_id=NEW.tenant_id
         AND current.routing_attempt_id=NEW.routing_attempt_id)
     OR (NEW.stream_session_id IS NOT NULL
         AND current.tenant_id=NEW.tenant_id
         AND current.stream_session_id=NEW.stream_session_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'provider_error_fact_immutable'); END;
CREATE TRIGGER trg_provider_error_facts_rowid_insert_prd056
AFTER INSERT ON provider_error_facts FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'provider_error_fact_immutable'); END;
CREATE TRIGGER trg_provider_error_facts_update_prd056
BEFORE UPDATE ON provider_error_facts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_fact_immutable'); END;
CREATE TRIGGER trg_provider_error_facts_delete_prd056
BEFORE DELETE ON provider_error_facts FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_error_fact_immutable'); END;

-- Fact 一旦存在，完整 RoutingAttempt timeline 只能执行逐列全同值 UPDATE；rowid 和 DELETE 同样冻结。
CREATE TRIGGER trg_routing_attempt_provider_error_fact_update_prd056
BEFORE UPDATE ON routing_attempts FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM provider_error_facts fact
  WHERE fact.tenant_id=OLD.tenant_id AND fact.routing_attempt_id=OLD.routing_attempt_id
)
AND (
  NEW.rowid IS NOT OLD.rowid
  OR NEW.routing_attempt_id IS NOT OLD.routing_attempt_id
  OR NEW.tenant_id IS NOT OLD.tenant_id OR NEW.request_id IS NOT OLD.request_id
  OR NEW.trace_id IS NOT OLD.trace_id OR NEW.model_alias_id IS NOT OLD.model_alias_id
  OR NEW.fallback_policy_id IS NOT OLD.fallback_policy_id
  OR NEW.fallback_policy_version IS NOT OLD.fallback_policy_version
  OR NEW.attempt_sequence IS NOT OLD.attempt_sequence OR NEW.attempt_role IS NOT OLD.attempt_role
  OR NEW.trigger_type IS NOT OLD.trigger_type OR NEW.trigger_error_code IS NOT OLD.trigger_error_code
  OR NEW.model_id IS NOT OLD.model_id OR NEW.internal_model_id IS NOT OLD.internal_model_id
  OR NEW.provider_id IS NOT OLD.provider_id OR NEW.provider_code IS NOT OLD.provider_code
  OR NEW.status IS NOT OLD.status OR NEW.error_code IS NOT OLD.error_code
  OR NEW.error_category IS NOT OLD.error_category
  OR NEW.upstream_http_status IS NOT OLD.upstream_http_status
  OR NEW.retryable IS NOT OLD.retryable OR NEW.latency_ms IS NOT OLD.latency_ms
  OR NEW.usage_status IS NOT OLD.usage_status OR NEW.usage_ledger_id IS NOT OLD.usage_ledger_id
  OR NEW.estimated_cost_cny IS NOT OLD.estimated_cost_cny
  OR NEW.created_at IS NOT OLD.created_at OR NEW.completed_at IS NOT OLD.completed_at
  OR NEW.updated_at IS NOT OLD.updated_at
)
BEGIN SELECT RAISE(ABORT,'routing_attempt_provider_error_fact_immutable'); END;

CREATE TRIGGER trg_routing_attempt_provider_error_fact_delete_prd056
BEFORE DELETE ON routing_attempts FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM provider_error_facts fact
  WHERE fact.tenant_id=OLD.tenant_id AND fact.routing_attempt_id=OLD.routing_attempt_id
)
BEGIN SELECT RAISE(ABORT,'routing_attempt_provider_error_fact_immutable'); END;

CREATE TRIGGER trg_routing_attempt_provider_error_fact_conflict_insert_prd056
BEFORE INSERT ON routing_attempts FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM routing_attempts current
  JOIN provider_error_facts fact
    ON fact.tenant_id=current.tenant_id AND fact.routing_attempt_id=current.routing_attempt_id
  WHERE current.routing_attempt_id=NEW.routing_attempt_id
     OR (current.tenant_id=NEW.tenant_id AND current.request_id=NEW.request_id
         AND current.attempt_sequence=NEW.attempt_sequence)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'routing_attempt_provider_error_fact_immutable'); END;

-- StreamSession 反向冻结包含迁移新增列以及全部既有身份、计数、时间和终态列。
CREATE TRIGGER trg_stream_session_provider_error_fact_update_prd056
BEFORE UPDATE ON stream_sessions FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM provider_error_facts fact
  WHERE fact.tenant_id=OLD.tenant_id AND fact.stream_session_id=OLD.stream_session_id
)
AND (
  NEW.rowid IS NOT OLD.rowid
  OR NEW.stream_session_id IS NOT OLD.stream_session_id
  OR NEW.tenant_id IS NOT OLD.tenant_id OR NEW.request_id IS NOT OLD.request_id
  OR NEW.trace_id IS NOT OLD.trace_id OR NEW.virtual_key_id IS NOT OLD.virtual_key_id
  OR NEW.environment_id IS NOT OLD.environment_id
  OR NEW.environment_code IS NOT OLD.environment_code
  OR NEW.project_id IS NOT OLD.project_id OR NEW.application_id IS NOT OLD.application_id
  OR NEW.model_id IS NOT OLD.model_id OR NEW.provider_id IS NOT OLD.provider_id
  OR NEW.provider_code IS NOT OLD.provider_code OR NEW.status IS NOT OLD.status
  OR NEW.inter_token_timeout_ms IS NOT OLD.inter_token_timeout_ms
  OR NEW.chunk_count IS NOT OLD.chunk_count OR NEW.bytes_sent IS NOT OLD.bytes_sent
  OR NEW.first_chunk_at IS NOT OLD.first_chunk_at OR NEW.last_chunk_at IS NOT OLD.last_chunk_at
  OR NEW.error_code IS NOT OLD.error_code OR NEW.started_at IS NOT OLD.started_at
  OR NEW.completed_at IS NOT OLD.completed_at OR NEW.created_at IS NOT OLD.created_at
  OR NEW.updated_at IS NOT OLD.updated_at
  OR NEW.upstream_http_status IS NOT OLD.upstream_http_status
  OR NEW.retryable IS NOT OLD.retryable
)
BEGIN SELECT RAISE(ABORT,'stream_session_provider_error_fact_immutable'); END;

CREATE TRIGGER trg_stream_session_provider_error_fact_delete_prd056
BEFORE DELETE ON stream_sessions FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM provider_error_facts fact
  WHERE fact.tenant_id=OLD.tenant_id AND fact.stream_session_id=OLD.stream_session_id
)
BEGIN SELECT RAISE(ABORT,'stream_session_provider_error_fact_immutable'); END;

CREATE TRIGGER trg_stream_session_provider_error_fact_conflict_insert_prd056
BEFORE INSERT ON stream_sessions FOR EACH ROW
WHEN EXISTS (
  SELECT 1 FROM stream_sessions current
  JOIN provider_error_facts fact
    ON fact.tenant_id=current.tenant_id AND fact.stream_session_id=current.stream_session_id
  WHERE current.stream_session_id=NEW.stream_session_id
     OR (current.tenant_id=NEW.tenant_id AND current.request_id=NEW.request_id)
     OR (NEW.rowid!=-1 AND current.rowid=NEW.rowid)
)
BEGIN SELECT RAISE(ABORT,'stream_session_provider_error_fact_immutable'); END;


-- Audit 采用保真重建扩展 action/object CHECK；复制 rowid，保留既有 8 个索引和 5 个触发器。
CREATE TABLE audit_logs_prd056_new (
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
      'model_quality_score.updated',
      'cache_affinity.created', 'cache_affinity.updated',
      'finops.adjustment_created',
      'cost_anomaly.acknowledged', 'cost_anomaly.dismissed',
      'cost_anomaly.temporary_rate_limit_applied',
      'cost_anomaly.temporary_rate_limit_released',
      'error_map.created', 'error_map.updated'
    )
  ),
  object_type TEXT NOT NULL CHECK (
    object_type IN (
      'provider', 'provider_key', 'virtual_key', 'budget_rule', 'dlp_rule',
      'webhook_delivery', 'webhook_endpoint', 'usage_export_job', 'policy',
      'policy_snapshot', 'policy_explanation', 'routing_policy',
      'model_quality_score', 'cache_affinity', 'finops_adjustment', 'cost_anomaly',
      'provider_error_map'
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

-- Audit 重建按 rowid 和全部业务列原样复制，不能在增加对象类型约束时改写既有审计证据。
INSERT INTO audit_logs_prd056_new(
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

CREATE TEMP TABLE prd056_audit_copy_guard (
  source_count INTEGER NOT NULL,
  copied_count INTEGER NOT NULL,
  null_mismatch_count INTEGER NOT NULL,
  row_difference_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd056_audit_copy_guard
BEFORE INSERT ON prd056_audit_copy_guard FOR EACH ROW
WHEN NEW.source_count!=NEW.copied_count
  OR NEW.null_mismatch_count!=0 OR NEW.row_difference_count!=0
BEGIN SELECT RAISE(ABORT,'prd056_audit_copy_failed'); END;
-- 交换表名前核对行数、可空列和双向差集，任一不一致都在同一事务内终止迁移。
INSERT INTO prd056_audit_copy_guard(
  source_count,copied_count,null_mismatch_count,row_difference_count
)
SELECT
  (SELECT COUNT(*) FROM audit_logs),
  (SELECT COUNT(*) FROM audit_logs_prd056_new),
  CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE object_name IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd056_new WHERE object_name IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE client_ip IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd056_new WHERE client_ip IS NULL) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM audit_logs WHERE user_agent IS NULL)
    =(SELECT COUNT(*) FROM audit_logs_prd056_new WHERE user_agent IS NULL) THEN 0 ELSE 1 END,
  (SELECT COUNT(*) FROM (
    SELECT rowid,* FROM audit_logs
    EXCEPT SELECT rowid,* FROM audit_logs_prd056_new
  ))
  + (SELECT COUNT(*) FROM (
    SELECT rowid,* FROM audit_logs_prd056_new
    EXCEPT SELECT rowid,* FROM audit_logs
  ));
DROP TRIGGER trg_prd056_audit_copy_guard;
DROP TABLE prd056_audit_copy_guard;
DROP TABLE audit_logs;
-- 仅在 Audit 保真校验通过后替换正式表名，确保旧证据与新约束原子切换。
ALTER TABLE audit_logs_prd056_new RENAME TO audit_logs;

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
  WHERE action='policy.dry_run_executed';
CREATE UNIQUE INDEX idx_audit_logs_model_quality_request_unique
  ON audit_logs(tenant_id, actor_user_id, request_id)
  WHERE action='model_quality_score.updated';
CREATE UNIQUE INDEX idx_audit_logs_finops_adjustment_unique
  ON audit_logs(tenant_id, object_id)
  WHERE action='finops.adjustment_created';

-- Owner trigger 保留 FinOps/成本异常语义，并把 error-map Audit 复合绑定到同事务 source event。
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
      SELECT 1
      FROM cost_anomaly_actions action
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
      SELECT 1
      FROM provider_error_map_events event
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
)
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;
CREATE TRIGGER trg_audit_logs_prd050_rowid_insert
AFTER INSERT ON audit_logs FOR EACH ROW WHEN NEW.rowid<0
BEGIN SELECT RAISE(ABORT,'audit_log_immutable'); END;

COMMIT;
