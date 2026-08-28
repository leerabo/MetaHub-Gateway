"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，
Provider Key 与 Token 不得明文落日志。
来源文件：shared-sdk/python/metahub_shared/runtime/config.py
"""

from __future__ import annotations

import hmac
import ipaddress
import math
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, Field

from metahub_shared.business_verification import (
    DEFAULT_BUSINESS_VERIFICATION_ENCRYPTION_SECRET,
    BusinessVerificationCryptoError,
    validate_business_verification_secret,
)
from metahub_shared.files.crypto import DEFAULT_FILE_ENCRYPTION_SECRET
from metahub_shared.identity.crypto import DEFAULT_IDENTITY_PII_ENCRYPTION_SECRET
from metahub_shared.identity.mfa import DEFAULT_IDENTITY_MFA_ENCRYPTION_SECRET
from metahub_shared.mcp.contracts import McpContractError, McpSchemaLimits, McpVersionedSecret
from metahub_shared.mcp.crypto import (
    DEFAULT_MCP_ENCRYPTION_SECRET,
    DEFAULT_MCP_HMAC_SECRET,
    validate_mcp_secret_domains,
)
from metahub_shared.mcp.network import (
    MCP_SANDBOX_RESPONSE_RESERVE_SECONDS,
    McpNetworkPolicyCaps,
)
from metahub_shared.payment_commerce import (
    DEFAULT_PAYMENT_RECEIPT_ENCRYPTION_SECRET,
    DEFAULT_PAYMENT_WEBHOOK_SECRET,
    PaymentCryptoError,
    validate_payment_secret,
)
from metahub_shared.response_cache import (
    ResponseCacheContractError,
    VersionedResponseCacheSecret,
    validate_secret_keyring,
)

from .logging import is_sensitive_key

# PRD-002 只允许三类环境。这里不要放 staging、preprod 等别名，
# 因为运维最需要的是不同环境配置不会互相污染，而不是名字越多越灵活。
EnvironmentName = Literal["dev", "test", "prod"]
ConfigSource = Literal["default", "file", "env"]
ConfigStatus = Literal["configured", "missing", "masked", "defaulted"]
ServiceMode = Literal["internal_only", "public_service"]
McpExecutorMode = Literal["disabled", "test_loopback", "sandbox_http"]
IdentityChallengeDeliveryMode = Literal["disabled", "test_capture"]
CapacityRunnerMode = Literal["disabled", "test_loopback", "isolated_http"]

# CacheAffinity 使用独立密钥域；默认值只保证本地可运行，生产 Gateway 必须显式替换。
DEFAULT_CACHE_AFFINITY_HMAC_SECRET = "dev-only-change-me-metahub-cache-affinity-hmac-secret"
DEFAULT_IDENTITY_SESSION_SECRET = "dev-only-change-me-metahub-identity-session-secret-2026"
DEFAULT_IDENTITY_CHALLENGE_SECRET = "dev-only-change-me-metahub-identity-challenge-secret-2026"
DEFAULT_ENTERPRISE_SSO_HMAC_SECRET = (
    "dev-only-change-me-metahub-enterprise-sso-hmac-secret-2026"
)
DEFAULT_ENTERPRISE_SSO_ENCRYPTION_SECRET = (
    "dev-only-change-me-metahub-enterprise-sso-encryption-secret-2026"
)
DEFAULT_WORKSPACE_SESSION_SECRET = "dev-only-change-me-metahub-workspace-session-secret-2026"
DEFAULT_PLATFORM_SESSION_SECRET = "dev-only-change-me-metahub-platform-session-secret-2026"
DEFAULT_SUPPORT_SESSION_SECRET = "dev-only-change-me-metahub-support-session-secret-2026"
DEFAULT_PLATFORM_BOOTSTRAP_RECOVERY_SECRET = (
    "dev-only-change-me-metahub-platform-bootstrap-recovery-secret-2026"
)
CACHE_AFFINITY_EXAMPLE_HMAC_SECRET = (
    "replace-with-cache-affinity-hmac-secret-at-least-32-characters"
)
# Response Cache 的四个根域分别服务 cache identity、entry 密文、Control 幂等 owner 和
# commit-unknown failure spool。公开开发值只用于 dev/test；生产即使版本不同也禁止复用。
DEFAULT_RESPONSE_CACHE_HMAC_SECRET = "dev-only-change-me-metahub-response-cache-hmac-secret-2026"
DEFAULT_RESPONSE_CACHE_ENCRYPTION_SECRET = (
    "dev-only-change-me-metahub-response-cache-encryption-secret-2026"
)
DEFAULT_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_SECRET = (
    "dev-only-change-me-metahub-response-cache-control-idempotency-secret-2026"
)
DEFAULT_RESPONSE_CACHE_FAILURE_SPOOL_SECRET = (
    "dev-only-change-me-metahub-response-cache-failure-spool-secret-2026"
)
# 企业 Chat 的 Control -> Gateway 身份使用独立密钥域。默认值仅服务于本机回环开发，
# 生产环境必须显式覆盖，且该值永远不能进入配置摘要、repr、日志或 API 响应。
DEFAULT_INTERNAL_CHAT_TOKEN = "dev-only-change-me-metahub-internal-chat-token-2026"
DEFAULT_INTERNAL_BATCH_TOKEN = "dev-only-change-me-metahub-internal-batch-token-2026"
DEFAULT_BATCH_ENCRYPTION_SECRET = "dev-only-change-me-metahub-batch-encryption-secret-2026"
DEFAULT_APPROVAL_HMAC_SECRET = "dev-only-change-me-metahub-approval-hmac-secret-2026"
DEFAULT_GATEWAY_PLACEMENT_SIGNING_SECRET = (
    "dev-only-change-me-metahub-gateway-placement-signing-secret-2026"
)
DEFAULT_EDGE_SNAPSHOT_SIGNING_SECRET = (
    "dev-only-change-me-metahub-edge-snapshot-signing-secret-2026"
)
DEFAULT_EDGE_SUPPORT_BUNDLE_ENCRYPTION_SECRET = (
    "dev-only-change-me-metahub-edge-support-bundle-encryption-secret-2026"
)
# 租户导出 ZIP 与注销留存分段共享一个 PRD-089 根密钥，但通过固定 AAD/domain 分离用途。
# 公开默认值只服务 dev/test；生产缺失时仅 tenant-data 功能关闭失败，不能回退到 Auth/Provider 密钥。
DEFAULT_TENANT_DATA_ENCRYPTION_SECRET = (
    "dev-only-change-me-metahub-tenant-data-encryption-secret-2026"
)

# 运行时启动时间和默认配置集中定义，供健康检查、审计响应和本地默认值复用。
STARTED_AT = datetime.now(UTC)

DEFAULT_CONFIG: dict[str, str] = {
    "METAHUB_ENV": "dev",
    "METAHUB_LOG_LEVEL": "INFO",
    "METAHUB_PUBLIC_BASE_URL": "",
    "METAHUB_CONTROL_API_BASE_URL": "http://localhost:8001",
    "METAHUB_GATEWAY_API_BASE_URL": "http://localhost:8000",
    "METAHUB_ALLOWED_ORIGINS": "http://localhost:5173",
    "METAHUB_SERVICE_MODE": "internal_only",
    "METAHUB_AUTH_TOKEN_SECRET": "dev-only-change-me-metahub-auth-secret",
    "METAHUB_IDENTITY_SESSION_SECRET": DEFAULT_IDENTITY_SESSION_SECRET,
    "METAHUB_IDENTITY_CHALLENGE_SECRET": DEFAULT_IDENTITY_CHALLENGE_SECRET,
    "METAHUB_ENTERPRISE_SSO_HMAC_SECRET": DEFAULT_ENTERPRISE_SSO_HMAC_SECRET,
    "METAHUB_ENTERPRISE_SSO_ENCRYPTION_SECRET": DEFAULT_ENTERPRISE_SSO_ENCRYPTION_SECRET,
    "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET": DEFAULT_IDENTITY_PII_ENCRYPTION_SECRET,
    "METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE": "disabled",
    "METAHUB_WORKSPACE_SESSION_SECRET": DEFAULT_WORKSPACE_SESSION_SECRET,
    "METAHUB_PLATFORM_SESSION_SECRET": DEFAULT_PLATFORM_SESSION_SECRET,
    "METAHUB_PLATFORM_SESSION_TTL_SECONDS": "3600",
    "METAHUB_SUPPORT_SESSION_SECRET": DEFAULT_SUPPORT_SESSION_SECRET,
    "METAHUB_SUPPORT_SESSION_TTL_SECONDS": "900",
    "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET": DEFAULT_IDENTITY_MFA_ENCRYPTION_SECRET,
    "METAHUB_PLATFORM_BOOTSTRAP_ENABLED": "false",
    "METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET": DEFAULT_PLATFORM_BOOTSTRAP_RECOVERY_SECRET,
    "METAHUB_WORKSPACE_ALLOWED_REGIONS": "cn-north-1,cn-east-1",
    "METAHUB_WORKSPACE_RESIDENCY_POLICY_VERSION": "cn-residency-v1",
    "METAHUB_CACHE_AFFINITY_HMAC_SECRET": DEFAULT_CACHE_AFFINITY_HMAC_SECRET,
    "METAHUB_GATEWAY_PLACEMENT_ENABLED": "false",
    "METAHUB_GATEWAY_CELL_CODE": "",
    "METAHUB_GATEWAY_PLACEMENT_SIGNING_VERSION": "1",
    "METAHUB_GATEWAY_PLACEMENT_SIGNING_SECRET": "",
    "METAHUB_GATEWAY_PLACEMENT_SNAPSHOT_TTL_SECONDS": "30",
    "METAHUB_EDGE_SNAPSHOT_SIGNING_VERSION": "1",
    "METAHUB_EDGE_SNAPSHOT_SIGNING_SECRET": "",
    "METAHUB_GATEWAY_CAPACITY_TENANT_LIMIT": "100",
    "METAHUB_GATEWAY_CAPACITY_WORKSPACE_LIMIT": "50",
    "METAHUB_GATEWAY_CAPACITY_KEY_LIMIT": "10",
    "METAHUB_GATEWAY_CAPACITY_CELL_LIMIT": "500",
    # 企业认证字段使用独立根密钥；空生产配置只关闭对应业务入口，不得回退其他密钥域。
    "METAHUB_BUSINESS_VERIFICATION_KEY_VERSION": "1",
    "METAHUB_BUSINESS_VERIFICATION_ENCRYPTION_SECRET": "",
    "METAHUB_BUSINESS_VERIFICATION_DOWNLOAD_TTL_SECONDS": "120",
    # 支付验签与原始 receipt 密文使用两个独立根域；上一验签 key 只在显式轮换窗口启用。
    "METAHUB_PAYMENT_WEBHOOK_KEY_VERSION": "1",
    "METAHUB_PAYMENT_WEBHOOK_SECRET": "",
    "METAHUB_PAYMENT_WEBHOOK_PREVIOUS_KEY_VERSION": "",
    "METAHUB_PAYMENT_WEBHOOK_PREVIOUS_SECRET": "",
    "METAHUB_PAYMENT_RECEIPT_ENCRYPTION_KEY_VERSION": "1",
    "METAHUB_PAYMENT_RECEIPT_ENCRYPTION_SECRET": "",
    "METAHUB_PAYMENT_RECEIPT_STORAGE_ROOT": ".data/payment-receipts",
    "METAHUB_PAYMENT_WEBHOOK_TOLERANCE_SECONDS": "300",
    "METAHUB_PAYMENT_WEBHOOK_MAX_BODY_BYTES": "65536",
    "METAHUB_PAYMENT_WORKER_POLL_INTERVAL_SECONDS": "2",
    # PRD-100 keeps local/test SQLite explicit while production uses PostgreSQL.
    # Infrastructure credentials remain absent by default and are always masked.
    "METAHUB_DATABASE_POOL_MIN_SIZE": "1",
    "METAHUB_DATABASE_POOL_MAX_SIZE": "10",
    "METAHUB_DATABASE_POOL_TIMEOUT_SECONDS": "3",
    "METAHUB_DATABASE_STATEMENT_TIMEOUT_MS": "30000",
    "METAHUB_DATABASE_LOCK_TIMEOUT_MS": "5000",
    "METAHUB_INFRASTRUCTURE_PROBE_TIMEOUT_SECONDS": "3",
    "METAHUB_OBJECT_STORE_ENDPOINT_URL": "",
    "METAHUB_OBJECT_STORE_REGION": "",
    "METAHUB_OBJECT_STORE_BUCKET": "",
    "METAHUB_OBJECT_STORE_ACCESS_KEY_ID": "",
    "METAHUB_OBJECT_STORE_SECRET_ACCESS_KEY": "",
    "METAHUB_OBJECT_STORE_FORCE_PATH_STYLE": "true",
    "METAHUB_KMS_PROVIDER": "disabled",
    "METAHUB_KMS_KEY_ID": "",
    "METAHUB_KMS_TEST_MASTER_KEY": "",
    "METAHUB_CONFIG_DISTRIBUTION_SIGNING_KEY_ID": "",
    "METAHUB_STATUS_BUNDLE_ROOT": ".data/status-page",
    "METAHUB_STATUS_BUNDLE_TTL_SECONDS": "86400",
    "METAHUB_BACKUP_WORKER_ENABLED": "false",
    "METAHUB_BACKUP_POLL_INTERVAL_SECONDS": "5",
    "METAHUB_BACKUP_DEFAULT_RETENTION_DAYS": "30",
    "METAHUB_BACKUP_RESTORE_DATABASE_URL": "",
    "METAHUB_CAPACITY_RUNNER_MODE": "disabled",
    "METAHUB_CAPACITY_RUNNER_BASE_URL": "",
    "METAHUB_CAPACITY_RUNNER_INTERNAL_TOKEN": "",
    "METAHUB_CAPACITY_RUNNER_PERMIT_SECRET": "",
    "METAHUB_CAPACITY_RUNNER_HTTP_TIMEOUT_SECONDS": "30",
    # Response Cache 是可选能力；旧部署升级后必须保持关闭，不能因默认密钥存在而读取缓存。
    "METAHUB_RESPONSE_CACHE_ENABLED": "false",
    "METAHUB_RESPONSE_CACHE_HMAC_VERSION": "1",
    "METAHUB_RESPONSE_CACHE_HMAC_SECRET": "",
    "METAHUB_RESPONSE_CACHE_ENCRYPTION_VERSION": "1",
    "METAHUB_RESPONSE_CACHE_ENCRYPTION_SECRET": "",
    "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_VERSION": "1",
    "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_SECRET": "",
    "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_PREVIOUS_VERSION": "",
    "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_PREVIOUS_SECRET": "",
    "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_VERSION": "1",
    "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_SECRET": "",
    "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_PREVIOUS_VERSION": "",
    "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_PREVIOUS_SECRET": "",
    "METAHUB_RESPONSE_CACHE_MAX_READY_ENTRIES_PER_TENANT": "10000",
    "METAHUB_RESPONSE_CACHE_MAX_PLAINTEXT_BYTES": "1048576",
    "METAHUB_RESPONSE_CACHE_FILL_LEASE_SECONDS": "90",
    "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_ROOT": ".data/response-cache-failure-spool",
    "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_POLL_INTERVAL_SECONDS": "2",
    "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_CLAIM_LEASE_SECONDS": "60",
    # MCP defaults to disabled and cannot open tenant sockets in production.  Dev/test receive
    # two public, independent local roots at load time; production never synthesizes a key.
    "METAHUB_MCP_ENABLED": "false",
    "METAHUB_MCP_ENCRYPTION_VERSION": "1",
    "METAHUB_MCP_ENCRYPTION_SECRET": "",
    "METAHUB_MCP_ENCRYPTION_PREVIOUS_VERSION": "",
    "METAHUB_MCP_ENCRYPTION_PREVIOUS_SECRET": "",
    "METAHUB_MCP_HMAC_VERSION": "1",
    "METAHUB_MCP_HMAC_SECRET": "",
    "METAHUB_MCP_HMAC_PREVIOUS_VERSION": "",
    "METAHUB_MCP_HMAC_PREVIOUS_SECRET": "",
    "METAHUB_MCP_EXECUTOR_MODE": "disabled",
    "METAHUB_MCP_TEST_LOOPBACK_ENABLED": "false",
    "METAHUB_MCP_SCHEDULER_ENABLED": "false",
    "METAHUB_MCP_SCHEDULER_POLL_INTERVAL_SECONDS": "2",
    "METAHUB_MCP_SCHEDULER_BATCH_SIZE": "20",
    "METAHUB_MCP_GLOBAL_CONCURRENCY": "4",
    "METAHUB_MCP_TENANT_CONCURRENCY": "1",
    "METAHUB_MCP_OPERATION_LEASE_SECONDS": "60",
    "METAHUB_MCP_INVOCATION_LEASE_SECONDS": "90",
    "METAHUB_MCP_PROBE_DEADLINE_SECONDS": "10",
    "METAHUB_MCP_CALL_DEADLINE_SECONDS": "30",
    # PRD-083 production execution is Scheduler -> isolated Sandbox only.  Empty secret/URL
    # defaults preserve disabled upgrades; enabling sandbox_http requires the complete set.
    "METAHUB_MCP_SANDBOX_BASE_URL": "",
    "METAHUB_MCP_SANDBOX_INTERNAL_TOKEN": "",
    "METAHUB_MCP_SANDBOX_ENVELOPE_SECRET": "",
    "METAHUB_MCP_SANDBOX_RESPONSE_KEY_VERSION": "1",
    "METAHUB_MCP_SANDBOX_RESPONSE_SECRET_CURRENT": "",
    "METAHUB_MCP_SANDBOX_RESPONSE_SECRET_PREVIOUS": "",
    "METAHUB_MCP_NETWORK_CONNECT_TIMEOUT_MAX_SECONDS": "5",
    "METAHUB_MCP_NETWORK_PROBE_TIMEOUT_MAX_SECONDS": "10",
    "METAHUB_MCP_NETWORK_CALL_TIMEOUT_MAX_SECONDS": "30",
    "METAHUB_MCP_NETWORK_FAILURE_THRESHOLD_MAX": "10",
    "METAHUB_MCP_NETWORK_OPEN_SECONDS_MAX": "3600",
    "METAHUB_MCP_REGISTER_MAX_BYTES": "16384",
    "METAHUB_MCP_REFRESH_MAX_BYTES": "4096",
    "METAHUB_MCP_POLICY_MAX_BYTES": "32768",
    "METAHUB_MCP_INVOKE_MAX_BYTES": "131072",
    "METAHUB_MCP_ARGUMENTS_MAX_BYTES": "65536",
    "METAHUB_MCP_SCHEMA_MAX_BYTES": "65536",
    "METAHUB_MCP_SERVER_SCHEMA_MAX_BYTES": "1048576",
    "METAHUB_MCP_RESULT_MAX_BYTES": "1048576",
    "METAHUB_MCP_TREE_MAX_DEPTH": "12",
    "METAHUB_MCP_TREE_MAX_NODES": "1024",
    "METAHUB_MCP_SCHEMA_MAX_PROPERTIES": "256",
    "METAHUB_MCP_SCHEMA_MAX_KEYWORDS": "2048",
    "METAHUB_MCP_SCHEMA_MAX_COMBINATOR_BRANCHES": "64",
    "METAHUB_MCP_SCHEMA_MAX_ENUM_ITEMS": "256",
    "METAHUB_MCP_SCHEMA_MAX_ENUM_ITEM_BYTES": "4096",
    "METAHUB_MCP_SCHEMA_MAX_PATTERN_LENGTH": "512",
    "METAHUB_MCP_VALIDATOR_MAX_STEPS": "100000",
    "METAHUB_MCP_MAX_STRING_LENGTH": "65536",
    "METAHUB_MCP_MAX_ARRAY_ITEMS": "1024",
    # 空默认值让生产与未启用 Chat 的部署保持可启动；load 阶段仅在 dev/test 回环环境
    # 注入公开的本地开发值，内部 Chat 入口遇到生产缺失值必须失败关闭。
    "METAHUB_INTERNAL_CHAT_TOKEN": "",
    # Batch 使用独立服务身份和加密根域。空默认让未启用 Batch 的旧部署继续启动；
    # dev/test 在加载期注入公开本地值，生产和启用的 batch-worker 必须显式配置。
    "METAHUB_INTERNAL_BATCH_TOKEN": "",
    "METAHUB_BATCH_ENCRYPTION_SECRET": "",
    "METAHUB_BATCH_WORKER_ENABLED": "false",
    # Production and direct launches retain recovery workers. The local
    # three-service launcher explicitly disables them for a lean dev profile.
    "METAHUB_CONTROL_BACKGROUND_SCHEDULERS_ENABLED": "true",
    "METAHUB_BATCH_POLL_INTERVAL_SECONDS": "1",
    "METAHUB_BATCH_GLOBAL_CONCURRENCY": "2",
    "METAHUB_BATCH_TENANT_CONCURRENCY": "1",
    "METAHUB_BATCH_LEASE_SECONDS": "90",
    "METAHUB_BATCH_HEARTBEAT_SECONDS": "30",
    "METAHUB_BATCH_GATEWAY_CONCURRENCY": "1",
    "METAHUB_BATCH_REALTIME_COOLDOWN_MILLISECONDS": "250",
    "METAHUB_BATCH_REALTIME_RESERVE_PERCENT": "25",
    "METAHUB_BATCH_MAX_PENDING_JOBS_PER_TENANT": "100",
    "METAHUB_BATCH_MAX_PENDING_TASKS_PER_TENANT": "10000",
    "METAHUB_BATCH_MAX_RESULT_BYTES": "1048576",
    # 在线实验不新增 secret；调度、租约和硬截止均为有界部署事实，客户端不能覆盖。
    "METAHUB_EXPERIMENT_SCHEDULER_INTERVAL_SECONDS": "5",
    "METAHUB_EXPERIMENT_FINALIZER_LEASE_SECONDS": "90",
    "METAHUB_EXPERIMENT_EXECUTION_LEASE_SECONDS": "90",
    "METAHUB_EXPERIMENT_EXECUTION_HEARTBEAT_SECONDS": "20",
    "METAHUB_EXPERIMENT_SCHEDULER_BATCH_LIMIT": "20",
    "METAHUB_EXPERIMENT_EXECUTION_MAX_SECONDS": "3600",
    "METAHUB_EXPERIMENT_SETTLEMENT_GRACE_SECONDS": "3600",
    # 会话风险默认关闭，避免升级后旧部署因尚未配置 Redis/keyring 改变既有 Chat 行为。
    # 版本可以进入脱敏配置摘要；两项 secret 必须始终按敏感字段屏蔽。
    "METAHUB_SESSION_RISK_ENABLED": "false",
    "METAHUB_SESSION_RISK_HMAC_VERSION": "1",
    "METAHUB_SESSION_RISK_HMAC_SECRET": "",
    "METAHUB_SESSION_RISK_PREVIOUS_HMAC_VERSION": "",
    "METAHUB_SESSION_RISK_PREVIOUS_HMAC_SECRET": "",
    # Approval idempotency, step-up tickets, target fingerprints, and cursors
    # share one independent root while remaining separated by fixed domains.
    "METAHUB_APPROVAL_HMAC_VERSION": "1",
    "METAHUB_APPROVAL_HMAC_SECRET": "",
    "METAHUB_APPROVAL_PREVIOUS_HMAC_VERSION": "",
    "METAHUB_APPROVAL_PREVIOUS_HMAC_SECRET": "",
    "METAHUB_APPROVAL_SCHEDULER_ENABLED": "false",
    "METAHUB_APPROVAL_SCHEDULER_POLL_INTERVAL_SECONDS": "2",
    "METAHUB_APPROVAL_SCHEDULER_BATCH_SIZE": "50",
    "METAHUB_APPROVAL_OUTBOX_ENABLED": "true",
    "METAHUB_APPROVAL_OUTBOX_POLL_INTERVAL_SECONDS": "2",
    "METAHUB_APPROVAL_OUTBOX_BATCH_SIZE": "50",
    "METAHUB_APPROVAL_WRITEBACK_ENABLED": "true",
    "METAHUB_APPROVAL_WRITEBACK_POLL_INTERVAL_SECONDS": "2",
    "METAHUB_APPROVAL_WRITEBACK_BATCH_SIZE": "50",
    # Prompt 模板正文必须使用显式独立根密钥；空默认值只让未启用模板的旧部署继续启动，
    # 任何模板发布、读取或调用都会在业务边界固定失败为暂时不可用。
    "METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET": "",
    # 文件正文使用第四个独立密钥域。dev/test 在 load 阶段注入公开开发值；生产缺失保持
    # None，只让文件 API 503 失败关闭，不能拖垮健康、登录和其他控制面能力。
    "METAHUB_FILE_ENCRYPTION_SECRET": "",
    "METAHUB_TENANT_DATA_ENCRYPTION_VERSION": "1",
    "METAHUB_TENANT_DATA_ENCRYPTION_SECRET": "",
    "METAHUB_TENANT_DATA_SCHEDULER_ENABLED": "true",
    "METAHUB_TENANT_DATA_POLL_INTERVAL_SECONDS": "2",
    "METAHUB_FILE_STORAGE_ROOT": ".data/file-storage",
    "METAHUB_FILE_MAX_UPLOAD_BYTES": "20971520",
    "METAHUB_FILE_PARSE_CONCURRENCY": "2",
    # 文件安全扫描和 OCR 使用独立 limiter；像素、页数和图片数都是部署级硬预算，浏览器不能覆盖。
    "METAHUB_FILE_SECURITY_SCAN_CONCURRENCY": "2",
    "METAHUB_FILE_SECURITY_OCR_CONCURRENCY": "1",
    "METAHUB_FILE_SECURITY_MAX_OCR_PAGES": "20",
    "METAHUB_FILE_SECURITY_MAX_IMAGES": "50",
    "METAHUB_FILE_SECURITY_MAX_IMAGE_PIXELS": "12000000",
    "METAHUB_FILE_SECURITY_MAX_TOTAL_PIXELS": "50000000",
    # FinOps Usage spool 只保存经过白名单筛选的账务恢复数据。默认落在仓库本地数据目录，
    # 生产环境应显式改为 Gateway 服务账号独占的本地磁盘目录，不能指向网络共享。
    "METAHUB_FINOPS_USAGE_SPOOL_ROOT": ".data/finops-usage-spool",
    "METAHUB_FINOPS_USAGE_SPOOL_POLL_INTERVAL_SECONDS": "2",
    "METAHUB_FINOPS_USAGE_SPOOL_CLAIM_LEASE_SECONDS": "60",
    "METAHUB_SESSION_TTL_SECONDS": "28800",
    "METAHUB_LOGIN_MAX_FAILURES": "5",
    "METAHUB_LOGIN_LOCK_SECONDS": "900",
    "METAHUB_DEFAULT_TENANT_SLUG": "default",
    "METAHUB_VERSION": "0.1.0-dev",
    "METAHUB_BUILD_COMMIT": "unknown",
    "METAHUB_BUILD_TIME": "unknown",
    "METAHUB_PROVIDER_HEALTH_ENABLED": "true",
    "METAHUB_PROVIDER_HEALTH_INTERVAL_SECONDS": "60",
    "METAHUB_PROVIDER_HEALTH_TIMEOUT_SECONDS": "5",
    "METAHUB_PROVIDER_HEALTH_FAILURE_THRESHOLD": "3",
    "METAHUB_PROVIDER_HEALTH_LEASE_SECONDS": "20",
    "METAHUB_PROVIDER_HEALTH_STALE_SECONDS": "180",
    "METAHUB_PROVIDER_HEALTH_CONCURRENCY": "1",
    "METAHUB_PROVIDER_HEALTH_PRIVATE_CIDRS": "",
    # 本地模型只允许部署运维开放私网和端口；租户请求不能携带或放宽这些边界。
    "METAHUB_LOCAL_ENDPOINT_PRIVATE_CIDRS": "",
    "METAHUB_LOCAL_ENDPOINT_ALLOWED_PORTS": "80,443,8000,8080,8443,11434",
    "METAHUB_LOCAL_ENDPOINT_ALLOW_LOOPBACK": "false",
    "METAHUB_LOCAL_ENDPOINT_CONNECT_TIMEOUT_SECONDS": "5",
    "METAHUB_LOCAL_ENDPOINT_OVERALL_TIMEOUT_SECONDS": "120",
    "METAHUB_LOCAL_ENDPOINT_MAX_RESPONSE_BYTES": "16777216",
    "METAHUB_LOCAL_ENDPOINT_MAX_STREAM_BYTES": "67108864",
    "METAHUB_LOCAL_SECRET_MANAGEMENT_ENABLED": "false",
    "METAHUB_LOCAL_SECRET_DATABASE_PATH": ".data/private-gateway/local-secrets.sqlite3",
    "METAHUB_LOCAL_SECRET_KEYSTORE_PATH": ".data/private-gateway/local-keystore.json",
    "METAHUB_LOCAL_SECRET_DEPLOYMENT_ID": "",
    "METAHUB_LOCAL_SECRET_ADMIN_HANDOFF_SECRET": "",
    "METAHUB_LOCAL_MANAGEMENT_ALLOWED_ORIGINS": "http://localhost:5173",
    "METAHUB_LOCAL_MANAGEMENT_ALLOWED_CLIENT_CIDRS": "",
    "METAHUB_LOCAL_MANAGEMENT_ALLOW_LOOPBACK": "true",
    "METAHUB_EDGE_BACKUP_ROOT": ".data/private-gateway/edge-backups",
    "METAHUB_EDGE_BACKUP_CUSTOMER_OBJECT_STORE_ROOT": "",
    "METAHUB_EDGE_SUPPORT_BUNDLE_ROOT": ".data/private-gateway/support-bundles",
    "METAHUB_EDGE_SUPPORT_BUNDLE_ENCRYPTION_SECRET": (
        DEFAULT_EDGE_SUPPORT_BUNDLE_ENCRYPTION_SECRET
    ),
    "METAHUB_EDGE_SUPPORT_BUNDLE_TTL_SECONDS": "86400",
    # 用量导出只在控制面运行；同步、总量和字节三层上限共同保护 SQLite 与进程内存。
    "METAHUB_USAGE_EXPORT_ENABLED": "true",
    "METAHUB_USAGE_EXPORT_POLL_INTERVAL_SECONDS": "2",
    "METAHUB_USAGE_EXPORT_SYNC_ROW_LIMIT": "5000",
    "METAHUB_USAGE_EXPORT_MAX_ROWS": "100000",
    "METAHUB_USAGE_EXPORT_MAX_BYTES": "52428800",
    "METAHUB_USAGE_EXPORT_LEASE_SECONDS": "60",
    "METAHUB_USAGE_EXPORT_MAX_ATTEMPTS": "3",
    "METAHUB_USAGE_EXPORT_RETENTION_HOURS": "24",
    # 查看事件 outbox 只做 indexed 单行领取，默认开启以恢复已提交的访问审计事件。
    "METAHUB_REQUEST_EXPLANATION_EVENT_ENABLED": "true",
    "METAHUB_REQUEST_EXPLANATION_EVENT_POLL_INTERVAL_SECONDS": "2",
    # 查询事件 outbox 与请求解释事件使用独立开关，便于运维分别隔离积压或迁移故障；
    # 默认开启只扫描 0045 的 indexed source 状态，不会把 Webhook 网络调用放进 GET 热路径。
    "METAHUB_REQUEST_LOG_QUERY_EVENT_ENABLED": "true",
    "METAHUB_REQUEST_LOG_QUERY_EVENT_POLL_INTERVAL_SECONDS": "2",
    # 管理助手 outbox 默认恢复投递；关闭开关只暂停 drain，不能跳过 Query/Audit/event 原子写入。
    "METAHUB_ADMIN_ASSISTANT_EVENT_ENABLED": "true",
    "METAHUB_ADMIN_ASSISTANT_EVENT_POLL_INTERVAL_SECONDS": "2",
    # 成本异常检测默认关闭，避免旧部署升级后未经容量评估就启动历史账本扫描。
    # 规则参数使用整数毫单位/微元，禁止浮点配置改变阈值边界。
    "METAHUB_COST_ANOMALY_ENABLED": "false",
    "METAHUB_COST_ANOMALY_POLL_INTERVAL_SECONDS": "30",
    "METAHUB_COST_ANOMALY_SPIKE_MULTIPLIER_MILLIS": "5000",
    "METAHUB_COST_ANOMALY_MIN_COST_MICROS": "10000",
    "METAHUB_COST_ANOMALY_WORKDAY_START_HOUR": "9",
    "METAHUB_COST_ANOMALY_WORKDAY_END_HOUR": "18",
    # 策略集共用一个极短 deadline；节点与策略边界检查超时后必须 fail closed。
    "METAHUB_POLICY_EVALUATION_TIMEOUT_MS": "5",
    # 策略快照同步默认关闭，避免旧环境或单元测试启动后意外访问控制面。
    "METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED": "false",
    "METAHUB_POLICY_SNAPSHOT_POLL_INTERVAL_SECONDS": "2",
    "METAHUB_POLICY_SNAPSHOT_HTTP_TIMEOUT_SECONDS": "2",
    "METAHUB_POLICY_SNAPSHOT_MAX_AGE_SECONDS": "300",
    "METAHUB_POLICY_SNAPSHOT_EMERGENCY_MAX_AGE_SECONDS": "3600",
    "METAHUB_POLICY_SNAPSHOT_MAX_PAYLOAD_BYTES": "4194304",
    # mask 运行时默认关闭；必须先完成全部 Gateway 滚动升级和 readiness 自检，再由运维显式开启。
    "METAHUB_POLICY_MASK_RUNTIME_ENABLED": "false",
    # Provider 错误映射是独立诊断配置；默认关闭可保持旧部署零网络副作用。
    "METAHUB_PROVIDER_ERROR_MAP_SYNC_ENABLED": "false",
    "METAHUB_PROVIDER_ERROR_MAP_POLL_INTERVAL_SECONDS": "2",
    "METAHUB_PROVIDER_ERROR_MAP_HTTP_TIMEOUT_SECONDS": "2",
    "METAHUB_PROVIDER_ERROR_MAP_REFRESH_QUEUE_TIMEOUT_SECONDS": "2",
    "METAHUB_PROVIDER_ERROR_MAP_FRESH_SECONDS": "300",
    "METAHUB_PROVIDER_ERROR_MAP_LKG_SECONDS": "3600",
    "METAHUB_PROVIDER_ERROR_MAP_MAX_PAYLOAD_BYTES": "262144",
    "METAHUB_PROVIDER_ERROR_MAP_REFRESH_CONCURRENCY": "4",
    # 流式正文禁用 httpx read timeout，由数据面块间看门狗统一判定上游停顿。
    "METAHUB_STREAM_INTER_TOKEN_TIMEOUT_SECONDS": "30",
    "METAHUB_STREAM_CONNECT_TIMEOUT_SECONDS": "30",
    "METAHUB_STREAM_WRITE_TIMEOUT_SECONDS": "30",
    "METAHUB_STREAM_POOL_TIMEOUT_SECONDS": "5",
    # 输出安全边界属于部署级安全合同，租户请求不得动态放宽。
    "METAHUB_OUTPUT_SAFETY_INTERVAL_TOKENS": "32",
    "METAHUB_OUTPUT_SAFETY_WINDOW_TOKENS": "256",
    "METAHUB_OUTPUT_SAFETY_MAX_FULL_TEXT_CODE_POINTS": "262144",
}


class ConfigError(ValueError):
    """配置错误。

    code/message/suggestion 三段式结构会被 FastAPI 异常处理器转换为统一 API 错误。
    后续 PRD-004 可以把这些错误码迁到集中错误码表，但 PRD-002 先把契约固定下来。
    """

    def __init__(self, code: str, message: str, suggestion: str) -> None:
        """在 运行配置 内部复用 init 辅助逻辑，集中处理边界值和错误映射。

        边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
        """

        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestion = suggestion


@dataclass(frozen=True)
class ConfigValue:
    """单个配置项的原始值与来源。

    原始值只在进程内使用，不能直接返回给 API 或写入日志。对外只能使用
    ConfigItemSummary 中的 value_preview。
    """

    value: str
    source: ConfigSource


class EnvironmentInfo(BaseModel):
    """承载 运行配置 的 EnvironmentInfo 数据结构，明确跨模块传递时允许出现的字段。

    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    name: EnvironmentName
    is_production: bool
    config_profile: str
    service_mode: ServiceMode
    loaded_at: datetime


