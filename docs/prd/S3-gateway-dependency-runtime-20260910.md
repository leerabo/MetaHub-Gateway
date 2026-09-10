# S3 Gateway dependency runtime revision

Status: implementation and focused acceptance complete. Full S3 acceptance
remains open. Records: `docs/testing/PRD-113-gateway-dependency-runtime-20260910.md`
and its companion sensitive audit.
Ownership: PRD-113 actual Gateway dependency behavior, with PRD-108/110 policy
and configuration boundaries preserved. No changes to the published trial.

## Evidence and scope

The rate-limit Redis adapter currently waits indefinitely for connect, AUTH,
SELECT, command response and socket closure. A dependency accepting a connection
without responding does not trigger the intended fail-closed error. It also
rejects `rediss://`, while production configuration explicitly requires TLS.
AUTH/SELECT replies are not checked, and a false PING result is ignored by the
admission service. Fix these actual execution gaps together.

Provider-key selection currently decrypts local AES-GCM ciphertext; ordinary
Gateway calls do not invoke an external KMS. File blobs currently use the local
encrypted blob store. Do not claim either path is an external KMS/S3 fault test.
This host has neither Docker nor an installed WSL/Redis service. No service
installation, reboot, production credential access or trial change is needed
to exercise actual TCP/TLS faults in the existing Gateway client.

## Shared design

- Preserve the existing Redis counter/Batch interfaces and cache ownership.
  Add redis/rediss URL validation and default certificate/hostname validation,
  following the existing session-risk adapter's connection conventions.
- Use a bounded whole-command deadline across connect/TLS, AUTH, SELECT, write
  and complete response read. A peer sending occasional fragments must not
  renew the deadline. Bound close separately and abort a stuck transport.
- Reject non-OK AUTH/SELECT and non-PONG health replies. Strip network and
  server error causes. Cancellation must remain cancellation, not a retryable
  rate-limit error. Connection configuration repr must not contain credentials.
- Bound and validate response framing before allocation/recursion. Preserve
  the Redis response types required by the current counters and Batch Lua.
- Use real loopback TCP/TLS fault probes for withheld AUTH/SELECT/PING, partial
  responses, malformed acknowledgements, reset, certificate errors and recovery.
  These are protocol probes, not a Redis replacement or performance evidence.
- Exercise the actual Gateway HTTP route with its database-backed active
  rule and a blackholed Redis connection; assert bounded safe 503, no upstream
  request, and recorded safe request outcome. A key without limits must remain
  independent of the failed counter dependency.

## Pre-coding review 1: authority and sensitivity

- P0: never disable TLS verification or fall back to plaintext after TLS failure.
  A certificate failure must occur before an AUTH credential is sent.
- P0: replacing Redis with an in-memory allowance would bypass enforcement.
  Existing active-rule fail-closed semantics remain authoritative.
- P1: raw socket/URL exceptions can include credentials. Public store errors
  use a fixed message and suppress the original exception chain.
- P1: a protocol probe proves transport behavior, not Redis durability, Lua
  atomicity, cluster failover or full S3 production-readiness.
- P1: keep synthetic setup isolated and close all listeners, transports and
  background tasks. No existing database, service or retained evidence is reset.

## Pre-coding review 2: deadlines and recovery

- P0: per-fragment deadlines allow a slow peer to hold a request indefinitely.
  Use a single command budget, plus separately bounded transport shutdown.
- P0: waiting indefinitely in cleanup defeats the request deadline. Abort the
  transport when close times out; preserve cancellation and the original result.
- P1: AUTH/SELECT may return valid RESP but the wrong value. Require exact OK;
  a boolean PING failure must not be silently ignored by the caller.
- P1: malformed bulk/array lengths can allocate or recurse before the timer
  fires. Apply size, depth and framing bounds as well as the network deadline.
- P1: current single-dimension INCR/EXPIRE and concurrency sequences are not
  atomic. This revision must not claim recovery of ambiguous counter mutations;
  their atomicity and native Redis acceptance remain explicit follow-on work.

## Verification plan

First reproduce the unbounded wait and TLS rejection, then run affected
transport/counter/Batch tests. Run one isolated full-schema Gateway HTTP fixture
for active-rule failure, no-limit independence, safe persistence and shutdown.
Record PRD-113 tests/security separately and commit only the scoped revision.
Native dependency services, actual fail policies for KMS/object storage, noisy
neighbors, 2x peak, 72-hour stability and PRD-111 restoration remain open gates.

## HTTP finding and reviewed extension

The first full-schema HTTP probe returned the correct 503 but took 4.906 seconds,
failing the unchanged 2.5-second test ceiling. A separate read-only profile of
the same isolated SQLite database took 1.067 seconds, including 0.963 seconds in
SQLite execute and 0.099 seconds closing the schema-bearing connection. Gateway
loaders repeatedly open independent unpooled handles. This is additional local
database overhead, not proof that the Redis deadline failed.

Reuse the existing `database_connection_pool_scope` opt-in at the Gateway ASGI
HTTP boundary. It returns each physical handle only when its logical owner calls
close; it does not share the active connection or transaction across operations.
PostgreSQL already has independent pooled checkouts and remains unchanged.

### Extension pre-coding review 1

- A request-wide shared transaction would change loader/worker ownership and is
  rejected. Reuse only closed handles, preserving rollback and commit guards.
- A pure ASGI wrapper must span the complete response, including streaming and
  background work. Do not stop the scope when response headers are returned.
- Concurrent requests and tasks must acquire independent handles. Context
  inheritance carries an opt-in boolean, never a mutable request connection.
- Do not edit the published trial or reuse its database. The first HTTP fixture
  unintentionally loaded the repo's `.env` through the normal config loader.
  Further fixtures use an empty working directory and explicit synthetic secrets.

### Extension pre-coding review 2

- Scope reset must run on exception, cancellation and disconnect; non-HTTP
  ASGI traffic must preserve its original behavior.
- Test rollback on close and dynamic commit rejection through the existing
  shared SDK tests, plus concurrent, streaming and cancellation wrapper tests.
- Keep the original 2.5-second HTTP ceiling. Record the measured result; do not
  label this targeted route fixture a full-worker or production SLO benchmark.
- Include the existing uncommitted shared pooling helper and its focused tests
  as an explicit dependency of this revision, without any other trial edits.
