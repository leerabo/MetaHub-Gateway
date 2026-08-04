from metahub_shared.runtime.error_diagnosis import diagnose_error
from metahub_shared.runtime.errors import ERROR_SPECS, list_error_codes


def test_error_code_registry_has_required_metadata() -> None:
    """主流程：错误码注册表必须包含 HTTP 状态、owner 和 retryable 元数据。"""

    codes = {item.code: item for item in list_error_codes()}

    assert "infra.not_found" in codes
    assert codes["infra.not_found"].http_status == 404
    assert codes["infra.unhandled"].retryable is True
    assert codes["db.migration_failed"].owner == "db"
    assert codes["health.dependency_failed"].owner == "health"
    assert codes["gateway.upstream_rate_limited"].diagnosis.rule_id == "upstream_rate_limited"
    assert codes["gateway.upstream_auth_failed"].diagnosis.category == "provider_auth"
    assert set(codes) == set(ERROR_SPECS)


def test_error_diagnosis_maps_known_and_unknown_errors_without_sensitive_text() -> None:
    """主流程/安全：典型错误能归因，未知错误只返回安全摘要且不包含上游原文。"""

    rate_limited = diagnose_error("gateway.upstream_rate_limited", http_status=429)
    quota = diagnose_error(
        "gateway.upstream_error",
        http_status=402,
        provider_error_type="insufficient_quota",
        message="insufficient_quota for sk-secret and prompt content",
    )
    unknown = diagnose_error("vendor.raw_error", message="sk-secret Authorization prompt response")

    assert rate_limited.rule_id == "upstream_rate_limited"
    assert rate_limited.retryable is True
    assert quota.rule_id == "upstream_quota_exceeded"
    assert "sk-secret" not in quota.safe_summary
    assert unknown.rule_id == "unknown_error"
    assert "Authorization" not in unknown.safe_summary


def test_method_not_allowed_error_metadata_and_labels_are_stable() -> None:
    """统一合同：405 必须进入错误目录、专项诊断和中英文展示标签。"""

    zh_codes = {item.code: item for item in list_error_codes("zh-CN")}
    en_codes = {item.code: item for item in list_error_codes("en-US")}
    method_error = zh_codes["infra.method_not_allowed"]

    assert method_error.http_status == 405
    assert method_error.owner == "infra"
    assert method_error.retryable is False
    assert method_error.message == "Request method is not allowed."
    assert method_error.suggestion == "Use a method listed in the Allow response header."
    assert method_error.diagnosis.rule_id == "infra_method_not_allowed"
    assert method_error.diagnosis.category == "request_validation"
    assert method_error.label == "请求方法不受支持"
    assert en_codes["infra.method_not_allowed"].label == "Method not allowed"
