-- PRD-063：租户级 Provider Adapter 冻结目录激活事实。
-- 可执行 spec 只存在于 Shared SDK；本表只保存版本身份、追踪与幂等结果，不复制 Endpoint、
-- Header、Provider Key、Prompt、Response 或完整 spec JSON。

CREATE TABLE provider_adapter_catalog_activations (
  activation_id TEXT PRIMARY KEY CHECK (
    length(activation_id)=37
    AND substr(activation_id,1,5)='paca_'
    AND substr(activation_id,6) NOT GLOB '*[^0-9a-f]*'
  ),
  tenant_id TEXT NOT NULL,
  catalog_version TEXT NOT NULL CHECK (
    length(catalog_version) BETWEEN 3 AND 80
    AND catalog_version=lower(catalog_version)
    AND catalog_version NOT GLOB '*[^a-z0-9_.-]*'
  ),
  catalog_hash TEXT NOT NULL CHECK (
    length(catalog_hash)=64 AND catalog_hash NOT GLOB '*[^0-9a-f]*'
  ),
  spec_count INTEGER NOT NULL CHECK (typeof(spec_count)='integer' AND spec_count BETWEEN 1 AND 64),
  status TEXT NOT NULL CHECK (status IN ('active','superseded')),
  -- changed 冻结原请求语义：bootstrap/同版本收据为 0，真实版本切换为 1；代码升级后
  -- 重放旧 request_id 仍能返回原结论，不能根据当前 active 反推。
  changed INTEGER NOT NULL CHECK (changed IN (0,1)),
  request_id TEXT NOT NULL CHECK (
    length(request_id) BETWEEN 1 AND 128
    AND request_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  trace_id TEXT NOT NULL CHECK (
    length(trace_id) BETWEEN 1 AND 128
    AND trace_id NOT GLOB '*[^A-Za-z0-9_.:-]*'
  ),
  activated_by TEXT NOT NULL CHECK (
    activated_by IN ('adapter-spec-bootstrap','adapter-spec-publisher')
  ),
  activated_at TEXT NOT NULL CHECK (
    length(activated_at)=20 AND substr(activated_at,11,1)='T' AND substr(activated_at,20,1)='Z'
  ),
  superseded_at TEXT CHECK (
    superseded_at IS NULL OR (
      length(superseded_at)=20
      AND substr(superseded_at,11,1)='T'
      AND substr(superseded_at,20,1)='Z'
      AND superseded_at>=activated_at
    )
  ),
  created_at TEXT NOT NULL CHECK (
    length(created_at)=20 AND substr(created_at,11,1)='T' AND substr(created_at,20,1)='Z'
  ),
  updated_at TEXT NOT NULL CHECK (
    length(updated_at)=20 AND substr(updated_at,11,1)='T' AND substr(updated_at,20,1)='Z'
  ),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  UNIQUE (tenant_id,request_id),
  CHECK (
    (status='active' AND superseded_at IS NULL)
    OR (status='superseded' AND superseded_at IS NOT NULL)
  ),
  CHECK (updated_at>=created_at AND created_at=activated_at)
);

-- 每租户只能有一个当前目录；历史与同版本 no-op 收据不占用 active 槽位。
CREATE UNIQUE INDEX idx_provider_adapter_activations_active_prd063
  ON provider_adapter_catalog_activations(tenant_id)
  WHERE status='active';
CREATE INDEX idx_provider_adapter_activations_catalog_prd063
  ON provider_adapter_catalog_activations(tenant_id,catalog_version,status);

CREATE TRIGGER trg_provider_adapter_activations_owner_insert_prd063
BEFORE INSERT ON provider_adapter_catalog_activations FOR EACH ROW
WHEN NOT EXISTS (SELECT 1 FROM tenants tenant WHERE tenant.tenant_id=NEW.tenant_id)
  OR (
    NEW.activated_by='adapter-spec-bootstrap'
    AND (
      NEW.catalog_version!='adapter-spec-v1'
      OR NEW.catalog_hash!='9757f206d4bd0ec625d39173d4c5e5bd1f6d36132b3117008ffb1e5e7ba69870'
      OR NEW.spec_count!=9 OR NEW.status!='active' OR NEW.changed!=0
      OR NEW.request_id NOT LIKE 'req_prd063_bootstrap_%'
      OR NEW.trace_id NOT LIKE 'trc_prd063_bootstrap_%'
    )
  )
BEGIN SELECT RAISE(ABORT,'provider_adapter_activation_owner_invalid'); END;

CREATE TRIGGER trg_provider_adapter_activations_immutable_update_prd063
BEFORE UPDATE ON provider_adapter_catalog_activations FOR EACH ROW
WHEN NEW.rowid!=OLD.rowid
  OR NEW.activation_id!=OLD.activation_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.catalog_version!=OLD.catalog_version OR NEW.catalog_hash!=OLD.catalog_hash
  OR NEW.spec_count!=OLD.spec_count OR NEW.changed!=OLD.changed
  OR NEW.request_id!=OLD.request_id OR NEW.trace_id!=OLD.trace_id
  OR NEW.activated_by!=OLD.activated_by OR NEW.activated_at!=OLD.activated_at
  OR NEW.created_at!=OLD.created_at
  OR OLD.status!='active' OR NEW.status!='superseded'
  OR OLD.superseded_at IS NOT NULL OR NEW.superseded_at IS NULL
  OR NEW.updated_at!=NEW.superseded_at OR NEW.updated_at<OLD.updated_at
BEGIN SELECT RAISE(ABORT,'provider_adapter_activation_immutable'); END;

CREATE TRIGGER trg_provider_adapter_activations_immutable_delete_prd063
BEFORE DELETE ON provider_adapter_catalog_activations FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'provider_adapter_activation_immutable'); END;

