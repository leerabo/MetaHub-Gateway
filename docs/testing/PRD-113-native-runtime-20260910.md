# PRD-113 native capacity runtime acceptance

Status: focused acceptance passed. All owned clusters stopped. Full S3 gates
remain open as listed below.

## Scope

- Reuse 17 capacity lifecycle cases on native PostgreSQL with one empty-job,
  seeded template and a separately cloned database per case.
- Verify 0159 installed trigger, down/up and idempotent migration replay.
  Retained capacity facts reject both the public rollback operation and native
  down SQL without losing the trigger, job or domain record.
  Queued forecast cancellation explicitly covers the third projection branch.
- Compete two external observers for one capacity job. Exactly one owns the
  observation; the job remains on its original attempt.
- Exercise both cancellation/completion lock orders on separate connections
  with actual database clocks. Observe `pg_blocking_pids` before releasing the
  winning transaction. Verify job, attempt, wait, effect, domain and baseline.
- Re-run the existing eight native PRD-106 job scenarios and affected SQLite
  capacity lifecycle cases as regressions.

## Runner

`.venv/Scripts/python.exe -X utf8 scripts/acceptance/s3-postgresql-runtime.py`

Each invocation creates a new loopback-only cluster under `.tmp`, generates
synthetic seed credentials, excludes inherited MetaHub/PostgreSQL configuration
from the pytest child, and records a unique `.artifacts/s3-capacity-native-*`
JUnit and aggregate report. It never accepts a trial database or reused cluster.
The temporary cluster is retained stopped; only databases created by test
fixtures are removed. No paid model or customer credential is required.

## Execution notes

- The first run did not reach migrations/tests: Windows inherited a pg_ctl
  output pipe after the parent exited. The owned test cluster was stopped,
  unblocking the runner. Failure evidence is retained at
  `.artifacts/s3-capacity-native-0o25ditb/report.json`.
- The runner now directs lifecycle output to a file, not a pipe. This changes
  only the acceptance harness, not application execution or the trial.
- The second run stopped after baseline installation because the standalone
  Python lacked `pglast`; `.artifacts/s3-capacity-native-wvl0e2tf/report.json`
  retains that failed run. Use the project's virtual environment, which has
  the required parser, and verify parsing before creating a cluster.
- The next run installed 0159 but could not collect capacity tests because the
  virtual environment lacked `filelock` (report:
  `.artifacts/s3-capacity-native-sp5vagr4/report.json`). Installed `filelock`
  3.32.6 and `psutil` 7.2.2 within the existing capacity-runner dependency ranges,
  only in `.venv`. A sanitized pytest collection preflight now precedes cluster
  creation. No dependency change was applied to running trial processes.
- SQLite lifecycle regression: 17 passed, zero failures/errors/skips,
  `.artifacts/s3-capacity-sqlite-20260910-r1.xml` (283.185 seconds).
- Harness regressions: 7 passed, including normal/timeout output-file closure,
  parser preflight, sanitized collection failure before cluster creation,
  child completion/timeout handling, and an actual Windows venv parent/child
  timeout with both processes confirmed exited.
  Evidence: `.artifacts/s3-capacity-harness-20260910-r4.xml` (4.176 seconds).
- Native full selection: `.artifacts/s3-capacity-native-8gw9w9l_/junit.xml`,
  30 tests, 28 passed, two failed, zero errors/skips. Both failures were new
  concurrency test inputs using `operator_containment`, which is not an allowed
  platform-job action reason. Corrected to `operator_cancelled`; no production
  change was required. The invalid request never reached the expected row lock.
- The focused rerun selects only the corrected concurrency cases and the added
  queued forecast cancellation case. `--match` records the pytest filter in
  the report; filtered success must not be labeled a whole-suite rerun.
- Repeated template cloning caused frequent checkpoints with the inherited
  16 MB test WAL limit. The runner now uses a 128 MB maximum and 16 MB minimum;
  durability settings are unchanged. This is test setup tuning, not performance
  measurement or a trial configuration change.

## Final results

- Corrected/new native cases: `.artifacts/s3-capacity-native-yx5z85vx/junit.xml`
  and `report.json`, three passed, zero failures/errors/skips, owned cluster
  stopped. Whole invocation 248.19 seconds, including two native schema builds.
- Together with the unchanged passing cases from the full run, 31 distinct
  native cases have passed. This is cumulative full-run plus focused-rerun
  evidence, not a claim that the earlier 30-case run had no failures.
- Both actual lock orders were observed before releasing the winning
  transaction. Cancellation won with no baseline; completion won with exactly
  one baseline and a revision-conflict response to the late cancellation.
- Total distinct passing cases in scope: native 31, SQLite 17, harness 7.
  Ruff and scoped diff whitespace checks passed. No product change was needed
  for the native assertions; production behavior and the published trial remain
  unchanged by this revision.

## Remaining gates

The response fixtures are protocol/transaction fixtures, not actual HTTP or
performance measurements. This run does not prove Gateway Redis/KMS/object
storage fail policies, 100+ distinct-tenant noisy-neighbor fairness, 2x peak,
72-hour stability, actual DR restoration, or all S3 batch gates.
