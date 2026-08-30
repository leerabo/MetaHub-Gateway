# Developer contracts and SDK lifecycle

PRD-133 publishes an explicit public registry and signed SDK lifecycle. It does not reflect FastAPI
routers, database schemas, platform routes, machine routes, or private response models.

## Developer API

All Developer routes require the existing Bearer credential with `developer_portal.read`, reject
Cookie credentials, return `Cache-Control: no-store, private`, and use strict response DTOs.

| Method | Route | Contract |
| --- | --- | --- |
| GET | `/developer/api-versions` | Published, deprecated, or sunset public versions and compatibility digests |
| GET | `/developer/changelog?limit=&cursor=` | Stable HMAC cursor over public lifecycle events |
| GET | `/developer/webhook-schemas` | Exact published event schemas and sample digests |
| GET | `/developer/sdks/{python|javascript}` | Published SDK metadata, artifact receipt, signature, and public key |
| GET | `/developer/sdks/{language}/{version}/download` | Immutable ZIP with digest, receipt, nosniff, and no-store headers |

The Console validates the exact filename, media type, `Content-Length`, `Digest`, immutable receipt,
artifact SHA-256, signature SHA-256, and Ed25519 signature before offering the Blob for download. The
canonical signed payload is:

```json
{
  "artifact_sha256": "<sha256>",
  "contract_sha256": "<sha256>",
  "language": "python",
  "purpose": "metahub.public-sdk.release/v1",
  "sdk_version": "1.0.0"
}
```

Keys are serialized in the shown order without whitespace. Python and JavaScript are the only
accepted languages. Published signatures and public keys are verification material, not credentials;
private signing keys never enter the API, Console, SDK archive, log, event, or evidence.

## Platform lifecycle

Platform routes use only the Platform Session Cookie, exact Origin/CSRF checks, and an
`Idempotency-Key` on writes. Workspace, Support, and Bearer credentials are rejected.

| Method | Route | Required permission |
| --- | --- | --- |
| GET | `/platform/developer-contracts` | `platform.developer_contract.read` |
| POST | `/platform/developer-contracts/build` | `platform.developer_contract.manage` |
| POST | `/platform/developer-contracts/{api_version_id}/approve` | `platform.developer_contract.approve` |
| POST | `/platform/developer-contracts/deprecations` | `platform.developer_contract.manage` |
| POST | `/platform/developer-contracts/deprecations/{notice_id}/{announce|withdraw|sunset}` | Manage or approve according to the transition |
| POST | `/platform/developer-contracts/sdks/{sdk_release_id}/withdraw` | `platform.developer_contract.manage` |

Build creates a canonical contract snapshot and both required SDKs in `pending_approval`. Approval
must come from a different currently authorized operator, rechecks the revision and complete build
evidence, signs the contract and SDK payloads, and publishes the Webhook schemas and outbox events in
one transaction. SDK withdrawal is append-only state; it does not delete the artifact audit history.

Deprecation requires a newer published replacement, explicit affected operations, a migration guide,
and at least 90 days of notice. The scheduler may advance an announced notice to
`sunset_scheduled`, but it never approves sunset. Final sunset requires the deadline, notification
watermark, adoption watermark, compatibility gate, revision, and a separately authorized approver.

## Failure and recovery

All responses use the shared fixed error envelope with `request_id` and `trace_id`. Revision changes
fail with `developer_contract.revision_conflict`; self-approval, incomplete release evidence, invalid
notice periods, unmet sunset gates, corrupt artifacts, unavailable signing, and idempotency conflicts
remain distinct fixed codes. Scheduler leases use persistent generation fencing; retry reclaims only
expired work and never treats an unknown result as success.

Migration `0125_public_api_sdk_lifecycle` is reversible only while no contract, SDK, notice, schema,
download audit, event, command, or scheduler business fact exists. Once facts exist, the down migration
fails closed instead of silently deleting lifecycle evidence.
