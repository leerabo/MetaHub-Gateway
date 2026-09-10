# PRD-113 Gateway dependency runtime acceptance

Status: focused transport, connection-lifecycle and full-schema HTTP acceptance
passed. This is not full S3 acceptance.

## Implementation

- Redis and verified TLS Redis use one deadline across connect/handshake,
  AUTH, SELECT, send and complete RESP reply. Shutdown has its own short bound.
- Exact AUTH/SELECT/PING acknowledgements are required. RESP byte/value/depth,
  framing and signed integer bounds reject malformed dependency replies.
- Network failures have fixed public text and no displayed underlying cause.
  Caller cancellation stays cancellation. No in-memory fail-open was added.
- Gateway HTTP uses the existing independently owned connection-pool scope.
  Closed SQLite handles retain parsed schema; active transactions are never
  shared. Pure ASGI scope spans streamed bodies and background callbacks.
- The reused shared SDK pooling helper and four tests were already present in
  the worktree from the trial work. This PRD takes that narrow shared dependency,
  not the unrelated frontend/API edits or published trial processes.

## Execution

- Initial five red reproductions failed on the original adapter, as recorded
  in `.artifacts/s3-redis-transport-red-20260910.xml`.
- The first post-fix run exposed two probe teardown errors: clean client EOF
  was treated as malformed input. Correcting only that probe condition gave
  five passes in `.artifacts/s3-redis-transport-20260910-r2.xml`.
- Expanded transport coverage initially passed 79 cases. Adding response
  callbacks accidentally called the connection-reset sentinel as a class;
  r4 retained that probe failure. Sentinel dispatch was corrected.
- Final transport: `.artifacts/s3-redis-transport-20260910-r5.xml`, 85 passed,
  zero failures/errors/skips, 7.945 seconds. Includes real TCP resets, blackholes,
  partial and dripped replies, recovery, real TLS with trusted/untrusted and
  hostname-mismatch certificates, TLS blackhole, cancellation and bounded close.
  Connection/write backpressure and stuck close are explicitly mock-backed
  boundary tests, not native socket failure measurements.
- Related counters, Batch and session-risk RESP regressions:
  `.artifacts/s3-redis-focused-20260910-r1.xml`, 22 passed, zero failures/errors/
  skips, 3.497 seconds.
- Gateway scope and shared pool ownership:
  `.artifacts/s3-gateway-pool-20260910-r1.xml`, 11 passed, zero failures/errors/
  skips, 1.375 seconds. Covers stream/background lifetime, concurrent handles,
  cancellation/error reset, rollback, rejected commit, and non-HTTP isolation.
- Existing database connection-lifecycle regressions:
  `.artifacts/s3-gateway-db-lifecycle-20260910-r1.xml`, 13 passed with no
  failures/errors/skips. Shared request ownership remains unchanged outside
  the explicit independent-pooling scope.

## HTTP finding

The original full-schema route fixture returned the correct safe 503 but took
4.906 seconds, exceeding its unchanged 2.5-second ceiling. Evidence:
`.artifacts/s3-redis-http-20260910-r1.xml`. No successful HTTP acceptance is
claimed from that run. Request log latency was 4325 ms; overall middleware
latency was 4885 ms. A separate read-only profile on the isolated database took
1.067 seconds, with 0.963 seconds in SQLite execute and 0.099 in physical close.
Repeated schema parsing prompted the reviewed pooling extension.

The pooled rerun (`.artifacts/s3-redis-http-20260910-r2.xml`) met the same HTTP
ceiling at 2.078 seconds but failed an invalid probe assumption of exactly one
Redis connection. Actual Gateway admission also invokes PRD-110's independent
abuse-risk counter before KEY-specific rate limits. The revised probe accepts
AUTH/SELECT, then distinguishes risk EVAL from rate-limit PING/INCRBY/EXPIRE.
It checks both actual policies without disabling either one: risk-counter
failure increments `gateway_fail_open`, while an active KEY limit returns 503.
A no-limit KEY must not issue rate-limit commands but still runs risk assessment.

The initial fixture used normal config loading from the repository directory,
which read `.env`. The next fixture removes inherited MetaHub overrides, uses
an empty temporary working directory and explicit synthetic secrets/seed paths.
No values from the repository config are printed or copied into evidence.

The HTTP fixture uses actual localhost HTTP and Redis sockets, the production
OpenAI-compatible route/middleware, a full migrated SQLite schema and a local
synthetic Provider. It does not install the unrelated full Gateway worker stack.
It checks active-rule fail-closed, no-limit key independence, same-store recovery,
durable request outcomes, foreign keys, integrity and secret-free evidence.

## Final HTTP and review results

- `.artifacts/s3-redis-http-20260910-r3.xml`: one end-to-end case passed, no
  failures/errors/skips, total 205.241 seconds including a fresh full schema.
  The faulted HTTP request measured 2.000 seconds, meeting the unchanged
  2.5-second ceiling. This is a single local measurement, not a p95 claim.
- Active KEY limit: public 503 with `gateway.rate_limit_unavailable`, matching
  request ID and no-store, zero Provider calls, persisted failed request.
- No-limit KEY: public 200 and one local Provider call despite the same Redis
  outage. It issues no KEY-limit commands; PRD-110 records its own degraded
  Gateway policy. The two policies were verified without disabling either.
- Recovery: same app and rate-limit store, successful PING/INCRBY/EXPIRE through
  TCP, public 200 and a second local Provider call, persisted successful request.
- Exactly three selected request outcomes persist. SQLite quick_check is ok,
  foreign-key violations are zero, and the responses/rows/logs contain none
  of the synthetic Provider/Virtual KEY/Redis credential values.
- Total focused evidence: 132 passing cases across the five final artifacts.
  Scoped Ruff and diff whitespace checks passed. Final JUnit secret-pattern
  scan had no matches. All owned test processes, HTTP/Redis listeners and
  handlers ended; no acceptance session remains running.
- No frontend publication, trial restart, database migration or paid model call
  was performed. Frontend 5173/PID 11248 and Control 8001/PID 17220 are unchanged.

## Remaining gates

Protocol peers are not native Redis durability, Lua atomicity or cluster proof.
Single-dimension INCR/EXPIRE/concurrency sequences still need atomicity and
ambiguous-write recovery work. Full Gateway worker startup/shutdown, native
dependency fail policies, external KMS/object storage, 100+ tenant noisy-neighbor
baseline, 2x peak, 72-hour stability, PRD-111 actual restore and complete S3 batch
gates remain unproven. The published trial is not restarted or migrated.
