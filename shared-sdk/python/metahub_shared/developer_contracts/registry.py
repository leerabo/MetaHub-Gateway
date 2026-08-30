"""Explicit public surface used by PRD-133 contract and SDK releases.

This module never imports or reflects a web framework router. Every operation and
Webhook field must be deliberately registered here before it can be published.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Literal

from metahub_shared.config_distribution import canonical_json_bytes

PUBLIC_API_REGISTRY_VERSION = 1
SDK_LANGUAGES = ("python", "javascript")
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_FIELDS = frozenset(
    {
        "authorization",
        "cookie",
        "password",
        "prompt",
        "provider_key",
        "response",
        "secret",
        "token",
        "virtual_key",
    }
)


@dataclass(frozen=True, slots=True)
class PublicOperation:
    operation_id: str
    method: Literal["GET", "POST", "PATCH", "DELETE"]
    path: str
    auth_kind: Literal["bearer", "workspace_cookie"]
    permission: str
    request_schema: str | None
    response_schema: str
    error_schema: Literal["MetaHubErrorV1"] = "MetaHubErrorV1"
    pagination: Literal["none", "stable_cursor"] = "none"
    idempotency: Literal["none", "required"] = "none"
    stability: Literal["stable"] = "stable"


@dataclass(frozen=True, slots=True)
class WebhookSchema:
    event_name: str
    schema_version: int
    required_fields: tuple[str, ...]
    optional_fields: tuple[str, ...] = ()

    def document(self) -> dict[str, object]:
        properties = {
            field: {"type": "string", "maxLength": 160}
            for field in (*self.required_fields, *self.optional_fields)
        }
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "additionalProperties": False,
            "properties": properties,
            "required": list(self.required_fields),
            "type": "object",
        }


_OPERATIONS = (
    PublicOperation(
        "developer.api_versions.list",
        "GET",
        "/developer/api-versions",
        "bearer",
        "developer_portal.read",
        None,
        "ApiVersionListResponseV1",
    ),
    PublicOperation(
        "developer.changelog.list",
        "GET",
        "/developer/changelog",
        "bearer",
        "developer_portal.read",
        None,
        "DeveloperChangelogResponseV1",
        pagination="stable_cursor",
    ),
    PublicOperation(
        "developer.webhook_schemas.list",
        "GET",
        "/developer/webhook-schemas",
        "bearer",
        "developer_portal.read",
        None,
        "WebhookSchemaListResponseV1",
    ),
    PublicOperation(
        "developer.sdk_releases.list",
        "GET",
        "/developer/sdks/{language}",
        "bearer",
        "developer_portal.read",
        None,
        "SdkReleaseListResponseV1",
    ),
    PublicOperation(
        "developer.sdk_releases.download",
        "GET",
        "/developer/sdks/{language}/{sdk_version}/download",
        "bearer",
        "developer_portal.read",
        None,
        "SdkArtifactV1",
    ),
    PublicOperation(
        "virtual_keys.list",
        "GET",
        "/virtual-keys",
        "bearer",
        "virtual_key.read",
        None,
        "VirtualKeyListResponseV1",
    ),
    PublicOperation(
        "models.list",
        "GET",
        "/models",
        "bearer",
        "model_catalog.read",
        None,
        "ModelListResponseV1",
    ),
    PublicOperation(
        "request_logs.list",
        "GET",
        "/request-logs",
        "bearer",
        "request_log.read",
        None,
        "RequestLogListResponseV1",
        pagination="stable_cursor",
    ),
    PublicOperation(
        "webhooks.create",
        "POST",
        "/webhooks",
        "bearer",
        "webhook.manage",
        "WebhookCreateRequestV1",
        "WebhookResponseV1",
        idempotency="required",
    ),
)

_WEBHOOKS = (
    WebhookSchema(
        "api.deprecated",
        1,
        (
            "api_version",
            "migration_guide_code",
            "notice_id",
            "replacement_version",
            "sunset_at",
        ),
        ("operation_count",),
    ),
    WebhookSchema(
        "api.sunset_scheduled",
        1,
        ("api_version", "notice_id", "sunset_at", "notification_watermark"),
    ),
    WebhookSchema(
        "sdk.released",
        1,
        ("api_version", "language", "sdk_version", "artifact_sha256"),
    ),
    WebhookSchema(
        "webhook.schema_published",
        1,
        ("api_version", "event_name", "schema_version", "schema_sha256"),
    ),
)


def public_operations() -> tuple[PublicOperation, ...]:
    return _OPERATIONS


def webhook_schemas() -> tuple[WebhookSchema, ...]:
    return _WEBHOOKS


def canonical_public_contract() -> bytes:
    document = {
        "operations": [asdict(operation) for operation in _OPERATIONS],
        "registry_version": PUBLIC_API_REGISTRY_VERSION,
        "webhook_schemas": [
            {
                "event_name": schema.event_name,
                "json_schema": schema.document(),
                "schema_version": schema.schema_version,
            }
            for schema in _WEBHOOKS
        ],
    }
    _reject_sensitive_fields(document)
    return canonical_json_bytes(document)


def contract_digest(contract: bytes | None = None) -> str:
    return hashlib.sha256(contract or canonical_public_contract()).hexdigest()


def classify_breaking_changes(baseline: bytes | None) -> tuple[str, ...]:
    """Compare all behavior-bearing operation and event fields.

    An absent baseline is the first release and therefore compatible. Additive
    operations and optional Webhook fields are compatible; removals and mutations
    are returned as stable reason codes.
    """

    if baseline is None:
        return ()
    try:
        previous = json.loads(baseline)
        current = json.loads(canonical_public_contract())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ("baseline.invalid",)
    if type(previous) is not dict or type(current) is not dict:
        return ("baseline.invalid",)
    reasons: list[str] = []
    previous_operations = _keyed(previous.get("operations"), "operation_id")
    current_operations = _keyed(current.get("operations"), "operation_id")
    if previous_operations is None or current_operations is None:
        return ("baseline.invalid",)
    for operation_id, old in previous_operations.items():
        new = current_operations.get(operation_id)
        if new is None:
            reasons.append(f"operation.removed:{operation_id}")
            continue
        for field in (
            "method",
            "path",
            "auth_kind",
            "permission",
            "request_schema",
            "response_schema",
            "error_schema",
            "pagination",
            "idempotency",
            "stability",
        ):
            if old.get(field) != new.get(field):
                reasons.append(f"operation.{field}_changed:{operation_id}")
    previous_webhooks = _keyed(previous.get("webhook_schemas"), "event_name")
    current_webhooks = _keyed(current.get("webhook_schemas"), "event_name")
    if previous_webhooks is None or current_webhooks is None:
        return ("baseline.invalid",)
    for event_name, old in previous_webhooks.items():
        new = current_webhooks.get(event_name)
        if new is None:
            reasons.append(f"webhook.removed:{event_name}")
            continue
        if old.get("schema_version") != new.get("schema_version"):
            reasons.append(f"webhook.version_changed:{event_name}")
        old_schema = old.get("json_schema")
        new_schema = new.get("json_schema")
        if type(old_schema) is not dict or type(new_schema) is not dict:
            reasons.append(f"webhook.schema_invalid:{event_name}")
            continue
        old_required = set(old_schema.get("required", ()))
        new_required = set(new_schema.get("required", ()))
        old_properties = set((old_schema.get("properties") or {}).keys())
        new_properties = set((new_schema.get("properties") or {}).keys())
        if not old_properties <= new_properties:
            reasons.append(f"webhook.fields_removed:{event_name}")
        if new_required - old_required:
            reasons.append(f"webhook.required_added:{event_name}")
    return tuple(sorted(set(reasons)))


def build_sdk_archive(
    language: str,
    *,
    sdk_version: str,
    api_version: str,
    contract_sha256: str,
) -> tuple[bytes, Mapping[str, str]]:
    checked_language = _language(language)
    if _SEMVER.fullmatch(sdk_version) is None or _SEMVER.fullmatch(api_version) is None:
        raise ValueError("invalid version")
    if _HEX_64.fullmatch(contract_sha256) is None:
        raise ValueError("invalid contract digest")
    files = _sdk_files(checked_language, sdk_version, api_version, contract_sha256)
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True
    ) as archive:
        for name, content in sorted(files.items()):
            raw = content.encode("utf-8")
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, raw)
    manifest = MappingProxyType(
        {
            name: hashlib.sha256(content.encode("utf-8")).hexdigest()
            for name, content in files.items()
        }
    )
    return buffer.getvalue(), manifest


def sdk_signature_payload(
    *, language: str, sdk_version: str, contract_sha256: str, artifact_sha256: str
) -> bytes:
    document = {
        "artifact_sha256": artifact_sha256,
        "contract_sha256": contract_sha256,
        "language": _language(language),
        "purpose": "metahub.public-sdk.release/v1",
        "sdk_version": sdk_version,
    }
    if _SEMVER.fullmatch(sdk_version) is None or any(
        _HEX_64.fullmatch(value) is None for value in (contract_sha256, artifact_sha256)
    ):
        raise ValueError("invalid signature payload")
    return canonical_json_bytes(document)


def _sdk_files(
    language: str, sdk_version: str, api_version: str, contract_sha256: str
) -> dict[str, str]:
    manifest = (
        json.dumps(
            {
                "api_version": api_version,
                "contract_sha256": contract_sha256,
                "language": language,
                "sdk_version": sdk_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    readme = (
        "# MetaHub Public SDK\n\n"
        f"Version: {sdk_version}\nAPI: {api_version}\n"
        "Authentication is read only from METAHUB_API_KEY at runtime.\n"
        "No credential is bundled in this artifact.\n"
    )
    if language == "python":
        client = (
            "import os\n"
            "from dataclasses import dataclass\n\n"
            "@dataclass(frozen=True)\n"
            "class MetaHubClient:\n"
            "    base_url: str\n\n"
            "    def authorization_header(self) -> dict[str, str]:\n"
            "        value = os.environ.get('METAHUB_API_KEY')\n"
            "        if not value:\n"
            "            raise RuntimeError('METAHUB_API_KEY is required')\n"
            "        return {'Authorization': 'Bearer ' + value}\n"
        )
        return {"README.md": readme, "manifest.json": manifest, "metahub_sdk/client.py": client}
    client = (
        "export class MetaHubClient {\n"
        "  constructor({ baseUrl }) { this.baseUrl = baseUrl; }\n"
        "  authorizationHeader(env = process.env) {\n"
        "    const value = env.METAHUB_API_KEY;\n"
        "    if (!value) throw new Error('METAHUB_API_KEY is required');\n"
        "    return { Authorization: `Bearer ${value}` };\n"
        "  }\n"
        "}\n"
    )
    return {"README.md": readme, "manifest.json": manifest, "src/client.js": client}


def _language(value: str) -> str:
    if value not in SDK_LANGUAGES:
        raise ValueError("unsupported language")
    return value


def _keyed(value: object, key: str) -> dict[str, dict[str, object]] | None:
    if type(value) is not list:
        return None
    result: dict[str, dict[str, object]] = {}
    for item in value:
        if type(item) is not dict or type(item.get(key)) is not str or item[key] in result:
            return None
        result[str(item[key])] = item
    return result


def _reject_sensitive_fields(value: object) -> None:
    if type(value) is dict:
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SENSITIVE_FIELDS:
                raise ValueError("sensitive public field")
            _reject_sensitive_fields(item)
    elif type(value) is list:
        for item in value:
            _reject_sensitive_fields(item)
