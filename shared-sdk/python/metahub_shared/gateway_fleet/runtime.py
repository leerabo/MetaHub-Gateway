"""Gateway placement 的租户绑定签名验证、严格制品解析与四层本地容量租约。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from metahub_shared.runtime.errors import ApiError

ResourceClass = Literal["chat", "embeddings"]
# 签名制品字段集合是严格解析白名单，额外 owner、算法或载荷字段一律拒绝。
_SNAPSHOT_FIELDS = {
    "tenant_id",
    "workspace_id",
    "gateway_cell_id",
    "cell_code",
    "region_code",
    "cell_status",
    "placement_revision",
    "residency_policy_version",
    "generation",
    "signing_key_version",
    "issued_at",
    "expires_at",
    "signature",
}


@dataclass(frozen=True, slots=True, repr=False)
class PlacementSnapshot:
    """只保存数据面准入所需的公开 owner、Cell、generation、有效期和签名。"""

    tenant_id: str
    workspace_id: str
    gateway_cell_id: str
    cell_code: str
    region_code: str
    cell_status: Literal["active", "draining", "unavailable"]
    placement_revision: int
    residency_policy_version: str
    generation: int
    signing_key_version: int
    issued_at: datetime
    expires_at: datetime
    signature: str


@dataclass(slots=True, repr=False)
class _CapacityLease:
    """持有一次四维计数；幂等释放避免流终态与异常清理重复扣减。"""

    controller: GatewayCapacityController
    tenant_id: str
    workspace_id: str
    virtual_key_id: str
    gateway_cell_id: str
    generation: int
    released: bool = False

    async def release(self) -> None:
        """在同一锁内幂等释放 tenant、Workspace、Key 与 Cell 计数。"""

        if self.released:
            return
        async with self.controller._lock:
            if self.released:
                return
            self.controller._decrement("tenant", self.tenant_id)
            self.controller._decrement("workspace", self.workspace_id)
            self.controller._decrement("key", self.virtual_key_id)
            self.controller._decrement("cell", self.gateway_cell_id)
            self.released = True


@dataclass(frozen=True, slots=True, repr=False)
class GatewayAdmission:
    """把冻结 placement 与容量租约绑定到同一次请求或流生命周期。"""

    snapshot: PlacementSnapshot
    capacity_lease: _CapacityLease

    async def release(self) -> None:
        """由普通请求 finally 或流终态统一释放容量。"""

        await self.capacity_lease.release()


class GatewayCapacityController:
    """使用部署拥有的固定上限执行进程内原子四维容量准入。"""

    def __init__(
        self,
        *,
        tenant_limit: int,
        workspace_limit: int,
        key_limit: int,
        cell_limit: int,
    ) -> None:
        """验证部署拥有的四层容量上限并初始化原子计数器，拒绝零值和布尔值。"""

        limits = (tenant_limit, workspace_limit, key_limit, cell_limit)
        # 容量上限只能是正整数，布尔值也不能借由整数子类语义穿透。
        if any(type(value) is not int or value < 1 for value in limits):
            raise ValueError("gateway capacity limits must be positive integers")
        self._limits = {
            "tenant": tenant_limit,
            "workspace": workspace_limit,
            "key": key_limit,
            "cell": cell_limit,
        }
        self._counts: dict[tuple[str, str], int] = {}
        self._lock = asyncio.Lock()

    @property
    def limits(self) -> tuple[int, int, int, int]:
        """按 tenant、Workspace、Virtual Key、Cell 固定顺序返回只读容量上限。"""

        return (
            self._limits["tenant"],
            self._limits["workspace"],
            self._limits["key"],
            self._limits["cell"],
        )

    async def acquire(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        virtual_key_id: str,
        gateway_cell_id: str,
        generation: int,
    ) -> _CapacityLease:
        """先检查全部维度再整体加一，任何维度耗尽都不留下部分计数。"""

        dimensions = (
            ("tenant", tenant_id),
            ("workspace", workspace_id),
            ("key", virtual_key_id),
            ("cell", gateway_cell_id),
        )
        async with self._lock:
            for kind, owner in dimensions:
                if self._counts.get((kind, owner), 0) >= self._limits[kind]:
                    # Cell 总池耗尽使用 503，便于调度层区分基础设施容量故障。
                    if kind == "cell":
                        raise ApiError("gateway.cell_capacity_exhausted")
                    # tenant、Workspace 或 Key 任一耗尽都按租户压力返回 429。
                    raise ApiError("gateway.tenant_capacity_exhausted")
            for kind, owner in dimensions:
                key = (kind, owner)
                self._counts[key] = self._counts.get(key, 0) + 1
        return _CapacityLease(
            controller=self,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            virtual_key_id=virtual_key_id,
            gateway_cell_id=gateway_cell_id,
            generation=generation,
        )

    def _decrement(self, kind: str, owner: str) -> None:
        """在持有控制器锁时递减单个 owner 计数，并在归零后删除计数键。"""

        key = (kind, owner)
        remaining = self._counts.get(key, 0) - 1
        if remaining > 0:
            self._counts[key] = remaining
        else:
            self._counts.pop(key, None)


def create_placement_snapshot(
    *,
    signing_secret: str,
    signing_key_version: int,
    tenant_id: str,
    workspace_id: str,
    gateway_cell_id: str,
    cell_code: str,
    region_code: str,
    placement_revision: int,
    residency_policy_version: str,
    generation: int,
    ttl_seconds: int,
    cell_status: Literal["active", "draining", "unavailable"] = "active",
    now: datetime | None = None,
) -> PlacementSnapshot:
    """Control 使用独立 HMAC 域创建绑定 tenant、Workspace、Cell 与有效期的短期制品。"""

    issued_at = (now or datetime.now(UTC)).astimezone(UTC)
    unsigned = PlacementSnapshot(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        gateway_cell_id=gateway_cell_id,
        cell_code=cell_code,
        region_code=region_code,
        cell_status=cell_status,
        placement_revision=placement_revision,
        residency_policy_version=residency_policy_version,
        generation=generation,
        signing_key_version=signing_key_version,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(seconds=ttl_seconds),
        signature="",
    )
    return PlacementSnapshot(
        **{**asdict(unsigned), "signature": _signature(unsigned, signing_secret)}
    )


def verify_placement_snapshot(
    snapshot: PlacementSnapshot,
    *,
    signing_secret: str,
    signing_key_version: int,
    tenant_id: str,
    workspace_id: str,
    now: datetime | None = None,
) -> None:
    """数据面验证 owner、key version、Cell、时间、generation 与常量时间签名。"""

    current = (now or datetime.now(UTC)).astimezone(UTC)
    if (
        snapshot.tenant_id != tenant_id
        or snapshot.workspace_id != workspace_id
        or snapshot.signing_key_version != signing_key_version
        or snapshot.cell_status != "active"
        or snapshot.issued_at > current + timedelta(seconds=5)
        or snapshot.expires_at <= current
        or snapshot.expires_at <= snapshot.issued_at
        or snapshot.placement_revision < 1
        or snapshot.generation < 1
    ):
        # Owner、key version、Cell 状态、时间或 generation 任一异常都使用同一 503。
        raise ApiError("gateway.placement_unavailable")
    expected = _signature(snapshot, signing_secret)
    # 签名使用常量时间比较，错误时不区分密钥、载荷或签名字段。
    if not hmac.compare_digest(snapshot.signature, expected):
        raise ApiError("gateway.placement_unavailable")


def install_placement_snapshot(app_state: Any, snapshot: PlacementSnapshot) -> None:
    """只安装更高 generation 或同 generation 的更新续签，拒绝回退。"""

    snapshots = getattr(app_state, "gateway_placement_snapshots", None)
    if snapshots is None:
        snapshots = {}
        app_state.gateway_placement_snapshots = snapshots
    # 非字典存储表示进程状态被污染，必须拒绝安装而不是覆盖未知对象。
    if not isinstance(snapshots, dict):
        raise ValueError("gateway placement snapshot store is invalid")
    key = (snapshot.tenant_id, snapshot.workspace_id)
    current = snapshots.get(key)
    # 旧 generation 或同 generation 的非更新续签不能回退当前已安装快照。
    if current is not None and (
        not isinstance(current, PlacementSnapshot)
        or current.generation > snapshot.generation
        or (
            current.generation == snapshot.generation
            and current.issued_at >= snapshot.issued_at
        )
    ):
        # generation 回退或非更新续签可能恢复旧路由，必须保持当前 LKG 不变。
        raise ValueError("gateway placement generation is stale")
    snapshots[key] = snapshot


def remove_placement_snapshot(
    app_state: Any,
    *,
    tenant_id: str,
    workspace_id: str,
    generation: int,
) -> bool:
    """应用已验证的新 Cell generation，只撤销对应 owner 的旧本地制品。"""

    snapshots = getattr(app_state, "gateway_placement_snapshots", None)
    if not isinstance(snapshots, dict):
        return False
    key = (tenant_id, workspace_id)
    current = snapshots.get(key)
    if not isinstance(current, PlacementSnapshot) or current.generation > generation:
        return False
    snapshots.pop(key, None)
    return True


def encode_placement_snapshot(snapshot: PlacementSnapshot) -> str:
    """规范化序列化签名后的公开 placement 制品，不包含运行时 Secret。"""

    payload = {
        **asdict(snapshot),
        "issued_at": _time(snapshot.issued_at),
        "expires_at": _time(snapshot.expires_at),
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def decode_placement_snapshot(payload: str) -> PlacementSnapshot:
    """在验签前严格解析不可信持久制品，拒绝重复键、额外字段和非标准数。"""

    # 持久制品必须是有界字符串，避免在 JSON 解析前消耗无界内存。
    if type(payload) is not str or not (100 <= len(payload.encode("utf-8")) <= 4096):
        raise ValueError("gateway placement snapshot payload is invalid")
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    # JSON 类型、重复字段、非标准数字和语法错误统一折叠为固定载荷错误。
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("gateway placement snapshot payload is invalid") from None
    # 字段集合必须与版本化白名单完全相等，额外字段也不能向后兼容接受。
    if type(value) is not dict or set(value) != _SNAPSHOT_FIELDS:
        raise ValueError("gateway placement snapshot schema is invalid")
    text_fields = (
        "tenant_id",
        "workspace_id",
        "gateway_cell_id",
        "cell_code",
        "region_code",
        "cell_status",
        "residency_policy_version",
        "issued_at",
        "expires_at",
        "signature",
    )
    integer_fields = (
        "placement_revision",
        "generation",
        "signing_key_version",
    )
    # 文本 owner 与时间字段都必须非空且类型精确，禁止隐式字符串转换。
    if any(type(value[name]) is not str or not value[name] for name in text_fields):
        raise ValueError("gateway placement snapshot field is invalid")
    # revision、generation 与 key version 必须是正整数，布尔值不能作为整数接受。
    if any(type(value[name]) is not int or value[name] < 1 for name in integer_fields):
        raise ValueError("gateway placement snapshot generation is invalid")
    # Cell 状态只接受签名协议冻结枚举，未知状态必须失败关闭。
    if value["cell_status"] not in {"active", "draining", "unavailable"}:
        raise ValueError("gateway placement snapshot Cell status is invalid")
    signature = value["signature"]
    # 签名字段必须是规范小写 SHA-256 十六进制，拒绝宽松编码和前后空白。
    if len(signature) != 64 or any(character not in "0123456789abcdef" for character in signature):
        raise ValueError("gateway placement snapshot signature is invalid")
    return PlacementSnapshot(
        tenant_id=value["tenant_id"],
        workspace_id=value["workspace_id"],
        gateway_cell_id=value["gateway_cell_id"],
        cell_code=value["cell_code"],
        region_code=value["region_code"],
        cell_status=value["cell_status"],
        placement_revision=value["placement_revision"],
        residency_policy_version=value["residency_policy_version"],
        generation=value["generation"],
        signing_key_version=value["signing_key_version"],
        issued_at=_parse_time(value["issued_at"]),
        expires_at=_parse_time(value["expires_at"]),
        signature=signature,
    )


async def admit_gateway_request(
    config: Any,
    app_state: Any,
    *,
    tenant_id: str,
    workspace_id: str,
    virtual_key_id: str,
    resource_class: ResourceClass,
) -> GatewayAdmission | None:
    """在正文解析前读取本地签名 placement 并原子获取四维容量令牌。"""

    if not bool(getattr(config, "gateway_placement_enabled", False)):
        return None
    # 只有已覆盖的 OpenAI 热路径资源类型能进入容量外壳，未知类型失败关闭。
    if resource_class not in {"chat", "embeddings"}:
        raise ApiError("gateway.placement_unavailable")
    secret = getattr(config, "gateway_placement_signing_secret", None)
    version = int(getattr(config, "gateway_placement_signing_version", 1))
    snapshots = getattr(app_state, "gateway_placement_snapshots", None)
    snapshot = snapshots.get((tenant_id, workspace_id)) if isinstance(snapshots, dict) else None
    # 启用态缺少独立密钥或 owner-bound 本地快照时，正文解析前固定返回 503。
    if not secret or not isinstance(snapshot, PlacementSnapshot):
        raise ApiError("gateway.placement_unavailable")
    verify_placement_snapshot(
        snapshot,
        signing_secret=secret,
        signing_key_version=version,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )
    limits = (
        int(getattr(config, "gateway_capacity_tenant_limit", 100)),
        int(getattr(config, "gateway_capacity_workspace_limit", 50)),
        int(getattr(config, "gateway_capacity_key_limit", 10)),
        int(getattr(config, "gateway_capacity_cell_limit", 500)),
    )
    controller = getattr(app_state, "gateway_capacity_controller", None)
    if not isinstance(controller, GatewayCapacityController) or controller.limits != limits:
        controller = GatewayCapacityController(
            tenant_limit=limits[0],
            workspace_limit=limits[1],
            key_limit=limits[2],
            cell_limit=limits[3],
        )
        app_state.gateway_capacity_controller = controller
    lease = await controller.acquire(
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        virtual_key_id=virtual_key_id,
        gateway_cell_id=snapshot.gateway_cell_id,
        generation=snapshot.generation,
    )
    return GatewayAdmission(snapshot=snapshot, capacity_lease=lease)


def _signature(snapshot: PlacementSnapshot, secret: str) -> str:
    """以固定字段顺序和独立 HMAC 域计算制品签名，不把原始密钥写入载荷。"""

    payload = {
        "cell_code": snapshot.cell_code,
        "cell_status": snapshot.cell_status,
        "expires_at": _time(snapshot.expires_at),
        "gateway_cell_id": snapshot.gateway_cell_id,
        "generation": snapshot.generation,
        "issued_at": _time(snapshot.issued_at),
        "placement_revision": snapshot.placement_revision,
        "region_code": snapshot.region_code,
        "residency_policy_version": snapshot.residency_policy_version,
        "signing_key_version": snapshot.signing_key_version,
        "tenant_id": snapshot.tenant_id,
        "workspace_id": snapshot.workspace_id,
    }
    canonical = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    return hmac.new(
        secret.encode("utf-8"), b"metahub/gateway-placement/v1\0" + canonical, hashlib.sha256
    ).hexdigest()


def _time(value: datetime) -> str:
    """把制品时间规范化为毫秒精度 UTC 文本，确保签名在不同进程间稳定。"""

    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    """严格解析制品时间并要求显式时区，错误统一映射为固定时间戳异常。"""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 解析失败不能携带原始时间文本，避免持久制品内容进入异常日志。
    except ValueError:
        raise ValueError("gateway placement snapshot timestamp is invalid") from None
    # 缺少时区的时间会造成跨 Cell 验证分歧，因此拒绝本地时间语义。
    if parsed.tzinfo is None:
        raise ValueError("gateway placement snapshot timestamp is invalid")
    return parsed.astimezone(UTC)


def _json_object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """构造严格 JSON 对象并拒绝重复键，防止解析器与验签字段选择产生歧义。"""

    value: dict[str, object] = {}
    for key, item in pairs:
        # 重复字段会产生签名解释歧义，必须在构造对象时立即拒绝。
        if key in value:
            raise ValueError("gateway placement snapshot contains duplicate fields")
        value[key] = item
    return value


def _reject_json_constant(_: str) -> None:
    """拒绝 NaN 与 Infinity 等非标准 JSON 数字，维持跨语言规范化一致。"""

    # 非标准数字不能进入签名载荷，统一使用固定解析错误。
    raise ValueError("gateway placement snapshot contains a non-standard number")
