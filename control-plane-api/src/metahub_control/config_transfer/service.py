"""配置导出、预校验和原子导入服务。

CLI 只能通过本服务所在的 Control API 操作配置。导入先把不可信 JSON 归一为 create-only 计划，再在
单个 ``BEGIN IMMEDIATE`` 事务中写入四类配置、逐行事实和审计；任一错误都不得留下半批配置。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.security import hash_secret, secret_prefix
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import build_webhook_event

from metahub_control.audit import (
    AuditRecord,
    AuditRequestMeta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    record_audit_log,
)
from metahub_control.auth import AuthContext
from metahub_control.model_aliases.models import ModelAliasInfo
from metahub_control.model_aliases.service import (
    ROUTING_MODES,
    _dispatch_routing_policy_event,
    _insert_routing_policy_event,
    _load_model_alias,
    _mutation_fingerprint,
    _normalize_alias,
    _normalize_fallback_conditions,
    _normalize_quality_cost_config,
    _write_fallback_policy,
)
from metahub_control.model_aliases.service import (
    _normalize_description as _normalize_alias_description,
)
from metahub_control.model_aliases.service import (
    _normalize_status as _normalize_alias_status,
)
from metahub_control.models.service import (
    _normalize_capabilities,
    _normalize_context_window,
    _normalize_currency,
    _normalize_display_name,
    _normalize_internal_model_id,
    _normalize_price,
    _normalize_provider_model_id,
    _parse_capabilities,
)
from metahub_control.models.service import (
    _normalize_optional_text as _normalize_model_optional_text,
)
from metahub_control.models.service import (
    _normalize_status as _normalize_model_status,
)
from metahub_control.providers.service import (
    _normalize_auth_type,
    _normalize_base_url,
    _normalize_code,
    _normalize_maintenance_note,
    _normalize_provider_type,
    _validate_provider_auth_combination,
)
from metahub_control.providers.service import (
    _normalize_description as _normalize_provider_description,
)
from metahub_control.providers.service import (
    _normalize_name as _normalize_provider_name,
)
from metahub_control.providers.service import (
    _normalize_status as _normalize_provider_status,
)
from metahub_control.virtual_keys.service import (
    _generate_virtual_key,
    _normalize_limit_status,
    _normalize_limit_value,
)
from metahub_control.virtual_keys.service import (
    _normalize_description as _normalize_virtual_key_description,
)
from metahub_control.virtual_keys.service import (
    _normalize_name as _normalize_virtual_key_name,
)
from metahub_control.virtual_keys.service import (
    _normalize_status as _normalize_virtual_key_status,
)

from .models import (
    ConfigExport,
    ConfigExportResponse,
    ConfigImportResponse,
    GeneratedVirtualKey,
    ImportCounts,
    ImportReport,
    ImportRowError,
)
from .parser import MAX_CONFIG_BODY_BYTES, config_json_structure_error

logger = logging.getLogger("metahub.control.config_transfer")

# schema、集合、正文行数和错误数量都是跨 API/CLI 的稳定机器合同。
SCHEMA_VERSION = "metahub.config/v1"
COLLECTIONS = ("providers", "models", "aliases", "virtual_keys")
OBJECT_TYPES = {
    "providers": "provider",
    "models": "model",
    "aliases": "alias",
    "virtual_keys": "virtual_key",
}
MAX_COLLECTION_ROWS = {
    "providers": 500,
    "models": 2_000,
    "aliases": 2_000,
    "virtual_keys": 2_000,
}
MAX_TOTAL_ROWS = 5_000
MAX_RETURNED_ERRORS = 200
# 无效文档不能返回由攻击者凭据派生的稳定指纹；只有完整规范化成功后才替换正式 hash。
INVALID_MANIFEST_SHA256 = "0" * 64

# 未知字段名不回显；已知凭据语义统一折叠为 credential，避免攻击者把秘密放在 JSON key 中。
SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:authorization|cookie|credential|secret|token|password|passphrase|bearer|"
    r"plain[_-]?value|key[_-]?hash|key[_-]?prefix|encrypted[_-]?key|"
    r"(?:api|access|private|provider)[_-]?key|(?:^|[_-])key(?:$|[_-]))",
    re.IGNORECASE,
)
# camelCase 凭据字段先归一为下划线形式，避免 clientKey/signingKey/virtualKey 绕过敏感错误合同。
CAMEL_CASE_BOUNDARY_PATTERN = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
SAFE_REFERENCE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")

PROVIDER_FIELDS = frozenset(
    {
        "provider_code",
        "name",
        "provider_type",
        "base_url",
        "auth_type",
        "description",
        "maintenance_note",
        "status",
    }
)
MODEL_FIELDS = frozenset(
    {
        "internal_model_id",
        "provider_code",
        "provider_model_id",
        "display_name",
        "capabilities",
        "context_window",
        "input_price_per_1k",
        "output_price_per_1k",
        "currency",
        "price_schema_version",
        "description",
        "status",
    }
)
ALIAS_FIELDS = frozenset(
    {
        "alias",
        "environment",
        "default_internal_model_id",
        "fallback_internal_model_id",
        "fallback_conditions",
        "routing_mode",
        "quality_cost_config",
        "description",
        "status",
    }
)
VIRTUAL_KEY_FIELDS = frozenset(
    {
        "name",
        "environment",
        "description",
        "project_code",
        "application_code",
        "status",
        "rpm_limit",
        "tpm_limit",
        "concurrency_limit",
        "rate_limit_status",
    }
)

# 输入 JSON 必须按 wire 类型精确匹配；使用 ``type`` 而不是 ``isinstance``，明确排除 bool 冒充 int。
# 领域 CRUD 归一器原本接收 Pydantic 已校验值，会执行 int/str 转换，迁移入口必须先阻断这种静默改写。
STRICT_IMPORT_FIELD_TYPES: dict[tuple[str, str], tuple[type, ...]] = {
    ("providers", "provider_code"): (str,),
    ("providers", "name"): (str,),
    ("providers", "provider_type"): (str,),
    ("providers", "base_url"): (str,),
    ("providers", "auth_type"): (str,),
    ("providers", "description"): (str, type(None)),
    ("providers", "maintenance_note"): (str, type(None)),
    ("providers", "status"): (str,),
    ("models", "internal_model_id"): (str,),
    ("models", "provider_code"): (str,),
    ("models", "provider_model_id"): (str,),
    ("models", "display_name"): (str, type(None)),
    ("models", "capabilities"): (dict,),
    ("models", "context_window"): (int,),
    ("models", "input_price_per_1k"): (int, float),
    ("models", "output_price_per_1k"): (int, float),
    ("models", "currency"): (str,),
    ("models", "price_schema_version"): (str, type(None)),
    ("models", "description"): (str, type(None)),
    ("models", "status"): (str,),
    ("aliases", "alias"): (str,),
    ("aliases", "environment"): (str,),
    ("aliases", "default_internal_model_id"): (str,),
    ("aliases", "fallback_internal_model_id"): (str, type(None)),
    ("aliases", "fallback_conditions"): (list, type(None)),
    ("aliases", "routing_mode"): (str,),
    ("aliases", "quality_cost_config"): (dict,),
    ("aliases", "description"): (str, type(None)),
    ("aliases", "status"): (str,),
    ("virtual_keys", "name"): (str,),
    ("virtual_keys", "environment"): (str,),
    ("virtual_keys", "description"): (str, type(None)),
    ("virtual_keys", "project_code"): (str, type(None)),
    ("virtual_keys", "application_code"): (str, type(None)),
    ("virtual_keys", "status"): (str,),
    ("virtual_keys", "rpm_limit"): (int, type(None)),
    ("virtual_keys", "tpm_limit"): (int, type(None)),
    ("virtual_keys", "concurrency_limit"): (int, type(None)),
    ("virtual_keys", "rate_limit_status"): (str, type(None)),
}

# RoutingPolicy trigger 的 v1 初始值必须与数据库迁移保持一致，导出/导入可保留休眠质量配置。
DEFAULT_QUALITY_COST_CONFIG = {
    "quality_weight_bps": 5_000,
    "cost_weight_bps": 3_000,
    "latency_weight_bps": 2_000,
    "quality_floor_bps": 6_000,
    "minimum_sample_count": 20,
}


@dataclass
class _PlanRow:
    """一条已规范化并解析目标引用的导入计划。"""

    collection: str
    row_number: int
    values: dict[str, object]
    action: str = "created"
    object_id: str = ""
    business_key_sha256: str = ""


@dataclass
class _ImportPlan:
    """一次预检快照内的完整导入计划和安全错误集合。"""

    schema_version: str = SCHEMA_VERSION
    source_tenant_id: str = ""
    manifest_sha256: str = ""
    rows: dict[str, list[_PlanRow]] = field(
        default_factory=lambda: {collection: [] for collection in COLLECTIONS}
    )
    errors: list[ImportRowError] = field(default_factory=list)
    counts: ImportCounts = field(default_factory=ImportCounts)


@dataclass(frozen=True)
class _ReplayedImport:
    """精确重试恢复的报告、持久事件身份和原提交时间。"""

    report: ImportReport
    event_id: str
    occurred_at: datetime
    source_trace_id: str


def _connect(
    config: AppConfig,
    *,
    error_code: str = "config_transfer.import_unavailable",
) -> sqlite3.Connection:
    """创建配置迁移连接，并用调用链对应的稳定错误码隐藏基础设施细节。"""

    try:
        return connect_database(config.database_url)
    # 数据库路径和驱动异常属于基础设施细节，只允许映射为调用链指定的稳定领域码。
    except DbError as exc:
        # 原异常可能包含主机或文件路径，保留异常链供内部诊断但禁止进入公开响应。
        raise ApiError(error_code) from exc


def _utcnow() -> datetime:
    """返回 UTC aware 时间，保证导出、事实和事件使用同一时区。"""

    return datetime.now(UTC)


def _to_db_time(value: datetime) -> str:
    """把时间编码为 SQLite 可逐字比较的秒级 UTC 稳定文本。"""

    # 迁移事实与 AuditLog 的 owner trigger 会逐字比较时间；统一截断到秒，既满足
    # 数据库的 20 字符 UTC CHECK，也避免同一事务因微秒表示差异而被错误拒绝。
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _from_db_time(value: object) -> datetime:
    """严格解析秒级 UTC 数据库时间，损坏事实统一关闭导入恢复。"""

    if type(value) is not str:
        # 已持久事实类型异常表示数据库状态损坏，不能猜测转换后继续重放事件。
        raise ApiError("config_transfer.import_unavailable")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 数据库时间解析失败不能把原始列值或底层异常暴露给迁移客户端。
    except ValueError as exc:
        raise ApiError("config_transfer.import_unavailable") from exc
    if parsed.tzinfo is None or _to_db_time(parsed) != value:
        # 只承认迁移 CHECK 使用的 canonical UTC 秒格式，避免同一事实产生多个事件时间表示。
        raise ApiError("config_transfer.import_unavailable")
    return parsed.astimezone(UTC)


def _canonical_json_bytes(value: object) -> bytes:
    """生成禁止 NaN 的 canonical UTF-8 JSON，供清单及业务键计算稳定哈希。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    """计算安全对象的 canonical SHA-256，不保存或记录对象原文。"""

    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _manifest_hash(document: dict[str, object]) -> str:
    """计算业务 manifest hash，并排除导出时间这一无语义易变字段。"""

    stable = {key: value for key, value in document.items() if key != "exported_at"}
    return _sha256(stable)