-- 升级已有数据库时为每个现存租户写入发布目录身份；这是首次 bootstrap，不伪造 spec_updated。
INSERT INTO provider_adapter_catalog_activations(
  activation_id,tenant_id,catalog_version,catalog_hash,spec_count,status,changed,
  request_id,trace_id,activated_by,activated_at,superseded_at,created_at,updated_at
)
SELECT
  'paca_'||lower(hex(randomblob(16))),tenant.tenant_id,'adapter-spec-v1',
  '9757f206d4bd0ec625d39173d4c5e5bd1f6d36132b3117008ffb1e5e7ba69870',
  9,'active',0,
  'req_prd063_bootstrap_'||lower(hex(randomblob(12))),
  'trc_prd063_bootstrap_'||lower(hex(randomblob(12))),
  'adapter-spec-bootstrap',strftime('%Y-%m-%dT%H:%M:%SZ','now'),NULL,
  strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
FROM tenants tenant;

-- 0055 后创建的租户必须在同一 INSERT 语句事务内得到目录激活，避免 PAGE-090 短暂 503。
CREATE TRIGGER trg_tenants_adapter_spec_initialize_prd063
AFTER INSERT ON tenants FOR EACH ROW
BEGIN
  -- 新租户激活必须由触发器写入固定 bootstrap 身份，不能依赖异步任务补偿。
  INSERT INTO provider_adapter_catalog_activations(
    activation_id,tenant_id,catalog_version,catalog_hash,spec_count,status,changed,
    request_id,trace_id,activated_by,activated_at,superseded_at,created_at,updated_at
  ) VALUES(
    'paca_'||lower(hex(randomblob(16))),NEW.tenant_id,'adapter-spec-v1',
    '9757f206d4bd0ec625d39173d4c5e5bd1f6d36132b3117008ffb1e5e7ba69870',
    9,'active',0,
    'req_prd063_bootstrap_'||lower(hex(randomblob(12))),
    'trc_prd063_bootstrap_'||lower(hex(randomblob(12))),
    'adapter-spec-bootstrap',strftime('%Y-%m-%dT%H:%M:%SZ','now'),NULL,
    strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
  );
END;