class ConfigItemSummary(BaseModel):
    """承载 运行配置 的 ConfigItemSummary 数据结构，明确跨模块传递时允许出现的字段。

    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    key: str
    source: ConfigSource
    status: ConfigStatus
    value_preview: str


class ConfigSummary(BaseModel):
    """承载 运行配置 的 ConfigSummary 数据结构，明确跨模块传递时允许出现的字段。

    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    service_name: str
    environment: EnvironmentName
    loaded_at: datetime
    items: list[ConfigItemSummary]
    warnings: list[str]
    request_id: str


class AppConfig(BaseModel):
    """服务运行配置。

    这个对象可以保存运行时需要用到的真实配置，但敏感字段必须设置 exclude=True，
    避免被 model_dump、日志或接口摘要意外输出。面向控制台的只读摘要使用
    config_items，而不是这些原始字段。
    """

    service_name: str
    environment: EnvironmentName = "dev"
    environment_info: EnvironmentInfo
    log_level: str = "INFO"
    public_base_url: str = ""
    control_api_base_url: str = "http://localhost:8001"
    gateway_api_base_url: str = "http://localhost:8000"
    allowed_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    workspace_allowed_regions: tuple[str, ...] = ("cn-north-1", "cn-east-1")
    workspace_residency_policy_version: str = "cn-residency-v1"
    gateway_placement_enabled: bool = False
    gateway_cell_code: str | None = None
    gateway_placement_signing_version: int = 1
    gateway_placement_signing_secret: str | None = Field(
        default=None, exclude=True, repr=False
    )
    gateway_placement_snapshot_ttl_seconds: int = 30
    edge_snapshot_signing_version: int = 1
    edge_snapshot_signing_secret: str | None = Field(
        default=None, exclude=True, repr=False
    )
    gateway_capacity_tenant_limit: int = 100
    gateway_capacity_workspace_limit: int = 50
    gateway_capacity_key_limit: int = 10
    gateway_capacity_cell_limit: int = 500
    business_verification_key_version: int = 1
    business_verification_encryption_secret: str | None = Field(
        default=None, exclude=True, repr=False
    )
    business_verification_download_ttl_seconds: int = 120
    payment_webhook_key_version: int = 1
    payment_webhook_secret: str | None = Field(default=None, exclude=True, repr=False)
    payment_webhook_previous_key_version: int | None = None
    payment_webhook_previous_secret: str | None = Field(default=None, exclude=True, repr=False)
    payment_receipt_encryption_key_version: int = 1
    payment_receipt_encryption_secret: str | None = Field(
        default=None, exclude=True, repr=False
    )
    payment_receipt_storage_root: str = Field(
        default=".data/payment-receipts", exclude=True, repr=False
    )
    payment_webhook_tolerance_seconds: int = 300
    payment_webhook_max_body_bytes: int = 65_536
    payment_worker_poll_interval_seconds: int = 2
    service_mode: ServiceMode = "internal_only"
    provider_base_urls: dict[str, str] = Field(default_factory=dict)
    features: dict[str, bool] = Field(default_factory=dict)
    config_items: list[ConfigItemSummary] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    sensitive_keys_masked: bool = True
    database_url: str | None = Field(default=None, exclude=True, repr=False)
    redis_url: str | None = Field(default=None, exclude=True, repr=False)
    database_pool_min_size: int = 1
    database_pool_max_size: int = 10
    database_pool_timeout_seconds: int = 3
    database_statement_timeout_ms: int = 30_000
    database_lock_timeout_ms: int = 5_000
    infrastructure_probe_timeout_seconds: int = 3
    object_store_endpoint_url: str | None = Field(default=None, exclude=True, repr=False)
    object_store_region: str | None = Field(default=None, exclude=True, repr=False)
    object_store_bucket: str | None = Field(default=None, exclude=True, repr=False)
    object_store_access_key_id: str | None = Field(default=None, exclude=True, repr=False)
    object_store_secret_access_key: str | None = Field(default=None, exclude=True, repr=False)
    object_store_force_path_style: bool = True
    kms_provider: Literal["disabled", "aws", "test"] = "disabled"
    kms_key_id: str | None = Field(default=None, exclude=True, repr=False)
    kms_test_master_key: str | None = Field(default=None, exclude=True, repr=False)
    config_distribution_signing_key_id: str | None = Field(
        default=None, exclude=True, repr=False
    )
    status_bundle_root: str = Field(default=".data/status-page", exclude=True, repr=False)
    status_bundle_ttl_seconds: int = 86_400
    edge_support_bundle_root: str = Field(
        default=".data/private-gateway/support-bundles", exclude=True, repr=False
    )
    edge_support_bundle_encryption_secret: str | None = Field(
        default=None, exclude=True, repr=False
    )
    edge_support_bundle_ttl_seconds: int = 86_400
    backup_worker_enabled: bool = False
    backup_poll_interval_seconds: int = 5
    backup_default_retention_days: int = 30
    backup_restore_database_url: str | None = Field(default=None, exclude=True, repr=False)
    capacity_runner_mode: CapacityRunnerMode = "disabled"
    capacity_runner_base_url: str | None = Field(default=None, exclude=True, repr=False)
    capacity_runner_internal_token: str | None = Field(default=None, exclude=True, repr=False)
    capacity_runner_permit_secret: str | None = Field(default=None, exclude=True, repr=False)
    capacity_runner_http_timeout_seconds: int = 30
    provider_api_keys: dict[str, str] = Field(default_factory=dict, exclude=True, repr=False)
    internal_config_token: str | None = Field(default=None, exclude=True, repr=False)
    # 内部 Chat Token 只供 Control 与 Gateway 之间的服务身份校验，不能复用用户 Session、
    # Virtual Key 或策略同步 Token，也不能通过 model_dump/repr 意外泄露。
    internal_chat_token: str | None = Field(default=None, exclude=True, repr=False)
    # Batch 服务身份与正文/结果根密钥都是独立敏感域；数值门限是部署事实，HTTP 不能覆盖。
    internal_batch_token: str | None = Field(default=None, exclude=True, repr=False)
    batch_encryption_secret: str | None = Field(default=None, exclude=True, repr=False)
    batch_worker_enabled: bool = False
    control_background_schedulers_enabled: bool = True
    batch_poll_interval_seconds: float = 1.0
    batch_global_concurrency: int = 2
    batch_tenant_concurrency: int = 1
    batch_lease_seconds: int = 90
    batch_heartbeat_seconds: int = 30
    batch_gateway_concurrency: int = 1
    batch_realtime_cooldown_milliseconds: int = 250
    batch_realtime_reserve_percent: int = 25
    batch_max_pending_jobs_per_tenant: int = 100
    batch_max_pending_tasks_per_tenant: int = 10_000
    batch_max_result_bytes: int = 1_048_576
    experiment_scheduler_interval_seconds: int = 5
    experiment_finalizer_lease_seconds: int = 90
    experiment_execution_lease_seconds: int = 90
    experiment_execution_heartbeat_seconds: int = 20
    experiment_scheduler_batch_limit: int = 20
    experiment_execution_max_seconds: int = 3600
    experiment_settlement_grace_seconds: int = 3600
    # 多轮风险开关与 key 版本是部署级合同；secret 使用独立根域且绝不能进入序列化、repr、
    # 配置摘要明文或业务日志。previous 只服务于受控轮换，不得自动替代缺失 current。
    session_risk_enabled: bool = False
    session_risk_hmac_version: int = 1
    session_risk_hmac_secret: str | None = Field(default=None, exclude=True, repr=False)
    session_risk_previous_hmac_version: int | None = None
    session_risk_previous_hmac_secret: str | None = Field(default=None, exclude=True, repr=False)
    # Approval secrets are excluded from both repr and serialization. The
    # retained key is verification-only; all new facts use the current key.
    approval_hmac_version: int = 1
    approval_hmac_secret: str | None = Field(default=None, exclude=True, repr=False)
    approval_previous_hmac_version: int | None = None
    approval_previous_hmac_secret: str | None = Field(default=None, exclude=True, repr=False)
    approval_max_pending_tasks_per_tenant: int = 1_000
    approval_scheduler_enabled: bool = False
    approval_scheduler_poll_interval_seconds: float = 2.0
    approval_scheduler_batch_size: int = 50
    approval_outbox_enabled: bool = True
    approval_outbox_poll_interval_seconds: float = 2.0
    approval_outbox_batch_size: int = 50
    approval_writeback_enabled: bool = True
    approval_writeback_poll_interval_seconds: float = 2.0
    approval_writeback_batch_size: int = 50
    # 模板正文、变量 schema 和 change summary 共用同一独立根密钥并在实现中分域派生；
    # 缺失值允许非模板功能启动，但模板 API/Gateway 必须失败关闭，绝不回退 Auth/Provider 密钥。
    prompt_template_encryption_secret: str | None = Field(default=None, exclude=True, repr=False)
    # 原文件、抽取正文和文件名使用独立根密钥；root 也可能泄露宿主结构，两者均不参与
    # model_dump/repr。数值上限是部署级资源门禁，租户请求不能动态覆盖。
    file_encryption_secret: str | None = Field(default=None, exclude=True, repr=False)
    # PRD-089 的导出制品和留存归档只允许使用独立密钥域；secret 不参与 repr/model_dump。
    tenant_data_encryption_version: int = 1
    tenant_data_encryption_secret: str | None = Field(default=None, exclude=True, repr=False)
    tenant_data_scheduler_enabled: bool = True
    tenant_data_poll_interval_seconds: int = 2
    file_storage_root: str = Field(default=".data/file-storage", exclude=True, repr=False)
    file_max_upload_bytes: int = 20_971_520
    file_parse_concurrency: int = 2
    # 安全扫描与 OCR 分开限流，防止低成本 TXT 被昂贵视觉任务耗尽同一容量池。
    file_security_scan_concurrency: int = 2
    file_security_ocr_concurrency: int = 1
    file_security_max_ocr_pages: int = 20
    file_security_max_images: int = 50
    file_security_max_image_pixels: int = 12_000_000
    file_security_max_total_pixels: int = 50_000_000
    auth_token_secret: str = Field(exclude=True, repr=False)
    identity_session_secret: str = Field(
        default=DEFAULT_IDENTITY_SESSION_SECRET,
        exclude=True,
        repr=False,
    )
    identity_challenge_secret: str = Field(
        default=DEFAULT_IDENTITY_CHALLENGE_SECRET,
        exclude=True,
        repr=False,
    )
    enterprise_sso_hmac_secret: str = Field(
        default=DEFAULT_ENTERPRISE_SSO_HMAC_SECRET,
        exclude=True,
        repr=False,
    )
    enterprise_sso_encryption_secret: str = Field(
        default=DEFAULT_ENTERPRISE_SSO_ENCRYPTION_SECRET,
        exclude=True,
        repr=False,
    )
    workspace_session_secret: str = Field(
        default=DEFAULT_WORKSPACE_SESSION_SECRET,
        exclude=True,
        repr=False,
    )
    workspace_session_ttl_seconds: int = 7200
    platform_session_secret: str = Field(
        default=DEFAULT_PLATFORM_SESSION_SECRET, exclude=True, repr=False
    )
    platform_session_ttl_seconds: int = 3600
    support_session_secret: str = Field(
        default=DEFAULT_SUPPORT_SESSION_SECRET, exclude=True, repr=False
    )
    support_session_ttl_seconds: int = 900
    identity_mfa_encryption_secret: str = Field(
        default=DEFAULT_IDENTITY_MFA_ENCRYPTION_SECRET, exclude=True, repr=False
    )
    platform_bootstrap_enabled: bool = False
    platform_bootstrap_recovery_secret: str = Field(
        default=DEFAULT_PLATFORM_BOOTSTRAP_RECOVERY_SECRET, exclude=True, repr=False
    )
    identity_pii_encryption_secret: str = Field(
        default=DEFAULT_IDENTITY_PII_ENCRYPTION_SECRET,
        exclude=True,
        repr=False,
    )
    identity_challenge_delivery_mode: IdentityChallengeDeliveryMode = "disabled"
    # Prompt 指纹根密钥与登录 Token 密钥必须分域，且不得进入 model_dump、repr 或配置摘要明文。
    cache_affinity_hmac_secret: str = Field(exclude=True, repr=False)
    # Response Cache 的部署级总开关默认关闭；四个根密钥均从 repr/model_dump 排除。
    # Control/failure spool 的 previous pair 只服务受控轮换读取，永远不能成为新写入 key。
    response_cache_enabled: bool = False
    response_cache_hmac_version: int = 1
    response_cache_hmac_secret: str | None = Field(default=None, exclude=True, repr=False)
    response_cache_encryption_version: int = 1
    response_cache_encryption_secret: str | None = Field(default=None, exclude=True, repr=False)
    response_cache_control_idempotency_version: int = 1
    response_cache_control_idempotency_secret: str | None = Field(
        default=None, exclude=True, repr=False
    )
    response_cache_control_idempotency_previous_version: int | None = None
    response_cache_control_idempotency_previous_secret: str | None = Field(
        default=None, exclude=True, repr=False
    )
    response_cache_failure_spool_version: int = 1
    response_cache_failure_spool_secret: str | None = Field(default=None, exclude=True, repr=False)
    response_cache_failure_spool_previous_version: int | None = None
    response_cache_failure_spool_previous_secret: str | None = Field(
        default=None, exclude=True, repr=False
    )
    response_cache_max_ready_entries_per_tenant: int = 10_000
    response_cache_max_plaintext_bytes: int = 1_048_576
    response_cache_fill_lease_seconds: int = 90
    response_cache_failure_spool_root: str = Field(
        default=".data/response-cache-failure-spool", exclude=True, repr=False
    )
    response_cache_failure_spool_poll_interval_seconds: int = 2
    response_cache_failure_spool_claim_lease_seconds: int = 60
    # MCP cryptographic roots and executor controls are deployment-owned.  Secret fields never
    # participate in model_dump/repr; previous keys are retained for bounded reads only.
    mcp_enabled: bool = False
    mcp_encryption_version: int = 1
    mcp_encryption_secret: str | None = Field(default=None, exclude=True, repr=False)
    mcp_encryption_previous_version: int | None = None
    mcp_encryption_previous_secret: str | None = Field(default=None, exclude=True, repr=False)
    mcp_hmac_version: int = 1
    mcp_hmac_secret: str | None = Field(default=None, exclude=True, repr=False)
    mcp_hmac_previous_version: int | None = None
    mcp_hmac_previous_secret: str | None = Field(default=None, exclude=True, repr=False)
    mcp_executor_mode: McpExecutorMode = "disabled"
    mcp_test_loopback_enabled: bool = False
    mcp_scheduler_enabled: bool = False
    mcp_scheduler_poll_interval_seconds: float = 2.0
    mcp_scheduler_batch_size: int = 20
    mcp_global_concurrency: int = 4
    mcp_tenant_concurrency: int = 1
    mcp_operation_lease_seconds: int = 60
    mcp_invocation_lease_seconds: int = 90
    mcp_probe_deadline_seconds: int = 10
    mcp_call_deadline_seconds: int = 30
    mcp_sandbox_base_url: str | None = Field(default=None, exclude=True, repr=False)
    mcp_sandbox_internal_token: str | None = Field(default=None, exclude=True, repr=False)
    mcp_sandbox_envelope_secret: str | None = Field(default=None, exclude=True, repr=False)
    mcp_sandbox_response_key_version: int = 1
    mcp_sandbox_response_secret_current: str | None = Field(default=None, exclude=True, repr=False)
    mcp_sandbox_response_secret_previous: str | None = Field(default=None, exclude=True, repr=False)
    mcp_network_connect_timeout_max_seconds: int = 5
    mcp_network_probe_timeout_max_seconds: int = 10
    mcp_network_call_timeout_max_seconds: int = 30
    mcp_network_failure_threshold_max: int = 10
    mcp_network_open_seconds_max: int = 3600
    mcp_register_max_bytes: int = 16_384
    mcp_refresh_max_bytes: int = 4_096
    mcp_policy_max_bytes: int = 32_768
    mcp_invoke_max_bytes: int = 131_072
    mcp_arguments_max_bytes: int = 65_536
    mcp_schema_max_bytes: int = 65_536
    mcp_server_schema_max_bytes: int = 1_048_576
    mcp_result_max_bytes: int = 1_048_576
    mcp_tree_max_depth: int = 12
    mcp_tree_max_nodes: int = 1_024
    mcp_schema_max_properties: int = 256
    mcp_schema_max_keywords: int = 2_048
    mcp_schema_max_combinator_branches: int = 64
    mcp_schema_max_enum_items: int = 256
    mcp_schema_max_enum_item_bytes: int = 4_096
    mcp_schema_max_pattern_length: int = 512
    mcp_validator_max_steps: int = 100_000
    mcp_max_string_length: int = 65_536
    mcp_max_array_items: int = 1_024
    # spool 路径可能暴露宿主机目录结构，因此与数据库 URL 一样禁止进入 model_dump/repr。
    # HMAC 复用 auth_token_secret 但在实现中使用独立域分隔，不能直接对正文签名。
    finops_usage_spool_root: str = Field(
        default=".data/finops-usage-spool", exclude=True, repr=False
    )
    finops_usage_spool_poll_interval_seconds: int = 2
    finops_usage_spool_claim_lease_seconds: int = 60
    session_ttl_seconds: int = 28800
    login_max_failures: int = 5
    login_lock_seconds: int = 900
    default_tenant_slug: str = "default"
    provider_health_enabled: bool = True
    provider_health_interval_seconds: int = 60
    provider_health_timeout_seconds: int = 5
    provider_health_failure_threshold: int = 3
    provider_health_lease_seconds: int = 20
    provider_health_stale_seconds: int = 180
    provider_health_concurrency: int = 1
    provider_health_private_cidrs: list[str] = Field(default_factory=list)
    local_endpoint_private_cidrs: list[str] = Field(default_factory=list)
    local_endpoint_allowed_ports: list[int] = Field(
        default_factory=lambda: [80, 443, 8000, 8080, 8443, 11434]
    )
    local_endpoint_allow_loopback: bool = False
    local_endpoint_connect_timeout_seconds: int = 5
    local_endpoint_overall_timeout_seconds: int = 120
    local_endpoint_max_response_bytes: int = 16_777_216
    local_endpoint_max_stream_bytes: int = 67_108_864
    local_secret_management_enabled: bool = False
    local_secret_database_path: str = Field(
        default=".data/private-gateway/local-secrets.sqlite3", exclude=True, repr=False
    )
    local_secret_keystore_path: str = Field(
        default=".data/private-gateway/local-keystore.json", exclude=True, repr=False
    )
    local_secret_deployment_id: str | None = None
    local_secret_admin_handoff_secret: bytes | None = Field(
        default=None, exclude=True, repr=False
    )
    local_management_allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )
    local_management_allowed_client_cidrs: list[str] = Field(default_factory=list)
    local_management_allow_loopback: bool = True
    edge_backup_root: str = Field(
        default=".data/private-gateway/edge-backups", exclude=True, repr=False
    )
    edge_backup_customer_object_store_root: str | None = Field(
        default=None, exclude=True, repr=False
    )
    # 导出配置带安全默认值，兼容测试直接构造 AppConfig，同时禁止无界任务和长期 BLOB。
    usage_export_enabled: bool = True
    usage_export_poll_interval_seconds: int = 2
    usage_export_sync_row_limit: int = 5000
    usage_export_max_rows: int = 100000
    usage_export_max_bytes: int = 52428800
    usage_export_lease_seconds: int = 60
    usage_export_max_attempts: int = 3
    usage_export_retention_hours: int = 24
    # 查看事件 worker 只领取 0044 source outbox；独立开关避免与成本异常扫描耦合。
    request_explanation_event_enabled: bool = True
    request_explanation_event_poll_interval_seconds: int = 2
    # 查询事件 worker 只领取 0045 source outbox；配置不能由租户请求覆盖，避免绕过审计投递。
    request_log_query_event_enabled: bool = True
    request_log_query_event_poll_interval_seconds: int = 2
    # 助手事件 worker 只领取 0070 AssistantAudit outbox；部署开关不能由租户请求覆盖。
    admin_assistant_event_enabled: bool = True
    admin_assistant_event_poll_interval_seconds: int = 2
    # 检测器只读取正式账本；开关与门限均为部署级配置，不能由租户请求动态覆盖。
    cost_anomaly_enabled: bool = False
    cost_anomaly_poll_interval_seconds: int = 30
    cost_anomaly_spike_multiplier_millis: int = 5000
    cost_anomaly_min_cost_micros: int = 10_000
    cost_anomaly_workday_start_hour: int = 9
    cost_anomaly_workday_end_hour: int = 18
    # 该门限覆盖整个策略集，API 不能按请求放宽；默认值兼容直接构造 AppConfig 的旧测试。
    policy_evaluation_timeout_ms: int = 5
    # 快照同步配置全部有界；Token 继续复用 exclude=True 的 internal_config_token，不能复制到
    # 新的可序列化字段或配置摘要值中。
    policy_snapshot_sync_enabled: bool = False
    policy_snapshot_poll_interval_seconds: int = 2
    policy_snapshot_http_timeout_seconds: int = 2
    policy_snapshot_max_age_seconds: int = 300
    policy_snapshot_emergency_max_age_seconds: int = 3600
    policy_snapshot_max_payload_bytes: int = 4_194_304
    # 部署级门禁不能由租户请求或数据库模板修改；默认 false 保证 mixed-version 升级期间失败关闭。
    policy_mask_runtime_enabled: bool = False
    # 错误映射缓存独立于策略缓存；小数 timeout 支持精确 deadline，所有字段均有旧构造兼容默认。
    provider_error_map_sync_enabled: bool = False
    provider_error_map_poll_interval_seconds: int = 2
    provider_error_map_http_timeout_seconds: float = 2.0
    provider_error_map_refresh_queue_timeout_seconds: float = 2.0
    provider_error_map_fresh_seconds: float = 300.0
    provider_error_map_lkg_seconds: float = 3600.0
    provider_error_map_max_payload_bytes: int = 262_144
    provider_error_map_refresh_concurrency: int = 4
    # 每个字段都带默认值，既兼容测试中直接构造 AppConfig，也避免旧配置文件升级后缺字段。
    stream_inter_token_timeout_seconds: int = 30
    stream_connect_timeout_seconds: int = 30
    stream_write_timeout_seconds: int = 30
    stream_pool_timeout_seconds: int = 5
    # 流式检查点和全文上限必须有旧构造兼容默认值，同时由 load_app_config 执行严格范围校验。
    output_safety_interval_tokens: int = 32
    output_safety_window_tokens: int = 256
    output_safety_max_full_text_code_points: int = 262_144

    @property
    def response_cache_hmac_key(self) -> VersionedResponseCacheSecret | None:
        """返回 active cache-key HMAC key；缺失时由业务边界固定旁路或 503。"""

        # 缺失 active Secret 时业务分支必须旁路或 503，不能回退其他 HMAC 域。
        if self.response_cache_hmac_secret is None:
            return None
        # DTO 的 Secret 字段已从 repr 隐藏，调用方只在受控密码学栈内解包。
        return VersionedResponseCacheSecret(
            self.response_cache_hmac_version,
            self.response_cache_hmac_secret,
        )

    @property
    def response_cache_encryption_key(self) -> VersionedResponseCacheSecret | None:
        """返回 active entry 加密密钥 DTO，同时继续以 ``repr=False`` 保护根 Secret。"""

        # 缺失加密根密钥时绝不回退 Auth、File 或 Batch 根域，缓存能力保持不可用。
        if self.response_cache_encryption_secret is None:
            return None
        # 版本与 Secret 作为一体传给 HKDF，避免调用方错配配置中的两个独立字段。
        return VersionedResponseCacheSecret(
            self.response_cache_encryption_version,
            self.response_cache_encryption_secret,
        )

    @property
    def response_cache_control_idempotency_keyring(
        self,
    ) -> tuple[VersionedResponseCacheSecret, ...]:
        """返回 active-first Control 幂等验证 keyring，旧版本只用于查询历史 owner。"""

        # 没有 active key 就不能安全生成或查询幂等 owner，空 tuple 明确表示配置不可用。
        if self.response_cache_control_idempotency_secret is None:
            return ()
        keys = [
            VersionedResponseCacheSecret(
                self.response_cache_control_idempotency_version,
                self.response_cache_control_idempotency_secret,
            )
        ]
        # previous 必须在 load 阶段成对校验；这里只按 active-first 顺序暴露只读验证材料。
        if (
            self.response_cache_control_idempotency_previous_version is not None
            and self.response_cache_control_idempotency_previous_secret is not None
        ):
            keys.append(
                VersionedResponseCacheSecret(
                    self.response_cache_control_idempotency_previous_version,
                    self.response_cache_control_idempotency_previous_secret,
                )
            )
        return tuple(keys)

    @property
    def response_cache_failure_spool_keyring(
        self,
    ) -> tuple[VersionedResponseCacheSecret, ...]:
        """返回 active-first failure spool 验证 keyring，旧版本仅用于恢复既有记录。"""

        # 缺失 active key 时不能签名 outcome-unknown 记录，也不能猜测使用 retained key。
        if self.response_cache_failure_spool_secret is None:
            return ()
        keys = [
            VersionedResponseCacheSecret(
                self.response_cache_failure_spool_version,
                self.response_cache_failure_spool_secret,
            )
        ]
        # 恢复器按顺序尝试 active 与 retained version，新写入始终只取第一项。
        if (
            self.response_cache_failure_spool_previous_version is not None
            and self.response_cache_failure_spool_previous_secret is not None
        ):
            keys.append(
                VersionedResponseCacheSecret(
                    self.response_cache_failure_spool_previous_version,
                    self.response_cache_failure_spool_previous_secret,
                )
            )
        return tuple(keys)

    @property
    def mcp_encryption_keyring(self) -> tuple[McpVersionedSecret, ...]:
        """Return active-first MCP encryption keys; an empty tuple means fail closed."""

        if self.mcp_encryption_secret is None:
            return ()
        keys = [McpVersionedSecret(self.mcp_encryption_version, self.mcp_encryption_secret)]
        if (
            self.mcp_encryption_previous_version is not None
            and self.mcp_encryption_previous_secret is not None
        ):
            keys.append(
                McpVersionedSecret(
                    self.mcp_encryption_previous_version,
                    self.mcp_encryption_previous_secret,
                )
            )
        return tuple(keys)

    @property
    def mcp_hmac_keyring(self) -> tuple[McpVersionedSecret, ...]:
        """Return active-first MCP HMAC keys; retained keys never sign new facts."""

        if self.mcp_hmac_secret is None:
            return ()
        keys = [McpVersionedSecret(self.mcp_hmac_version, self.mcp_hmac_secret)]
        if self.mcp_hmac_previous_version is not None and self.mcp_hmac_previous_secret is not None:
            keys.append(
                McpVersionedSecret(
                    self.mcp_hmac_previous_version,
                    self.mcp_hmac_previous_secret,
                )
            )
        return tuple(keys)

    @property
    def mcp_schema_limits(self) -> McpSchemaLimits:
        """Project deployment-owned AppConfig budgets into the Shared Schema validator."""

        return McpSchemaLimits(
            max_schema_bytes=self.mcp_schema_max_bytes,
            max_instance_bytes=self.mcp_arguments_max_bytes,
            max_result_bytes=self.mcp_result_max_bytes,
            max_depth=self.mcp_tree_max_depth,
            max_nodes=self.mcp_tree_max_nodes,
            max_properties=self.mcp_schema_max_properties,
            max_string_length=self.mcp_max_string_length,
            max_array_items=self.mcp_max_array_items,
            max_keywords=self.mcp_schema_max_keywords,
            max_combinator_branches=self.mcp_schema_max_combinator_branches,
            max_enum_items=self.mcp_schema_max_enum_items,
            max_enum_item_bytes=self.mcp_schema_max_enum_item_bytes,
            max_pattern_length=self.mcp_schema_max_pattern_length,
            max_evaluation_steps=self.mcp_validator_max_steps,
        )

    def mcp_network_policy_caps(self) -> McpNetworkPolicyCaps:
        """Project the immutable PRD-083 caps shared with Scheduler and Sandbox."""

        return McpNetworkPolicyCaps(
            connect_timeout_max_seconds=self.mcp_network_connect_timeout_max_seconds,
            probe_timeout_max_seconds=self.mcp_network_probe_timeout_max_seconds,
            call_timeout_max_seconds=self.mcp_network_call_timeout_max_seconds,
            failure_threshold_max=self.mcp_network_failure_threshold_max,
            open_seconds_max=self.mcp_network_open_seconds_max,
        )


class ServiceInfo(BaseModel):
    """服务版本信息。

    它属于进程级元数据，不是业务数据，所以 PRD-002 不要求 tenant_id。后续业务表
    从 PRD-003 开始默认带 tenant_id。
    """

    service_name: str
    version: str
    build_commit: str
    build_time: str
    started_at: datetime
    uptime_seconds: float
    request_id: str
    trace_id: str


