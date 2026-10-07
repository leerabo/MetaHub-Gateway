-- metahub: navigation-contract-split-v2
-- Promote the Governance/System split into the persisted contract. The prior
-- snapshot remains immutable history; only the active head and projection move.

PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS trg_workspace_navigation_item_update_prd146;
DROP TRIGGER IF EXISTS trg_workspace_navigation_item_delete_prd146;
ALTER TABLE workspace_navigation_contract_items RENAME TO workspace_navigation_contract_items_prd184_old;
CREATE TABLE workspace_navigation_contract_items(
  navigation_contract_item_id TEXT PRIMARY KEY CHECK(
    length(navigation_contract_item_id)=36 AND substr(navigation_contract_item_id,1,4)='nci_'),
  navigation_contract_snapshot_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  contract_generation INTEGER NOT NULL CHECK(contract_generation>=1),
  source_navigation_id TEXT NOT NULL CHECK(length(source_navigation_id) BETWEEN 2 AND 80),
  source_kind TEXT NOT NULL CHECK(source_kind IN('excel','extra')),
  disposition_code TEXT CHECK(disposition_code IS NULL OR disposition_code GLOB 'D[0-2][0-9]'),
  placement TEXT NOT NULL CHECK(placement IN(
    'primary','detail','advanced','account','developer_center','task_home','replaced','retired_internal')),
  route_code TEXT NOT NULL CHECK(length(route_code) BETWEEN 3 AND 120),
  group_code TEXT CHECK(group_code IS NULL OR group_code IN(
    'home','workspace','gateway','finance','governance','applications','system','observability','developer-center')),
  required_permissions_json TEXT NOT NULL CHECK(json_valid(required_permissions_json)
    AND json_type(required_permissions_json)='array' AND json(required_permissions_json)=required_permissions_json),
  display_order INTEGER NOT NULL CHECK(display_order BETWEEN 0 AND 1000),
  item_sha256 TEXT NOT NULL CHECK(length(item_sha256)=64),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(navigation_contract_snapshot_id,source_navigation_id),
  FOREIGN KEY(workspace_id,tenant_id,navigation_contract_snapshot_id,contract_generation)
    REFERENCES workspace_navigation_contract_snapshots(
      workspace_id,tenant_id,navigation_contract_snapshot_id,generation),
  CHECK((source_kind='excel' AND disposition_code IS NOT NULL)
    OR(source_kind='extra' AND disposition_code IS NULL))
);
INSERT INTO workspace_navigation_contract_items
SELECT * FROM workspace_navigation_contract_items_prd184_old;
DROP TABLE workspace_navigation_contract_items_prd184_old;
CREATE TRIGGER trg_workspace_navigation_item_update_prd146
BEFORE UPDATE ON workspace_navigation_contract_items
BEGIN SELECT RAISE(ABORT,'workspace_navigation_item_immutable'); END;
CREATE TRIGGER trg_workspace_navigation_item_delete_prd146
BEFORE DELETE ON workspace_navigation_contract_items
BEGIN SELECT RAISE(ABORT,'workspace_navigation_item_immutable'); END;

DROP TRIGGER IF EXISTS trg_workspace_navigation_preference_owner_prd146;
DROP INDEX IF EXISTS uq_workspace_navigation_preference_active_prd146;
ALTER TABLE workspace_navigation_preferences RENAME TO workspace_navigation_preferences_prd184_old;
CREATE TABLE workspace_navigation_preferences(
  navigation_preference_id TEXT PRIMARY KEY CHECK(
    length(navigation_preference_id)=36 AND substr(navigation_preference_id,1,4)='nvp_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  workspace_membership_id TEXT NOT NULL,
  membership_revision INTEGER NOT NULL CHECK(membership_revision>=1),
  last_primary_route_code TEXT,
  expanded_group_code TEXT CHECK(expanded_group_code IS NULL OR expanded_group_code IN(
    'home','workspace','gateway','finance','governance','applications','system','observability','developer-center')),
  sidebar_collapsed INTEGER NOT NULL CHECK(sidebar_collapsed IN(0,1)),
  contract_snapshot_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('active','membership_revoked')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id) REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(workspace_membership_id) REFERENCES workspace_memberships(workspace_membership_id),
  FOREIGN KEY(contract_snapshot_id) REFERENCES workspace_navigation_contract_snapshots(navigation_contract_snapshot_id)
);
INSERT INTO workspace_navigation_preferences SELECT * FROM workspace_navigation_preferences_prd184_old;
DROP TABLE workspace_navigation_preferences_prd184_old;
CREATE UNIQUE INDEX uq_workspace_navigation_preference_active_prd146
  ON workspace_navigation_preferences(workspace_id,tenant_id,account_id) WHERE status='active';
