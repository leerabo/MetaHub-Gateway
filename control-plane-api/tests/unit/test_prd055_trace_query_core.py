"""PRD-055 高级查询核心合同的聚焦单元测试。

这些测试不依赖 HTTP 或数据库，专门锁定 legacy model 与 v1 精确模型的不同语义、
snapshot_version 正整数边界，以及规范化指纹不会把两种查询错误合并。
"""

from __future__ import annotations

import base64
import json
from typing import Any, cast

import pytest

from metahub_control.request_logs import query as query_service
from metahub_shared.runtime.errors import ApiError


def _wire_filters(payload: dict[str, object]) -> str:
    """把测试筛选编码成公开 Base64URL 无填充 wire 格式。"""

    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def test_legacy_model_uses_explicit_or_mode_but_inline_equal_fields_use_and() -> None:
    """兼容 model 只按显式模式走 OR，inline 相等字段仍保持精确交集。"""

    legacy = query_service._parse_query(
        cast(Any, None),
        cast(Any, None),
        (("model", "same-model"),),
    )
    inline = query_service._parse_query(
        cast(Any, None),
        cast(Any, None),
        (
            (
                "filters",
                _wire_filters(
                    {
                        "v": 1,
                        "request_model": "same-model",
                        "resolved_model": "same-model",
                    }
                ),
            ),
        ),
    )

    assert legacy.legacy_model == "same-model"
    assert legacy.filters == {"v": 1}
    assert inline.legacy_model is None
    assert inline.filters["request_model"] == inline.filters["resolved_model"] == "same-model"
    assert legacy.fingerprint != inline.fingerprint

    legacy_sql, legacy_values = query_service._build_where(
        "tenant_a",
        legacy.filters,
        legacy_model=legacy.legacy_model,
    )
    inline_sql, inline_values = query_service._build_where(
        "tenant_a",
        inline.filters,
        legacy_model=inline.legacy_model,
    )
    assert "(rl.request_model=? OR rl.resolved_model=?)" in legacy_sql
    assert legacy_values == ["tenant_a", "same-model", "same-model"]
    assert " OR " not in inline_sql
    assert "rl.request_model=? AND rl.resolved_model=?" in inline_sql
    assert inline_values == ["tenant_a", "same-model", "same-model"]


def test_pagination_error_preserves_legacy_code_and_uses_prd055_code() -> None:
    """旧显式查询保留原错误码，新 filters 合同使用专用分页错误码。"""

    with pytest.raises(ApiError) as legacy_error:
        query_service._parse_query(
            cast(Any, None),
            cast(Any, None),
            (("limit", "101"),),
        )
    assert legacy_error.value.code == "request_log.query_invalid"

    with pytest.raises(ApiError) as inline_error:
        query_service._parse_query(
            cast(Any, None),
            cast(Any, None),
            (("filters", _wire_filters({"v": 1})), ("limit", "101")),
        )
    assert inline_error.value.code == "request_log.pagination_invalid"


@pytest.mark.parametrize(
    ("payload", "legacy_items"),
    [
        (
            {"v": 1, "created_from": "2026-07-01T00:00:00Z"},
            (("created_from", "2026-07-01T00:00:00Z"),),
        ),
        (
            {"v": 1, "created_to": "2026-07-02T00:00:00Z"},
            (("created_to", "2026-07-02T00:00:00Z"),),
        ),
    ],
)
def test_time_window_requires_both_bounds(
    payload: dict[str, object],
    legacy_items: tuple[tuple[str, str], ...],
) -> None:
    """时间窗口必须成对提供，防止 inline 与 legacy 任一入口形成无界扫描。"""

    with pytest.raises(ApiError) as inline_error:
        query_service.normalize_filters(payload, error_code="request_log.query_invalid")
    assert inline_error.value.code == "request_log.query_invalid"

    with pytest.raises(ApiError) as legacy_error:
        query_service._parse_query(cast(Any, None), cast(Any, None), legacy_items)
    assert legacy_error.value.code == "request_log.query_invalid"