class _RowInvalid(Exception):
    """控制单行归一化提前结束；公开错误已经写入计划，不携带原值。"""


def _add_error(
    plan: _ImportPlan,
    collection: str,
    row_number: int,
    field_name: str,
    code: str,
) -> None:
    """追加稳定行错误，未知字段和值都不能进入报告。"""

    plan.errors.append(
        ImportRowError(
            object_type=OBJECT_TYPES.get(collection, "document"),
            row=row_number,
            field=field_name,
            code=code,
        )
    )


def _normalized_field(
    plan: _ImportPlan,
    collection: str,
    row_number: int,
    field_name: str,
    normalizer: Any,
    value: object,
) -> object:
    """先执行精确 wire 类型门禁，再调用既有领域归一器。

    参数包含计划、集合、行号、白名单字段名、领域归一器和原始值；返回规范业务值。
    类型或领域校验失败只记录稳定行错误，不回显攻击者字段值，也不允许异常穿透为 500。
    """

    allowed_types = STRICT_IMPORT_FIELD_TYPES.get((collection, field_name))
    if allowed_types is None or type(value) not in allowed_types:
        # 精确类型是迁移 wire 合同；禁止 bool/小数/数字字符串被普通 CRUD 归一器静默转换。
        _add_error(plan, collection, row_number, field_name, "config_transfer.import_invalid")
        raise _RowInvalid
    try:
        return normalizer(value)
    # 领域归一器返回的公开错误码可安全复用，但绝不能把输入值拼入行级错误。
    except ApiError as exc:
        _add_error(plan, collection, row_number, field_name, exc.code)
        raise _RowInvalid from exc
    # 既有归一器假定调用方已完成 DTO 校验；意外类型异常仍需折叠，避免形成 500 或泄露。
    except (AttributeError, TypeError, ValueError) as exc:
        _add_error(plan, collection, row_number, field_name, "config_transfer.import_invalid")
        raise _RowInvalid from exc


def _has_sensitive_key(value: object) -> bool:
    """递归识别凭据语义字段名；只返回布尔结论，不回传攻击者 key。"""

    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                normalized_key = CAMEL_CASE_BOUNDARY_PATTERN.sub("_", str(key)).replace("-", "_")
                if SENSITIVE_KEY_PATTERN.search(normalized_key):
                    return True
                stack.append(child)
        elif isinstance(current, list):
            stack.extend(current)
    return False


def _normalize_reference(value: object, *, max_length: int = 120) -> str:
    """归一化环境、项目和应用等稳定业务引用。"""

    if type(value) is not str:
        # 稳定引用只接受 JSON 字符串，数字和布尔值不得通过隐式 ``str`` 转换改变语义。
        raise ApiError("config_transfer.import_invalid")
    normalized = value.strip().lower()
    if not normalized or len(normalized) > max_length or SAFE_REFERENCE_PATTERN.fullmatch(normalized) is None:
        # 引用会进入唯一键和审计指纹，必须拒绝空值、超长值及不可移植字符。
        raise ApiError("config_transfer.import_invalid")
    return normalized


def _normalize_optional_reference(value: object) -> str | None:
    """归一化可选业务引用；缺失和 null 表示未绑定，空字符串不是合法引用。"""

    if value is None:
        return None
    return _normalize_reference(value)


