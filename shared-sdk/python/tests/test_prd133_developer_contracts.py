"""PRD-133 explicit public registry and reproducible SDK contracts."""

from __future__ import annotations

import hashlib
import json
import zipfile
from io import BytesIO

import pytest
from metahub_shared.developer_contracts import (
    SDK_LANGUAGES,
    build_sdk_archive,
    canonical_public_contract,
    classify_breaking_changes,
    contract_digest,
    public_operations,
    sdk_signature_payload,
    webhook_schemas,
)


def test_public_registry_is_explicit_deterministic_and_excludes_private_routes() -> None:
    first = canonical_public_contract()
    second = canonical_public_contract()
    document = json.loads(first)

    assert first == second
    assert contract_digest() == hashlib.sha256(first).hexdigest()
    assert {operation.operation_id for operation in public_operations()} >= {
        "developer.api_versions.list",
        "developer.changelog.list",
        "developer.webhook_schemas.list",
        "developer.sdk_releases.list",
    }
    assert {schema.event_name for schema in webhook_schemas()} == {
        "api.deprecated",
        "api.sunset_scheduled",
        "sdk.released",
        "webhook.schema_published",
    }
    deprecated = next(
        schema for schema in webhook_schemas() if schema.event_name == "api.deprecated"
    )
    assert "migration_guide_code" in deprecated.required_fields
    assert all(
        not item["path"].startswith(("/internal", "/platform", "/machine"))
        for item in document["operations"]
    )
    assert all(item["error_schema"] == "MetaHubErrorV1" for item in document["operations"])


def test_breaking_change_classifier_covers_behavior_and_webhook_requirements() -> None:
    baseline = json.loads(canonical_public_contract())
    baseline["operations"][0]["permission"] = "developer_portal.legacy_read"
    baseline["webhook_schemas"][0]["json_schema"]["required"].remove("notice_id")

    reasons = classify_breaking_changes(
        json.dumps(baseline, sort_keys=True, separators=(",", ":")).encode()
    )

    assert "operation.permission_changed:developer.api_versions.list" in reasons
    assert "webhook.required_added:api.deprecated" in reasons
    assert classify_breaking_changes(None) == ()
    assert classify_breaking_changes(b"not-json") == ("baseline.invalid",)


@pytest.mark.parametrize("language", SDK_LANGUAGES)
def test_sdk_archive_is_reproducible_closed_and_credential_free(language: str) -> None:
    kwargs = {
        "sdk_version": "1.2.3",
        "api_version": "1.1.0",
        "contract_sha256": "a" * 64,
    }
    first, first_manifest = build_sdk_archive(language, **kwargs)
    second, second_manifest = build_sdk_archive(language, **kwargs)

    assert first == second
    assert dict(first_manifest) == dict(second_manifest)
    with zipfile.ZipFile(BytesIO(first)) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        assert all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist())
        files = {name: archive.read(name) for name in names}
    assert first_manifest == {
        name: hashlib.sha256(content).hexdigest() for name, content in files.items()
    }
    joined = b"\n".join(files.values())
    assert b"METAHUB_API_KEY" in joined
    assert b"api_key=" not in joined.lower()
    assert b"bearer test" not in joined.lower()


def test_sdk_inputs_and_signature_payload_are_strict() -> None:
    payload = sdk_signature_payload(
        language="python",
        sdk_version="1.2.3",
        contract_sha256="b" * 64,
        artifact_sha256="c" * 64,
    )
    assert json.loads(payload) == {
        "artifact_sha256": "c" * 64,
        "contract_sha256": "b" * 64,
        "language": "python",
        "purpose": "metahub.public-sdk.release/v1",
        "sdk_version": "1.2.3",
    }
    with pytest.raises(ValueError, match="unsupported language"):
        build_sdk_archive(
            "ruby", sdk_version="1.2.3", api_version="1.1.0", contract_sha256="a" * 64
        )
    with pytest.raises(ValueError, match="invalid version"):
        build_sdk_archive(
            "python", sdk_version="latest", api_version="1.1.0", contract_sha256="a" * 64
        )
