# PRD-113 chaos execution sensitive audit

Scope: runner/fault integration, affected Control result validation, targeted tests
and independent design/test records. Unrelated trial changes are excluded.

- Remote requests carry a closed execution permit and binding code, never
  caller-selected network addresses, filesystem paths, scripts or commands.
  Deployment-only bindings use private IP origins and separate credential files.
- Runner, fault-control and synthetic target credentials remain distinct.
  Fault requests are signed in a separate domain; responses validate execution
  digest, binding, sequence, lease time and required protocol version.
- HTTP clients disable environment proxies and redirects. Response sizes and
  request deadlines are bounded. Error codes do not include reply bodies,
  request permits, secret file contents or raw underlying exceptions.
- The private runner journal intentionally retains original deployment endpoint
  and credential file references to recover after configuration drift. These
  are not secret values and are never exposed through public DTOs. Journal
  storage must remain restricted to the runner deployment identity.
- Request and response bodies are not retained. Real test journals are checked
  against synthetic credentials and a private-response marker. Evidence contains
  fixed outcomes, counts, histograms, resource windows and configuration hashes.
- Unconfirmed fault reversal prevents terminal state, including after restart.
  A successful lease release with nonzero affected connections is insufficient.
  Held traffic is discarded on release/expiry; failed close confirmation or
  failed detach persistence cannot manufacture zero outstanding connections.
- Successful chaos evidence additionally requires actual fault observations and
  independent recovery probes. Simulation and protocol fixtures are not used
  as production resilience evidence. Cancellation does not claim target recovery.
- Test process termination targets only a process object created by that test.
  Tests neither restart published trial services nor read user provider keys.

Final scan: 19 source/test/record files, zero provider-key/private-key/command
execution pattern matches. Ruff, compileall and scoped whitespace checks passed.
The final affected run passed all 205 tests without failures or skips, including
nine real HTTP scenarios and 17 database integration tests. The only warning is
the existing Starlette/httpx test-client deprecation. All owned test processes
exited. Trial frontend PID 11248 and Control PID 17220 remain unchanged; Control
health is `ok`. No trial migration, provider call or credential access occurred.

Remaining audits include real Gateway fail policies, production dual approvals,
full deployment isolation, DR consistency and the complete S3 security gate.
