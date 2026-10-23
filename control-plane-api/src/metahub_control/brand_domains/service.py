"""PRD-142 owner-separated brand state, release, asset, and public read services."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.security_service import (
    consume_global_step_up_grant_in_transaction,
    consume_step_up_grant_in_transaction,
)
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)

from .models import (
    BrandAssetSummary,
    BrandDomainSummary,
    ChannelBrandCommand,
    ChannelBrandInfo,
    ChannelBrandResponse,
    DeploymentBrandCommand,
    DeploymentBrandInfo,
    DeploymentBrandResponse,
    PlatformBrandCommand,
    PlatformBrandInfo,
    PlatformBrandResponse,
    PublicBrandInfo,
    PublicBrandResponse,
)
from .png import validate_brand_png

_DEFAULT = {
    "product_name": "MetaHub",
    "primary_color": "#176B87",
    "secondary_color": "#F59E0B",
    "login_title": "企业 AI Gateway",
    "login_message": "安全、可追踪的企业模型网关",
}
_VISUAL_FIELDS = tuple(_DEFAULT)
_COLOR = re.compile(r"^#[0-9A-F]{6}$")
_ID = re.compile(r"^[a-z]{3}_[0-9a-f]{32}$")
_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_RESERVED_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".test",
    ".invalid",
    ".example",
)
_PUBLIC_SUFFIXES = frozenset({"com", "net", "org", "edu", "gov", "cn", "uk", "co.uk"})


@dataclass(frozen=True, slots=True)
class PublicAsset:
    body: bytes
    etag: str


def get_platform_brand(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> PlatformBrandResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _platform_actor(connection, config, session_token, None, "platform.brand.read")
        result = _platform_response(
            connection, _platform_row(connection), actor, request_id, trace_id
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("branding.unavailable") from None
    finally:
        connection.close()


def mutate_platform_brand(
    config: AppConfig,
    command: PlatformBrandCommand,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    expected_revision: int,
    request_id: str,
    trace_id: str,
) -> PlatformBrandResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        permission = (
            "platform.brand.release"
            if command.action in {"release", "suspend", "rollback_lkg"}
            else "platform.brand.manage"
        )
        actor = _platform_actor(connection, config, session_token, csrf_token, permission)
        row = _platform_row(connection)
        owner_id = str(row["platform_brand_id"])
        request_sha = _request_sha(command, expected_revision)
        replay = _command_replay(
            connection,
            config,
            actor_domain="platform",
            actor_id=actor.operator_id,
            owner_kind="platform",
            owner_id=owner_id,
            action=command.action,
            idempotency_key=idempotency_key,
            request_sha=request_sha,
            conflict_code="platform.idempotency_conflict",
        )
        if replay:
            result = _platform_response(
                connection, row, actor, request_id, trace_id, idempotent_replay=True
            )
            connection.commit()
            return result
        _expect_revision(row, expected_revision)
        now = _utcnow()
        consume_global_step_up_grant_in_transaction(
            connection,
            config,
            account_id=actor.account_id,
            global_session_id=actor.global_session_id,
            grant=step_up_grant,
            action_code=f"brand.platform.{command.action}",
            resource_type="platform_brand",
            resource_id=owner_id,
            now=now,
        )
        challenge = _apply_platform_action(
            connection,
            config,
            row,
            command,
            actor,
            idempotency_key=idempotency_key,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        current = _platform_row(connection)
        _record_command(
            connection,
            config,
            actor_domain="platform",
            actor_id=actor.operator_id,
            owner_kind="platform",
            owner_id=owner_id,
            action=command.action,
            idempotency_key=idempotency_key,
            request_sha=request_sha,
            expected_revision=expected_revision,
            result_revision=int(current["revision"]),
            result_generation=int(current["published_generation"]),
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        result = _platform_response(
            connection, current, actor, request_id, trace_id, challenge=challenge
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        _raise_integrity(exc, platform=True)
    except (sqlite3.Error, DbError, OSError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("branding.unavailable") from None
    finally:
        connection.close()


def get_channel_brand(
    config: AppConfig,
    *,
    session_token: str,
    channel_partner_id: str,
    request_id: str,
    trace_id: str,
) -> ChannelBrandResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _platform_actor(
            connection, config, session_token, None, "platform.channel_brand.read"
        )
        partner, scope_revision = _channel_owner(
            connection, actor, channel_partner_id, require_active=False
        )
        row = _channel_brand_row(connection, channel_partner_id)
        if row is None:
            row = _synthetic_channel_brand(partner, actor.operator_id)
        result = _channel_response(
            connection,
            row,
            partner,
            actor,
            scope_revision,
            request_id,
            trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("branding.unavailable") from None
    finally:
        connection.close()


def mutate_channel_brand(
    config: AppConfig,
    command: ChannelBrandCommand,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    expected_revision: int,
    channel_partner_id: str,
    request_id: str,
    trace_id: str,
) -> ChannelBrandResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        permission = (
            "platform.channel_brand.release"
            if command.action in {"release", "suspend", "rollback_lkg"}
            else "platform.channel_brand.manage"
        )
        actor = _platform_actor(connection, config, session_token, csrf_token, permission)
        partner, scope_revision = _channel_owner(
            connection,
            actor,
            channel_partner_id,
            require_active=command.action not in {"suspend", "rollback_lkg"},
        )
        row = _channel_brand_row(connection, channel_partner_id)
        if row is None:
            row = _insert_channel_brand(connection, partner, actor.operator_id)
        owner_id = str(row["channel_brand_id"])
        request_sha = _request_sha(command, expected_revision)
        replay = _command_replay(
            connection,
            config,
            actor_domain="platform",
            actor_id=actor.operator_id,
            owner_kind="channel",
            owner_id=owner_id,
            action=command.action,
            idempotency_key=idempotency_key,
            request_sha=request_sha,
            conflict_code="platform.idempotency_conflict",
        )
        if replay:
            result = _channel_response(
                connection,
                row,
                partner,
                actor,
                scope_revision,
                request_id,
                trace_id,
                idempotent_replay=True,
            )
            connection.commit()
            return result
        _expect_revision(row, expected_revision)
        now = _utcnow()
        consume_global_step_up_grant_in_transaction(
            connection,
            config,
            account_id=actor.account_id,
            global_session_id=actor.global_session_id,
            grant=step_up_grant,
            action_code=f"brand.channel.{command.action}",
            resource_type="channel_brand",
            resource_id=owner_id,
            now=now,
        )
        challenge = _apply_channel_action(
            connection,
            config,
            row,
            partner,
            command,
            actor,
            idempotency_key=idempotency_key,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        current = _required_channel_brand_row(connection, channel_partner_id)
        _record_command(
            connection,
            config,
            actor_domain="platform",
            actor_id=actor.operator_id,
            owner_kind="channel",
            owner_id=owner_id,
            action=command.action,
            idempotency_key=idempotency_key,
            request_sha=request_sha,
            expected_revision=expected_revision,
            result_revision=int(current["revision"]),
            result_generation=int(current["published_generation"]),
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        result = _channel_response(
            connection,
            current,
            partner,
            actor,
            scope_revision,
            request_id,
            trace_id,
            challenge=challenge,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        _raise_integrity(exc, platform=True)
    except (sqlite3.Error, DbError, OSError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("branding.unavailable") from None
    finally:
        connection.close()


def get_deployment_brand(
    config: AppConfig,
    *,
    session_token: str,
    edge_deployment_id: str,
    request_id: str,
    trace_id: str,
) -> DeploymentBrandResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _workspace_actor(
            connection, config, session_token, None, "deployment.branding.read"
        )
        deployment = _deployment_owner(connection, actor, edge_deployment_id)
        row = _deployment_brand_row(connection, edge_deployment_id)
        if row is None:
            row = _synthetic_deployment_brand(deployment, actor.account_id)
        result = _deployment_response(connection, row, actor, request_id, trace_id)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("branding.unavailable") from None
    finally:
        connection.close()


def mutate_deployment_brand(
    config: AppConfig,
    command: DeploymentBrandCommand,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    expected_revision: int,
    edge_deployment_id: str,
    request_id: str,
    trace_id: str,
) -> DeploymentBrandResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _workspace_actor(
            connection, config, session_token, csrf_token, "deployment.branding.manage"
        )
        deployment = _deployment_owner(connection, actor, edge_deployment_id)
        row = _deployment_brand_row(connection, edge_deployment_id)
        if row is None:
            row = _insert_deployment_brand(connection, deployment, actor.account_id)
        owner_id = str(row["deployment_brand_id"])
        request_sha = _request_sha(command, expected_revision)
        replay = _command_replay(
            connection,
            config,
            actor_domain="workspace",
            actor_id=actor.membership_id,
            owner_kind="deployment",
            owner_id=owner_id,
            action=command.action,
            idempotency_key=idempotency_key,
            request_sha=request_sha,
            conflict_code="workspace.idempotency_conflict",
        )
        if replay:
            result = _deployment_response(
                connection,
                row,
                actor,
                request_id,
                trace_id,
                idempotent_replay=True,
            )
            connection.commit()
            return result
        _expect_revision(row, expected_revision)
        now = _utcnow()
        consume_step_up_grant_in_transaction(
            connection,
            config,
            context=actor,
            grant=step_up_grant,
            action_code=f"brand.deployment.{command.action}",
            resource_type="deployment_brand",
            resource_id=owner_id,
            now=now,
        )
        _apply_deployment_action(
            connection,
            config,
            row,
            command,
            actor,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        current = _required_deployment_brand_row(connection, edge_deployment_id)
        _record_command(
            connection,
            config,
            actor_domain="workspace",
            actor_id=actor.membership_id,
            owner_kind="deployment",
            owner_id=owner_id,
            action=command.action,
            idempotency_key=idempotency_key,
            request_sha=request_sha,
            expected_revision=expected_revision,
            result_revision=int(current["revision"]),
            result_generation=int(current["desired_generation"]),
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        result = _deployment_response(connection, current, actor, request_id, trace_id)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        _raise_integrity(exc, platform=False)
    except (sqlite3.Error, DbError, OSError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("branding.unavailable") from None
    finally:
        connection.close()


def get_public_brand(
    config: AppConfig,
    *,
    canonical_host: str,
    request_id: str,
    trace_id: str,
) -> PublicBrandResponse:
    connection = _connect(config)
    try:
        host = canonicalize_request_host(canonical_host)
        release = _public_release(connection, host)
        info = _public_info(release)
        return PublicBrandResponse(branding=info, request_id=request_id, trace_id=trace_id)
    except (ApiError, sqlite3.Error, DbError, OSError, KeyError, TypeError, ValueError):
        try:
            release = _platform_lkg(connection)
            return PublicBrandResponse(
                branding=_public_info(release), request_id=request_id, trace_id=trace_id
            )
        except Exception:
            etag = _sha(_canonical_json({**_DEFAULT, "generation": 1}).encode("utf-8"))
            return PublicBrandResponse(
                branding=PublicBrandInfo(
                    **_DEFAULT,
                    logo_path=None,
                    favicon_path=None,
                    brand_generation=1,
                    public_etag=f'"brand-{etag}"',
                ),
                request_id=request_id,
                trace_id=trace_id,
            )
    finally:
        connection.close()


def get_public_asset(config: AppConfig, *, canonical_host: str, asset_id: str) -> PublicAsset:
    if _ID.fullmatch(asset_id) is None or not asset_id.startswith("bas_"):
        raise ApiError("branding.logo_not_configured")
    connection = _connect(config)
    try:
        release = _public_release(connection, canonicalize_request_host(canonical_host))
        if asset_id not in {release["logo_asset_id"], release["favicon_asset_id"]}:
            raise ApiError("branding.logo_not_configured")
        row = connection.execute(
            "SELECT * FROM brand_assets WHERE brand_asset_id=? AND status='published'",
            (asset_id,),
        ).fetchone()
        if row is None:
            raise ApiError("branding.logo_not_configured")
        body = _asset_path(config, str(row["object_key"])).read_bytes()
        body, width, height, digest = validate_brand_png(body)
        if (
            digest != row["content_sha256"]
            or len(body) != row["byte_count"]
            or width != row["width"]
            or height != row["height"]
        ):
            raise ApiError("branding.logo_not_configured")
        return PublicAsset(body=body, etag=f'"sha256-{digest}"')
    except ApiError:
        raise
    except (sqlite3.Error, DbError, OSError, ValueError, TypeError):
        raise ApiError("branding.logo_not_configured") from None
    finally:
        connection.close()


def scan_brand_asset_once(config: AppConfig) -> bool:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM brand_assets WHERE status='staged' "
            "ORDER BY created_at,brand_asset_id LIMIT 1"
        ).fetchone()
        if row is None:
            connection.commit()
            return False
        status = "clean"
        try:
            body = _asset_path(config, str(row["object_key"])).read_bytes()
            body, width, height, digest = validate_brand_png(body)
            if (
                len(body) != row["byte_count"]
                or width != row["width"]
                or height != row["height"]
                or digest != row["content_sha256"]
            ):
                status = "rejected"
        except (ApiError, OSError):
            status = "rejected"
            digest = _sha(b"brand-asset-scan-rejected")
        scan_digest = _sha(f"bounded-png-v1:{status}:{digest}".encode("ascii"))
        stamp = _db_time(_utcnow())
        connection.execute(
            "UPDATE brand_assets SET status=?,scan_result_sha256=?,scanned_at=? "
            "WHERE brand_asset_id=? AND status='staged'",
            (status, scan_digest, stamp, row["brand_asset_id"]),
        )
        connection.commit()
        return True
    except (sqlite3.Error, DbError, OSError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("branding.unavailable") from None
    finally:
        connection.close()


def record_domain_observation(
    config: AppConfig,
    *,
    verified_domain_id: str,
    challenge_generation: int,
    resolver_code: str,
    observation_sha256: str,
    dnssec_valid: bool,
    certificate_ready: bool,
    routing_ready: bool,
    request_id: str = "brand-domain-scheduler",
    trace_id: str = "brand-domain-scheduler",
) -> BrandDomainSummary:
    if resolver_code not in {"resolver_a", "resolver_b"} or not _is_sha(observation_sha256):
        raise ApiError("branding.custom_domain_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM brand_verified_domains WHERE verified_domain_id=?",
            (verified_domain_id,),
        ).fetchone()
        if row is None or int(row["challenge_generation"]) != challenge_generation:
            raise ApiError("branding.not_found")
        existing = connection.execute(
            "SELECT * FROM brand_domain_observations WHERE verified_domain_id=? "
            "AND challenge_generation=? AND resolver_code=?",
            (verified_domain_id, challenge_generation, resolver_code),
        ).fetchone()
        if existing is not None and existing["observation_sha256"] != observation_sha256:
            stamp = _db_time(_utcnow())
            connection.execute(
                "UPDATE brand_verified_domains SET status='reverification_required',"
                "certificate_ready=0,routing_ready=0,revision=revision+1,updated_at=? "
                "WHERE verified_domain_id=? AND revision=?",
                (stamp, verified_domain_id, row["revision"]),
            )
            connection.commit()
            return _domain_summary(
                connection,
                connection.execute(
                    "SELECT * FROM brand_verified_domains WHERE verified_domain_id=?",
                    (verified_domain_id,),
                ).fetchone(),
            )
        if existing is None:
            connection.execute(
                """
                INSERT INTO brand_domain_observations(
                  brand_domain_observation_id,verified_domain_id,challenge_generation,
                  resolver_code,observation_sha256,dnssec_valid,observed_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    _id("bdo"),
                    verified_domain_id,
                    challenge_generation,
                    resolver_code,
                    observation_sha256,
                    int(dnssec_valid),
                    _db_time(_utcnow()),
                ),
            )
        observation_state = connection.execute(
            "SELECT count(*) AS observation_count,"
            "count(DISTINCT observation_sha256) AS digest_count "
            "FROM brand_domain_observations WHERE verified_domain_id=? "
            "AND challenge_generation=? AND dnssec_valid=1",
            (verified_domain_id, challenge_generation),
        ).fetchone()
        count = int(observation_state["observation_count"])
        digest_count = int(observation_state["digest_count"])
        status = "pending"
        if count >= 2 and digest_count != 1:
            status = "reverification_required"
            certificate_ready = False
            routing_ready = False
        elif count >= 2:
            status = "active" if certificate_ready and routing_ready else "verified"
        verified_count = count if digest_count == 1 else 0
        first_verified = row["status"] not in {"verified", "active"} and status in {
            "verified",
            "active",
        }
        stamp = _db_time(_utcnow())
        connection.execute(
            """
            UPDATE brand_verified_domains
            SET observation_count=?,status=?,certificate_ready=?,routing_ready=?,
              revision=revision+1,last_verified_at=CASE WHEN ?>=2 THEN ? ELSE last_verified_at END,
              expires_at=CASE WHEN ?>=2 THEN ? ELSE expires_at END,updated_at=?
            WHERE verified_domain_id=? AND revision=?
            """,
            (
                min(count, 2),
                status,
                int(certificate_ready),
                int(routing_ready),
                verified_count,
                stamp,
                verified_count,
                _db_time(_utcnow() + timedelta(days=1)),
                stamp,
                verified_domain_id,
                row["revision"],
            ),
        )
        current = connection.execute(
            "SELECT * FROM brand_verified_domains WHERE verified_domain_id=?",
            (verified_domain_id,),
        ).fetchone()
        if first_verified:
            owner_id = _domain_owner_id(current)
            _write_event(
                connection,
                event_type="brand_domain.verified",
                owner_kind=str(current["owner_kind"]),
                owner_id=owner_id,
                object_id=verified_domain_id,
                status=status,
                revision=int(current["revision"]),
                generation=challenge_generation,
                config_sha=None,
                asset_sha=None,
                domain_sha=_sha(str(current["canonical_domain"]).encode("ascii")),
                actor_domain="scheduler",
                actor_id="brand-domain-verifier",
                request_id=request_id,
                trace_id=trace_id,
                now=_utcnow(),
            )
        connection.commit()
        return _domain_summary(connection, current)
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("branding.unavailable") from None
    finally:
        connection.close()


