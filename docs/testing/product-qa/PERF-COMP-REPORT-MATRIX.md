# PERF / COMP Evidence Matrix

This matrix records the final executed PERF and COMP evidence. `not_run` and `skipped` are never treated as passes.

| Area | Profile / check | State | Evidence | Result |
| --- | --- | --- | --- | --- |
| PERF-01 | Isolated ASGI health fan-out | passed | final `performance-junit.xml` | 24 concurrent requests, zero errors |
| PERF-03..05 | Loopback smoke | passed | `performance-live.json` | 30/30 HTTP 200, zero errors; p95 <= 15.144 ms |
| PERF-06..07 | Loopback pressure | passed | `performance-pressure.json` | 120/120 HTTP 200, concurrency 8, zero errors; p95 <= 39.100 ms |
| PERF-08 | Short stability | passed | `stability-short-awake.json` | 8.006 seconds, 4,322/4,322 successful |
| PERF-08 / PERF-10 | Long stability | passed | `stability-long-awake.json` | 14,400.467 seconds, 112,157/112,157 successful, zero errors |
| PERF-09 | Empty SQLite migration and request-lifecycle health | passed | final PERF JUnit and browser JSON | 82 migrations, latest `0082`, integrity `ok`, FK 0 |
| COMP-01 | Chromium | passed | 3 browser JSON artifacts | DPR 1/1.25/1.5; 9 journeys |
| COMP-02 | Edge | passed | 3 browser JSON artifacts | DPR 1/1.25/1.5; 9 journeys |
| COMP-03 | Firefox | passed | 3 browser JSON artifacts | DPR 1/1.25/1.5; 9 journeys |
| COMP-04 | Display scale | passed | 9 browser JSON artifacts | DPR 1, 1.25, and 1.5 |
| COMP-05 | Locale and long text | passed | 9 browser JSON artifacts | en-US admin; zh-CN developer/viewer |
| COMP-06 | Failure and recovery | passed | admin journey in each artifact | onboarding, accordion, bounded data navigation, injected failure, request ID, recovery |
| COMP-07 | UTF-8/CSV safety | passed | Backend/Web JUnit | BOM, CRLF, formula protection, safe filename |

All JSON evidence is aggregate and redacted. It contains no URLs, cookies, credentials, Authorization headers, tenant identifiers, request/response bodies, Provider Keys, Prompts, Responses, database paths, or uploaded filenames. The independent 97-case audit is `.artifacts/product-qa/20260820-132657/case-audit.json`.
