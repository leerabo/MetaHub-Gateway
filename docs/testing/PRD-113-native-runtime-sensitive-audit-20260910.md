# PRD-113 native capacity runtime sensitive audit

Scope: native capacity test fixtures, reusable operator seeding and the isolated
PostgreSQL acceptance runner. No product authorization or DTO changes.

- The runner only creates a fresh `.tmp/s3-capacity-native-*` cluster, listens
  on loopback and stops that same owned instance. It does not accept a caller
  database URL, touch the trial schema or delete retained test directories.
- Tests reuse UUID database creation/deletion helpers with psycopg identifiers;
  their template has no capacity jobs and each test gets a separate clone.
- MetaHub and PostgreSQL environment overrides are excluded from the pytest
  child. Seed credential fields are generated, and inherited seed file/fields
  are also removed inside the template fixture. No user credential is read.
- Test result fixtures explicitly verify transaction logic only. Their
  simulated statistics must not be used for operational/performance acceptance.
- Concurrent operations observe only PostgreSQL blocking-PID counts and
  synthetic task IDs. The report stores counts, migration version, process
  completion and elapsed time, never credentials, connection URLs or permits.
- Local lifecycle/JUnit artifacts contain synthetic execution evidence only.
  Exception messages from domain failures remain fixed safe outcome codes.
- Queued cancellation and rollback guards retain facts; concurrent completion
  cannot write a baseline after cancellation wins. Atomic rollback checks
  include effect, attempt and external-wait state as well as job/domain state.

## Final verification

- Native full selection has 28 passed/two test-input failures. Its corrected
  focused selection has three passed, including the added forecast case:
  31 distinct passing native cases. SQLite 17 and harness 7 also passed.
- All newly created clusters are stopped, including failed setup runs.
  Harness timeout coverage includes actual Windows parent/child processes;
  the owned tree was killed before reaping the launcher. Trial processes were
  not targeted, restarted or migrated.
- Final JUnit/aggregate artifacts and scoped source changes were checked for
  provider-key, bearer-token and credential-bearing connection-string patterns.
  No matching sensitive values were found. Reports contain only synthetic
  identifiers, test names, fixed reason codes and aggregate results.
- This is a scoped review, not the full S3 cross-domain security gate.
