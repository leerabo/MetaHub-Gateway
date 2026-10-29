-- metahub: navigation-contract-split-v2
-- Promote the Governance/System split into the persisted contract.

ALTER TABLE workspace_navigation_contract_items
  DROP CONSTRAINT ck_workspace_navigation_contract_items_8;
ALTER TABLE workspace_navigation_contract_items
  ADD CONSTRAINT ck_workspace_navigation_contract_items_8 CHECK(
    group_code IS NULL OR group_code IN(
      'home','workspace','gateway','finance','governance','applications',
      'system','observability','developer-center')) NOT VALID;
ALTER TABLE workspace_navigation_contract_items
  VALIDATE CONSTRAINT ck_workspace_navigation_contract_items_8;
ALTER TABLE workspace_navigation_preferences
  DROP CONSTRAINT ck_workspace_navigation_preferences_3;
ALTER TABLE workspace_navigation_preferences
  ADD CONSTRAINT ck_workspace_navigation_preferences_3 CHECK(
    expanded_group_code IS NULL OR expanded_group_code IN(
      'home','workspace','gateway','finance','governance','applications',
      'system','observability','developer-center')) NOT VALID;
ALTER TABLE workspace_navigation_preferences
  VALIDATE CONSTRAINT ck_workspace_navigation_preferences_3;

CREATE TEMP TABLE prd184_contract_upgrade ON COMMIT DROP AS
SELECT
  head.workspace_id, head.tenant_id,
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
  ON projection.workspace_navigation_projection_id=projection_head.workspace_navigation_projection_id;

INSERT INTO workspace_navigation_contract_snapshots(
  navigation_contract_snapshot_id,workspace_id,tenant_id,contract_version,
  source_catalog_version,source_catalog_sha256,disposition_sha256,extra_sha256,
  item_count,status,revision,generation,created_at)
SELECT upgrade.new_snapshot_id, old.workspace_id, old.tenant_id, old.contract_version,
  old.source_catalog_version, old.source_catalog_sha256, old.disposition_sha256,
  old.extra_sha256, old.item_count, 'active', upgrade.new_revision,
  upgrade.new_generation, metahub_utc_now()
FROM prd184_contract_upgrade upgrade
JOIN workspace_navigation_contract_snapshots old
  ON old.navigation_contract_snapshot_id=upgrade.old_snapshot_id;

INSERT INTO workspace_navigation_contract_items(
  navigation_contract_item_id,navigation_contract_snapshot_id,workspace_id,tenant_id,
  contract_generation,source_navigation_id,source_kind,disposition_code,placement,
  route_code,group_code,required_permissions_json,display_order,item_sha256,created_at)
SELECT 'nci_' || substr(metahub_sha256(upgrade.new_snapshot_id || ':' || old.source_navigation_id),1,32),
  upgrade.new_snapshot_id, old.workspace_id, old.tenant_id, upgrade.new_generation,
  old.source_navigation_id, old.source_kind, old.disposition_code, old.placement,
  old.route_code,
  CASE old.group_code WHEN 'observability' THEN CASE old.source_navigation_id
    WHEN 'request-logs' THEN 'governance' WHEN 'request-explanations' THEN 'governance'
    ELSE 'system' END ELSE old.group_code END,
  old.required_permissions_json, old.display_order,
  metahub_sha256(json_array(old.source_navigation_id,old.source_kind,old.disposition_code,
    old.placement,old.route_code,
    CASE old.group_code WHEN 'observability' THEN CASE old.source_navigation_id
      WHEN 'request-logs' THEN 'governance' WHEN 'request-explanations' THEN 'governance'
      ELSE 'system' END ELSE old.group_code END,
    old.required_permissions_json::json,old.display_order)::text), metahub_utc_now()
FROM prd184_contract_upgrade upgrade
JOIN workspace_navigation_contract_items old
  ON old.navigation_contract_snapshot_id=upgrade.old_snapshot_id;

INSERT INTO workspace_navigation_projection_versions(
  workspace_navigation_projection_id,workspace_id,tenant_id,navigation_contract_snapshot_id,
  contract_generation,entitlement_generation,status,content_sha256,generation,observed_at,created_at)
SELECT upgrade.new_projection_id, old.workspace_id, old.tenant_id, upgrade.new_snapshot_id,
  upgrade.new_generation, old.entitlement_generation, old.status, old.content_sha256,
  upgrade.projection_generation, metahub_utc_now(), metahub_utc_now()
FROM prd184_contract_upgrade upgrade
JOIN workspace_navigation_projection_versions old
  ON old.workspace_navigation_projection_id=upgrade.old_projection_id;
INSERT INTO workspace_navigation_projection_items(
  workspace_navigation_projection_id,workspace_id,tenant_id,projection_generation,
  source_navigation_id,route_code,placement,group_code,required_permissions_json,display_order,item_sha256)
SELECT upgrade.new_projection_id, old.workspace_id, old.tenant_id, upgrade.projection_generation,
  old.source_navigation_id, old.route_code, old.placement,
  CASE old.group_code WHEN 'observability' THEN CASE old.source_navigation_id
    WHEN 'request-logs' THEN 'governance' WHEN 'request-explanations' THEN 'governance'
    ELSE 'system' END ELSE old.group_code END,
  old.required_permissions_json, old.display_order, old.item_sha256
FROM prd184_contract_upgrade upgrade
JOIN workspace_navigation_projection_items old
  ON old.workspace_navigation_projection_id=upgrade.old_projection_id;
INSERT INTO workspace_navigation_projection_sources(
  workspace_navigation_projection_id,workspace_id,tenant_id,projection_generation,
  source_code,source_revision,source_watermark,freshness)
SELECT upgrade.new_projection_id, old.workspace_id, old.tenant_id, upgrade.projection_generation,
  old.source_code, old.source_revision, old.source_watermark, old.freshness
FROM prd184_contract_upgrade upgrade
JOIN workspace_navigation_projection_sources old
  ON old.workspace_navigation_projection_id=upgrade.old_projection_id;

UPDATE workspace_navigation_contract_heads head
SET navigation_contract_snapshot_id=upgrade.new_snapshot_id,
    generation=upgrade.new_generation, revision=head.revision+1, updated_at=metahub_utc_now()
FROM prd184_contract_upgrade upgrade
WHERE upgrade.workspace_id=head.workspace_id AND upgrade.tenant_id=head.tenant_id;
UPDATE workspace_navigation_projection_heads head
SET workspace_navigation_projection_id=upgrade.new_projection_id,
    generation=upgrade.projection_generation, revision=head.revision+1, updated_at=metahub_utc_now()
FROM prd184_contract_upgrade upgrade
WHERE upgrade.workspace_id=head.workspace_id AND upgrade.tenant_id=head.tenant_id;
UPDATE workspace_navigation_preferences pref
SET contract_snapshot_id=upgrade.new_snapshot_id,
    expanded_group_code=CASE pref.expanded_group_code WHEN 'observability' THEN NULL ELSE pref.expanded_group_code END
FROM prd184_contract_upgrade upgrade
WHERE upgrade.workspace_id=pref.workspace_id AND upgrade.tenant_id=pref.tenant_id;
UPDATE workspace_navigation_member_states state
SET contract_snapshot_id=upgrade.new_snapshot_id
FROM prd184_contract_upgrade upgrade
WHERE upgrade.workspace_id=state.workspace_id AND upgrade.tenant_id=state.tenant_id;
