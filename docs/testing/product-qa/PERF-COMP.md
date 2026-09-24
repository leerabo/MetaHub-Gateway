# Product QA: PERF / COMP

## Safety boundary

These assets establish a repeatable local baseline, not a production load tool. Isolated tests use fresh SQLite and ASGI transport. Runtime samples use only unauthenticated `GET` requests to loopback health pages; credentials, HTTPS, external hosts, query strings, fragments, cookies, bodies, and mutations are rejected. Evidence contains aggregate timings/statuses only.

## Assets

| Group | Asset | Verification |
| --- | --- | --- |
| PERF-01 / PERF-09 | `tests/product_qa/performance/test_product_qa_performance.py` | isolated ASGI fan-out, empty SQLite migration, integrity, FK, request reuse, and physical close |
| PERF-03 / PERF-06 / PERF-07 | `scripts/quality/product_qa_performance.py` | bounded loopback smoke, pressure, and post-pressure health |
| PERF-08 / PERF-10 | `scripts/quality/product_qa_continuous_stability.py` | PRD-148: at least 72 hours, completed non-diagnostic status, max observation gap, window hash chain, source revision and redacted evidence |
| COMP-01..07 | `tests/product_qa/web/*` and `realBrowserJourneys.mjs` | browser, viewport, DPR, locale, recovery, encoding, and layout execution |
| All 97 cases | `scripts/quality/product_qa_case_audit.py` | plan-to-evidence mapping; rejects missing, skipped, failed, stale, or sensitive evidence |

## Historical evidence (2026-08-20)

The historical grouped artifact is `.artifacts/product-qa/20260820-132657/`. Its four-hour window, old migrations and missing current source manifest do not satisfy PRD-148. Use the current commands in [README.md](README.md) for a new batch; do not reseal historical evidence.

| Run | Requests | Successes | Errors | Result |
| --- | ---: | ---: | ---: | --- |
| Live smoke | 30 | 30 | 0 | passed |
| Pressure, concurrency 8 | 120 | 120 | 0 | passed |
| Short stability | 4,322 | 4,322 | 0 | passed |
| Long stability | 112,157 | 112,157 | 0 | passed |

Long stability observed 14,400.467 seconds, maximum observation gap 0.622 seconds, and `sleep_prevention_active=true`. It passed only because every response was HTTP 200 and no transport errors occurred.

## Historical reproduction commands (not the current GA gate)

```powershell
C:\MetaHub\.venv\Scripts\python.exe -m pytest -q --basetemp C:\MetaHub\.tmp\product-qa-perf C:\MetaHub\tests\product_qa\performance\test_product_qa_performance.py C:\MetaHub\tests\product_qa\performance\test_product_qa_stability_gate.py

powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\MetaHub\scripts\quality\run-product-qa.ps1 -Group All -RunBrowserMatrix -RunLivePerformance -RunPressure -PressureRequests 120 -PressureConcurrency 8 -PressureDurationSeconds 0

C:\MetaHub\.venv\Scripts\python.exe C:\MetaHub\scripts\quality\product_qa_stability_gate.py --profile long_stability --run-long --output C:\MetaHub\.artifacts\product-qa\stability-long-awake.json
```

The historical profile used the 14,000-second floor. The current PRD-148 audit requires 259,200 observed seconds and rejects this profile as GA evidence. Windows observation holds `SetThreadExecutionState` to prevent idle standby.

## Compatibility matrix

| ID | Environment | Executed | Acceptance |
| --- | --- | --- | --- |
| COMP-01 | Chromium | 3 DPRs x 3 journeys | login, collapse, navigation, validation, logout |
| COMP-02 | Edge | 3 DPRs x 3 journeys | same flow, zero console/request failures |
| COMP-03 | Firefox | 3 DPRs x 3 journeys | same flow, focus and responsive behavior |
| COMP-04 | 100/125/150% DPR | all three browsers | labels and controls remain reachable |
| COMP-05 | en-US and zh-CN | all 9 artifacts | no overlap, clipping, or internal primary guidance |
| COMP-06 | injected service failure | admin journey in all 9 artifacts | safe error/request ID and recovery |
| COMP-07 | UTF-8/CSV | Backend/Web contracts | BOM, CRLF, safe filename, formula protection |

All 27 journeys passed with zero unexpected console errors, request failures, horizontal overflow, clipped controls, and interactive overlaps. Across the 36 measured Provider, Usage, Project, and Virtual Key data-page transitions, latency was 484-1,675 ms with a 1,101.2 ms average and every sample below the 4,000 ms release limit. The 97-case report is `.artifacts/product-qa/20260820-132657/case-audit.json`.