def _normalize_exported_at(value: object) -> str:
    """校验来源导出时间并输出 canonical UTC 文本；该字段不参与 manifest hash。"""

    if type(value) is not str:
        # 来源时间仅用于可读元数据，仍必须采用明确字符串合同，不能猜测 epoch 或本地时间。
        raise ApiError("config_transfer.import_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 时间解析异常可能含攻击者原值，对外只保留固定导入错误码。
    except ValueError as exc:
        raise ApiError("config_transfer.import_invalid") from exc
    if parsed.tzinfo is None:
        # 无时区时间在跨环境迁移时含义不确定，禁止按服务器本地时区补全。
        raise ApiError("config_transfer.import_invalid")
    return _to_db_time(parsed)


def _normalize_document_shape(document: dict[str, Any], plan: _ImportPlan) -> bool:
    """校验顶层字段、集合大小和敏感 key，返回是否可以继续逐行归一化。"""

    expected = {"schema_version", "source_tenant_id", "exported_at", *COLLECTIONS}
    if _has_sensitive_key(document):
        # 秘密语义优先于 exact-shape 报错，保证额外 authorization/api_key/key 也统一折叠。
        _add_error(plan, "document", 0, "credential", "config_transfer.import_sensitive_field")
        return False
    if set(document) != expected:
        _add_error(plan, "document", 0, "unknown", "config_transfer.import_invalid")
        return False
    if document.get("schema_version") != SCHEMA_VERSION:
        _add_error(plan, "document", 0, "schema_version", "config_transfer.schema_unsupported")
        return False
    source_tenant_id = document.get("source_tenant_id")
    # 来源租户只作为清单来源元数据参与 hash；它不能覆盖认证上下文中的目标 tenant_id，
    # 也不能接受可混淆类型或无界字符串形成跨租户查询输入。
    if type(source_tenant_id) is not str or not source_tenant_id.strip() or len(source_tenant_id) > 160:
        _add_error(plan, "document", 0, "source_tenant_id", "config_transfer.import_invalid")
        return False
    try:
        _normalize_exported_at(document.get("exported_at"))
    # 来源时间错误转换成文档级安全报告，不能中断为框架 500。
    except ApiError as exc:
        _add_error(plan, "document", 0, "exported_at", exc.code)
        return False
    total_rows = 0
    for collection in COLLECTIONS:
        rows = document.get(collection)
        if not isinstance(rows, list):
            _add_error(plan, collection, 0, collection, "config_transfer.import_invalid")
            return False
        total_rows += len(rows)
        if len(rows) > MAX_COLLECTION_ROWS[collection]:
            # 行数是请求资源边界而非业务字段错误，必须由统一 413 信封承载。
            raise ApiError("config_transfer.body_too_large")
    if total_rows > MAX_TOTAL_ROWS:
        # 跨集合总行数超限同样在逐行归一化前拒绝，避免构造放大的错误报告。
        raise ApiError("config_transfer.body_too_large")
    plan.source_tenant_id = source_tenant_id.strip()
    return True


def _require_exact_fields(
    plan: _ImportPlan,
    collection: str,
    row_number: int,
    row: object,
    allowed_fields: frozenset[str],
) -> dict[str, object]:
    """要求一行是对象且没有未知字段，未知 key 永远不原样回显。"""

    if not isinstance(row, dict):
        _add_error(plan, collection, row_number, "row", "config_transfer.import_invalid")
        raise _RowInvalid
    if any(key not in allowed_fields for key in row):
        _add_error(plan, collection, row_number, "unknown", "config_transfer.import_invalid")
        raise _RowInvalid
    return row


def _normalize_provider_rows(document: dict[str, Any], plan: _ImportPlan) -> None:
    """归一化 Provider 行并拒绝规范业务键重复。"""

    seen: set[str] = set()
    for row_number, raw in enumerate(document["providers"], start=1):
        try:
            row = _require_exact_fields(plan, "providers", row_number, raw, PROVIDER_FIELDS)
            values = {
                "provider_code": _normalized_field(
                    plan, "providers", row_number, "provider_code", _normalize_code, row.get("provider_code")
                ),
                "name": _normalized_field(
                    plan, "providers", row_number, "name", _normalize_provider_name, row.get("name")
                ),
                "provider_type": _normalized_field(
                    plan,
                    "providers",
                    row_number,
                    "provider_type",
                    _normalize_provider_type,
                    row.get("provider_type", "openai_compatible"),
                ),
                "base_url": _normalized_field(
                    plan, "providers", row_number, "base_url", _normalize_base_url, row.get("base_url")
                ),
                "auth_type": _normalized_field(
                    plan,
                    "providers",
                    row_number,
                    "auth_type",
                    _normalize_auth_type,
                    row.get("auth_type", "bearer"),
                ),
                "description": _normalized_field(
                    plan,
                    "providers",
                    row_number,
                    "description",
                    _normalize_provider_description,
                    row.get("description"),
                ),
                "maintenance_note": _normalized_field(
                    plan,
                    "providers",
                    row_number,
                    "maintenance_note",
                    _normalize_maintenance_note,
                    row.get("maintenance_note"),
                ),
                "status": _normalized_field(
                    plan,
                    "providers",
                    row_number,
                    "status",
                    _normalize_provider_status,
                    row.get("status", "active"),
                ),
            }
            try:
                # 导入与普通 CRUD 共用同一 type/auth spec 门禁，防止 create-only 旁路写入
                # custom_header 或厂家+none 后再由健康探测/Gateway 才发现不兼容。
                if values["provider_type"] == "local_endpoint":
                    raise ApiError("provider.local_endpoint_requires_local_api")
                _validate_provider_auth_combination(
                    values["provider_type"],
                    values["auth_type"],
                )
            # CRUD 门禁错误转换成带行号导入错误，不能让异常中断整份清单或泄露内部细节。
            except ApiError as exc:
                _add_error(plan, "providers", row_number, "auth_type", exc.code)
                raise _RowInvalid from exc
            provider_code = str(values["provider_code"])
            # 同一清单内的规范业务键必须唯一，否则行顺序会影响后续引用解析和 manifest 语义。
            if provider_code in seen:
                _add_error(plan, "providers", row_number, "provider_code", "config_transfer.import_invalid")
                continue
            seen.add(provider_code)
            plan.rows["providers"].append(
                _PlanRow(
                    "providers",
                    row_number,
                    values,
                    business_key_sha256=_sha256({"provider_code": provider_code}),
                )
            )
        except _RowInvalid:
            continue


def _normalize_model_rows(document: dict[str, Any], plan: _ImportPlan) -> None:
    """归一化 Model 行，引用暂保留业务 code 供数据库快照解析。"""

    seen: set[str] = set()
    for row_number, raw in enumerate(document["models"], start=1):
        try:
            row = _require_exact_fields(plan, "models", row_number, raw, MODEL_FIELDS)
            price_version = _normalized_field(
                plan,
                "models",
                row_number,
                "price_schema_version",
                lambda value: _normalize_model_optional_text(
                    value, 80, "model.create_invalid"
                ),
                row.get("price_schema_version"),
            )
            description = _normalized_field(
                plan,
                "models",
                row_number,
                "description",
                lambda value: _normalize_model_optional_text(
                    value, 240, "model.create_invalid"
                ),
                row.get("description"),
            )
            values = {
                "internal_model_id": _normalized_field(
                    plan,
                    "models",
                    row_number,
                    "internal_model_id",
                    _normalize_internal_model_id,
                    row.get("internal_model_id"),
                ),
                "provider_code": _normalized_field(
                    plan, "models", row_number, "provider_code", _normalize_code, row.get("provider_code")
                ),
                "provider_model_id": _normalized_field(
                    plan,
                    "models",
                    row_number,
                    "provider_model_id",
                    _normalize_provider_model_id,
                    row.get("provider_model_id"),
                ),
                "display_name": _normalized_field(
                    plan,
                    "models",
                    row_number,
                    "display_name",
                    _normalize_display_name,
                    row.get("display_name"),
                ),
                "capabilities": _normalized_field(
                    plan,
                    "models",
                    row_number,
                    "capabilities",
                    _normalize_capabilities,
                    row.get("capabilities"),
                ),
                "context_window": _normalized_field(
                    plan,
                    "models",
                    row_number,
                    "context_window",
                    _normalize_context_window,
                    row.get("context_window", 0),
                ),
                "input_price_per_1k": _normalized_field(
                    plan,
                    "models",
                    row_number,
                    "input_price_per_1k",
                    _normalize_price,
                    row.get("input_price_per_1k", 0),
                ),
                "output_price_per_1k": _normalized_field(
                    plan,
                    "models",
                    row_number,
                    "output_price_per_1k",
                    _normalize_price,
                    row.get("output_price_per_1k", 0),
                ),
                "currency": _normalized_field(
                    plan,
                    "models",
                    row_number,
                    "currency",
                    _normalize_currency,
                    row.get("currency", "CNY"),
                ),
                "price_schema_version": price_version,
                "description": description,
                "status": _normalized_field(
                    plan,
                    "models",
                    row_number,
                    "status",
                    _normalize_model_status,
                    row.get("status", "active"),
                ),
            }
            internal_id = str(values["internal_model_id"])
            if internal_id in seen:
                _add_error(plan, "models", row_number, "internal_model_id", "config_transfer.import_invalid")
                continue
            seen.add(internal_id)
            plan.rows["models"].append(
                _PlanRow(
                    "models",
                    row_number,
                    values,
                    business_key_sha256=_sha256({"internal_model_id": internal_id}),
                )
            )
        except _RowInvalid:
            continue


def _normalize_alias_rows(document: dict[str, Any], plan: _ImportPlan) -> None:
    """归一化清单中的 Alias、Fallback 与 RoutingPolicy 字段。

    参数：``document`` 是通过顶层形状门禁的原始清单，``plan`` 接收规范行和安全错误。
    返回：无返回值；合法行追加到计划，非法行只记录稳定字段名和错误码。
    约束：模型引用仍保留业务 ID，必须在目标租户数据库快照中再解析，来源 UUID 不可信。
    """

    seen: set[tuple[str, str]] = set()
    for row_number, raw in enumerate(document["aliases"], start=1):
        try:
            row = _require_exact_fields(plan, "aliases", row_number, raw, ALIAS_FIELDS)
            alias = _normalized_field(
                plan, "aliases", row_number, "alias", _normalize_alias, row.get("alias")
            )
            environment = _normalized_field(
                plan,
                "aliases",
                row_number,
                "environment",
                _normalize_reference,
                row.get("environment"),
            )
            default_model = _normalized_field(
                plan,
                "aliases",
                row_number,
                "default_internal_model_id",
                _normalize_internal_model_id,
                row.get("default_internal_model_id"),
            )
            fallback_raw = row.get("fallback_internal_model_id")
            fallback_model = None
            if fallback_raw is not None:
                fallback_model = _normalized_field(
                    plan,
                    "aliases",
                    row_number,
                    "fallback_internal_model_id",
                    _normalize_internal_model_id,
                    fallback_raw,
                )
            if fallback_model == default_model:
                _add_error(plan, "aliases", row_number, "fallback_internal_model_id", "model_alias.fallback_invalid")
                raise _RowInvalid
            routing_mode = row.get("routing_mode", "fixed")
            if type(routing_mode) is not str or routing_mode not in ROUTING_MODES:
                _add_error(plan, "aliases", row_number, "routing_mode", "model_alias.routing_mode_invalid")
                raise _RowInvalid
            quality = _normalized_field(
                plan,
                "aliases",
                row_number,
                "quality_cost_config",
                _normalize_quality_cost_config,
                row.get("quality_cost_config", DEFAULT_QUALITY_COST_CONFIG),
            )
            if fallback_model is None:
                raw_conditions = row.get("fallback_conditions", [])
                if raw_conditions not in (None, []):
                    _add_error(plan, "aliases", row_number, "fallback_conditions", "model_alias.fallback_invalid")
                    raise _RowInvalid
                fallback_conditions: list[str] = []
            else:
                fallback_conditions = _normalized_field(
                    plan,
                    "aliases",
                    row_number,
                    "fallback_conditions",
                    _normalize_fallback_conditions,
                    row.get("fallback_conditions"),
                )
            values = {
                "alias": alias,
                "environment": environment,
                "default_internal_model_id": default_model,
                "fallback_internal_model_id": fallback_model,
                "fallback_conditions": fallback_conditions,
                "routing_mode": routing_mode,
                "quality_cost_config": quality.model_dump(),
                "description": _normalized_field(
                    plan,
                    "aliases",
                    row_number,
                    "description",
                    _normalize_alias_description,
                    row.get("description"),
                ),
                "status": _normalized_field(
                    plan,
                    "aliases",
                    row_number,
                    "status",
                    _normalize_alias_status,
                    row.get("status", "active"),
                ),
            }
            key = (str(environment), str(alias))
            if key in seen:
                _add_error(plan, "aliases", row_number, "alias", "config_transfer.import_invalid")
                continue
            seen.add(key)
            plan.rows["aliases"].append(
                _PlanRow(
                    "aliases",
                    row_number,
                    values,
                    business_key_sha256=_sha256({"environment": environment, "alias": alias}),
                )
            )
        except _RowInvalid:
            continue


def _normalize_virtual_key_rows(document: dict[str, Any], plan: _ImportPlan) -> None:
    """归一化 Virtual Key 名称、业务绑定和基础限流，不接收任何旧凭据。"""

    seen: set[tuple[object, ...]] = set()
    for row_number, raw in enumerate(document["virtual_keys"], start=1):
        try:
            row = _require_exact_fields(plan, "virtual_keys", row_number, raw, VIRTUAL_KEY_FIELDS)
            environment = _normalized_field(
                plan,
                "virtual_keys",
                row_number,
                "environment",
                _normalize_reference,
                row.get("environment"),
            )
            project_code = _normalized_field(
                plan,
                "virtual_keys",
                row_number,
                "project_code",
                _normalize_optional_reference,
                row.get("project_code"),
            )
            application_code = _normalized_field(
                plan,
                "virtual_keys",
                row_number,
                "application_code",
                _normalize_optional_reference,
                row.get("application_code"),
            )
            if application_code is not None and project_code is None:
                _add_error(plan, "virtual_keys", row_number, "project_code", "application.project_mismatch")
                raise _RowInvalid
            limit_values = {
                name: _normalized_field(
                    plan,
                    "virtual_keys",
                    row_number,
                    name,
                    _normalize_limit_value,
                    row.get(name),
                )
                for name in ("rpm_limit", "tpm_limit", "concurrency_limit")
            }
            has_rule = any(value is not None for value in limit_values.values()) or row.get(
                "rate_limit_status"
            ) is not None
            rate_status = None
            if has_rule:
                rate_status = _normalized_field(
                    plan,
                    "virtual_keys",
                    row_number,
                    "rate_limit_status",
                    _normalize_limit_status,
                    row.get("rate_limit_status", "active"),
                )
            values = {
                "name": _normalized_field(
                    plan,
                    "virtual_keys",
                    row_number,
                    "name",
                    _normalize_virtual_key_name,
                    row.get("name"),
                ),
                "environment": environment,
                "description": _normalized_field(
                    plan,
                    "virtual_keys",
                    row_number,
                    "description",
                    _normalize_virtual_key_description,
                    row.get("description"),
                ),
                "project_code": project_code,
                "application_code": application_code,
                "status": _normalized_field(
                    plan,
                    "virtual_keys",
                    row_number,
                    "status",
                    _normalize_virtual_key_status,
                    row.get("status", "active"),
                ),
                **limit_values,
                "rate_limit_status": rate_status,
            }
            key = (environment, values["name"], project_code, application_code)
            if key in seen:
                _add_error(plan, "virtual_keys", row_number, "name", "config_transfer.import_invalid")
                continue
            seen.add(key)
            plan.rows["virtual_keys"].append(
                _PlanRow(
                    "virtual_keys",
                    row_number,
                    values,
                    business_key_sha256=_sha256(
                        {
                            "environment": environment,
                            "name": values["name"],
                            "project_code": project_code,
                            "application_code": application_code,
                        }
                    ),
                )
            )
        except _RowInvalid:
            continue


def _normalize_import_document(document: dict[str, Any]) -> _ImportPlan:
    """把严格 JSON 对象转换为数据库无关的规范计划。"""

    plan = _ImportPlan(manifest_sha256=INVALID_MANIFEST_SHA256)
    if not _normalize_document_shape(document, plan):
        return plan
    _normalize_provider_rows(document, plan)
    _normalize_model_rows(document, plan)
    _normalize_alias_rows(document, plan)
    _normalize_virtual_key_rows(document, plan)
    # 只有所有行都完成规范化后才能计算 manifest；错误清单使用固定零摘要，避免凭据或脏值
    # 通过哈希侧信道形成可跨请求比对的指纹。
    if not plan.errors:
        normalized_manifest = {
            "schema_version": SCHEMA_VERSION,
            "source_tenant_id": plan.source_tenant_id,
            **{
                collection: [row.values for row in plan.rows[collection]]
                for collection in COLLECTIONS
            },
        }
        plan.manifest_sha256 = _sha256(normalized_manifest)
    return plan


def _increment_count(plan: _ImportPlan, collection: str, action: str) -> None:
    """按集合和 create-only 动作推进导入报告计数。

    参数：``plan`` 保存当前批次计数，``collection`` 和 ``action`` 必须来自内部冻结枚举。
    返回：无返回值，原地递增对应 ``created`` 或 ``unchanged`` 字段。
    约束：该函数不接收用户自定义字段名，防止动态属性写入改变审计和 Webhook 合同。
    """

    singular = {
        "providers": "providers",
        "models": "models",
        "aliases": "aliases",
        "virtual_keys": "virtual_keys",
    }[collection]
    field_name = f"{singular}_{action}"
    setattr(plan.counts, field_name, getattr(plan.counts, field_name) + 1)


def _mark_conflict(plan: _ImportPlan, row: _PlanRow, field_name: str) -> None:
    """把同业务键不同配置折叠为稳定 conflict 行错误。"""

    _add_error(
        plan,
        row.collection,
        row.row_number,
        field_name,
        "config_transfer.import_conflict",
    )


def _resolve_environment(
    connection: sqlite3.Connection,
    tenant_id: str,
    plan: _ImportPlan,
    row: _PlanRow,
) -> tuple[str, str] | None:
    """在当前租户解析 active Environment code，禁止来源 ID 参与。"""

    environment = str(row.values["environment"])
    result = connection.execute(
        "SELECT environment_id, code, status FROM environments WHERE tenant_id=? AND code=?",
        (tenant_id, environment),
    ).fetchone()
    if result is None or result["status"] != "active":
        error_code = (
            "model_alias.environment_invalid"
            if row.collection == "aliases"
            else "virtual_key.environment_invalid"
        )
        _add_error(
            plan,
            row.collection,
            row.row_number,
            "environment",
            error_code,
        )
        return None
    return str(result["environment_id"]), str(result["code"])


def _prepare_provider_actions(
    connection: sqlite3.Connection,
    tenant_id: str,
    plan: _ImportPlan,
) -> dict[str, tuple[str, str]]:
    """解析 Provider create/unchanged，并返回 code 到目标 ID/状态映射。"""

    resolved: dict[str, tuple[str, str]] = {}
    for row in plan.rows["providers"]:
        code = str(row.values["provider_code"])
        existing = connection.execute(
            """
            SELECT provider_id, provider_code, name, provider_type, base_url, auth_type,
                   description, maintenance_note, status
            FROM providers
            WHERE tenant_id=? AND provider_code=?
            """,
            (tenant_id, code),
        ).fetchone()
        if existing is None:
            row.object_id = str(uuid4())
            row.action = "created"
            resolved[code] = (row.object_id, str(row.values["status"]))
            _increment_count(plan, row.collection, row.action)
            continue
        existing_values = {
            "provider_code": str(existing["provider_code"]),
            "name": str(existing["name"]),
            "provider_type": str(existing["provider_type"]),
            "base_url": str(existing["base_url"]),
            "auth_type": str(existing["auth_type"]),
            "description": existing["description"],
            "maintenance_note": existing["maintenance_note"],
            "status": str(existing["status"]),
        }
        row.object_id = str(existing["provider_id"])
        if existing_values != row.values:
            _mark_conflict(plan, row, "provider_code")
            continue
        row.action = "unchanged"
        resolved[code] = (row.object_id, str(existing["status"]))
        _increment_count(plan, row.collection, row.action)

    # Model 可以引用 manifest 未列出但目标租户已经存在的 Provider；仍按租户条件解析。
    for model_row in plan.rows["models"]:
        code = str(model_row.values["provider_code"])
        if code in resolved:
            continue
        existing = connection.execute(
            "SELECT provider_id, status FROM providers WHERE tenant_id=? AND provider_code=?",
            (tenant_id, code),
        ).fetchone()
        if existing is not None:
            resolved[code] = (str(existing["provider_id"]), str(existing["status"]))
    return resolved


def _prepare_model_actions(
    connection: sqlite3.Connection,
    tenant_id: str,
    plan: _ImportPlan,
    providers: dict[str, tuple[str, str]],
) -> dict[str, tuple[str, str, str]]:
    """解析 Model create/unchanged，并校验目标 Provider 属于当前租户。"""

    resolved: dict[str, tuple[str, str, str]] = {}
    for row in plan.rows["models"]:
        internal_id = str(row.values["internal_model_id"])
        provider = providers.get(str(row.values["provider_code"]))
        # Provider 映射只由当前 tenant_id 查询及本批新建计划组成，缺失时不能回退到其他租户。
        if provider is None:
            _add_error(plan, "models", row.row_number, "provider_code", "provider.not_found")
            continue
        provider_id, provider_status = provider
        # active Model 不能绑定 disabled Provider，否则导入成功后会立即产生不可路由状态。
        if row.values["status"] == "active" and provider_status != "active":
            _add_error(plan, "models", row.row_number, "provider_code", "model.provider_invalid")
            continue
        existing = connection.execute(
            """
            SELECT m.*, p.provider_code
            FROM models m
            JOIN providers p ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
            WHERE m.tenant_id=? AND m.model_alias=?
            """,
            (tenant_id, internal_id),
        ).fetchone()
        if existing is None:
            row.object_id = str(uuid4())
            row.action = "created"
            row.values["_provider_id"] = provider_id
            resolved[internal_id] = (row.object_id, str(row.values["status"]), provider_status)
            _increment_count(plan, row.collection, row.action)
            continue
        try:
            capabilities = _parse_capabilities(existing["capabilities"])
        # 存量能力 JSON 无法规范解析时不能猜测或覆盖，按该租户业务键冲突返回安全行错误。
        except ApiError:
            _add_error(plan, "models", row.row_number, "capabilities", "config_transfer.import_conflict")
            continue
        existing_values = {
            "internal_model_id": str(existing["model_alias"]),
            "provider_code": str(existing["provider_code"]),
            "provider_model_id": str(existing["provider_model_id"] or existing["upstream_model_name"]),
            "display_name": existing["display_name"],
            "capabilities": capabilities,
            "context_window": int(existing["context_window"]),
            "input_price_per_1k": float(existing["input_price_per_1k"]),
            "output_price_per_1k": float(existing["output_price_per_1k"]),
            "currency": str(existing["currency"]),
            "price_schema_version": existing["price_schema_version"],
            "description": existing["description"],
            "status": str(existing["status"]),
        }
        requested_values = {key: value for key, value in row.values.items() if not key.startswith("_")}
        row.object_id = str(existing["model_id"])
        if existing_values != requested_values:
            _mark_conflict(plan, row, "internal_model_id")
            continue
        row.action = "unchanged"
        row.values["_provider_id"] = provider_id
        resolved[internal_id] = (row.object_id, str(existing["status"]), provider_status)
        _increment_count(plan, row.collection, row.action)

    # Alias 允许引用目标租户已有、未在 manifest 重复声明的 Model。
    for alias_row in plan.rows["aliases"]:
        references = [str(alias_row.values["default_internal_model_id"])]
        if alias_row.values["fallback_internal_model_id"] is not None:
            references.append(str(alias_row.values["fallback_internal_model_id"]))
        for internal_id in references:
            if internal_id in resolved:
                continue
            existing = connection.execute(
                """
                SELECT m.model_id, m.status AS model_status, p.status AS provider_status
                FROM models m
                JOIN providers p ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
                WHERE m.tenant_id=? AND m.model_alias=?
                """,
                (tenant_id, internal_id),
            ).fetchone()
            if existing is not None:
                resolved[internal_id] = (
                    str(existing["model_id"]),
                    str(existing["model_status"]),
                    str(existing["provider_status"]),
                )
    return resolved


def _alias_values_from_info(info: ModelAliasInfo) -> dict[str, object]:
    """把现有 Alias DTO 投影为 ConfigExport v1 的可比较字段。"""

    policy = info.routing_policy
    if policy is None or policy.status != "enabled":
        # v1 清单没有 RoutingPolicy status 字段；停用策略无法无损表达，禁止被投影成启用策略。
        raise ApiError("config_transfer.import_unavailable")
    # v1 迁移实际生效的 Fallback 图。停用策略保留的 dormant 模型引用不参与运行时
    # 路由，也没有独立 schema 字段可表达，因此按“无 fallback”投影，避免目标端误启用。
    fallback_enabled = (
        info.fallback_policy is not None
        and info.fallback_policy.status == "enabled"
        and info.fallback_internal_model_id is not None
    )
    fallback_conditions: list[str] = []
    if fallback_enabled and info.fallback_policy is not None:
        fallback_conditions = list(info.fallback_policy.trigger_conditions)
    return {
        "alias": info.alias,
        "environment": info.environment,
        "default_internal_model_id": info.default_internal_model_id,
        "fallback_internal_model_id": (
            info.fallback_internal_model_id if fallback_enabled else None
        ),
        "fallback_conditions": fallback_conditions,
        "routing_mode": policy.routing_mode,
        "quality_cost_config": policy.quality_cost_config.model_dump(),
        "description": info.description,
        "status": info.status,
    }


def _prepare_alias_actions(
    connection: sqlite3.Connection,
    tenant_id: str,
    plan: _ImportPlan,
    models: dict[str, tuple[str, str, str]],
) -> None:
    """在目标租户快照中解析 Alias 引用并决定 create-only 动作。

    参数：数据库连接、认证上下文派生的 ``tenant_id``、规范计划及同租户 Model 映射。
    返回：无返回值，原地补齐目标对象 ID、动作和安全计数。
    约束：默认与回退 Model、Environment、RoutingPolicy 都必须属于同一租户；已有业务键
    仅在完整运行语义相等时标记 ``unchanged``，绝不覆盖目标配置。
    """

    for row in plan.rows["aliases"]:
        environment = _resolve_environment(connection, tenant_id, plan, row)
        if environment is None:
            continue
        environment_id, _ = environment
        default_ref = models.get(str(row.values["default_internal_model_id"]))
        fallback_ref = None
        # Fallback 也只能从当前租户模型映射解析，来源数据库 UUID 永远不参与目标绑定。
        if row.values["fallback_internal_model_id"] is not None:
            fallback_ref = models.get(str(row.values["fallback_internal_model_id"]))
        if default_ref is None:
            _add_error(plan, "aliases", row.row_number, "default_internal_model_id", "model_alias.model_invalid")
            continue
        # 清单显式声明 fallback 时必须解析成功，不能静默降级为无回退路由。
        if row.values["fallback_internal_model_id"] is not None and fallback_ref is None:
            _add_error(plan, "aliases", row.row_number, "fallback_internal_model_id", "model_alias.fallback_model_not_found")
            continue
        if row.values["status"] == "active":
            if default_ref[1] != "active" or default_ref[2] != "active":
                _add_error(plan, "aliases", row.row_number, "default_internal_model_id", "model_alias.model_invalid")
                continue
            if fallback_ref is not None and (fallback_ref[1] != "active" or fallback_ref[2] != "active"):
                _add_error(plan, "aliases", row.row_number, "fallback_internal_model_id", "model_alias.fallback_model_disabled")
                continue
        existing = connection.execute(
            """
            SELECT model_alias_id
            FROM model_aliases
            WHERE tenant_id=? AND environment_id=? AND alias=?
            """,
            (tenant_id, environment_id, row.values["alias"]),
        ).fetchone()
        row.values["_environment_id"] = environment_id
        row.values["_default_model_id"] = default_ref[0]
        row.values["_fallback_model_id"] = fallback_ref[0] if fallback_ref is not None else None
        if existing is None:
            row.object_id = str(uuid4())
            row.action = "created"
            _increment_count(plan, row.collection, row.action)
            continue
        row.object_id = str(existing["model_alias_id"])
        try:
            info = _load_model_alias(connection, tenant_id, row.object_id)
            if info.routing_policy is None or info.routing_policy.status != "enabled":
                # 已有停用策略不能被 create-only 导入静默视为 unchanged，否则目标运行语义会漂移。
                _mark_conflict(plan, row, "alias")
                continue
            existing_values = _alias_values_from_info(info)
        # 聚合读取失败表示目标租户现状无法可靠比较；统一折叠，避免泄露内部路由错误或 SQL。
        except (ApiError, sqlite3.Error) as exc:
            raise ApiError("config_transfer.import_unavailable") from exc
        requested_values = {key: value for key, value in row.values.items() if not key.startswith("_")}
        if existing_values != requested_values:
            _mark_conflict(plan, row, "alias")
            continue
        row.action = "unchanged"
        _increment_count(plan, row.collection, row.action)


def _resolve_project_application(
    connection: sqlite3.Connection,
    tenant_id: str,
    plan: _ImportPlan,
    row: _PlanRow,
) -> tuple[str | None, str | None] | None:
    """按当前租户业务 code 解析可选 Project/Application，并复核 owner 关系。"""

    project_code = row.values["project_code"]
    application_code = row.values["application_code"]
    if project_code is None:
        return None, None
    project = connection.execute(
        "SELECT project_id, status FROM projects WHERE tenant_id=? AND project_code=?",
        (tenant_id, project_code),
    ).fetchone()
    if project is None or project["status"] != "active":
        _add_error(plan, "virtual_keys", row.row_number, "project_code", "project.not_found")
        return None
    project_id = str(project["project_id"])
    if application_code is None:
        return project_id, None
    application = connection.execute(
        """
        SELECT application_id, project_id, status
        FROM applications
        WHERE tenant_id=? AND application_code=?
        """,
        (tenant_id, application_code),
    ).fetchone()
    if (
        application is None
        or application["status"] != "active"
        or application["project_id"] != project_id
    ):
        _add_error(plan, "virtual_keys", row.row_number, "application_code", "application.project_mismatch")
        return None
    return project_id, str(application["application_id"])


def _virtual_key_existing_values(row: sqlite3.Row) -> dict[str, object]:
    """把 Virtual Key JOIN 行投影为不含 hash/prefix 的可比较字段。"""

    return {
        "name": str(row["name"]),
        "environment": str(row["environment_code"]),
        "description": row["description"],
        "project_code": row["project_code"],
        "application_code": row["application_code"],
        "status": str(row["status"]),
        "rpm_limit": row["rpm_limit"],
        "tpm_limit": row["tpm_limit"],
        "concurrency_limit": row["concurrency_limit"],
        "rate_limit_status": row["rate_limit_status"],
    }


def _prepare_virtual_key_actions(
    connection: sqlite3.Connection,
    tenant_id: str,
    plan: _ImportPlan,
) -> None:
    """在目标租户中解析 Virtual Key 四元身份并决定 create-only 动作。

    参数：数据库连接、认证上下文派生的 ``tenant_id`` 与已规范化导入计划。
    返回：无返回值，原地写入目标绑定 ID、动作和生成/停用计数。
    约束：只比较名称、环境、项目、应用及限流元数据，不读取或复用旧 key hash/prefix；
    多条候选属于历史歧义，必须失败而不能任意选择一条。
    """

    for row in plan.rows["virtual_keys"]:
        environment = _resolve_environment(connection, tenant_id, plan, row)
        if environment is None:
            continue
        environment_id, environment_code = environment
        binding = _resolve_project_application(connection, tenant_id, plan, row)
        if binding is None:
            continue
        project_id, application_id = binding
        candidates = connection.execute(
            """
            SELECT vk.virtual_key_id, vk.name, vk.description, vk.status,
                   env.code AS environment_code,
                   project.project_code, application.application_code,
                   limits.rpm_limit, limits.tpm_limit, limits.concurrency_limit,
                   limits.status AS rate_limit_status
            FROM virtual_keys vk
            JOIN environments env
              ON env.tenant_id=vk.tenant_id AND env.environment_id=vk.environment_id
            LEFT JOIN projects project
              ON project.tenant_id=vk.tenant_id AND project.project_id=vk.project_id
            LEFT JOIN applications application
              ON application.tenant_id=vk.tenant_id AND application.application_id=vk.application_id
            LEFT JOIN virtual_key_rate_limit_rules limits
              ON limits.tenant_id=vk.tenant_id AND limits.virtual_key_id=vk.virtual_key_id
            WHERE vk.tenant_id=? AND vk.environment_id=? AND vk.name=?
              AND ((vk.project_id IS NULL AND ? IS NULL) OR vk.project_id=?)
              AND ((vk.application_id IS NULL AND ? IS NULL) OR vk.application_id=?)
            ORDER BY vk.created_at ASC, vk.virtual_key_id ASC
            """,
            (
                tenant_id,
                environment_id,
                row.values["name"],
                project_id,
                project_id,
                application_id,
                application_id,
            ),
        ).fetchall()
        row.values["_environment_id"] = environment_id
        row.values["_project_id"] = project_id
        row.values["_application_id"] = application_id
        if not candidates:
            row.object_id = str(uuid4())
            row.action = "created"
            _increment_count(plan, row.collection, row.action)
            if row.values["status"] == "disabled":
                plan.counts.disabled_virtual_keys += 1
            else:
                plan.counts.generated_virtual_keys += 1
            continue
        if len(candidates) > 1:
            _add_error(plan, "virtual_keys", row.row_number, "name", "config_transfer.import_ambiguous")
            continue
        existing = candidates[0]
        row.object_id = str(existing["virtual_key_id"])
        requested_values = {key: value for key, value in row.values.items() if not key.startswith("_")}
        if _virtual_key_existing_values(existing) != requested_values:
            _mark_conflict(plan, row, "name")
            continue
        row.action = "unchanged"
        _increment_count(plan, row.collection, row.action)


def _prepare_import_plan(
    connection: sqlite3.Connection,
    tenant_id: str,
    document: dict[str, Any],
) -> _ImportPlan:
    """在单个数据库快照内构建完整 create-only 计划。"""

    plan = _normalize_import_document(document)
    # 结构或字段错误已经形成安全报告；继续查询数据库既无意义，也可能产生基于目标状态的侧信道。
    if plan.errors:
        return plan
    providers = _prepare_provider_actions(connection, tenant_id, plan)
    models = _prepare_model_actions(connection, tenant_id, plan, providers)
    _prepare_alias_actions(connection, tenant_id, plan, models)
    _prepare_virtual_key_actions(connection, tenant_id, plan)
    return plan


def _report(
    plan: _ImportPlan,
    *,
    status: str,
    applied: bool,
    request_id: str,
    trace_id: str,
    config_import_id: str | None = None,
) -> ImportReport:
    """把内部计划转换为有界、稳定排序且不含秘密的 ImportReport。"""

    object_order = {
        "document": 0,
        "provider": 1,
        "model": 2,
        "alias": 3,
        "virtual_key": 4,
    }
    errors = sorted(
        plan.errors,
        key=lambda item: (
            object_order.get(item.object_type, len(object_order)),
            item.row,
            item.field,
            item.code,
        ),
    )
    return ImportReport(
        status=status,
        valid=not errors,
        applied=applied,
        manifest_sha256=plan.manifest_sha256,
        schema_version=plan.schema_version,
        counts=plan.counts,
        error_count=len(errors),
        errors_truncated=len(errors) > MAX_RETURNED_ERRORS,
        errors=errors[:MAX_RETURNED_ERRORS],
        config_import_id=config_import_id,
        request_id=request_id,
        trace_id=trace_id,
    )


def validate_import(
    config: AppConfig,
    context: AuthContext,
    document: dict[str, Any],
    *,
    request_id: str,
    trace_id: str,
) -> ConfigImportResponse:
    """在一致性只读快照内执行完整导入预检，不生成 Key 或写业务事实。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        plan = _prepare_import_plan(connection, context.user.tenant_id, document)
        connection.rollback()
    # 已规范化的领域错误属于公开合同，回滚只读快照后保持原错误码，不重复转换。
    except ApiError:
        connection.rollback()
        raise
    # 数据库、类型或编码异常可能包含存量值与路径；先回滚，再统一折叠为稳定不可用错误。
    except (sqlite3.Error, TypeError, ValueError, OverflowError, UnicodeError) as exc:
        connection.rollback()
        raise ApiError("config_transfer.import_unavailable") from exc
    finally:
        connection.close()
    report = _report(
        plan,
        status="validated" if not plan.errors else "invalid",
        applied=False,
        request_id=request_id,
        trace_id=trace_id,
    )
    return ConfigImportResponse(
        report=report,
        generated_virtual_keys=[],
        request_id=request_id,
        trace_id=trace_id,
    )


def _export_capabilities(raw: object) -> dict[str, bool]:
    """严格解析持久模型能力对象，拒绝重复键、非布尔值和损坏 JSON。"""

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        """在历史 JSON 出现重复键时失败关闭，避免导出时静默选择后一个值。"""

        result: dict[str, object] = {}
        for key, value in pairs:
            # 重复键没有唯一 canonical 语义，不能让 JSON 解码器静默采用后值后再参与导出哈希。
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        parsed = json.loads(str(raw), object_pairs_hook=reject_duplicate_keys)
        return _normalize_capabilities(parsed)
    # 持久脏值属于当前租户配置状态问题；响应不能携带原 JSON 或解码器上下文。
    except (ApiError, TypeError, ValueError) as exc:
        raise ApiError("config_transfer.export_state_invalid") from exc


def _load_export_providers(
    connection: sqlite3.Connection,
    tenant_id: str,
) -> list[dict[str, object]]:
    """按 provider_code 投影当前租户 Provider 白名单字段。"""

    local_endpoint = connection.execute(
        "SELECT 1 FROM providers WHERE tenant_id=? AND provider_type='local_endpoint' LIMIT 1",
        (tenant_id,),
    ).fetchone()
    if local_endpoint is not None:
        raise ApiError("provider.local_endpoint_requires_local_api")

    rows = connection.execute(
        """
        SELECT provider_code,name,provider_type,base_url,auth_type,
               description,maintenance_note,status
        FROM providers
        WHERE tenant_id=?
        ORDER BY provider_code
        """,
        (tenant_id,),
    ).fetchall()
    return [
        {
            "provider_code": row["provider_code"],
            "name": row["name"],
            "provider_type": row["provider_type"],
            "base_url": row["base_url"],
            "auth_type": row["auth_type"],
            "description": row["description"],
            "maintenance_note": row["maintenance_note"],
            "status": row["status"],
        }
        for row in rows
    ]


def _load_export_models(
    connection: sqlite3.Connection,
    tenant_id: str,
) -> list[dict[str, object]]:
    """投影 Model 及其同租户 Provider 业务 code，不暴露数据库 UUID。"""

    rows = connection.execute(
        """
        SELECT m.model_alias AS internal_model_id,p.provider_code,m.provider_model_id,
               m.display_name,m.capabilities,m.context_window,m.input_price_per_1k,
               m.output_price_per_1k,m.currency,m.price_schema_version,m.description,m.status
        FROM models m
        JOIN providers p ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
        WHERE m.tenant_id=?
        ORDER BY m.model_alias
        """,
        (tenant_id,),
    ).fetchall()
    model_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM models WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()[0]
    )
    if len(rows) != model_count:
        # 缺失 owner Provider 的历史 Model 不可迁移，不能把孤立引用导出成部分配置。
        raise ApiError("config_transfer.export_state_invalid")
    return [
        {
            "internal_model_id": row["internal_model_id"],
            "provider_code": row["provider_code"],
            "provider_model_id": row["provider_model_id"],
            "display_name": row["display_name"],
            "capabilities": _export_capabilities(row["capabilities"]),
            "context_window": row["context_window"],
            "input_price_per_1k": row["input_price_per_1k"],
            "output_price_per_1k": row["output_price_per_1k"],
            "currency": row["currency"],
            "price_schema_version": row["price_schema_version"],
            "description": row["description"],
            "status": row["status"],
        }
        for row in rows
    ]


def _load_export_aliases(
    connection: sqlite3.Connection,
    tenant_id: str,
) -> list[dict[str, object]]:
    """通过现有 Alias 聚合器导出完整有效路由、Fallback 和质量成本配置。"""

    references = connection.execute(
        """
        SELECT a.model_alias_id,a.environment AS legacy_environment,e.code AS environment_code
        FROM model_aliases a
        JOIN environments e
          ON e.tenant_id=a.tenant_id
         AND (e.environment_id=a.environment_id OR (a.environment_id IS NULL AND e.code=a.environment))
        WHERE a.tenant_id=?
        ORDER BY e.code,a.alias
        """,
        (tenant_id,),
    ).fetchall()
    alias_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM model_aliases WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()[0]
    )
    # JOIN 投影数必须与当前租户 Alias 事实数一致；缺失 owner Environment 不能被部分导出掩盖。
    if len(references) != alias_count:
        raise ApiError("config_transfer.export_state_invalid")
    projected: list[dict[str, object]] = []
    for reference in references:
        try:
            info = _load_model_alias(connection, tenant_id, str(reference["model_alias_id"]))
            values = _alias_values_from_info(info)
        # 聚合路由的任一损坏都意味着 v1 无法无损表示；统一关闭，且不暴露内部对象或 SQL。
        except (ApiError, sqlite3.Error, TypeError, ValueError) as exc:
            raise ApiError("config_transfer.export_state_invalid") from exc
        # environment_id 是当前权威绑定；legacy 文本不一致表示存量状态无法无损迁移。
        if str(reference["legacy_environment"]) != str(reference["environment_code"]):
            raise ApiError("config_transfer.export_state_invalid")
        values["environment"] = str(reference["environment_code"])
        projected.append(values)
    return projected


def _load_export_virtual_keys(
    connection: sqlite3.Connection,
    tenant_id: str,
) -> list[dict[str, object]]:
    """导出 Virtual Key 元数据和限流，不读取 key_prefix、key_hash 或旧明文。"""

    rows = connection.execute(
        """
        SELECT vk.name,vk.environment AS legacy_environment,e.code AS environment_code,
               vk.description,vk.status,vk.project_id,vk.application_id,
               p.project_code,a.application_code,a.project_id AS application_project_id,
               r.rpm_limit,r.tpm_limit,r.concurrency_limit,r.status AS rate_limit_status
        FROM virtual_keys vk
        JOIN environments e
          ON e.tenant_id=vk.tenant_id
         AND (e.environment_id=vk.environment_id OR (vk.environment_id IS NULL AND e.code=vk.environment))
        LEFT JOIN projects p
          ON p.tenant_id=vk.tenant_id AND p.project_id=vk.project_id
        LEFT JOIN applications a
          ON a.tenant_id=vk.tenant_id AND a.application_id=vk.application_id
        LEFT JOIN virtual_key_rate_limit_rules r
          ON r.tenant_id=vk.tenant_id AND r.virtual_key_id=vk.virtual_key_id
        WHERE vk.tenant_id=?
        ORDER BY e.code,vk.name,COALESCE(p.project_code,''),COALESCE(a.application_code,'')
        """,
        (tenant_id,),
    ).fetchall()
    key_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM virtual_keys WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()[0]
    )
    if len(rows) != key_count:
        # 缺失环境、owner 关系或重复限流行都会改变投影基数，必须整体失败关闭。
        raise ApiError("config_transfer.export_state_invalid")
    projected: list[dict[str, object]] = []
    for row in rows:
        # environment_id 是权威租户绑定；旧文本漂移时不能猜测应导出哪一个环境。
        if str(row["legacy_environment"]) != str(row["environment_code"]):
            raise ApiError("config_transfer.export_state_invalid")
        # 非空 Project 外键必须能在同租户解析业务 code，否则目标端无法重建 owner 关系。
        if row["project_id"] is not None and row["project_code"] is None:
            raise ApiError("config_transfer.export_state_invalid")
        # Application 必须与同租户 Project 形成完整 owner 链；孤立或错绑引用禁止部分导出。
        if row["application_id"] is not None and (
            row["application_code"] is None
            or row["project_id"] is None
            or row["application_project_id"] != row["project_id"]
        ):
            # 该引用无法按同租户业务键无损重建，失败响应仅保留稳定状态错误码。
            raise ApiError("config_transfer.export_state_invalid")
        projected.append(
            {
                "name": row["name"],
                "environment": row["environment_code"],
                "description": row["description"],
                "project_code": row["project_code"],
                "application_code": row["application_code"],
                "status": row["status"],
                "rpm_limit": row["rpm_limit"],
                "tpm_limit": row["tpm_limit"],
                "concurrency_limit": row["concurrency_limit"],
                "rate_limit_status": row["rate_limit_status"],
            }
        )
    return projected


def _build_export_document(
    connection: sqlite3.Connection,
    tenant_id: str,
    exported_at: datetime,
) -> dict[str, Any]:
    """在一个只读快照中构造仅含稳定业务字段的 ConfigExport 文档。"""

    return {
        "schema_version": SCHEMA_VERSION,
        "source_tenant_id": tenant_id,
        "exported_at": _to_db_time(exported_at),
        "providers": _load_export_providers(connection, tenant_id),
        "models": _load_export_models(connection, tenant_id),
        "aliases": _load_export_aliases(connection, tenant_id),
        "virtual_keys": _load_export_virtual_keys(connection, tenant_id),
    }


def _normalized_export_from_plan(
    plan: _ImportPlan,
    *,
    exported_at: datetime,
) -> ConfigExport:
    """把已在源租户复核为 unchanged 的计划转成无内部 UUID 的公开 DTO。"""

    collections = {
        collection: [
            {key: value for key, value in row.values.items() if not key.startswith("_")}
            for row in plan.rows[collection]
        ]
        for collection in COLLECTIONS
    }
    config_export = ConfigExport(
        schema_version=plan.schema_version,
        source_tenant_id=plan.source_tenant_id,
        exported_at=exported_at,
        providers=collections["providers"],
        models=collections["models"],
        aliases=collections["aliases"],
        virtual_keys=collections["virtual_keys"],
    )
    portable_document = config_export.model_dump(mode="json")
    try:
        portable_bytes = _canonical_json_bytes(portable_document)
    # 导出 DTO 即使构造成功也必须可编码为导入端接受的 UTF-8 JSON，损坏文本整体失败关闭。
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ApiError("config_transfer.export_state_invalid") from exc
    if (
        len(portable_bytes) > MAX_CONFIG_BODY_BYTES
        or config_json_structure_error(portable_document) is not None
    ):
        # 在记录导出事实前复用导入资源门禁，禁止产生无法 round-trip 的“成功”文件。
        raise ApiError("config_transfer.export_state_invalid")
    return config_export


def _export_audit_summary(
    config_export: ConfigExport,
    manifest_sha256: str,
) -> dict[str, object]:
    """生成与 config_exports 逐字段可复核的安全审计摘要。"""

    return {
        "schema_version": config_export.schema_version,
        "manifest_sha256": manifest_sha256,
        "providers_count": len(config_export.providers),
        "models_count": len(config_export.models),
        "aliases_count": len(config_export.aliases),
        "virtual_keys_count": len(config_export.virtual_keys),
    }


def export_config(
    config: AppConfig,
    context: AuthContext,
    *,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> ConfigExportResponse:
    """导出当前租户一致配置快照，并原子记录不可变导出事实与审计。"""

    tenant_id = context.user.tenant_id
    exported_at = _utcnow().replace(microsecond=0)
    connection = _connect(config, error_code="config_transfer.export_unavailable")
    audit_record: AuditRecord | None = None
    try:
        connection.execute("BEGIN")
        raw_document = _build_export_document(connection, tenant_id, exported_at)
        plan = _prepare_import_plan(connection, tenant_id, raw_document)
        # 源租户自身投影必须全部可按业务键精确回读为 unchanged；任何偏差都说明
        # 存量状态无法由 v1 无损表达，不能生成看似成功的部分迁移文件。
        if plan.errors or any(
            row.action != "unchanged"
            for collection in COLLECTIONS
            for row in plan.rows[collection]
        ):
            # 源租户回读不是全量 unchanged 时，文件无法保证 round-trip，必须在记录导出事实前失败。
            raise ApiError("config_transfer.export_state_invalid")
        config_export = _normalized_export_from_plan(plan, exported_at=exported_at)
        connection.rollback()

        config_export_id = str(uuid4())
        now = _to_db_time(exported_at)
        summary = _export_audit_summary(config_export, plan.manifest_sha256)
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """
            SELECT manifest_sha256 FROM config_exports
            WHERE tenant_id=? AND actor_user_id=? AND request_id=?
            """,
            (tenant_id, context.user.user_id, request_id),
        ).fetchone()
        if existing is not None:
            # 导出正文未持久化，服务端无法安全重放旧文件；request_id 复用一律显式冲突。
            raise ApiError("config_transfer.export_request_conflict")
        connection.execute(
            """
            INSERT INTO config_exports(
              config_export_id,tenant_id,actor_user_id,schema_version,manifest_sha256,
              providers_count,models_count,aliases_count,virtual_keys_count,
              request_id,trace_id,created_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                config_export_id,
                tenant_id,
                context.user.user_id,
                config_export.schema_version,
                plan.manifest_sha256,
                summary["providers_count"],
                summary["models_count"],
                summary["aliases_count"],
                summary["virtual_keys_count"],
                request_id,
                trace_id,
                now,
            ),
        )
        audit_record = record_audit_log(
            connection,
            context,
            action="config.exported",
            object_type="config_transfer",
            object_id=config_export_id,
            object_name=None,
            before_summary={},
            after_summary=summary,
            request_meta=audit_request_meta,
            request_id=request_id,
            trace_id=trace_id,
            created_at=exported_at,
        )
        commit_audited_transaction(connection)
    # 领域错误需要先回滚当前快照或写事务，再按导出 API 的公开错误空间做精确转换。
    except ApiError as exc:
        connection.rollback()
        if exc.code == "config_transfer.body_too_large":
            # 源租户状态超出可移植清单边界时属于导出状态无效，不能对导出调用返回导入错误码。
            raise ApiError("config_transfer.export_state_invalid") from exc
        if exc.code in {
            "config_transfer.export_state_invalid",
            "config_transfer.export_request_conflict",
            "provider.local_endpoint_requires_local_api",
        }:
            raise
        # 其他内部领域错误不能暴露模块实现细节，统一成为可重试的导出不可用。
        raise ApiError("config_transfer.export_unavailable") from exc
    # SQL、历史类型、数值和编码异常可能携带路径或配置原值，回滚后仅保留稳定错误码。
    except (sqlite3.Error, TypeError, ValueError, OverflowError, UnicodeError) as exc:
        connection.rollback()
        raise ApiError("config_transfer.export_unavailable") from exc
    finally:
        connection.close()

    dispatch_audit_recorded(
        config,
        context,
        audit_record,
        request_id=request_id,
        trace_id=trace_id,
    )
    logger.info(
        "config_transfer.exported",
        extra={
            "event": "config_transfer.exported",
            "tenant_id": tenant_id,
            "actor_id": context.user.user_id,
            "object_id": config_export_id,
            "request_id": request_id,
            "trace_id": trace_id,
            "counts": {
                "providers": len(config_export.providers),
                "models": len(config_export.models),
                "aliases": len(config_export.aliases),
                "virtual_keys": len(config_export.virtual_keys),
            },
        },
    )
    return ConfigExportResponse(
        config_export=config_export,
        config_export_id=config_export_id,
        manifest_sha256=plan.manifest_sha256,
        request_id=request_id,
        trace_id=trace_id,
    )


