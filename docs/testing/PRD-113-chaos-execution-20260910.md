# PRD-113 chaos execution verification

Status: implementation and affected verification complete. S3 remains incomplete.
Scope: measured chaos orchestration, PRD-106 stop ownership and proxy protocol 3.
Design and two pre-coding reviews: `docs/prd/S3-chaos-runner-integration-20260910.md`.

## Evidence

- `.artifacts/prd113-chaos-drain-20260910-r1.xml`: 56 passed. Actual TCP
  timeout expiry/release discards old bytes, a fresh connection recovers, and
  protocol/journal tests reject unconfirmed stop or recovery claims.
- `.artifacts/prd113-chaos-http-20260910-r2.xml`: six real HTTP scenarios passed.
  Two full 10-second faults, cancellation, error-threshold stop, remote apply
  with failed response, and abrupt runner death followed by original-binding
  cleanup despite changed deployment settings.
- `.artifacts/prd113-chaos-final-20260910-r5.xml`: 205 passed, zero failures,
  errors or skips in 563.46 seconds. This includes 37 fault-proxy tests, 26 chaos
  protocol/journal tests, nine real HTTP chaos scenarios, 35 durable load-runner
  tests, 68 result-integrity tests, 17 capacity database integration tests, five
  runner API tests, two deployment structure tests and six configuration tests.
- The final affected run also includes no-effect rejection, target failure after
  reversal, unavailable reversal proof across shutdown, preserved cancellation,
  schema upgrade, failed detach persistence, and bounded socket-close confirmation.
  It retains load measurement, result validation, deployment and Control API coverage.

## Issues found and corrected

- Natural fault expiry can allow an already-dispatched request to reach the
  target before the runner cancels its remaining requests. The full-run test
  bounds that boundary by configured concurrency and verifies a static request
  count after termination. Separate real-byte tests require previously withheld
  payloads to be discarded and never reach the upstream.
- Restart originally overwrote a deferred user cancellation with interruption.
  Preserve the cancellation both before and after saved reversal, across
  repeated restarts. The actual missing-proof/restart scenario reproduced this.
- `TimeoutError` is an `OSError`: socket-close verification must handle timeout
  before the general socket error branch. A timeout is not close confirmation.
- Ruff's format check also identified a pre-existing formatting difference in
  unchanged `engine.py`. Leave it untouched and check the edited file set.
- The r3 run reproduced the lost cancellation (66 passed, one failed); r4
  reproduced the timeout exception ordering issue (35 passed, one failed).
  Both failures were fixed and are covered by the passing r5 run. They are not
  reported as successful runs or removed from the evidence directory.

Ruff check, formatting of all 16 edited Python files, compileall and the scoped
whitespace audit passed. The one warning is the existing Starlette/httpx test
client deprecation, not a skipped test. All owned test services/processes exited.

## Scope limits

These tests prove real runner/proxy/HTTP execution with synthetic local identities.
They do not prove the actual Gateway's Redis/KMS/object-storage fail policies,
100 distinct tenant identities, noisy-neighbor fairness, 2x peak degradation,
72-hour stability, multi-region DR, or Docker runtime isolation. No such gate is
marked passed. PostgreSQL 0159 execution/concurrency and full S3 gates remain open.

The running trial on 5173/8001 is excluded. Its frontend/control processes and
schema 0150 database are not restarted, migrated or used as a chaos target.
Tests use new `.tmp` directories and generated credentials, with no paid calls.