CREATE TRIGGER trg_workspace_navigation_preference_owner_prd146
BEFORE UPDATE ON workspace_navigation_preferences FOR EACH ROW
WHEN NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.account_id!=OLD.account_id OR NEW.workspace_membership_id!=OLD.workspace_membership_id
  OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'workspace_navigation_preference_owner_immutable'); END;

CREATE TEMP TABLE prd184_contract_upgrade AS
SELECT
  head.workspace_id,
  head.tenant_id,
  head.navigation_contract_snapshot_id AS old_snapshot_id,
  'ncs_' || substr(metahub_sha256(
    head.workspace_id || ':' || head.tenant_id || ':workspace-navigation-v1:' || (head.generation + 1)
  ),1,32) AS new_snapshot_id,
  head.generation + 1 AS new_generation,
  head.revision + 1 AS new_revision,
  projection.workspace_navigation_projection_id AS old_projection_id,
  'wnp_' || substr(metahub_sha256(
    head.workspace_id || ':' || head.tenant_id || ':' || projection.content_sha256 || ':' || (projection.generation + 1)
  ),1,32) AS new_projection_id,
  projection.generation + 1 AS projection_generation
FROM workspace_navigation_contract_heads head
JOIN workspace_navigation_projection_heads projection_head
  ON projection_head.workspace_id=head.workspace_id AND projection_head.tenant_id=head.tenant_id
JOIN workspace_navigation_projection_versions projection
  ON projection.workspace_navigation_projection_id=projection_head.workspace_navigation_projection_id
WHERE head.generation = (
  SELECT max(generation) FROM workspace_navigation_contract_snapshots current
  WHERE current.workspace_id=head.workspace_id AND current.tenant_id=head.tenant_id
);

INSERT INTO workspace_navigation_contract_snapshots(
  navigation_contract_snapshot_id,workspace_id,tenant_id,contract_version,
  source_catalog_version,source_catalog_sha256,disposition_sha256,extra_sha256,
  item_count,status,revision,generation,created_at)
SELECT
  upgrade.new_snapshot_id, old.workspace_id, old.tenant_id, old.contract_version,
  old.source_catalog_version, old.source_catalog_sha256, old.disposition_sha256,
  old.extra_sha256, old.item_count, 'active', upgrade.new_revision,
  upgrade.new_generation, strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd184_contract_upgrade upgrade
JOIN workspace_navigation_contract_snapshots old
  ON old.navigation_contract_snapshot_id=upgrade.old_snapshot_id;

INSERT INTO workspace_navigation_contract_items(
  navigation_contract_item_id,navigation_contract_snapshot_id,workspace_id,tenant_id,
  contract_generation,source_navigation_id,source_kind,disposition_code,placement,
  route_code,group_code,required_permissions_json,display_order,item_sha256,created_at)
SELECT
  'nci_' || substr(metahub_sha256(upgrade.new_snapshot_id || ':' || old.source_navigation_id),1,32),
  upgrade.new_snapshot_id, old.workspace_id, old.tenant_id, upgrade.new_generation,
  old.source_navigation_id, old.source_kind, old.disposition_code, old.placement,
  old.route_code,
  CASE old.group_code
    WHEN 'observability' THEN CASE old.source_navigation_id
      WHEN 'request-logs' THEN 'governance'
      WHEN 'request-explanations' THEN 'governance'
      ELSE 'system'
    END
    ELSE old.group_code
  END,
  old.required_permissions_json, old.display_order,
  metahub_sha256(json_array(
    old.source_navigation_id, old.source_kind, old.disposition_code, old.placement,
    old.route_code,
    CASE old.group_code
      WHEN 'observability' THEN CASE old.source_navigation_id
        WHEN 'request-logs' THEN 'governance'
        WHEN 'request-explanations' THEN 'governance'
        ELSE 'system'
      END
      ELSE old.group_code
    END,
    json(old.required_permissions_json), old.display_order
  )), strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd184_contract_upgrade upgrade
