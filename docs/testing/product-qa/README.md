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
  -RunLivePerformance -RunPressure
```

The runner writes a new timestamped directory and an `evidence-manifest.json` binding all output files to source snapshots captured before and after the batch. Both HEAD and source bytes (including uncommitted source files) must match. Changing source during execution invalidates the batch. The pressure gate uses 600 requests by default. SQLite acceptance now requires 153 applied migrations through 0155.

The `15/15` run in `.artifacts/product-qa/20260820-132657/summary.json` is historical. It lacks current source provenance and cannot certify PRD-148.

After the grouped run and the dedicated long-stability window, audit every catalog case:

```powershell
.\.venv\Scripts\python.exe scripts\quality\product_qa_case_audit.py `
  --evidence-dir <current-batch-directory> `
  --long-stability <completed-continuous-observation-directory>\status.json `
  --output <current-batch-directory>\case-audit.json
```

The acceptance target is `97/97 passed`; this is not a statement that the current batch has passed. The audit requires the exact 15-task set, current source and artifact hashes, zero failed gate tasks, zero skipped JUnit cases, the full 9-browser/DPR matrix, redacted evidence, and `all_108_prds_complete=false`. Missing source manifests cannot be reconstructed after a run to turn old outputs into current evidence. Passing this audit is local product QA, not independent GA approval.

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

PRD-148 uses a separate 72-hour continuous observation. Start it once for the release under observation; keep an existing healthy process running:

```powershell
.\.venv\Scripts\python.exe scripts\quality\product_qa_continuous_stability.py `
  --target console=http://127.0.0.1:5173/ `
  --target control=http://127.0.0.1:8001/health `
  --target gateway=http://127.0.0.1:8000/health `
  --source-revision (git rev-parse HEAD) `
  --output-dir <new-continuous-observation-directory> --duration-seconds 259200
```

It passes only after at least 259,200 observed seconds, zero transport/HTTP errors, and a maximum observation gap of 10 seconds or less. The audit checks the `continuous-availability.v1` schema, source revision, completed non-diagnostic status, target counts and adjacent `windows.jsonl` hash chain. A four-hour profile, an unfinished process, or a diagnostic run cannot satisfy GA long stability. On Windows the runner holds a system-required execution state to prevent idle standby. Local HTTP availability does not establish business-flow, deployment, rollback, or independent approval facts.

## Acceptance and safety

WB checks tenant schema boundaries and request-scoped SQLite lifecycle safety. API checks unified errors and request correlation. BB and GR exercise role behavior, failure recovery, and privacy. SEC treats tenant escape, authorization bypass, sensitive echo, and unsafe logs as release-blocking. PERF records aggregate p50/p95/p99, throughput, status counts, errors, observation gaps, and data-page navigation latency. WEB/UI/UCD/COMP/FLOW check product language, administrator onboarding, default-collapsed accordion navigation, accessibility, responsive layout, real pointer and keyboard behavior, locale, failure/recovery, and supported browser/DPR combinations.

Backend authorization remains authoritative even when a navigation control is hidden. No evidence contains Provider Key, Token, Prompt, Response, request body, uploaded filename, cookies, screenshots, browser profiles, or database paths. `all_108_prds_complete` remains false in every report.