def load_app_config(service_name: str) -> AppConfig:
    """加载服务配置。

    PRD-002 的优先级是：内置默认值 < .env < METAHUB_CONFIG_FILE 指向的配置文件 < 环境变量。
    M0 只在进程启动时加载一次，修改配置后必须重启服务，避免引入动态配置中心的复杂度。
    """

    values = _merge_config_values()
    _validate_mcp_config_keys(values)
    environment = _read_environment(values)
    service_mode = _read_service_mode(values)
    loaded_at = datetime.now(UTC)
    provider_base_urls = _read_provider_base_urls(values)
    provider_api_keys = _read_provider_api_keys(values)
    auth_token_secret = _value(values, "METAHUB_AUTH_TOKEN_SECRET")
    identity_session_secret = _value(values, "METAHUB_IDENTITY_SESSION_SECRET")
    identity_challenge_secret = _value(values, "METAHUB_IDENTITY_CHALLENGE_SECRET")
    enterprise_sso_hmac_secret = _value(values, "METAHUB_ENTERPRISE_SSO_HMAC_SECRET")
    enterprise_sso_encryption_secret = _value(
        values, "METAHUB_ENTERPRISE_SSO_ENCRYPTION_SECRET"
    )
    workspace_session_secret = _value(values, "METAHUB_WORKSPACE_SESSION_SECRET")
    platform_session_secret = _value(values, "METAHUB_PLATFORM_SESSION_SECRET")
    support_session_secret = _value(values, "METAHUB_SUPPORT_SESSION_SECRET")
    identity_mfa_encryption_secret = _value(
        values, "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET"
    )
    platform_bootstrap_recovery_secret = _value(
        values, "METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET"
    )
    identity_pii_encryption_secret = _value(
        values, "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET"
    )
    identity_challenge_delivery_mode = (
        _value(values, "METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE").strip().lower()
    )
    cache_affinity_hmac_secret = _value(values, "METAHUB_CACHE_AFFINITY_HMAC_SECRET")
    gateway_placement_enabled = _boolean(values, "METAHUB_GATEWAY_PLACEMENT_ENABLED")
    gateway_cell_code = _optional_value(values, "METAHUB_GATEWAY_CELL_CODE")
    gateway_placement_signing_version = _positive_int(
        values, "METAHUB_GATEWAY_PLACEMENT_SIGNING_VERSION"
    )
    configured_gateway_placement_secret = _raw_optional_value(
        values, "METAHUB_GATEWAY_PLACEMENT_SIGNING_SECRET"
    )
    gateway_placement_signing_secret = configured_gateway_placement_secret or (
        DEFAULT_GATEWAY_PLACEMENT_SIGNING_SECRET
        if environment in {"dev", "test"}
        else None
    )
    if gateway_placement_enabled and (
        gateway_placement_signing_secret is None
        or len(gateway_placement_signing_secret.encode("utf-8")) < 32
        or _secret_reused(
            gateway_placement_signing_secret,
            (
                auth_token_secret,
                identity_session_secret,
                identity_challenge_secret,
                workspace_session_secret,
                platform_session_secret,
                support_session_secret,
                identity_mfa_encryption_secret,
                platform_bootstrap_recovery_secret,
                identity_pii_encryption_secret,
                cache_affinity_hmac_secret,
            ),
        )
        or (
            environment == "prod"
            and hmac.compare_digest(
                gateway_placement_signing_secret,
                DEFAULT_GATEWAY_PLACEMENT_SIGNING_SECRET,
            )
        )
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="Gateway placement signing requires an independent production secret.",
            suggestion=(
                "Configure METAHUB_GATEWAY_PLACEMENT_SIGNING_SECRET with at least 32 characters."
            ),
        )

    if gateway_placement_enabled and service_name == "gateway-data-plane" and (
        gateway_cell_code is None
        or re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{1,38}[a-z0-9])?", gateway_cell_code
        )
        is None
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="Gateway placement requires an explicit local Cell code.",
            suggestion="Configure METAHUB_GATEWAY_CELL_CODE with the assigned active Cell code.",
        )

    business_verification_key_version = _positive_int(
        values, "METAHUB_BUSINESS_VERIFICATION_KEY_VERSION"
    )
    configured_business_verification_secret = _raw_optional_value(
        values, "METAHUB_BUSINESS_VERIFICATION_ENCRYPTION_SECRET"
    )
    business_verification_encryption_secret = configured_business_verification_secret or (
        DEFAULT_BUSINESS_VERIFICATION_ENCRYPTION_SECRET
        if environment in {"dev", "test"}
        else None
    )
    if business_verification_encryption_secret is not None:
        try:
            validate_business_verification_secret(
                business_verification_encryption_secret,
                production=environment == "prod",
                forbidden_secrets=(
                    auth_token_secret,
                    identity_session_secret,
                    identity_challenge_secret,
                    workspace_session_secret,
                    platform_session_secret,
                    support_session_secret,
                    identity_mfa_encryption_secret,
                    platform_bootstrap_recovery_secret,
                    identity_pii_encryption_secret,
                    cache_affinity_hmac_secret,
                    gateway_placement_signing_secret,
                ),
            )
        # 企业认证根密钥错误只映射部署合同，不暴露与哪个既有密钥重复。
        except BusinessVerificationCryptoError:
            raise ConfigError(
                code="infra.config_invalid",
                message="Business verification encryption requires an independent secret.",
                suggestion=(
                    "Configure METAHUB_BUSINESS_VERIFICATION_ENCRYPTION_SECRET with an "
                    "independent 32-512 byte value."
                ),
            ) from None

    payment_webhook_key_version = _positive_int(values, "METAHUB_PAYMENT_WEBHOOK_KEY_VERSION")
    payment_webhook_previous_key_version = _optional_positive_int(
        values, "METAHUB_PAYMENT_WEBHOOK_PREVIOUS_KEY_VERSION"
    )
    configured_payment_webhook_secret = _raw_optional_value(
        values, "METAHUB_PAYMENT_WEBHOOK_SECRET"
    )
    payment_webhook_previous_secret = _raw_optional_value(
        values, "METAHUB_PAYMENT_WEBHOOK_PREVIOUS_SECRET"
    )
    configured_payment_receipt_secret = _raw_optional_value(
        values, "METAHUB_PAYMENT_RECEIPT_ENCRYPTION_SECRET"
    )
    payment_webhook_secret = configured_payment_webhook_secret or (
        DEFAULT_PAYMENT_WEBHOOK_SECRET if environment in {"dev", "test"} else None
    )
    payment_receipt_encryption_secret = configured_payment_receipt_secret or (
        DEFAULT_PAYMENT_RECEIPT_ENCRYPTION_SECRET
        if environment in {"dev", "test"}
        else None
    )
    if (payment_webhook_previous_key_version is None) != (
        payment_webhook_previous_secret is None
    ) or payment_webhook_previous_key_version == payment_webhook_key_version:
        raise ConfigError(
            code="infra.config_invalid",
            message="Payment webhook previous key configuration is incomplete.",
            suggestion="Configure both previous key fields with a version different from current.",
        )
    payment_forbidden = (
        auth_token_secret,
        identity_session_secret,
        identity_challenge_secret,
        workspace_session_secret,
        platform_session_secret,
        support_session_secret,
        identity_mfa_encryption_secret,
        platform_bootstrap_recovery_secret,
        identity_pii_encryption_secret,
        cache_affinity_hmac_secret,
        gateway_placement_signing_secret,
        business_verification_encryption_secret,
    )
    try:
        if payment_webhook_secret is not None:
            validate_payment_secret(
                payment_webhook_secret,
                production=environment == "prod",
                forbidden_secrets=payment_forbidden,
            )
        if payment_receipt_encryption_secret is not None:
            validate_payment_secret(
                payment_receipt_encryption_secret,
                production=environment == "prod",
                forbidden_secrets=payment_forbidden + (payment_webhook_secret,),
            )
        if payment_webhook_previous_secret is not None:
            validate_payment_secret(
                payment_webhook_previous_secret,
                production=environment == "prod",
                forbidden_secrets=payment_forbidden
                + (payment_webhook_secret, payment_receipt_encryption_secret),
            )
    # 支付密钥错误只报告固定部署合同，不能透露发生复用的具体密钥域。
    except PaymentCryptoError:
        raise ConfigError(
            code="infra.config_invalid",
            message="Payment webhook and receipt encryption require independent secrets.",
            suggestion="Configure distinct payment secrets with 32-512 UTF-8 bytes.",
        ) from None

    if (
        len(identity_session_secret) < 32
        or hmac.compare_digest(identity_session_secret, auth_token_secret)
        or hmac.compare_digest(identity_session_secret, cache_affinity_hmac_secret)
        or (environment == "prod" and identity_session_secret == DEFAULT_IDENTITY_SESSION_SECRET)
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_IDENTITY_SESSION_SECRET is not an independent production secret.",
            suggestion="Configure a unique identity session secret of at least 32 characters.",
        )

    if (
        len(identity_challenge_secret) < 32
        or hmac.compare_digest(identity_challenge_secret, auth_token_secret)
        or hmac.compare_digest(identity_challenge_secret, identity_session_secret)
        or hmac.compare_digest(identity_challenge_secret, cache_affinity_hmac_secret)
        or (
            service_name == "control-plane-api"
            and environment == "prod"
            and identity_challenge_secret == DEFAULT_IDENTITY_CHALLENGE_SECRET
        )
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_IDENTITY_CHALLENGE_SECRET is not an independent production secret.",
            suggestion="Configure a unique identity challenge secret of at least 32 characters.",
        )
    if identity_challenge_delivery_mode not in {"disabled", "test_capture"} or (
        service_name == "control-plane-api"
        and environment == "prod"
        and identity_challenge_delivery_mode == "test_capture"
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE is invalid for this environment.",
            suggestion="Use disabled until a production delivery adapter is configured.",
        )

    enterprise_sso_keys = {
        "METAHUB_ENTERPRISE_SSO_HMAC_SECRET",
        "METAHUB_ENTERPRISE_SSO_ENCRYPTION_SECRET",
    }
    enterprise_sso_forbidden_secrets = tuple(
        item.value
        for key, item in values.items()
        if key not in enterprise_sso_keys and is_sensitive_key(key)
    )
    invalid_enterprise_sso_secret = (
        not 32 <= len(enterprise_sso_hmac_secret.encode("utf-8")) <= 512
        or not 32 <= len(enterprise_sso_encryption_secret.encode("utf-8")) <= 512
        or _secret_reused(enterprise_sso_hmac_secret, enterprise_sso_forbidden_secrets)
        or _secret_reused(enterprise_sso_encryption_secret, enterprise_sso_forbidden_secrets)
        or hmac.compare_digest(
            enterprise_sso_hmac_secret, enterprise_sso_encryption_secret
        )
    )
    invalid_enterprise_sso_secret = invalid_enterprise_sso_secret or (
        service_name == "control-plane-api"
        and environment == "prod"
        and (
            hmac.compare_digest(
                enterprise_sso_hmac_secret, DEFAULT_ENTERPRISE_SSO_HMAC_SECRET
            )
            or hmac.compare_digest(
                enterprise_sso_encryption_secret,
                DEFAULT_ENTERPRISE_SSO_ENCRYPTION_SECRET,
            )
        )
    )
    identity_pii_forbidden_secrets = (
        auth_token_secret,
        identity_session_secret,
        identity_challenge_secret,
        workspace_session_secret,
        cache_affinity_hmac_secret,
        _raw_optional_value(values, "METAHUB_BATCH_ENCRYPTION_SECRET"),
        _raw_optional_value(values, "METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET"),
        _raw_optional_value(values, "METAHUB_FILE_ENCRYPTION_SECRET"),
        _raw_optional_value(values, "METAHUB_TENANT_DATA_ENCRYPTION_SECRET"),
        _raw_optional_value(values, "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET"),
        _raw_optional_value(values, "METAHUB_RESPONSE_CACHE_HMAC_SECRET"),
        _raw_optional_value(values, "METAHUB_RESPONSE_CACHE_ENCRYPTION_SECRET"),
        _raw_optional_value(values, "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_SECRET"),
        _raw_optional_value(values, "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_SECRET"),
        _raw_optional_value(values, "METAHUB_MCP_ENCRYPTION_SECRET"),
        _raw_optional_value(values, "METAHUB_MCP_HMAC_SECRET"),
        _raw_optional_value(values, "METAHUB_MCP_SANDBOX_ENVELOPE_SECRET"),
        _raw_optional_value(values, "METAHUB_SESSION_RISK_HMAC_SECRET"),
        _raw_optional_value(values, "METAHUB_APPROVAL_HMAC_SECRET"),
        *provider_api_keys.values(),
    )
    if (
        not _valid_identity_pii_secret(identity_pii_encryption_secret)
        or _secret_reused(identity_pii_encryption_secret, identity_pii_forbidden_secrets)
        or (
            service_name == "control-plane-api"
            and environment == "prod"
            and identity_pii_encryption_secret == DEFAULT_IDENTITY_PII_ENCRYPTION_SECRET
        )
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message=(
                "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET is not an independent production "
                "secret."
            ),
            suggestion="Configure a unique Identity PII secret of at least 32 characters.",
        )

    if (
        len(workspace_session_secret) < 32
        or hmac.compare_digest(workspace_session_secret, auth_token_secret)
        or hmac.compare_digest(workspace_session_secret, identity_session_secret)
        or hmac.compare_digest(workspace_session_secret, identity_challenge_secret)
        or hmac.compare_digest(workspace_session_secret, identity_pii_encryption_secret)
        or hmac.compare_digest(workspace_session_secret, cache_affinity_hmac_secret)
        or (
            service_name == "control-plane-api"
            and environment == "prod"
            and workspace_session_secret == DEFAULT_WORKSPACE_SESSION_SECRET
        )
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_WORKSPACE_SESSION_SECRET is not an independent production secret.",
            suggestion="Configure a unique Workspace session secret of at least 32 characters.",
        )

    platform_secret_domains = (
        platform_session_secret,
        support_session_secret,
        identity_mfa_encryption_secret,
        platform_bootstrap_recovery_secret,
    )
    existing_identity_domains = (
        auth_token_secret,
        identity_session_secret,
        identity_challenge_secret,
        identity_pii_encryption_secret,
        workspace_session_secret,
        cache_affinity_hmac_secret,
        _raw_optional_value(values, "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET"),
    )
    platform_defaults = (
        DEFAULT_PLATFORM_SESSION_SECRET,
        DEFAULT_SUPPORT_SESSION_SECRET,
        DEFAULT_IDENTITY_MFA_ENCRYPTION_SECRET,
        DEFAULT_PLATFORM_BOOTSTRAP_RECOVERY_SECRET,
    )
    invalid_platform_domain = any(
        len(value.encode("utf-8")) < 32 for value in platform_secret_domains
    ) or any(_secret_reused(value, existing_identity_domains) for value in platform_secret_domains)
    invalid_platform_domain = invalid_platform_domain or any(
        hmac.compare_digest(left, right)
        for index, left in enumerate(platform_secret_domains)
        for right in platform_secret_domains[index + 1 :]
    )
    if invalid_platform_domain or (
        service_name == "control-plane-api"
        and environment == "prod"
        and any(
            hmac.compare_digest(value, default)
            for value, default in zip(platform_secret_domains, platform_defaults, strict=True)
        )
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="Platform IAM secrets are not independent production secrets.",
            suggestion="Configure four unique Platform IAM secrets of at least 32 characters.",
        )

    if invalid_enterprise_sso_secret:
        raise ConfigError(
            code="infra.config_invalid",
            message="Enterprise SSO requires two independent secret domains.",
            suggestion=(
                "Configure distinct METAHUB_ENTERPRISE_SSO_HMAC_SECRET and "
                "METAHUB_ENTERPRISE_SSO_ENCRYPTION_SECRET values."
            ),
        )

    environment_info = EnvironmentInfo(
        name=environment,
        is_production=environment == "prod",
        config_profile=_config_profile(values),
        service_mode=service_mode,
        loaded_at=loaded_at,
    )
    warnings = _build_warnings(environment, provider_base_urls, values)
    config_items = _build_config_items(values)
    usage_export_sync_row_limit = _bounded_int(
        values, "METAHUB_USAGE_EXPORT_SYNC_ROW_LIMIT", minimum=1, maximum=100000
    )
    usage_export_max_rows = _bounded_int(
        values, "METAHUB_USAGE_EXPORT_MAX_ROWS", minimum=1, maximum=1000000
    )
    # 同步阈值大于硬上限会让异步分支永远不可达，启动时必须拒绝这种含糊配置。
    if usage_export_sync_row_limit > usage_export_max_rows:
        raise ConfigError(
            code="infra.config_invalid",
            message=(
                "METAHUB_USAGE_EXPORT_SYNC_ROW_LIMIT cannot exceed METAHUB_USAGE_EXPORT_MAX_ROWS."
            ),
            suggestion="Lower the sync row limit or increase the bounded max rows value.",
        )

    policy_snapshot_sync_enabled = _boolean(values, "METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED")
    policy_snapshot_poll_interval_seconds = _bounded_int(
        values,
        "METAHUB_POLICY_SNAPSHOT_POLL_INTERVAL_SECONDS",
        minimum=1,
        maximum=60,
    )
    policy_snapshot_http_timeout_seconds = _bounded_int(
        values,
        "METAHUB_POLICY_SNAPSHOT_HTTP_TIMEOUT_SECONDS",
        minimum=1,
        maximum=10,
    )
    policy_snapshot_max_age_seconds = _bounded_int(
        values,
        "METAHUB_POLICY_SNAPSHOT_MAX_AGE_SECONDS",
        minimum=5,
        maximum=86_400,
    )
    policy_snapshot_emergency_max_age_seconds = _bounded_int(
        values,
        "METAHUB_POLICY_SNAPSHOT_EMERGENCY_MAX_AGE_SECONDS",
        minimum=60,
        maximum=604_800,
    )
    policy_snapshot_max_payload_bytes = _bounded_int(
        values,
        "METAHUB_POLICY_SNAPSHOT_MAX_PAYLOAD_BYTES",
        minimum=1_024,
        maximum=16_777_216,
    )
    policy_mask_runtime_enabled = _boolean(values, "METAHUB_POLICY_MASK_RUNTIME_ENABLED")
    provider_error_map_sync_enabled = _boolean(values, "METAHUB_PROVIDER_ERROR_MAP_SYNC_ENABLED")
    provider_error_map_poll_interval_seconds = _bounded_int(
        values,
        "METAHUB_PROVIDER_ERROR_MAP_POLL_INTERVAL_SECONDS",
        minimum=1,
        maximum=300,
    )
    provider_error_map_http_timeout_seconds = _bounded_float(
        values,
        "METAHUB_PROVIDER_ERROR_MAP_HTTP_TIMEOUT_SECONDS",
        minimum=0.1,
        maximum=10.0,
    )
    provider_error_map_refresh_queue_timeout_seconds = _bounded_float(
        values,
        "METAHUB_PROVIDER_ERROR_MAP_REFRESH_QUEUE_TIMEOUT_SECONDS",
        minimum=0.1,
        maximum=10.0,
    )
    provider_error_map_fresh_seconds = _bounded_float(
        values,
        "METAHUB_PROVIDER_ERROR_MAP_FRESH_SECONDS",
        minimum=1.0,
        maximum=86_400.0,
    )
    provider_error_map_lkg_seconds = _bounded_float(
        values,
        "METAHUB_PROVIDER_ERROR_MAP_LKG_SECONDS",
        minimum=1.0,
        maximum=604_800.0,
    )
    provider_error_map_max_payload_bytes = _bounded_int(
        values,
        "METAHUB_PROVIDER_ERROR_MAP_MAX_PAYLOAD_BYTES",
        minimum=4_096,
        maximum=1_048_576,
    )
    provider_error_map_refresh_concurrency = _bounded_int(
        values,
        "METAHUB_PROVIDER_ERROR_MAP_REFRESH_CONCURRENCY",
        minimum=1,
        maximum=32,
    )
    internal_config_token = _optional_value(values, "METAHUB_INTERNAL_CONFIG_TOKEN")
    control_api_base_url = _value(values, "METAHUB_CONTROL_API_BASE_URL")
    gateway_api_base_url = _value(values, "METAHUB_GATEWAY_API_BASE_URL")
    configured_internal_chat_token = _optional_value(values, "METAHUB_INTERNAL_CHAT_TOKEN")
    configured_internal_batch_token = _raw_optional_value(values, "METAHUB_INTERNAL_BATCH_TOKEN")
    configured_batch_encryption_secret = _raw_optional_value(
        values, "METAHUB_BATCH_ENCRYPTION_SECRET"
    )
    prompt_template_encryption_secret = _optional_value(
        values, "METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET"
    )
    configured_file_encryption_secret = _raw_optional_value(
        values, "METAHUB_FILE_ENCRYPTION_SECRET"
    )
    # 本地默认值只保证开箱验收；生产缺失保持 None，由文件 API 固定返回 503。
    file_encryption_secret = configured_file_encryption_secret or (
        DEFAULT_FILE_ENCRYPTION_SECRET if environment in {"dev", "test"} else None
    )
    tenant_data_encryption_version = _positive_int(values, "METAHUB_TENANT_DATA_ENCRYPTION_VERSION")
    configured_tenant_data_encryption_secret = _raw_optional_value(
        values, "METAHUB_TENANT_DATA_ENCRYPTION_SECRET"
    )
    tenant_data_encryption_secret = configured_tenant_data_encryption_secret or (
        DEFAULT_TENANT_DATA_ENCRYPTION_SECRET if environment in {"dev", "test"} else None
    )
    edge_snapshot_signing_version = _positive_int(
        values, "METAHUB_EDGE_SNAPSHOT_SIGNING_VERSION"
    )
    configured_edge_snapshot_signing_secret = _raw_optional_value(
        values, "METAHUB_EDGE_SNAPSHOT_SIGNING_SECRET"
    )
    edge_snapshot_signing_secret = configured_edge_snapshot_signing_secret or (
        DEFAULT_EDGE_SNAPSHOT_SIGNING_SECRET if environment in {"dev", "test"} else None
    )
    response_cache_enabled = _boolean(values, "METAHUB_RESPONSE_CACHE_ENABLED")
    response_cache_hmac_version = _positive_int(values, "METAHUB_RESPONSE_CACHE_HMAC_VERSION")
    response_cache_encryption_version = _positive_int(
        values, "METAHUB_RESPONSE_CACHE_ENCRYPTION_VERSION"
    )
    response_cache_control_idempotency_version = _positive_int(
        values, "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_VERSION"
    )
    response_cache_control_idempotency_previous_version = _optional_positive_int(
        values, "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_PREVIOUS_VERSION"
    )
    response_cache_failure_spool_version = _positive_int(
        values, "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_VERSION"
    )
    response_cache_failure_spool_previous_version = _optional_positive_int(
        values, "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_PREVIOUS_VERSION"
    )
    configured_response_cache_hmac_secret = _raw_optional_value(
        values, "METAHUB_RESPONSE_CACHE_HMAC_SECRET"
    )
    configured_response_cache_encryption_secret = _raw_optional_value(
        values, "METAHUB_RESPONSE_CACHE_ENCRYPTION_SECRET"
    )
    configured_response_cache_control_idempotency_secret = _raw_optional_value(
        values, "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_SECRET"
    )
    configured_response_cache_failure_spool_secret = _raw_optional_value(
        values, "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_SECRET"
    )
    # 四个公开占位值只服务 dev/test 的真实迁移和运行验收；prod 无论功能开关状态
    # 都不自动生成密钥，避免不同副本静默形成不可恢复的独立缓存域。
    response_cache_hmac_secret = configured_response_cache_hmac_secret or (
        DEFAULT_RESPONSE_CACHE_HMAC_SECRET if environment in {"dev", "test"} else None
    )
    response_cache_encryption_secret = configured_response_cache_encryption_secret or (
        DEFAULT_RESPONSE_CACHE_ENCRYPTION_SECRET if environment in {"dev", "test"} else None
    )
    response_cache_control_idempotency_secret = (
        configured_response_cache_control_idempotency_secret
        or (
            DEFAULT_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_SECRET
            if environment in {"dev", "test"}
            else None
        )
    )
    response_cache_failure_spool_secret = configured_response_cache_failure_spool_secret or (
        DEFAULT_RESPONSE_CACHE_FAILURE_SPOOL_SECRET if environment in {"dev", "test"} else None
    )
    # previous Secret 不得注入开发默认值；它只在明确轮换窗口中验证已有 owner/spool。
    response_cache_control_idempotency_previous_secret = _raw_optional_value(
        values, "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_PREVIOUS_SECRET"
    )
    response_cache_failure_spool_previous_secret = _raw_optional_value(
        values, "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_PREVIOUS_SECRET"
    )
    response_cache_max_ready_entries_per_tenant = _bounded_int(
        values,
        "METAHUB_RESPONSE_CACHE_MAX_READY_ENTRIES_PER_TENANT",
        minimum=1,
        maximum=1_000_000,
    )
    response_cache_max_plaintext_bytes = _bounded_int(
        values,
        "METAHUB_RESPONSE_CACHE_MAX_PLAINTEXT_BYTES",
        minimum=1_024,
        maximum=1_048_576,
    )
    response_cache_fill_lease_seconds = _bounded_int(
        values,
        "METAHUB_RESPONSE_CACHE_FILL_LEASE_SECONDS",
        minimum=10,
        maximum=3_600,
    )
    response_cache_failure_spool_poll_interval_seconds = _bounded_int(
        values,
        "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_POLL_INTERVAL_SECONDS",
        minimum=1,
        maximum=60,
    )
    response_cache_failure_spool_claim_lease_seconds = _bounded_int(
        values,
        "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_CLAIM_LEASE_SECONDS",
        minimum=10,
        maximum=3_600,
    )
    session_risk_enabled = _boolean(values, "METAHUB_SESSION_RISK_ENABLED")
    session_risk_hmac_version = _positive_int(values, "METAHUB_SESSION_RISK_HMAC_VERSION")
    # secret 必须保留原始空白供强度门禁拒绝，不能先 ``strip`` 后把错误配置变成另一把 key。
    session_risk_hmac_secret = _raw_optional_value(values, "METAHUB_SESSION_RISK_HMAC_SECRET")
    session_risk_previous_hmac_version = _optional_positive_int(
        values, "METAHUB_SESSION_RISK_PREVIOUS_HMAC_VERSION"
    )
    session_risk_previous_hmac_secret = _raw_optional_value(
        values, "METAHUB_SESSION_RISK_PREVIOUS_HMAC_SECRET"
    )
    approval_hmac_version = _positive_int(values, "METAHUB_APPROVAL_HMAC_VERSION")
    configured_approval_hmac_secret = _raw_optional_value(values, "METAHUB_APPROVAL_HMAC_SECRET")
    approval_hmac_secret = configured_approval_hmac_secret or (
        DEFAULT_APPROVAL_HMAC_SECRET if environment in {"dev", "test"} else None
    )
    approval_previous_hmac_version = _optional_positive_int(
        values, "METAHUB_APPROVAL_PREVIOUS_HMAC_VERSION"
    )
    approval_previous_hmac_secret = _raw_optional_value(
        values, "METAHUB_APPROVAL_PREVIOUS_HMAC_SECRET"
    )
    approval_scheduler_enabled = _boolean(values, "METAHUB_APPROVAL_SCHEDULER_ENABLED")
    approval_scheduler_poll_interval_seconds = _bounded_float(
        values,
        "METAHUB_APPROVAL_SCHEDULER_POLL_INTERVAL_SECONDS",
        minimum=0.1,
        maximum=60.0,
    )
    approval_scheduler_batch_size = _bounded_int(
        values, "METAHUB_APPROVAL_SCHEDULER_BATCH_SIZE", minimum=1, maximum=100
    )
    approval_outbox_enabled = _boolean(values, "METAHUB_APPROVAL_OUTBOX_ENABLED")
    approval_outbox_poll_interval_seconds = _bounded_float(
        values,
        "METAHUB_APPROVAL_OUTBOX_POLL_INTERVAL_SECONDS",
        minimum=0.1,
        maximum=60.0,
    )
    approval_outbox_batch_size = _bounded_int(
        values, "METAHUB_APPROVAL_OUTBOX_BATCH_SIZE", minimum=1, maximum=100
    )
    approval_writeback_enabled = _boolean(values, "METAHUB_APPROVAL_WRITEBACK_ENABLED")
    approval_writeback_poll_interval_seconds = _bounded_float(
        values,
        "METAHUB_APPROVAL_WRITEBACK_POLL_INTERVAL_SECONDS",
        minimum=0.1,
        maximum=60.0,
    )
    approval_writeback_batch_size = _bounded_int(
        values, "METAHUB_APPROVAL_WRITEBACK_BATCH_SIZE", minimum=1, maximum=100
    )
    # 开发默认值只在非生产环境存在；生产缺失保持 None，由内部 Chat 路由按固定 503
    # 失败关闭，不影响未启用该功能的既有控制面和数据面能力启动。
    internal_chat_token = configured_internal_chat_token or (
        DEFAULT_INTERNAL_CHAT_TOKEN if environment in {"dev", "test"} else None
    )
    internal_batch_token = configured_internal_batch_token or (
        DEFAULT_INTERNAL_BATCH_TOKEN if environment in {"dev", "test"} else None
    )
    batch_encryption_secret = configured_batch_encryption_secret or (
        DEFAULT_BATCH_ENCRYPTION_SECRET if environment in {"dev", "test"} else None
    )
    batch_worker_enabled = _boolean(values, "METAHUB_BATCH_WORKER_ENABLED")
    batch_poll_interval_seconds = _bounded_float(
        values, "METAHUB_BATCH_POLL_INTERVAL_SECONDS", minimum=0.1, maximum=60.0
    )
    batch_global_concurrency = _bounded_int(
        values, "METAHUB_BATCH_GLOBAL_CONCURRENCY", minimum=1, maximum=32
    )
    batch_tenant_concurrency = _bounded_int(
        values, "METAHUB_BATCH_TENANT_CONCURRENCY", minimum=1, maximum=8
    )
    batch_lease_seconds = _bounded_int(
        values, "METAHUB_BATCH_LEASE_SECONDS", minimum=30, maximum=3600
    )
    batch_heartbeat_seconds = _bounded_int(
        values, "METAHUB_BATCH_HEARTBEAT_SECONDS", minimum=5, maximum=300
    )
    batch_gateway_concurrency = _bounded_int(
        values, "METAHUB_BATCH_GATEWAY_CONCURRENCY", minimum=1, maximum=1
    )
    batch_realtime_cooldown_milliseconds = _bounded_nonnegative_int(
        values,
        "METAHUB_BATCH_REALTIME_COOLDOWN_MILLISECONDS",
        maximum=5000,
    )
    batch_realtime_reserve_percent = _bounded_int(
        values, "METAHUB_BATCH_REALTIME_RESERVE_PERCENT", minimum=1, maximum=90
    )
    batch_max_pending_jobs_per_tenant = _bounded_int(
        values,
        "METAHUB_BATCH_MAX_PENDING_JOBS_PER_TENANT",
        minimum=1,
        maximum=10_000,
    )
    batch_max_pending_tasks_per_tenant = _bounded_int(
        values,
        "METAHUB_BATCH_MAX_PENDING_TASKS_PER_TENANT",
        minimum=1,
        maximum=1_000_000,
    )
    batch_max_result_bytes = _bounded_int(
        values,
        "METAHUB_BATCH_MAX_RESULT_BYTES",
        minimum=1024,
        maximum=1_048_576,
    )
    experiment_scheduler_interval_seconds = _bounded_int(
        values,
        "METAHUB_EXPERIMENT_SCHEDULER_INTERVAL_SECONDS",
        minimum=1,
        maximum=60,
    )
    experiment_finalizer_lease_seconds = _bounded_int(
        values,
        "METAHUB_EXPERIMENT_FINALIZER_LEASE_SECONDS",
        minimum=30,
        maximum=300,
    )
    experiment_execution_lease_seconds = _bounded_int(
        values,
        "METAHUB_EXPERIMENT_EXECUTION_LEASE_SECONDS",
        minimum=30,
        maximum=300,
    )
    experiment_execution_heartbeat_seconds = _bounded_int(
        values,
        "METAHUB_EXPERIMENT_EXECUTION_HEARTBEAT_SECONDS",
        minimum=5,
        maximum=60,
    )
    # heartbeat 必须严格小于 lease/3，确保抖动后仍至少保留两次续租机会。
    if 3 * experiment_execution_heartbeat_seconds >= experiment_execution_lease_seconds:
        raise ConfigError(
            code="infra.config_invalid",
            message=(
                "METAHUB_EXPERIMENT_EXECUTION_HEARTBEAT_SECONDS must be lower "
                "than one third of METAHUB_EXPERIMENT_EXECUTION_LEASE_SECONDS."
            ),
            suggestion="Lower the heartbeat interval or increase the execution lease.",
        )
    experiment_scheduler_batch_limit = _bounded_int(
        values,
        "METAHUB_EXPERIMENT_SCHEDULER_BATCH_LIMIT",
        minimum=1,
        maximum=100,
    )
    experiment_execution_max_seconds = _bounded_int(
        values,
        "METAHUB_EXPERIMENT_EXECUTION_MAX_SECONDS",
        minimum=60,
        maximum=86_400,
    )
    experiment_settlement_grace_seconds = _bounded_int(
        values,
        "METAHUB_EXPERIMENT_SETTLEMENT_GRACE_SECONDS",
        minimum=60,
        maximum=86_400,
    )
    _validate_policy_snapshot_config(
        service_name=service_name,
        environment=environment,
        service_mode=service_mode,
        sync_enabled=policy_snapshot_sync_enabled,
        poll_interval_seconds=policy_snapshot_poll_interval_seconds,
        max_age_seconds=policy_snapshot_max_age_seconds,
        emergency_max_age_seconds=policy_snapshot_emergency_max_age_seconds,
        control_api_base_url=control_api_base_url,
        internal_config_token=internal_config_token,
    )
    _validate_provider_error_map_config(
        service_name=service_name,
        sync_enabled=provider_error_map_sync_enabled,
        fresh_seconds=provider_error_map_fresh_seconds,
        lkg_seconds=provider_error_map_lkg_seconds,
        control_api_base_url=control_api_base_url,
        internal_config_token=internal_config_token,
        environment=environment,
    )
    _validate_internal_chat_config(
        service_name=service_name,
        environment=environment,
        gateway_api_base_url=gateway_api_base_url,
        internal_chat_token=internal_chat_token,
    )
    _validate_prompt_template_encryption_secret(
        environment=environment,
        encryption_secret=prompt_template_encryption_secret,
        auth_token_secret=auth_token_secret,
    )
    _validate_file_encryption_secret(
        environment=environment,
        encryption_secret=file_encryption_secret,
        auth_token_secret=auth_token_secret,
        cache_affinity_hmac_secret=cache_affinity_hmac_secret,
        prompt_template_encryption_secret=prompt_template_encryption_secret,
        provider_key_encryption_secret=_raw_optional_value(
            values, "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET"
        ),
    )
    _validate_tenant_data_encryption_secret(
        environment=environment,
        encryption_secret=tenant_data_encryption_secret,
        forbidden_secrets=(
            auth_token_secret,
            identity_challenge_secret,
            identity_pii_encryption_secret,
            cache_affinity_hmac_secret,
            internal_config_token,
            internal_chat_token,
            internal_batch_token,
            batch_encryption_secret,
            prompt_template_encryption_secret,
            file_encryption_secret,
            edge_snapshot_signing_secret,
            _raw_optional_value(values, "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET"),
            session_risk_hmac_secret,
            session_risk_previous_hmac_secret,
            response_cache_hmac_secret,
            response_cache_encryption_secret,
            response_cache_control_idempotency_secret,
            response_cache_failure_spool_secret,
            *provider_api_keys.values(),
        ),
    )
    _validate_edge_snapshot_signing_secret(
        environment=environment,
        signing_secret=edge_snapshot_signing_secret,
        forbidden_secrets=(
            auth_token_secret,
            identity_session_secret,
            identity_challenge_secret,
            workspace_session_secret,
            platform_session_secret,
            support_session_secret,
            identity_mfa_encryption_secret,
            platform_bootstrap_recovery_secret,
            identity_pii_encryption_secret,
            cache_affinity_hmac_secret,
            gateway_placement_signing_secret,
            tenant_data_encryption_secret,
            prompt_template_encryption_secret,
            file_encryption_secret,
            _raw_optional_value(values, "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET"),
        ),
    )
    _validate_session_risk_hmac_config(
        enabled=session_risk_enabled,
        current_version=session_risk_hmac_version,
        current_secret=session_risk_hmac_secret,
        previous_version=session_risk_previous_hmac_version,
        previous_secret=session_risk_previous_hmac_secret,
        auth_token_secret=auth_token_secret,
        cache_affinity_hmac_secret=cache_affinity_hmac_secret,
    )
    _validate_approval_hmac_config(
        environment=environment,
        current_version=approval_hmac_version,
        current_secret=approval_hmac_secret,
        previous_version=approval_previous_hmac_version,
        previous_secret=approval_previous_hmac_secret,
        forbidden_secrets=(
            auth_token_secret,
            identity_challenge_secret,
            identity_pii_encryption_secret,
            cache_affinity_hmac_secret,
            internal_config_token,
            internal_chat_token,
            internal_batch_token,
            batch_encryption_secret,
            prompt_template_encryption_secret,
            file_encryption_secret,
            _raw_optional_value(values, "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET"),
            session_risk_hmac_secret,
            session_risk_previous_hmac_secret,
            response_cache_hmac_secret,
            response_cache_encryption_secret,
            response_cache_control_idempotency_secret,
            response_cache_control_idempotency_previous_secret,
            response_cache_failure_spool_secret,
            response_cache_failure_spool_previous_secret,
            *provider_api_keys.values(),
        ),
    )
    _validate_batch_config(
        service_name=service_name,
        environment=environment,
        enabled=batch_worker_enabled,
        database_url=_optional_value(values, "METAHUB_DATABASE_URL"),
        gateway_api_base_url=gateway_api_base_url,
        internal_batch_token=internal_batch_token,
        batch_encryption_secret=batch_encryption_secret,
        internal_config_token=internal_config_token,
        internal_chat_token=internal_chat_token,
        auth_token_secret=auth_token_secret,
        cache_affinity_hmac_secret=cache_affinity_hmac_secret,
        prompt_template_encryption_secret=prompt_template_encryption_secret,
        file_encryption_secret=file_encryption_secret,
        provider_key_encryption_secret=_raw_optional_value(
            values, "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET"
        ),
        session_risk_hmac_secret=session_risk_hmac_secret,
        session_risk_previous_hmac_secret=session_risk_previous_hmac_secret,
        global_concurrency=batch_global_concurrency,
        tenant_concurrency=batch_tenant_concurrency,
        lease_seconds=batch_lease_seconds,
        heartbeat_seconds=batch_heartbeat_seconds,
    )
    _validate_response_cache_config(
        environment=environment,
        enabled=response_cache_enabled,
        hmac_version=response_cache_hmac_version,
        hmac_secret=response_cache_hmac_secret,
        encryption_version=response_cache_encryption_version,
        encryption_secret=response_cache_encryption_secret,
        control_version=response_cache_control_idempotency_version,
        control_secret=response_cache_control_idempotency_secret,
        control_previous_version=(response_cache_control_idempotency_previous_version),
        control_previous_secret=(response_cache_control_idempotency_previous_secret),
        failure_spool_version=response_cache_failure_spool_version,
        failure_spool_secret=response_cache_failure_spool_secret,
        failure_spool_previous_version=(response_cache_failure_spool_previous_version),
        failure_spool_previous_secret=(response_cache_failure_spool_previous_secret),
        forbidden_secrets=(
            auth_token_secret,
            cache_affinity_hmac_secret,
            internal_config_token,
            internal_chat_token,
            internal_batch_token,
            batch_encryption_secret,
            prompt_template_encryption_secret,
            file_encryption_secret,
            _raw_optional_value(values, "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET"),
            session_risk_hmac_secret,
            session_risk_previous_hmac_secret,
            *provider_api_keys.values(),
        ),
    )
    # 先保留既有快照配置错误优先级，再校验新增 Prompt HMAC 门禁，便于运维逐项修复启动配置。
    _validate_cache_affinity_hmac_secret(
        service_name=service_name,
        environment=environment,
        values=values,
        cache_affinity_hmac_secret=cache_affinity_hmac_secret,
        auth_token_secret=auth_token_secret,
    )

    request_explanation_event_enabled = _boolean(
        values, "METAHUB_REQUEST_EXPLANATION_EVENT_ENABLED"
    )
    request_explanation_event_poll_interval_seconds = _bounded_int(
        values,
        "METAHUB_REQUEST_EXPLANATION_EVENT_POLL_INTERVAL_SECONDS",
        minimum=1,
        maximum=60,
    )
    request_log_query_event_enabled = _boolean(values, "METAHUB_REQUEST_LOG_QUERY_EVENT_ENABLED")
    request_log_query_event_poll_interval_seconds = _bounded_int(
        values,
        "METAHUB_REQUEST_LOG_QUERY_EVENT_POLL_INTERVAL_SECONDS",
        minimum=1,
        maximum=60,
    )
    admin_assistant_event_enabled = _boolean(values, "METAHUB_ADMIN_ASSISTANT_EVENT_ENABLED")
    admin_assistant_event_poll_interval_seconds = _bounded_int(
        values,
        "METAHUB_ADMIN_ASSISTANT_EVENT_POLL_INTERVAL_SECONDS",
        minimum=1,
        maximum=60,
    )

    cost_anomaly_enabled = _boolean(values, "METAHUB_COST_ANOMALY_ENABLED")
    cost_anomaly_poll_interval_seconds = _bounded_int(
        values,
        "METAHUB_COST_ANOMALY_POLL_INTERVAL_SECONDS",
        minimum=5,
        maximum=300,
    )
    cost_anomaly_spike_multiplier_millis = _bounded_int(
        values,
        "METAHUB_COST_ANOMALY_SPIKE_MULTIPLIER_MILLIS",
        minimum=1001,
        maximum=100_000,
    )
    cost_anomaly_min_cost_micros = _bounded_int(
        values,
        "METAHUB_COST_ANOMALY_MIN_COST_MICROS",
        minimum=1,
        maximum=1_000_000_000_000,
    )
    cost_anomaly_workday_start_hour = _bounded_hour(
        values, "METAHUB_COST_ANOMALY_WORKDAY_START_HOUR"
    )
    cost_anomaly_workday_end_hour = _bounded_hour(values, "METAHUB_COST_ANOMALY_WORKDAY_END_HOUR")
    # 工作时段必须是同一自然日内的严格递增区间，跨午夜配置在 v1 失败关闭。
    if cost_anomaly_workday_start_hour >= cost_anomaly_workday_end_hour:
        # v1 的工作时段不能跨午夜；非工作时段规则依赖单一左闭右开日内区间。
        raise ConfigError(
            code="infra.config_invalid",
            message=(
                "METAHUB_COST_ANOMALY_WORKDAY_START_HOUR must be lower than "
                "METAHUB_COST_ANOMALY_WORKDAY_END_HOUR."
            ),
            suggestion="Configure a bounded daytime interval such as 9 through 18.",
        )

    output_safety_interval_tokens = _bounded_int(
        values, "METAHUB_OUTPUT_SAFETY_INTERVAL_TOKENS", minimum=8, maximum=256
    )
    output_safety_window_tokens = _bounded_int(
        values, "METAHUB_OUTPUT_SAFETY_WINDOW_TOKENS", minimum=32, maximum=1024
    )
    # 滑窗小于检查间隔会在相邻检查点之间制造无法复核的缺口，因此启动阶段直接拒绝配置。
    if output_safety_window_tokens < output_safety_interval_tokens:
        raise ConfigError(
            code="infra.config_invalid",
            message=(
                "METAHUB_OUTPUT_SAFETY_WINDOW_TOKENS must be greater than or equal to "
                "METAHUB_OUTPUT_SAFETY_INTERVAL_TOKENS."
            ),
            suggestion="Increase the output safety window or reduce the scan interval.",
        )

    file_security_max_image_pixels = _bounded_int(
        values,
        "METAHUB_FILE_SECURITY_MAX_IMAGE_PIXELS",
        minimum=1,
        maximum=50_000_000,
    )
    file_security_max_total_pixels = _bounded_int(
        values,
        "METAHUB_FILE_SECURITY_MAX_TOTAL_PIXELS",
        minimum=1,
        maximum=200_000_000,
    )
    if file_security_max_total_pixels < file_security_max_image_pixels:
        # 总预算小于单图预算会让同一图片在两个层次得到矛盾结论，启动阶段必须拒绝。
        raise ConfigError(
            code="infra.config_invalid",
            message=(
                "METAHUB_FILE_SECURITY_MAX_TOTAL_PIXELS cannot be lower than "
                "METAHUB_FILE_SECURITY_MAX_IMAGE_PIXELS."
            ),
            suggestion="Increase the total pixel budget or lower the per-image budget.",
        )

    local_endpoint_private_cidrs = _canonical_network_list(
        _value(values, "METAHUB_LOCAL_ENDPOINT_PRIVATE_CIDRS")
    )
    local_endpoint_allowed_ports = _bounded_port_list(
        _value(values, "METAHUB_LOCAL_ENDPOINT_ALLOWED_PORTS")
    )
    local_endpoint_allow_loopback = _boolean(values, "METAHUB_LOCAL_ENDPOINT_ALLOW_LOOPBACK")
    if environment == "prod" and local_endpoint_allow_loopback:
        raise ConfigError(
            code="infra.config_invalid",
            message="Local model loopback endpoints are forbidden in production.",
            suggestion="Disable METAHUB_LOCAL_ENDPOINT_ALLOW_LOOPBACK in production.",
        )

    local_secret_management_enabled = _boolean(
        values, "METAHUB_LOCAL_SECRET_MANAGEMENT_ENABLED"
    )
    local_secret_deployment_id = _optional_value(
        values, "METAHUB_LOCAL_SECRET_DEPLOYMENT_ID"
    )
    local_management_allowed_origins = _local_management_origins(
        _value(values, "METAHUB_LOCAL_MANAGEMENT_ALLOWED_ORIGINS"),
        environment=environment,
        enabled=local_secret_management_enabled,
    )
    local_management_allowed_client_cidrs = _canonical_network_list(
        _value(values, "METAHUB_LOCAL_MANAGEMENT_ALLOWED_CLIENT_CIDRS")
    )
    local_management_allow_loopback = _boolean(
        values, "METAHUB_LOCAL_MANAGEMENT_ALLOW_LOOPBACK"
    )
    local_secret_database_path = _value(values, "METAHUB_LOCAL_SECRET_DATABASE_PATH")
    local_secret_keystore_path = _value(values, "METAHUB_LOCAL_SECRET_KEYSTORE_PATH")
    edge_backup_root = _value(values, "METAHUB_EDGE_BACKUP_ROOT")
    edge_backup_customer_object_store_root = _optional_value(
        values, "METAHUB_EDGE_BACKUP_CUSTOMER_OBJECT_STORE_ROOT"
    )
    edge_support_bundle_root = _value(values, "METAHUB_EDGE_SUPPORT_BUNDLE_ROOT")
    edge_support_bundle_encryption_secret = (
        _optional_value(values, "METAHUB_EDGE_SUPPORT_BUNDLE_ENCRYPTION_SECRET")
        if service_name == "gateway-data-plane"
        else None
    )
    edge_support_bundle_ttl_seconds = _bounded_int(
        values,
        "METAHUB_EDGE_SUPPORT_BUNDLE_TTL_SECONDS",
        minimum=300,
        maximum=604_800,
    )
    local_secret_admin_handoff_secret_value = _raw_optional_value(
        values, "METAHUB_LOCAL_SECRET_ADMIN_HANDOFF_SECRET"
    )
    try:
        local_secret_admin_handoff_secret = (
            None
            if local_secret_admin_handoff_secret_value is None
            else local_secret_admin_handoff_secret_value.encode("ascii")
        )
    except UnicodeEncodeError:
        local_secret_admin_handoff_secret = None
    if local_secret_management_enabled and (
        service_name != "gateway-data-plane"
        or local_secret_deployment_id is None
        or re.fullmatch(r"edp_[0-9a-f]{32}", local_secret_deployment_id) is None
        or local_secret_admin_handoff_secret is None
        or not 32 <= len(local_secret_admin_handoff_secret) <= 512
        or any(
            character < 33 or character > 126
            for character in local_secret_admin_handoff_secret
        )
        or not set(local_management_allowed_origins).issubset(
            _split_origins(_value(values, "METAHUB_ALLOWED_ORIGINS"))
        )
        or (not local_management_allow_loopback and not local_management_allowed_client_cidrs)
        or Path(local_secret_database_path).resolve()
        == Path(local_secret_keystore_path).resolve()
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="Local secret management deployment boundary is invalid.",
            suggestion=(
                "Configure one Edge deployment, distinct local database/key-store paths, "
                "an independent handoff credential, and an allowed local management origin."
            ),
        )

    if service_name == "gateway-data-plane" and (
        edge_support_bundle_encryption_secret is None
        or len(edge_support_bundle_encryption_secret.encode("utf-8")) < 32
        or (
            environment == "prod"
            and edge_support_bundle_encryption_secret
            == DEFAULT_EDGE_SUPPORT_BUNDLE_ENCRYPTION_SECRET
        )
        or Path(edge_support_bundle_root).resolve()
        in {
            Path(local_secret_database_path).resolve(),
            Path(local_secret_keystore_path).resolve(),
        }
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="Edge support bundle encryption or storage boundary is invalid.",
            suggestion=(
                "Configure an independent support-bundle root and a deployment-specific "
                "encryption secret of at least 32 bytes."
            ),
        )

    if service_name == "gateway-data-plane" and (
        not edge_backup_root
        or Path(edge_backup_root).resolve()
        in {
            Path(local_secret_database_path).resolve(),
            Path(local_secret_keystore_path).resolve(),
            Path(edge_support_bundle_root).resolve(),
        }
        or (
            edge_backup_customer_object_store_root is not None
            and Path(edge_backup_customer_object_store_root).resolve()
            in {
                Path(local_secret_database_path).resolve(),
                Path(local_secret_keystore_path).resolve(),
                Path(edge_support_bundle_root).resolve(),
                Path(edge_backup_root).resolve(),
            }
        )
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="Edge backup local and customer storage boundaries are invalid.",
            suggestion=(
                "Configure distinct backup, customer object-store mount, Secret database, "
                "key-store, and support-bundle paths."
            ),
        )

    mcp_values = _load_mcp_config(
        values,
        environment=environment,
        service_name=service_name,
        forbidden_secrets=(
            auth_token_secret,
            identity_challenge_secret,
            identity_pii_encryption_secret,
            cache_affinity_hmac_secret,
            internal_config_token,
            internal_chat_token,
            internal_batch_token,
            batch_encryption_secret,
            prompt_template_encryption_secret,
            file_encryption_secret,
            approval_hmac_secret,
            approval_previous_hmac_secret,
            session_risk_hmac_secret,
            session_risk_previous_hmac_secret,
            response_cache_hmac_secret,
            response_cache_encryption_secret,
            response_cache_control_idempotency_secret,
            response_cache_control_idempotency_previous_secret,
            response_cache_failure_spool_secret,
            response_cache_failure_spool_previous_secret,
            _raw_optional_value(values, "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET"),
            *provider_api_keys.values(),
        ),
    )

    database_pool_min_size = _bounded_int(
        values, "METAHUB_DATABASE_POOL_MIN_SIZE", minimum=0, maximum=32
    )
    database_pool_max_size = _bounded_int(
        values, "METAHUB_DATABASE_POOL_MAX_SIZE", minimum=1, maximum=128
    )
    database_pool_timeout_seconds = _bounded_int(
        values, "METAHUB_DATABASE_POOL_TIMEOUT_SECONDS", minimum=1, maximum=30
    )
    database_statement_timeout_ms = _bounded_int(
        values, "METAHUB_DATABASE_STATEMENT_TIMEOUT_MS", minimum=100, maximum=300000
    )
    database_lock_timeout_ms = _bounded_int(
        values, "METAHUB_DATABASE_LOCK_TIMEOUT_MS", minimum=100, maximum=60000
    )
    infrastructure_probe_timeout_seconds = _bounded_int(
        values, "METAHUB_INFRASTRUCTURE_PROBE_TIMEOUT_SECONDS", minimum=1, maximum=10
    )
    kms_provider = _kms_provider(values)
    backup_worker_enabled = _boolean(values, "METAHUB_BACKUP_WORKER_ENABLED")
    backup_poll_interval_seconds = _bounded_int(
        values, "METAHUB_BACKUP_POLL_INTERVAL_SECONDS", minimum=1, maximum=300
    )
    backup_default_retention_days = _bounded_int(
        values, "METAHUB_BACKUP_DEFAULT_RETENTION_DAYS", minimum=1, maximum=3650
    )
    capacity_runner_mode = _value(values, "METAHUB_CAPACITY_RUNNER_MODE")
    if capacity_runner_mode not in {"disabled", "test_loopback", "isolated_http"}:
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_CAPACITY_RUNNER_MODE is invalid.",
            suggestion="Use disabled, test_loopback, or isolated_http.",
        )
    capacity_runner_base_url = _optional_value(values, "METAHUB_CAPACITY_RUNNER_BASE_URL")
    capacity_runner_internal_token = _raw_optional_value(
        values, "METAHUB_CAPACITY_RUNNER_INTERNAL_TOKEN"
    )
    capacity_runner_permit_secret = _raw_optional_value(
        values, "METAHUB_CAPACITY_RUNNER_PERMIT_SECRET"
    )
    if capacity_runner_mode == "test_loopback" and environment != "test":
        raise ConfigError(
            code="infra.config_invalid",
            message="Capacity runner loopback is restricted to test environments.",
            suggestion="Use isolated_http outside automated tests.",
        )
    if capacity_runner_mode == "isolated_http" and (
        capacity_runner_base_url is None
        or capacity_runner_internal_token is None
        or capacity_runner_permit_secret is None
        or len(capacity_runner_internal_token) < 32
        or len(capacity_runner_permit_secret) < 32
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="The isolated capacity runner configuration is incomplete.",
            suggestion="Configure its internal URL, token, and independent permit secret.",
        )
    if capacity_runner_mode == "isolated_http":
        parsed_runner_url = urlsplit(capacity_runner_base_url or "")
        if (
            parsed_runner_url.scheme not in {"http", "https"}
            or not parsed_runner_url.hostname
            or parsed_runner_url.username
            or parsed_runner_url.password
            or parsed_runner_url.query
            or parsed_runner_url.fragment
            or parsed_runner_url.path not in {"", "/"}
        ):
            raise ConfigError(
                code="infra.config_invalid",
                message="METAHUB_CAPACITY_RUNNER_BASE_URL is invalid.",
                suggestion="Use an HTTP(S) service origin without credentials, path, or query.",
            )
        runner_credentials = (
            capacity_runner_internal_token or "",
            capacity_runner_permit_secret or "",
        )
        try:
            runner_credential_bytes = tuple(value.encode("ascii") for value in runner_credentials)
        except UnicodeEncodeError:
            runner_credential_bytes = ()
        if len(runner_credential_bytes) != 2 or any(
            not 32 <= len(value) <= 512
            or any(character.isspace() or ord(character) < 32 for character in credential)
            for credential, value in zip(runner_credentials, runner_credential_bytes, strict=True)
        ):
            raise ConfigError(
                code="infra.config_invalid",
                message="Capacity runner credentials do not meet security requirements.",
                suggestion="Use independent 32-512 byte printable ASCII values.",
            )
        other_security_domains = tuple(
            value
            for key in (
                "METAHUB_INTERNAL_CONFIG_TOKEN",
                "METAHUB_INTERNAL_CHAT_TOKEN",
                "METAHUB_INTERNAL_BATCH_TOKEN",
                "METAHUB_AUTH_TOKEN_SECRET",
                "METAHUB_PROVIDER_KEY_ENCRYPTION_SECRET",
                "METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET",
                "METAHUB_FILE_ENCRYPTION_SECRET",
                "METAHUB_BATCH_ENCRYPTION_SECRET",
                "METAHUB_MCP_ENCRYPTION_SECRET",
                "METAHUB_MCP_HMAC_SECRET",
                "METAHUB_MCP_SANDBOX_INTERNAL_TOKEN",
                "METAHUB_MCP_SANDBOX_ENVELOPE_SECRET",
                "METAHUB_MCP_SANDBOX_PERMIT_SECRET",
                "METAHUB_MCP_SANDBOX_RESPONSE_SECRET_CURRENT",
                "METAHUB_CACHE_AFFINITY_HMAC_SECRET",
                "METAHUB_GATEWAY_PLACEMENT_SIGNING_SECRET",
                "METAHUB_BUSINESS_VERIFICATION_ENCRYPTION_SECRET",
                "METAHUB_PAYMENT_WEBHOOK_SECRET",
                "METAHUB_PAYMENT_RECEIPT_ENCRYPTION_SECRET",
                "METAHUB_SESSION_RISK_HMAC_SECRET",
                "METAHUB_APPROVAL_HMAC_SECRET",
                "METAHUB_RESPONSE_CACHE_HMAC_SECRET",
                "METAHUB_RESPONSE_CACHE_ENCRYPTION_SECRET",
                "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_SECRET",
                "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_SECRET",
                "METAHUB_IDENTITY_SESSION_SECRET",
                "METAHUB_IDENTITY_CHALLENGE_SECRET",
                "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET",
                "METAHUB_WORKSPACE_SESSION_SECRET",
                "METAHUB_PLATFORM_SESSION_SECRET",
                "METAHUB_SUPPORT_SESSION_SECRET",
                "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET",
                "METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET",
                "METAHUB_TENANT_DATA_ENCRYPTION_SECRET",
                "METAHUB_OBJECT_STORE_SECRET_ACCESS_KEY",
                "METAHUB_KMS_TEST_MASTER_KEY",
            )
            if (value := _raw_optional_value(values, key)) is not None
        )
        if hmac.compare_digest(*runner_credentials) or any(
            hmac.compare_digest(credential, other)
            for credential in runner_credentials
            for other in other_security_domains
        ):
            raise ConfigError(
                code="infra.config_invalid",
                message="Capacity runner credentials must use independent security domains.",
                suggestion="Configure dedicated internal token and permit secret values.",
            )
    _validate_production_infrastructure_config(
        environment=environment,
        database_url=_optional_value(values, "METAHUB_DATABASE_URL"),
        database_pool_min_size=database_pool_min_size,
        database_pool_max_size=database_pool_max_size,
        redis_url=_optional_value(values, "METAHUB_REDIS_URL"),
        object_store_endpoint_url=_optional_value(values, "METAHUB_OBJECT_STORE_ENDPOINT_URL"),
        object_store_region=_optional_value(values, "METAHUB_OBJECT_STORE_REGION"),
        object_store_bucket=_optional_value(values, "METAHUB_OBJECT_STORE_BUCKET"),
        object_store_access_key_id=_optional_value(
            values, "METAHUB_OBJECT_STORE_ACCESS_KEY_ID"
        ),
        object_store_secret_access_key=_optional_value(
            values, "METAHUB_OBJECT_STORE_SECRET_ACCESS_KEY"
        ),
        kms_provider=kms_provider,
        kms_key_id=_optional_value(values, "METAHUB_KMS_KEY_ID"),
        kms_test_master_key=_optional_value(values, "METAHUB_KMS_TEST_MASTER_KEY"),
        backup_worker_enabled=backup_worker_enabled,
        backup_restore_database_url=_optional_value(
            values, "METAHUB_BACKUP_RESTORE_DATABASE_URL"
        ),
    )

    return AppConfig(
        service_name=service_name,
        environment=environment,
        environment_info=environment_info,
        log_level=_value(values, "METAHUB_LOG_LEVEL").upper(),
        public_base_url=_value(values, "METAHUB_PUBLIC_BASE_URL"),
        control_api_base_url=control_api_base_url,
        gateway_api_base_url=gateway_api_base_url,
        allowed_origins=_split_origins(_value(values, "METAHUB_ALLOWED_ORIGINS")),
        workspace_allowed_regions=_workspace_allowed_regions(
            _value(values, "METAHUB_WORKSPACE_ALLOWED_REGIONS")
        ),
        workspace_residency_policy_version=_workspace_residency_policy_version(
            _value(values, "METAHUB_WORKSPACE_RESIDENCY_POLICY_VERSION")
        ),
        gateway_placement_enabled=gateway_placement_enabled,
        gateway_cell_code=gateway_cell_code,
        gateway_placement_signing_version=gateway_placement_signing_version,
        gateway_placement_signing_secret=gateway_placement_signing_secret,
        gateway_placement_snapshot_ttl_seconds=_bounded_int(
            values,
            "METAHUB_GATEWAY_PLACEMENT_SNAPSHOT_TTL_SECONDS",
            minimum=5,
            maximum=300,
        ),
        edge_snapshot_signing_version=edge_snapshot_signing_version,
        edge_snapshot_signing_secret=edge_snapshot_signing_secret,
        gateway_capacity_tenant_limit=_bounded_int(
            values, "METAHUB_GATEWAY_CAPACITY_TENANT_LIMIT", minimum=1, maximum=100000
        ),
        gateway_capacity_workspace_limit=_bounded_int(
            values, "METAHUB_GATEWAY_CAPACITY_WORKSPACE_LIMIT", minimum=1, maximum=100000
        ),
        gateway_capacity_key_limit=_bounded_int(
            values, "METAHUB_GATEWAY_CAPACITY_KEY_LIMIT", minimum=1, maximum=100000
        ),
        gateway_capacity_cell_limit=_bounded_int(
            values, "METAHUB_GATEWAY_CAPACITY_CELL_LIMIT", minimum=1, maximum=1000000
        ),
        business_verification_key_version=business_verification_key_version,
        business_verification_encryption_secret=business_verification_encryption_secret,
        business_verification_download_ttl_seconds=_bounded_int(
            values,
            "METAHUB_BUSINESS_VERIFICATION_DOWNLOAD_TTL_SECONDS",
            minimum=30,
            maximum=300,
        ),
        payment_webhook_key_version=payment_webhook_key_version,
        payment_webhook_secret=payment_webhook_secret,
        payment_webhook_previous_key_version=payment_webhook_previous_key_version,
        payment_webhook_previous_secret=payment_webhook_previous_secret,
        payment_receipt_encryption_key_version=_positive_int(
            values, "METAHUB_PAYMENT_RECEIPT_ENCRYPTION_KEY_VERSION"
        ),
        payment_receipt_encryption_secret=payment_receipt_encryption_secret,
        payment_receipt_storage_root=_validated_file_storage_root(
            _value(values, "METAHUB_PAYMENT_RECEIPT_STORAGE_ROOT")
        ),
        payment_webhook_tolerance_seconds=_bounded_int(
            values, "METAHUB_PAYMENT_WEBHOOK_TOLERANCE_SECONDS", minimum=30, maximum=900
        ),
        payment_webhook_max_body_bytes=_bounded_int(
            values, "METAHUB_PAYMENT_WEBHOOK_MAX_BODY_BYTES", minimum=1024, maximum=1048576
        ),
        payment_worker_poll_interval_seconds=_bounded_int(
            values, "METAHUB_PAYMENT_WORKER_POLL_INTERVAL_SECONDS", minimum=1, maximum=60
        ),
        service_mode=service_mode,
        provider_base_urls=provider_base_urls,
        features={
            "business_api_enabled": False,
            "database_required": bool(_value(values, "METAHUB_DATABASE_URL")),
            "provider_calls_enabled": bool(provider_base_urls),
            "policy_snapshot_sync_enabled": policy_snapshot_sync_enabled,
            "policy_mask_runtime_enabled": policy_mask_runtime_enabled,
            "provider_error_map_sync_enabled": provider_error_map_sync_enabled,
            "cost_anomaly_enabled": cost_anomaly_enabled,
            "request_explanation_event_enabled": request_explanation_event_enabled,
            "request_log_query_event_enabled": request_log_query_event_enabled,
            "admin_assistant_event_enabled": admin_assistant_event_enabled,
            "session_risk_enabled": session_risk_enabled,
            "file_processing_enabled": file_encryption_secret is not None,
            "batch_worker_enabled": batch_worker_enabled,
            "response_cache_enabled": response_cache_enabled,
            "mcp_enabled": bool(mcp_values["mcp_enabled"]),
            "gateway_placement_enabled": gateway_placement_enabled,
            "local_secret_management_enabled": local_secret_management_enabled,
            "payment_commerce_enabled": (
                payment_webhook_secret is not None
                and payment_receipt_encryption_secret is not None
            ),
        },
        config_items=config_items,
        warnings=warnings,
        database_url=_optional_value(values, "METAHUB_DATABASE_URL"),
        redis_url=_optional_value(values, "METAHUB_REDIS_URL"),
        database_pool_min_size=database_pool_min_size,
        database_pool_max_size=database_pool_max_size,
        database_pool_timeout_seconds=database_pool_timeout_seconds,
        database_statement_timeout_ms=database_statement_timeout_ms,
        database_lock_timeout_ms=database_lock_timeout_ms,
        infrastructure_probe_timeout_seconds=infrastructure_probe_timeout_seconds,
        object_store_endpoint_url=_optional_value(
            values, "METAHUB_OBJECT_STORE_ENDPOINT_URL"
        ),
        object_store_region=_optional_value(values, "METAHUB_OBJECT_STORE_REGION"),
        object_store_bucket=_optional_value(values, "METAHUB_OBJECT_STORE_BUCKET"),
        object_store_access_key_id=_optional_value(
            values, "METAHUB_OBJECT_STORE_ACCESS_KEY_ID"
        ),
        object_store_secret_access_key=_optional_value(
            values, "METAHUB_OBJECT_STORE_SECRET_ACCESS_KEY"
        ),
        object_store_force_path_style=_boolean(
            values, "METAHUB_OBJECT_STORE_FORCE_PATH_STYLE"
        ),
        kms_provider=kms_provider,
        kms_key_id=_optional_value(values, "METAHUB_KMS_KEY_ID"),
        kms_test_master_key=_optional_value(values, "METAHUB_KMS_TEST_MASTER_KEY"),
        config_distribution_signing_key_id=_optional_value(
            values, "METAHUB_CONFIG_DISTRIBUTION_SIGNING_KEY_ID"
        ),
        status_bundle_root=_value(values, "METAHUB_STATUS_BUNDLE_ROOT"),
        status_bundle_ttl_seconds=_bounded_int(
            values, "METAHUB_STATUS_BUNDLE_TTL_SECONDS", minimum=300, maximum=604800
        ),
        edge_support_bundle_root=edge_support_bundle_root,
        edge_support_bundle_encryption_secret=edge_support_bundle_encryption_secret,
        edge_support_bundle_ttl_seconds=edge_support_bundle_ttl_seconds,
        backup_worker_enabled=backup_worker_enabled,
        backup_poll_interval_seconds=backup_poll_interval_seconds,
        backup_default_retention_days=backup_default_retention_days,
        backup_restore_database_url=_optional_value(
            values, "METAHUB_BACKUP_RESTORE_DATABASE_URL"
        ),
        capacity_runner_mode=capacity_runner_mode,
        capacity_runner_base_url=capacity_runner_base_url,
        capacity_runner_internal_token=capacity_runner_internal_token,
        capacity_runner_permit_secret=capacity_runner_permit_secret,
        capacity_runner_http_timeout_seconds=_bounded_int(
            values,
            "METAHUB_CAPACITY_RUNNER_HTTP_TIMEOUT_SECONDS",
            minimum=1,
            maximum=300,
        ),
        provider_api_keys=provider_api_keys,
        internal_config_token=internal_config_token,
        internal_chat_token=internal_chat_token,
        internal_batch_token=internal_batch_token,
        batch_encryption_secret=batch_encryption_secret,
        batch_worker_enabled=batch_worker_enabled,
        control_background_schedulers_enabled=_control_background_schedulers_enabled(
            values, environment
        ),
        batch_poll_interval_seconds=batch_poll_interval_seconds,
        batch_global_concurrency=batch_global_concurrency,
        batch_tenant_concurrency=batch_tenant_concurrency,
        batch_lease_seconds=batch_lease_seconds,
        batch_heartbeat_seconds=batch_heartbeat_seconds,
        batch_gateway_concurrency=batch_gateway_concurrency,
        batch_realtime_cooldown_milliseconds=batch_realtime_cooldown_milliseconds,
        batch_realtime_reserve_percent=batch_realtime_reserve_percent,
        batch_max_pending_jobs_per_tenant=batch_max_pending_jobs_per_tenant,
        batch_max_pending_tasks_per_tenant=batch_max_pending_tasks_per_tenant,
        batch_max_result_bytes=batch_max_result_bytes,
        experiment_scheduler_interval_seconds=experiment_scheduler_interval_seconds,
        experiment_finalizer_lease_seconds=experiment_finalizer_lease_seconds,
        experiment_execution_lease_seconds=experiment_execution_lease_seconds,
        experiment_execution_heartbeat_seconds=experiment_execution_heartbeat_seconds,
        experiment_scheduler_batch_limit=experiment_scheduler_batch_limit,
        experiment_execution_max_seconds=experiment_execution_max_seconds,
        experiment_settlement_grace_seconds=experiment_settlement_grace_seconds,
        session_risk_enabled=session_risk_enabled,
        session_risk_hmac_version=session_risk_hmac_version,
        session_risk_hmac_secret=session_risk_hmac_secret,
        session_risk_previous_hmac_version=session_risk_previous_hmac_version,
        session_risk_previous_hmac_secret=session_risk_previous_hmac_secret,
        approval_hmac_version=approval_hmac_version,
        approval_hmac_secret=approval_hmac_secret,
        approval_previous_hmac_version=approval_previous_hmac_version,
        approval_previous_hmac_secret=approval_previous_hmac_secret,
        approval_scheduler_enabled=approval_scheduler_enabled,
        approval_scheduler_poll_interval_seconds=(approval_scheduler_poll_interval_seconds),
        approval_scheduler_batch_size=approval_scheduler_batch_size,
        approval_outbox_enabled=approval_outbox_enabled,
        approval_outbox_poll_interval_seconds=approval_outbox_poll_interval_seconds,
        approval_outbox_batch_size=approval_outbox_batch_size,
        approval_writeback_enabled=approval_writeback_enabled,
        approval_writeback_poll_interval_seconds=(approval_writeback_poll_interval_seconds),
        approval_writeback_batch_size=approval_writeback_batch_size,
        prompt_template_encryption_secret=prompt_template_encryption_secret,
        file_encryption_secret=file_encryption_secret,
        tenant_data_encryption_version=tenant_data_encryption_version,
        tenant_data_encryption_secret=tenant_data_encryption_secret,
        tenant_data_scheduler_enabled=_boolean(values, "METAHUB_TENANT_DATA_SCHEDULER_ENABLED"),
        tenant_data_poll_interval_seconds=_bounded_int(
            values,
            "METAHUB_TENANT_DATA_POLL_INTERVAL_SECONDS",
            minimum=1,
            maximum=60,
        ),
        file_storage_root=_validated_file_storage_root(_value(values, "METAHUB_FILE_STORAGE_ROOT")),
        file_max_upload_bytes=_bounded_int(
            values,
            "METAHUB_FILE_MAX_UPLOAD_BYTES",
            minimum=1_048_576,
            maximum=52_428_800,
        ),
        file_parse_concurrency=_bounded_int(
            values,
            "METAHUB_FILE_PARSE_CONCURRENCY",
            minimum=1,
            maximum=8,
        ),
        file_security_scan_concurrency=_bounded_int(
            values,
            "METAHUB_FILE_SECURITY_SCAN_CONCURRENCY",
            minimum=1,
            maximum=4,
        ),
        file_security_ocr_concurrency=_bounded_int(
            values,
            "METAHUB_FILE_SECURITY_OCR_CONCURRENCY",
            minimum=1,
            maximum=2,
        ),
        file_security_max_ocr_pages=_bounded_int(
            values,
            "METAHUB_FILE_SECURITY_MAX_OCR_PAGES",
            minimum=1,
            maximum=100,
        ),
        file_security_max_images=_bounded_int(
            values,
            "METAHUB_FILE_SECURITY_MAX_IMAGES",
            minimum=1,
            maximum=200,
        ),
        file_security_max_image_pixels=file_security_max_image_pixels,
        file_security_max_total_pixels=file_security_max_total_pixels,
        auth_token_secret=auth_token_secret,
        identity_session_secret=identity_session_secret,
        identity_challenge_secret=identity_challenge_secret,
        enterprise_sso_hmac_secret=enterprise_sso_hmac_secret,
        enterprise_sso_encryption_secret=enterprise_sso_encryption_secret,
        workspace_session_secret=workspace_session_secret,
        platform_session_secret=platform_session_secret,
        platform_session_ttl_seconds=_bounded_int(
            values, "METAHUB_PLATFORM_SESSION_TTL_SECONDS", minimum=60, maximum=3600
        ),
        support_session_secret=support_session_secret,
        support_session_ttl_seconds=_bounded_int(
            values, "METAHUB_SUPPORT_SESSION_TTL_SECONDS", minimum=60, maximum=900
        ),
        identity_mfa_encryption_secret=identity_mfa_encryption_secret,
        platform_bootstrap_enabled=_boolean(values, "METAHUB_PLATFORM_BOOTSTRAP_ENABLED"),
        platform_bootstrap_recovery_secret=platform_bootstrap_recovery_secret,
        identity_pii_encryption_secret=identity_pii_encryption_secret,
        identity_challenge_delivery_mode=identity_challenge_delivery_mode,
        cache_affinity_hmac_secret=cache_affinity_hmac_secret,
        response_cache_enabled=response_cache_enabled,
        response_cache_hmac_version=response_cache_hmac_version,
        response_cache_hmac_secret=response_cache_hmac_secret,
        response_cache_encryption_version=response_cache_encryption_version,
        response_cache_encryption_secret=response_cache_encryption_secret,
        response_cache_control_idempotency_version=(response_cache_control_idempotency_version),
        response_cache_control_idempotency_secret=(response_cache_control_idempotency_secret),
        response_cache_control_idempotency_previous_version=(
            response_cache_control_idempotency_previous_version
        ),
        response_cache_control_idempotency_previous_secret=(
            response_cache_control_idempotency_previous_secret
        ),
        response_cache_failure_spool_version=response_cache_failure_spool_version,
        response_cache_failure_spool_secret=response_cache_failure_spool_secret,
        response_cache_failure_spool_previous_version=(
            response_cache_failure_spool_previous_version
        ),
        response_cache_failure_spool_previous_secret=(response_cache_failure_spool_previous_secret),
        response_cache_max_ready_entries_per_tenant=(response_cache_max_ready_entries_per_tenant),
        response_cache_max_plaintext_bytes=response_cache_max_plaintext_bytes,
        response_cache_fill_lease_seconds=response_cache_fill_lease_seconds,
        response_cache_failure_spool_root=(
            _validated_response_cache_failure_spool_root(
                _value(values, "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_ROOT")
            )
        ),
        response_cache_failure_spool_poll_interval_seconds=(
            response_cache_failure_spool_poll_interval_seconds
        ),
        response_cache_failure_spool_claim_lease_seconds=(
            response_cache_failure_spool_claim_lease_seconds
        ),
        finops_usage_spool_root=_validated_local_spool_root(
            _value(values, "METAHUB_FINOPS_USAGE_SPOOL_ROOT")
        ),
        finops_usage_spool_poll_interval_seconds=_bounded_int(
            values,
            "METAHUB_FINOPS_USAGE_SPOOL_POLL_INTERVAL_SECONDS",
            minimum=1,
            maximum=60,
        ),
        finops_usage_spool_claim_lease_seconds=_bounded_int(
            values,
            "METAHUB_FINOPS_USAGE_SPOOL_CLAIM_LEASE_SECONDS",
            minimum=10,
            maximum=3600,
        ),
        session_ttl_seconds=_positive_int(values, "METAHUB_SESSION_TTL_SECONDS"),
        login_max_failures=_positive_int(values, "METAHUB_LOGIN_MAX_FAILURES"),
        login_lock_seconds=_positive_int(values, "METAHUB_LOGIN_LOCK_SECONDS"),
        default_tenant_slug=_value(values, "METAHUB_DEFAULT_TENANT_SLUG").strip() or "default",
        provider_health_enabled=_boolean(values, "METAHUB_PROVIDER_HEALTH_ENABLED"),
        provider_health_interval_seconds=_bounded_int(
            values, "METAHUB_PROVIDER_HEALTH_INTERVAL_SECONDS", minimum=10, maximum=3600
        ),
        provider_health_timeout_seconds=_bounded_int(
            values, "METAHUB_PROVIDER_HEALTH_TIMEOUT_SECONDS", minimum=1, maximum=15
        ),
        provider_health_failure_threshold=_bounded_int(
            values, "METAHUB_PROVIDER_HEALTH_FAILURE_THRESHOLD", minimum=1, maximum=10
        ),
        provider_health_lease_seconds=_bounded_int(
            values, "METAHUB_PROVIDER_HEALTH_LEASE_SECONDS", minimum=5, maximum=120
        ),
        provider_health_stale_seconds=_bounded_int(
            values, "METAHUB_PROVIDER_HEALTH_STALE_SECONDS", minimum=30, maximum=86400
        ),
        provider_health_concurrency=_bounded_int(
            values, "METAHUB_PROVIDER_HEALTH_CONCURRENCY", minimum=1, maximum=4
        ),
        provider_health_private_cidrs=[
            item.strip()
            for item in _value(values, "METAHUB_PROVIDER_HEALTH_PRIVATE_CIDRS").split(",")
            if item.strip()
        ],
        local_endpoint_private_cidrs=local_endpoint_private_cidrs,
        local_endpoint_allowed_ports=local_endpoint_allowed_ports,
        local_endpoint_allow_loopback=local_endpoint_allow_loopback,
        local_endpoint_connect_timeout_seconds=_bounded_int(
            values,
            "METAHUB_LOCAL_ENDPOINT_CONNECT_TIMEOUT_SECONDS",
            minimum=1,
            maximum=30,
        ),
        local_endpoint_overall_timeout_seconds=_bounded_int(
            values,
            "METAHUB_LOCAL_ENDPOINT_OVERALL_TIMEOUT_SECONDS",
            minimum=1,
            maximum=600,
        ),
        local_endpoint_max_response_bytes=_bounded_int(
            values,
            "METAHUB_LOCAL_ENDPOINT_MAX_RESPONSE_BYTES",
            minimum=65_536,
            maximum=67_108_864,
        ),
        local_endpoint_max_stream_bytes=_bounded_int(
            values,
            "METAHUB_LOCAL_ENDPOINT_MAX_STREAM_BYTES",
            minimum=65_536,
            maximum=268_435_456,
        ),
        local_secret_management_enabled=local_secret_management_enabled,
        local_secret_database_path=local_secret_database_path,
        local_secret_keystore_path=local_secret_keystore_path,
        local_secret_deployment_id=local_secret_deployment_id,
        local_secret_admin_handoff_secret=local_secret_admin_handoff_secret,
        local_management_allowed_origins=local_management_allowed_origins,
        local_management_allowed_client_cidrs=local_management_allowed_client_cidrs,
        local_management_allow_loopback=local_management_allow_loopback,
        edge_backup_root=edge_backup_root,
        edge_backup_customer_object_store_root=edge_backup_customer_object_store_root,
        usage_export_enabled=_boolean(values, "METAHUB_USAGE_EXPORT_ENABLED"),
        usage_export_poll_interval_seconds=_bounded_int(
            values, "METAHUB_USAGE_EXPORT_POLL_INTERVAL_SECONDS", minimum=1, maximum=60
        ),
        usage_export_sync_row_limit=usage_export_sync_row_limit,
        usage_export_max_rows=usage_export_max_rows,
        usage_export_max_bytes=_bounded_int(
            # BytesIO、SQLite BLOB 与 HTTP Response 会形成有限副本，
            # MVP 硬上限不得超过设计的 50 MiB。
            values,
            "METAHUB_USAGE_EXPORT_MAX_BYTES",
            minimum=1024,
            maximum=52428800,
        ),
        usage_export_lease_seconds=_bounded_int(
            values, "METAHUB_USAGE_EXPORT_LEASE_SECONDS", minimum=5, maximum=3600
        ),
        usage_export_max_attempts=_bounded_int(
            values, "METAHUB_USAGE_EXPORT_MAX_ATTEMPTS", minimum=1, maximum=3
        ),
        usage_export_retention_hours=_bounded_int(
            values, "METAHUB_USAGE_EXPORT_RETENTION_HOURS", minimum=1, maximum=720
        ),
        request_explanation_event_enabled=request_explanation_event_enabled,
        request_explanation_event_poll_interval_seconds=(
            request_explanation_event_poll_interval_seconds
        ),
        request_log_query_event_enabled=request_log_query_event_enabled,
        request_log_query_event_poll_interval_seconds=(
            request_log_query_event_poll_interval_seconds
        ),
        admin_assistant_event_enabled=admin_assistant_event_enabled,
        admin_assistant_event_poll_interval_seconds=(admin_assistant_event_poll_interval_seconds),
        cost_anomaly_enabled=cost_anomaly_enabled,
        cost_anomaly_poll_interval_seconds=cost_anomaly_poll_interval_seconds,
        cost_anomaly_spike_multiplier_millis=cost_anomaly_spike_multiplier_millis,
        cost_anomaly_min_cost_micros=cost_anomaly_min_cost_micros,
        cost_anomaly_workday_start_hour=cost_anomaly_workday_start_hour,
        cost_anomaly_workday_end_hour=cost_anomaly_workday_end_hour,
        policy_evaluation_timeout_ms=_bounded_int(
            values, "METAHUB_POLICY_EVALUATION_TIMEOUT_MS", minimum=1, maximum=100
        ),
        policy_snapshot_sync_enabled=policy_snapshot_sync_enabled,
        policy_snapshot_poll_interval_seconds=policy_snapshot_poll_interval_seconds,
        policy_snapshot_http_timeout_seconds=policy_snapshot_http_timeout_seconds,
        policy_snapshot_max_age_seconds=policy_snapshot_max_age_seconds,
        policy_snapshot_emergency_max_age_seconds=(policy_snapshot_emergency_max_age_seconds),
        policy_snapshot_max_payload_bytes=policy_snapshot_max_payload_bytes,
        policy_mask_runtime_enabled=policy_mask_runtime_enabled,
        provider_error_map_sync_enabled=provider_error_map_sync_enabled,
        provider_error_map_poll_interval_seconds=(provider_error_map_poll_interval_seconds),
        provider_error_map_http_timeout_seconds=(provider_error_map_http_timeout_seconds),
        provider_error_map_refresh_queue_timeout_seconds=(
            provider_error_map_refresh_queue_timeout_seconds
        ),
        provider_error_map_fresh_seconds=provider_error_map_fresh_seconds,
        provider_error_map_lkg_seconds=provider_error_map_lkg_seconds,
        provider_error_map_max_payload_bytes=provider_error_map_max_payload_bytes,
        provider_error_map_refresh_concurrency=provider_error_map_refresh_concurrency,
        stream_inter_token_timeout_seconds=_bounded_int(
            values, "METAHUB_STREAM_INTER_TOKEN_TIMEOUT_SECONDS", minimum=1, maximum=600
        ),
        stream_connect_timeout_seconds=_bounded_int(
            values, "METAHUB_STREAM_CONNECT_TIMEOUT_SECONDS", minimum=1, maximum=120
        ),
        stream_write_timeout_seconds=_bounded_int(
            values, "METAHUB_STREAM_WRITE_TIMEOUT_SECONDS", minimum=1, maximum=120
        ),
        stream_pool_timeout_seconds=_bounded_int(
            values, "METAHUB_STREAM_POOL_TIMEOUT_SECONDS", minimum=1, maximum=60
        ),
        output_safety_interval_tokens=output_safety_interval_tokens,
        output_safety_window_tokens=output_safety_window_tokens,
        output_safety_max_full_text_code_points=_bounded_int(
            values,
            "METAHUB_OUTPUT_SAFETY_MAX_FULL_TEXT_CODE_POINTS",
            minimum=1,
            maximum=262_144,
        ),
        **mcp_values,
    )


