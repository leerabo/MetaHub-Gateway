# PRD-113 leased dependency fault proxy

Design and two pre-coding reviews: `docs/prd/S3-fault-lease-proxy-20260910.md`.
This is a component execution record, not a full S3 acceptance record.

## Verification

The focused suite tests real bytes through ephemeral loopback TCP listeners:
normal/large streaming, half-close, timeout/release on existing connections,
expiry without heartbeat, idle connection reset, new-connection recovery,
connection limits, store failure, graceful shutdown and abrupt process death.
The crash test terminates only its own multiprocessing child, checks the active
durable lease survived the crash, restarts the proxy on the same journal, then
proves the old lease cannot resume and a new real connection forwards data.

Store/protocol tests cover duplicate/reordered/conflicting commands, release
before apply, concurrent requests, binding ownership, original duration, clock
rollback, bounded forward skew, startup expiry, restart fencing, private-IP and
fixed-manifest validation, deployment credential separation, API authentication,
request-size bounds, request IDs, and redacted/no-store failure responses.

The original five runner tests remain included to ensure authentication and kill
fencing still work and HTTP cannot report simulated measurements as real runs.

Commands (repository root, fresh disposable pytest base directory):

```text
.tools/Python312-x64/python.exe -X utf8 -m pytest
  control-plane-api/tests/unit/test_capacity_fault_proxy.py
  control-plane-api/tests/unit/test_prd113_capacity_runner.py
  --basetemp=<fresh-temporary-parent>/pytest
  --junitxml=.artifacts/prd113-fault-proxy-20260910-final-r2.xml
.venv/Scripts/ruff.exe check <four fault modules and focused test file>
.venv/Scripts/ruff.exe format --check <four fault modules and focused test file>
git diff --cached --check
```

Initial component run: 20 passed. Extended run: 26 passed, one test-fixture error
because the echo peer did not handle Windows connection reset during cleanup.
The fixture now handles that expected reset while retaining process termination,
durable-state and actual recovery assertions. Subsequent run: 28 passed.
Request-limit/manifest additions and the original runner tests: 36 passed.
Final run: **37 passed** (32 fault-proxy tests plus five existing runner tests),
no failures or skips, in 10.55 seconds. Evidence:
`.artifacts/prd113-fault-proxy-20260910-final-r2.xml`.
Ruff check and formatting checks passed for all five Python files.

## Not yet proved

Runner renewal/release integration, response-before-fault/reversal ordering,
continuous stop conditions and independent target recovery, resource pressure,
actual Gateway dependency fail policies and 100+ distinct tenants, 2x peak,
72-hour soak, PostgreSQL 0159 execution/concurrency, and PRD-111 restore evidence.
No frontend change, live DB migration, paid provider call, or trial restart.
