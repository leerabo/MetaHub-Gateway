"""Platform-owned registration policy and bounded, single-use local image challenges."""

from __future__ import annotations

import base64
import hmac
import io
import secrets
from datetime import timedelta
from uuid import uuid4

from metahub_shared.db.schema import table_exists
from metahub_shared.runtime.errors import ApiError
from PIL import Image, ImageDraw, ImageFont

from .self_service import (
    _connect,
    _consume_rate_limits,
    _db_time,
    _secret_hmac,
    _utcnow,
)


def read_policy_in_transaction(connection, config):
    if not table_exists(connection, "registration_policy"):
        # Older schema retains its email-verification-only contract until migration.
        return {"email_verification": True, "human_verification": False, "revision": 0}
    row = connection.execute(
        "SELECT * FROM registration_policy WHERE environment=?", (config.environment,)
    ).fetchone()
    if row is None:
        raise ApiError("identity.unavailable")
    return {
        "email_verification": bool(row["email_verification"]) or config.environment == "prod",
        "human_verification": bool(row["human_verification"]),
        "revision": int(row["revision"]),
    }


def public_registration_policy(config):
    connection = _connect(config)
    try:
        policy = read_policy_in_transaction(connection, config)
        return {
            **policy,
            "email_delivery_available": _delivery_available(config),
            "local_registration": config.environment == "dev",
        }
    finally:
        connection.close()


def _delivery_available(config):
    return (
        config.identity_challenge_delivery_mode == "test_capture" and config.environment != "prod"
    )


def platform_registration_policy(
    config,
    *,
    session_token,
    csrf_token=None,
    payload=None,
    request_id,
    trace_id,
):
    from metahub_control.platform_access.service import (
        _audit,
        authenticate_platform_session_in_transaction,
    )

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE" if payload is not None else "BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
        )
        if "platform.operator.manage" not in actor.permissions:
            raise ApiError("platform.permission_denied")
        before = read_policy_in_transaction(connection, config)
        if payload is not None:
            if (
                type(payload["email_verification"]) is not bool
                or type(payload["human_verification"]) is not bool
                or type(payload["revision"]) is not int
            ):
                raise ApiError("platform.request_invalid")
            if not payload["email_verification"] and config.environment != "dev":
                raise ApiError("identity.local_registration_forbidden")
            if (
                payload["email_verification"]
                and not before["email_verification"]
                and not _delivery_available(config)
            ):
                raise ApiError("identity.delivery_unavailable")
            if before["revision"] != payload["revision"] or before["revision"] == 0:
                raise ApiError("platform.revision_conflict")
            result = connection.execute(
                "UPDATE registration_policy SET email_verification=?,human_verification=?,"
                "revision=revision+1 WHERE environment=? AND revision=?",
                (
                    int(payload["email_verification"]),
                    int(payload["human_verification"]),
                    config.environment,
                    payload["revision"],
                ),
            )
            if result.rowcount != 1:
                raise ApiError("platform.revision_conflict")
            _audit(
                connection,
                actor_domain="platform",
                actor_id=actor.operator_id,
                action="registration_policy.updated",
                object_type="registration_policy",
                object_id=config.environment,
                reason="registration_security_configuration",
                request_id=request_id,
                trace_id=trace_id,
                now=_utcnow(),
                before=before,
                after=read_policy_in_transaction(connection, config),
            )
        policy = read_policy_in_transaction(connection, config)
        connection.commit()
        return {
            **policy,
            "environment": config.environment,
            "email_delivery_available": _delivery_available(config),
            "request_id": request_id,
            "trace_id": trace_id,
        }
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def create_registration_captcha(config, *, device_token, client_host, user_agent):
    _consume_rate_limits(
        config,
        action="verification",
        primary_key=device_token,
        device_token=device_token,
        client_host=client_host,
        user_agent=user_agent,
    )
    challenge_id = f"cap_{uuid4().hex}"
    answer = "".join(secrets.choice("23456789") for _ in range(5))
    expires = _utcnow() + timedelta(minutes=3)
    image = Image.new("RGB", (180, 64), "#f1f5ef")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=34)
    for index, digit in enumerate(answer):
        draw.text(
            (12 + index * 32, 9 + secrets.randbelow(9)),
            digit,
            font=font,
            fill=secrets.choice(("#244638", "#39434b", "#4b3c46")),
        )
    for _ in range(5):
        draw.line(
            (
                secrets.randbelow(180),
                secrets.randbelow(64),
                secrets.randbelow(180),
                secrets.randbelow(64),
            ),
            fill="#9aaca0",
            width=1,
        )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM registration_captchas WHERE expires_at<?", (_db_time(_utcnow()),)
        )
        if connection.execute("SELECT count(*) FROM registration_captchas").fetchone()[0] >= 2000:
            raise ApiError("identity.rate_limited")
        connection.execute(
            "INSERT INTO registration_captchas(challenge_id,answer_hmac,device_hmac,expires_at)"
            " VALUES(?,?,?,?)",
            (
                challenge_id,
                _secret_hmac(config, challenge_id, answer),
                _secret_hmac(config, "captcha-device", device_token),
                _db_time(expires),
            ),
        )
        connection.commit()
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    return {
        "challenge_id": challenge_id,
        "expires_at": _db_time(expires),
        "image": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"),
    }


def consume_registration_captcha(config, *, challenge_id, answer, device_token):
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM registration_captchas WHERE challenge_id=?", (challenge_id,)
        ).fetchone()
        if (
            row is None
            or row["consumed"]
            or row["attempts"] >= 5
            or row["expires_at"] <= _db_time(_utcnow())
            or not hmac.compare_digest(
                row["device_hmac"], _secret_hmac(config, "captcha-device", device_token)
            )
        ):
            raise ApiError("identity.captcha_invalid")
        valid = hmac.compare_digest(row["answer_hmac"], _secret_hmac(config, challenge_id, answer))
        updated = connection.execute(
            "UPDATE registration_captchas SET attempts=attempts+1,consumed=?"
            " WHERE challenge_id=? AND consumed=0 AND attempts=?",
            (int(valid), challenge_id, row["attempts"]),
        )
        connection.commit()
        if not valid or updated.rowcount != 1:
            raise ApiError("identity.captcha_invalid")
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
