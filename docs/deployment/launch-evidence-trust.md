# Launch evidence trust

PRD-138 accepts release-candidate evidence, not public GA approval. The production
Control application installs an Ed25519 verifier when
`METAHUB_LAUNCH_EVIDENCE_TRUST_BUNDLE_PATH` is configured. This setting is omitted
from configuration summaries and representations. It is a filesystem path, not a
URL or a private signing key.

## Deployment

Mount a deployment-owned JSON file read-only into every Control replica and set
the variable to its absolute in-container path. The Compose default does not
provision trust or mount a trust file: supply these through the deployment's
Control-only override. Do not give Gateway, Console, or customers write access.
Use directory/file permissions that allow Control to read but not replace it.
Private keys belong to the independent evidence producers, outside Control.

The following is a schema illustration, not an operational trust bundle:

```json
{
  "schema_version": "metahub.launch-evidence-trust.v1",
  "keys": [
    {
      "key_id": "api-current",
      "gate_code": "api",
      "verifier_code": "api-verifier",
      "verifier_version": 1,
      "public_key": "<32-byte Ed25519 public key, unpadded base64url>",
      "not_before": "2026-09-07T00:00:00Z",
      "not_after": "2026-10-07T00:00:00Z"
    }
  ]
}
```

Use the exact gate, verifier code, and version returned by
`GET /platform/launch-gates`. Each key is authorized only for that tuple.
`key_id` is a unique deployment label, not an HTTP request field. Overlapping keys
can support rotation; the verifier tries matching active keys. The interval is
inclusive at `not_before` and exclusive at `not_after`, evaluated at verification
time against Control's UTC clock. Keep deployment clocks synchronized.

Trust is reread for each new evidence submission. Replace the file atomically and
distribute consistent contents to all replicas. The file is limited to 65,536
bytes and 160 keys. Unknown properties, duplicate JSON properties, duplicate key
IDs, invalid intervals, noncanonical key encodings, and malformed JSON fail
closed. An empty key list trusts no producers.

## Failure And Rotation

An unconfigured verifier returns `503 launch.verifier_unavailable`. An unreadable
or invalid configured file, an untrusted key, or an invalid signature returns
`403 launch.verifier_invalid`. File paths and parser/crypto exception details are
not included in the API error. Fix the deployment configuration; never substitute
an always-successful verifier or customer-supplied public key.

Removing or expiring a key affects future submissions. Existing evidence stores
only signature digests, so it cannot be cryptographically revalidated after key
revocation. A successful idempotent replay returns the original fact without a
new verification. For retrospective withdrawal, invalidate/replace the candidate
under the existing generation workflow and collect fresh evidence and approvals.
Do not claim that editing trust automatically revokes existing candidates.

See [the signing contract](../api/launch-evidence.md). A valid signature proves
possession of an authorized key and integrity of the envelope. It does not prove
that the underlying tests ran, that 72 hours elapsed, or that independent people
approved a release. Those remain separate evidence and governance requirements.