JOIN workspace_navigation_contract_items old
  ON old.navigation_contract_snapshot_id=upgrade.old_snapshot_id;

INSERT INTO workspace_navigation_projection_versions(
  workspace_navigation_projection_id,workspace_id,tenant_id,
  navigation_contract_snapshot_id,contract_generation,entitlement_generation,
  status,content_sha256,generation,observed_at,created_at)
SELECT
  upgrade.new_projection_id, old.workspace_id, old.tenant_id,
  upgrade.new_snapshot_id, upgrade.new_generation, old.entitlement_generation,
  old.status, old.content_sha256, upgrade.projection_generation,
  strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd184_contract_upgrade upgrade
JOIN workspace_navigation_projection_versions old
  ON old.workspace_navigation_projection_id=upgrade.old_projection_id;

INSERT INTO workspace_navigation_projection_items(
  workspace_navigation_projection_id,workspace_id,tenant_id,projection_generation,
  source_navigation_id,route_code,placement,group_code,required_permissions_json,
  display_order,item_sha256)
SELECT
  upgrade.new_projection_id, old.workspace_id, old.tenant_id, upgrade.projection_generation,
  old.source_navigation_id, old.route_code, old.placement,
  CASE old.group_code
    WHEN 'observability' THEN CASE old.source_navigation_id
      WHEN 'request-logs' THEN 'governance'
      WHEN 'request-explanations' THEN 'governance'
      ELSE 'system'
    END
    ELSE old.group_code
  END,
  old.required_permissions_json, old.display_order, old.item_sha256
FROM prd184_contract_upgrade upgrade
JOIN workspace_navigation_projection_items old
  ON old.workspace_navigation_projection_id=upgrade.old_projection_id;

INSERT INTO workspace_navigation_projection_sources(
  workspace_navigation_projection_id,workspace_id,tenant_id,projection_generation,
  source_code,source_revision,source_watermark,freshness)
SELECT upgrade.new_projection_id, old.workspace_id, old.tenant_id,
  upgrade.projection_generation, old.source_code, old.source_revision,
  old.source_watermark, old.freshness
FROM prd184_contract_upgrade upgrade
JOIN workspace_navigation_projection_sources old
  ON old.workspace_navigation_projection_id=upgrade.old_projection_id;

UPDATE workspace_navigation_contract_heads
SET navigation_contract_snapshot_id=(SELECT new_snapshot_id FROM prd184_contract_upgrade
    WHERE workspace_id=workspace_navigation_contract_heads.workspace_id
      AND tenant_id=workspace_navigation_contract_heads.tenant_id),
    generation=(SELECT new_generation FROM prd184_contract_upgrade
      WHERE workspace_id=workspace_navigation_contract_heads.workspace_id
        AND tenant_id=workspace_navigation_contract_heads.tenant_id),
    revision=revision+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE EXISTS (SELECT 1 FROM prd184_contract_upgrade
  WHERE workspace_id=workspace_navigation_contract_heads.workspace_id
    AND tenant_id=workspace_navigation_contract_heads.tenant_id);

UPDATE workspace_navigation_projection_heads
SET workspace_navigation_projection_id=(SELECT new_projection_id FROM prd184_contract_upgrade
    WHERE workspace_id=workspace_navigation_projection_heads.workspace_id
      AND tenant_id=workspace_navigation_projection_heads.tenant_id),
    generation=(SELECT projection_generation FROM prd184_contract_upgrade
      WHERE workspace_id=workspace_navigation_projection_heads.workspace_id
        AND tenant_id=workspace_navigation_projection_heads.tenant_id),
    revision=revision+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE EXISTS (SELECT 1 FROM prd184_contract_upgrade
  WHERE workspace_id=workspace_navigation_projection_heads.workspace_id
    AND tenant_id=workspace_navigation_projection_heads.tenant_id);