def build_service_info(config: AppConfig, request_id: str, trace_id: str) -> ServiceInfo:
    """生成当前请求可追踪的服务版本信息。"""

    now = datetime.now(UTC)
    values = _merge_config_values()
    return ServiceInfo(
        service_name=config.service_name,
        version=_value(values, "METAHUB_VERSION"),
        build_commit=_value(values, "METAHUB_BUILD_COMMIT"),
        build_time=_value(values, "METAHUB_BUILD_TIME"),
        started_at=STARTED_AT,
        uptime_seconds=(now - STARTED_AT).total_seconds(),
        request_id=request_id,
        trace_id=trace_id,
    )


def build_config_summary(config: AppConfig, request_id: str) -> ConfigSummary:
    """生成对外安全的配置摘要。

    该函数只使用 config_items 中的安全预览，不读取 raw secret 字段，因此可以被控制面
    API 和控制台复用。
    """

    _ensure_no_secret_preview(config.config_items)
    return ConfigSummary(
        service_name=config.service_name,
        environment=config.environment,
        loaded_at=config.environment_info.loaded_at,
        items=config.config_items,
        warnings=config.warnings,
        request_id=request_id,
    )


def config_loaded_log_payload(config: AppConfig) -> dict[str, object]:
    """生成 config.loaded 日志 payload。

    日志中只记录 key/source/status，不记录 value_preview，避免把 URL、内部 token
    或未来新增的敏感配置写入日志。
    """

    return {
        "environment": config.environment,
        "service_mode": config.service_mode,
        "items": [
            {"key": item.key, "source": item.source, "status": item.status}
            for item in config.config_items
        ],
        "warnings": config.warnings,
    }


