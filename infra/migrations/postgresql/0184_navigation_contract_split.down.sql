-- metahub: navigation-contract-split-v2 down
UPDATE workspace_navigation_contract_heads head
SET navigation_contract_snapshot_id=previous.navigation_contract_snapshot_id,
    generation=previous.generation, revision=head.revision+1, updated_at=metahub_utc_now()
FROM workspace_navigation_contract_snapshots current
JOIN workspace_navigation_contract_snapshots previous
  ON previous.workspace_id=current.workspace_id AND previous.tenant_id=current.tenant_id
 AND previous.generation=current.generation-1
WHERE current.navigation_contract_snapshot_id=head.navigation_contract_snapshot_id;
