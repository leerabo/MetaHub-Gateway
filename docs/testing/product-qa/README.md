# MetaHub Product QA Gate

This is the product-level acceptance gate for WB/API/BB/GR/PERF/WEB/UI/UCD/COMP/SEC/FLOW. The runner groups work into Backend, Performance, and Product Experience lanes; it does not treat static definitions, `not_run`, `skipped`, stale exit codes, or missing stderr evidence as passes.

| Lane | Domains | Evidence |
| --- | --- | --- |
| Backend contract | WB / API / BB / GR / SEC | `tests/product_qa/backend/test_backend_product_qa.py` and fresh SQLite/ASGI JUnit |
| Runtime behavior | PERF | `tests/product_qa/performance/*`, live/pressure JSON, and long-stability JSON |
| Product experience | WEB / UI / UCD / COMP / FLOW | Node contracts, Console lint, and isolated real-browser matrix |

The declared catalog contains 97 cases: WB-01..08, API-01..08, BB-01..14, GR-01..09, PERF-01..10, WEB-01..08, UI-01..07, UCD-01..09, COMP-01..07, SEC-01..07, and FLOW-01..10. Existing PRD tests remain the engineering baseline. PRD-073 is regression context and is not reimplemented here.

## Final grouped run

```powershell
.\scripts\quality\run-product-qa.ps1 -Group All -RunBrowserMatrix `
  -RunLivePerformance -RunPressure -PressureRequests 120 -PressureConcurrency 8 `
  -PressureDurationSeconds 0
```

The final run produced `15/15 tasks passed` at `.artifacts/product-qa/20260820-132657/summary.json`.

After the grouped run and the dedicated long-stability window, audit every catalog case:

```powershell
.\.venv\Scripts\python.exe scripts\quality\product_qa_case_audit.py `
  --evidence-dir .artifacts\product-qa\20260820-132657 `
  --long-stability .artifacts\product-qa\stability-long-awake.json `
  --output .artifacts\product-qa\20260820-132657\case-audit.json
```

The final audit result is `97/97 passed`. It requires zero failed gate tasks, zero skipped JUnit cases, the full 9-browser/DPR matrix, redacted evidence, and `all_108_prds_complete=false`.

## Iteration commands

```powershell
.\scripts\quality\run-product-qa.ps1 -Group Backend
.\scripts\quality\run-product-qa.ps1 -Group Performance
.\scripts\quality\run-product-qa.ps1 -Group Web
```

The full Web matrix is explicit:

```powershell
.\scripts\quality\run-product-qa.ps1 -Group Web -RunBrowserMatrix
```

The loopback performance samples are read-only and reject credentials, query strings, fragments, external hosts, and HTTPS:

```powershell
.\scripts\quality\run-product-qa.ps1 -Group Performance -RunLivePerformance -RunPressure
```

The four-hour observation is a separate dedicated window:

```powershell
.\.venv\Scripts\python.exe scripts\quality\product_qa_stability_gate.py `
  --profile long_stability --run-long `
  --output .artifacts\product-qa\stability-long-awake.json
```

It passes only after 14,000 observed seconds, zero transport/HTTP errors, all 2xx responses, and a maximum observation gap of 10 seconds or less. On Windows the runner holds a system-required execution state to prevent idle standby.

## Acceptance and safety

WB checks tenant schema boundaries and request-scoped SQLite lifecycle safety. API checks unified errors and request correlation. BB and GR exercise role behavior, failure recovery, and privacy. SEC treats tenant escape, authorization bypass, sensitive echo, and unsafe logs as release-blocking. PERF records aggregate p50/p95/p99, throughput, status counts, errors, observation gaps, and data-page navigation latency. WEB/UI/UCD/COMP/FLOW check product language, administrator onboarding, default-collapsed accordion navigation, accessibility, responsive layout, real pointer and keyboard behavior, locale, failure/recovery, and supported browser/DPR combinations.

Backend authorization remains authoritative even when a navigation control is hidden. No evidence contains Provider Key, Token, Prompt, Response, request body, uploaded filename, cookies, screenshots, browser profiles, or database paths. `all_108_prds_complete` remains false in every report.
