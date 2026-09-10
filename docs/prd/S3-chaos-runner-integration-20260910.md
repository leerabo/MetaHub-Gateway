# S3 chaos runner integration

Status: implementation and affected verification complete (205 tests passed).
PRD-106/111/113 shared execution design. This is not a completed S3 gate.
Prerequisite: `S3-fault-lease-proxy-20260910.md` and commit `e9302df`.
This revision does not change the running trial or waive any S3 acceptance gate.

## Execution and evidence

Deployment selects a closed fault binding and a separately configured probe target
for each service/environment/region/fault tuple. A shared dependency fault still
probes a concrete Gateway/Control service. All addresses, credentials, model names,
probe rates and recovery limits remain deployment-owned, never request-supplied.

The runner persists original fault binding/credential references under the original
execution and generation before sending an apply. Apply is sequence 1; bounded
renewals advance sequences. Cleanup uses the reserved final sequence. Status and
cleanup retain the original execution digest and never reapply on recovery.
Unknown responses cause stop/reconciliation, not an independent fault experiment.

Real chaos requires baseline requests, independently renewed short leases, real
request aggregation, observed proxy effects, stop-condition monitoring, explicit
reversal confirmation and fresh recovery requests. Failed/cancelled experiments
can stop safely without claiming recovery. Only a measured complete run with
proven injection, reversal and recovery may return a successful chaos result.
No payloads, credentials or customer identity are retained as measurement evidence.

Runner restart leaves unreconciled fault ownership nonterminal. Before another
execution it queries/releases the original binding, even if it disappeared from
current settings. File references must remain mounted until cleanup completes.
An unavailable proxy never becomes confirmed cleanup merely because a timer has
elapsed. The proxy's own lease expiry limits effects independently, but Control
still needs a bound reply before projecting terminal state.

Runner request termination, fault reversal, and target recovery are distinct wire
facts. Control rejects measured chaos results missing the new evidence and refuses
terminal chaos status whose stop scope only covers runner HTTP requests. Existing
load results and explicit test-loopback simulation retain their separate contracts.

## Pre-coding review 1: ownership and ordering

- P0: restart currently terminates running rows before reversing faults. Persist
  fault ownership before apply, retain pending cleanup across restart, and guard
  terminal database transitions until reversal is acknowledged.
- P0: cancellation overtaking an unknown apply must install a remote tombstone.
  Cleanup always uses the final sequence and original complete execution binding.
- P1: settings drift must not strand an earlier effect. Persist original private
  service configuration and credential references, not credential contents.
- P1: bounded signature expiry, no proxy/redirect inheritance, size-limited replies,
  exact execution/binding verification, and fresh signed status protect replay.
- P1: no fault renewal after cancellation; a final cleanup sequence fences any
  already-in-flight renewal even when its HTTP response arrives late.

## Pre-coding review 2: proof, failure and compatibility

- P0: fault apply acknowledgment alone is not proof of injected traffic. Capture
  aggregate proxy effects and inspect real target outcomes, not target parameters.
- P0: recovered proxy does not imply recovered business service. Fresh successful
  probes must follow confirmed reversal; cancellation cannot manufacture recovery.
- P1: stop-triggering error rates and lease failures stop request tasks first,
  close connections, then reconcile faults. Missing cleanup remains nonterminal.
- P1: bounded retries per reconciliation cycle keep shutdown finite without
  discarding the remaining durable responsibility.
- P1: new measured-chaos evidence is mandatory; old/simulation evidence cannot
  establish production success. No compatibility default may assert fault reversal.
- P1: migrations belong only to the isolated runner/proxy journals. Preserve live
  Control schema 0150 and keep original load/cancel regression coverage focused.

## Verification plan

Affected protocol/journal tests, real TCP fault and HTTP probe integration, lost
apply/release replies, crash between apply and acknowledgment, cancellation races,
missing telemetry/recovery, settings drift, and terminal-status validation. Retain
independent PRD-113 test/security records. Full S3 database, browser, real Gateway,
100+ tenant, peak/soak and actual DR restore gates remain separate and required.

## Implemented details

- The runner journal is version 2. Original fault bindings and credential file
  references are saved before apply. SQL guards reject terminal transitions while
  fault reversal is unconfirmed. Restart fences the old generation without
  losing pending cleanup or an already-persisted user cancellation.
- The proxy journal and response protocol are version 3. Aggregate observations
  include reset connections, withheld bytes and affected connections still open.
  Old observation counters survive the additive journal upgrade.
- Release is not sufficient while affected connections remain. Held timeout
  requests are discarded on expiry/release, never resumed. Socket-close timeout
  triggers transport abort and a second bounded confirmation. Unconfirmed close
  or failed counter persistence leaves durable responsibility outstanding.
- The client uses fresh domain-separated signatures, no environment proxy or
  redirects, a 16 KiB response cap, 3-second exchanges and an 8-second cleanup
  cycle. An unknown apply is fenced with final sequence 2147483647. Lost release
  replies are recovered by status, not by replaying a new final-sequence command.
- Measured results need healthy baseline probes, real observed fault traffic,
  requested duration, confirmed reversal, and three consecutive healthy recovery
  rounds. Target-process resource windows and fixed aggregate outcomes are kept
  on failures as well as completed experiments.
- Actual HTTP tests use an independent authenticated target process, the fault
  service on ephemeral sockets, and the runner's real background execution.
  Cancellation, missing proof, failed apply reply, unobserved effects, target
  failure after reversal and abrupt runner death are exercised separately.

Validation and limitations are recorded independently under
`docs/testing/PRD-113-chaos-execution-20260910.md` and its sensitive audit.