def record_deployment_brand_ack(
    config: AppConfig,
    *,
    edge_deployment_id: str,
    generation: int,
    manifest_sha256: str,
    accepted: bool,
) -> bool:
    if generation < 1 or not _is_sha(manifest_sha256):
        raise ApiError("branding.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        bundle = connection.execute(
            "SELECT * FROM deployment_brand_bundles WHERE edge_deployment_id=? "
            "AND generation=? AND manifest_sha256=?",
            (edge_deployment_id, generation, manifest_sha256),
        ).fetchone()
        brand = _required_deployment_brand_row(connection, edge_deployment_id)
        if bundle is None:
            raise ApiError("branding.not_found")
        status = "applied" if accepted else "rejected"
        if generation < int(brand["applied_generation"]):
            status = "stale"
        ack_id = _stable_id("dba", f"{edge_deployment_id}:{generation}")
        connection.execute(
            """
            INSERT INTO deployment_brand_acks(
              deployment_brand_ack_id,edge_deployment_id,deployment_brand_bundle_id,
              applied_generation,lkg_generation,manifest_sha256,status,observed_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING
            """,
            (
                ack_id,
                edge_deployment_id,
                bundle["deployment_brand_bundle_id"],
                generation,
                max(1, int(brand["lkg_generation"]), generation if accepted else 1),
                manifest_sha256,
                status,
                _db_time(_utcnow()),
            ),
        )
        if status == "applied" and generation >= int(brand["applied_generation"]):
            connection.execute(
                "UPDATE deployment_brands SET applied_generation=?,lkg_generation=?,"
                "revision=revision+1,updated_at=? WHERE edge_deployment_id=? AND revision=?",
                (
                    generation,
                    generation,
                    _db_time(_utcnow()),
                    edge_deployment_id,
                    brand["revision"],
                ),
            )
            connection.execute(
                "UPDATE deployment_brand_bundles SET status='applied',updated_at=? "
                "WHERE deployment_brand_bundle_id=? AND status='available'",
                (_db_time(_utcnow()), bundle["deployment_brand_bundle_id"]),
            )
        connection.commit()
        return status == "applied"
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("branding.unavailable") from None
    finally:
        connection.close()


