# Product QA Backend: WB/API/BB/GR/SEC

## Scope

This executable suite is the product-level complement to PRD-specific tests.
It uses a fresh SQLite database and FastAPI ASGI transport, so it does not
depend on, or modify, a developer's running Control API or local data.

| Group | Script coverage | Evidence |
| --- | --- | --- |
| WB | `test_white_box_all_core_business_tables_carry_tenant_id`; `test_white_box_sqlite_request_scope_restores_connection_local_safety_state` | schema and request-scoped connection-local PRAGMA state |
| API | `test_api_unified_error_contract_preserves_client_request_id` | public error payload, `request_id`, and `trace_id` |
| BB | tenant update and backend RBAC tests | status, stable error code, isolated data |
| GR | failed-login privacy test | HTTP response plus structured log capture |
| SEC | all tests above plus CSV evidence | tenant boundary, deny-by-backend, no input echo, request-lifecycle safety |

## Preconditions and cleanup

The tests create their database inside pytest's `tmp_path`. They seed only a
test admin identity and use synthetic values. Access tokens remain in process
memory and are never rendered in assertions, logs, screenshots, or reports.
Pytest removes the temporary database after the run.

## Run

Use a repository-local pytest base directory on this Windows workstation:

```powershell
C:\MetaHub\.venv\Scripts\python.exe -m pytest -q --basetemp C:\MetaHub\.tmp\product-qa-backend C:\MetaHub\tests\product_qa\backend\test_backend_product_qa.py
```

## Acceptance criteria

All eight backend tests must pass. A missing `tenant_id`, a non-uniform error response,
a cross-tenant write, an RBAC bypass, a private input in response/log capture,
or a request-scoped connection leaking read-only state into the next request is release-blocking.
