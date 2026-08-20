# Product QA: WEB / UI / UCD / COMP / FLOW

## Scope

This suite is the product acceptance layer above PRD engineering tests. It validates visible behavior, accessibility, responsive layout, user comprehension, supported browsers, and complete journeys without exposing credentials, Provider Keys, Prompts, Responses, or file contents.

## Executed assets

| Area | Asset | Final evidence |
| --- | --- | --- |
| WEB/UI/UCD | `tests/product_qa/web/productQaContracts.test.mjs` | 16 Node tests passed, 0 failed, 0 skipped, 0 todo |
| FLOW definitions | `humanMouseJourneys.test.mjs`, `serviceRecoveryFlow.test.mjs` | included in the 16-test Web contract task |
| CSV | `csvExportEvidence.test.mjs` | UTF-8 BOM, CRLF, formula-safe cells, safe filename and content type |
| Real FLOW/COMP | `realBrowserJourneys.mjs` via `product_qa_browser_runtime.py` | 9 browser/DPR artifacts, 27 passing journeys |
| Static quality | `console-web/scripts/lint.mjs` | 299 files linted successfully |

## Browser matrix

| Browser | DPR | Viewports per artifact | Result |
| --- | --- | --- | --- |
| Chromium | 1 / 1.25 / 1.5 | 1280x720, 1440x900, 390x844 | 9/9 journeys passed |
| Edge | 1 / 1.25 / 1.5 | 1280x720, 1440x900, 390x844 | 9/9 journeys passed |
| Firefox | 1 / 1.25 / 1.5 | 1280x720, 1440x900, 390x844 | 9/9 journeys passed |

Each artifact uses a disposable SQLite database with 82 applied migrations, latest `0082`, `integrity_check=ok`, and zero foreign-key violations. Each role also records `keyboard_navigation_passed=true` and `native_validation_passed=true`.

Across all 27 journeys there were zero unexpected console errors, zero unexpected request failures, zero horizontal overflow, zero clipped controls, and zero interactive overlaps. Intentional AbortController cancellations are tracked separately.

## Journey coverage

| Journey | Role | Viewport | Locale | Covered behavior |
| --- | --- | --- | --- | --- |
| FLOW-01 | admin | 1280x720 | en-US | keyboard/native validation, mouse login, onboarding, default-collapsed accordion, Providers, Usage, bounded data latency, injected 503, request ID, recovery, logout |
| FLOW-02 | developer | 1440x900 | zh-CN | keyboard/native validation, default-collapsed accordion, Projects, Virtual Keys, bounded data latency, product-language and sensitive-text checks, logout |
| FLOW-03 | viewer | 390x844 | zh-CN | keyboard/native validation, collapsed mobile navigation, read-only surface, no sensitive text, logout |

The admin flow performs a deliberate service interruption and recovery in every browser/DPR artifact. It asserts a user-visible support-safe `request_id` and a usable recovered shell. The browser harness never logs request bodies or credentials.

## UI/UCD acceptance

The executed checks cover: keyboard-reachable labelled controls; sidebar `aria-controls` and `aria-expanded`; stable collapsed width; product labels instead of PRD/migration/table/source internals; password input semantics; loading/error/empty/retry states; role-specific navigation; mobile reachability; English/Chinese text; safe request IDs; and backend authorization as the source of truth.

All nine admin journeys displayed the enterprise initialization guide. The 36 measured data-page transitions were 484-1,675 ms with a 1,101.2 ms average; all stayed below the 4,000 ms release limit.

UCD here is an executable acceptance test, not a substitute for a separate moderated user-research study. The final 97-case audit is `.artifacts/product-qa/20260820-132657/case-audit.json`.