def test_all_v1_fields_normalize_and_build_tenant_first_sql() -> None:
    """完整 v1 字段集合应规范化，并全部进入 tenant-first 参数化 SQL。"""

    payload: dict[str, object] = {
        "v": 1,
        "request_id": "req_prd055_all",
        "trace_id": "trc_prd055_all",
        "status": " FAILED ",
        "request_model": "company-default",
        "resolved_model": "deepseek-chat",
        "provider_code": "deepseek",
        "virtual_key_id": "vk_prd055",
        "environment_id": "env_prd055",
        "project_id": "prj_prd055",
        "application_id": "app_prd055",
        "snapshot_version": 3,
        "error_code": "gateway.upstream_error",
        "created_from": "2026-07-27T00:00:00+08:00",
        "created_to": "2026-07-28T00:00:00+08:00",
        "latency_ms_min": 10,
        "latency_ms_max": 5000,
        "total_tokens_min": 1,
        "total_tokens_max": 100000,
        "cost_amount_min": "0.000001",
        "cost_amount_max": "100.000000",
        "currency": "cny",
        "terminal_stage": "PROVIDER",
    }
    normalized = query_service.normalize_filters(
        payload,
        error_code="request_log.query_invalid",
    )
    assert normalized["status"] == "failed"
    assert normalized["currency"] == "CNY"
    assert normalized["terminal_stage"] == "provider"
    assert normalized["created_from"] == "2026-07-26T16:00:00Z"
    assert normalized["created_to"] == "2026-07-27T16:00:00Z"

    where_sql, parameters = query_service._build_where("tenant_prd055", normalized)
    assert where_sql.startswith("WHERE rl.tenant_id=?")
    expected_fragments = (
        "rl.request_id=?",
        "rl.trace_id=?",
        "rl.status=?",
        "rl.request_model=?",
        "rl.resolved_model=?",
        "rl.provider_code=?",
        "rl.virtual_key_id=?",
        "rl.environment_id=?",
        "rl.project_id=?",
        "rl.application_id=?",
        "rl.snapshot_version=?",
        "rl.error_code=?",
        "rl.currency=?",
        "rl.created_at>=?",
        "rl.created_at<?",
        "rl.latency_ms>=?",
        "rl.latency_ms<=?",
        "rl.total_tokens>=?",
        "rl.total_tokens<=?",
        "rl.estimated_cost_amount>=?",
        "rl.estimated_cost_amount<=?",
        f"({query_service.TERMINAL_STAGE_SQL})=?",
    )
    for fragment in expected_fragments:
        assert fragment in where_sql
    # v 不进入 WHERE；其余 22 个字段加可信 tenant 恰好形成 23 个绑定参数。
    assert parameters[0] == "tenant_prd055"
    assert len(parameters) == len(normalized)
    canonical = query_service.canonical_filters(normalized)
    assert json.loads(canonical) == normalized
    assert " " not in canonical


def test_sort_matrix_has_stable_tie_breakers_and_nulls_last() -> None:
    """四种排序和两个方向都保留稳定尾键，可空主列始终排在非空值之后。"""

    for sort_by in query_service.SORT_FIELDS:
        for direction in query_service.SORT_DIRECTIONS:
            order_sql = query_service._build_order(sort_by, direction)
            keyword = direction.upper()
            assert order_sql.startswith("ORDER BY ")
            assert order_sql.endswith(
                f"rl.created_at {keyword}, rl.request_id {keyword}"
            )
            if sort_by == "created_at":
                assert "IS NULL" not in order_sql
            else:
                assert f"(rl.{sort_by} IS NULL) ASC" in order_sql
                assert f"rl.{sort_by} {keyword}" in order_sql


