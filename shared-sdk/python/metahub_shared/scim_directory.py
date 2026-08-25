"""PRD-115 SCIM token, filter, version, and role contracts.

The module is deliberately storage-free. It normalizes only the RFC subset supported by
MetaHub and derives irreversible values in a SCIM-specific HMAC domain so raw Bearer,
external identifiers, and user names never need to enter events or logs.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
from dataclasses import dataclass
from typing import Literal

SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCIM_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
SCIM_ENTERPRISE_USER_SCHEMA = (
    "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
)

ScimFilterAttribute = Literal["userName", "externalId", "displayName"]
ScimRole = Literal[
    "admin",
    "developer",
    "security_admin",
    "finance_admin",
    "legal_admin",
    "auditor",
    "viewer",
]

_HMAC_DOMAIN = b"metahub/prd115/scim/hmac/v1\x00"
_FILTER = re.compile(
    r'^\s*(userName|externalId|displayName)\s+eq\s+"([^"\\]{1,320})"\s*$',
    re.ASCII,
)
_EXTERNAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+ -]{0,254}$", re.ASCII)
_BLOCKED_DOMAIN_SUFFIXES = (
    ".local",
    ".internal",
    ".localhost",
    ".home",
    ".lan",
    ".corp",
)
_ROLE_RISK = {
    "viewer": 0,
    "auditor": 1,
    "developer": 2,
    "finance_admin": 3,
    "legal_admin": 3,
    "security_admin": 4,
    "admin": 5,
}


@dataclass(frozen=True, slots=True)
class ScimFilter:
    attribute: ScimFilterAttribute
    value: str


def scim_hmac(secret: object, purpose: object, value: object) -> str:
    """Return a domain-separated digest for one bounded SCIM identity value."""

    if not isinstance(secret, str) or not 32 <= len(secret.encode("utf-8")) <= 512:
        raise ValueError("scim.secret_invalid")
    if not isinstance(purpose, str) or re.fullmatch(r"[a-z][a-z0-9_]{1,31}", purpose) is None:
        raise ValueError("scim.input_invalid")
    if not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= 4096:
        raise ValueError("scim.input_invalid")
    return hmac.new(
        secret.encode("utf-8"),
        _HMAC_DOMAIN + purpose.encode("ascii") + b"\x00" + value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def normalize_scim_user_name(value: object) -> str:
    """Normalize the supported email-shaped SCIM userName without accepting owner data."""

    if not isinstance(value, str) or value != value.strip() or value.count("@") != 1:
        raise ValueError("scim.user_name_invalid")
    local, domain = value.rsplit("@", 1)
    if not 1 <= len(local) <= 64 or any(ord(character) < 33 for character in local):
        raise ValueError("scim.user_name_invalid")
    normalized = f"{local.casefold()}@{_normalize_domain(domain)}"
    if len(normalized) > 320:
        raise ValueError("scim.user_name_invalid")
    return normalized


def _normalize_domain(value: str) -> str:
    try:
        normalized = value.rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        raise ValueError("scim.user_name_invalid") from None
    if (
        not 3 <= len(normalized) <= 253
        or normalized.count(".") < 1
        or any(normalized.endswith(suffix) for suffix in _BLOCKED_DOMAIN_SUFFIXES)
    ):
        raise ValueError("scim.user_name_invalid")
    try:
        ipaddress.ip_address(normalized.strip("[]"))
    except ValueError:
        pass
    else:
        raise ValueError("scim.user_name_invalid") from None
    labels = normalized.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in label)
        for label in labels
    ):
        raise ValueError("scim.user_name_invalid")
    return normalized


def normalize_scim_external_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or _EXTERNAL_ID.fullmatch(value) is None
    ):
        raise ValueError("scim.external_id_invalid")
    return value


def parse_scim_filter(value: object) -> ScimFilter:
    """Parse only a single equality expression; boolean composition is intentionally absent."""

    if not isinstance(value, str) or len(value) > 384:
        raise ValueError("scim.filter_invalid")
    match = _FILTER.fullmatch(value)
    if match is None:
        raise ValueError("scim.filter_invalid")
    attribute, raw = match.groups()
    normalized = normalize_scim_user_name(raw) if attribute == "userName" else raw.strip()
    if attribute == "externalId":
        normalized = normalize_scim_external_id(normalized)
    return ScimFilter(attribute=attribute, value=normalized)  # type: ignore[arg-type]


def choose_lowest_risk_role(values: object) -> ScimRole | None:
    """Merge group mappings conservatively; owner and machine/platform roles are never accepted."""

    if not isinstance(values, (tuple, list, set, frozenset)):
        raise ValueError("scim.role_invalid")
    roles = list(values)
    if any(not isinstance(role, str) or role not in _ROLE_RISK for role in roles):
        raise ValueError("scim.role_invalid")
    if not roles:
        return None
    return min(roles, key=lambda role: (_ROLE_RISK[role], role))  # type: ignore[return-value]


def parse_scim_version(value: object, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool):
        raise ValueError("scim.version_invalid")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"(?:0|[1-9][0-9]*)", value):
        result = int(value)
    elif isinstance(value, str) and re.fullmatch(r'(?:W/)?"(?:0|[1-9][0-9]*)"', value):
        result = int(value[value.find('"') + 1 : -1])
    else:
        raise ValueError("scim.version_invalid")
    if result < (0 if allow_zero else 1) or result > 2_147_483_647:
        raise ValueError("scim.version_invalid")
    return result


__all__ = [
    "SCIM_ENTERPRISE_USER_SCHEMA",
    "SCIM_ERROR_SCHEMA",
    "SCIM_GROUP_SCHEMA",
    "SCIM_LIST_SCHEMA",
    "SCIM_PATCH_SCHEMA",
    "SCIM_USER_SCHEMA",
    "ScimFilter",
    "choose_lowest_risk_role",
    "normalize_scim_external_id",
    "normalize_scim_user_name",
    "parse_scim_filter",
    "parse_scim_version",
    "scim_hmac",
]
