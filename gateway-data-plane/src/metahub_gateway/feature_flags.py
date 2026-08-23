"""Fast fail-closed Gateway projection for already-authorized feature capabilities."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from types import MappingProxyType

_WORKSPACE_ID = re.compile(r"^wsp_[0-9a-f]{32}$")
_FLAG_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,79}$")


@dataclass(frozen=True, slots=True)
class VerifiedCapabilitySnapshot:
    """Final Workspace decisions after upstream signature and owner verification."""

    workspace_id: str
    generation: int
    issued_at: datetime
    expires_at: datetime
    capabilities: Mapping[str, bool]

    def __post_init__(self) -> None:
        issued_at = _utc(self.issued_at)
        expires_at = _utc(self.expires_at)
        if (
            _WORKSPACE_ID.fullmatch(self.workspace_id) is None
            or self.generation < 1
            or expires_at <= issued_at
            or not isinstance(self.capabilities, Mapping)
            or len(self.capabilities) > 1000
        ):
            raise ValueError("feature capability snapshot is invalid")
        normalized: dict[str, bool] = {}
        for code, enabled in self.capabilities.items():
            if _FLAG_CODE.fullmatch(code) is None or type(enabled) is not bool:
                raise ValueError("feature capability snapshot is invalid")
            normalized[code] = enabled
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(
            self,
            "capabilities",
            MappingProxyType(dict(sorted(normalized.items()))),
        )


class LocalFeatureGate:
    """Atomic generation-fenced snapshot owner with sub-millisecond read path."""

    __slots__ = ("_lock", "_snapshot", "_workspace_id")

    def __init__(self, workspace_id: str) -> None:
        if _WORKSPACE_ID.fullmatch(workspace_id) is None:
            raise ValueError("feature gate workspace is invalid")
        self._workspace_id = workspace_id
        self._snapshot: VerifiedCapabilitySnapshot | None = None
        self._lock = RLock()

    def apply_verified_snapshot(self, snapshot: VerifiedCapabilitySnapshot) -> bool:
        if snapshot.workspace_id != self._workspace_id:
            return False
        with self._lock:
            current = self._snapshot
            if current is not None and snapshot.generation <= current.generation:
                return False
            self._snapshot = snapshot
            return True

    def enabled(
        self,
        flag_code: str,
        *,
        workspace_id: str,
        now: datetime | None = None,
    ) -> bool:
        if workspace_id != self._workspace_id or _FLAG_CODE.fullmatch(flag_code) is None:
            return False
        evaluated_at = _utc(now) if now is not None else datetime.now(UTC)
        with self._lock:
            snapshot = self._snapshot
            return bool(
                snapshot is not None
                and snapshot.expires_at > evaluated_at
                and snapshot.capabilities.get(flag_code, False)
            )

    @property
    def generation(self) -> int:
        with self._lock:
            return self._snapshot.generation if self._snapshot is not None else 0


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("feature capability timestamp is invalid")
    if value.tzinfo is None:
        raise ValueError("feature capability timestamp is invalid")
    return value.astimezone(UTC)


__all__ = ["LocalFeatureGate", "VerifiedCapabilitySnapshot"]