def _insert_provider(
    connection: sqlite3.Connection,
    context: AuthContext,
    row: _PlanRow,
    now: str,
) -> None:
    """写入一条已验证 Provider；tenant 和 actor 只能来自 AuthContext。"""

    status = str(row.values["status"])
    connection.execute(
        """
        INSERT INTO providers(
          provider_id, tenant_id, provider_code, name, base_url, status, provider_type,
          auth_type, description, maintenance_note, health_status, created_by,
          disabled_at, disabled_by, created_at, updated_at, next_probe_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', ?, ?, ?, ?, ?, ?)
        """,
        (
            row.object_id,
            context.user.tenant_id,
            row.values["provider_code"],
            row.values["name"],
            row.values["base_url"],
            status,
            row.values["provider_type"],
            row.values["auth_type"],
            row.values["description"],
            row.values["maintenance_note"],
            context.user.user_id,
            now if status == "disabled" else None,
            context.user.user_id if status == "disabled" else None,
            now,
            now,
            now if status == "active" else None,
        ),
    )


def _insert_model(
    connection: sqlite3.Connection,
    context: AuthContext,
    row: _PlanRow,
    now: str,
) -> None:
    """在导入事务中写入一条已解析目标 Provider 的 Model。

    参数：事务连接、可信 ``AuthContext``、已解析 Provider ID 的计划行和统一提交时间。
    返回：无返回值，插入失败由外层整批事务回滚。
    约束：tenant_id 与 created_by 只能来自认证上下文，清单不能指定归属；能力对象仅以
    规范 JSON 持久化，避免不同序列化顺序破坏 create-only 比较。
    """

    status = str(row.values["status"])
    connection.execute(
        """
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name,
          provider_model_id, display_name, capabilities, context_window,
          input_price_per_1k, output_price_per_1k, currency, price_schema_version,
          description, status, created_by, disabled_at, disabled_by, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.object_id,
            context.user.tenant_id,
            row.values["_provider_id"],
            row.values["internal_model_id"],
            row.values["provider_model_id"],
            row.values["provider_model_id"],
            row.values["display_name"],
            json.dumps(row.values["capabilities"], ensure_ascii=False, sort_keys=True),
            row.values["context_window"],
            row.values["input_price_per_1k"],
            row.values["output_price_per_1k"],
            row.values["currency"],
            row.values["price_schema_version"],
            row.values["description"],
            status,
            context.user.user_id,
            now if status == "disabled" else None,
            context.user.user_id if status == "disabled" else None,
            now,
            now,
        ),
    )


def _insert_alias(
    connection: sqlite3.Connection,
    context: AuthContext,
    row: _PlanRow,
    plan: _ImportPlan,
    now: str,
    trace_id: str,
) -> list[object]:
    """写入 Alias、完整路由配置、FallbackPolicy 和持久路由事件。"""

    status = str(row.values["status"])
    connection.execute(
        """
        INSERT INTO model_aliases(
          model_alias_id, tenant_id, alias, environment, environment_id,
          default_model_id, fallback_model_id, status, description, created_by,
          disabled_at, disabled_by, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.object_id,
            context.user.tenant_id,
            row.values["alias"],
            row.values["environment"],
            row.values["_environment_id"],
            row.values["_default_model_id"],
            row.values["_fallback_model_id"],
            status,
            row.values["description"],
            context.user.user_id,
            now if status == "disabled" else None,
            context.user.user_id if status == "disabled" else None,
            now,
            now,
        ),
    )
    policy = connection.execute(
        """
        SELECT routing_policy_id,routing_mode,quality_weight_bps,cost_weight_bps,
               latency_weight_bps,quality_floor_bps,minimum_sample_count,status,version
        FROM model_alias_routing_policies
        WHERE tenant_id=? AND model_alias_id=?
        """,
        (context.user.tenant_id, row.object_id),
    ).fetchone()
    if (
        policy is None
        or int(policy["version"]) != 1
        or str(policy["status"]) != "enabled"
    ):
        # INSERT trigger 必须原子创建同租户 v1/enabled 策略；缺失或漂移说明事务事实不完整。
        raise sqlite3.IntegrityError("routing policy trigger missing")
    quality = row.values["quality_cost_config"]
    # 质量配置已在归一化阶段确定为对象，内部类型漂移必须回滚整批，不能写入部分 Alias。
    if not isinstance(quality, dict):
        raise sqlite3.IntegrityError("quality config invalid")
    child_request_prefix = f"cfg_{plan.manifest_sha256[:24]}_{row.row_number:04d}"
    base_fingerprint_values = {
        "manifest_sha256": plan.manifest_sha256,
        "row_number": row.row_number,
        "business_key_sha256": row.business_key_sha256,
    }
    # Alias INSERT trigger 创建的是合法 fixed/enabled v1。先冻结创建事实，再根据清单执行
    # 显式 v2 更新；不能在 v1 上偷改字段，否则会绕过既有版本 trigger 和事件合同。
    initial_info = _load_model_alias(connection, context.user.tenant_id, row.object_id)
    events = [
        _insert_routing_policy_event(
            connection,
            context,
            initial_info,
            request_id=f"{child_request_prefix}_c",
            trace_id=trace_id,
            fingerprint=_mutation_fingerprint(
                "config_import_alias_created",
                base_fingerprint_values,
            ),
            change_kinds=["created"],
            changed_routing_fields=[
                "alias",
                "environment",
                "environment_id",
                "status",
                "default_model_id",
                "fallback_model_id",
                "routing_mode",
                "quality_cost_config",
            ],
            previous_mode=None,
            now=now,
        )
    ]
    initial_quality = {
        "quality_weight_bps": int(policy["quality_weight_bps"]),
        "cost_weight_bps": int(policy["cost_weight_bps"]),
        "latency_weight_bps": int(policy["latency_weight_bps"]),
        "quality_floor_bps": int(policy["quality_floor_bps"]),
        "minimum_sample_count": int(policy["minimum_sample_count"]),
    }
    mode_changed = str(policy["routing_mode"]) != row.values["routing_mode"]
    quality_changed = initial_quality != quality
    if mode_changed or quality_changed:
        cursor = connection.execute(
            """
            UPDATE model_alias_routing_policies
            SET routing_mode=?,quality_weight_bps=?,cost_weight_bps=?,latency_weight_bps=?,
                quality_floor_bps=?,minimum_sample_count=?,version=version+1,
                updated_by=?,updated_at=?
            WHERE tenant_id=? AND routing_policy_id=? AND version=1
            """,
            (
                row.values["routing_mode"],
                quality["quality_weight_bps"],
                quality["cost_weight_bps"],
                quality["latency_weight_bps"],
                quality["quality_floor_bps"],
                quality["minimum_sample_count"],
                context.user.user_id,
                now,
                context.user.tenant_id,
                policy["routing_policy_id"],
            ),
        )
        # version=1 的条件更新必须恰好命中当前租户新建策略，否则说明 trigger 或并发状态异常。
        if cursor.rowcount != 1:
            raise sqlite3.IntegrityError("routing policy initialization conflict")
    # 只有显式解析成功的同租户 fallback Model 才创建策略，空引用保持“无回退”语义。
    if row.values["_fallback_model_id"] is not None:
        _write_fallback_policy(
            connection,
            tenant_id=context.user.tenant_id,
            model_alias_id=row.object_id,
            enabled=True,
            trigger_conditions=list(row.values["fallback_conditions"]),
            actor_id=context.user.user_id,
            now=now,
        )
    if mode_changed or quality_changed:
        final_info = _load_model_alias(connection, context.user.tenant_id, row.object_id)
        changed_fields = []
        change_kinds = []
        if mode_changed:
            change_kinds.append("mode")
            changed_fields.append("routing_mode")
        if quality_changed:
            change_kinds.append("quality_cost")
            changed_fields.append("quality_cost_config")
        events.append(
            _insert_routing_policy_event(
                connection,
                context,
                final_info,
                request_id=f"{child_request_prefix}_u",
                trace_id=trace_id,
                fingerprint=_mutation_fingerprint(
                    "config_import_alias_initialized",
                    base_fingerprint_values,
                ),
                change_kinds=change_kinds,
                changed_routing_fields=changed_fields,
                previous_mode=str(policy["routing_mode"]),
                now=now,
            )
        )
    return events