def _merge_config_values() -> dict[str, ConfigValue]:
    """在 运行配置 内部复用 merge config values 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    values = {key: ConfigValue(value, "default") for key, value in DEFAULT_CONFIG.items()}

    default_env_file = Path.cwd() / ".env"
    if default_env_file.exists():
        _overlay_file(values, default_env_file)

    explicit_file = os.getenv("METAHUB_CONFIG_FILE")
    if explicit_file:
        explicit_path = Path(explicit_file).expanduser()
        if not explicit_path.is_absolute():
            explicit_path = Path.cwd() / explicit_path
        if not explicit_path.exists():
            # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
            raise ConfigError(
                code="infra.config_profile_not_found",
                message=f"Config file does not exist: {explicit_path}",
                suggestion="Check METAHUB_CONFIG_FILE or remove it to use .env/defaults.",
            )
        same_as_default_file = default_env_file.exists() and (
            explicit_path.resolve() == default_env_file.resolve()
        )
        if not same_as_default_file:
            _overlay_file(values, explicit_path)

    # 环境变量永远最高优先级，保证容器、CI 和生产部署可以覆盖文件配置。
    for key, value in os.environ.items():
        if key.startswith("METAHUB_"):
            values[key] = ConfigValue(value, "env")

    return values


def _overlay_file(values: dict[str, ConfigValue], path: Path) -> None:
    """在 运行配置 内部复用 overlay file 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    for key, value in _parse_env_file(path).items():
        values[key] = ConfigValue(value, "file")


def _parse_env_file(path: Path) -> dict[str, str]:
    """解析 运行配置 的 parse env file 数据，把数据库或外部输入转换为内部稳定类型。

    边界：解析失败要么返回空值口径，要么抛统一错误，不能把底层异常透出给 API。
    """

    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
        if key.startswith("METAHUB_"):
            result[key] = value
    return result


def _validated_local_spool_root(value: str) -> str:
    """校验并规范化 FinOps Usage spool 的本地目录。

    参数：配置文件或环境变量中的目录文本。返回：绝对规范路径字符串。
    约束：拒绝空值、控制字符、UNC/网络共享和文件系统根目录；是否存在重解析点、
    是否由当前服务账号持有等运行时事实由 spool 模块在每次读写前重新校验。
    """

    raw = str(value or "").strip()
    # 路径只允许有界本地目录；原值不能进入错误文本，以免暴露宿主目录结构。
    if (
        not raw
        or len(raw) > 1024
        or any(ord(character) < 32 for character in raw)
        or raw.startswith(("\\\\", "//"))
    ):
        # 路径错误只返回统一配置码，不能把原始宿主机目录回显到日志或 API。
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_FINOPS_USAGE_SPOOL_ROOT must be a bounded local path.",
            suggestion="Use a Gateway service-account-owned local data directory.",
        )
    candidate = Path(raw).expanduser()
    absolute = candidate if candidate.is_absolute() else Path.cwd() / candidate
    normalized = Path(os.path.abspath(absolute))
    if normalized == Path(normalized.anchor):
        # 根目录无法形成可审计的专属边界，误配置时也会把无关文件纳入恢复扫描。
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_FINOPS_USAGE_SPOOL_ROOT cannot be a filesystem root.",
            suggestion="Choose a dedicated child directory for the Gateway spool.",
        )
    return str(normalized)


def _validated_response_cache_failure_spool_root(value: str) -> str:
    """规范化只保存固定失败 owner 的 Response Cache 本地恢复目录。

    路径本身可能暴露宿主结构，所以错误只点名配置键；UNC、控制字符和文件系统根会
    扩大恢复扫描边界，必须在 worker 创建目录前失败关闭。
    """

    raw = str(value or "").strip()
    if (
        not raw
        or len(raw) > 1024
        or any(ord(character) < 32 for character in raw)
        or raw.startswith(("\\\\", "//"))
    ):
        # 共享运行时必须保持租户、权限、用量和错误合同兼容，避免敏感信息进入日志。
        raise ConfigError(
            code="infra.config_invalid",
            message=("METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_ROOT must be a bounded local path."),
            suggestion="Use a Gateway service-account-owned local recovery directory.",
        )
    candidate = Path(raw).expanduser()
    absolute = candidate if candidate.is_absolute() else Path.cwd() / candidate
    normalized = Path(os.path.abspath(absolute))
    if normalized == Path(normalized.anchor):
        # 根目录既无法证明 owner 独占，也会让 quarantine/清理误触及无关文件。
        raise ConfigError(
            code="infra.config_invalid",
            message=("METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_ROOT cannot be a filesystem root."),
            suggestion="Choose a dedicated child directory for cache recovery records.",
        )
    return str(normalized)


def _validated_file_storage_root(value: str) -> str:
    """校验并规范化只保存认证密文的本地文件对象根目录。

    参数：配置文件或环境变量中的目录文本。返回：绝对规范路径字符串。约束：拒绝空值、
    控制字符、UNC/网络共享和文件系统根目录；符号链接与 owner 在 Blob Store 每次操作复核。
    """

    raw = str(value or "").strip()
    if (
        not raw
        or len(raw) > 1024
        or any(ord(character) < 32 for character in raw)
        or raw.startswith(("\\\\", "//"))
    ):
        # 路径错误不能回显部署目录，只报告固定配置字段和修复方向。
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_FILE_STORAGE_ROOT must be a bounded local path.",
            suggestion="Use a Control service-account-owned local encrypted data directory.",
        )
    candidate = Path(raw).expanduser()
    absolute = candidate if candidate.is_absolute() else Path.cwd() / candidate
    normalized = Path(os.path.abspath(absolute))
    if normalized == Path(normalized.anchor):
        # 文件系统根无法形成服务账号独占边界，误配置还会扩大对象枚举范围。
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_FILE_STORAGE_ROOT cannot be a filesystem root.",
            suggestion="Choose a dedicated child directory for encrypted file objects.",
        )
    return str(normalized)


def _read_environment(values: dict[str, ConfigValue]) -> EnvironmentName:
    """在 运行配置 内部复用 read environment 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    raw = _value(values, "METAHUB_ENV").strip().lower()
    if raw not in {"dev", "test", "prod"}:
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        # 超出边界会放大探测压力或使故障长期不恢复，因此启动时直接拒绝配置。
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_ENV must be one of dev, test, or prod.",
            suggestion="Set METAHUB_ENV=dev for local development.",
        )
    return raw  # type: ignore[return-value]


def _read_service_mode(values: dict[str, ConfigValue]) -> ServiceMode:
    """在 运行配置 内部复用 read service mode 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    raw = _value(values, "METAHUB_SERVICE_MODE").strip().lower()
    if raw not in {"internal_only", "public_service"}:
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_SERVICE_MODE must be internal_only or public_service.",
            suggestion="Use internal_only for MVP internal enterprise scenarios.",
        )
    return raw  # type: ignore[return-value]


def _read_provider_base_urls(values: dict[str, ConfigValue]) -> dict[str, str]:
    """在 运行配置 内部复用 read provider base urls 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    providers: dict[str, str] = {}
    prefix = "METAHUB_PROVIDER_"
    suffix = "_BASE_URL"
    for key, item in values.items():
        # 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
        if key.startswith(prefix) and key.endswith(suffix) and item.value:
            provider = key[len(prefix) : -len(suffix)].lower()
            if not _looks_like_http_url(item.value):
                # 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
                # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
                raise ConfigError(
                    code="infra.config_invalid",
                    message=f"{key} must start with http:// or https://.",
                    suggestion="Set Provider base URLs to the upstream model endpoint base URL.",
                )
            providers[provider] = item.value
    return providers


def _read_provider_api_keys(values: dict[str, ConfigValue]) -> dict[str, str]:
    """在 运行配置 内部复用 read provider api keys 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    keys: dict[str, str] = {}
    prefix = "METAHUB_PROVIDER_"
    suffixes = ("_API_KEY", "_KEY", "_TOKEN", "_SECRET")
    for key, item in values.items():
        # 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
        if key.startswith(prefix) and item.value:
            for suffix in suffixes:
                # 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
                if key.endswith(suffix):
                    provider = key[len(prefix) : -len(suffix)].lower()
                    keys[provider] = item.value
    return keys


