# S3 native capacity runtime verification

Status: design and two pre-coding reviews complete; focused acceptance passed.
Scope: PRD-106/113 PostgreSQL 0159 migration, external execution ownership,
atomic domain projection and cancellation concurrency. This does not replace
the separate Gateway dependency, DR, performance or 72-hour S3 gates.

## Shared design

Reuse the existing capacity lifecycle scenarios against native PostgreSQL, not
an SQL parser or SQLite emulator. A seeded native template contains no capacity
jobs. Each scenario clones it into a uniquely named disposable database; no
case can accidentally observe another case's root job or idempotency record.
Reuse the same platform-operator setup for SQLite and PostgreSQL.

Run 0159 down/up with no capacity facts, verify an idempotent migration replay,
and verify down refuses retained capacity facts without deleting them. Native
tests inspect the installed trigger and transaction outcomes, not just SQL text.
Add competing cancellation/completion and exclusive external-claim scenarios
with separate connections, actual row locks and bounded synchronization.

The local runner may start only a new cluster or an explicitly named, currently
stopped test cluster under `.tmp`. Bind loopback on an available port, generate
synthetic seed credentials in the child environment, and pass only test database
URLs. Test helpers already provide structured database-name quoting and close
pools before cloning/deleting a database created by that test.

Persist JUnit and a small aggregate execution report with migration version,
results and owned-instance shutdown. No password, seed key, payload, permit or
database connection string belongs in the report. A skipped native suite is
missing evidence, not success. The published trial is excluded.

## Pre-coding review 1: ownership and isolation

- P0: never point these tests at the trial database or an existing shared server.
  Validate the explicit cluster path and stopped state before any startup action.
- P0: test deletion must target only a UUID-named database created by its fixture.
  Use the existing native database helper rather than string-built SQL names.
- P1: resetting a global test table can hide cross-case errors. Isolate complete
  databases while sharing the seeded template and fixture logic.
- P1: lifecycle response fixtures test transaction behavior only. They cannot
  establish actual HTTP fault effects, performance, Gateway or DR correctness.

## Pre-coding review 2: races and proof

- P0: cancellation must acquire the root job lock before a domain update;
  late success cannot create a baseline once cancellation owns the root.
- P0: failed domain completion must roll back the job, attempt, effect, wait and
  baseline together. Re-observation must safely finish the same attempt.
- P1: use database clocks in native concurrency tests and explicit barriers or
  events. Existing accelerated-clock scenarios remain lifecycle checks only.
- P1: down/up must preserve immutable migration facts and reject down with
  retained capacity jobs; never drop business facts to make rollback pass.
- P1: verify final process exit and keep failed-run reports. Do not restart a
  still-running instance because an observation timed out.

## Verified implementation

The native lifecycle suite and shared operator fixture are implemented. Across
the retained full run and corrected focused rerun, 31 distinct native cases
passed, including all three capacity terminal-projection branches. Affected
SQLite cases (17) and harness cases (7) also passed without skips. The harness
tests include a real Windows launcher/child timeout and confirmed tree exit.

All owned clusters are stopped. Failed setup and test runs remain recorded.
No trial service restart, trial migration or model call occurred. See the
independent PRD-113 native-runtime testing and sensitive-audit records. These
results do not complete the outstanding S3 runtime, DR or performance gates.