def _insert_virtual_key(
    connection: sqlite3.Connection,
    context: AuthContext,
    row: _PlanRow,
    now: str,
) -> GeneratedVirtualKey | None:
    """生成全新 Virtual Key，仅持久化 hash/prefix，active 明文只返回一次。"""

    environment = str(row.values["environment"])
    plain_value = ""
    key_hash = ""
    for _attempt in range(5):
        candidate = _generate_virtual_key(environment)
        candidate_hash = hash_secret(candidate)
        # key_hash 是全库唯一认证索引；导入持有 IMMEDIATE 写锁，预检后到 INSERT 之间
        # 不会出现其他写者。碰撞时只丢弃内存候选，任何明文都不进入异常或日志。
        collision = connection.execute(
            "SELECT 1 FROM virtual_keys WHERE key_hash=?",
            (candidate_hash,),
        ).fetchone()
        if collision is None:
            plain_value = candidate
            key_hash = candidate_hash
            break
    # 有限次碰撞后失败会回滚整批；异常只含固定文本，绝不拼接候选 Key 或 hash。
    if not plain_value:
        raise sqlite3.IntegrityError("virtual key generation exhausted")
    prefix = secret_prefix(plain_value)
    status = str(row.values["status"])
    connection.execute(
        """
        INSERT INTO virtual_keys(
          virtual_key_id, tenant_id, name, key_prefix, key_hash, status, environment,
          environment_id, description, project_id, application_id, created_by,
          disabled_at, disabled_by, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.object_id,
            context.user.tenant_id,
            row.values["name"],
            prefix,
            key_hash,
            status,
            environment,
            row.values["_environment_id"],
            row.values["description"],
            row.values["_project_id"],
            row.values["_application_id"],
            context.user.user_id,
            now if status == "disabled" else None,
            context.user.user_id if status == "disabled" else None,
            now,
            now,
        ),
    )
    if row.values["rate_limit_status"] is not None:
        connection.execute(
            """
            INSERT INTO virtual_key_rate_limit_rules(
              rate_limit_rule_id, tenant_id, virtual_key_id, rpm_limit, tpm_limit,
              concurrency_limit, status, created_by, updated_by, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                context.user.tenant_id,
                row.object_id,
                row.values["rpm_limit"],
                row.values["tpm_limit"],
                row.values["concurrency_limit"],
                row.values["rate_limit_status"],
                context.user.user_id,
                context.user.user_id,
                now,
                now,
            ),
        )
    if status != "active":
        # disabled Key 明文没有可用交付语义，立即丢弃；后续必须删除并通过普通创建流程重建。
        return None
    return GeneratedVirtualKey(
        virtual_key_id=row.object_id,
        name=str(row.values["name"]),
        environment=environment,
        key_prefix=prefix,
        plain_value=plain_value,
    )