def test_combined_status_provider_sort_uses_bounded_tenant_first_hint() -> None:
    """跨列排序的 status+provider 查询应缩小候选集，普通查询不强制索引。"""

    filters = {"v": 1, "status": "failed", "provider_code": "deepseek"}
    assert query_service._matched_index_hint(filters, "latency_ms") == (
        "INDEXED BY idx_request_logs_status_time_prd055"
    )
    assert query_service._matched_index_hint(filters, "created_at") == ""
    assert query_service._matched_index_hint(
        {"v": 1, "status": "failed"},
        "latency_ms",
    ) == ""


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"v": 2},
        {"v": True},
        {"v": 1, "tenant_id": "other"},
        {"v": 1, "status": {"nested": True}},
        {"v": 1, "cost_amount_min": 0.1},
        {"v": 1, "snapshot_version": False},
        {"v": 1, "terminal_stage": "invented"},
        {"v": 1, "currency": "EUR"},
        {
            "v": 1,
            "created_from": "2026-07-29T00:00:00Z",
            "created_to": "2026-07-28T00:00:00Z",
        },
        {
            "v": 1,
            "created_from": "2026-06-01T00:00:00Z",
            "created_to": "2026-07-28T00:00:00Z",
        },
        {"v": 1, "latency_ms_min": 2, "latency_ms_max": 1},
        {"v": 1, "total_tokens_min": 2, "total_tokens_max": 1},
        {"v": 1, "cost_amount_min": "2.0", "cost_amount_max": "1.0"},
        {"v": 1, "provider_code": "bad\ncontrol"},
    ],
)
def test_invalid_v1_filter_shapes_fail_closed(payload: dict[str, object]) -> None:
    """未知版本/字段、嵌套、浮点、非法范围和控制字符统一失败关闭。"""

    with pytest.raises(ApiError) as error:
        query_service.normalize_filters(payload, error_code="request_log.query_invalid")
    assert error.value.code == "request_log.query_invalid"


@pytest.mark.parametrize(
    ("query_items", "expected_code"),
    [
        ((("unknown", "value"),), "request_log.query_invalid"),
        ((("status", "failed"), ("status", "success")), "request_log.query_invalid"),
        (
            (("filters", _wire_filters({"v": 1})), ("status", "failed")),
            "request_log.query_invalid",
        ),
        ((("filters", "%%%"),), "request_log.query_invalid"),
        (
            (("filters", _wire_filters({"v": 1})), ("limit", "101")),
            "request_log.pagination_invalid",
        ),
        ((("offset", "10001"),), "request_log.query_invalid"),
        ((("sort_by", "created_at DESC"),), "request_log.sort_invalid"),
    ],
)
def test_query_mode_and_pagination_errors_are_stable(
    query_items: tuple[tuple[str, str], ...],
    expected_code: str,
) -> None:
    """重复/混合/未知 query、非法 wire、排序和深分页返回冻结错误码。"""

    with pytest.raises(ApiError) as error:
        query_service._parse_query(
            cast(Any, None),
            cast(Any, None),
            query_items,
        )
    assert error.value.code == expected_code


def test_duplicate_json_keys_are_rejected_before_normalization() -> None:
    """重复 JSON key 不能由解析器静默采用首值或末值。"""

    raw = b'{"v":1,"status":"failed","status":"success"}'
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    with pytest.raises(ApiError) as error:
        query_service.decode_filters(token)
    assert error.value.code == "request_log.query_invalid"


@pytest.mark.parametrize(
    ("payload", "legacy_items"),
    [
        ({"v": 1, "snapshot_version": 0}, (("snapshot_version", "0"),)),
        ({"v": 1, "snapshot_version": -1}, (("snapshot_version", "-1"),)),
    ],
)
def test_snapshot_version_rejects_non_positive_values(
    payload: dict[str, object],
    legacy_items: tuple[tuple[str, str], ...],
) -> None:
    """inline 与 legacy 两条入口都只接受大于等于一的快照版本。"""

    with pytest.raises(ApiError) as inline_error:
        query_service.normalize_filters(payload, error_code="request_log.query_invalid")
    assert inline_error.value.code == "request_log.query_invalid"

    with pytest.raises(ApiError) as legacy_error:
        query_service._parse_query(cast(Any, None), cast(Any, None), legacy_items)
    assert legacy_error.value.code == "request_log.query_invalid"
