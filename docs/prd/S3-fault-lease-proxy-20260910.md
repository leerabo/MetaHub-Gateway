# S3 leased fault injection

Status: proxy component implemented and tested; runner integration remains open.
This extends PRD-106/111/113 execution design,
not the S3 completion gate. Source: incremental workbook `01_PRD总表!A18:Y25`.
The workbook's historical completion cells do not prove the missing runtime gates.

## Shared design

Real dependency faults need a deployment-owned TCP proxy, separate from the load
executor and Control. Each listener has one private-IP upstream and a closed list
of permitted fault codes. Requests never supply addresses, credentials, scripts,
filesystem paths, or commands. Timeout blocks byte forwarding; unavailability
closes connections. Resource pressure needs another adapter and remains rejected.

The proxy accepts short signed commands tied to the complete capacity execution,
monotonic sequence, action and absolute expiry. Its own SQLite journal serializes
apply/renew/release, preserves cancellation tombstones, and bounds each experiment
by its original duration. Renewals cannot revive expired or released executions.
Replaying a command cannot extend its lease. At most one fault owns a binding.

The TCP forwarding path observes lease expiry independently of the load executor.
The proxy never reinstates faults after restart. A single process lock prevents
one process from clearing a live owner's leases. On proxy/host failure, sockets
close; availability is not promised until the proxy returns. Store failure closes
connections and must not be called successful reversal or target recovery.

The API is `POST /internal/capacity/faults`, with an independent bearer credential
and a signed `fault-lease` command (`apply`, `renew`, `release`, `status`). Maximum
body size is 16 KiB, including chunked requests. Validation errors never echo the
permit. Responses are no-store and failures include a generated request ID.

`metahub_capacity_runner.fault_app:app` reads the JSON path from
`METAHUB_CAPACITY_FAULT_SETTINGS_FILE`. It requires a dedicated absolute state
directory, independent absolute internal-token/signing-secret file paths, and a
nonempty binding list. Run one worker on a private management interface. Each
binding names the service/environment/region, fixed private listen/upstream IPs
and ports, supported fault codes, and maximum connections. No new public endpoint
or trial deployment has been added in this revision.

Do not enable real chaos in the runner until it can renew/release these leases,
measure actual requests, wait for reversal, and probe recovery independently.
`runner_requests stopped`, `fault no longer injected`, and `target recovered` are
different facts. Neither a lease response nor a TCP proxy test proves Gateway's
dependency fail policy, tenant isolation, DR consistency, or a 72-hour soak.

## Pre-coding review 1: authority and delayed messages

- Fixed deployment bindings and existing fault manifests bound target selection.
- Signed command expiry is at most 30 seconds and cannot extend the original run.
- Persist before applying. Release before apply leaves a tombstone; late apply
  cannot inject after cancellation. Same sequence with changed content conflicts.
- A renewal after expiry cannot resurrect a fault even if no timer has swept it.
- No proxy-wide reset and no customer keys, payloads or raw permits in the journal.
- Reject duplicate binding codes/listeners and self-forwarding loops at startup.

## Pre-coding review 2: failure and evidence

- Streaming uses bounded chunks and drain backpressure; idle reads periodically
  check fault state, so already-open connections observe reset and lease expiry.
- A timed-out command reply does not authorize a second independent experiment.
  Durable object/digest/sequence binding makes retries idempotent.
- A dedicated lifetime lock fences startup recovery. Graceful shutdown closes
  listeners and active streams. Crash closes sockets; restart ends active leases.
- Wall-clock leases are conservatively capped by a monotonic process deadline,
  so a backward clock jump cannot keep a fault active indefinitely.
- Tests must send real bytes through ephemeral loopback listeners, exercise lease
  expiry, release, replay, restart and competing owners, and retain no payloads.

## Remaining integration

PRD-113 executor renewal/reversal/recovery measurements; PRD-106 external stop
projection; PRD-111 actual restore checks; production deployment and full S3 gates.
Do not touch the published trial processes, database, credentials or providers.
