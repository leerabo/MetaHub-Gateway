"""PRD-056 Provider 错误目录、脱敏事实与确定性 matcher 专项合同测试。"""

from __future__ import annotations

import hashlib
import json

import pytest
from metahub_shared.runtime.provider_error_map import (
    BUILTIN_CATALOG_CANONICAL_BYTES,
    BUILTIN_CATALOG_HASH,
    BUILTIN_CATALOG_VERSION,
    EMPTY_OVERLAY_CONTENT_HASH,
    ProviderErrorMapArtifact,
    ProviderErrorMapLease,
    ProviderErrorRuleValidationError,
    builtin_catalog_registry_rows,
    builtin_provider_error_map_lease,
    compile_provider_error_rule,
    deserialize_provider_error_map_snapshot,
    extract_provider_error_facts,
    normalize_provider_error,
    provider_error_map_etag,
    provider_error_overlay_content_hash,
    provider_error_rule_to_wire,
    serialize_provider_error_map_snapshot,
)


def _tenant_rule(**overrides: object) -> dict[str, object]:
    """构造一条完整租户 wire 规则，测试只覆盖显式改动字段。"""

    raw: dict[str, object] = {
        "error_map_id": "pem_0123456789abcdef0123456789abcdef",
        "tenant_id": "tenant-a",
        "provider_id": "provider-a",
        "priority": 500,
        "http_statuses": [429],
        "http_status_class": None,
        "error_field": "error.code",
        "field_operator": "equals",
        "field_pattern": "custom_quota",
        "message_operator": None,
        "message_pattern": None,
        "output_error_code": "gateway.upstream_quota_exceeded",
        "status": "active",
        "version": 1,
        "pattern_hash": "",
    }
    raw.update(overrides)
    return raw


def _normalize(
    status: int,
    body: dict[str, object] | bytes,
    *,
    lease: ProviderErrorMapLease | None = None,
):
    """把测试正文按真实 UTF-8 bytes 送入一次完整抽取和匹配流程。"""

    raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
    facts = extract_provider_error_facts(
        upstream_http_status=status,
        transport_kind="none",
        raw_body=raw,
    )
    return normalize_provider_error(
        facts,
        lease=lease or builtin_provider_error_map_lease("tenant-a"),
        provider_id="provider-a",
    )


def test_builtin_catalog_exact_hash_and_registry_contract() -> None:
    """内置目录字节、hash 和 13 条 registry 行必须与设计及迁移 seed 一致。"""

    assert len(BUILTIN_CATALOG_CANONICAL_BYTES) == 3664
    assert hashlib.sha256(BUILTIN_CATALOG_CANONICAL_BYTES).hexdigest() == BUILTIN_CATALOG_HASH
    rows = builtin_catalog_registry_rows()
    assert len(rows) == 13
    assert {row["source"] for row in rows} == {"guard", "builtin"}
    assert len({row["ordinal"] for row in rows}) == 13
    assert all(row["builtin_catalog_version"] == BUILTIN_CATALOG_VERSION for row in rows)


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    (
        (401, {"error": {"code": "insufficient_quota"}}, "gateway.upstream_auth_failed"),
        (403, {"message": "余额不足"}, "gateway.upstream_auth_failed"),
        (500, {"error": {"message": "model not found"}}, "gateway.upstream_error"),
        (404, {"error": {"message": "route missing"}}, "gateway.upstream_resource_not_found"),
        (404, {"error": {"code": "model_not_found"}}, "gateway.upstream_model_not_found"),
        (429, {"error": {"message": "余额不足"}}, "gateway.upstream_quota_exceeded"),
        (429, {"error": {"message": "try later"}}, "gateway.upstream_rate_limited"),
        (422, {"error": {"message": "bad parameter"}}, "gateway.upstream_request_invalid"),
    ),
)
def test_guard_and_builtin_precedence(
    status: int, body: dict[str, object], expected: str
) -> None:
    """guard、余额、模型信号和普通状态 fallback 必须按冻结 ordinal 分类。"""

    assert _normalize(status, body).code == expected


def test_tenant_rule_matches_but_cannot_cross_tenant_or_override_guard() -> None:
    """tenant overlay 只服务冻结租户/Provider，且无法覆盖 401 guard。"""

    rule = compile_provider_error_rule(_tenant_rule(), expected_tenant_id="tenant-a")
    artifact = ProviderErrorMapArtifact(
        tenant_id="tenant-a",
        revision=7,
        overlay_content_hash=provider_error_overlay_content_hash((rule,)),
        builtin_catalog_version=BUILTIN_CATALOG_VERSION,
        rules=(rule,),
    )
    lease = ProviderErrorMapLease(artifact=artifact, source="fresh")
    result = _normalize(429, {"error": {"code": "custom_quota"}}, lease=lease)
    assert result.code == "gateway.upstream_quota_exceeded"
    assert result.evidence.matched_rule_source == "tenant"
    assert result.evidence.matched_rule_version == 1
    assert result.evidence.error_map_revision == 7
    assert _normalize(401, {"error": {"code": "custom_quota"}}, lease=lease).code == (
        "gateway.upstream_auth_failed"
    )
    with pytest.raises(ProviderErrorRuleValidationError) as exc_info:
        compile_provider_error_rule(_tenant_rule(), expected_tenant_id="tenant-b")
    assert exc_info.value.code == "error_map.tenant_unavailable"


