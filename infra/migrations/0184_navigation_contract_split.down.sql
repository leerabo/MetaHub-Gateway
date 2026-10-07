-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;
UPDATE workspace_navigation_contract_heads
SET navigation_contract_snapshot_id=(
      SELECT previous.navigation_contract_snapshot_id
      FROM workspace_navigation_contract_snapshots current
      JOIN workspace_navigation_contract_snapshots previous
        ON previous.workspace_id=current.workspace_id
       AND previous.tenant_id=current.tenant_id
       AND previous.generation=current.generation-1
      WHERE current.navigation_contract_snapshot_id=workspace_navigation_contract_heads.navigation_contract_snapshot_id
    ),
    generation=generation-1,
    revision=revision+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE generation>1;
COMMIT;
PRAGMA foreign_keys = ON;