def _validate_response_cache_config(
    *,
    environment: EnvironmentName,
    enabled: bool,
    hmac_version: int,
    hmac_secret: str | None,
    encryption_version: int,
    encryption_secret: str | None,
    control_version: int,
    control_secret: str | None,
    control_previous_version: int | None,
    control_previous_secret: str | None,
    failure_spool_version: int,
    failure_spool_secret: str | None,
    failure_spool_previous_version: int | None,
    failure_spool_previous_secret: str | None,
    forbidden_secrets: tuple[str | None, ...],
) -> None:
    """校验 Response Cache 四个独立根域及两个 retained verification keyring。

    功能关闭允许生产部署不持有这些 Secret；一旦任一 active Secret 存在，就必须四项完整，
    防止后续只切开关时用部分密钥读写。所有错误均使用固定配置描述，不回显 Secret、摘要或
    与哪个业务域发生冲突。
    """

    active = (
        hmac_secret,
        encryption_secret,
        control_secret,
        failure_spool_secret,
    )
    # 功能已启用却缺任一根域时必须在启动期失败关闭，不能等首次请求再回退其他 Secret。
    if enabled and any(secret is None for secret in active):
        # 已启用能力若缺密钥，启动时就失败关闭；运行时不能回退 Auth 或缓存亲和密钥。
        raise ConfigError(
            code="infra.config_missing",
            message="Enabled Response Cache secret configuration is incomplete.",
            suggestion="Configure all four independent Response Cache secrets and restart.",
        )
    # 即使功能关闭，部分 active 配置也会在未来只切开关时造成不一致，必须作为错误拒绝。
    if any(secret is None for secret in active) and any(secret is not None for secret in active):
        # 共享运行时必须保持租户、权限、用量和错误合同兼容，避免敏感信息进入日志。
        raise ConfigError(
            code="infra.config_invalid",
            message="Response Cache active secrets must be configured as one complete set.",
            suggestion="Configure all four independent secrets or clear the partial set.",
        )

    control_previous_complete = (
        control_previous_version is not None and control_previous_secret is not None
    )
    if control_previous_complete != (
        control_previous_version is not None or control_previous_secret is not None
    ):
        # previous version/Secret 缺一会让跨轮换 owner 查询漏读，不能静默忽略半组配置。
        raise ConfigError(
            code="infra.config_invalid",
            message="Response Cache Control previous key configuration is incomplete.",
            suggestion="Configure both previous version and secret, or clear both values.",
        )
    failure_previous_complete = (
        failure_spool_previous_version is not None and failure_spool_previous_secret is not None
    )
    # failure spool retained version/Secret 缺一会导致已落盘记录在轮换后无法验证。
    if failure_previous_complete != (
        failure_spool_previous_version is not None or failure_spool_previous_secret is not None
    ):
        # 共享运行时必须保持租户、权限、用量和错误合同兼容，避免敏感信息进入日志。
        raise ConfigError(
            code="infra.config_invalid",
            message="Response Cache failure spool previous key configuration is incomplete.",
            suggestion="Configure both previous version and secret, or clear both values.",
        )
    if (control_previous_complete or failure_previous_complete) and any(
        secret is None for secret in active
    ):
        # retained key 没有 active 写入 key 时不存在明确轮换方向，必须拒绝含糊 keyring。
        raise ConfigError(
            code="infra.config_invalid",
            message="Response Cache previous keys require a complete active secret set.",
            suggestion="Configure the active key set before retaining previous verification keys.",
        )
    # 生产关闭且完全未配置是允许状态，业务接口据此固定报告配置不可用。
    if all(secret is None for secret in active):
        return

    assert all(secret is not None for secret in active)
    hmac_secret_value = hmac_secret
    encryption_secret_value = encryption_secret
    control_secret_value = control_secret
    failure_spool_secret_value = failure_spool_secret
    assert hmac_secret_value is not None
    assert encryption_secret_value is not None
    assert control_secret_value is not None
    assert failure_spool_secret_value is not None
    control_keyring = [VersionedResponseCacheSecret(control_version, control_secret_value)]
    if control_previous_complete:
        assert control_previous_version is not None
        assert control_previous_secret is not None
        control_keyring.append(
            VersionedResponseCacheSecret(
                control_previous_version,
                control_previous_secret,
            )
        )
    failure_keyring = [
        VersionedResponseCacheSecret(failure_spool_version, failure_spool_secret_value)
    ]
    if failure_previous_complete:
        assert failure_spool_previous_version is not None
        assert failure_spool_previous_secret is not None
        failure_keyring.append(
            VersionedResponseCacheSecret(
                failure_spool_previous_version,
                failure_spool_previous_secret,
            )
        )
    response_secrets = (
        hmac_secret_value,
        encryption_secret_value,
        control_secret_value,
        failure_spool_secret_value,
        control_previous_secret,
        failure_spool_previous_secret,
    )
    for secret in response_secrets:
        # previous 可选为空；非空项都必须执行与 active 相同的强度和编码门禁。
        if secret is None:
            continue
        try:
            encoded = secret.encode("utf-8")
        # 孤立 surrogate 无法跨进程形成同一 key，错误中也不能回显原 Secret。
        except UnicodeEncodeError:
            raise ConfigError(
                code="infra.config_invalid",
                message="Response Cache secret encoding is invalid.",
                suggestion="Configure independent high-entropy UTF-8 secrets.",
            ) from None
        # 部署 Secret 限定 32..512 UTF-8 bytes 且无空白/控制字符，避免 Header/env 解析漂移。
        if not 32 <= len(encoded) <= 512 or any(
            character.isspace() or ord(character) < 32 for character in secret
        ):
            # 共享运行时必须保持租户、权限、用量和错误合同兼容，避免敏感信息进入日志。
            raise ConfigError(
                code="infra.config_invalid",
                message="Response Cache secret does not meet security requirements.",
                suggestion="Use 32-512 byte secrets without whitespace or control characters.",
            )
    try:
        # 单元素 keyring 同样执行 UTF-8、32..512 bytes、空白和控制字符门禁。
        validate_secret_keyring((VersionedResponseCacheSecret(hmac_version, hmac_secret_value),))
        validate_secret_keyring(
            (VersionedResponseCacheSecret(encryption_version, encryption_secret_value),)
        )
        validate_secret_keyring(tuple(control_keyring))
        validate_secret_keyring(tuple(failure_keyring))
    # 纯合同层错误统一转为配置错误，启动日志只显示配置键级修复方向。
    except ResponseCacheContractError:
        raise ConfigError(
            code="infra.config_invalid",
            message="Response Cache key configuration does not meet security requirements.",
            suggestion="Use distinct positive versions and independent 32-512 byte secrets.",
        ) from None

    encoded_response_secrets = [
        secret.encode("utf-8") for secret in response_secrets if secret is not None
    ]
    for index, candidate in enumerate(encoded_response_secrets):
        # active/previous 任意两项都必须物理不同，版本号变化不能掩盖 Secret 复用。
        if any(
            hmac.compare_digest(candidate, other) for other in encoded_response_secrets[index + 1 :]
        ):
            # 共享运行时必须保持租户、权限、用量和错误合同兼容，避免敏感信息进入日志。
            raise ConfigError(
                code="infra.config_invalid",
                message="Response Cache secrets must use independent key domains.",
                suggestion="Replace every reused active or previous Response Cache secret.",
            )
        for forbidden in forbidden_secrets:
            # 缺失的其他能力 Secret 不参与比较，也不能被当作空字符串密钥。
            if forbidden is None:
                continue
            try:
                forbidden_bytes = forbidden.encode("utf-8")
            # 其他域的编码错误由其专用配置门禁处理，此处只负责安全域等值比较。
            except (AttributeError, UnicodeEncodeError):
                # 其他配置域仍由自己的门禁报告非法编码；这里不能回显或改写其值。
                continue
            # 与 Auth/Provider/File/Batch 等任一根域相同都会扩大单点泄露半径。
            if hmac.compare_digest(candidate, forbidden_bytes):
                raise ConfigError(
                    code="infra.config_invalid",
                    message="Response Cache secrets must not reuse another security domain.",
                    suggestion="Configure dedicated Response Cache secrets and restart.",
                )

    if environment == "prod":
        development_values = {
            DEFAULT_RESPONSE_CACHE_HMAC_SECRET,
            DEFAULT_RESPONSE_CACHE_ENCRYPTION_SECRET,
            DEFAULT_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_SECRET,
            DEFAULT_RESPONSE_CACHE_FAILURE_SPOOL_SECRET,
        }
        # 显式复制开发占位到 prod 仍是不安全配置，不能因来源变成 env/file 而放行。
        if any(secret in development_values for secret in response_secrets if secret is not None):
            # 公开仓库占位可用于本地验收，但绝不能因显式复制进 prod 而被视为有效配置。
            raise ConfigError(
                code="infra.config_invalid",
                message="Response Cache development secrets must be replaced in prod.",
                suggestion="Configure independent production secrets and restart all services.",
            )


def _validate_cache_affinity_hmac_secret(
    *,
    service_name: str,
    environment: EnvironmentName,
    values: dict[str, ConfigValue],
    cache_affinity_hmac_secret: str,
    auth_token_secret: str,
) -> None:
    """校验 Gateway Prompt 指纹独立密钥，Control 不承担该运行时密钥门禁。

    参数：服务名、环境、带来源配置、亲和 HMAC Secret 与登录 Token Secret。
    返回：合法时无返回值；生产 Gateway 不满足显式来源、长度和密钥分域要求时抛统一
    ``ConfigError``。错误信息只描述配置键，不回显或计算可关联的 Secret 摘要。
    """

    if service_name != "gateway-data-plane":
        # Control 不接触 Prompt 正文，强制其持有 Gateway 根密钥会扩大 Secret 暴露面。
        return
    try:
        cache_affinity_hmac_secret.encode("utf-8")
    # 孤立 surrogate 无法形成跨进程稳定 HMAC key，启动时只返回固定安全错误。
    except (AttributeError, UnicodeEncodeError):
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_CACHE_AFFINITY_HMAC_SECRET is invalid.",
            suggestion="Configure an independent UTF-8 HMAC secret and restart Gateway.",
        ) from None
    item = values.get("METAHUB_CACHE_AFFINITY_HMAC_SECRET")
    if environment != "prod":
        # 非生产内置值用于本地开箱运行；空覆盖仍要阻断，避免运行时退回公共或空 HMAC key。
        if cache_affinity_hmac_secret:
            return
    elif (
        item is not None
        and item.source != "default"
        and len(cache_affinity_hmac_secret) >= 32
        and cache_affinity_hmac_secret
        not in {
            DEFAULT_CACHE_AFFINITY_HMAC_SECRET,
            CACHE_AFFINITY_EXAMPLE_HMAC_SECRET,
        }
        and not hmac.compare_digest(cache_affinity_hmac_secret, auth_token_secret)
    ):
        # 只有显式、高熵占位之外且与登录密钥分域的生产值才允许 Gateway 接收流量。
        return
    raise ConfigError(
        code="infra.config_invalid",
        message="METAHUB_CACHE_AFFINITY_HMAC_SECRET does not meet Gateway security requirements.",
        suggestion=(
            "Set an explicit secret of at least 32 characters, distinct from authentication "
            "and example values, then restart Gateway."
        ),
    )


def _validate_mcp_config_keys(values: dict[str, ConfigValue]) -> None:
    """Reject undeclared MCP environment switches instead of silently ignoring bypass knobs."""

    allowed = {key for key in DEFAULT_CONFIG if key.startswith("METAHUB_MCP_")}
    if any(key.startswith("METAHUB_MCP_") and key not in allowed for key in values):
        raise ConfigError(
            code="infra.config_invalid",
            message="An unsupported METAHUB_MCP configuration key is present.",
            suggestion="Remove undeclared MCP settings and use the fixed deployment profile.",
        )


def _load_mcp_config(
    values: dict[str, ConfigValue],
    *,
    environment: EnvironmentName,
    service_name: str = "control-plane-api",
    forbidden_secrets: tuple[object, ...],
) -> dict[str, Any]:
    """Load the fixed PRD-082/083 Control or Scheduler deployment surface."""

    # Direct Shared contract callers from PRD-082 can provide the then-current MCP key set.
    # AppConfig always supplies the complete merged map, while this compatibility fill only adds
    # declared defaults and never accepts an unknown bypass key.
    values = {
        **{
            key: ConfigValue(default, "default")
            for key, default in DEFAULT_CONFIG.items()
            if key.startswith("METAHUB_MCP_") and key not in values
        },
        **values,
    }

    enabled = _boolean(values, "METAHUB_MCP_ENABLED")
    encryption_version = _positive_int(values, "METAHUB_MCP_ENCRYPTION_VERSION")
    hmac_version = _positive_int(values, "METAHUB_MCP_HMAC_VERSION")
    encryption_previous_version = _optional_positive_int(
        values, "METAHUB_MCP_ENCRYPTION_PREVIOUS_VERSION"
    )
    hmac_previous_version = _optional_positive_int(values, "METAHUB_MCP_HMAC_PREVIOUS_VERSION")
    configured_encryption_secret = _raw_optional_value(values, "METAHUB_MCP_ENCRYPTION_SECRET")
    configured_hmac_secret = _raw_optional_value(values, "METAHUB_MCP_HMAC_SECRET")
    encryption_secret = configured_encryption_secret or (
        DEFAULT_MCP_ENCRYPTION_SECRET if environment in {"dev", "test"} else None
    )
    hmac_secret = configured_hmac_secret or (
        DEFAULT_MCP_HMAC_SECRET if environment in {"dev", "test"} else None
    )
    encryption_previous_secret = _raw_optional_value(
        values, "METAHUB_MCP_ENCRYPTION_PREVIOUS_SECRET"
    )
    hmac_previous_secret = _raw_optional_value(values, "METAHUB_MCP_HMAC_PREVIOUS_SECRET")
    if (encryption_previous_version is None) != (encryption_previous_secret is None) or (
        hmac_previous_version is None
    ) != (hmac_previous_secret is None):
        raise ConfigError(
            code="infra.config_invalid",
            message="MCP previous key versions and secrets must be configured in complete pairs.",
            suggestion="Configure both retained fields for each domain, or clear both.",
        )
    if (
        encryption_previous_version is not None
        and encryption_previous_version >= encryption_version
    ) or (hmac_previous_version is not None and hmac_previous_version >= hmac_version):
        raise ConfigError(
            code="infra.config_invalid",
            message="MCP previous key versions must be lower than their current versions.",
            suggestion="Advance the current version and retain only an older verification key.",
        )
    if (
        (encryption_secret is None) != (hmac_secret is None)
        or enabled
        and (encryption_secret is None or hmac_secret is None)
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="Enabled MCP requires independent encryption and HMAC roots.",
            suggestion="Configure both current MCP secrets before enabling the feature.",
        )
    if (encryption_previous_secret is not None and encryption_secret is None) or (
        hmac_previous_secret is not None and hmac_secret is None
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="MCP retained keys require complete current key domains.",
            suggestion="Configure both current roots before retaining an older key.",
        )

    raw_executor_mode = _value(values, "METAHUB_MCP_EXECUTOR_MODE").strip()
    if raw_executor_mode not in {"disabled", "test_loopback", "sandbox_http"}:
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_MCP_EXECUTOR_MODE is invalid.",
            suggestion="Choose disabled, test_loopback, or the isolated sandbox_http adapter.",
        )
    executor_mode: McpExecutorMode = raw_executor_mode  # type: ignore[assignment]
    test_loopback_enabled = _boolean(values, "METAHUB_MCP_TEST_LOOPBACK_ENABLED")
    if (executor_mode == "test_loopback") != test_loopback_enabled:
        raise ConfigError(
            code="infra.config_invalid",
            message="MCP test loopback mode and its explicit enable flag must agree.",
            suggestion="Enable both only in dev/test, or keep both disabled.",
        )
    if environment == "prod" and (executor_mode == "test_loopback" or test_loopback_enabled):
        raise ConfigError(
            code="infra.config_invalid",
            message="Production MCP executor cannot use the test loopback adapter.",
            suggestion="Use disabled for Control or sandbox_http for the dedicated Scheduler.",
        )
    if executor_mode == "test_loopback" and not enabled:
        raise ConfigError(
            code="infra.config_invalid",
            message="MCP test loopback executor requires the feature to be enabled.",
            suggestion="Enable MCP only in an isolated dev/test profile, or disable the adapter.",
        )

    scheduler_enabled = _boolean(values, "METAHUB_MCP_SCHEDULER_ENABLED")
    if scheduler_enabled and not enabled:
        raise ConfigError(
            code="infra.config_invalid",
            message="MCP scheduler cannot run while the feature is disabled.",
            suggestion="Enable MCP first or stop its scheduler.",
        )
    if executor_mode == "sandbox_http" and not scheduler_enabled:
        raise ConfigError(
            code="infra.config_invalid",
            message="MCP sandbox_http executor requires the Scheduler role.",
            suggestion="Enable the dedicated Scheduler or keep the executor disabled.",
        )
    scheduler_poll = _bounded_float(
        values,
        "METAHUB_MCP_SCHEDULER_POLL_INTERVAL_SECONDS",
        minimum=0.1,
        maximum=60.0,
    )
    scheduler_batch = _bounded_int(
        values, "METAHUB_MCP_SCHEDULER_BATCH_SIZE", minimum=1, maximum=100
    )
    global_concurrency = _bounded_int(
        values, "METAHUB_MCP_GLOBAL_CONCURRENCY", minimum=1, maximum=32
    )
    tenant_concurrency = _bounded_int(
        values, "METAHUB_MCP_TENANT_CONCURRENCY", minimum=1, maximum=8
    )
    if tenant_concurrency > global_concurrency:
        raise ConfigError(
            code="infra.config_invalid",
            message="MCP tenant concurrency cannot exceed global concurrency.",
            suggestion="Lower the tenant limit or raise the bounded global limit.",
        )
    operation_lease = _bounded_int(
        values, "METAHUB_MCP_OPERATION_LEASE_SECONDS", minimum=30, maximum=3_600
    )
    invocation_lease = _bounded_int(
        values, "METAHUB_MCP_INVOCATION_LEASE_SECONDS", minimum=30, maximum=3_600
    )
    probe_deadline = _bounded_int(
        values, "METAHUB_MCP_PROBE_DEADLINE_SECONDS", minimum=1, maximum=30
    )
    call_deadline = _bounded_int(
        values, "METAHUB_MCP_CALL_DEADLINE_SECONDS", minimum=1, maximum=120
    )
    if probe_deadline >= operation_lease or call_deadline >= invocation_lease:
        raise ConfigError(
            code="infra.config_invalid",
            message="MCP leases must outlive their probe and call deadlines.",
            suggestion="Increase lease duration or lower the bounded deadline.",
        )

    caps = McpNetworkPolicyCaps(
        connect_timeout_max_seconds=_bounded_int(
            values, "METAHUB_MCP_NETWORK_CONNECT_TIMEOUT_MAX_SECONDS", minimum=1, maximum=10
        ),
        probe_timeout_max_seconds=_bounded_int(
            values, "METAHUB_MCP_NETWORK_PROBE_TIMEOUT_MAX_SECONDS", minimum=1, maximum=30
        ),
        call_timeout_max_seconds=_bounded_int(
            values, "METAHUB_MCP_NETWORK_CALL_TIMEOUT_MAX_SECONDS", minimum=1, maximum=120
        ),
        failure_threshold_max=_bounded_int(
            values, "METAHUB_MCP_NETWORK_FAILURE_THRESHOLD_MAX", minimum=1, maximum=10
        ),
        open_seconds_max=_bounded_int(
            values, "METAHUB_MCP_NETWORK_OPEN_SECONDS_MAX", minimum=10, maximum=3600
        ),
    )
    if (
        operation_lease <= caps.probe_timeout_max_seconds + MCP_SANDBOX_RESPONSE_RESERVE_SECONDS + 6
        or invocation_lease <= caps.call_timeout_max_seconds
        or probe_deadline > caps.probe_timeout_max_seconds
        or call_deadline > caps.call_timeout_max_seconds
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="MCP leases and deadlines do not fit the shared network policy caps.",
            suggestion=(
                "Keep deadlines within caps and leases beyond response and finalize reserves."
            ),
        )

    sandbox_base_url = _raw_optional_value(values, "METAHUB_MCP_SANDBOX_BASE_URL")
    sandbox_internal_token = _raw_optional_value(values, "METAHUB_MCP_SANDBOX_INTERNAL_TOKEN")
    sandbox_envelope_secret = _raw_optional_value(values, "METAHUB_MCP_SANDBOX_ENVELOPE_SECRET")
    sandbox_response_version = _positive_int(values, "METAHUB_MCP_SANDBOX_RESPONSE_KEY_VERSION")
    sandbox_response_current = _raw_optional_value(
        values, "METAHUB_MCP_SANDBOX_RESPONSE_SECRET_CURRENT"
    )
    sandbox_response_previous = _raw_optional_value(
        values, "METAHUB_MCP_SANDBOX_RESPONSE_SECRET_PREVIOUS"
    )
    sandbox_values = (
        sandbox_base_url,
        sandbox_internal_token,
        sandbox_envelope_secret,
        sandbox_response_current,
    )
    if executor_mode == "sandbox_http":
        if any(value is None for value in sandbox_values):
            raise ConfigError(
                code="infra.config_missing",
                message="MCP Scheduler Sandbox configuration is incomplete.",
                suggestion="Configure the internal URL and all independent Scheduler secrets.",
            )
        assert sandbox_base_url is not None
        sandbox_base_url = _validated_mcp_sandbox_url(sandbox_base_url)
        _validate_mcp_sandbox_secrets(
            secrets=(
                sandbox_internal_token,
                sandbox_envelope_secret,
                sandbox_response_current,
                sandbox_response_previous,
            ),
            forbidden_secrets=(
                *forbidden_secrets,
                encryption_secret,
                encryption_previous_secret,
                hmac_secret,
                hmac_previous_secret,
            ),
        )
        if sandbox_response_previous is not None and sandbox_response_version < 2:
            raise ConfigError(
                code="infra.config_invalid",
                message="MCP Sandbox previous response key requires a consecutive version.",
                suggestion="Advance the current response version before retaining the old key.",
            )
    elif any(value is not None for value in (*sandbox_values, sandbox_response_previous)):
        raise ConfigError(
            code="infra.config_invalid",
            message="MCP Sandbox Scheduler settings are forbidden outside sandbox_http mode.",
            suggestion="Clear Sandbox settings from Control and non-Scheduler processes.",
        )

    is_scheduler_service = service_name == "mcp-scheduler-worker"
    if environment == "prod":
        if is_scheduler_service and (not scheduler_enabled or executor_mode != "sandbox_http"):
            raise ConfigError(
                code="infra.config_invalid",
                message="Production MCP Scheduler role configuration is incomplete.",
                suggestion="Enable its Scheduler and use only the sandbox_http executor.",
            )
        if not is_scheduler_service and (scheduler_enabled or executor_mode != "disabled"):
            raise ConfigError(
                code="infra.config_invalid",
                message="Production Control and non-Scheduler roles cannot claim MCP jobs.",
                suggestion="Disable their MCP Scheduler and executor settings.",
            )

    bounded_ints: dict[str, tuple[int, int]] = {
        "mcp_register_max_bytes": (1_024, 16_384),
        "mcp_refresh_max_bytes": (2, 4_096),
        "mcp_policy_max_bytes": (1_024, 32_768),
        "mcp_invoke_max_bytes": (1_024, 131_072),
        "mcp_arguments_max_bytes": (1_024, 65_536),
        "mcp_schema_max_bytes": (1_024, 65_536),
        "mcp_server_schema_max_bytes": (1_024, 1_048_576),
        "mcp_result_max_bytes": (1_024, 1_048_576),
        "mcp_tree_max_depth": (4, 12),
        "mcp_tree_max_nodes": (64, 1_024),
        "mcp_schema_max_properties": (1, 256),
        "mcp_schema_max_keywords": (64, 2_048),
        "mcp_schema_max_combinator_branches": (1, 64),
        "mcp_schema_max_enum_items": (1, 256),
        "mcp_schema_max_enum_item_bytes": (64, 4_096),
        "mcp_schema_max_pattern_length": (1, 512),
        "mcp_validator_max_steps": (1_000, 100_000),
        "mcp_max_string_length": (64, 65_536),
        "mcp_max_array_items": (1, 1_024),
    }
    budget_values = {
        field: _bounded_int(
            values,
            "METAHUB_" + field.upper(),
            minimum=minimum,
            maximum=maximum,
        )
        for field, (minimum, maximum) in bounded_ints.items()
    }
    if (
        budget_values["mcp_arguments_max_bytes"] > budget_values["mcp_invoke_max_bytes"]
        or budget_values["mcp_schema_max_bytes"] > budget_values["mcp_server_schema_max_bytes"]
        or budget_values["mcp_schema_max_enum_item_bytes"] > budget_values["mcp_schema_max_bytes"]
        or budget_values["mcp_schema_max_properties"] > budget_values["mcp_tree_max_nodes"]
        or budget_values["mcp_max_array_items"] > budget_values["mcp_tree_max_nodes"]
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="MCP nested resource budgets are inconsistent.",
            suggestion="Keep child limits within their request, Schema, and tree parent budgets.",
        )

    if encryption_secret is not None and hmac_secret is not None:
        encryption_ring = [McpVersionedSecret(encryption_version, encryption_secret)]
        hmac_ring = [McpVersionedSecret(hmac_version, hmac_secret)]
        if encryption_previous_version is not None and encryption_previous_secret is not None:
            encryption_ring.append(
                McpVersionedSecret(encryption_previous_version, encryption_previous_secret)
            )
        if hmac_previous_version is not None and hmac_previous_secret is not None:
            hmac_ring.append(McpVersionedSecret(hmac_previous_version, hmac_previous_secret))
        try:
            validate_mcp_secret_domains(
                encryption_keyring=tuple(encryption_ring),
                hmac_keyring=tuple(hmac_ring),
                forbidden_secrets=forbidden_secrets,
            )
        except McpContractError:
            raise ConfigError(
                code="infra.config_invalid",
                message="MCP secret domains do not meet isolation requirements.",
                suggestion="Use independent current/previous roots not shared by another domain.",
            ) from None

    return {
        "mcp_enabled": enabled,
        "mcp_encryption_version": encryption_version,
        "mcp_encryption_secret": encryption_secret,
        "mcp_encryption_previous_version": encryption_previous_version,
        "mcp_encryption_previous_secret": encryption_previous_secret,
        "mcp_hmac_version": hmac_version,
        "mcp_hmac_secret": hmac_secret,
        "mcp_hmac_previous_version": hmac_previous_version,
        "mcp_hmac_previous_secret": hmac_previous_secret,
        "mcp_executor_mode": executor_mode,
        "mcp_test_loopback_enabled": test_loopback_enabled,
        "mcp_scheduler_enabled": scheduler_enabled,
        "mcp_scheduler_poll_interval_seconds": scheduler_poll,
        "mcp_scheduler_batch_size": scheduler_batch,
        "mcp_global_concurrency": global_concurrency,
        "mcp_tenant_concurrency": tenant_concurrency,
        "mcp_operation_lease_seconds": operation_lease,
        "mcp_invocation_lease_seconds": invocation_lease,
        "mcp_probe_deadline_seconds": probe_deadline,
        "mcp_call_deadline_seconds": call_deadline,
        "mcp_sandbox_base_url": sandbox_base_url,
        "mcp_sandbox_internal_token": sandbox_internal_token,
        "mcp_sandbox_envelope_secret": sandbox_envelope_secret,
        "mcp_sandbox_response_key_version": sandbox_response_version,
        "mcp_sandbox_response_secret_current": sandbox_response_current,
        "mcp_sandbox_response_secret_previous": sandbox_response_previous,
        "mcp_network_connect_timeout_max_seconds": caps.connect_timeout_max_seconds,
        "mcp_network_probe_timeout_max_seconds": caps.probe_timeout_max_seconds,
        "mcp_network_call_timeout_max_seconds": caps.call_timeout_max_seconds,
        "mcp_network_failure_threshold_max": caps.failure_threshold_max,
        "mcp_network_open_seconds_max": caps.open_seconds_max,
        **budget_values,
    }


