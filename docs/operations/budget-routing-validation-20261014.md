# Budget and routing validation — 2026-10-14

## Result

- Budget form uses “组织” for the existing tenant scope. API scope values remain compatible.
- Business budgets expand organization → project → application. Personal key budgets have a separate table. Applications without caps are explicitly labelled.
- Active threshold/exhausted budgets appear in a visible alert area. Alert-only explicitly allows continued spending; block performs estimated-cost admission before upstream calls.
- Request details show budget decisions independently of the request's actual failure cause.
- Budget writes reject parent caps below the sum of active direct child caps, atomically, for the same environment and day/month/year period. Personal caps are independent. Existing invalid allocations are not silently altered.
- Low-cost template requests re-rank qualified models using current effective prices, workspace FX and estimated request usage. The preview model is a reference, not a permanent binding.
- Provider actions use compact buttons and a top-layer dropdown; both themes were visually inspected.

## User's reported request

The recorded failure was `gateway.upstream_stream_timeout`. Its budget decision was exhausted/allow: the rule was alert-only. This distinguishes the actual streaming timeout from an exhausted budget that did not block the call. Alert events for 80% and exhaustion were already present in the original database; the page now makes current alerts visible.

The existing organization day cap is 1 CNY while a project day cap is 5 CNY. The new save validation requires increasing the parent or reducing child allocations; this work did not change the user's caps or enforcement choice.

## Verification

- 69 targeted frontend unit tests passed.
- 76 shared routing tests passed.
- New allocation integration test passed (including parent totals, application totals, different periods, disabling and re-enabling, and transaction rollback).
- New dynamic-price integration test passed, including API selection before and after a price update and no upstream attempt on a blocking budget.
- Isolated ASGI API verification returned 402 with budget failure fields and zero upstream attempts.
- Isolated guard verification reached 80%, allowed calls, and produced exactly one deduplicated alert event.
- Frontend lint, production surface verification and build passed: 305 shipped assets.
- API 8001 and gateway 8000 restarted with the original database/configuration; health endpoints returned 200. Frontend 5173 returned 200.

## Test limitation

Two existing guard smoke tests (`test_budget_block_stops_before_upstream_and_logs_reason`, `test_budget_warning_event_is_deduplicated_per_day`) did not pass. Their initial fixtures used an unbound seeded key; attempts to adapt their historical fixtures did not resolve the failures. Those exploratory fixture changes were removed. The complete legacy guard suite is not claimed to pass. The isolated guard/API checks above supplement, rather than replace, that missing regression coverage.

### PostgreSQL and fixture follow-up — 2026-10-15

The shared cost-rate resolver now uses backend-aware schema inspection instead of querying SQLite's catalog on PostgreSQL. A real native PostgreSQL check used a separate acceptance schema with native column types and checks, verified five price observations across three immutable versions (two version transitions), timezone-equivalent timestamps, and rejection of future/expired prices. It also executed the native workspace FX query. The imported business schema and original SQLite data were not changed. This supplementary check does not prove the complete Ubuntu gateway request flow.

The two guard smoke fixtures now use an active project/application binding, immutable workspace price versions in current per-million units, and one consistent simulated clock for selection, admission and accounting. Both updated tests passed on 2026-10-15 (820.26 seconds): blocked requests return 402 without upstream calls, an established block survives a subsequent zero-price version, the next business day releases it, and repeated allowed requests at the 80% threshold create exactly one warning event. The complete 30-case legacy suite remains unverified. The earlier failed runs returned 503 for unavailable effective prices, rather than the expected budget decision.

## Visual evidence

Screenshots under `output/ui-audit-20261014/`:

- `budget-alert-dark.png`
- `budget-tree-dark.png`
- `budget-allocation-error-light.png`
- `provider-menu-light.png`
- `cost-template-dark.png`

The screenshots use an isolated database copy and a test account. Test-only cap changes did not affect the original database. No external model calls were made for these validations.

## Personnel entry and usage scope follow-up

- The personnel budget section now has a write-authorized “新增人员预算” action. It opens the existing form with employee-key scope selected and employee search focused. Personnel caps remain per key; multiple keys require separate rules.
- Manual employee search cancels the pending automatic search so a delayed duplicate response cannot reset the selected employee/key. A single available key is also displayed explicitly.
- Organization usage defaults to “组织总览”. The header states organization scope, and “我的用量” is a separate optional view rather than an always-visible panel under organization totals. Switching to personal usage hides organization metrics, filters and export controls. Existing backend organization and personal endpoints and permissions remain unchanged.
- An isolated QA account visually verified organization totals of 30 calls from another member versus 0 personal calls, including switching between views. Both themes and the personnel dialog were inspected.
- 28 budget/usage unit tests passed. Frontend lint passed for 868 files; production surface verification passed for 284 modules and 305 shipped sources; the production build passed for 305 assets.
- Additional visual evidence: `personnel-budget-entry-dark.png`, `personnel-budget-dialog-light.png`, `usage-organization-overview-dark.png`, `usage-personal-view-dark.png`, and `usage-organization-overview-light.png` in the same screenshot directory.