def canonicalize_request_host(value: object) -> str:
    if type(value) is not str or not value or any(character.isspace() for character in value):
        raise ApiError("branding.request_invalid")
    host = value.rstrip(".").lower()
    if host.startswith("["):
        if "]:" in host:
            host = host[1 : host.index("]")]
        elif host.endswith("]"):
            host = host[1:-1]
    elif host.count(":") == 1:
        name, port = host.rsplit(":", 1)
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ApiError("branding.request_invalid")
        host = name
    if not host or len(host) > 253:
        raise ApiError("branding.request_invalid")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        raise ApiError("branding.request_invalid") from None
    if any(_DOMAIN_LABEL.fullmatch(label) is None for label in ascii_host.split(".")):
        raise ApiError("branding.request_invalid")
    return ascii_host


def _apply_platform_action(
    connection: sqlite3.Connection,
    config: AppConfig,
    row: sqlite3.Row,
    command: PlatformBrandCommand,
    actor: PlatformContext,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> str | None:
    if command.action in {"update", "attach_asset", "remove_asset", "request_domain"}:
        draft = _mutable(row)
        challenge = _apply_draft_action(
            connection,
            config,
            draft,
            command,
            owner_kind="platform",
            owner_id=str(row["platform_brand_id"]),
            actor_id=actor.operator_id,
            idempotency_key=idempotency_key,
            now=now,
        )
        _update_brand_row(
            connection,
            "platform_brands",
            "platform_brand_id",
            str(row["platform_brand_id"]),
            draft,
            actor_column="updated_by_operator_id",
            actor_id=actor.operator_id,
            status="draft",
            now=now,
        )
        return challenge
    if command.action == "release":
        _release_public_brand(
            connection,
            row,
            owner_kind="platform",
            actor_id=actor.operator_id,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
            require_domain=False,
        )
    elif command.action == "suspend":
        _suspend_public_brand(
            connection,
            row,
            owner_kind="platform",
            actor_id=actor.operator_id,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
    else:
        _rollback_public_brand(
            connection,
            row,
            owner_kind="platform",
            actor_id=actor.operator_id,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
    return None


def _apply_channel_action(
    connection: sqlite3.Connection,
    config: AppConfig,
    row: sqlite3.Row,
    partner: sqlite3.Row,
    command: ChannelBrandCommand,
    actor: PlatformContext,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> str | None:
    if command.action in {"update", "attach_asset", "remove_asset", "request_domain"}:
        draft = _mutable(row)
        challenge = _apply_draft_action(
            connection,
            config,
            draft,
            command,
            owner_kind="channel",
            owner_id=str(row["channel_brand_id"]),
            actor_id=actor.operator_id,
            idempotency_key=idempotency_key,
            now=now,
        )
        _update_brand_row(
            connection,
            "channel_brands",
            "channel_brand_id",
            str(row["channel_brand_id"]),
            draft,
            actor_column="updated_by_operator_id",
            actor_id=actor.operator_id,
            status="pending_domain" if command.action == "request_domain" else "draft",
            extra={
                "contract_sha256": str(partner["contract_sha256"]),
                "contract_revision": int(partner["revision"]),
            },
            now=now,
        )
        return challenge
    if str(partner["status"]) != "active":
        raise ApiError("channel.partner_not_found")
    if row["contract_sha256"] != partner["contract_sha256"] or int(row["contract_revision"]) != int(
        partner["revision"]
    ):
        raise ApiError("branding.revision_conflict")
    if command.action == "release":
        _release_public_brand(
            connection,
            row,
            owner_kind="channel",
            actor_id=actor.operator_id,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
            require_domain=True,
        )
    elif command.action == "suspend":
        _suspend_public_brand(
            connection,
            row,
            owner_kind="channel",
            actor_id=actor.operator_id,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
    else:
        _rollback_public_brand(
            connection,
            row,
            owner_kind="channel",
            actor_id=actor.operator_id,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
    return None


def _apply_deployment_action(
    connection: sqlite3.Connection,
    config: AppConfig,
    row: sqlite3.Row,
    command: DeploymentBrandCommand,
    actor: WorkspaceSessionContext,
    *,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    owner_id = str(row["deployment_brand_id"])
    if command.action in {"update", "attach_asset", "remove_asset"}:
        draft = _mutable(row)
        _apply_draft_action(
            connection,
            config,
            draft,
            command,
            owner_kind="deployment",
            owner_id=owner_id,
            actor_id=actor.account_id,
            idempotency_key="deployment-no-domain",
            now=now,
        )
        _update_brand_row(
            connection,
            "deployment_brands",
            "deployment_brand_id",
            owner_id,
            draft,
            actor_column="updated_by_account_id",
            actor_id=actor.account_id,
            status="draft",
            now=now,
        )
        return
    if command.action == "publish":
        _publish_deployment_brand(
            connection,
            config,
            row,
            actor,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
    elif command.action == "suspend":
        generation = int(row["desired_generation"]) + 1
        _insert_release(
            connection,
            row,
            owner_kind="deployment",
            generation=generation,
            status="suspended",
            operator_id=None,
            account_id=actor.account_id,
            now=now,
        )
        connection.execute(
            "UPDATE deployment_brands SET status='suspended',desired_generation=?,"
            "revision=revision+1,updated_by_account_id=?,updated_at=? "
            "WHERE deployment_brand_id=? AND revision=?",
            (generation, actor.account_id, _db_time(now), owner_id, row["revision"]),
        )
        _write_brand_changed(
            connection,
            row,
            owner_kind="deployment",
            status="suspended",
            revision=int(row["revision"]) + 1,
            generation=generation,
            actor_domain="workspace",
            actor_id=actor.membership_id,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
    else:
        _rollback_deployment_brand(
            connection,
            config,
            row,
            actor,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )


def _apply_draft_action(
    connection: sqlite3.Connection,
    config: AppConfig,
    row: sqlite3.Row,
    command: object,
    *,
    owner_kind: str,
    owner_id: str,
    actor_id: str,
    idempotency_key: str,
    now: datetime,
) -> str | None:
    action = str(command.action)
    if action == "update":
        changed = False
        for field in _VISUAL_FIELDS:
            if field in command.model_fields_set:  # type: ignore[attr-defined]
                value = getattr(command, field)
                _validate_visual(field, value)
                changed = changed or row[field] != value
                _set_row_value(row, field, value)
        if not changed:
            raise ApiError("branding.request_invalid")
        return None
    if action == "attach_asset":
        asset_id = _stage_asset(
            connection,
            config,
            owner_kind=owner_kind,
            owner_id=owner_id,
            asset_kind=str(command.asset_kind),
            encoded=command.png_base64,
            now=now,
        )
        _set_row_value(row, f"draft_{command.asset_kind}_asset_id", asset_id)
        return None
    if action == "remove_asset":
        if row[f"draft_{command.asset_kind}_asset_id"] is None:
            raise ApiError("branding.request_invalid")
        _set_row_value(row, f"draft_{command.asset_kind}_asset_id", None)
        return None
    if action == "request_domain":
        challenge, domain_id = _request_domain(
            connection,
            config,
            owner_kind=owner_kind,
            owner_id=owner_id,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            domain=command.domain,
            now=now,
        )
        _set_row_value(row, "draft_domain_id", domain_id)
        return challenge
    return None


def _update_brand_row(
    connection: sqlite3.Connection,
    table: str,
    id_column: str,
    owner_id: str,
    row: sqlite3.Row,
    *,
    actor_column: str,
    actor_id: str,
    status: str,
    now: datetime,
    extra: dict[str, object] | None = None,
) -> None:
    values = {field: row[field] for field in _VISUAL_FIELDS}
    values.update(
        {
            "draft_logo_asset_id": row["draft_logo_asset_id"],
            "draft_favicon_asset_id": row["draft_favicon_asset_id"],
        }
    )
    if "draft_domain_id" in row.keys():
        values["draft_domain_id"] = row["draft_domain_id"]
    values.update(extra or {})
    values["status"] = status
    values["source_sha256"] = _source_sha(values)
    values[actor_column] = actor_id
    values["revision"] = int(row["revision"]) + 1
    values["updated_at"] = _db_time(now)
    assignments = ",".join(f"{name}=?" for name in values)
    changed = connection.execute(
        f"UPDATE {table} SET {assignments} WHERE {id_column}=? AND revision=?",
        (*values.values(), owner_id, row["revision"]),
    ).rowcount
    if changed != 1:
        raise ApiError("branding.revision_conflict")


def _release_public_brand(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    owner_kind: str,
    actor_id: str,
    request_id: str,
    trace_id: str,
    now: datetime,
    require_domain: bool,
) -> None:
    if row["updated_by_operator_id"] == actor_id:
        raise ApiError("platform.permission_denied")
    _require_assets_ready(connection, row, owner_kind)
    domain = _release_domain(connection, row, owner_kind, required=require_domain)
    generation = int(row["published_generation"]) + 1
    _insert_release(
        connection,
        row,
        owner_kind=owner_kind,
        generation=generation,
        status="released",
        operator_id=actor_id,
        account_id=None,
        now=now,
    )
    id_column = "platform_brand_id" if owner_kind == "platform" else "channel_brand_id"
    table = "platform_brands" if owner_kind == "platform" else "channel_brands"
    old_generation = int(row["published_generation"])
    lkg = generation if old_generation == 0 else old_generation
    changed = connection.execute(
        f"UPDATE {table} SET status='released',published_generation=?,lkg_generation=?,"
        "revision=revision+1,updated_by_operator_id=?,updated_at=? "
        f"WHERE {id_column}=? AND revision=?",
        (generation, lkg, actor_id, _db_time(now), row[id_column], row["revision"]),
    ).rowcount
    if changed != 1:
        raise ApiError("branding.revision_conflict")
    _publish_assets(connection, row, now)
    connection.execute(
        "UPDATE brand_public_heads SET status='revoked',cache_epoch=cache_epoch+1,updated_at=? "
        f"WHERE owner_kind=? AND {id_column}=? AND status!='revoked'",
        (_db_time(now), owner_kind, row[id_column]),
    )
    if domain is not None:
        release_id = _stable_id("brl", f"{owner_kind}:{row[id_column]}:{generation}")
        head_id = _stable_id("bph", f"{domain['canonical_domain']}:{generation}")
        platform_id = row[id_column] if owner_kind == "platform" else None
        channel_id = row[id_column] if owner_kind == "channel" else None
        connection.execute(
            """
            INSERT INTO brand_public_heads(
              brand_public_head_id,canonical_host,owner_kind,platform_brand_id,
              channel_brand_id,brand_release_id,generation,status,cache_epoch,updated_at)
            VALUES(?,?,?,?,?,?,?,'active',?,?)
            ON CONFLICT(canonical_host) DO UPDATE SET
              owner_kind=excluded.owner_kind,platform_brand_id=excluded.platform_brand_id,
              channel_brand_id=excluded.channel_brand_id,brand_release_id=excluded.brand_release_id,
              generation=excluded.generation,status='active',
              cache_epoch=brand_public_heads.cache_epoch+1,updated_at=excluded.updated_at
            """,
            (
                head_id,
                domain["canonical_domain"],
                owner_kind,
                platform_id,
                channel_id,
                release_id,
                generation,
                generation,
                _db_time(now),
            ),
        )
    _write_brand_changed(
        connection,
        row,
        owner_kind=owner_kind,
        status="released",
        revision=int(row["revision"]) + 1,
        generation=generation,
        actor_domain="platform",
        actor_id=actor_id,
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )


def _suspend_public_brand(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    owner_kind: str,
    actor_id: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    generation = int(row["published_generation"]) + 1
    _insert_release(
        connection,
        row,
        owner_kind=owner_kind,
        generation=generation,
        status="suspended",
        operator_id=actor_id,
        account_id=None,
        now=now,
    )
    table = "platform_brands" if owner_kind == "platform" else "channel_brands"
    id_column = "platform_brand_id" if owner_kind == "platform" else "channel_brand_id"
    connection.execute(
        f"UPDATE {table} SET status='suspended',published_generation=?,revision=revision+1,"
        f"updated_by_operator_id=?,updated_at=? WHERE {id_column}=? AND revision=?",
        (generation, actor_id, _db_time(now), row[id_column], row["revision"]),
    )
    connection.execute(
        "UPDATE brand_public_heads SET status='revoked',cache_epoch=cache_epoch+1,updated_at=? "
        f"WHERE owner_kind=? AND {id_column}=? AND status!='revoked'",
        (_db_time(now), owner_kind, row[id_column]),
    )
    _write_brand_changed(
        connection,
        row,
        owner_kind=owner_kind,
        status="suspended",
        revision=int(row["revision"]) + 1,
        generation=generation,
        actor_domain="platform",
        actor_id=actor_id,
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )


def _rollback_public_brand(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    owner_kind: str,
    actor_id: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    lkg = int(row["lkg_generation"])
    if lkg < 1 or lkg == int(row["published_generation"]):
        raise ApiError("branding.revision_conflict")
    source = _release_row(connection, owner_kind, _brand_id(row, owner_kind), lkg)
    generation = int(row["published_generation"]) + 1
    _insert_release_from_release(
        connection,
        source,
        owner_kind=owner_kind,
        owner_id=_brand_id(row, owner_kind),
        generation=generation,
        operator_id=actor_id,
        account_id=None,
        now=now,
    )
    table = "platform_brands" if owner_kind == "platform" else "channel_brands"
    id_column = "platform_brand_id" if owner_kind == "platform" else "channel_brand_id"
    assignments = ",".join(f"{field}=?" for field in _VISUAL_FIELDS)
    connection.execute(
        f"UPDATE {table} SET {assignments},draft_logo_asset_id=?,draft_favicon_asset_id=?,"
        "draft_domain_id=?,status='released',published_generation=?,revision=revision+1,"
        f"updated_by_operator_id=?,updated_at=? WHERE {id_column}=? AND revision=?",
        (
            *(source[field] for field in _VISUAL_FIELDS),
            source["logo_asset_id"],
            source["favicon_asset_id"],
            source["verified_domain_id"],
            generation,
            actor_id,
            _db_time(now),
            row[id_column],
            row["revision"],
        ),
    )
    connection.execute(
        "UPDATE brand_public_heads SET brand_release_id=?,generation=?,status='active',"
        "cache_epoch=cache_epoch+1,updated_at=? "
        f"WHERE owner_kind=? AND {id_column}=?",
        (
            _stable_id("brl", f"{owner_kind}:{row[id_column]}:{generation}"),
            generation,
            _db_time(now),
            owner_kind,
            row[id_column],
        ),
    )
    _write_brand_changed(
        connection,
        source,
        owner_kind=owner_kind,
        status="released",
        revision=int(row["revision"]) + 1,
        generation=generation,
        actor_domain="platform",
        actor_id=actor_id,
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )


def _publish_deployment_brand(
    connection: sqlite3.Connection,
    config: AppConfig,
    row: sqlite3.Row,
    actor: WorkspaceSessionContext,
    *,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    _require_assets_ready(connection, row, "deployment")
    generation = int(row["desired_generation"]) + 1
    release_id = _insert_release(
        connection,
        row,
        owner_kind="deployment",
        generation=generation,
        status="published",
        operator_id=None,
        account_id=actor.account_id,
        now=now,
    )
    asset_sha = _combined_asset_sha(connection, row)
    unsigned = {
        "schema_version": 1,
        "workspace_id": row["workspace_id"],
        "tenant_id": row["tenant_id"],
        "edge_deployment_id": row["edge_deployment_id"],
        "deployment_brand_id": row["deployment_brand_id"],
        "generation": generation,
        "visual": {field: row[field] for field in _VISUAL_FIELDS},
        "logo_asset_id": row["draft_logo_asset_id"],
        "favicon_asset_id": row["draft_favicon_asset_id"],
        "asset_sha256": asset_sha,
        "expires_at": _db_time(now + timedelta(days=30)),
    }
    unsigned_json = _canonical_json(unsigned)
    signing_secret = config.edge_snapshot_signing_secret or config.auth_token_secret
    signature = hmac.new(
        signing_secret.encode("utf-8"), unsigned_json.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    manifest_json = _canonical_json({**unsigned, "signature": signature})
    manifest_sha = _sha(manifest_json.encode("utf-8"))
    bundle_id = _stable_id("dbb", f"{row['edge_deployment_id']}:{generation}")
    connection.execute(
        """
        INSERT INTO deployment_brand_bundles(
          deployment_brand_bundle_id,workspace_id,tenant_id,edge_deployment_id,
          deployment_brand_id,brand_release_id,generation,manifest_json,manifest_sha256,
          signature_sha256,status,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,'available',?,?)
        """,
        (
            bundle_id,
            row["workspace_id"],
            row["tenant_id"],
            row["edge_deployment_id"],
            row["deployment_brand_id"],
            release_id,
            generation,
            manifest_json,
            manifest_sha,
            _sha(signature.encode("ascii")),
            _db_time(now),
            _db_time(now),
        ),
    )
    source_sha = _source_sha(
        {
            **{field: row[field] for field in _VISUAL_FIELDS},
            "draft_logo_asset_id": row["draft_logo_asset_id"],
            "draft_favicon_asset_id": row["draft_favicon_asset_id"],
            "status": "published",
        }
    )
    visual_assignments = ",".join(f"{field}=?" for field in _VISUAL_FIELDS)
    connection.execute(
        f"UPDATE deployment_brands SET {visual_assignments},draft_logo_asset_id=?,"
        "draft_favicon_asset_id=?,status='published',desired_generation=?,lkg_generation=?,"
        "source_sha256=?,revision=revision+1,updated_by_account_id=?,updated_at=? "
        "WHERE deployment_brand_id=? AND revision=?",
        (
            *(row[field] for field in _VISUAL_FIELDS),
            row["draft_logo_asset_id"],
            row["draft_favicon_asset_id"],
            generation,
            int(row["lkg_generation"]),
            source_sha,
            actor.account_id,
            _db_time(now),
            row["deployment_brand_id"],
            row["revision"],
        ),
    )
    _publish_assets(connection, row, now)
    _write_brand_changed(
        connection,
        row,
        owner_kind="deployment",
        status="published",
        revision=int(row["revision"]) + 1,
        generation=generation,
        actor_domain="workspace",
        actor_id=actor.membership_id,
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )


def _rollback_deployment_brand(
    connection: sqlite3.Connection,
    config: AppConfig,
    row: sqlite3.Row,
    actor: WorkspaceSessionContext,
    *,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    lkg = int(row["lkg_generation"])
    if lkg < 1 or lkg == int(row["desired_generation"]):
        raise ApiError("branding.revision_conflict")
    source = _release_row(connection, "deployment", str(row["deployment_brand_id"]), lkg)
    draft = _mutable(row)
    for field in _VISUAL_FIELDS:
        _set_row_value(draft, field, source[field])
    _set_row_value(draft, "draft_logo_asset_id", source["logo_asset_id"])
    _set_row_value(draft, "draft_favicon_asset_id", source["favicon_asset_id"])
    _publish_deployment_brand(
        connection,
        config,
        draft,
        actor,
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )


def _request_domain(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    owner_kind: str,
    owner_id: str,
    actor_id: str,
    idempotency_key: str,
    domain: object,
    now: datetime,
) -> tuple[str, str]:
    canonical = _canonical_domain(domain)
    challenge = _challenge(config, owner_kind, owner_id, actor_id, idempotency_key)
    challenge_hmac = _hmac(config, "brand-domain-challenge", challenge)
    columns = {
        "platform": (owner_id, None, None),
        "channel": (None, owner_id, None),
        "deployment": (None, None, owner_id),
    }[owner_kind]
    existing = connection.execute(
        "SELECT * FROM brand_verified_domains WHERE canonical_domain=?", (canonical,)
    ).fetchone()
    if existing is not None:
        if _domain_owner_id(existing) != owner_id or existing["owner_kind"] != owner_kind:
            raise ApiError("branding.custom_domain_conflict")
        generation = int(existing["challenge_generation"]) + 1
        connection.execute(
            """
            UPDATE brand_verified_domains SET challenge_hmac=?,challenge_generation=?,
              observation_count=0,status='pending',certificate_ready=0,routing_ready=0,
              revision=revision+1,expires_at=NULL,last_verified_at=NULL,updated_at=?
            WHERE verified_domain_id=? AND revision=?
            """,
            (
                challenge_hmac,
                generation,
                _db_time(now),
                existing["verified_domain_id"],
                existing["revision"],
            ),
        )
        return challenge, str(existing["verified_domain_id"])
    domain_id = _id("vdo")
    connection.execute(
        """
        INSERT INTO brand_verified_domains(
          verified_domain_id,owner_kind,platform_brand_id,channel_brand_id,
          deployment_brand_id,canonical_domain,challenge_hmac,challenge_generation,
          observation_count,status,certificate_ready,routing_ready,revision,expires_at,
          last_verified_at,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,1,0,'pending',0,0,1,NULL,NULL,?,?)
        """,
        (
            domain_id,
            owner_kind,
            *columns,
            canonical,
            challenge_hmac,
            _db_time(now),
            _db_time(now),
        ),
    )
    return challenge, domain_id


def _stage_asset(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    owner_kind: str,
    owner_id: str,
    asset_kind: str,
    encoded: object,
    now: datetime,
) -> str:
    if type(encoded) is not str or encoded.startswith("data:"):
        raise ApiError("branding.logo_invalid")
    try:
        body = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error):
        raise ApiError("branding.logo_invalid") from None
    body, width, height, digest = validate_brand_png(body)
    asset_id = _id("bas")
    object_key = f"brand-assets/{asset_id}.png"
    target = _asset_path(config, object_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            target.unlink(missing_ok=True)
        finally:
            raise
    owner_values = {
        "platform": (owner_id, None, None),
        "channel": (None, owner_id, None),
        "deployment": (None, None, owner_id),
    }[owner_kind]
    connection.execute(
        """
        INSERT INTO brand_assets(
          brand_asset_id,owner_kind,platform_brand_id,channel_brand_id,deployment_brand_id,
          asset_kind,mime_type,object_key,byte_count,width,height,content_sha256,
          scan_result_sha256,status,content_revision,created_at,scanned_at,published_at)
        VALUES(?,?,?,?,?,?,'image/png',?,?,?,?,?,NULL,'staged',1,?,NULL,NULL)
        """,
        (
            asset_id,
            owner_kind,
            *owner_values,
            asset_kind,
            object_key,
            len(body),
            width,
            height,
            digest,
            _db_time(now),
        ),
    )
    return asset_id


def _insert_release(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    owner_kind: str,
    generation: int,
    status: str,
    operator_id: str | None,
    account_id: str | None,
    now: datetime,
) -> str:
    owner_id = _brand_id(row, owner_kind)
    release_id = _stable_id("brl", f"{owner_kind}:{owner_id}:{generation}")
    owners = {
        "platform": (owner_id, None, None),
        "channel": (None, owner_id, None),
        "deployment": (None, None, owner_id),
    }[owner_kind]
    domain_id = row["draft_domain_id"] if "draft_domain_id" in row.keys() else None
    config_sha = _source_sha(
        {
            **{field: row[field] for field in _VISUAL_FIELDS},
            "logo": row["draft_logo_asset_id"],
            "favicon": row["draft_favicon_asset_id"],
            "domain": domain_id,
        }
    )
    asset_sha = _combined_asset_sha(connection, row)
    domain_sha = None
    if domain_id:
        domain = connection.execute(
            "SELECT canonical_domain FROM brand_verified_domains WHERE verified_domain_id=?",
            (domain_id,),
        ).fetchone()
        if domain is not None:
            domain_sha = _sha(str(domain[0]).encode("ascii"))
    connection.execute(
        """
        INSERT INTO brand_releases(
          brand_release_id,owner_kind,platform_brand_id,channel_brand_id,deployment_brand_id,
          generation,status,product_name,primary_color,secondary_color,login_title,
          login_message,logo_asset_id,favicon_asset_id,verified_domain_id,config_sha256,
          asset_sha256,domain_sha256,released_by_operator_id,published_by_account_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            release_id,
            owner_kind,
            *owners,
            generation,
            status,
            *(row[field] for field in _VISUAL_FIELDS),
            row["draft_logo_asset_id"],
            row["draft_favicon_asset_id"],
            domain_id,
            config_sha,
            asset_sha,
            domain_sha,
            operator_id,
            account_id,
            _db_time(now),
        ),
    )
    return release_id


def _insert_release_from_release(
    connection: sqlite3.Connection,
    source: sqlite3.Row,
    *,
    owner_kind: str,
    owner_id: str,
    generation: int,
    operator_id: str | None,
    account_id: str | None,
    now: datetime,
) -> str:
    release_id = _stable_id("brl", f"{owner_kind}:{owner_id}:{generation}")
    owners = {
        "platform": (owner_id, None, None),
        "channel": (None, owner_id, None),
        "deployment": (None, None, owner_id),
    }[owner_kind]
    connection.execute(
        """
        INSERT INTO brand_releases(
          brand_release_id,owner_kind,platform_brand_id,channel_brand_id,deployment_brand_id,
          generation,status,product_name,primary_color,secondary_color,login_title,
          login_message,logo_asset_id,favicon_asset_id,verified_domain_id,config_sha256,
          asset_sha256,domain_sha256,released_by_operator_id,published_by_account_id,created_at)
        VALUES(?,?,?,?,?,?,'lkg',?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            release_id,
            owner_kind,
            *owners,
            generation,
            *(source[field] for field in _VISUAL_FIELDS),
            source["logo_asset_id"],
            source["favicon_asset_id"],
            source["verified_domain_id"],
            source["config_sha256"],
            source["asset_sha256"],
            source["domain_sha256"],
            operator_id,
            account_id,
            _db_time(now),
        ),
    )
    return release_id


def _write_brand_changed(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    owner_kind: str,
    status: str,
    revision: int,
    generation: int,
    actor_domain: str,
    actor_id: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    event_type = {
        "platform": "platform_brand.changed",
        "channel": "channel_brand.changed",
        "deployment": "deployment_brand.changed",
    }[owner_kind]
    _write_event(
        connection,
        event_type=event_type,
        owner_kind=owner_kind,
        owner_id=_brand_id(row, owner_kind),
        object_id=_brand_id(row, owner_kind),
        status=status,
        revision=revision,
        generation=generation,
        config_sha=_source_sha({field: row[field] for field in _VISUAL_FIELDS}),
        asset_sha=_combined_asset_sha(connection, row),
        domain_sha=_domain_sha(connection, row),
        actor_domain=actor_domain,
        actor_id=actor_id,
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )


def _write_event(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    owner_kind: str,
    owner_id: str,
    object_id: str,
    status: str,
    revision: int,
    generation: int,
    config_sha: str | None,
    asset_sha: str | None,
    domain_sha: str | None,
    actor_domain: str,
    actor_id: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    payload = {
        "schema_version": 1,
        "object_id": object_id,
        "owner_kind": owner_kind,
        "status": status,
        "revision": revision,
        "generation": generation,
        "config_sha256": config_sha,
        "asset_sha256": asset_sha,
        "domain_sha256": domain_sha,
        "occurred_at": _db_time(now),
    }
    payload_json = _canonical_json(payload)
    event_id = _stable_id("bre", f"{event_type}:{owner_kind}:{owner_id}:{generation}")
    connection.execute(
        """
        INSERT INTO brand_events(
          brand_event_id,event_type,owner_kind,owner_id,object_id,status,revision,generation,
          config_sha256,asset_sha256,domain_sha256,payload_json,payload_sha256,actor_domain,
          actor_id,request_id,trace_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            event_type,
            owner_kind,
            owner_id,
            object_id,
            status,
            revision,
            generation,
            config_sha,
            asset_sha,
            domain_sha,
            payload_json,
            _sha(payload_json.encode("utf-8")),
            actor_domain,
            actor_id,
            request_id,
            trace_id,
            _db_time(now),
        ),
    )
    connection.execute(
        """
        INSERT INTO brand_event_outbox(
          brand_event_outbox_id,brand_event_id,status,attempt_count,generation,
          next_attempt_at,created_at,updated_at)
        VALUES(?,?,'pending',0,0,?,?,?)
        """,
        (
            _stable_id("beo", event_id),
            event_id,
            _db_time(now),
            _db_time(now),
            _db_time(now),
        ),
    )


def _record_command(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    actor_domain: str,
    actor_id: str,
    owner_kind: str,
    owner_id: str,
    action: str,
    idempotency_key: str,
    request_sha: str,
    expected_revision: int,
    result_revision: int,
    result_generation: int,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO brand_commands(
          brand_command_id,actor_domain,actor_id,owner_kind,owner_id,action_code,
          idempotency_hmac,request_sha256,expected_revision,result_revision,
          result_generation,request_id,trace_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("bco"),
            actor_domain,
            actor_id,
            owner_kind,
            owner_id,
            action,
            _idempotency_hmac(config, idempotency_key),
            request_sha,
            expected_revision,
            result_revision,
            result_generation,
            request_id,
            trace_id,
            _db_time(now),
        ),
    )


def _command_replay(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    actor_domain: str,
    actor_id: str,
    owner_kind: str,
    owner_id: str,
    action: str,
    idempotency_key: str,
    request_sha: str,
    conflict_code: str,
) -> bool:
    row = connection.execute(
        """
        SELECT action_code,request_sha256 FROM brand_commands
        WHERE actor_domain=? AND actor_id=? AND owner_kind=? AND owner_id=?
          AND idempotency_hmac=?
        """,
        (
            actor_domain,
            actor_id,
            owner_kind,
            owner_id,
            _idempotency_hmac(config, idempotency_key),
        ),
    ).fetchone()
    if row is None:
        return False
    if row["action_code"] != action or not hmac.compare_digest(
        str(row["request_sha256"]), request_sha
    ):
        raise ApiError(conflict_code)
    return True


def _platform_response(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    actor: PlatformContext,
    request_id: str,
    trace_id: str,
    *,
    challenge: str | None = None,
    idempotent_replay: bool = False,
) -> PlatformBrandResponse:
    actions: list[str] = []
    if "platform.brand.manage" in actor.permissions:
        actions.extend(("update", "attach_asset", "remove_asset", "request_domain"))
    if "platform.brand.release" in actor.permissions:
        actions.extend(("release", "suspend", "rollback_lkg"))
    return PlatformBrandResponse(
        branding=PlatformBrandInfo(
            platform_brand_id=str(row["platform_brand_id"]),
            status=str(row["status"]),
            **{field: str(row[field]) for field in _VISUAL_FIELDS},
            logo=_asset_summary(connection, row["draft_logo_asset_id"]),
            favicon=_asset_summary(connection, row["draft_favicon_asset_id"]),
            domain=_domain_for_brand(connection, row["draft_domain_id"], challenge),
            published_generation=int(row["published_generation"]),
            lkg_generation=int(row["lkg_generation"]),
            revision=int(row["revision"]),
            source_sha256=str(row["source_sha256"]),
            actions=tuple(actions),
            updated_at=_parse_time(row["updated_at"]),
        ),
        request_id=request_id,
        trace_id=trace_id,
        idempotent_replay=idempotent_replay,
    )


def _channel_response(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    partner: sqlite3.Row,
    actor: PlatformContext,
    scope_revision: int | None,
    request_id: str,
    trace_id: str,
    *,
    challenge: str | None = None,
    idempotent_replay: bool = False,
) -> ChannelBrandResponse:
    actions: list[str] = []
    active = str(partner["status"]) == "active"
    if active and "platform.channel_brand.manage" in actor.permissions:
        actions.extend(("update", "attach_asset", "remove_asset", "request_domain"))
    if "platform.channel_brand.release" in actor.permissions:
        actions.extend(("release", "suspend", "rollback_lkg"))
    elif not active and "platform.channel_brand.manage" in actor.permissions:
        actions.append("suspend")
    return ChannelBrandResponse(
        branding=ChannelBrandInfo(
            channel_brand_id=str(row["channel_brand_id"]),
            status=str(row["status"]),
            **{field: str(row[field]) for field in _VISUAL_FIELDS},
            logo=_asset_summary(connection, row["draft_logo_asset_id"]),
            favicon=_asset_summary(connection, row["draft_favicon_asset_id"]),
            domain=_domain_for_brand(connection, row["draft_domain_id"], challenge),
            contract_status=str(partner["status"]),
            contract_sha256=str(partner["contract_sha256"]),
            contract_revision=int(partner["revision"]),
            partner_scope_revision=scope_revision,
            published_generation=int(row["published_generation"]),
            lkg_generation=int(row["lkg_generation"]),
            revision=int(row["revision"]),
            source_sha256=str(row["source_sha256"]),
            actions=tuple(actions),
            updated_at=_parse_time(row["updated_at"]),
        ),
        request_id=request_id,
        trace_id=trace_id,
        idempotent_replay=idempotent_replay,
    )


def _deployment_response(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    actor: WorkspaceSessionContext,
    request_id: str,
    trace_id: str,
    *,
    idempotent_replay: bool = False,
) -> DeploymentBrandResponse:
    actions = (
        ("update", "attach_asset", "remove_asset", "publish", "suspend", "rollback_lkg")
        if "deployment.branding.manage" in actor.permission_codes
        else ()
    )
    ack = connection.execute(
        "SELECT status FROM deployment_brand_acks WHERE edge_deployment_id=? "
        "ORDER BY applied_generation DESC LIMIT 1",
        (row["edge_deployment_id"],),
    ).fetchone()
    return DeploymentBrandResponse(
        branding=DeploymentBrandInfo(
            deployment_brand_id=str(row["deployment_brand_id"]),
            status=str(row["status"]),
            **{field: str(row[field]) for field in _VISUAL_FIELDS},
            logo=_asset_summary(connection, row["draft_logo_asset_id"]),
            favicon=_asset_summary(connection, row["draft_favicon_asset_id"]),
            desired_generation=int(row["desired_generation"]),
            applied_generation=int(row["applied_generation"]),
            lkg_generation=int(row["lkg_generation"]),
            revision=int(row["revision"]),
            source_sha256=str(row["source_sha256"]),
            ack_status=str(ack[0]) if ack is not None else None,
            actions=actions,
            updated_at=_parse_time(row["updated_at"]),
        ),
        request_id=request_id,
        trace_id=trace_id,
        idempotent_replay=idempotent_replay,
    )


def _public_release(connection: sqlite3.Connection, host: str) -> sqlite3.Row:
    head = connection.execute(
        """
        SELECT release.* FROM brand_public_heads head
        JOIN brand_releases release ON release.brand_release_id=head.brand_release_id
        LEFT JOIN channel_brands brand ON brand.channel_brand_id=head.channel_brand_id
        LEFT JOIN channel_partners partner ON partner.channel_partner_id=brand.channel_partner_id
        WHERE head.canonical_host=? AND head.status='active'
          AND (head.owner_kind='platform' OR(
            head.owner_kind='channel' AND brand.status='released' AND partner.status='active'))
        """,
        (host,),
    ).fetchone()
    if head is not None and _release_assets_available(connection, head):
        return head
    return _platform_lkg(connection)


def _platform_lkg(connection: sqlite3.Connection) -> sqlite3.Row:
    brand = _platform_row(connection)
    generation = (
        int(brand["published_generation"])
        if brand["status"] == "released"
        else int(brand["lkg_generation"])
    )
    row = _release_row(connection, "platform", str(brand["platform_brand_id"]), generation)
    if not _release_assets_available(connection, row):
        row = _release_row(
            connection, "platform", str(brand["platform_brand_id"]), int(brand["lkg_generation"])
        )
    return row


def _public_info(release: sqlite3.Row) -> PublicBrandInfo:
    digest = _sha(
        _canonical_json(
            {
                **{field: release[field] for field in _VISUAL_FIELDS},
                "logo": release["logo_asset_id"],
                "favicon": release["favicon_asset_id"],
                "generation": release["generation"],
            }
        ).encode("utf-8")
    )
    return PublicBrandInfo(
        **{field: str(release[field]) for field in _VISUAL_FIELDS},
        logo_path=(
            f"/branding/public/assets/{release['logo_asset_id']}"
            if release["logo_asset_id"]
            else None
        ),
        favicon_path=(
            f"/branding/public/assets/{release['favicon_asset_id']}"
            if release["favicon_asset_id"]
            else None
        ),
        brand_generation=int(release["generation"]),
        public_etag=f'"brand-{digest}"',
    )


def _platform_actor(
    connection: sqlite3.Connection,
    config: AppConfig,
    token: str,
    csrf: str | None,
    permission: str,
) -> PlatformContext:
    actor = authenticate_platform_session_in_transaction(
        connection, config, session_token=token, csrf_token=csrf, touch=True
    )
    if permission not in actor.permissions:
        raise ApiError("platform.permission_denied")
    return actor


def _workspace_actor(
    connection: sqlite3.Connection,
    config: AppConfig,
    token: str,
    csrf: str | None,
    permission: str,
) -> WorkspaceSessionContext:
    actor = authenticate_workspace_session_in_transaction(
        connection, config, session_token=token, csrf_token=csrf, touch=True
    )
    if permission not in actor.permission_codes:
        raise ApiError("workspace_session.permission_denied")
    return actor


def _channel_owner(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    channel_partner_id: str,
    *,
    require_active: bool,
) -> tuple[sqlite3.Row, int | None]:
    if _ID.fullmatch(channel_partner_id) is None or not channel_partner_id.startswith("par_"):
        raise ApiError("channel.partner_not_found")
    partner = connection.execute(
        "SELECT * FROM channel_partners WHERE channel_partner_id=?", (channel_partner_id,)
    ).fetchone()
    if partner is None or (require_active and partner["status"] != "active"):
        raise ApiError("channel.partner_not_found")
    if "platform_owner" in actor.roles or "release_manager" in actor.roles:
        return partner, None
    if "platform_channel" not in actor.roles:
        raise ApiError("channel.partner_not_found")
    scope = connection.execute(
        "SELECT revision FROM platform_channel_partner_scopes "
        "WHERE operator_id=? AND channel_partner_id=? AND status='active'",
        (actor.operator_id, channel_partner_id),
    ).fetchone()
    if scope is None:
        raise ApiError("channel.partner_not_found")
    return partner, int(scope["revision"])


def _deployment_owner(
    connection: sqlite3.Connection,
    actor: WorkspaceSessionContext,
    edge_deployment_id: str,
) -> sqlite3.Row:
    if _ID.fullmatch(edge_deployment_id) is None or not edge_deployment_id.startswith("edp_"):
        raise ApiError("edge.deployment_not_found")
    row = connection.execute(
        "SELECT * FROM edge_deployments WHERE edge_deployment_id=? AND workspace_id=? "
        "AND tenant_id=? AND status='active'",
        (edge_deployment_id, actor.workspace_id, actor.tenant_id),
    ).fetchone()
    if row is None:
        raise ApiError("edge.deployment_not_found")
    return row


def _platform_row(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM platform_brands WHERE singleton_id=1").fetchone()
    if row is None:
        raise ApiError("branding.unavailable")
    return row


def _channel_brand_row(
    connection: sqlite3.Connection, channel_partner_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM channel_brands WHERE channel_partner_id=?", (channel_partner_id,)
    ).fetchone()


def _required_channel_brand_row(
    connection: sqlite3.Connection, channel_partner_id: str
) -> sqlite3.Row:
    row = _channel_brand_row(connection, channel_partner_id)
    if row is None:
        raise ApiError("branding.not_found")
    return row


def _deployment_brand_row(
    connection: sqlite3.Connection, edge_deployment_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM deployment_brands WHERE edge_deployment_id=?", (edge_deployment_id,)
    ).fetchone()


def _required_deployment_brand_row(
    connection: sqlite3.Connection, edge_deployment_id: str
) -> sqlite3.Row:
    row = _deployment_brand_row(connection, edge_deployment_id)
    if row is None:
        raise ApiError("branding.not_found")
    return row


def _synthetic_channel_brand(partner: sqlite3.Row, actor_id: str) -> dict[str, object]:
    now = _db_time(_utcnow())
    values: dict[str, object] = {
        "channel_brand_id": _stable_id("cbr", str(partner["channel_partner_id"])),
        "channel_partner_id": partner["channel_partner_id"],
        "owner_workspace_id": partner["owner_workspace_id"],
        "tenant_id": partner["tenant_id"],
        "contract_sha256": partner["contract_sha256"],
        "contract_revision": partner["revision"],
        "status": "draft",
        **_DEFAULT,
        "draft_logo_asset_id": None,
        "draft_favicon_asset_id": None,
        "draft_domain_id": None,
        "published_generation": 0,
        "lkg_generation": 0,
        "revision": 1,
        "updated_by_operator_id": actor_id,
        "created_at": now,
        "updated_at": now,
    }
    values["source_sha256"] = _source_sha(values)
    return values


def _insert_channel_brand(
    connection: sqlite3.Connection, partner: sqlite3.Row, actor_id: str
) -> sqlite3.Row:
    values = _synthetic_channel_brand(partner, actor_id)
    columns = tuple(values)
    connection.execute(
        f"INSERT INTO channel_brands({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
        tuple(values[column] for column in columns),
    )
    return _required_channel_brand_row(connection, str(partner["channel_partner_id"]))


def _synthetic_deployment_brand(deployment: sqlite3.Row, actor_id: str) -> dict[str, object]:
    now = _db_time(_utcnow())
    values: dict[str, object] = {
        "deployment_brand_id": _stable_id("dbr", str(deployment["edge_deployment_id"])),
        "workspace_id": deployment["workspace_id"],
        "tenant_id": deployment["tenant_id"],
        "edge_deployment_id": deployment["edge_deployment_id"],
        "status": "draft",
        **_DEFAULT,
        "draft_logo_asset_id": None,
        "draft_favicon_asset_id": None,
        "desired_generation": 0,
        "applied_generation": 0,
        "lkg_generation": 0,
        "revision": 1,
        "updated_by_account_id": actor_id,
        "created_at": now,
        "updated_at": now,
    }
    values["source_sha256"] = _source_sha(values)
    return values


def _insert_deployment_brand(
    connection: sqlite3.Connection, deployment: sqlite3.Row, actor_id: str
) -> sqlite3.Row:
    values = _synthetic_deployment_brand(deployment, actor_id)
    columns = tuple(values)
    placeholders = ",".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO deployment_brands({','.join(columns)}) VALUES({placeholders})",
        tuple(values[column] for column in columns),
    )
    return _required_deployment_brand_row(connection, str(deployment["edge_deployment_id"]))


def _release_row(
    connection: sqlite3.Connection, owner_kind: str, owner_id: str, generation: int
) -> sqlite3.Row:
    column = {
        "platform": "platform_brand_id",
        "channel": "channel_brand_id",
        "deployment": "deployment_brand_id",
    }[owner_kind]
    row = connection.execute(
        f"SELECT * FROM brand_releases WHERE owner_kind=? AND {column}=? AND generation=?",
        (owner_kind, owner_id, generation),
    ).fetchone()
    if row is None:
        raise ApiError("branding.unavailable")
    return row


def _asset_summary(connection: sqlite3.Connection, asset_id: object) -> BrandAssetSummary | None:
    if asset_id is None:
        return None
    row = connection.execute(
        "SELECT * FROM brand_assets WHERE brand_asset_id=?", (asset_id,)
    ).fetchone()
    if row is None:
        raise ApiError("branding.unavailable")
    return BrandAssetSummary(
        asset_id=str(row["brand_asset_id"]),
        kind=str(row["asset_kind"]),
        status=str(row["status"]),
        byte_count=int(row["byte_count"]),
        width=int(row["width"]),
        height=int(row["height"]),
        content_sha256=str(row["content_sha256"]),
    )


def _domain_for_brand(
    connection: sqlite3.Connection, domain_id: object, challenge: str | None
) -> BrandDomainSummary | None:
    if domain_id is None:
        return None
    row = connection.execute(
        "SELECT * FROM brand_verified_domains WHERE verified_domain_id=?", (domain_id,)
    ).fetchone()
    if row is None:
        raise ApiError("branding.unavailable")
    return _domain_summary(connection, row, challenge)


def _domain_summary(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    challenge: str | None = None,
) -> BrandDomainSummary:
    del connection
    return BrandDomainSummary(
        domain_id=str(row["verified_domain_id"]),
        canonical_domain=str(row["canonical_domain"]),
        status=str(row["status"]),
        observation_count=int(row["observation_count"]),
        certificate_ready=bool(row["certificate_ready"]),
        routing_ready=bool(row["routing_ready"]),
        revision=int(row["revision"]),
        challenge=challenge,
    )


def _release_domain(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    owner_kind: str,
    *,
    required: bool,
) -> sqlite3.Row | None:
    domain_id = row["draft_domain_id"]
    if domain_id is None:
        if required:
            raise ApiError("branding.custom_domain_invalid")
        return None
    domain = connection.execute(
        "SELECT * FROM brand_verified_domains WHERE verified_domain_id=?",
        (domain_id,),
    ).fetchone()
    if (
        domain is None
        or domain["owner_kind"] != owner_kind
        or _domain_owner_id(domain) != _brand_id(row, owner_kind)
        or domain["status"] != "active"
        or not domain["certificate_ready"]
        or not domain["routing_ready"]
    ):
        raise ApiError("branding.custom_domain_invalid")
    return domain


def _require_assets_ready(
    connection: sqlite3.Connection, row: sqlite3.Row, owner_kind: str
) -> None:
    owner_id = _brand_id(row, owner_kind)
    owner_column = {
        "platform": "platform_brand_id",
        "channel": "channel_brand_id",
        "deployment": "deployment_brand_id",
    }[owner_kind]
    for column in ("draft_logo_asset_id", "draft_favicon_asset_id"):
        asset_id = row[column]
        if asset_id is None:
            continue
        asset = connection.execute(
            f"SELECT 1 FROM brand_assets WHERE brand_asset_id=? AND owner_kind=? "
            f"AND {owner_column}=? AND status IN('clean','published')",
            (asset_id, owner_kind, owner_id),
        ).fetchone()
        if asset is None:
            raise ApiError("branding.logo_invalid")


def _publish_assets(connection: sqlite3.Connection, row: sqlite3.Row, now: datetime) -> None:
    for column in ("draft_logo_asset_id", "draft_favicon_asset_id"):
        if row[column] is not None:
            connection.execute(
                "UPDATE brand_assets SET status='published',published_at=? "
                "WHERE brand_asset_id=? AND status IN('clean','published')",
                (_db_time(now), row[column]),
            )


def _combined_asset_sha(connection: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    digests: list[str] = []
    logo_column = "logo_asset_id" if "logo_asset_id" in row.keys() else "draft_logo_asset_id"
    favicon_column = (
        "favicon_asset_id" if "favicon_asset_id" in row.keys() else "draft_favicon_asset_id"
    )
    for column in (logo_column, favicon_column):
        if row[column] is None:
            continue
        asset = connection.execute(
            "SELECT content_sha256 FROM brand_assets WHERE brand_asset_id=?", (row[column],)
        ).fetchone()
        if asset is None:
            raise ApiError("branding.unavailable")
        digests.append(str(asset[0]))
    return _sha("|".join(digests).encode("ascii")) if digests else None


def _domain_sha(connection: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    column = "verified_domain_id" if "verified_domain_id" in row.keys() else "draft_domain_id"
    if column not in row.keys() or row[column] is None:
        return None
    domain = connection.execute(
        "SELECT canonical_domain FROM brand_verified_domains WHERE verified_domain_id=?",
        (row[column],),
    ).fetchone()
    return _sha(str(domain[0]).encode("ascii")) if domain is not None else None


def _release_assets_available(connection: sqlite3.Connection, row: sqlite3.Row) -> bool:
    for column in ("logo_asset_id", "favicon_asset_id"):
        if row[column] is None:
            continue
        asset = connection.execute(
            "SELECT status FROM brand_assets WHERE brand_asset_id=?", (row[column],)
        ).fetchone()
        if asset is None or asset[0] != "published":
            return False
    return True


def _canonical_domain(value: object) -> str:
    if type(value) is not str or not value or value.startswith("*."):
        raise ApiError("branding.custom_domain_invalid")
    if any(character.isspace() for character in value) or any(
        character in value for character in ("/", "\\", ":", "@", "_")
    ):
        raise ApiError("branding.custom_domain_invalid")
    try:
        domain = value.rstrip(".").lower().encode("idna").decode("ascii")
    except UnicodeError:
        raise ApiError("branding.custom_domain_invalid") from None
    if not 4 <= len(domain) <= 253 or "." not in domain:
        raise ApiError("branding.custom_domain_invalid")
    try:
        ipaddress.ip_address(domain)
        raise ApiError("branding.custom_domain_invalid")
    except ValueError:
        pass
    if domain in _PUBLIC_SUFFIXES or any(
        domain == suffix[1:] or domain.endswith(suffix) for suffix in _RESERVED_SUFFIXES
    ):
        raise ApiError("branding.custom_domain_invalid")
    if any(_DOMAIN_LABEL.fullmatch(label) is None for label in domain.split(".")):
        raise ApiError("branding.custom_domain_invalid")
    return domain


def _validate_visual(field: str, value: object) -> None:
    if type(value) is not str or not value:
        raise ApiError("branding.request_invalid")
    if field in {"primary_color", "secondary_color"}:
        if _COLOR.fullmatch(value) is None:
            raise ApiError("branding.request_invalid")
        return
    maximum = {"product_name": 80, "login_title": 120, "login_message": 240}[field]
    if len(value.encode("utf-8")) > maximum or any(
        ord(character) < 32 or 127 <= ord(character) <= 159 for character in value
    ):
        raise ApiError("branding.request_invalid")


def _expect_revision(row: object, expected: int) -> None:
    if type(expected) is not int or expected < 1 or int(row["revision"]) != expected:
        raise ApiError("branding.revision_conflict")


def _set_row_value(row: object, key: str, value: object) -> None:
    if isinstance(row, dict):
        row[key] = value
        return
    # sqlite.Row is immutable; draft actions operate on a private mutable projection.
    raise TypeError("brand row must be mutable")


def _mutable(row: sqlite3.Row | dict[str, object]) -> dict[str, object]:
    return dict(row) if not isinstance(row, dict) else row


def _brand_id(row: object, owner_kind: str) -> str:
    return str(
        row[f"{owner_kind}_brand_id"] if owner_kind != "deployment" else row["deployment_brand_id"]
    )


def _domain_owner_id(row: sqlite3.Row) -> str:
    return str(
        row[
            {
                "platform": "platform_brand_id",
                "channel": "channel_brand_id",
                "deployment": "deployment_brand_id",
            }[str(row["owner_kind"])]
        ]
    )


def _request_sha(command: object, expected_revision: int) -> str:
    payload = command.model_dump(mode="json", exclude_unset=True)  # type: ignore[attr-defined]
    return _sha(
        _canonical_json({"expected_revision": expected_revision, "payload": payload}).encode(
            "utf-8"
        )
    )


def _challenge(
    config: AppConfig, owner_kind: str, owner_id: str, actor_id: str, idempotency_key: str
) -> str:
    material = f"{owner_kind}\x1f{owner_id}\x1f{actor_id}\x1f{idempotency_key}".encode()
    token = hmac.new(
        config.auth_token_secret.encode("utf-8"),
        b"brand-domain-token-v1\x00" + material,
        hashlib.sha256,
    ).hexdigest()
    return f"metahub-brand={token}"


def _idempotency_hmac(config: AppConfig, key: str) -> str:
    return _hmac(config, "brand-idempotency-v1", key)


def _hmac(config: AppConfig, domain: str, value: str) -> str:
    return hmac.new(
        config.auth_token_secret.encode("utf-8"),
        domain.encode("ascii") + b"\x00" + value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _source_sha(values: dict[str, object]) -> str:
    return _sha(_canonical_json(values).encode("utf-8"))


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


def _stable_id(prefix: str, value: str) -> str:
    return f"{prefix}_{_sha(value.encode('utf-8'))[:32]}"


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha(value: object) -> bool:
    return type(value) is str and len(value) == 64 and not set(value) - set("0123456789abcdef")


def _asset_path(config: AppConfig, object_key: str) -> Path:
    if not object_key.startswith("brand-assets/") or ".." in object_key or "\\" in object_key:
        raise ApiError("branding.unavailable")
    database_url = config.database_url or ""
    if database_url.startswith("sqlite:///"):
        root = Path(database_url.removeprefix("sqlite:///")).resolve().parent / ".brand-assets"
    else:
        root = Path(".data/brand-assets").resolve()
    target = (root / object_key.removeprefix("brand-assets/")).resolve()
    if target.parent != root:
        raise ApiError("branding.unavailable")
    return target


def _platform_brand_id(row: sqlite3.Row) -> str:
    return str(row["platform_brand_id"])


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        connection = connect_database(config.database_url)
        if getattr(connection, "backend", "sqlite") == "sqlite":
            connection.execute("PRAGMA busy_timeout=5000")
        return connection
    except DbError:
        raise ApiError("branding.unavailable") from None


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    if type(value) is not str:
        raise ApiError("branding.unavailable")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _raise_integrity(exc: sqlite3.IntegrityError, *, platform: bool) -> None:
    message = str(exc)
    if "canonical_domain" in message or "brand_verified_domains" in message:
        raise ApiError("branding.custom_domain_conflict") from None
    if "brand_commands" in message:
        raise ApiError(
            "platform.idempotency_conflict" if platform else "workspace.idempotency_conflict"
        ) from None
    raise ApiError("branding.unavailable") from None


__all__ = [
    "PublicAsset",
    "canonicalize_request_host",
    "get_channel_brand",
    "get_deployment_brand",
    "get_platform_brand",
    "get_public_asset",
    "get_public_brand",
    "mutate_channel_brand",
    "mutate_deployment_brand",
    "mutate_platform_brand",
    "record_deployment_brand_ack",
    "record_domain_observation",
    "scan_brand_asset_once",
]
