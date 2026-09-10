# PRD-113 fault proxy sensitive audit

Scope: four new `metahub_capacity_runner/fault_*.py` modules, their focused test,
joint design supplement and PRD-113 records. Existing trial edits are excluded.

- Deployment configuration exclusively owns IPs, ports and credential paths.
  Hostnames, public/wildcard/metadata addresses, looped listeners, unknown fields
  and resource-pressure commands are rejected. No shell or subprocess execution
  exists in the service. Only the test starts/terminates an owned test process.
- Internal bearer and permit-signing credentials come from separate files, are
  never returned, and cannot be equal. Requests are signed for a separate domain.
- Journal fields contain object/execution/config/command digests, fixed binding
  and fault codes, bounded sequence numbers, state and timestamps. No request or
  response payload, customer identity, model key, raw permit or credential.
- TCP bytes are forwarded in bounded chunks, with backpressure and connection
  caps; no body logger, packet dump or retained traffic evidence is implemented.
- HTTP bodies, including chunked input, are limited to 16 KiB. Validation errors
  return fixed codes and generated request IDs, not the invalid input. Responses
  are no-store. Raw SQLite/secret-file errors are not sent to callers.
- Cancellation tombstones survive restart. Leases expire independently of the
  runner and cannot be revived by late commands. Restart ends active leases;
  store failure closes connections and is not reported as target recovery.
- Tests use generated synthetic credentials and ephemeral local ports. They do
  not read or send the user's provided provider keys or touch trial identities.
- Live verification retained frontend PID 11248 and Control PID 17220, with
  Control health `ok`. No trial migration or process restart was performed.
- Targeted source/test/record scan found no provider-key/private-key patterns or
  command-evaluation calls. Its only match was this record's prose stating that
  the service does not execute subprocesses. All 37 affected tests passed.

Remaining security gates: actual runner/Control stop-result integration, topology
and deployment isolation verification, production dual approval, resource pressure
adapter, operational fault-effect auditing, and full S3 recovery/security tests.
