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
