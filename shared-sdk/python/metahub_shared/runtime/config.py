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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from metahub_shared.files.crypto import DEFAULT_FILE_ENCRYPTION_SECRET

from .logging import is_sensitive_key

# PRD-002 只允许三类环境。这里不要放 staging、preprod 等别名，
# 因为运维最需要的是不同环境配置不会互相污染，而不是名字越多越灵活。
EnvironmentName = Literal["dev", "test", "prod"]
ConfigSource = Literal["default", "file", "env"]
ConfigStatus = Literal["configured", "missing", "masked", "defaulted"]
ServiceMode = Literal["internal_only", "public_service"]

# CacheAffinity 使用独立密钥域；默认值只保证本地可运行，生产 Gateway 必须显式替换。
DEFAULT_CACHE_AFFINITY_HMAC_SECRET = (
    "dev-only-change-me-metahub-cache-affinity-hmac-secret"
)
CACHE_AFFINITY_EXAMPLE_HMAC_SECRET = (
    "replace-with-cache-affinity-hmac-secret-at-least-32-characters"
)
# 企业 Chat 的 Control -> Gateway 身份使用独立密钥域。默认值仅服务于本机回环开发，
# 生产环境必须显式覆盖，且该值永远不能进入配置摘要、repr、日志或 API 响应。
DEFAULT_INTERNAL_CHAT_TOKEN = (
    "dev-only-change-me-metahub-internal-chat-token-2026"
)
DEFAULT_INTERNAL_BATCH_TOKEN = (
    "dev-only-change-me-metahub-internal-batch-token-2026"
)
DEFAULT_BATCH_ENCRYPTION_SECRET = (
    "dev-only-change-me-metahub-batch-encryption-secret-2026"
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
    "METAHUB_CACHE_AFFINITY_HMAC_SECRET": DEFAULT_CACHE_AFFINITY_HMAC_SECRET,
    # 空默认值让生产与未启用 Chat 的部署保持可启动；load 阶段仅在 dev/test 回环环境
    # 注入公开的本地开发值，内部 Chat 入口遇到生产缺失值必须失败关闭。
    "METAHUB_INTERNAL_CHAT_TOKEN": "",
    # Batch 使用独立服务身份和加密根域。空默认让未启用 Batch 的旧部署继续启动；
    # dev/test 在加载期注入公开本地值，生产和启用的 batch-worker 必须显式配置。
    "METAHUB_INTERNAL_BATCH_TOKEN": "",
    "METAHUB_BATCH_ENCRYPTION_SECRET": "",
    "METAHUB_BATCH_WORKER_ENABLED": "false",
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
    # 会话风险默认关闭，避免升级后旧部署因尚未配置 Redis/keyring 改变既有 Chat 行为。
    # 版本可以进入脱敏配置摘要；两项 secret 必须始终按敏感字段屏蔽。
    "METAHUB_SESSION_RISK_ENABLED": "false",
    "METAHUB_SESSION_RISK_HMAC_VERSION": "1",
    "METAHUB_SESSION_RISK_HMAC_SECRET": "",
    "METAHUB_SESSION_RISK_PREVIOUS_HMAC_VERSION": "",
    "METAHUB_SESSION_RISK_PREVIOUS_HMAC_SECRET": "",
    # Prompt 模板正文必须使用显式独立根密钥；空默认值只让未启用模板的旧部署继续启动，
    # 任何模板发布、读取或调用都会在业务边界固定失败为暂时不可用。
    "METAHUB_PROMPT_TEMPLATE_ENCRYPTION_SECRET": "",
    # 文件正文使用第四个独立密钥域。dev/test 在 load 阶段注入公开开发值；生产缺失保持
    # None，只让文件 API 503 失败关闭，不能拖垮健康、登录和其他控制面能力。
    "METAHUB_FILE_ENCRYPTION_SECRET": "",
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
    service_mode: ServiceMode = "internal_only"
    provider_base_urls: dict[str, str] = Field(default_factory=dict)
    features: dict[str, bool] = Field(default_factory=dict)
    config_items: list[ConfigItemSummary] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    sensitive_keys_masked: bool = True
    database_url: str | None = Field(default=None, exclude=True, repr=False)
    redis_url: str | None = Field(default=None, exclude=True, repr=False)
    provider_api_keys: dict[str, str] = Field(default_factory=dict, exclude=True, repr=False)
    internal_config_token: str | None = Field(default=None, exclude=True, repr=False)
    # 内部 Chat Token 只供 Control 与 Gateway 之间的服务身份校验，不能复用用户 Session、
    # Virtual Key 或策略同步 Token，也不能通过 model_dump/repr 意外泄露。
    internal_chat_token: str | None = Field(default=None, exclude=True, repr=False)
    # Batch 服务身份与正文/结果根密钥都是独立敏感域；数值门限是部署事实，HTTP 不能覆盖。
    internal_batch_token: str | None = Field(default=None, exclude=True, repr=False)
    batch_encryption_secret: str | None = Field(default=None, exclude=True, repr=False)
    batch_worker_enabled: bool = False
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
    # 多轮风险开关与 key 版本是部署级合同；secret 使用独立根域且绝不能进入序列化、repr、
    # 配置摘要明文或业务日志。previous 只服务于受控轮换，不得自动替代缺失 current。
    session_risk_enabled: bool = False
    session_risk_hmac_version: int = 1
    session_risk_hmac_secret: str | None = Field(default=None, exclude=True, repr=False)
    session_risk_previous_hmac_version: int | None = None
    session_risk_previous_hmac_secret: str | None = Field(
        default=None, exclude=True, repr=False
    )
    # 模板正文、变量 schema 和 change summary 共用同一独立根密钥并在实现中分域派生；
    # 缺失值允许非模板功能启动，但模板 API/Gateway 必须失败关闭，绝不回退 Auth/Provider 密钥。
    prompt_template_encryption_secret: str | None = Field(
        default=None, exclude=True, repr=False
    )
    # 原文件、抽取正文和文件名使用独立根密钥；root 也可能泄露宿主结构，两者均不参与
    # model_dump/repr。数值上限是部署级资源门禁，租户请求不能动态覆盖。
    file_encryption_secret: str | None = Field(default=None, exclude=True, repr=False)
    file_storage_root: str = Field(
        default=".data/file-storage", exclude=True, repr=False
    )
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
    # Prompt 指纹根密钥与登录 Token 密钥必须分域，且不得进入 model_dump、repr 或配置摘要明文。
    cache_affinity_hmac_secret: str = Field(exclude=True, repr=False)
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
    environment = _read_environment(values)
    service_mode = _read_service_mode(values)
    loaded_at = datetime.now(UTC)
    provider_base_urls = _read_provider_base_urls(values)
    provider_api_keys = _read_provider_api_keys(values)
    auth_token_secret = _value(values, "METAHUB_AUTH_TOKEN_SECRET")
    cache_affinity_hmac_secret = _value(values, "METAHUB_CACHE_AFFINITY_HMAC_SECRET")

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
                "METAHUB_USAGE_EXPORT_SYNC_ROW_LIMIT cannot exceed "
                "METAHUB_USAGE_EXPORT_MAX_ROWS."
            ),
            suggestion="Lower the sync row limit or increase the bounded max rows value.",
        )

    policy_snapshot_sync_enabled = _boolean(
        values, "METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED"
    )
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
    policy_mask_runtime_enabled = _boolean(
        values, "METAHUB_POLICY_MASK_RUNTIME_ENABLED"
    )
    provider_error_map_sync_enabled = _boolean(
        values, "METAHUB_PROVIDER_ERROR_MAP_SYNC_ENABLED"
    )
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
    configured_internal_chat_token = _optional_value(
        values, "METAHUB_INTERNAL_CHAT_TOKEN"
    )
    configured_internal_batch_token = _raw_optional_value(
        values, "METAHUB_INTERNAL_BATCH_TOKEN"
    )
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
    session_risk_enabled = _boolean(values, "METAHUB_SESSION_RISK_ENABLED")
    session_risk_hmac_version = _positive_int(
        values, "METAHUB_SESSION_RISK_HMAC_VERSION"
    )
    # secret 必须保留原始空白供强度门禁拒绝，不能先 ``strip`` 后把错误配置变成另一把 key。
    session_risk_hmac_secret = _raw_optional_value(
        values, "METAHUB_SESSION_RISK_HMAC_SECRET"
    )
    session_risk_previous_hmac_version = _optional_positive_int(
        values, "METAHUB_SESSION_RISK_PREVIOUS_HMAC_VERSION"
    )
    session_risk_previous_hmac_secret = _raw_optional_value(
        values, "METAHUB_SESSION_RISK_PREVIOUS_HMAC_SECRET"
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
    _validate_session_risk_hmac_config(
        enabled=session_risk_enabled,
        current_version=session_risk_hmac_version,
        current_secret=session_risk_hmac_secret,
        previous_version=session_risk_previous_hmac_version,
        previous_secret=session_risk_previous_hmac_secret,
        auth_token_secret=auth_token_secret,
        cache_affinity_hmac_secret=cache_affinity_hmac_secret,
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
    request_log_query_event_enabled = _boolean(
        values, "METAHUB_REQUEST_LOG_QUERY_EVENT_ENABLED"
    )
    request_log_query_event_poll_interval_seconds = _bounded_int(
        values,
        "METAHUB_REQUEST_LOG_QUERY_EVENT_POLL_INTERVAL_SECONDS",
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
    cost_anomaly_workday_end_hour = _bounded_hour(
        values, "METAHUB_COST_ANOMALY_WORKDAY_END_HOUR"
    )
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

    return AppConfig(
        service_name=service_name,
        environment=environment,
        environment_info=environment_info,
        log_level=_value(values, "METAHUB_LOG_LEVEL").upper(),
        public_base_url=_value(values, "METAHUB_PUBLIC_BASE_URL"),
        control_api_base_url=control_api_base_url,
        gateway_api_base_url=gateway_api_base_url,
        allowed_origins=_split_origins(_value(values, "METAHUB_ALLOWED_ORIGINS")),
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
            "session_risk_enabled": session_risk_enabled,
            "file_processing_enabled": file_encryption_secret is not None,
            "batch_worker_enabled": batch_worker_enabled,
        },
        config_items=config_items,
        warnings=warnings,
        database_url=_optional_value(values, "METAHUB_DATABASE_URL"),
        redis_url=_optional_value(values, "METAHUB_REDIS_URL"),
        provider_api_keys=provider_api_keys,
        internal_config_token=internal_config_token,
        internal_chat_token=internal_chat_token,
        internal_batch_token=internal_batch_token,
        batch_encryption_secret=batch_encryption_secret,
        batch_worker_enabled=batch_worker_enabled,
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
        session_risk_enabled=session_risk_enabled,
        session_risk_hmac_version=session_risk_hmac_version,
        session_risk_hmac_secret=session_risk_hmac_secret,
        session_risk_previous_hmac_version=session_risk_previous_hmac_version,
        session_risk_previous_hmac_secret=session_risk_previous_hmac_secret,
        prompt_template_encryption_secret=prompt_template_encryption_secret,
        file_encryption_secret=file_encryption_secret,
        file_storage_root=_validated_file_storage_root(
            _value(values, "METAHUB_FILE_STORAGE_ROOT")
        ),
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
        cache_affinity_hmac_secret=cache_affinity_hmac_secret,
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
        usage_export_enabled=_boolean(values, "METAHUB_USAGE_EXPORT_ENABLED"),
        usage_export_poll_interval_seconds=_bounded_int(
            values, "METAHUB_USAGE_EXPORT_POLL_INTERVAL_SECONDS", minimum=1, maximum=60
        ),
        usage_export_sync_row_limit=usage_export_sync_row_limit,
        usage_export_max_rows=usage_export_max_rows,
        usage_export_max_bytes=_bounded_int(
            # BytesIO、SQLite BLOB 与 HTTP Response 会形成有限副本，
            # MVP 硬上限不得超过设计的 50 MiB。
            values, "METAHUB_USAGE_EXPORT_MAX_BYTES", minimum=1024, maximum=52428800
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
        policy_snapshot_emergency_max_age_seconds=(
            policy_snapshot_emergency_max_age_seconds
        ),
        policy_snapshot_max_payload_bytes=policy_snapshot_max_payload_bytes,
        policy_mask_runtime_enabled=policy_mask_runtime_enabled,
        provider_error_map_sync_enabled=provider_error_map_sync_enabled,
        provider_error_map_poll_interval_seconds=(
            provider_error_map_poll_interval_seconds
        ),
        provider_error_map_http_timeout_seconds=(
            provider_error_map_http_timeout_seconds
        ),
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
        "METAHUB_DATABASE_URL",
        "METAHUB_REDIS_URL",
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
        "METAHUB_SESSION_RISK_ENABLED",
        "METAHUB_SESSION_RISK_HMAC_VERSION",
        "METAHUB_SESSION_RISK_HMAC_SECRET",
        "METAHUB_SESSION_RISK_PREVIOUS_HMAC_VERSION",
        "METAHUB_SESSION_RISK_PREVIOUS_HMAC_SECRET",
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
        "METAHUB_CACHE_AFFINITY_HMAC_SECRET",
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
        _validate_policy_snapshot_control_url(
            control_api_base_url, environment=environment
        )


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
    if service_name == "batch-worker" and enabled and (
        internal_batch_token is None
        or batch_encryption_secret is None
        or database_url is None
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
        if (
            not 32 <= len(token_bytes) <= 512
            or any(
                character.isspace() or ord(character) < 32
                for character in internal_batch_token
            )
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
        if (
            not 32 <= len(secret_bytes) <= 512
            or any(
                character.isspace() or ord(character) < 32
                for character in batch_encryption_secret
            )
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
    if (
        not 32 <= len(encoded) <= 512
        or any(character.isspace() or ord(character) < 32 for character in secret)
    ):
        # 长度、空白或控制字符任一异常都使用固定字段级错误，不泄露实际值或长度。
        raise ConfigError(
            code="infra.config_invalid",
            message="Session risk HMAC secret does not meet security requirements.",
            suggestion="Use a 32-512 byte secret without whitespace or control characters.",
        )


def _validate_internal_chat_gateway_url(
    value: str, *, environment: EnvironmentName
) -> None:
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


def _validate_policy_snapshot_control_url(
    value: str, *, environment: EnvironmentName
) -> None:
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


def _bounded_int(
    values: dict[str, ConfigValue], key: str, *, minimum: int, maximum: int
) -> int:
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


def _bounded_nonnegative_int(
    values: dict[str, ConfigValue], key: str, *, maximum: int
) -> int:
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
        or normalized == "metahub_finops_usage_spool_root"
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
