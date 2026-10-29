"""PRD-045 策略快照的跨服务 wire、canonical hash 与不可变运行合同。

控制面和数据面必须共同调用本模块，不能各自拼接 JSON 或信任持久化 AST。控制面发布时
重新编译策略表达式；数据面收到 wire 后再次编译 canonical expression，并核对表达式、
变量、condition hash、双 hash 和租户边界。模块不执行网络、数据库或日志操作，因此不会
把 DSL、Prompt、Response、Provider Key 或内部 Token 扩散到异常和结构化日志。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, NoReturn

from .policy_dsl import (
    ALLOWED_ACTIONS,
    ALLOWED_VARIABLES,
    DSL_VERSION,
    MAX_POLICIES_PER_SET,
    CompiledPolicyExpression,
    PolicyActionError,
    PolicyDslError,
    canonical_policy_action,
    compile_policy_expression,
)

# 两个 schema 版本分别解释执行内容和完整 wire；任何字段语义变化都必须发布新版本，
# 不能在旧字符串下静默改变 hash 或数据面执行结果。
POLICY_SNAPSHOT_CONTENT_SCHEMA_VERSION = "policy-snapshot-content-v1"
POLICY_SNAPSHOT_WIRE_SCHEMA_VERSION = "policy-snapshot-wire-v1"

# wire 资源上限同时保护 JSON 解析内存和 Gateway 缓存。部署可把限制收紧，但不能放宽到
# 超过 shared 绝对上限；单快照策略数复用 PRD-044 的整组求值上限。
MAX_POLICY_SNAPSHOT_WIRE_BYTES = 16_777_216
MAX_POLICY_SNAPSHOT_ITEMS = MAX_POLICIES_PER_SET
MAX_SNAPSHOT_IDENTIFIER_CODEPOINTS = 128
MAX_SNAPSHOT_IDENTIFIER_UTF8_BYTES = 512
MAX_POLICY_PRIORITY = 10_000

PolicySnapshotOperation = Literal["publish", "rollback"]
PolicySnapshotActionType = Literal["allow", "deny", "mask", "log_only", "route_to"]

# 哈希格式与两级字段白名单共同定义严格网络边界，未知键不得被兼容性逻辑静默忽略。
_LOWER_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_WIRE_FIELDS = frozenset(
    {
        "wire_schema_version",
        "tenant_id",
        "policy_snapshot_id",
        "snapshot_version",
        "operation",
        "previous_snapshot_id",
        "previous_snapshot_version",
        "rollback_source_snapshot_id",
        "rollback_source_snapshot_version",
        "policy_count",
        "content_hash",
        "artifact_hash",
        "policies",
    }
)
_ITEM_FIELDS = frozenset(
    {
        "evaluation_order",
        "policy_id",
        "policy_version",
        "policy_rule_id",
        "policy_action_id",
        "priority",
        "dsl_version",
        "canonical_expression",
        "condition_hash",
        "variables",
        "action_type",
        "target_model_id",
    }
)


class PolicySnapshotValidationError(ValueError):
    """表示快照对象不满足稳定 wire 或 hash 合同。

    异常只保存固定 reason 和安全字段路径，不保存原始 payload、表达式或底层异常正文。
    调用方可把 ``code`` 映射为统一错误；Gateway 刷新失败时应保留原 LKG，而不是部分覆盖。
    """

    code = "policy_snapshot.payload_invalid"

    def __init__(self, reason: str, *, field_path: str | None = None) -> None:
        """创建不回显输入的稳定校验错误。

        参数：reason 是模块内固定原因；field_path 仅允许由固定 schema 字段和数组下标组成。
        返回：不返回值，实例可由 API/缓存边界捕获并转换。
        约束：严禁把 wire 值、canonical expression 或内部 Token 拼入异常文本。
        """

        super().__init__("Policy snapshot payload is invalid.")
        self.reason = reason
        self.field_path = field_path


@dataclass(frozen=True, slots=True)
class PolicySnapshotItem:
    """一条已重新编译且可放入不可变快照的策略。

    ``policy_version`` 只属于来源 provenance，因此进入 artifact hash 而不进入 content hash。
    ``compiled_expression`` 由 shared 编译器生成且不序列化，Gateway 可直接把它装入求值输入，
    避免每次业务请求重新编译 DSL。
    """

    evaluation_order: int
    policy_id: str
    policy_version: int
    policy_rule_id: str
    policy_action_id: str
    priority: int
    dsl_version: str
    canonical_expression: str
    condition_hash: str
    variables: tuple[str, ...]
    action_type: PolicySnapshotActionType
    target_model_id: str | None
    compiled_expression: CompiledPolicyExpression = field(repr=False, compare=False)

    def as_content_dict(self) -> dict[str, object]:
        """返回精确 content-hash 项，不混入来源版本等纯 provenance。"""

        return {
            "evaluation_order": self.evaluation_order,
            "policy_id": self.policy_id,
            "policy_rule_id": self.policy_rule_id,
            "policy_action_id": self.policy_action_id,
            "priority": self.priority,
            "dsl_version": self.dsl_version,
            "canonical_expression": self.canonical_expression,
            "condition_hash": self.condition_hash,
            "variables": list(self.variables),
            "action_type": self.action_type,
            "target_model_id": self.target_model_id,
        }

    def as_wire_dict(self) -> dict[str, object]:
        """返回 artifact/wire 共用的完整项，并显式包含来源 Policy 版本。"""

        return {
            "evaluation_order": self.evaluation_order,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_rule_id": self.policy_rule_id,
            "policy_action_id": self.policy_action_id,
            "priority": self.priority,
            "dsl_version": self.dsl_version,
            "canonical_expression": self.canonical_expression,
            "condition_hash": self.condition_hash,
            "variables": list(self.variables),
            "action_type": self.action_type,
            "target_model_id": self.target_model_id,
        }


@dataclass(frozen=True, slots=True)
class PolicySnapshotArtifact:
    """通过双 hash 校验、可被 Gateway 原子缓存的完整快照。

    对象使用 frozen dataclass 和 tuple 保存结构，调用方应在一次请求开始时固定持有该引用；
    后台刷新只能用新对象替换 tenant cache，不能修改正在流式或 Fallback 中使用的版本。
    """

    wire_schema_version: str
    tenant_id: str
    policy_snapshot_id: str
    snapshot_version: int
    operation: PolicySnapshotOperation
    previous_snapshot_id: str | None
    previous_snapshot_version: int | None
    rollback_source_snapshot_id: str | None
    rollback_source_snapshot_version: int | None
    policy_count: int
    content_hash: str
    artifact_hash: str
    policies: tuple[PolicySnapshotItem, ...]

    @property
    def items(self) -> tuple[PolicySnapshotItem, ...]:
        """返回数据库领域常用的 items 只读别名，底层仍与 wire ``policies`` 同一 tuple。"""

        return self.policies

    def as_wire_dict(self) -> dict[str, object]:
        """生成不含 actor、原因、Prompt 或 Key 的精确内部 wire 对象。"""

        return {
            "wire_schema_version": self.wire_schema_version,
            "tenant_id": self.tenant_id,
            "policy_snapshot_id": self.policy_snapshot_id,
            "snapshot_version": self.snapshot_version,
            "operation": self.operation,
            "previous_snapshot_id": self.previous_snapshot_id,
            "previous_snapshot_version": self.previous_snapshot_version,
            "rollback_source_snapshot_id": self.rollback_source_snapshot_id,
            "rollback_source_snapshot_version": self.rollback_source_snapshot_version,
            "policy_count": self.policy_count,
            "content_hash": self.content_hash,
            "artifact_hash": self.artifact_hash,
            "policies": [item.as_wire_dict() for item in self.policies],
        }


def build_policy_snapshot_item(
    *,
    evaluation_order: int,
    policy_id: str,
    policy_version: int,
    policy_rule_id: str,
    policy_action_id: str,
    priority: int,
    action_type: str,
    target_model_id: str | None = None,
    expression: str | None = None,
    dsl_version: str | None = None,
    canonical_expression: str | None = None,
    condition_hash: str | None = None,
    variables: Sequence[str] | None = None,
) -> PolicySnapshotItem:
    """重新编译一条策略并生成 canonical 快照项。

    参数：传入来源字段、动作，以及原始 ``expression`` 或持久化 canonical 四元组；两者并存时
    仍以原始表达式重编，并核对调用方给出的 canonical/version/hash/variables。
    返回：字段均已边界校验、表达式由 PRD-044 编译器重新生成的不可变项。
    约束：本函数不信任已存 AST/hash；租户归属和 route target active 状态仍由控制面事务校验。
    """

    order = _strict_int(
        evaluation_order,
        field_path="policies[].evaluation_order",
        minimum=0,
        maximum=MAX_POLICY_SNAPSHOT_ITEMS - 1,
    )
    source_version = _strict_int(
        policy_version,
        field_path="policies[].policy_version",
        minimum=1,
    )
    safe_priority = _strict_int(
        priority,
        field_path="policies[].priority",
        minimum=1,
        maximum=MAX_POLICY_PRIORITY,
    )
    safe_policy_id = _bounded_identifier(policy_id, field_path="policies[].policy_id")
    safe_rule_id = _bounded_identifier(
        policy_rule_id, field_path="policies[].policy_rule_id"
    )
    safe_action_id = _bounded_identifier(
        policy_action_id, field_path="policies[].policy_action_id"
    )
    safe_target = _optional_identifier(
        target_model_id, field_path="policies[].target_model_id"
    )

    compile_source = expression if expression is not None else canonical_expression
    # 没有任何可重编文本时绝不能接受数据库 AST 或调用方声明的 hash 代替真实编译。
    if compile_source is None:
        raise PolicySnapshotValidationError(
            "expression_missing", field_path="policies[].canonical_expression"
        )
    try:
        # 快照只接收 shared 编译结果，不能从数据库 AST 或 wire JSON 直接构造可执行节点。
        compiled = compile_policy_expression(compile_source)
    except PolicyDslError:
        raise PolicySnapshotValidationError(
            "expression_invalid", field_path="policies[].canonical_expression"
        ) from None
    try:
        canonical_policy_action(action_type, safe_target)
    # 动作与目标组合不合法时整体拒绝，不能把 route_to 缺目标降级为默认路由。
    except PolicyActionError:
        raise PolicySnapshotValidationError(
            "action_invalid", field_path="policies[].action_type"
        ) from None

    # 持久化 DSL 版本只能作为待核对事实，版本漂移必须阻止发布和缓存替换。
    if dsl_version is not None and dsl_version != compiled.dsl_version:
        raise PolicySnapshotValidationError(
            "dsl_version_mismatch", field_path="policies[].dsl_version"
        )
    # 表达式即使语义等价也必须已经 canonical，避免 wire 原文未被完整 hash 覆盖。
    if canonical_expression is not None and canonical_expression != compiled.canonical_expression:
        raise PolicySnapshotValidationError(
            "canonical_expression_mismatch",
            field_path="policies[].canonical_expression",
        )
    # condition hash 必须等于本次重编结果，不能信任控制面或数据库保存的旧摘要。
    if condition_hash is not None and _sha256_hex(
        condition_hash, field_path="policies[].condition_hash"
    ) != compiled.condition_hash:
        # 摘要漂移说明持久事实已损坏，错误不得携带表达式或收到的 hash。
        raise PolicySnapshotValidationError(
            "condition_hash_mismatch", field_path="policies[].condition_hash"
        )
    # 变量数组参与上下文预取，类型、顺序或集合漂移都会改变数据面执行前置条件。
    if variables is not None:
        # 字符串本身属于 Sequence，但不能被拆成字符后伪装变量白名单数组。
        if isinstance(variables, (str, bytes, bytearray)) or not isinstance(
            variables, Sequence
        ):
            # 非数组变量输入无法与编译器依赖集合逐项比较，必须整体拒绝。
            raise PolicySnapshotValidationError(
                "variables_invalid", field_path="policies[].variables"
            )
        expected_variables = tuple(variables)
        # 调用方对象不能依靠宽松字符串转换进入 canonical 合同。
        if any(type(variable) is not str for variable in expected_variables):
            raise PolicySnapshotValidationError(
                "variables_invalid", field_path="policies[].variables"
            )
        # shared 编译器输出按名称排序，任何重复、缺失或重排都属于损坏事实。
        if expected_variables != compiled.variables:
            raise PolicySnapshotValidationError(
                "variables_mismatch", field_path="policies[].variables"
            )

    return PolicySnapshotItem(
        evaluation_order=order,
        policy_id=safe_policy_id,
        policy_version=source_version,
        policy_rule_id=safe_rule_id,
        policy_action_id=safe_action_id,
        priority=safe_priority,
        dsl_version=compiled.dsl_version,
        canonical_expression=compiled.canonical_expression,
        condition_hash=compiled.condition_hash,
        variables=compiled.variables,
        action_type=action_type,  # type: ignore[arg-type]
        target_model_id=safe_target,
        compiled_expression=compiled,
    )


def compute_policy_snapshot_content_hash(
    tenant_id: str, policies: Iterable[PolicySnapshotItem]
) -> str:
    """计算绑定租户和执行语义、但不绑定发布 provenance 的 content hash。

    参数：可信认证边界得到的 tenant_id，以及按 evaluation_order 排列的快照项。
    返回：``sha256(UTF-8 canonical JSON)`` 的 64 位小写十六进制字符串。
    约束：数组不排序；必须从 0 连续排列，且每项会重新编译核对，避免可变 AST 污染 hash。
    """

    safe_tenant_id = _bounded_identifier(tenant_id, field_path="tenant_id")
    stable_policies = _validate_policy_sequence(tuple(policies))
    payload = {
        "content_schema_version": POLICY_SNAPSHOT_CONTENT_SCHEMA_VERSION,
        "tenant_id": safe_tenant_id,
        "policies": [item.as_content_dict() for item in stable_policies],
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def compute_policy_snapshot_artifact_hash(
    *,
    tenant_id: str,
    policy_snapshot_id: str,
    snapshot_version: int,
    operation: str,
    previous_snapshot_id: str | None,
    previous_snapshot_version: int | None,
    rollback_source_snapshot_id: str | None,
    rollback_source_snapshot_version: int | None,
    content_hash: str,
    policies: Iterable[PolicySnapshotItem],
) -> str:
    """计算覆盖完整 wire envelope、来源版本和 items 的 artifact hash。

    参数：发布/回滚 envelope、已经按 shared 合同计算的 content_hash 与完整策略项。
    返回：稳定 64 位小写 SHA-256；结果可用于内部响应完整性与 ETag 输入。
    约束：artifact hash 不包含自身；可空 provenance 保留为 JSON null，整数拒绝 bool。
    """

    stable_policies = _validate_policy_sequence(tuple(policies))
    envelope = _validate_envelope(
        tenant_id=tenant_id,
        policy_snapshot_id=policy_snapshot_id,
        snapshot_version=snapshot_version,
        operation=operation,
        previous_snapshot_id=previous_snapshot_id,
        previous_snapshot_version=previous_snapshot_version,
        rollback_source_snapshot_id=rollback_source_snapshot_id,
        rollback_source_snapshot_version=rollback_source_snapshot_version,
    )
    safe_content_hash = _sha256_hex(content_hash, field_path="content_hash")
    payload = {
        "wire_schema_version": POLICY_SNAPSHOT_WIRE_SCHEMA_VERSION,
        **envelope,
        "policy_count": len(stable_policies),
        "content_hash": safe_content_hash,
        "policies": [item.as_wire_dict() for item in stable_policies],
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def build_policy_snapshot_artifact(
    *,
    tenant_id: str,
    policy_snapshot_id: str,
    snapshot_version: int,
    operation: str,
    previous_snapshot_id: str | None = None,
    previous_snapshot_version: int | None = None,
    rollback_source_snapshot_id: str | None = None,
    rollback_source_snapshot_version: int | None = None,
    policies: Iterable[PolicySnapshotItem] | None = None,
    items: Iterable[PolicySnapshotItem] | None = None,
    expected_content_hash: str | None = None,
    expected_artifact_hash: str | None = None,
) -> PolicySnapshotArtifact:
    """构造完整不可变快照，并可同时核对持久化或 wire 提供的双 hash。

    参数：envelope、策略项和两个可选期望 hash；控制面发布可不传期望值，Gateway 必须传入。
    返回：所有 canonical、顺序、provenance 和双 hash 均一致的 ``PolicySnapshotArtifact``。
    约束：任一字段不一致整体失败，调用方不得用部分成功项覆盖同 tenant 的 LKG。
    """

    stable_policies = _validate_policy_sequence(_coalesce_policy_items(policies, items))
    envelope = _validate_envelope(
        tenant_id=tenant_id,
        policy_snapshot_id=policy_snapshot_id,
        snapshot_version=snapshot_version,
        operation=operation,
        previous_snapshot_id=previous_snapshot_id,
        previous_snapshot_version=previous_snapshot_version,
        rollback_source_snapshot_id=rollback_source_snapshot_id,
        rollback_source_snapshot_version=rollback_source_snapshot_version,
    )
    content_hash = compute_policy_snapshot_content_hash(
        str(envelope["tenant_id"]), stable_policies
    )
    # Gateway 必须校验 wire 声明的执行内容摘要，失败时保留完整旧 LKG。
    if expected_content_hash is not None and _sha256_hex(
        expected_content_hash, field_path="content_hash"
    ) != content_hash:
        # 声明摘要与重算结果不一致时不能返回部分已验证的策略列表。
        raise PolicySnapshotValidationError("content_hash_mismatch", field_path="content_hash")

    artifact_hash = compute_policy_snapshot_artifact_hash(
        tenant_id=str(envelope["tenant_id"]),
        policy_snapshot_id=str(envelope["policy_snapshot_id"]),
        snapshot_version=int(envelope["snapshot_version"]),
        operation=str(envelope["operation"]),
        previous_snapshot_id=_optional_str(envelope["previous_snapshot_id"]),
        previous_snapshot_version=_optional_int(envelope["previous_snapshot_version"]),
        rollback_source_snapshot_id=_optional_str(
            envelope["rollback_source_snapshot_id"]
        ),
        rollback_source_snapshot_version=_optional_int(
            envelope["rollback_source_snapshot_version"]
        ),
        content_hash=content_hash,
        policies=stable_policies,
    )
    # artifact 额外绑定版本和 provenance，避免同 content 被替换到伪造 envelope。
    if expected_artifact_hash is not None and _sha256_hex(
        expected_artifact_hash, field_path="artifact_hash"
    ) != artifact_hash:
        # envelope 完整性失败时保留旧缓存，不能只采用已通过 content hash 的部分。
        raise PolicySnapshotValidationError(
            "artifact_hash_mismatch", field_path="artifact_hash"
        )

    return PolicySnapshotArtifact(
        wire_schema_version=POLICY_SNAPSHOT_WIRE_SCHEMA_VERSION,
        tenant_id=str(envelope["tenant_id"]),
        policy_snapshot_id=str(envelope["policy_snapshot_id"]),
        snapshot_version=int(envelope["snapshot_version"]),
        operation=str(envelope["operation"]),  # type: ignore[arg-type]
        previous_snapshot_id=_optional_str(envelope["previous_snapshot_id"]),
        previous_snapshot_version=_optional_int(envelope["previous_snapshot_version"]),
        rollback_source_snapshot_id=_optional_str(
            envelope["rollback_source_snapshot_id"]
        ),
        rollback_source_snapshot_version=_optional_int(
            envelope["rollback_source_snapshot_version"]
        ),
        policy_count=len(stable_policies),
        content_hash=content_hash,
        artifact_hash=artifact_hash,
        policies=stable_policies,
    )


def policy_snapshot_to_wire_dict(snapshot: PolicySnapshotArtifact) -> dict[str, object]:
    """把已校验快照复制为 JSON-compatible wire 字典，避免调用方手工漏字段。"""

    # 只允许本模块构造的已校验对象进入序列化，普通字典必须先走严格反序列化。
    if not isinstance(snapshot, PolicySnapshotArtifact):
        raise TypeError("snapshot must be a PolicySnapshotArtifact")
    # 序列化前再次计算双 hash，防止 compiled AST 被外部代码原地修改后继续下发。
    rebuilt = build_policy_snapshot_artifact(
        tenant_id=snapshot.tenant_id,
        policy_snapshot_id=snapshot.policy_snapshot_id,
        snapshot_version=snapshot.snapshot_version,
        operation=snapshot.operation,
        previous_snapshot_id=snapshot.previous_snapshot_id,
        previous_snapshot_version=snapshot.previous_snapshot_version,
        rollback_source_snapshot_id=snapshot.rollback_source_snapshot_id,
        rollback_source_snapshot_version=snapshot.rollback_source_snapshot_version,
        policies=snapshot.policies,
        expected_content_hash=snapshot.content_hash,
        expected_artifact_hash=snapshot.artifact_hash,
    )
    return rebuilt.as_wire_dict()


def serialize_policy_snapshot_wire(snapshot: PolicySnapshotArtifact) -> bytes:
    """按 canonical JSON 生成 UTF-8 wire 字节，供 Control 响应与固定向量复用。"""

    return _canonical_json_bytes(policy_snapshot_to_wire_dict(snapshot))


def deserialize_policy_snapshot_wire(
    payload: Mapping[str, object], *, expected_tenant_id: str | None = None
) -> PolicySnapshotArtifact:
    """严格反序列化已解析 JSON，并重新编译每条 canonical expression。

    参数：payload 必须是普通 JSON object；expected_tenant_id 来自路径、认证或缓存 key。
    返回：通过 schema、字段类型、顺序、DSL 和双 hash 校验的不可变快照。
    约束：拒绝未知字段、bool 冒充 int、tenant relabel 和任何局部篡改，不接受 AST。
    """

    raw = _strict_object(payload, expected_fields=_WIRE_FIELDS, field_path="snapshot")
    wire_schema_version = _strict_string(
        raw["wire_schema_version"], field_path="wire_schema_version", maximum=64
    )
    # 未知 wire 版本不能套用 v1 hash 解释，否则升级期间会产生错误等价关系。
    if wire_schema_version != POLICY_SNAPSHOT_WIRE_SCHEMA_VERSION:
        raise PolicySnapshotValidationError(
            "wire_schema_unsupported", field_path="wire_schema_version"
        )
    tenant_id = _wire_identifier(raw["tenant_id"], field_path="tenant_id")
    # 路径、认证或缓存 key 提供的可信租户必须与 payload 精确一致，hash 不是授权凭证。
    if expected_tenant_id is not None:
        safe_expected_tenant = _bounded_identifier(
            expected_tenant_id, field_path="expected_tenant_id"
        )
        # tenant relabel 即使重新计算双 hash 也不能跨越调用方已认证租户边界。
        if tenant_id != safe_expected_tenant:
            raise PolicySnapshotValidationError("tenant_mismatch", field_path="tenant_id")

    raw_policies = _strict_list(raw["policies"], field_path="policies")
    policy_count = _strict_int(
        raw["policy_count"],
        field_path="policy_count",
        minimum=0,
        maximum=MAX_POLICY_SNAPSHOT_ITEMS,
    )
    # 声明数量与实际数组不一致时不能部分构造运行对象或按较小值截断。
    if len(raw_policies) != policy_count:
        raise PolicySnapshotValidationError("policy_count_mismatch", field_path="policy_count")
    policies = tuple(
        _deserialize_policy_item(item, expected_order=index)
        for index, item in enumerate(raw_policies)
    )

    return build_policy_snapshot_artifact(
        tenant_id=tenant_id,
        policy_snapshot_id=_wire_identifier(
            raw["policy_snapshot_id"], field_path="policy_snapshot_id"
        ),
        snapshot_version=_strict_int(
            raw["snapshot_version"], field_path="snapshot_version", minimum=1
        ),
        operation=_strict_operation(raw["operation"]),
        previous_snapshot_id=_wire_optional_identifier(
            raw["previous_snapshot_id"], field_path="previous_snapshot_id"
        ),
        previous_snapshot_version=_strict_optional_int(
            raw["previous_snapshot_version"],
            field_path="previous_snapshot_version",
            minimum=1,
        ),
        rollback_source_snapshot_id=_wire_optional_identifier(
            raw["rollback_source_snapshot_id"],
            field_path="rollback_source_snapshot_id",
        ),
        rollback_source_snapshot_version=_strict_optional_int(
            raw["rollback_source_snapshot_version"],
            field_path="rollback_source_snapshot_version",
            minimum=1,
        ),
        policies=policies,
        expected_content_hash=_sha256_hex(raw["content_hash"], field_path="content_hash"),
        expected_artifact_hash=_sha256_hex(
            raw["artifact_hash"], field_path="artifact_hash"
        ),
    )


def deserialize_policy_snapshot_wire_json(
    payload: bytes | bytearray | str,
    *,
    expected_tenant_id: str | None = None,
    max_payload_bytes: int = MAX_POLICY_SNAPSHOT_WIRE_BYTES,
) -> PolicySnapshotArtifact:
    """从原始 UTF-8 JSON 执行有界、拒绝重复键的严格 wire 反序列化。

    参数：原始响应字节/字符串、可信 tenant 和部署配置的响应体上限。
    返回：与 ``deserialize_policy_snapshot_wire`` 相同的不可变快照。
    约束：调用方仍应先检查 Content-Length；这里再次检查实际字节，防止分块响应绕过上限。
    """

    safe_limit = _strict_int(
        max_payload_bytes,
        field_path="max_payload_bytes",
        minimum=1_024,
        maximum=MAX_POLICY_SNAPSHOT_WIRE_BYTES,
    )
    # 字符串输入也按 UTF-8 实际字节计费，不能用 code point 数量绕过响应体限制。
    if isinstance(payload, str):
        try:
            raw_bytes = payload.encode("utf-8")
        # 非法代理字符不能形成跨平台稳定 UTF-8 wire，错误中也不回显原文。
        except UnicodeEncodeError:
            raise PolicySnapshotValidationError("wire_unicode_invalid") from None
    elif isinstance(payload, (bytes, bytearray)) and not isinstance(payload, bool):
        raw_bytes = bytes(payload)
    else:
        # 文件对象、memoryview 或任意可迭代对象不属于明确网络输入合同。
        raise PolicySnapshotValidationError("wire_type_invalid")
    # Content-Length 只能作为前置优化，实际收到的字节仍必须再次执行硬上限。
    if len(raw_bytes) > safe_limit:
        raise PolicySnapshotValidationError("wire_too_large")
    try:
        text = raw_bytes.decode("utf-8")
        parsed = json.loads(
            text,
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    # JSON 语法、重复键、非法常量和 UTF-8 错误统一收敛，不传播响应正文或解析细节。
    except (UnicodeDecodeError, json.JSONDecodeError, PolicySnapshotValidationError):
        raise PolicySnapshotValidationError("wire_json_invalid") from None
    # 顶层数组或标量无法表达固定租户 envelope，必须在字段访问前拒绝。
    if not isinstance(parsed, dict):
        raise PolicySnapshotValidationError("wire_object_required")
    # 解析完成后仍需绑定可信租户并重编 DSL，JSON 成功绝不代表快照可以执行。
    return deserialize_policy_snapshot_wire(parsed, expected_tenant_id=expected_tenant_id)


def _deserialize_policy_item(payload: object, *, expected_order: int) -> PolicySnapshotItem:
    """严格恢复一条 wire 策略，并核对 shared 编译器的全部 canonical 输出。"""

    path = f"policies[{expected_order}]"
    raw = _strict_object(payload, expected_fields=_ITEM_FIELDS, field_path=path)
    order = _strict_int(
        raw["evaluation_order"],
        field_path=f"{path}.evaluation_order",
        minimum=0,
        maximum=MAX_POLICY_SNAPSHOT_ITEMS - 1,
    )
    if order != expected_order:
        # 连续 order 同时阻止重复、缺口和 wire 数组顺序与求值顺序不一致。
        raise PolicySnapshotValidationError(
            "evaluation_order_invalid", field_path=f"{path}.evaluation_order"
        )
    dsl_version = _strict_string(
        raw["dsl_version"], field_path=f"{path}.dsl_version", maximum=64
    )
    # 未知 DSL 版本不能借用当前编译器解释，否则条件语义可能在升级后悄然变化。
    if dsl_version != DSL_VERSION:
        raise PolicySnapshotValidationError(
            "dsl_version_unsupported", field_path=f"{path}.dsl_version"
        )
    canonical_expression = _strict_string(
        raw["canonical_expression"],
        field_path=f"{path}.canonical_expression",
        maximum=1_024,
        maximum_bytes=2_048,
    )
    action_type = _strict_action(raw["action_type"], field_path=f"{path}.action_type")
    target_model_id = _wire_optional_identifier(
        raw["target_model_id"], field_path=f"{path}.target_model_id"
    )
    item = build_policy_snapshot_item(
        evaluation_order=order,
        policy_id=_wire_identifier(raw["policy_id"], field_path=f"{path}.policy_id"),
        policy_version=_strict_int(
            raw["policy_version"], field_path=f"{path}.policy_version", minimum=1
        ),
        policy_rule_id=_wire_identifier(
            raw["policy_rule_id"], field_path=f"{path}.policy_rule_id"
        ),
        policy_action_id=_wire_identifier(
            raw["policy_action_id"], field_path=f"{path}.policy_action_id"
        ),
        priority=_strict_int(
            raw["priority"],
            field_path=f"{path}.priority",
            minimum=1,
            maximum=MAX_POLICY_PRIORITY,
        ),
        expression=canonical_expression,
        action_type=action_type,
        target_model_id=target_model_id,
    )
    raw_variables = _strict_list(raw["variables"], field_path=f"{path}.variables")
    variables = tuple(
        _strict_string(value, field_path=f"{path}.variables[{index}]", maximum=32)
        for index, value in enumerate(raw_variables)
    )
    # 未知变量不能触发数据面从请求 Header 或正文临时补值，必须失败关闭。
    if any(variable not in ALLOWED_VARIABLES for variable in variables):
        raise PolicySnapshotValidationError(
            "variable_unsupported", field_path=f"{path}.variables"
        )
    condition_hash = _sha256_hex(
        raw["condition_hash"], field_path=f"{path}.condition_hash"
    )
    # Gateway 重编后的展示文本必须逐字符一致，禁止语义等价但非 canonical 的 wire。
    if canonical_expression != item.canonical_expression:
        raise PolicySnapshotValidationError(
            "canonical_expression_mismatch",
            field_path=f"{path}.canonical_expression",
        )
    # 条件摘要只认可本次 shared 编译结果，持久值或传输值不能覆盖计算结果。
    if condition_hash != item.condition_hash:
        raise PolicySnapshotValidationError(
            "condition_hash_mismatch", field_path=f"{path}.condition_hash"
        )
    # 变量集合用于有条件加载预算上下文，漂移会改变热路径依赖，必须整体拒绝。
    if variables != item.variables:
        raise PolicySnapshotValidationError(
            "variables_mismatch", field_path=f"{path}.variables"
        )
    return item


def _validate_policy_sequence(
    policies: tuple[PolicySnapshotItem, ...],
) -> tuple[PolicySnapshotItem, ...]:
    """校验策略数组连续顺序、唯一来源和每项 canonical 完整性。"""

    # 策略总数复用 PRD-044 求值上限，阻止超大快照拖垮同步和整组 deadline。
    if len(policies) > MAX_POLICY_SNAPSHOT_ITEMS:
        raise PolicySnapshotValidationError("policy_count_exceeded", field_path="policies")
    unique_policy_ids: set[str] = set()
    unique_rule_ids: set[str] = set()
    unique_action_ids: set[str] = set()
    for expected_order, item in enumerate(policies):
        # 普通字典或子类对象未经过 shared 编译入口，不能直接参与 canonical hash。
        if not isinstance(item, PolicySnapshotItem):
            raise PolicySnapshotValidationError("policy_item_type_invalid", field_path="policies")
        # 数组位置就是求值顺序，必须从零连续，不能让两个解释不同的排序同时存在。
        if item.evaluation_order != expected_order:
            raise PolicySnapshotValidationError(
                "evaluation_order_invalid",
                field_path=f"policies[{expected_order}].evaluation_order",
            )
        # Policy、Rule、Action 来源在同一快照中均应唯一，重复通常表示损坏 join 或伪造 wire。
        if (
            item.policy_id in unique_policy_ids
            or item.policy_rule_id in unique_rule_ids
            or item.policy_action_id in unique_action_ids
        ):
            # 重复来源说明快照 join 或 wire 已损坏，不能用数组后项覆盖前项。
            raise PolicySnapshotValidationError("policy_source_duplicate", field_path="policies")
        unique_policy_ids.add(item.policy_id)
        unique_rule_ids.add(item.policy_rule_id)
        unique_action_ids.add(item.policy_action_id)
        # frozen 只能阻止字段替换，CompiledPolicyExpression 内部 AST 仍是 dict；重新编译可防止
        # 外部原地篡改 AST 后生成与 canonical_expression 不一致的 hash 或运行对象。
        rebuilt = build_policy_snapshot_item(
            evaluation_order=item.evaluation_order,
            policy_id=item.policy_id,
            policy_version=item.policy_version,
            policy_rule_id=item.policy_rule_id,
            policy_action_id=item.policy_action_id,
            priority=item.priority,
            expression=item.canonical_expression,
            action_type=item.action_type,
            target_model_id=item.target_model_id,
        )
        # frozen 外壳不足以保护内部 AST 字典，因此序列化与 hash 前仍需逐字段重编核对。
        if (
            item.dsl_version != rebuilt.dsl_version
            or item.canonical_expression != rebuilt.canonical_expression
            or item.condition_hash != rebuilt.condition_hash
            or item.variables != rebuilt.variables
            or item.compiled_expression.canonical_json
            != rebuilt.compiled_expression.canonical_json
            or item.compiled_expression.ast != rebuilt.compiled_expression.ast
            or item.compiled_expression.condition_hash
            != rebuilt.compiled_expression.condition_hash
        ):
            # 任一 compiled 字段漂移都说明对象不再是创建时的不可变运行事实。
            raise PolicySnapshotValidationError(
                "compiled_policy_mismatch", field_path=f"policies[{expected_order}]"
            )
    return policies


def _coalesce_policy_items(
    policies: Iterable[PolicySnapshotItem] | None,
    items: Iterable[PolicySnapshotItem] | None,
) -> tuple[PolicySnapshotItem, ...]:
    """统一 wire ``policies`` 与数据库 ``items`` 命名，且禁止两套来源同时传入。"""

    # 两种命名只是调用侧兼容别名，同时传入或同时缺失都会造成来源歧义。
    if (policies is None) == (items is None):
        raise PolicySnapshotValidationError("policy_items_source_invalid", field_path="policies")
    selected = policies if policies is not None else items
    # selected 已由上面的互斥判断保证非空引用；tuple 固定一次迭代结果，防止生成器二次漂移。
    return tuple(selected)  # type: ignore[arg-type]


def _validate_envelope(
    *,
    tenant_id: object,
    policy_snapshot_id: object,
    snapshot_version: object,
    operation: object,
    previous_snapshot_id: object,
    previous_snapshot_version: object,
    rollback_source_snapshot_id: object,
    rollback_source_snapshot_version: object,
) -> dict[str, object]:
    """校验发布图 envelope，阻止降版、半空复合引用和伪回滚 provenance。"""

    safe_tenant_id = _bounded_identifier(tenant_id, field_path="tenant_id")
    safe_snapshot_id = _bounded_identifier(
        policy_snapshot_id, field_path="policy_snapshot_id"
    )
    safe_version = _strict_int(snapshot_version, field_path="snapshot_version", minimum=1)
    safe_operation = _strict_operation(operation)
    safe_previous_id = _optional_identifier(
        previous_snapshot_id, field_path="previous_snapshot_id"
    )
    safe_previous_version = _strict_optional_int(
        previous_snapshot_version, field_path="previous_snapshot_version", minimum=1
    )
    safe_source_id = _optional_identifier(
        rollback_source_snapshot_id, field_path="rollback_source_snapshot_id"
    )
    safe_source_version = _strict_optional_int(
        rollback_source_snapshot_version,
        field_path="rollback_source_snapshot_version",
        minimum=1,
    )

    # 首版没有前驱，后续版本必须精确引用 vN-1，阻止降版和断裂发布图。
    if safe_version == 1:
        # 复合引用必须全空或全有，半空字段不能依赖数据库宽松解释。
        if safe_previous_id is not None or safe_previous_version is not None:
            raise PolicySnapshotValidationError(
                "previous_snapshot_invalid", field_path="previous_snapshot_id"
            )
    elif (
        safe_previous_id is None
        or safe_previous_version is None
        or safe_previous_version != safe_version - 1
        or safe_previous_id == safe_snapshot_id
    ):
        # 当前 ID、前驱 ID 与版本关系不一致时，artifact provenance 不可信。
        raise PolicySnapshotValidationError(
            "previous_snapshot_invalid", field_path="previous_snapshot_version"
        )

    # 正常发布绝不携带 rollback 来源，避免 Webhook 和历史把操作类型解释错。
    if safe_operation == "publish":
        # source 复合字段必须同时为空，不能只清理版本或 ID 的一半。
        if safe_source_id is not None or safe_source_version is not None:
            raise PolicySnapshotValidationError(
                "rollback_source_invalid", field_path="rollback_source_snapshot_id"
            )
    elif (
        safe_version < 3
        or safe_source_id is None
        or safe_source_version is None
        or safe_source_version != safe_version - 2
        or safe_source_id in {safe_snapshot_id, safe_previous_id}
    ):
        # M1 只回滚当前 head 的上一版，所以新 vN 的来源必须恰为 vN-2，不能伪装任意历史。
        raise PolicySnapshotValidationError(
            "rollback_source_invalid", field_path="rollback_source_snapshot_version"
        )

    return {
        "tenant_id": safe_tenant_id,
        "policy_snapshot_id": safe_snapshot_id,
        "snapshot_version": safe_version,
        "operation": safe_operation,
        "previous_snapshot_id": safe_previous_id,
        "previous_snapshot_version": safe_previous_version,
        "rollback_source_snapshot_id": safe_source_id,
        "rollback_source_snapshot_version": safe_source_version,
    }


def _strict_object(
    value: object, *, expected_fields: frozenset[str], field_path: str
) -> dict[str, object]:
    """要求普通 JSON object 且字段集合精确相等，拒绝静默兼容未知字段。"""

    # 网络合同只接受 JSON object 对应的普通 dict，模型对象不能绕过字段白名单。
    if type(value) is not dict:
        raise PolicySnapshotValidationError("object_required", field_path=field_path)
    raw = value
    # 字段集合必须精确相等，缺字段与未来未知字段都不能在旧 schema 下静默解释。
    if set(raw) != expected_fields or any(type(key) is not str for key in raw):
        raise PolicySnapshotValidationError("fields_invalid", field_path=field_path)
    return raw


def _strict_list(value: object, *, field_path: str) -> list[object]:
    """要求 wire JSON array，tuple/set 等进程内类型不能冒充网络合同。"""

    # tuple/set 没有 JSON wire 语义，接受它们会掩盖调用方绕过真实反序列化。
    if type(value) is not list:
        raise PolicySnapshotValidationError("array_required", field_path=field_path)
    return value


def _strict_int(
    value: object,
    *,
    field_path: str,
    minimum: int,
    maximum: int | None = None,
) -> int:
    """读取严格整数并显式拒绝 Python 中 ``bool`` 的 int 子类行为。"""

    # Python bool 是 int 子类但 JSON 语义不同，因此只接受精确内建整数类型。
    if type(value) is not int:
        raise PolicySnapshotValidationError("integer_required", field_path=field_path)
    # 越界版本、数量或大小不能被截断，否则 hash 与运行对象会出现分歧。
    if value < minimum or (maximum is not None and value > maximum):
        raise PolicySnapshotValidationError("integer_out_of_range", field_path=field_path)
    return value


def _strict_optional_int(
    value: object, *, field_path: str, minimum: int
) -> int | None:
    """读取保留 JSON null 的可空严格整数，非空值继续执行完整边界校验。"""

    if value is None:
        return None
    return _strict_int(value, field_path=field_path, minimum=minimum)


def _strict_string(
    value: object,
    *,
    field_path: str,
    maximum: int,
    maximum_bytes: int | None = None,
) -> str:
    """读取有 code point/UTF-8 双上限的字符串，并拒绝非法 Unicode。"""

    # 空值和超长文本都不能依赖后续编译器或数据库截断后再形成 hash。
    if type(value) is not str or not value or len(value) > maximum:
        raise PolicySnapshotValidationError("string_invalid", field_path=field_path)
    try:
        encoded = value.encode("utf-8")
    # 非法代理字符不能进入跨平台 canonical JSON，异常不携带原始输入。
    except UnicodeEncodeError:
        raise PolicySnapshotValidationError("unicode_invalid", field_path=field_path) from None
    # UTF-8 字节上限单独防止少量多字节字符放大网络和缓存占用。
    if maximum_bytes is not None and len(encoded) > maximum_bytes:
        raise PolicySnapshotValidationError("string_too_large", field_path=field_path)
    return value


def _bounded_identifier(value: object, *, field_path: str) -> str:
    """规范化进程内 ID，并拒绝空白、控制字符和无界 Unicode。"""

    text = _strict_string(
        value,
        field_path=field_path,
        maximum=MAX_SNAPSHOT_IDENTIFIER_CODEPOINTS,
        maximum_bytes=MAX_SNAPSHOT_IDENTIFIER_UTF8_BYTES,
    )
    normalized = unicodedata.normalize("NFC", text)
    # 空白和控制/格式字符会制造视觉相同 ID，不能参与租户或对象归属判断。
    if any(
        character.isspace() or unicodedata.category(character).startswith("C")
        for character in normalized
    ):
        # 视觉混淆或控制字符 ID 不能用于租户、策略和模型对象归属判断。
        raise PolicySnapshotValidationError("identifier_invalid", field_path=field_path)
    return normalized


def _wire_identifier(value: object, *, field_path: str) -> str:
    """读取必须已处于 NFC 的 wire ID，避免 hash 覆盖值与接收原文不同。"""

    normalized = _bounded_identifier(value, field_path=field_path)
    # wire 必须自行处于 NFC，不能让接收端归一化后 hash 覆盖另一个文本值。
    if normalized != value:
        raise PolicySnapshotValidationError("identifier_not_canonical", field_path=field_path)
    return normalized


def _optional_identifier(value: object, *, field_path: str) -> str | None:
    """读取进程内可空 ID；None 必须显式保留到 canonical JSON。"""

    if value is None:
        return None
    return _bounded_identifier(value, field_path=field_path)


def _wire_optional_identifier(value: object, *, field_path: str) -> str | None:
    """读取 wire 可空 ID，并在非空时要求 NFC canonical 形式。"""

    if value is None:
        return None
    return _wire_identifier(value, field_path=field_path)


def _strict_operation(value: object) -> PolicySnapshotOperation:
    """读取发布操作白名单，未知值不能进入 hash 或状态分支。"""

    # 未知操作不能在旧 schema 下退化成 publish，回滚 provenance 也不能被忽略。
    if value not in {"publish", "rollback"} or type(value) is not str:
        raise PolicySnapshotValidationError("operation_invalid", field_path="operation")
    return value  # type: ignore[return-value]


def _strict_action(value: object, *, field_path: str) -> str:
    """读取 DSL v1 已知动作；mask 由 Gateway 在能力门禁通过后作为非终止修饰动作执行。"""

    # 动作必须来自 DSL v1 白名单，未知值不能被运行层默认当作 allow。
    if type(value) is not str or value not in ALLOWED_ACTIONS:
        raise PolicySnapshotValidationError("action_invalid", field_path=field_path)
    return value


def _sha256_hex(value: object, *, field_path: str) -> str:
    """读取精确 64 位小写 SHA-256，拒绝大写、前缀和宽松转换。"""

    # 大写、前缀、短摘要或宽松字符串转换都会破坏跨服务固定 hash 合同。
    if type(value) is not str or _LOWER_SHA256_RE.fullmatch(value) is None:
        raise PolicySnapshotValidationError("hash_invalid", field_path=field_path)
    return value


def _canonical_json_bytes(payload: object) -> bytes:
    """生成 key 排序、数组保序、无空白/换行/BOM 的 UTF-8 canonical JSON。"""

    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    # 非 JSON 类型、非有限浮点或非法 Unicode 统一失败，不能回退到 repr 序列化。
    except (TypeError, ValueError, UnicodeEncodeError):
        raise PolicySnapshotValidationError("canonical_json_invalid") from None


def _json_object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """构造 JSON object 并拒绝重复键，避免解析器最后值覆盖安全字段。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        # 重复安全字段若由解析器最后值覆盖，会让签名、日志和执行看到不同事实。
        if key in result:
            raise PolicySnapshotValidationError("json_key_duplicate")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> NoReturn:
    """拒绝 JSON 标准之外的 NaN/Infinity，防止平台相关浮点序列化。"""

    # NaN/Infinity 不是标准 JSON，且平台序列化不一致，必须在对象构造前拒绝。
    raise PolicySnapshotValidationError("json_constant_invalid")


def _optional_str(value: object) -> str | None:
    """把已校验 envelope 可空字符串收窄为静态类型，不执行宽松转换。"""

    return value if isinstance(value, str) else None


def _optional_int(value: object) -> int | None:
    """把已校验 envelope 可空整数收窄为静态类型，并保持 bool 不可达。"""

    return value if type(value) is int else None