def _validated_mcp_sandbox_url(value: str) -> str:
    """Accept one canonical internal HTTP(S) origin with no path-carried credentials."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_MCP_SANDBOX_BASE_URL is invalid.",
            suggestion="Configure one canonical internal HTTP(S) origin.",
        ) from None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.hostname != parsed.hostname.lower()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port == 0
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_MCP_SANDBOX_BASE_URL is invalid.",
            suggestion="Remove userinfo, path, query, fragment, and non-HTTP transports.",
        )
    canonical = f"{parsed.scheme}://{parsed.hostname}"
    if ":" in parsed.hostname:
        canonical = f"{parsed.scheme}://[{parsed.hostname}]"
    if port is not None:
        canonical += f":{port}"
    if value.rstrip("/") != canonical:
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_MCP_SANDBOX_BASE_URL must be canonical.",
            suggestion="Use a lower-case origin without a trailing path.",
        )
    return canonical


def _validate_mcp_sandbox_secrets(
    *, secrets: tuple[object, ...], forbidden_secrets: tuple[object, ...]
) -> None:
    """Validate independent Scheduler authentication/response domains without disclosure."""

    present: list[bytes] = []
    for secret in secrets:
        if secret is None:
            continue
        if type(secret) is not str:
            raise ConfigError(
                code="infra.config_invalid",
                message="MCP Sandbox secret configuration is invalid.",
                suggestion="Use independent 32-512 byte UTF-8 secrets.",
            )
        try:
            encoded = secret.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise ConfigError(
                code="infra.config_invalid",
                message="MCP Sandbox secret configuration is invalid.",
                suggestion="Use independent 32-512 byte UTF-8 secrets.",
            ) from None
        if (
            not 32 <= len(encoded) <= 512
            or secret != secret.strip()
            or any(character.isspace() or ord(character) < 32 for character in secret)
        ):
            raise ConfigError(
                code="infra.config_invalid",
                message="MCP Sandbox secret configuration is invalid.",
                suggestion="Use independent 32-512 byte secrets without whitespace.",
            )
        if any(hmac.compare_digest(encoded, other) for other in present):
            raise ConfigError(
                code="infra.config_invalid",
                message="MCP Sandbox secrets must use independent key domains.",
                suggestion="Replace every reused Scheduler Sandbox secret.",
            )
        present.append(encoded)
    for candidate in present:
        for forbidden in forbidden_secrets:
            if type(forbidden) is not str:
                continue
            try:
                encoded_forbidden = forbidden.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                continue
            if hmac.compare_digest(candidate, encoded_forbidden):
                raise ConfigError(
                    code="infra.config_invalid",
                    message="MCP Sandbox secrets must not reuse another security domain.",
                    suggestion="Configure dedicated Scheduler Sandbox secrets.",
                )


def _build_config_items(values: dict[str, ConfigValue]) -> list[ConfigItemSummary]:
    """在 运行配置 内部复用 build config items 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    summary_keys = {
        "METAHUB_ENV",
        "METAHUB_LOG_LEVEL",
        "METAHUB_PUBLIC_BASE_URL",
        "METAHUB_CONTROL_API_BASE_URL",
        "METAHUB_GATEWAY_API_BASE_URL",
        "METAHUB_ALLOWED_ORIGINS",
        "METAHUB_SERVICE_MODE",
        "METAHUB_GATEWAY_PLACEMENT_ENABLED",
        "METAHUB_GATEWAY_CELL_CODE",
        "METAHUB_GATEWAY_PLACEMENT_SIGNING_VERSION",
        "METAHUB_GATEWAY_PLACEMENT_SIGNING_SECRET",
        "METAHUB_GATEWAY_PLACEMENT_SNAPSHOT_TTL_SECONDS",
        "METAHUB_EDGE_SNAPSHOT_SIGNING_VERSION",
        "METAHUB_EDGE_SNAPSHOT_SIGNING_SECRET",
        "METAHUB_GATEWAY_CAPACITY_TENANT_LIMIT",
        "METAHUB_GATEWAY_CAPACITY_WORKSPACE_LIMIT",
        "METAHUB_GATEWAY_CAPACITY_KEY_LIMIT",
        "METAHUB_GATEWAY_CAPACITY_CELL_LIMIT",
        "METAHUB_BUSINESS_VERIFICATION_KEY_VERSION",
        "METAHUB_BUSINESS_VERIFICATION_ENCRYPTION_SECRET",
        "METAHUB_BUSINESS_VERIFICATION_DOWNLOAD_TTL_SECONDS",
        "METAHUB_PAYMENT_WEBHOOK_KEY_VERSION",
        "METAHUB_PAYMENT_WEBHOOK_SECRET",
        "METAHUB_PAYMENT_WEBHOOK_PREVIOUS_KEY_VERSION",
        "METAHUB_PAYMENT_WEBHOOK_PREVIOUS_SECRET",
        "METAHUB_PAYMENT_RECEIPT_ENCRYPTION_KEY_VERSION",
        "METAHUB_PAYMENT_RECEIPT_ENCRYPTION_SECRET",
        "METAHUB_PAYMENT_RECEIPT_STORAGE_ROOT",
        "METAHUB_PAYMENT_WEBHOOK_TOLERANCE_SECONDS",
        "METAHUB_PAYMENT_WEBHOOK_MAX_BODY_BYTES",
        "METAHUB_PAYMENT_WORKER_POLL_INTERVAL_SECONDS",
        "METAHUB_DATABASE_URL",
        "METAHUB_REDIS_URL",
        "METAHUB_DATABASE_POOL_MIN_SIZE",
        "METAHUB_DATABASE_POOL_MAX_SIZE",
        "METAHUB_DATABASE_POOL_TIMEOUT_SECONDS",
        "METAHUB_DATABASE_STATEMENT_TIMEOUT_MS",
        "METAHUB_DATABASE_LOCK_TIMEOUT_MS",
        "METAHUB_INFRASTRUCTURE_PROBE_TIMEOUT_SECONDS",
        "METAHUB_OBJECT_STORE_ENDPOINT_URL",
        "METAHUB_OBJECT_STORE_REGION",
        "METAHUB_OBJECT_STORE_BUCKET",
        "METAHUB_OBJECT_STORE_ACCESS_KEY_ID",
        "METAHUB_OBJECT_STORE_SECRET_ACCESS_KEY",
        "METAHUB_OBJECT_STORE_FORCE_PATH_STYLE",
        "METAHUB_KMS_PROVIDER",
        "METAHUB_KMS_KEY_ID",
        "METAHUB_KMS_TEST_MASTER_KEY",
        "METAHUB_BACKUP_WORKER_ENABLED",
        "METAHUB_BACKUP_POLL_INTERVAL_SECONDS",
        "METAHUB_BACKUP_DEFAULT_RETENTION_DAYS",
        "METAHUB_BACKUP_RESTORE_DATABASE_URL",
        "METAHUB_CAPACITY_RUNNER_MODE",
        "METAHUB_CAPACITY_RUNNER_BASE_URL",
        "METAHUB_CAPACITY_RUNNER_INTERNAL_TOKEN",
        "METAHUB_CAPACITY_RUNNER_PERMIT_SECRET",
        "METAHUB_CAPACITY_RUNNER_HTTP_TIMEOUT_SECONDS",
        "METAHUB_INTERNAL_CONFIG_TOKEN",
        "METAHUB_INTERNAL_CHAT_TOKEN",
        "METAHUB_INTERNAL_BATCH_TOKEN",
        "METAHUB_BATCH_ENCRYPTION_SECRET",
        "METAHUB_BATCH_WORKER_ENABLED",
        "METAHUB_BATCH_POLL_INTERVAL_SECONDS",
        "METAHUB_BATCH_GLOBAL_CONCURRENCY",
        "METAHUB_BATCH_TENANT_CONCURRENCY",
        "METAHUB_BATCH_LEASE_SECONDS",
        "METAHUB_BATCH_HEARTBEAT_SECONDS",
        "METAHUB_BATCH_GATEWAY_CONCURRENCY",
        "METAHUB_BATCH_REALTIME_COOLDOWN_MILLISECONDS",
        "METAHUB_BATCH_REALTIME_RESERVE_PERCENT",
        "METAHUB_BATCH_MAX_PENDING_JOBS_PER_TENANT",
        "METAHUB_BATCH_MAX_PENDING_TASKS_PER_TENANT",
        "METAHUB_BATCH_MAX_RESULT_BYTES",
        "METAHUB_EXPERIMENT_SCHEDULER_INTERVAL_SECONDS",
        "METAHUB_EXPERIMENT_FINALIZER_LEASE_SECONDS",
        "METAHUB_EXPERIMENT_EXECUTION_LEASE_SECONDS",
        "METAHUB_EXPERIMENT_EXECUTION_HEARTBEAT_SECONDS",
        "METAHUB_EXPERIMENT_SCHEDULER_BATCH_LIMIT",
        "METAHUB_EXPERIMENT_EXECUTION_MAX_SECONDS",
        "METAHUB_EXPERIMENT_SETTLEMENT_GRACE_SECONDS",
        "METAHUB_SESSION_RISK_ENABLED",
        "METAHUB_SESSION_RISK_HMAC_VERSION",
        "METAHUB_SESSION_RISK_HMAC_SECRET",
        "METAHUB_SESSION_RISK_PREVIOUS_HMAC_VERSION",
        "METAHUB_SESSION_RISK_PREVIOUS_HMAC_SECRET",
        "METAHUB_APPROVAL_HMAC_VERSION",
        "METAHUB_APPROVAL_HMAC_SECRET",
        "METAHUB_APPROVAL_PREVIOUS_HMAC_VERSION",
        "METAHUB_APPROVAL_PREVIOUS_HMAC_SECRET",
        "METAHUB_APPROVAL_SCHEDULER_ENABLED",
        "METAHUB_APPROVAL_SCHEDULER_POLL_INTERVAL_SECONDS",
        "METAHUB_APPROVAL_SCHEDULER_BATCH_SIZE",
        "METAHUB_APPROVAL_OUTBOX_ENABLED",
        "METAHUB_APPROVAL_OUTBOX_POLL_INTERVAL_SECONDS",
        "METAHUB_APPROVAL_OUTBOX_BATCH_SIZE",
        "METAHUB_APPROVAL_WRITEBACK_ENABLED",
        "METAHUB_APPROVAL_WRITEBACK_POLL_INTERVAL_SECONDS",
        "METAHUB_APPROVAL_WRITEBACK_BATCH_SIZE",
        "METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET",
        "METAHUB_FILE_ENCRYPTION_SECRET",
        "METAHUB_FILE_MAX_UPLOAD_BYTES",
        "METAHUB_FILE_PARSE_CONCURRENCY",
        "METAHUB_FILE_SECURITY_SCAN_CONCURRENCY",
        "METAHUB_FILE_SECURITY_OCR_CONCURRENCY",
        "METAHUB_FILE_SECURITY_MAX_OCR_PAGES",
        "METAHUB_FILE_SECURITY_MAX_IMAGES",
        "METAHUB_FILE_SECURITY_MAX_IMAGE_PIXELS",
        "METAHUB_FILE_SECURITY_MAX_TOTAL_PIXELS",
        "METAHUB_AUTH_TOKEN_SECRET",
        "METAHUB_IDENTITY_SESSION_SECRET",
        "METAHUB_IDENTITY_CHALLENGE_SECRET",
        "METAHUB_IDENTITY_PII_ENCRYPTION_SECRET",
        "METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE",
        "METAHUB_ENTERPRISE_SSO_HMAC_SECRET",
        "METAHUB_ENTERPRISE_SSO_ENCRYPTION_SECRET",
        "METAHUB_WORKSPACE_SESSION_SECRET",
        "METAHUB_PLATFORM_SESSION_SECRET",
        "METAHUB_PLATFORM_SESSION_TTL_SECONDS",
        "METAHUB_SUPPORT_SESSION_SECRET",
        "METAHUB_SUPPORT_SESSION_TTL_SECONDS",
        "METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET",
        "METAHUB_PLATFORM_BOOTSTRAP_ENABLED",
        "METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET",
        "METAHUB_CACHE_AFFINITY_HMAC_SECRET",
        "METAHUB_RESPONSE_CACHE_ENABLED",
        "METAHUB_RESPONSE_CACHE_HMAC_VERSION",
        "METAHUB_RESPONSE_CACHE_HMAC_SECRET",
        "METAHUB_RESPONSE_CACHE_ENCRYPTION_VERSION",
        "METAHUB_RESPONSE_CACHE_ENCRYPTION_SECRET",
        "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_VERSION",
        "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_SECRET",
        "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_PREVIOUS_VERSION",
        "METAHUB_RESPONSE_CACHE_CONTROL_IDEMPOTENCY_PREVIOUS_SECRET",
        "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_VERSION",
        "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_SECRET",
        "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_PREVIOUS_VERSION",
        "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_PREVIOUS_SECRET",
        "METAHUB_RESPONSE_CACHE_MAX_READY_ENTRIES_PER_TENANT",
        "METAHUB_RESPONSE_CACHE_MAX_PLAINTEXT_BYTES",
        "METAHUB_RESPONSE_CACHE_FILL_LEASE_SECONDS",
        "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_ROOT",
        "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_POLL_INTERVAL_SECONDS",
        "METAHUB_RESPONSE_CACHE_FAILURE_SPOOL_CLAIM_LEASE_SECONDS",
        "METAHUB_FINOPS_USAGE_SPOOL_ROOT",
        "METAHUB_FINOPS_USAGE_SPOOL_POLL_INTERVAL_SECONDS",
        "METAHUB_FINOPS_USAGE_SPOOL_CLAIM_LEASE_SECONDS",
        "METAHUB_SESSION_TTL_SECONDS",
        "METAHUB_LOGIN_MAX_FAILURES",
        "METAHUB_LOGIN_LOCK_SECONDS",
        "METAHUB_DEFAULT_TENANT_SLUG",
        "METAHUB_CONFIG_FILE",
        "METAHUB_PROVIDER_HEALTH_ENABLED",
        "METAHUB_PROVIDER_HEALTH_INTERVAL_SECONDS",
        "METAHUB_PROVIDER_HEALTH_TIMEOUT_SECONDS",
        "METAHUB_PROVIDER_HEALTH_FAILURE_THRESHOLD",
        "METAHUB_PROVIDER_HEALTH_LEASE_SECONDS",
        "METAHUB_PROVIDER_HEALTH_STALE_SECONDS",
        "METAHUB_PROVIDER_HEALTH_CONCURRENCY",
        "METAHUB_PROVIDER_HEALTH_PRIVATE_CIDRS",
        "METAHUB_REQUEST_EXPLANATION_EVENT_ENABLED",
        "METAHUB_REQUEST_EXPLANATION_EVENT_POLL_INTERVAL_SECONDS",
        "METAHUB_REQUEST_LOG_QUERY_EVENT_ENABLED",
        "METAHUB_REQUEST_LOG_QUERY_EVENT_POLL_INTERVAL_SECONDS",
        "METAHUB_ADMIN_ASSISTANT_EVENT_ENABLED",
        "METAHUB_ADMIN_ASSISTANT_EVENT_POLL_INTERVAL_SECONDS",
        "METAHUB_COST_ANOMALY_ENABLED",
        "METAHUB_COST_ANOMALY_POLL_INTERVAL_SECONDS",
        "METAHUB_COST_ANOMALY_SPIKE_MULTIPLIER_MILLIS",
        "METAHUB_COST_ANOMALY_MIN_COST_MICROS",
        "METAHUB_COST_ANOMALY_WORKDAY_START_HOUR",
        "METAHUB_COST_ANOMALY_WORKDAY_END_HOUR",
        "METAHUB_POLICY_EVALUATION_TIMEOUT_MS",
        "METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED",
        "METAHUB_POLICY_SNAPSHOT_POLL_INTERVAL_SECONDS",
        "METAHUB_POLICY_SNAPSHOT_HTTP_TIMEOUT_SECONDS",
        "METAHUB_POLICY_SNAPSHOT_MAX_AGE_SECONDS",
        "METAHUB_POLICY_SNAPSHOT_EMERGENCY_MAX_AGE_SECONDS",
        "METAHUB_POLICY_SNAPSHOT_MAX_PAYLOAD_BYTES",
        "METAHUB_POLICY_MASK_RUNTIME_ENABLED",
        "METAHUB_STREAM_INTER_TOKEN_TIMEOUT_SECONDS",
        "METAHUB_STREAM_CONNECT_TIMEOUT_SECONDS",
        "METAHUB_STREAM_WRITE_TIMEOUT_SECONDS",
        "METAHUB_STREAM_POOL_TIMEOUT_SECONDS",
        "METAHUB_OUTPUT_SAFETY_INTERVAL_TOKENS",
        "METAHUB_OUTPUT_SAFETY_WINDOW_TOKENS",
        "METAHUB_OUTPUT_SAFETY_MAX_FULL_TEXT_CODE_POINTS",
    }
    summary_keys.update(key for key in DEFAULT_CONFIG if key.startswith("METAHUB_MCP_"))
    summary_keys.update(
        key
        for key in DEFAULT_CONFIG
        if key.startswith(
            (
                "METAHUB_LOCAL_SECRET_",
                "METAHUB_LOCAL_MANAGEMENT_",
                "METAHUB_EDGE_SUPPORT_BUNDLE_",
            )
        )
    )
    summary_keys.update(key for key in values if key.startswith("METAHUB_PROVIDER_"))

    return [_build_config_item(key, values.get(key)) for key in sorted(summary_keys)]


def _validate_policy_snapshot_config(
    *,
    service_name: str,
    environment: EnvironmentName,
    service_mode: ServiceMode,
    sync_enabled: bool,
    poll_interval_seconds: int,
    max_age_seconds: int,
    emergency_max_age_seconds: int,
    control_api_base_url: str,
    internal_config_token: str | None,
) -> None:
    """校验快照同步的交叉边界、跨服务身份和 Gateway Control URL。

    参数：已完成单字段解析的环境、模式、开关、时间门限、URL 与敏感 Token。
    返回：配置合法时无返回值；失败抛统一 ``ConfigError`` 并阻断服务启动。
    约束：生产/public Gateway 强制同步；任何服务启用同步或显式配置内部 Token 都执行
    高熵校验，只有真正发起拉取的 Gateway 校验 Control URL。Token 永远不进入错误正文。
    """

    # 普通 LKG 有效期至少覆盖一次轮询，否则健康配置也会在刷新间隔内自行过期。
    if max_age_seconds < poll_interval_seconds:
        raise ConfigError(
            code="infra.config_invalid",
            message=(
                "METAHUB_POLICY_SNAPSHOT_MAX_AGE_SECONDS cannot be lower than "
                "METAHUB_POLICY_SNAPSHOT_POLL_INTERVAL_SECONDS."
            ),
            suggestion="Increase snapshot max age or lower the bounded poll interval.",
        )
    # emergency 窗口是普通 LKG 的外层硬上限，不能比普通窗口更短而形成反向状态机。
    if emergency_max_age_seconds < max_age_seconds:
        raise ConfigError(
            code="infra.config_invalid",
            message=(
                "METAHUB_POLICY_SNAPSHOT_EMERGENCY_MAX_AGE_SECONDS cannot be lower than "
                "METAHUB_POLICY_SNAPSHOT_MAX_AGE_SECONDS."
            ),
            suggestion="Increase emergency max age so it covers the normal LKG window.",
        )
    if (
        service_name == "gateway-data-plane"
        and (environment == "prod" or service_mode == "public_service")
        and not sync_enabled
    ):
        # 面向生产或公网服务时关闭同步会让请求绕过已发布策略，必须 fail closed。
        raise ConfigError(
            code="infra.config_invalid",
            message="Policy snapshot sync cannot be disabled for this gateway deployment.",
            suggestion=(
                "Set METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED=true and configure internal access."
            ),
        )
    # 默认关闭且完全未配置内部身份时保持旧开发环境无副作用；显式 Token 即使暂未启用
    # 同步也要校验，避免部署切换开关后突然暴露一个长期存在的弱服务凭据。
    if not sync_enabled and internal_config_token is None:
        return
    # 同步接口始终使用独立服务身份，缺 Token 时不得退化为用户 Bearer 或匿名访问。
    if internal_config_token is None:
        raise ConfigError(
            code="infra.config_missing",
            message="METAHUB_INTERNAL_CONFIG_TOKEN is required for policy snapshot sync.",
            suggestion="Configure the same high-entropy internal token on Control and Gateway.",
        )
    try:
        token_bytes = internal_config_token.encode("ascii")
    # 非 ASCII Token 无法写入 httpx 请求头；启动阶段失败可避免首轮同步永久回退 builtin。
    except UnicodeEncodeError:
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_INTERNAL_CONFIG_TOKEN must contain ASCII characters only.",
            suggestion="Configure a high-entropy printable ASCII internal token.",
        ) from None
    # URL 结构必须唯一可解释，凭据和 query/fragment 不得进入日志、代理或重定向链路。
    if (
        len(token_bytes) < 32
        or len(token_bytes) > 512
        or any(character.isspace() or ord(character) < 32 for character in internal_config_token)
    ):
        # 这里只报告长度/字符合同，不回显或记录 Token，也不计算可被日志关联的明文摘要。
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_INTERNAL_CONFIG_TOKEN does not meet the security requirements.",
            suggestion=(
                "Use a 32-512 byte high-entropy token without whitespace or control characters."
            ),
        )
    # Control 只提供内部响应，不访问自身 base URL；只有 Gateway 启用同步时需要网络边界校验。
    if service_name == "gateway-data-plane" and sync_enabled:
        _validate_policy_snapshot_control_url(control_api_base_url, environment=environment)


def _validate_provider_error_map_config(
    *,
    service_name: str,
    sync_enabled: bool,
    fresh_seconds: float,
    lkg_seconds: float,
    control_api_base_url: str,
    internal_config_token: str | None,
    environment: EnvironmentName,
) -> None:
    """校验独立错误映射缓存的时间窗口、服务身份和 Control 网络边界。

    参数：服务名、同步开关、fresh/LKG 秒数、Control URL、内部 Token 和环境。
    返回：配置合法时无返回值。
    约束：规则仅影响诊断精度，关闭同步允许 builtin 降级；一旦开启则不得匿名拉取快照。
    """

    if lkg_seconds < fresh_seconds:
        # LKG 是 fresh 的外层硬窗口，反向配置会让同一制品在时间线上产生含糊状态。
        raise ConfigError(
            code="infra.config_invalid",
            message=(
                "METAHUB_PROVIDER_ERROR_MAP_LKG_SECONDS cannot be lower than "
                "METAHUB_PROVIDER_ERROR_MAP_FRESH_SECONDS."
            ),
            suggestion="Increase the LKG window so it covers the fresh snapshot window.",
        )
    if not sync_enabled:
        # 关闭时稳定使用 builtin 且不发网络请求；显式 Token 的强度已由共用内部身份门禁校验。
        return
    if internal_config_token is None:
        raise ConfigError(
            code="infra.config_missing",
            message="METAHUB_INTERNAL_CONFIG_TOKEN is required for provider error map sync.",
            suggestion="Configure the same high-entropy internal token on Control and Gateway.",
        )
    if service_name == "gateway-data-plane":
        # 只有实际发起拉取的 Gateway 校验目标 URL；Control 仅提供受 Token 保护的内部接口。
        _validate_policy_snapshot_control_url(
            control_api_base_url,
            environment=environment,
        )


def _validate_internal_chat_config(
    *,
    service_name: str,
    environment: EnvironmentName,
    gateway_api_base_url: str,
    internal_chat_token: str | None,
) -> None:
    """校验企业 Chat 的独立服务身份和 Control 到 Gateway 的目标边界。

    参数：当前服务、部署环境、Gateway 基础 URL 和内部 Chat Token。
    返回：配置合法时无返回值；否则抛出统一 ``ConfigError`` 阻断启动。
    约束：Token 仅允许可安全写入 HTTP Header 的 32..512 字节 ASCII；生产环境不得
    使用本地默认值。只有会主动调用 Gateway 的 Control 服务校验目标 URL，开发和测试
    的明文 HTTP 仅允许字面量回环地址，避免凭据经 DNS 重绑定或普通网络泄露。
    """

    # Chat 是可选 M2 能力；Token 缺失时路由失败关闭，但不能拖垮其他独立服务启动。
    if internal_chat_token is None:
        # 缺失身份不能调用内部 Chat；路由层会固定失败关闭。配置加载不阻断其他独立能力，
        # 从而避免一个可选 M2 入口扩大到整个 Gateway/Control 的启动可用性边界。
        return
    try:
        token_bytes = internal_chat_token.encode("ascii")
    # 非 ASCII 不能安全放入 HTTP Header，错误中也不得回显 Token 原值。
    except UnicodeEncodeError:
        # 错误只描述字段合同，绝不回显 Token、摘要或可关联前缀。
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_INTERNAL_CHAT_TOKEN must contain ASCII characters only.",
            suggestion="Configure a high-entropy printable ASCII internal Chat token.",
        ) from None
    if (
        len(token_bytes) < 32
        or len(token_bytes) > 512
        or any(character.isspace() or ord(character) < 32 for character in internal_chat_token)
    ):
        # 强度、长度或控制字符任一不合格都使用固定配置错误，禁止弱服务身份启动。
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_INTERNAL_CHAT_TOKEN does not meet the security requirements.",
            suggestion=(
                "Use a 32-512 byte high-entropy token without whitespace or control characters."
            ),
        )
    if environment == "prod" and hmac.compare_digest(
        internal_chat_token, DEFAULT_INTERNAL_CHAT_TOKEN
    ):
        # 本地默认值是公开开发约定，生产继续使用会使内部执行入口失去身份边界。
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_INTERNAL_CHAT_TOKEN must be explicitly replaced in prod.",
            suggestion=(
                "Configure the same independent high-entropy Chat token on Control and Gateway."
            ),
        )
    if service_name != "control-plane-api":
        return
    _validate_internal_chat_gateway_url(
        gateway_api_base_url,
        environment=environment,
    )


def _validate_batch_config(
    *,
    service_name: str,
    environment: EnvironmentName,
    enabled: bool,
    database_url: str | None,
    gateway_api_base_url: str,
    internal_batch_token: str | None,
    batch_encryption_secret: str | None,
    internal_config_token: str | None,
    internal_chat_token: str | None,
    auth_token_secret: str,
    cache_affinity_hmac_secret: str,
    prompt_template_encryption_secret: str | None,
    file_encryption_secret: str | None,
    provider_key_encryption_secret: str | None,
    session_risk_hmac_secret: str | None,
    session_risk_previous_hmac_secret: str | None,
    global_concurrency: int,
    tenant_concurrency: int,
    lease_seconds: int,
    heartbeat_seconds: int,
) -> None:
    """校验批任务并发、租约、内部认证与加密配置，拒绝复用其他安全域密钥。

    Validate Batch service identity, key isolation, and scheduler invariants.
    """

    # 批处理配置和密钥必须独立、完整且在范围内；不满足时拒绝启动。
    if tenant_concurrency > global_concurrency:
        # 批处理配置和密钥必须独立、完整且在范围内；不满足时拒绝启动。
        raise ConfigError(
            code="infra.config_invalid",
            message="Batch tenant concurrency cannot exceed global concurrency.",
            suggestion="Lower the tenant limit or increase the bounded global limit.",
        )
    if heartbeat_seconds * 2 >= lease_seconds:
        # 批处理配置和密钥必须独立、完整且在范围内；不满足时拒绝启动。
        raise ConfigError(
            code="infra.config_invalid",
            message="Batch heartbeat must be lower than half of the lease.",
            suggestion="Lower the heartbeat or increase the lease duration.",
        )
    if (
        service_name == "batch-worker"
        and enabled
        and (
            internal_batch_token is None or batch_encryption_secret is None or database_url is None
        )
    ):
        # 批处理配置和密钥必须独立、完整且在范围内；不满足时拒绝启动。
        raise ConfigError(
            code="infra.config_missing",
            message="Enabled Batch worker configuration is incomplete.",
            suggestion="Configure the Batch token, encryption secret, and database URL.",
        )
    # 批处理配置和密钥必须独立、完整且在范围内；不满足时拒绝启动。
    if internal_batch_token is not None:
        try:
            token_bytes = internal_batch_token.encode("ascii")
        # 批处理配置和密钥必须独立、完整且在范围内；不满足时拒绝启动。
        except UnicodeEncodeError:
            # 批处理配置和密钥必须独立、完整且在范围内；不满足时拒绝启动。
            raise ConfigError(
                code="infra.config_invalid",
                message="METAHUB_INTERNAL_BATCH_TOKEN must contain ASCII characters only.",
                suggestion="Configure an independent printable ASCII Batch token.",
            ) from None
        if not 32 <= len(token_bytes) <= 512 or any(
            character.isspace() or ord(character) < 32 for character in internal_batch_token
        ):
            # 批处理配置和密钥必须独立、完整且在范围内；不满足时拒绝启动。
            raise ConfigError(
                code="infra.config_invalid",
                message="METAHUB_INTERNAL_BATCH_TOKEN does not meet security requirements.",
                suggestion="Use a 32-512 byte token without whitespace or control characters.",
            )
        for other in (internal_config_token, internal_chat_token):
            # 批处理配置和密钥必须独立、完整且在范围内；不满足时拒绝启动。
            if other is not None and hmac.compare_digest(internal_batch_token, other):
                # 批处理配置和密钥必须独立、完整且在范围内；不满足时拒绝启动。
                raise ConfigError(
                    code="infra.config_invalid",
                    message="Batch and other internal service tokens must be different.",
                    suggestion="Configure a dedicated METAHUB_INTERNAL_BATCH_TOKEN.",
                )
        if environment == "prod" and hmac.compare_digest(
            internal_batch_token, DEFAULT_INTERNAL_BATCH_TOKEN
        ):
            # 批处理配置和密钥必须独立、完整且在范围内；不满足时拒绝启动。
            raise ConfigError(
                code="infra.config_invalid",
                message="METAHUB_INTERNAL_BATCH_TOKEN must be replaced in prod.",
                suggestion="Configure the same independent Batch token on Worker and Gateway.",
            )
    # 批处理配置和密钥必须独立、完整且在范围内；不满足时拒绝启动。
    if batch_encryption_secret is not None:
        try:
            secret_bytes = batch_encryption_secret.encode("utf-8")
        # 批处理配置和密钥必须独立、完整且在范围内；不满足时拒绝启动。
        except UnicodeEncodeError:
            # 批处理配置和密钥必须独立、完整且在范围内；不满足时拒绝启动。
            raise ConfigError(
                code="infra.config_invalid",
                message="METAHUB_BATCH_ENCRYPTION_SECRET is invalid.",
                suggestion="Configure an independent high-entropy UTF-8 secret.",
            ) from None
        if not 32 <= len(secret_bytes) <= 512 or any(
            character.isspace() or ord(character) < 32 for character in batch_encryption_secret
        ):
            # 批处理配置和密钥必须独立、完整且在范围内；不满足时拒绝启动。
            raise ConfigError(
                code="infra.config_invalid",
                message="METAHUB_BATCH_ENCRYPTION_SECRET does not meet security requirements.",
                suggestion="Use a 32-512 byte secret without whitespace or controls.",
            )
        other_secrets = (
            internal_config_token,
            internal_chat_token,
            internal_batch_token,
            auth_token_secret,
            cache_affinity_hmac_secret,
            prompt_template_encryption_secret,
            file_encryption_secret,
            provider_key_encryption_secret,
            session_risk_hmac_secret,
            session_risk_previous_hmac_secret,
        )
        if any(
            other is not None and hmac.compare_digest(batch_encryption_secret, other)
            for other in other_secrets
        ):
            # 批处理配置和密钥必须独立、完整且在范围内；不满足时拒绝启动。
            raise ConfigError(
                code="infra.config_invalid",
                message="Batch encryption secret must use an independent key domain.",
                suggestion="Configure a dedicated METAHUB_BATCH_ENCRYPTION_SECRET.",
            )
        if environment == "prod" and hmac.compare_digest(
            batch_encryption_secret, DEFAULT_BATCH_ENCRYPTION_SECRET
        ):
            # 批处理配置和密钥必须独立、完整且在范围内；不满足时拒绝启动。
            raise ConfigError(
                code="infra.config_invalid",
                message="METAHUB_BATCH_ENCRYPTION_SECRET must be replaced in prod.",
                suggestion="Configure an independent production Batch encryption secret.",
            )
    if service_name == "batch-worker" and enabled:
        _validate_internal_chat_gateway_url(
            gateway_api_base_url,
            environment=environment,
        )


def _validate_prompt_template_encryption_secret(
    *,
    environment: EnvironmentName,
    encryption_secret: str | None,
    auth_token_secret: str,
) -> None:
    """校验 Prompt 模板独立根密钥的强度与密钥域隔离。

    参数：部署环境、可选模板根密钥和登录 Token 根密钥。返回：合法或缺失时无返回值。
    缺失仅表示模板能力不可用，由模板路由固定返回 503；一旦显式配置，就必须至少 32 个
    UTF-8 字节、无控制/空白字符、不得复用 Auth 密钥，生产环境也不得使用示例占位值。
    """

    # 未配置时保持普通 Gateway/Chat 可用，模板分支再以固定 503 失败，不能生成默认密钥。
    if encryption_secret is None:
        return
    try:
        encoded = encryption_secret.encode("utf-8")
    # 非法 Unicode 无法在 Control/Gateway 间稳定派生同一 key，启动阶段即统一拒绝。
    except UnicodeEncodeError:
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET is invalid.",
            suggestion="Configure an independent high-entropy UTF-8 secret.",
        ) from None
    if (
        len(encoded) < 32
        or len(encoded) > 512
        or any(character.isspace() or ord(character) < 32 for character in encryption_secret)
    ):
        # 弱密钥不能延迟到第一次解密才发现，否则不同进程可能形成不一致可用性状态。
        raise ConfigError(
            code="infra.config_invalid",
            message=(
                "METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET does not meet security requirements."
            ),
            suggestion="Use an independent 32-512 byte secret without whitespace or controls.",
        )
    # Auth 与 Prompt 两个根域必须物理分离，不能只依赖派生 label 掩盖部署复用错误。
    if hmac.compare_digest(encryption_secret, auth_token_secret):
        raise ConfigError(
            code="infra.config_invalid",
            message="Prompt template and authentication secrets must be different.",
            suggestion="Configure a dedicated METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET.",
        )
    # 示例值公开存在于仓库，生产环境继续使用等价于没有保密性，必须在启动阶段阻断。
    if environment == "prod" and encryption_secret.startswith("replace-with-"):
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET must be replaced in prod.",
            suggestion="Configure the same independent production secret on Control and Gateway.",
        )


def _validate_file_encryption_secret(
    *,
    environment: EnvironmentName,
    encryption_secret: str | None,
    auth_token_secret: str,
    cache_affinity_hmac_secret: str,
    prompt_template_encryption_secret: str | None,
    provider_key_encryption_secret: str | None,
) -> None:
    """校验文件根密钥强度、生产默认值和业务域隔离。

    参数：环境、可选文件密钥及 Auth/Cache/Prompt/Provider 其他根密钥。返回：合法或生产
    缺失时无返回值。缺失只使文件 API 503；显式弱值、空白、开发默认值或跨域复用会阻断启动。
    """

    # 生产缺失密钥只关闭文件能力，健康、登录和其他控制面模块仍需保持可用。
    if encryption_secret is None:
        return
    try:
        encoded = encryption_secret.encode("utf-8")
    # 非 UTF-8 值会造成多副本派生不一致，必须在启动配置边界固定拒绝。
    except UnicodeEncodeError:
        # 错误信息只指出配置项，不回显候选密钥或编码失败位置。
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_FILE_ENCRYPTION_SECRET is invalid.",
            suggestion="Configure an independent high-entropy UTF-8 file secret.",
        ) from None
    if (
        len(encoded) < 32
        or len(encoded) > 512
        or any(character.isspace() or ord(character) < 32 for character in encryption_secret)
    ):
        # 弱密钥不能等到第一次文件上传才发现，否则多副本会形成不一致可用性状态。
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_FILE_ENCRYPTION_SECRET does not meet security requirements.",
            suggestion="Use an independent 32-512 byte secret without whitespace or controls.",
        )
    other_secrets = (
        auth_token_secret,
        cache_affinity_hmac_secret,
        prompt_template_encryption_secret,
        provider_key_encryption_secret,
    )
    if any(
        type(other) is str and hmac.compare_digest(encryption_secret, other)
        for other in other_secrets
    ):
        # 分域派生不能弥补部署层根密钥物理复用，统一拒绝且不说明命中了哪个字段。
        raise ConfigError(
            code="infra.config_invalid",
            message="File encryption secret must be isolated from other security domains.",
            suggestion="Configure a dedicated METAHUB_FILE_ENCRYPTION_SECRET.",
        )
    if environment == "prod" and (
        hmac.compare_digest(encryption_secret, DEFAULT_FILE_ENCRYPTION_SECRET)
        or encryption_secret.startswith("replace-with-")
    ):
        # 仓库公开的默认/示例值不能在生产加密企业文件。
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_FILE_ENCRYPTION_SECRET must be replaced in prod.",
            suggestion="Configure a dedicated production file encryption secret.",
        )


def _validate_tenant_data_encryption_secret(
    *,
    environment: EnvironmentName,
    encryption_secret: str | None,
    forbidden_secrets: tuple[str | None, ...],
) -> None:
    """校验 PRD-089 导出与留存根密钥，不让功能回退复用任何既有安全域。"""

    # 生产可以在尚未启用租户数据管理时缺省；业务入口会固定返回 unavailable，不能猜测其他密钥。
    if encryption_secret is None:
        return
    try:
        encoded = encryption_secret.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_TENANT_DATA_ENCRYPTION_SECRET is invalid.",
            suggestion="Configure an independent high-entropy UTF-8 tenant-data secret.",
        ) from None
    if not 32 <= len(encoded) <= 512 or any(
        character.isspace() or ord(character) < 32 for character in encryption_secret
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_TENANT_DATA_ENCRYPTION_SECRET does not meet security requirements.",
            suggestion="Use an independent 32-512 byte secret without whitespace or controls.",
        )
    if any(
        type(candidate) is str and hmac.compare_digest(encryption_secret, candidate)
        for candidate in forbidden_secrets
    ):
        # 根密钥物理隔离是部署合同；仅靠 AAD 分域不能抵消同一根密钥泄露的扩大影响。
        raise ConfigError(
            code="infra.config_invalid",
            message="Tenant-data encryption secret must use an independent key domain.",
            suggestion="Configure a dedicated METAHUB_TENANT_DATA_ENCRYPTION_SECRET.",
        )
    if environment == "prod" and (
        hmac.compare_digest(encryption_secret, DEFAULT_TENANT_DATA_ENCRYPTION_SECRET)
        or encryption_secret.startswith("replace-with-")
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_TENANT_DATA_ENCRYPTION_SECRET must be replaced in prod.",
            suggestion="Configure a dedicated production tenant-data encryption secret.",
        )


def _validate_edge_snapshot_signing_secret(
    *,
    environment: EnvironmentName,
    signing_secret: str | None,
    forbidden_secrets: tuple[str | None, ...],
) -> None:
    """Validate the independent Ed25519 root used for Edge desired-state snapshots."""

    if signing_secret is None:
        return
    try:
        encoded = signing_secret.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_EDGE_SNAPSHOT_SIGNING_SECRET is invalid.",
            suggestion="Configure an independent high-entropy UTF-8 signing secret.",
        ) from None
    if not 32 <= len(encoded) <= 512 or any(
        character.isspace() or ord(character) < 32 for character in signing_secret
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_EDGE_SNAPSHOT_SIGNING_SECRET does not meet security requirements.",
            suggestion="Use an independent 32-512 byte secret without whitespace or controls.",
        )
    if any(
        type(candidate) is str and hmac.compare_digest(signing_secret, candidate)
        for candidate in forbidden_secrets
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="Edge snapshot signing must use an independent key domain.",
            suggestion="Configure a dedicated METAHUB_EDGE_SNAPSHOT_SIGNING_SECRET.",
        )
    if environment == "prod" and (
        hmac.compare_digest(signing_secret, DEFAULT_EDGE_SNAPSHOT_SIGNING_SECRET)
        or signing_secret.startswith("replace-with-")
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_EDGE_SNAPSHOT_SIGNING_SECRET must be replaced in prod.",
            suggestion="Configure a dedicated production Edge snapshot signing secret.",
        )