def _insert_import_row_facts(
    connection: sqlite3.Connection,
    context: AuthContext,
    config_import_id: str,
    plan: _ImportPlan,
) -> None:
    """写入不含业务值的逐行创建/复用事实。"""

    for collection in COLLECTIONS:
        for row in plan.rows[collection]:
            connection.execute(
                """
                INSERT INTO config_import_rows(
                  tenant_id, config_import_id, collection,
                  row_number, object_type, action, object_id, business_key_sha256
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    context.user.tenant_id,
                    config_import_id,
                    collection,
                    row.row_number,
                    OBJECT_TYPES[collection],
                    row.action,
                    row.object_id,
                    row.business_key_sha256,
                ),
            )


def _import_audit_summary(plan: _ImportPlan) -> dict[str, object]:
    """构造 config.imported 审计白名单摘要，不包含配置正文。"""

    return {
        "schema_version": plan.schema_version,
        "manifest_sha256": plan.manifest_sha256,
        "providers_created_count": plan.counts.providers_created,
        "providers_unchanged_count": plan.counts.providers_unchanged,
        "models_created_count": plan.counts.models_created,
        "models_unchanged_count": plan.counts.models_unchanged,
        "aliases_created_count": plan.counts.aliases_created,
        "aliases_unchanged_count": plan.counts.aliases_unchanged,
        "virtual_keys_created_count": plan.counts.virtual_keys_created,
        "virtual_keys_unchanged_count": plan.counts.virtual_keys_unchanged,
        "generated_virtual_key_count": plan.counts.generated_virtual_keys,
        "disabled_virtual_key_count": plan.counts.disabled_virtual_keys,
    }


def _load_replayed_import(
    connection: sqlite3.Connection,
    context: AuthContext,
    request_id: str,
    manifest_sha256: str,
    trace_id: str,
) -> _ReplayedImport | None:
    """恢复相同 actor/request/hash 的安全报告，异意图复用返回 409。"""

    row = connection.execute(
        """
        SELECT * FROM config_imports
        WHERE tenant_id=? AND actor_user_id=? AND request_id=?
        """,
        (context.user.tenant_id, context.user.user_id, request_id),
    ).fetchone()
    if row is None:
        return None
    # 同 actor/request_id 只能重放同一 manifest；不同摘要代表不同意图，禁止返回旧成功事实。
    if row["manifest_sha256"] != manifest_sha256:
        raise ApiError("config_transfer.import_request_conflict")
    counts = ImportCounts(
        providers_created=int(row["providers_created_count"]),
        providers_unchanged=int(row["providers_unchanged_count"]),
        models_created=int(row["models_created_count"]),
        models_unchanged=int(row["models_unchanged_count"]),
        aliases_created=int(row["aliases_created_count"]),
        aliases_unchanged=int(row["aliases_unchanged_count"]),
        virtual_keys_created=int(row["virtual_keys_created_count"]),
        virtual_keys_unchanged=int(row["virtual_keys_unchanged_count"]),
        generated_virtual_keys=int(row["generated_virtual_key_count"]),
        disabled_virtual_keys=int(row["disabled_virtual_key_count"]),
    )
    return _ReplayedImport(
        report=ImportReport(
            status="replayed",
            valid=True,
            applied=True,
            manifest_sha256=manifest_sha256,
            schema_version=str(row["schema_version"]),
            counts=counts,
            error_count=0,
            errors_truncated=False,
            errors=[],
            config_import_id=str(row["config_import_id"]),
            request_id=request_id,
            # HTTP 报告属于本次重试调用，必须携带当前中间件 trace；原 trace 单独保留给 Webhook 补投。
            trace_id=trace_id,
        ),
        event_id=str(row["event_id"]),
        occurred_at=_from_db_time(row["created_at"]),
        source_trace_id=str(row["trace_id"]),
    )


def apply_import(
    config: AppConfig,
    context: AuthContext,
    document: dict[str, Any],
    *,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> ConfigImportResponse:
    """完整重检并原子提交 create-only 配置导入。"""

    try:
        normalized = _normalize_import_document(document)
    # 内部调用也必须执行与路由一致的失败关闭；异常正文不得越过统一导入错误合同。
    except (TypeError, ValueError, OverflowError, UnicodeError) as exc:
        raise ApiError("config_transfer.import_invalid") from exc
    if normalized.errors:
        report = _report(
            normalized,
            status="invalid",
            applied=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        return ConfigImportResponse(
            report=report,
            generated_virtual_keys=[],
            request_id=request_id,
            trace_id=trace_id,
        )

    connection = _connect(config)
    audit_record: AuditRecord | None = None
    route_events: list[object] = []
    generated_keys: list[GeneratedVirtualKey] = []
    config_import_id = str(uuid4())
    event_id = f"evt_{config_import_id}"
    now_datetime = _utcnow().replace(microsecond=0)
    now = _to_db_time(now_datetime)
    try:
        connection.execute("BEGIN IMMEDIATE")
        replayed = _load_replayed_import(
            connection,
            context,
            request_id,
            normalized.manifest_sha256,
            trace_id,
        )
        if replayed is not None:
            connection.rollback()
            replay_plan = _ImportPlan(
                schema_version=replayed.report.schema_version,
                manifest_sha256=replayed.report.manifest_sha256,
                counts=replayed.report.counts,
            )
            # 事务可能已提交但进程在建立 delivery 前退出；精确重试使用原 event_id、
            # trace 和提交时间再次投递，Webhook 的持久唯一约束负责幂等去重。
            _dispatch_config_imported(
                config,
                context,
                config_import_id=str(replayed.report.config_import_id),
                event_id=replayed.event_id,
                plan=replay_plan,
                occurred_at=replayed.occurred_at,
                request_id=request_id,
                trace_id=replayed.source_trace_id,
            )
            return ConfigImportResponse(
                report=replayed.report,
                generated_virtual_keys=[],
                request_id=request_id,
                trace_id=trace_id,
            )
        plan = _prepare_import_plan(connection, context.user.tenant_id, document)
        # 写锁内必须重做完整计划，关闭 validate/apply 间的 TOCTOU；错误时回滚且不生成任何 Key。
        if plan.errors:
            connection.rollback()
            report = _report(
                plan,
                status="invalid",
                applied=False,
                request_id=request_id,
                trace_id=trace_id,
            )
            return ConfigImportResponse(
                report=report,
                generated_virtual_keys=[],
                request_id=request_id,
                trace_id=trace_id,
            )
        for row in plan.rows["providers"]:
            if row.action == "created":
                _insert_provider(connection, context, row, now)
        for row in plan.rows["models"]:
            if row.action == "created":
                _insert_model(connection, context, row, now)
        for row in plan.rows["aliases"]:
            if row.action == "created":
                route_events.extend(_insert_alias(connection, context, row, plan, now, trace_id))
        for row in plan.rows["virtual_keys"]:
            if row.action == "created":
                generated = _insert_virtual_key(connection, context, row, now)
                if generated is not None:
                    generated_keys.append(generated)
        connection.execute(
            """
            INSERT INTO config_imports(
              config_import_id, tenant_id, actor_user_id, manifest_sha256, schema_version,
              providers_created_count, providers_unchanged_count,
              models_created_count, models_unchanged_count,
              aliases_created_count, aliases_unchanged_count,
              virtual_keys_created_count, virtual_keys_unchanged_count,
              generated_virtual_key_count, disabled_virtual_key_count,
              request_id, trace_id, event_id, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                config_import_id,
                context.user.tenant_id,
                context.user.user_id,
                plan.manifest_sha256,
                plan.schema_version,
                plan.counts.providers_created,
                plan.counts.providers_unchanged,
                plan.counts.models_created,
                plan.counts.models_unchanged,
                plan.counts.aliases_created,
                plan.counts.aliases_unchanged,
                plan.counts.virtual_keys_created,
                plan.counts.virtual_keys_unchanged,
                plan.counts.generated_virtual_keys,
                plan.counts.disabled_virtual_keys,
                request_id,
                trace_id,
                event_id,
                now,
            ),
        )
        _insert_import_row_facts(connection, context, config_import_id, plan)
        audit_record = record_audit_log(
            connection,
            context,
            action="config.imported",
            object_type="config_transfer",
            object_id=config_import_id,
            # 批次名称没有检索价值，且不得成为文件名或配置名称的审计旁路副本。
            object_name=None,
            before_summary={},
            after_summary=_import_audit_summary(plan),
            request_meta=audit_request_meta,
            request_id=request_id,
            trace_id=trace_id,
            created_at=now_datetime,
        )
        commit_audited_transaction(connection)
    # 所有领域异常先回滚四类配置、事实和审计，再决定保留公开冲突码或折叠内部错误。
    except ApiError as exc:
        connection.rollback()
        if exc.code in {
            "config_transfer.import_request_conflict",
            "config_transfer.import_unavailable",
        }:
            raise
        # Fallback、RoutingPolicy、Audit 等内部领域错误都属于一个导入事务失败，
        # 不得把私有子模块错误码暴露成可由客户依赖的 transfer 合同。
        raise ApiError("config_transfer.import_unavailable") from exc
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        # 并发唯一冲突、owner trigger 或事实不一致统一折叠，禁止泄露 SQL 和业务值。
        raise ApiError("config_transfer.import_unavailable") from exc
    except (sqlite3.Error, TypeError, ValueError, OverflowError, UnicodeError) as exc:
        connection.rollback()
        # 存量脏值、转换溢出和事务错误统一为导入不可用，不泄露数据库字段或原始异常。
        raise ApiError("config_transfer.import_unavailable") from exc
    finally:
        connection.close()

    dispatch_audit_recorded(
        config,
        context,
        audit_record,
        request_id=request_id,
        trace_id=trace_id,
    )
    for route_event in route_events:
        try:
            _dispatch_routing_policy_event(config, route_event)
        except Exception:
            # 事务已经提交且响应含一次性 Key；路由事件副作用失败只能记录安全摘要，绝不能阻断明文交付。
            logger.error(
                "config_transfer.routing_event_dispatch_failed",
                extra={
                    "event": "config_transfer.routing_event_dispatch_failed",
                    "tenant_id": context.user.tenant_id,
                    "actor_id": context.user.user_id,
                    "object_id": config_import_id,
                    "request_id": request_id,
                    "trace_id": trace_id,
                    "error_code": "webhook.delivery_unavailable",
                },
            )
    _dispatch_config_imported(
        config,
        context,
        config_import_id=config_import_id,
        event_id=event_id,
        plan=plan,
        occurred_at=now_datetime,
        request_id=request_id,
        trace_id=trace_id,
    )
    report = _report(
        plan,
        status="applied",
        applied=True,
        request_id=request_id,
        trace_id=trace_id,
        config_import_id=config_import_id,
    )
    _log_imported(context, config_import_id, plan, request_id, trace_id)
    return ConfigImportResponse(
        report=report,
        generated_virtual_keys=generated_keys,
        request_id=request_id,
        trace_id=trace_id,
    )


