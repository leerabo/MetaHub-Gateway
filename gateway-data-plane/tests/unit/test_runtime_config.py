import pytest
from metahub_shared.runtime.config import (
    DEFAULT_IDENTITY_SESSION_SECRET,
    ConfigError,
    load_app_config,
)


def test_load_app_config_defaults_to_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    """主流程：无额外环境变量时，本地开发默认进入 dev 环境。"""

    monkeypatch.delenv("METAHUB_ENV", raising=False)
    config = load_app_config("gateway-data-plane")

    assert config.environment == "dev"
    assert config.features["business_api_enabled"] is False
    assert config.sensitive_keys_masked is True


def test_load_app_config_rejects_unknown_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """异常流程：非法环境必须快速失败，避免生产环境误用本地配置。"""

    monkeypatch.setenv("METAHUB_ENV", "staging")

    with pytest.raises(ConfigError) as exc:
        load_app_config("gateway-data-plane")

    assert exc.value.code == "infra.config_invalid"
    assert "METAHUB_ENV" in exc.value.message


def test_load_app_config_uses_file_then_env_priority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """主流程：配置优先级必须是默认值 < 配置文件 < 环境变量。"""

    config_file = tmp_path / "dev.env"
    config_file.write_text(
        "\n".join(
            [
                "METAHUB_ENV=test",
                "METAHUB_DATABASE_URL=postgresql://file-user:file-pass@db/metahub",
                "METAHUB_REDIS_URL=redis://file-redis:6379/0",
                "METAHUB_PROVIDER_DEEPSEEK_BASE_URL=https://api.deepseek.com",
                "METAHUB_PROVIDER_DEEPSEEK_API_KEY=sk-file-secret",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("METAHUB_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("METAHUB_REDIS_URL", "redis://env-redis:6379/0")

    config = load_app_config("gateway-data-plane")

    assert config.environment == "test"
    assert config.database_url == "postgresql://file-user:file-pass@db/metahub"
    assert config.redis_url == "redis://env-redis:6379/0"
    assert config.provider_base_urls["deepseek"] == "https://api.deepseek.com"
    assert config.provider_api_keys["deepseek"] == "sk-file-secret"
    assert any(
        item.key == "METAHUB_REDIS_URL" and item.source == "env" and item.status == "masked"
        for item in config.config_items
    )


def test_config_summary_masks_provider_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """安全验收：配置摘要只能显示 Provider Key 已脱敏，不能包含明文。"""

    monkeypatch.setenv("METAHUB_PROVIDER_DEEPSEEK_API_KEY", "sk-live-secret")
    config = load_app_config("gateway-data-plane")
    rendered = "\n".join(item.model_dump_json() for item in config.config_items)

    assert "sk-live-secret" not in rendered
    assert "***MASKED***" in rendered


def test_policy_evaluation_timeout_uses_bounded_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主流程：策略求值 deadline 默认 5 ms，并允许部署在安全范围内收紧或放宽。"""

    monkeypatch.delenv("METAHUB_POLICY_EVALUATION_TIMEOUT_MS", raising=False)
    assert load_app_config("gateway-data-plane").policy_evaluation_timeout_ms == 5

    monkeypatch.setenv("METAHUB_POLICY_EVALUATION_TIMEOUT_MS", "100")
    assert load_app_config("gateway-data-plane").policy_evaluation_timeout_ms == 100


@pytest.mark.parametrize("raw_value", ["0", "101", "1.5", "true"])
def test_policy_evaluation_timeout_rejects_unsafe_values(
    monkeypatch: pytest.MonkeyPatch,
    raw_value: str,
) -> None:
    """异常流程：无效或越界 deadline 必须阻断启动，不能静默截断为放行风险配置。"""

    monkeypatch.setenv("METAHUB_POLICY_EVALUATION_TIMEOUT_MS", raw_value)

    with pytest.raises(ConfigError) as exc:
        load_app_config("gateway-data-plane")

    assert exc.value.code == "infra.config_invalid"
    assert "METAHUB_POLICY_EVALUATION_TIMEOUT_MS" in exc.value.message


def _enable_policy_snapshot_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """设置可运行的本地同步身份，供配置边界测试减少重复样板。"""

    monkeypatch.setenv(
        "METAHUB_CACHE_AFFINITY_HMAC_SECRET",
        "unit-test-cache-affinity-hmac-secret-0123456789abcdef",
    )
    monkeypatch.setenv(
        "METAHUB_IDENTITY_SESSION_SECRET",
        "unit-test-independent-identity-session-secret-0123456789abcdef",
    )
    monkeypatch.setenv("METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED", "true")
    monkeypatch.setenv(
        "METAHUB_INTERNAL_CONFIG_TOKEN",
        "unit-test-policy-snapshot-token-0123456789abcdef",
    )
    monkeypatch.setenv(
        "METAHUB_EDGE_SUPPORT_BUNDLE_ENCRYPTION_SECRET",
        "unit-test-independent-support-bundle-secret-0123456789abcdef",
    )
    monkeypatch.setenv(
        "METAHUB_DATABASE_URL",
        "postgresql://db.internal/metahub?sslmode=require",
    )
    monkeypatch.setenv("METAHUB_LOCAL_ENDPOINT_ALLOW_LOOPBACK", "false")
    monkeypatch.setenv("METAHUB_CONTROL_API_BASE_URL", "http://127.0.0.1:8001")


@pytest.mark.parametrize(
    "identity_secret",
    (
        "too-short",
        DEFAULT_IDENTITY_SESSION_SECRET,
        "unit-test-cache-affinity-hmac-secret-0123456789abcdef",
    ),
)
def test_prod_identity_session_secret_must_be_independent(
    monkeypatch: pytest.MonkeyPatch,
    identity_secret: str,
) -> None:
    _enable_policy_snapshot_sync(monkeypatch)
    monkeypatch.setenv("METAHUB_ENV", "prod")
    monkeypatch.setenv("METAHUB_CONTROL_API_BASE_URL", "https://control.internal.example")
    monkeypatch.setenv("METAHUB_IDENTITY_SESSION_SECRET", identity_secret)

    with pytest.raises(ConfigError) as exc:
        load_app_config("gateway-data-plane")

    assert exc.value.code == "infra.config_invalid"
    assert "METAHUB_IDENTITY_SESSION_SECRET" in exc.value.message


def test_policy_snapshot_config_has_safe_bounded_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认合同：旧开发环境不产生后台网络副作用，全部门限保持设计值。"""

    monkeypatch.delenv("METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED", raising=False)
    config = load_app_config("gateway-data-plane")

    assert config.policy_snapshot_sync_enabled is False
    assert config.policy_snapshot_poll_interval_seconds == 2
    assert config.policy_snapshot_http_timeout_seconds == 2
    assert config.policy_snapshot_max_age_seconds == 300
    assert config.policy_snapshot_emergency_max_age_seconds == 3600
    assert config.policy_snapshot_max_payload_bytes == 4_194_304
    assert config.features["policy_snapshot_sync_enabled"] is False


@pytest.mark.parametrize(
    ("key", "valid_minimum", "valid_maximum", "invalid_values"),
    [
        ("METAHUB_POLICY_SNAPSHOT_POLL_INTERVAL_SECONDS", "1", "60", ("0", "61")),
        ("METAHUB_POLICY_SNAPSHOT_HTTP_TIMEOUT_SECONDS", "1", "10", ("0", "11")),
        ("METAHUB_POLICY_SNAPSHOT_MAX_AGE_SECONDS", "5", "86400", ("4", "86401")),
        (
            "METAHUB_POLICY_SNAPSHOT_EMERGENCY_MAX_AGE_SECONDS",
            "60",
            "604800",
            ("59", "604801"),
        ),
        (
            "METAHUB_POLICY_SNAPSHOT_MAX_PAYLOAD_BYTES",
            "1024",
            "16777216",
            ("1023", "16777217"),
        ),
    ],
)
def test_policy_snapshot_numeric_config_enforces_inclusive_bounds(
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    valid_minimum: str,
    valid_maximum: str,
    invalid_values: tuple[str, str],
) -> None:
    """异常流程：轮询、超时、LKG 和响应体门限不得越过硬上下界。"""

    # 只验证单字段边界时关闭同步，并为时间交叉约束提供不会相互冲突的配套值。
    monkeypatch.setenv("METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED", "false")
    if key == "METAHUB_POLICY_SNAPSHOT_POLL_INTERVAL_SECONDS":
        monkeypatch.setenv("METAHUB_POLICY_SNAPSHOT_MAX_AGE_SECONDS", "300")
    if key == "METAHUB_POLICY_SNAPSHOT_MAX_AGE_SECONDS":
        monkeypatch.setenv("METAHUB_POLICY_SNAPSHOT_POLL_INTERVAL_SECONDS", "1")
        monkeypatch.setenv("METAHUB_POLICY_SNAPSHOT_EMERGENCY_MAX_AGE_SECONDS", "604800")
    if key == "METAHUB_POLICY_SNAPSHOT_EMERGENCY_MAX_AGE_SECONDS":
        monkeypatch.setenv("METAHUB_POLICY_SNAPSHOT_MAX_AGE_SECONDS", "5")

    monkeypatch.setenv(key, valid_minimum)
    load_app_config("gateway-data-plane")
    monkeypatch.setenv(key, valid_maximum)
    load_app_config("gateway-data-plane")
    for invalid in invalid_values:
        monkeypatch.setenv(key, invalid)
        with pytest.raises(ConfigError) as exc:
            load_app_config("gateway-data-plane")
        assert exc.value.code == "infra.config_invalid"
        assert key in exc.value.message


@pytest.mark.parametrize(
    ("updates", "message_key"),
    [
        (
            {
                "METAHUB_POLICY_SNAPSHOT_POLL_INTERVAL_SECONDS": "6",
                "METAHUB_POLICY_SNAPSHOT_MAX_AGE_SECONDS": "5",
            },
            "METAHUB_POLICY_SNAPSHOT_MAX_AGE_SECONDS",
        ),
        (
            {
                "METAHUB_POLICY_SNAPSHOT_MAX_AGE_SECONDS": "61",
                "METAHUB_POLICY_SNAPSHOT_EMERGENCY_MAX_AGE_SECONDS": "60",
            },
            "METAHUB_POLICY_SNAPSHOT_EMERGENCY_MAX_AGE_SECONDS",
        ),
    ],
)
def test_policy_snapshot_config_rejects_invalid_lkg_ordering(
    monkeypatch: pytest.MonkeyPatch,
    updates: dict[str, str],
    message_key: str,
) -> None:
    """保护模式：三层时间窗口必须单调覆盖，避免 LKG 状态机产生反向或空白区间。"""

    for key, value in updates.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ConfigError) as exc:
        load_app_config("gateway-data-plane")
    assert exc.value.code == "infra.config_invalid"
    assert message_key in exc.value.message


@pytest.mark.parametrize(
    ("environment", "service_mode"),
    [("prod", "internal_only"), ("dev", "public_service")],
)
def test_policy_snapshot_sync_cannot_be_disabled_for_protected_gateway_modes(
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    service_mode: str,
) -> None:
    """启动门禁：生产或 public_service 关闭同步会绕过策略，必须 fail closed。"""

    monkeypatch.setenv("METAHUB_ENV", environment)
    monkeypatch.setenv("METAHUB_SERVICE_MODE", service_mode)
    monkeypatch.setenv(
        "METAHUB_IDENTITY_SESSION_SECRET",
        "unit-test-independent-identity-session-secret-0123456789abcdef",
    )
    monkeypatch.setenv("METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED", "false")

    with pytest.raises(ConfigError) as exc:
        load_app_config("gateway-data-plane")

    assert exc.value.code == "infra.config_invalid"
    assert "cannot be disabled" in exc.value.message


@pytest.mark.parametrize("token", [None, "too-short-token"])
def test_policy_snapshot_sync_requires_high_entropy_excluded_token(
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
) -> None:
    """服务身份：开启同步必须有有界高熵 Token，且 model_dump/摘要不含明文。"""

    _enable_policy_snapshot_sync(monkeypatch)
    # 缺失与低强度 Token 分别覆盖配置缺项和安全边界，两者都不得启动后台同步。
    if token is None:
        monkeypatch.delenv("METAHUB_INTERNAL_CONFIG_TOKEN", raising=False)
    else:
        monkeypatch.setenv("METAHUB_INTERNAL_CONFIG_TOKEN", token)

    with pytest.raises(ConfigError) as exc:
        load_app_config("gateway-data-plane")
    assert exc.value.code in {"infra.config_missing", "infra.config_invalid"}
    assert token is None or token not in exc.value.message

    secure_token = "secure-policy-sync-token-0123456789abcdef"
    monkeypatch.setenv("METAHUB_INTERNAL_CONFIG_TOKEN", secure_token)
    config = load_app_config("gateway-data-plane")
    assert secure_token not in str(config.model_dump())
    assert secure_token not in "\n".join(item.model_dump_json() for item in config.config_items)
    assert any(
        item.key == "METAHUB_INTERNAL_CONFIG_TOKEN" and item.status == "masked"
        for item in config.config_items
    )


def test_control_snapshot_sync_requires_the_same_high_entropy_service_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跨服务身份：Control 启用内部快照合同后也必须要求高熵 Token，不能只校验 Gateway。"""

    monkeypatch.setenv("METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED", "true")
    monkeypatch.delenv("METAHUB_INTERNAL_CONFIG_TOKEN", raising=False)
    with pytest.raises(ConfigError) as missing:
        load_app_config("control-plane-api")
    assert missing.value.code == "infra.config_missing"

    monkeypatch.setenv("METAHUB_INTERNAL_CONFIG_TOKEN", "weak")
    with pytest.raises(ConfigError) as weak:
        load_app_config("control-plane-api")
    assert weak.value.code == "infra.config_invalid"
    assert "weak" not in weak.value.message

    secure = "control-policy-snapshot-token-0123456789abcdef"
    monkeypatch.setenv("METAHUB_INTERNAL_CONFIG_TOKEN", secure)
    config = load_app_config("control-plane-api")
    assert config.internal_config_token == secure
    assert secure not in str(config.model_dump())


def test_explicit_internal_token_is_validated_before_sync_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部署预配置：显式弱 Token 即使当前关闭同步也必须启动失败，避免开关切换后暴露弱凭据。"""

    monkeypatch.setenv("METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED", "false")
    monkeypatch.setenv("METAHUB_INTERNAL_CONFIG_TOKEN", "short-token")

    with pytest.raises(ConfigError) as exc:
        load_app_config("control-plane-api")

    assert exc.value.code == "infra.config_invalid"
    assert "short-token" not in exc.value.message


@pytest.mark.parametrize(
    ("environment", "url", "accepted"),
    [
        ("dev", "http://localhost:8001", True),
        ("test", "http://127.0.0.1:8001", True),
        ("test", "http://[::1]:8001", True),
        ("dev", "https://control.internal.example", True),
        ("dev", "http://control.internal.example", False),
        ("prod", "http://127.0.0.1:8001", False),
        ("prod", "https://control.internal.example", True),
        ("prod", "https://user:pass@control.internal.example", False),
        ("prod", "https://control.internal.example?token=secret", False),
    ],
)
def test_policy_snapshot_control_url_enforces_https_and_loopback_http(
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    url: str,
    accepted: bool,
) -> None:
    """网络边界：生产只用 HTTPS；dev/test 的 HTTP 只能是字面量回环且禁止 URL 凭据。"""

    _enable_policy_snapshot_sync(monkeypatch)
    monkeypatch.setenv("METAHUB_ENV", environment)
    monkeypatch.setenv("METAHUB_CONTROL_API_BASE_URL", url)

    if accepted:
        assert load_app_config("gateway-data-plane").control_api_base_url == url
    else:
        with pytest.raises(ConfigError) as exc:
            load_app_config("gateway-data-plane")
        assert exc.value.code == "infra.config_invalid"
        assert "secret" not in exc.value.message


def test_local_secret_management_config_is_disabled_and_path_safe_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("METAHUB_LOCAL_SECRET_MANAGEMENT_ENABLED", raising=False)
    config = load_app_config("gateway-data-plane")
    assert config.local_secret_management_enabled is False
    assert config.features["local_secret_management_enabled"] is False
    dumped = str(config.model_dump())
    assert config.local_secret_database_path not in dumped
    assert config.local_secret_keystore_path not in dumped
    assert config.edge_backup_root not in dumped


def test_local_secret_management_config_requires_exact_local_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    database_path = tmp_path / "custody.sqlite3"
    keystore_path = tmp_path / "keystore.json"
    monkeypatch.setenv("METAHUB_LOCAL_SECRET_MANAGEMENT_ENABLED", "true")
    monkeypatch.setenv("METAHUB_LOCAL_SECRET_DEPLOYMENT_ID", "edp_" + "1" * 32)
    monkeypatch.setenv(
        "METAHUB_LOCAL_SECRET_ADMIN_HANDOFF_SECRET", "h" * 48
    )
    monkeypatch.setenv("METAHUB_LOCAL_SECRET_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("METAHUB_LOCAL_SECRET_KEYSTORE_PATH", str(keystore_path))
    monkeypatch.setenv(
        "METAHUB_LOCAL_MANAGEMENT_ALLOWED_ORIGINS", "http://127.0.0.1:5173"
    )
    monkeypatch.setenv("METAHUB_ALLOWED_ORIGINS", "http://127.0.0.1:5173")
    config = load_app_config("gateway-data-plane")
    assert config.local_secret_management_enabled is True
    assert config.local_secret_deployment_id == "edp_" + "1" * 32
    assert config.local_management_allow_loopback is True

    monkeypatch.setenv("METAHUB_LOCAL_SECRET_KEYSTORE_PATH", str(database_path))
    with pytest.raises(ConfigError, match="deployment boundary"):
        load_app_config("gateway-data-plane")

    monkeypatch.setenv("METAHUB_LOCAL_SECRET_KEYSTORE_PATH", str(keystore_path))
    monkeypatch.setenv("METAHUB_EDGE_BACKUP_ROOT", str(database_path))
    with pytest.raises(ConfigError, match="backup local and customer storage"):
        load_app_config("gateway-data-plane")

    backup_root = tmp_path / "backups"
    monkeypatch.setenv("METAHUB_EDGE_BACKUP_ROOT", str(backup_root))
    monkeypatch.setenv(
        "METAHUB_EDGE_BACKUP_CUSTOMER_OBJECT_STORE_ROOT", str(backup_root)
    )
    with pytest.raises(ConfigError, match="backup local and customer storage"):
        load_app_config("gateway-data-plane")


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("METAHUB_LOCAL_SECRET_DEPLOYMENT_ID", "invalid"),
        ("METAHUB_LOCAL_MANAGEMENT_ALLOWED_ORIGINS", "http://management.example.test"),
        ("METAHUB_LOCAL_MANAGEMENT_ALLOWED_ORIGINS", "https://localhost:99999"),
        ("METAHUB_LOCAL_SECRET_ADMIN_HANDOFF_SECRET", "too-short"),
        ("METAHUB_LOCAL_SECRET_ADMIN_HANDOFF_SECRET", "h" * 32 + " "),
        ("METAHUB_ALLOWED_ORIGINS", "http://localhost:5174"),
    ),
)
def test_local_secret_management_config_rejects_ambiguous_identity_or_origin(
    monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv("METAHUB_LOCAL_SECRET_MANAGEMENT_ENABLED", "true")
    monkeypatch.setenv("METAHUB_LOCAL_SECRET_DEPLOYMENT_ID", "edp_" + "1" * 32)
    monkeypatch.setenv(
        "METAHUB_LOCAL_SECRET_ADMIN_HANDOFF_SECRET", "h" * 48
    )
    monkeypatch.setenv(key, value)
    with pytest.raises(ConfigError) as exc:
        load_app_config("gateway-data-plane")
    assert exc.value.code == "infra.config_invalid"
    if key == "METAHUB_LOCAL_SECRET_ADMIN_HANDOFF_SECRET":
        assert value not in exc.value.message
        assert value not in exc.value.suggestion
