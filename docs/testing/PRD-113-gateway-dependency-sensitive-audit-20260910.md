# PRD-113 Gateway dependency sensitive audit

Scope: Redis transport/deadlines, Gateway independently owned connection reuse,
isolated TCP/TLS/HTTP fixtures and their focused records. Full S3 security gates
are not claimed by this review.

- AUTH credentials are encoded only into the configured Redis connection.
  TLS uses default certificate/hostname validation and never retries plaintext.
  Trusted-certificate testing modifies only the test process trust environment.
- The connection-config repr omits its fields. Public store failures use fixed
  text and suppress the original exception chain. Tests inspect formatted
  exceptions and logs for synthetic sensitive values.
- RESP reply length, aggregate bytes, total values and depth are bounded before
  bulk allocation or recursion. Cancellation is not converted into an allowance
  or retryable business failure. No new business permission/DTO is introduced.
- SQLite pooling carries only an opt-in ContextVar boolean, not another
  request's connection. Physical handles are returned only on explicit logical
  close, which rolls back unfinished transactions and resets connection state.
  Existing commit-guard rejection remains enforced. PostgreSQL checkout behavior
  is unchanged; non-HTTP Gateway scopes remain outside the new wrapper.
- Test peers bind loopback ephemeral ports and close all owned tasks/listeners.
  Certificates and private keys are generated solely under pytest temporary
  directories; they are not repository or user credentials and are not committed.
- HTTP uses a separately migrated synthetic database and local fake Provider.
  It does not send paid model calls or touch the published trial database.
- Audit finding: the first HTTP test invoked the normal config loader from the
  repo directory, implicitly reading `.env`. Config logging masked its sensitive
  values, but that is not a credential-isolated fixture. The revised fixture
  clears inherited MetaHub environment values, changes to a fresh directory and
  supplies synthetic secrets/absolute migration and seed paths before app setup.
- Scanned r5 transport, related regressions, pool tests and initial HTTP JUnit
  for bearer material, production-shaped provider keys, credential-bearing
  connection URLs and private-key PEM. No matching values were found.
- Final HTTP proof is `.artifacts/s3-redis-http-20260910-r3.xml`: bounded public
  503/no upstream call, successful no-limit request, successful same-store
  recovery, three safe request outcomes, integrity ok and zero FK violations.
  Explicit synthetic secret values are absent from responses, rows and logs.
- PRD-110 abuse-risk assessment independently uses the same Redis deployment.
  Its existing Gateway degraded policy is not the active KEY-limit policy.
  The revised HTTP probe distinguishes their real commands and asserts each
  policy; it does not disable risk assessment to eliminate extra connections.
  The separate risk Redis adapter's TLS/cleanup/parser behavior is not proved
  by the rate-limit adapter tests and remains part of broader dependency gates.
- Existing `.tmp` evidence remains retained. Trial frontend PID 11248 and
  Control PID 17220 remain listening on 5173 and 8001 without restart/migration.

## Final checks

132 focused cases passed in the final five artifacts, with no failures/errors/
skips. Final HTTP latency was 2.000 seconds; this is not a production percentile.
Final JUnit files passed the same credential/private-key pattern scan. Scoped
Ruff and whitespace checks passed. No owned test process or listener remains
running. The historical failed runs remain recorded, not overwritten as success.