def _validate_approval_hmac_config(
    *,
    environment: EnvironmentName,
    current_version: int,
    current_secret: str | None,
    previous_version: int | None,
    previous_secret: str | None,
    forbidden_secrets: tuple[str | None, ...],
) -> None:
    """Validate the independent approval keyring without exposing secrets.

    Production may start without this optional capability, but approval routes
    and workers then fail closed because ``approval_hmac_secret`` remains None.
    Any configured key must be strong, complete, ordered, and physically
    distinct from every existing credential or cryptographic root.
    ``_validate_approval_hmac_config`` 校验审批运行配置，密钥、租约和容量参数
    缺失或非法时必须失败关闭。
    """

    if type(current_version) is not int or current_version < 1:
        # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
        raise ConfigError(
            code="infra.config_invalid",
            message="Approval HMAC key version is invalid.",
            suggestion="Configure a positive integer current HMAC key version.",
        )
    # 该治理分支必须保持租户隔离和失败关闭，并通过固定错误语义保留请求链路可追踪性。
    if (previous_version is None) != (previous_secret is None):
        # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
        raise ConfigError(
            code="infra.config_invalid",
            message="Approval previous HMAC version and secret must be configured together.",
            suggestion="Configure both previous values or remove both values.",
        )
    # 审批 HMAC 密钥必须满足独立版本和强度约束；配置缺失或轮换关系非法时立即失败关闭。
    if current_secret is None:
        if previous_version is not None:
            # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
            raise ConfigError(
                code="infra.config_invalid",
                message="Approval current HMAC secret is required for key rotation.",
                suggestion="Configure an independent current approval HMAC secret.",
            )
        return
    if previous_version is not None and previous_version >= current_version:
        # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
        raise ConfigError(
            code="infra.config_invalid",
            message="Approval current HMAC version must be greater than previous version.",
            suggestion="Increase the current version while retaining the older version.",
        )

    configured = tuple(secret for secret in (current_secret, previous_secret) if secret is not None)
    for secret in configured:
        _validate_approval_hmac_secret(secret)
        if any(
            candidate is not None and hmac.compare_digest(secret, candidate)
            for candidate in forbidden_secrets
        ):
            # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
            raise ConfigError(
                code="infra.config_invalid",
                message="Approval HMAC secret must use an independent key domain.",
                suggestion="Configure a dedicated approval HMAC secret.",
            )
    if previous_secret is not None and hmac.compare_digest(current_secret, previous_secret):
        # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
        raise ConfigError(
            code="infra.config_invalid",
            message="Approval current and previous HMAC secrets must be different.",
            suggestion="Rotate to a new independent current secret.",
        )
    # 审批 HMAC 密钥必须满足独立版本和强度约束；配置缺失或轮换关系非法时立即失败关闭。
    if environment == "prod" and current_secret == DEFAULT_APPROVAL_HMAC_SECRET:
        # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_APPROVAL_HMAC_SECRET must be replaced in prod.",
            suggestion="Configure a dedicated production approval HMAC secret.",
        )


def _validate_approval_hmac_secret(secret: str) -> None:
    """Enforce the frozen 32..512 UTF-8 byte approval-secret boundary.
    ``_validate_approval_hmac_secret`` 校验审批运行配置，密钥、租约和容量参数
    缺失或非法时必须失败关闭。
    """

    try:
        encoded = secret.encode("utf-8")
    # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    except UnicodeEncodeError:
        # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
        raise ConfigError(
            code="infra.config_invalid",
            message="Approval HMAC secret is invalid.",
            suggestion="Configure a valid independent UTF-8 secret.",
        ) from None
    if not 32 <= len(encoded) <= 512 or any(
        character.isspace() or ord(character) < 32 for character in secret
    ):
        # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
        raise ConfigError(
            code="infra.config_invalid",
            message="Approval HMAC secret does not meet security requirements.",
            suggestion="Use a 32-512 byte secret without whitespace or control characters.",
        )


def _validate_session_risk_hmac_config(
    *,
    enabled: bool,
    current_version: int,
    current_secret: str | None,
    previous_version: int | None,
    previous_secret: str | None,
    auth_token_secret: str,
    cache_affinity_hmac_secret: str,
) -> None:
    """校验会话风险 current/previous keyring 的完整性与根密钥隔离。

    参数：部署开关、两组版本/secret 和既有 Auth/Cache 根密钥。返回：合法时无返回值。
    约束：enabled 必须配置 current；previous 必须成对、版本更旧；所有显式 secret 均为
    32..512 UTF-8 bytes、无空白/控制字符，且不能复用其他安全域或彼此复用。
    """

    if type(enabled) is not bool or type(current_version) is not int or current_version < 1:
        # 运行配置解析后仍执行精确类型防线，避免测试直接调用时 bool 冒充整数版本。
        raise ConfigError(
            code="infra.config_invalid",
            message="Session risk HMAC key version is invalid.",
            suggestion="Configure a positive integer current HMAC key version.",
        )
    # 启用会话风险却没有 current key 将破坏租户隔离派生，必须在服务启动阶段失败关闭。
    if enabled and current_secret is None:
        # 启用后没有 current key 无法派生租户隔离 scope，必须在服务启动阶段失败关闭。
        raise ConfigError(
            code="infra.config_missing",
            message="METAHUB_SESSION_RISK_HMAC_SECRET is required when session risk is enabled.",
            suggestion="Configure the same independent current HMAC key on Gateway and Control.",
        )
    # previous 版本与密钥必须成对出现，禁止半组轮换配置产生多服务查找歧义。
    if (previous_version is None) != (previous_secret is None):
        # 单独配置 previous 版本或 secret 会让轮换查找产生歧义，禁止宽松忽略半组配置。
        raise ConfigError(
            code="infra.config_invalid",
            message="Session risk previous HMAC version and secret must be configured together.",
            suggestion="Configure both previous values or remove both values.",
        )
    if previous_version is not None and previous_version >= current_version:
        # current 必须严格更新；相等或倒置版本无法从持久行判断应该使用哪把密钥。
        raise ConfigError(
            code="infra.config_invalid",
            message="Session risk current HMAC version must be greater than previous version.",
            suggestion="Increase the current version while retaining the older previous version.",
        )

    configured_secrets = [
        secret for secret in (current_secret, previous_secret) if secret is not None
    ]
    for secret in configured_secrets:
        _validate_session_risk_hmac_secret(secret)
        if hmac.compare_digest(secret, auth_token_secret) or hmac.compare_digest(
            secret, cache_affinity_hmac_secret
        ):
            # 会话身份与登录/Prompt affinity 域必须使用物理独立根密钥，不能只靠 label 分隔。
            raise ConfigError(
                code="infra.config_invalid",
                message="Session risk HMAC secret must use an independent key domain.",
                suggestion="Configure a dedicated session risk HMAC secret.",
            )
    if (
        current_secret is not None
        and previous_secret is not None
        and hmac.compare_digest(current_secret, previous_secret)
    ):
        # 仅提高版本但复用同一 secret 不构成可审计轮换，且会延长旧密钥暴露窗口。
        raise ConfigError(
            code="infra.config_invalid",
            message="Session risk current and previous HMAC secrets must be different.",
            suggestion="Rotate to a new independent current secret.",
        )


def _validate_session_risk_hmac_secret(secret: str) -> None:
    """校验单把会话风险 HMAC secret 的 UTF-8 长度与可打印边界。"""

    try:
        encoded = secret.encode("utf-8")
    # 代理字符无法在多进程间稳定编码，错误正文不得回显 secret 或异常细节。
    except UnicodeEncodeError:
        raise ConfigError(
            code="infra.config_invalid",
            message="Session risk HMAC secret is invalid.",
            suggestion="Configure a valid independent UTF-8 secret.",
        ) from None
    if not 32 <= len(encoded) <= 512 or any(
        character.isspace() or ord(character) < 32 for character in secret
    ):
        # 长度、空白或控制字符任一异常都使用固定字段级错误，不泄露实际值或长度。
        raise ConfigError(
            code="infra.config_invalid",
            message="Session risk HMAC secret does not meet security requirements.",
            suggestion="Use a 32-512 byte secret without whitespace or control characters.",
        )


def _validate_internal_chat_gateway_url(value: str, *, environment: EnvironmentName) -> None:
    """限制企业 Chat 内部 Gateway URL，拒绝凭据、歧义和非回环明文 HTTP。

    参数：Gateway 基础 URL 与当前环境。返回：合法时无返回值。
    约束：生产只允许 HTTPS；开发和测试使用 HTTP 时只允许 localhost 或字面量回环 IP。
    URL 中的用户名、密码、query 和 fragment 都可能改变目标或泄露服务身份，统一拒绝。
    """

    try:
        parsed = urlsplit(value)
        _ = parsed.port
    # 非法端口等解析错误可能包含原 URL，公开配置异常只返回字段级固定说明。
    except ValueError:
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_GATEWAY_API_BASE_URL is invalid for internal Chat.",
            suggestion="Use an absolute HTTPS URL, or a loopback HTTP URL in dev/test.",
        ) from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        # 凭据、query、fragment 或非 HTTP(S) 目标都可能改变转发边界，统一拒绝。
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_GATEWAY_API_BASE_URL is invalid for internal Chat.",
            suggestion="Use an absolute URL without credentials, query parameters, or fragments.",
        )
    if environment == "prod" and parsed.scheme != "https":
        # 生产内部 Token 只能经 TLS 发送，不能因网络位于内网就允许明文 HTTP。
        raise ConfigError(
            code="infra.config_invalid",
            message="Internal Chat requires an HTTPS Gateway URL in prod.",
            suggestion="Configure a trusted HTTPS Gateway endpoint.",
        )
    if parsed.scheme == "http" and not _is_loopback_hostname(parsed.hostname):
        # 开发明文只允许字面量回环，拒绝普通主机名以避免 DNS 重绑定泄露 Token。
        raise ConfigError(
            code="infra.config_invalid",
            message="Internal Chat HTTP Gateway URL must use a loopback address.",
            suggestion="Use HTTPS, or localhost/loopback HTTP only for dev and test.",
        )


def _validate_policy_snapshot_control_url(value: str, *, environment: EnvironmentName) -> None:
    """限制 Gateway 同步 URL：生产仅 HTTPS，开发 HTTP 仅可信回环地址。

    参数：Control API base URL 和当前环境；返回：合法时无返回值。
    约束：拒绝 URL 凭据、query/fragment 和非 HTTP(S) scheme；HTTP 不能依赖可重绑定域名。
    """

    try:
        parsed = urlsplit(value)
        # 访问 port 会触发非法端口的 ValueError，必须在启动时而不是后台任务中发现。
        _ = parsed.port
    except ValueError:
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_CONTROL_API_BASE_URL is invalid for policy snapshot sync.",
            suggestion="Use an absolute HTTPS URL, or a loopback HTTP URL in dev/test.",
        ) from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        # 不唯一的 URL 解释会泄露身份或改变请求目标，必须在后台任务启动前拒绝。
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_CONTROL_API_BASE_URL is invalid for policy snapshot sync.",
            suggestion="Use an absolute URL without credentials, query parameters, or fragments.",
        )
    # 生产同步携带内部身份和 canonical DSL，只允许可信 TLS 通道传输。
    if environment == "prod" and parsed.scheme != "https":
        raise ConfigError(
            code="infra.config_invalid",
            message="Policy snapshot sync requires HTTPS in prod.",
            suggestion="Configure an HTTPS Control API URL with a trusted certificate.",
        )
    # 非生产 HTTP 也只允许字面量回环，普通域名会引入窃听和 DNS 重绑定风险。
    if parsed.scheme == "http" and not _is_loopback_hostname(parsed.hostname):
        raise ConfigError(
            code="infra.config_invalid",
            message="Policy snapshot sync HTTP URL must use a loopback address.",
            suggestion="Use HTTPS, or localhost/loopback HTTP only for dev and test.",
        )


def _is_loopback_hostname(hostname: str) -> bool:
    """判断 URL hostname 是否为固定 localhost 或字面量回环 IP，避免 DNS 重绑定。"""

    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    # 非 IP hostname 不能因为解析失败被误判为回环地址，统一按非回环处理。
    except ValueError:
        return False


def _build_config_item(key: str, item: ConfigValue | None) -> ConfigItemSummary:
    """在 运行配置 内部复用 build config item 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    if item is None or item.value == "":
        return ConfigItemSummary(key=key, source="default", status="missing", value_preview="")
    # 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
    if _is_sensitive_config_key(key):
        return ConfigItemSummary(
            key=key,
            source=item.source,
            status="masked",
            value_preview="***MASKED***",
        )
    if item.source == "default":
        status: ConfigStatus = "defaulted"
    else:
        status = "configured"
    return ConfigItemSummary(
        key=key,
        source=item.source,
        status=status,
        value_preview=_preview_value(item.value),
    )


def _build_warnings(
    environment: EnvironmentName,
    provider_base_urls: dict[str, str],
    values: dict[str, ConfigValue],
) -> list[str]:
    """在 运行配置 内部复用 build warnings 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    warnings: list[str] = []
    # 认证令牌属于访问边界，失效或缺失时必须走统一错误。
    if environment == "prod" and not _optional_value(values, "METAHUB_INTERNAL_CONFIG_TOKEN"):
        warnings.append("METAHUB_INTERNAL_CONFIG_TOKEN is required before exposing prod internals.")
    auth_secret = values.get("METAHUB_AUTH_TOKEN_SECRET")
    # 令牌和密钥只允许哈希、指纹或密文参与判断，失败时不能暴露明文或有效性细节。
    if environment == "prod" and (auth_secret is None or auth_secret.source == "default"):
        warnings.append("METAHUB_AUTH_TOKEN_SECRET must be set to a non-default value in prod.")
    if not provider_base_urls:
        warnings.append("No Provider base URL configured; model calls are disabled.")
    if not _optional_value(values, "METAHUB_DATABASE_URL"):
        warnings.append("Database URL is not configured; database-backed PRDs cannot run yet.")
    if not _optional_value(values, "METAHUB_REDIS_URL"):
        warnings.append("Redis URL is not configured; cache/rate-limit PRDs cannot run yet.")
    return warnings


def _kms_provider(values: dict[str, ConfigValue]) -> Literal["disabled", "aws", "test"]:
    value = _value(values, "METAHUB_KMS_PROVIDER").strip().lower()
    if value not in {"disabled", "aws", "test"}:
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_KMS_PROVIDER is unsupported.",
            suggestion="Use disabled, aws, or the explicit local/test provider.",
        )
    return value  # type: ignore[return-value]


def _database_url_identity(value: str | None) -> tuple[str, str, int | None, str]:
    parsed = urlsplit(value or "")
    return parsed.scheme.lower(), parsed.hostname or "", parsed.port, parsed.path


def _validate_production_infrastructure_config(
    *,
    environment: EnvironmentName,
    database_url: str | None,
    database_pool_min_size: int,
    database_pool_max_size: int,
    redis_url: str | None,
    object_store_endpoint_url: str | None,
    object_store_region: str | None,
    object_store_bucket: str | None,
    object_store_access_key_id: str | None,
    object_store_secret_access_key: str | None,
    kms_provider: Literal["disabled", "aws", "test"],
    kms_key_id: str | None,
    kms_test_master_key: str | None,
    backup_worker_enabled: bool,
    backup_restore_database_url: str | None,
) -> None:
    """Reject ambiguous production fallback and incomplete backup security domains."""

    if database_pool_min_size > database_pool_max_size:
        raise ConfigError(
            code="infra.config_invalid",
            message="Database pool minimum cannot exceed its maximum.",
            suggestion="Lower the pool minimum or increase the bounded maximum.",
        )
    database_scheme = urlsplit(database_url).scheme.lower() if database_url else None
    if database_scheme not in {None, "sqlite", "postgres", "postgresql"}:
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_DATABASE_URL uses an unsupported backend.",
            suggestion="Use explicit SQLite for local/test or PostgreSQL for production.",
        )
    if environment == "prod" and database_scheme == "sqlite":
        raise ConfigError(
            code="infra.config_invalid",
            message="SQLite is not a production database backend.",
            suggestion="Configure a PostgreSQL 16 URL; production never falls back to SQLite.",
        )
    if environment == "prod" and database_scheme in {"postgres", "postgresql"}:
        sslmode = parse_qs(urlsplit(database_url).query).get("sslmode", [""])[-1].lower()
        if sslmode not in {"require", "verify-ca", "verify-full"}:
            raise ConfigError(
                code="infra.config_invalid",
                message="Production PostgreSQL must require TLS.",
                suggestion="Set sslmode=require, verify-ca, or verify-full in the database URL.",
            )
    redis_scheme = urlsplit(redis_url).scheme.lower() if redis_url else None
    if redis_scheme not in {None, "redis", "rediss"}:
        raise ConfigError(
            code="infra.config_invalid",
            message="METAHUB_REDIS_URL uses an unsupported scheme.",
            suggestion="Use redis:// locally or rediss:// with TLS in production.",
        )
    if environment == "prod" and redis_url and redis_scheme != "rediss":
        raise ConfigError(
            code="infra.config_invalid",
            message="Production Redis must use TLS.",
            suggestion="Configure a rediss:// URL and server certificate validation.",
        )

    object_values = (
        object_store_endpoint_url,
        object_store_region,
        object_store_bucket,
        object_store_access_key_id,
        object_store_secret_access_key,
    )
    if any(object_values) and not all(object_values):
        raise ConfigError(
            code="infra.config_invalid",
            message="S3-compatible object store configuration is incomplete.",
            suggestion="Configure endpoint, region, bucket, access key, and secret together.",
        )
    if object_store_bucket and re.fullmatch(
        r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", object_store_bucket
    ) is None:
        raise ConfigError(
            code="infra.config_invalid",
            message="Object store bucket name is invalid.",
            suggestion="Use a valid lower-case S3 bucket name.",
        )
    if object_store_endpoint_url:
        endpoint = urlsplit(object_store_endpoint_url)
        if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
            raise ConfigError(
                code="infra.config_invalid",
                message="Object store endpoint is invalid.",
                suggestion="Configure an absolute HTTP(S) S3-compatible endpoint.",
            )
        if environment == "prod" and endpoint.scheme != "https":
            raise ConfigError(
                code="infra.config_invalid",
                message="Production object storage must use TLS.",
                suggestion="Configure an HTTPS S3-compatible endpoint.",
            )

    if kms_provider == "aws" and not kms_key_id:
        raise ConfigError(
            code="infra.config_invalid",
            message="AWS KMS requires an explicit key identifier.",
            suggestion="Configure the deployment KeyRing identifier.",
        )
    if kms_provider == "test" and (
        environment == "prod" or not kms_test_master_key or len(kms_test_master_key) < 32
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="The local KMS test provider is not valid for this environment.",
            suggestion="Use it only in dev/test with an explicit 32-character test key.",
        )
    if backup_worker_enabled and (
        not database_url
        or not all(object_values)
        or kms_provider == "disabled"
        or (
            database_scheme in {"postgres", "postgresql"}
            and not backup_restore_database_url
        )
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="Backup worker infrastructure is incomplete.",
            suggestion=(
                "Configure database, isolated restore target, object storage, and KMS before "
                "enabling the backup worker."
            ),
        )
    restore_scheme = (
        urlsplit(backup_restore_database_url).scheme.lower()
        if backup_restore_database_url
        else None
    )
    if restore_scheme not in {None, "sqlite", "postgres", "postgresql"}:
        raise ConfigError(
            code="infra.config_invalid",
            message="Backup restore verification target is unsupported.",
            suggestion="Use an explicit isolated SQLite or PostgreSQL restore target.",
        )
    if (
        database_scheme in {"postgres", "postgresql"}
        and backup_restore_database_url
        and _database_url_identity(database_url)
        == _database_url_identity(backup_restore_database_url)
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="Backup restore target must be isolated from the source database.",
            suggestion="Configure a distinct restore-verification database.",
        )


def _ensure_no_secret_preview(items: list[ConfigItemSummary]) -> None:
    """在 运行配置 内部复用 ensure no secret preview 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    for item in items:
        # 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
        if (
            _is_sensitive_config_key(item.key)
            and item.status != "missing"
            and item.value_preview != "***MASKED***"
        ):
            # 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
            # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
            raise ConfigError(
                code="infra.config_secret_unmasked",
                message=f"Sensitive config item is not masked: {item.key}",
                suggestion="Fix config summary masking before exposing the endpoint.",
            )


def _split_origins(raw: str | None) -> list[str]:
    """在 运行配置 内部复用 split origins 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    if not raw:
        return ["http://localhost:5173"]
    return [item.strip() for item in raw.split(",") if item.strip()]


def _local_management_origins(
    raw: str, *, environment: EnvironmentName, enabled: bool
) -> list[str]:
    origins = [item.strip() for item in raw.split(",") if item.strip()]
    if not 1 <= len(origins) <= 8 or len(origins) != len(set(origins)):
        raise ConfigError(
            code="infra.config_invalid",
            message="Local management Origin allowlist is invalid.",
            suggestion="Configure one to eight distinct HTTPS or local development origins.",
        )
    for origin in origins:
        try:
            parsed = urlsplit(origin)
            port = parsed.port
        except ValueError:
            raise ConfigError(
                code="infra.config_invalid",
                message="Local management Origin allowlist is invalid.",
                suggestion="Use HTTPS origins; dev/test HTTP is limited to literal loopback.",
            ) from None
        loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or port == 0
            or (
                enabled
                and parsed.scheme == "http"
                and (environment == "prod" or not loopback)
            )
        ):
            raise ConfigError(
                code="infra.config_invalid",
                message="Local management Origin allowlist is invalid.",
                suggestion="Use HTTPS origins; dev/test HTTP is limited to literal loopback.",
            )
    return origins


def _workspace_allowed_regions(raw: str | None) -> tuple[str, ...]:
    regions = tuple(dict.fromkeys(item.strip() for item in (raw or "").split(",") if item.strip()))
    if (
        not regions
        or len(regions) > 8
        or any(
            region != region.lower()
            or not region.startswith("cn-")
            or len(region) not in range(3, 41)
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in region)
            or region.startswith("-")
            or region.endswith("-")
            for region in regions
        )
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="Workspace Region allowlist is invalid.",
            suggestion="Configure one to eight canonical cn-* Region codes.",
        )
    return regions


def _workspace_residency_policy_version(raw: str | None) -> str:
    value = (raw or "").strip()
    if (
        len(value) not in range(1, 81)
        or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for character in value
        )
    ):
        raise ConfigError(
            code="infra.config_invalid",
            message="Workspace residency policy version is invalid.",
            suggestion="Configure a bounded alphanumeric residency policy version.",
        )
    return value


_LOCAL_ENDPOINT_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)

# These metadata service addresses remain blocked even when an operator allows the
# surrounding private range. Keep the set shared by Control probes and Gateway calls.
LOCAL_ENDPOINT_BLOCKED_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("fd00:ec2::254"),
        ipaddress.ip_address("fd20:ce::254"),
    }
)


def _canonical_network_list(raw: str) -> list[str]:
    """Validate deployment-owned private CIDRs and return a stable canonical list."""

    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        try:
            network = ipaddress.ip_network(item, strict=True)
        except ValueError as exc:
            raise ConfigError(
                code="infra.config_invalid",
                message="Local model private CIDR configuration is invalid.",
                suggestion="Use canonical, non-overlapping IPv4 or IPv6 CIDR values.",
            ) from exc
        # Loopback is controlled only by METAHUB_LOCAL_ENDPOINT_ALLOW_LOOPBACK. The CIDR
        # allowlist is limited to enterprise RFC1918 IPv4 and IPv6 ULA networks.
        if network.is_loopback or not any(
            network.version == allowed.version and network.subnet_of(allowed)
            for allowed in _LOCAL_ENDPOINT_PRIVATE_NETWORKS
        ):
            raise ConfigError(
                code="infra.config_invalid",
                message="Local model private CIDR configuration is not private.",
                suggestion="Use RFC1918 IPv4 or IPv6 ULA CIDRs; configure loopback separately.",
            )
        if any(network == existing or network.overlaps(existing) for existing in networks):
            raise ConfigError(
                code="infra.config_invalid",
                message="Local model private CIDR configuration overlaps.",
                suggestion="Remove duplicate or overlapping CIDR values.",
            )
        networks.append(network)
    networks.sort(key=lambda item: (item.version, int(item.network_address), item.prefixlen))
    return [str(item) for item in networks]


def _bounded_port_list(raw: str) -> list[int]:
    """Parse the deployment-owned local model port allowlist."""

    ports: list[int] = []
    for item in (part.strip() for part in raw.split(",")):
        if not item or not item.isascii() or not item.isdigit():
            raise ConfigError(
                code="infra.config_invalid",
                message="Local model allowed ports configuration is invalid.",
                suggestion="Use a comma-separated list of ports between 1 and 65535.",
            )
        port = int(item)
        if port < 1 or port > 65_535 or port in ports:
            raise ConfigError(
                code="infra.config_invalid",
                message="Local model allowed ports configuration is invalid.",
                suggestion="Use unique ports between 1 and 65535.",
            )
        ports.append(port)
    if not ports or len(ports) > 32:
        raise ConfigError(
            code="infra.config_invalid",
            message="Local model allowed ports configuration is invalid.",
            suggestion="Configure between one and 32 allowed ports.",
        )
    return sorted(ports)


def _valid_identity_pii_secret(secret: object) -> bool:
    if type(secret) is not str or secret != secret.strip():
        return False
    try:
        encoded = secret.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return 32 <= len(encoded) <= 512 and all(ord(character) >= 33 for character in secret)


def _secret_reused(candidate: str, forbidden: tuple[object, ...]) -> bool:
    try:
        encoded_candidate = candidate.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return True
    for value in forbidden:
        if type(value) is not str or not value:
            continue
        try:
            encoded_value = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            continue
        if hmac.compare_digest(encoded_candidate, encoded_value):
            return True
    return False


def _value(values: dict[str, ConfigValue], key: str) -> str:
    """在 运行配置 内部复用 value 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    item = values.get(key)
    return item.value if item else ""


def _optional_value(values: dict[str, ConfigValue], key: str) -> str | None:
    """在 运行配置 内部复用 optional value 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    value = _value(values, key).strip()
    return value or None


def _raw_optional_value(values: dict[str, ConfigValue], key: str) -> str | None:
    """读取可选敏感值但保留首尾空白，交由强度门禁明确拒绝错误配置。"""

    value = _value(values, key)
    return value if value else None


def _optional_positive_int(values: dict[str, ConfigValue], key: str) -> int | None:
    """读取成对配置中的可选正整数，空值返回 None，非法值使用统一配置错误。"""

    if not _value(values, key).strip():
        return None
    # 非空值复用正整数门禁，确保错误不回显原始配置文本。
    return _positive_int(values, key)


def _positive_int(values: dict[str, ConfigValue], key: str) -> int:
    """在 运行配置 内部复用 positive int 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    raw = _value(values, key).strip()
    try:
        value = int(raw)
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except ValueError as exc:
        # 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
        raise ConfigError(
            code="infra.config_invalid",
            message=f"{key} must be a positive integer.",
            suggestion=f"Set {key} to a positive integer.",
        ) from exc
    if value <= 0:
        # 密钥相关分支只处理哈希、前缀或密文，禁止明文 Key 进入日志。
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        raise ConfigError(
            code="infra.config_invalid",
            message=f"{key} must be greater than zero.",
            suggestion=f"Set {key} to a positive integer.",
        )
    return value


def _bounded_int(values: dict[str, ConfigValue], key: str, *, minimum: int, maximum: int) -> int:
    """读取有上下界的健康探测配置，阻止异常参数放大调度或上游压力。"""

    value = _positive_int(values, key)
    if value < minimum or value > maximum:
        # 越界参数可能造成探测风暴或状态长期过期，必须在进程启动阶段拒绝。
        raise ConfigError(
            code="infra.config_invalid",
            message=f"{key} must be between {minimum} and {maximum}.",
            suggestion=f"Set {key} to a value between {minimum} and {maximum}.",
        )
    return value


def _bounded_nonnegative_int(values: dict[str, ConfigValue], key: str, *, maximum: int) -> int:
    """解析有上限的非负整型配置，拒绝布尔值、越界值和非规范文本。

    Read a canonical ASCII integer in ``0..maximum`` without accepting signs.
    """

    raw = _value(values, key).strip()
    if not raw or not raw.isascii() or not raw.isdecimal():
        # 批处理配置和密钥必须独立、完整且在范围内；不满足时拒绝启动。
        raise ConfigError(
            code="infra.config_invalid",
            message=f"{key} must be a non-negative integer.",
            suggestion=f"Set {key} to an integer from 0 through {maximum}.",
        )
    value = int(raw)
    if value > maximum:
        # 批处理配置和密钥必须独立、完整且在范围内；不满足时拒绝启动。
        raise ConfigError(
            code="infra.config_invalid",
            message=f"{key} must be between 0 and {maximum}.",
            suggestion=f"Set {key} to an integer from 0 through {maximum}.",
        )
    return value


def _bounded_float(
    values: dict[str, ConfigValue],
    key: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    """读取有限小数秒配置，并在启动前执行闭区间门禁。

    参数：合并配置、键名及允许的最小/最大值。返回：规范化有限浮点值。
    约束：不接受 NaN/Infinity，错误正文不回显原始配置，避免日志污染或敏感值外泄。
    """

    raw = _value(values, key).strip()
    try:
        value = float(raw)
    # 配置原文可能包含部署细节，转换失败时只返回固定字段名和允许范围，不回显原值。
    except ValueError:
        raise ConfigError(
            code="infra.config_invalid",
            message=f"{key} must be a finite number.",
            suggestion=f"Set {key} to a value between {minimum} and {maximum}.",
        ) from None
    if not math.isfinite(value) or value < minimum or value > maximum:
        # 非有限值会绕过普通大小比较；必须显式拒绝，不能让 deadline 永不触发。
        raise ConfigError(
            code="infra.config_invalid",
            message=f"{key} must be between {minimum} and {maximum}.",
            suggestion=f"Set {key} to a finite value in the documented range.",
        )
    return value


def _boolean(values: dict[str, ConfigValue], key: str) -> bool:
    """解析严格布尔配置，避免拼写错误被静默当作关闭或开启。"""

    raw = _value(values, key).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    # 未知布尔拼写不能静默退化，否则运维会误判调度开关实际状态。
    raise ConfigError(
        code="infra.config_invalid",
        message=f"{key} must be a boolean.",
        suggestion=f"Set {key}=true or {key}=false.",
    )


def _control_background_schedulers_enabled(
    values: dict[str, ConfigValue], environment: EnvironmentName
) -> bool:
    """Read the scheduler profile; local launchers may explicitly disable it."""

    return _boolean(values, "METAHUB_CONTROL_BACKGROUND_SCHEDULERS_ENABLED")


def _bounded_hour(values: dict[str, ConfigValue], key: str) -> int:
    """读取 0..23 的整点配置，允许午夜 0 点但拒绝浮点和含糊文本。

    参数：合并后的配置映射与待解析键名。返回：合法的日内小时整数。
    约束：工作时段的起止关系由调用方统一检查，错误正文不得回显原始配置值。
    """

    raw = _value(values, key).strip()
    # 显式 ASCII 十进制可同时排除符号、小数和科学计数法，避免时段解释漂移。
    if not raw or not raw.isascii() or not raw.isdecimal():
        raise ConfigError(
            code="infra.config_invalid",
            message=f"{key} must be an integer hour between 0 and 23.",
            suggestion=f"Set {key} to an integer from 0 through 23.",
        )
    value = int(raw)
    if value > 23:
        # 越界小时会改变非工作时段分类，必须在 scheduler 启动前失败关闭。
        raise ConfigError(
            code="infra.config_invalid",
            message=f"{key} must be between 0 and 23.",
            suggestion=f"Set {key} to an integer from 0 through 23.",
        )
    return value


def _config_profile(values: dict[str, ConfigValue]) -> str:
    """在 运行配置 内部复用 config profile 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    explicit = _optional_value(values, "METAHUB_CONFIG_FILE")
    if explicit:
        return Path(explicit).name
    if (Path.cwd() / ".env").exists():
        return ".env"
    return "default"


def _is_sensitive_config_key(key: str) -> bool:
    """在 运行配置 内部复用 is sensitive config key 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    normalized = key.lower()
    # “inter-token timeout”中的 token 指模型输出分片，不是认证令牌；该数值应在安全配置摘要中可见。
    if normalized == "metahub_stream_inter_token_timeout_seconds":
        return False
    return (
        is_sensitive_key(key)
        or "database_url" in normalized
        or "redis_url" in normalized
        or "object_store_endpoint_url" in normalized
        or "object_store_region" in normalized
        or "object_store_bucket" in normalized
        or "object_store_access_key_id" in normalized
        or "kms_key_id" in normalized
        or "kms_test_master_key" in normalized
        or normalized == "metahub_finops_usage_spool_root"
        or normalized == "metahub_response_cache_failure_spool_root"
        or normalized.endswith("_url_with_password")
    )


def _looks_like_http_url(value: str) -> bool:
    """在 运行配置 内部复用 looks like http url 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    return value.startswith("http://") or value.startswith("https://")


def _preview_value(value: str) -> str:
    """生成 运行配置 可安全展示或落日志的 preview value 内容。

    安全：输出只能包含脱敏摘要，不能泄露明文密钥、Token、Prompt 或完整凭据。
    """

    if len(value) <= 160:
        return value
    return f"{value[:157]}..."
