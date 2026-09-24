# PRD-148 Customer Trial Handoff (2026-09-23)

## Approved scope for tomorrow

The current build may be demonstrated as a controlled technical trial on this
machine, or on a separately verified device in the same LAN. The customer may
use the Workspace experience to receive one MetaHub Gateway address and one
personal MetaHub KEY, then call an allowed client protocol path. The admin
demonstration may cover Provider, model, default route, employee key issuance,
usage display, budget display and key revocation.

The demonstrated workspace must be the healthy `default` workspace route. Do
not select the separate `company-default` route: it currently points to an
unhealthy DeepSeek Provider.

## Tomorrow's operator checklist

1. Start the local Console, Gateway, Control and Status services, then verify
   their four loopback health endpoints return `200` before the customer joins.
2. Sign into the `default` Workspace as its organization administrator. This
   is a Workspace organization role, not the separate SaaS Platform operator
   identity.
3. Show the healthy default route and issue the employee's personal MetaHub
   KEY. Give the employee only the one Gateway address and that one personal
   KEY; model names are only needed when the client itself requires one.
4. Demonstrate one permitted client Gateway request, basic usage visibility,
   and personal-key revocation. Keep the KIMI Provider credential confined to
   the administrator configuration and do not expose it to the employee.
   Provider health alone does not meet this step: issue a new personal KEY and
   complete the request through the Gateway before the customer joins.
   The local operator may run `scripts/dev/verify-personal-gateway-key.ps1`;
   it prompts for the just-issued KEY, omits `model` to exercise the default
   route, and can use `-ExpectUnauthorized` after revocation.
5. If a second device is used, verify its LAN reachability before the customer
   session. A loopback or same-machine LAN self-check is not evidence that the
   second device can connect.

## Verified evidence

- Current local-runtime route readiness confirms the active `default` alias
  resolves to active `kimi_real_prd148`, an active chat/stream model and an
  active credential whose latest probe was healthy with zero consecutive
  failures. `company-default` remains excluded because its DeepSeek Provider
  has no usable credential. No credential or personal KEY was exported.
  Evidence: `outputs/prd148-live-route-readiness-20260923-s3.json`.

- The locally running Console, Gateway, Control and Status services listen on
  ports 5173, 8000, 8001 and 8002. The local account entry regression is fixed:
  both loopback origins pass credentialed CORS preflight, and Chromium shows the
  login form without the account-service connection alert. Evidence:
  `outputs/prd148-local-console-loopback-20260923.json`.
- Public entry, registration, reset, privacy and status pages passed 18
  Chromium checks across desktop and mobile viewports. Evidence:
  `outputs/prd148-public-runtime-current-20260923.json`.
- Workspace admin, developer and viewer/employee each passed 60 registered
  routes; Platform operator checks use a separate Platform session. Support,
  Partner and Device boundaries were checked with seeded data. Evidence:
  `outputs/prd148-human-audit-current-20260923.json` and
  `docs/testing/PRD-148-human-audit-20260923.md`.
- The high-frequency Workspace customer tasks passed 24/24 current-source
  reversible Chromium interactions across organization admin, developer and
  viewer/employee roles. The Private Gateway detail now keeps installation and
  health in the first view while low-frequency runtime and governance controls
  are initially collapsed; the expand/re-collapse behavior was verified.
  Evidence: `.artifacts/product-qa/20260923-core-task-interaction/` and
  `.artifacts/product-qa/20260923-private-gateway-layering-r2/`.
- A historical local authenticated KIMI flow completed login, Provider/model/
  route reads, employee and personal-key creation, Gateway call, usage and
  budget reads, key revocation and post-revocation 401. It remains historical,
  not a replacement for a new current-source organization creation. Evidence:
  `outputs/prd148-real-current-flow-20260919.json`.

## Explicit exclusions from customer promises

- There is no public Internet deployment, TLS endpoint, external-device
  firewall verification or public uptime commitment.
- The local database has no subscriptions or entitlement snapshots. Its meter
  outbox has four pending records and one `platform_metering.owner_invalid`
  failure. Do not represent the trial as paid quota, settled usage or financial
  reconciliation.
- This is not GA and PRD-148 remains incomplete. It does not include release
  signoff, production rollback rehearsal, commercial reconciliation or a
  scheduler-enabled stability observation.

## Customer wording

Describe the offering as: "MetaHub controlled technical trial for validating
organization access, Gateway model calls, personal keys, revocation and basic
usage visibility." Do not describe it as a public service, paid plan, SLA or
production launch.

## Current source binding

The delivery-preparation role, public-entry and Gateway audit baseline is
bound by `outputs/prd148-source-current-20260923-trial-handoff.json` to
revision `0b3fd65fe31e9c6a320ae9d2a399e36c07e34fdf`, 2698 tracked/eligible
source files and SHA-256
`b6855b993c3e7de2ddfb18d06781b0a6b030b9c481d42c553db4cb96d16d392d`.

The subsequent current source adds the Private Gateway complexity reduction
and the module-origin regression repair. It is independently bound by
`outputs/prd148-source-current-20260923-private-gateway-layering-r2.json` to
the same revision, 2698 files and SHA-256
`d3e2954e9e51746973f0d974889de2f97f69eba364f11b9adcdc81d26e6594b5`.
The focused Private Gateway and core-task browser evidence above covers that
later change. These two source snapshots are intentionally recorded
separately; neither is a GA approval.

The latest current source further reduces installer density by keeping the
active deployment choices, progress and signed downloads visible while placing
low-frequency machine requirements in a closed “环境要求” section. Its isolated
Device and Partner browser regression passed, and it is bound by
`outputs/prd148-source-current-20260923-installer-density-r3.json` to the same
revision, 2698 files and SHA-256
`107409ad5ec76a7bb2a9d2c236fbbe53eb2ad08f9f77c350150db9ede28145ee`.
