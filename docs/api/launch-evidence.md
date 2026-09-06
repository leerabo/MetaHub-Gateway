# Launch evidence signing contract

The PRD-138 evidence route is
`POST /platform/launch-gates/{launch_gate_id}/evidence`. It requires the existing
Platform session, exact Origin/CSRF validation, one-time step-up grant,
`Idempotency-Key`, quoted `If-Match`, and `platform.launch.evidence.submit`
permission. Signing does not replace authentication or authorization.

## Envelope

The signer must bind all 17 fields below. Candidate and gate digests come from
`GET /platform/launch-gates`; tenant ownership must come from the authorized
artifact producer's owner metadata, not a user-chosen tenant override. The public
Console DTO deliberately does not disclose `tenant_id`. Control independently
resolves the owner and candidate/gate metadata from its database.

```text
launch_candidate_id  candidate_sha256  launch_gate_id  gate_code  policy_sha256
workspace_id  tenant_id  artifact_sha256  verifier_code  verifier_version
outcome  metric_value  metric_unit  environment_sha256  provenance_sha256
started_at  finished_at
```

Include `metric_value` and `metric_unit` as null when absent. Normalize timestamps
to UTC with exactly three fractional digits and a trailing `Z` (milliseconds,
truncated, not rounded). Serialize with Python-compatible JSON semantics:
sorted property names, no whitespace, ASCII escapes, and no NaN or infinity.
`verifier_version` is an integer; a present metric is a float, so `1.0` must not
be serialized as `1`. Hash the UTF-8 JSON with SHA-256. This is the existing
Python canonical format, not a claim of RFC 8785 compatibility. Non-Python
signers must match it exactly, including floating-point serialization.

Do not include `expected_revision`, `confirmed`, `signature`, or
`envelope_sha256` in the hashed JSON. The shared
`evidence_envelope_sha256(EvidenceVerificationRequest)` helper implements this
normalization for Python producers. The request object's own digest must match
before `evidence_signing_bytes` returns the bytes to sign:

```python
message = b"metahub.launch-evidence.v1\0" + bytes.fromhex(envelope_sha256)
signature = base64.urlsafe_b64encode(private_key.sign(message)).rstrip(b"=").decode("ascii")
```

Sign the 59-byte message with Ed25519, not the hex string or the raw JSON. Send
the resulting 64-byte signature as canonical unpadded base64url (86 characters)
in the existing `signature` body field. Do not send a public key or key ID.
Private keys must stay in the authorized producer's signing facility; do not
generate a shared production key in Control or place one in source or fixtures.

## Submission And Storage

The HTTP body includes the candidate ID, current `expected_revision`, Workspace
ID, artifact digest, verifier code/version, outcome, optional metric pair,
environment/provenance digests, timezone-aware start/finish timestamps,
signature, and `confirmed: true`. The header revision must match the body.
Control builds the envelope from authenticated, database-owned facts, verifies
outside the transaction, then rechecks current permission, revision, ownership,
policy, and time validity before writing. A wrong signature creates no evidence,
event, outbox item, candidate revision, or action-command fact.

The database retains the envelope digest and SHA-256 of the ASCII signature,
never the raw signature or private key. Configure public trust using
[the deployment guide](../deployment/launch-evidence-trust.md).

The crypto/API fixtures use generated in-memory keys and synthetic metrics. They
verify implementation contracts only, not real QA results, independent signoff,
72-hour operational stability, four pilot waves, or GA eligibility.