def _dispatch_config_imported(
    config: AppConfig,
    context: AuthContext,
    *,
    config_import_id: str,
    event_id: str,
    plan: _ImportPlan,
    occurred_at: datetime,
    request_id: str,
    trace_id: str,
) -> None:
    """提交后尽力投递 config.imported；外部失败不能回滚已生效配置。"""

    try:
        event = build_webhook_event(
            tenant_id=context.user.tenant_id,
            event_type="config.imported",
            event_id=event_id,
            timestamp=occurred_at,
            actor_id=context.user.user_id,
            object_id=config_import_id,
            trace_id=trace_id,
            source_request_id=request_id,
            payload={
                "config_import_id": config_import_id,
                "schema_version": plan.schema_version,
                "manifest_sha256": plan.manifest_sha256,
                **plan.counts.model_dump(),
                "applied_at": _to_db_time(occurred_at),
            },
        )
        from metahub_control.webhooks.service import dispatch_webhook_event

        dispatch_webhook_event(
            config,
            context.user.tenant_id,
            event,
            request_id=request_id,
            trace_id=trace_id,
        )
    except Exception:
        logger.error(
            "config_transfer.webhook_dispatch_failed",
            extra={
                "event": "config_transfer.webhook_dispatch_failed",
                "tenant_id": context.user.tenant_id,
                "actor_id": context.user.user_id,
                "object_id": config_import_id,
                "request_id": request_id,
                "trace_id": trace_id,
                "error_code": "webhook.delivery_unavailable",
            },
        )


def _log_imported(
    context: AuthContext,
    config_import_id: str,
    plan: _ImportPlan,
    request_id: str,
    trace_id: str,
) -> None:
    """记录不含正文、名称、URL 或 Key 的导入完成摘要。"""

    logger.info(
        "config_transfer.imported",
        extra={
            "event": "config_transfer.imported",
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": config_import_id,
            "request_id": request_id,
            "trace_id": trace_id,
            "counts": plan.counts.model_dump(),
        },
    )