UPDATE workspace_navigation_preferences
SET contract_snapshot_id=(SELECT new_snapshot_id FROM prd184_contract_upgrade
    WHERE workspace_id=workspace_navigation_preferences.workspace_id
      AND tenant_id=workspace_navigation_preferences.tenant_id),
    expanded_group_code=CASE expanded_group_code WHEN 'observability' THEN NULL ELSE expanded_group_code END
WHERE EXISTS (SELECT 1 FROM prd184_contract_upgrade
  WHERE workspace_id=workspace_navigation_preferences.workspace_id
    AND tenant_id=workspace_navigation_preferences.tenant_id);
UPDATE workspace_navigation_member_states
SET contract_snapshot_id=(SELECT new_snapshot_id FROM prd184_contract_upgrade
    WHERE workspace_id=workspace_navigation_member_states.workspace_id
      AND tenant_id=workspace_navigation_member_states.tenant_id)
WHERE EXISTS (SELECT 1 FROM prd184_contract_upgrade
  WHERE workspace_id=workspace_navigation_member_states.workspace_id
    AND tenant_id=workspace_navigation_member_states.tenant_id);
CREATE TEMP TABLE prd184_task_upgrade AS
SELECT
  head.workspace_membership_id,
  head.workspace_id,
  head.tenant_id,
  head.role_task_home_version_id AS old_version_id,
  head.generation AS old_generation,
  head.revision AS old_revision,
  upgrade.new_snapshot_id,
  upgrade.new_generation,
  'rth_' || substr(metahub_sha256(
    head.workspace_membership_id || ':' || (head.generation + 1) || ':' || version.source_digest
  ),1,32) AS new_version_id
FROM workspace_role_task_home_heads head
JOIN workspace_role_task_home_versions version
  ON version.role_task_home_version_id=head.role_task_home_version_id
JOIN prd184_contract_upgrade upgrade
  ON upgrade.workspace_id=head.workspace_id AND upgrade.tenant_id=head.tenant_id;

INSERT INTO workspace_role_task_home_versions(
  role_task_home_version_id,workspace_id,tenant_id,workspace_membership_id,
  membership_revision,role_code,contract_snapshot_id,contract_generation,
  source_digest,status,generation,observed_at,created_at)
SELECT
  task.new_version_id, old.workspace_id, old.tenant_id, old.workspace_membership_id,
  old.membership_revision, old.role_code, task.new_snapshot_id, task.new_generation,
  old.source_digest, old.status, task.old_generation + 1,
  strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd184_task_upgrade task
JOIN workspace_role_task_home_versions old
  ON old.role_task_home_version_id=task.old_version_id;
INSERT INTO workspace_role_task_home_tasks(
  role_task_home_version_id,workspace_id,tenant_id,workspace_membership_id,
  task_code,source_code,source_revision,evidence_digest,status,priority,
  action_route_code,completion_generation,created_at)
SELECT
  task.new_version_id, old.workspace_id, old.tenant_id, old.workspace_membership_id,
  old.task_code, old.source_code, old.source_revision, old.evidence_digest,
  old.status, old.priority, old.action_route_code, old.completion_generation,
  strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd184_task_upgrade task
JOIN workspace_role_task_home_tasks old
  ON old.role_task_home_version_id=task.old_version_id;
UPDATE workspace_role_task_home_heads
SET role_task_home_version_id=(SELECT new_version_id FROM prd184_task_upgrade
    WHERE workspace_membership_id=workspace_role_task_home_heads.workspace_membership_id),
    generation=generation+1,
    revision=revision+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE EXISTS (SELECT 1 FROM prd184_task_upgrade
  WHERE workspace_membership_id=workspace_role_task_home_heads.workspace_membership_id);
DROP TABLE prd184_task_upgrade;

DROP TABLE prd184_contract_upgrade;
COMMIT;
PRAGMA foreign_keys=ON;
