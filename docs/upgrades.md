# Upgrades and rollback

An upgrade is a staged, forward-only switch. The controller verifies the release manifest, rejects a database image change, builds the new services beside the current release, runs preflight checks, creates and verifies an encrypted backup, migrates the database, and starts the new services only after readiness checks pass.

The Ubuntu controller accepts a verified release directory and version:

```bash
sudo scripts/deploy/metahubctl.sh upgrade /opt/metahub-state \
  /opt/metahub-releases/v2.1.0 v2.1.0
```

The release directory must contain `release-manifest.json` and the matching production image lock. The controller rejects backwards versions, database image changes, failed preflight checks, invalid backups, and unhealthy services.

Keep the old release directory and the verified backup until business acceptance is complete. Schema migrations are forward-only; there is no automatic downgrade. If the rollout is interrupted, inspect the state directory and use the recovery command with a new target state directory:

```bash
sudo scripts/deploy/metahubctl.sh recover /opt/metahub-state \
  /opt/metahub-state/backups/<prefix> \
  /opt/metahub-recovery \
  /opt/metahub-releases/v2.1.0
```

Do not delete Docker volumes or edit the database manually during an incident. Preserve `recovery.json`, the backup prefix, and service logs for diagnosis.

## SaaS administrator upgrade center

The `platform_owner` can open **Platform → Infrastructure** in the SaaS console. The page shows the running release, verified releases available from the host updater, and recent jobs. **Back up and upgrade** queues a signed release for the host update agent; it does not accept shell commands or arbitrary paths. **Restart service** queues a controlled restart of the currently active release without changing the database or release pointer. Both actions require an explicit confirmation and are recorded in the job list. During either operation the SaaS console and organization gateways may be briefly unavailable; gateway users receive the new gateway version when the system release completes.

The host update service must be enabled for these controls to be available. A failed upgrade blocks further actions until an operator inspects the backup and recovery state. Do not use the restart control as a substitute for recovery after a failed migration.