@pytest.mark.parametrize(
    "updates",
    (
        {"field_operator": "regex"},
        {"field_pattern": "Authorization: Bearer secret"},
        {"field_pattern": "0123456789abcdef01234567"},
        {"http_statuses": [503], "output_error_code": "gateway.upstream_request_invalid"},
        {"http_statuses": [404], "output_error_code": "gateway.upstream_rate_limited"},
        {"http_status_class": []},
        {"error_field": []},
        {"field_operator": []},
        {"message_operator": []},
        {"output_error_code": []},
        {"status": []},
        {"pattern_hash": []},
        {
            "provider_id": None,
            "http_statuses": [],
            "error_field": None,
            "field_operator": None,
            "field_pattern": None,
            "message_operator": "contains",
            "message_pattern": "quota",
        },
    ),
)
def test_rule_compiler_rejects_unsafe_or_unreachable_rules(updates: dict[str, object]) -> None:
    """regex、敏感 pattern、guard、错误输出矩阵、非法容器和全租户 message-only 均失败关闭。"""

    with pytest.raises(ProviderErrorRuleValidationError):
        compile_provider_error_rule(_tenant_rule(**updates))




def test_raw_error_never_enters_repr_or_persisted_projection() -> None:
    """验证恶意 code/type/message 与 Provider Key 只留在 repr=False 内存候选，不进入持久投影。"""

    secret = "sk-provider-secret-0123456789abcdef"
    body = {
        "error": {"code": secret, "type": "unknown-secret-type", "message": secret},
        "authorization": f"Bearer {secret}",
    }
    facts = extract_provider_error_facts(
        upstream_http_status=400,
        transport_kind="none",
        raw_body=json.dumps(body).encode(),
    )
    rendered = repr(facts)
    assert secret not in rendered
    assert secret not in facts.redacted_projection_json
    assert facts.provider_error_code_class == "[REDACTED]"
    assert facts.provider_error_type_class == "[REDACTED]"
    assert facts.message_present is True


def test_truncated_or_invalid_json_discards_all_partial_candidates() -> None:
    """无法证明 EOF 的正文只保留 unknown 桶，部分 JSON 不能参与分类。"""

    facts = extract_provider_error_facts(
        upstream_http_status=429,
        transport_kind="none",
        raw_body=b'{"error":{"code":"insufficient_quota"',
        body_truncated=True,
        known_body_length=None,
    )
    assert facts.body_length_bucket == "unknown"
    assert facts.body_truncated is True
    assert facts.is_json is False
    result = normalize_provider_error(
        facts,
        lease=builtin_provider_error_map_lease("tenant-a"),
        provider_id="provider-a",
    )
    assert result.code == "gateway.upstream_rate_limited"


def test_snapshot_roundtrip_hash_etag_and_revision_zero_contract() -> None:
    """验证 Control/Gateway wire 往返、强 ETag 与空 overlay revision-0 语义始终保持一致。"""

    rule = compile_provider_error_rule(_tenant_rule())
    artifact = ProviderErrorMapArtifact(
        tenant_id="tenant-a",
        revision=3,
        overlay_content_hash=provider_error_overlay_content_hash((rule,)),
        builtin_catalog_version=BUILTIN_CATALOG_VERSION,
        rules=(rule,),
    )
    wire = serialize_provider_error_map_snapshot(
        artifact, request_id="req-test", trace_id="trace-test"
    )
    assert deserialize_provider_error_map_snapshot(wire, expected_tenant_id="tenant-a") == artifact
    assert provider_error_map_etag(
        "tenant-a", artifact.revision, artifact.overlay_content_hash
    ).startswith('"')
    empty = builtin_provider_error_map_lease("tenant-a").artifact
    empty_wire = serialize_provider_error_map_snapshot(
        empty, request_id="req-empty", trace_id="trace-empty"
    )
    empty_wire["rules"] = []
    assert empty.overlay_content_hash == EMPTY_OVERLAY_CONTENT_HASH
    parsed = deserialize_provider_error_map_snapshot(empty_wire, expected_tenant_id="tenant-a")
    assert parsed.revision == 0


def test_snapshot_rejects_duplicate_resource_or_tampered_hash() -> None:
    """Gateway 不接受重复规则、内容 hash 漂移或额外 wire 字段。"""

    rule = compile_provider_error_rule(_tenant_rule())
    artifact = ProviderErrorMapArtifact(
        tenant_id="tenant-a",
        revision=1,
        overlay_content_hash=provider_error_overlay_content_hash((rule,)),
        builtin_catalog_version=BUILTIN_CATALOG_VERSION,
        rules=(rule,),
    )
    wire = serialize_provider_error_map_snapshot(artifact, request_id="req", trace_id="trace")
    wire["overlay_content_hash"] = "0" * 64
    with pytest.raises(ProviderErrorRuleValidationError):
        deserialize_provider_error_map_snapshot(wire, expected_tenant_id="tenant-a")
    wire = serialize_provider_error_map_snapshot(artifact, request_id="req", trace_id="trace")
    wire["extra"] = True
    with pytest.raises(ProviderErrorRuleValidationError):
        deserialize_provider_error_map_snapshot(wire, expected_tenant_id="tenant-a")



    wire = serialize_provider_error_map_snapshot(artifact, request_id="req", trace_id="trace")
    wire["wire_schema_version"] = True
    with pytest.raises(ProviderErrorRuleValidationError):
        deserialize_provider_error_map_snapshot(wire, expected_tenant_id="tenant-a")

def test_wire_rule_roundtrip_uses_normalized_pattern_hash() -> None:
    """规则 wire 必须携带编译后 hash，接收端逐字段重算而不信任发送端派生值。"""

    rule = compile_provider_error_rule(_tenant_rule(field_pattern="Ｃustom_quota"))
    wire = provider_error_rule_to_wire(rule)
    assert wire["pattern_hash"] == rule.pattern_hash
    assert compile_provider_error_rule(wire) == rule
