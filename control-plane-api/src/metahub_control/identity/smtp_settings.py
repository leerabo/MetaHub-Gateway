"""Platform-owned SMTP settings, encrypted at rest and resolved at delivery time."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import smtplib
import ssl
from datetime import timedelta
from email.headerregistry import Address
from email.message import EmailMessage

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from metahub_shared.db import connect_database
from metahub_shared.db.schema import table_exists
from metahub_shared.runtime.errors import ApiError


def _aad(config):
    return ("metahub/platform-smtp/v1/" + config.environment).encode()


def _cipher(config):
    key = hmac.new(
        config.identity_pii_encryption_secret.encode(),
        b"metahub/platform-smtp/encryption/v1",
        hashlib.sha256,
    ).digest()
    return AESGCM(key)


def _encrypt(config, settings):
    nonce = secrets.token_bytes(12)
    data = json.dumps(settings, ensure_ascii=False, separators=(",", ":")).encode()
    return (
        "v1:"
        + base64.b64encode(nonce + _cipher(config).encrypt(nonce, data, _aad(config))).decode()
    )


def _decrypt(config, value):
    try:
        if not value.startswith("v1:"):
            raise ValueError()
        data = base64.b64decode(value[3:], validate=True)
        return json.loads(_cipher(config).decrypt(data[:12], data[12:], _aad(config)))
    except Exception:
        raise ApiError("identity.delivery_unavailable") from None


def _environment_settings(config):
    return dict(
        enabled=config.identity_challenge_delivery_mode == "smtp",
        host=config.smtp_host,
        port=config.smtp_port,
        security=config.smtp_security,
        username=config.smtp_username,
        password=config.smtp_password,
        sender=config.smtp_sender,
        timeout_seconds=config.smtp_timeout_seconds,
    )


def _read(connection, config):
    row = connection.execute(
        "SELECT * FROM platform_smtp_settings WHERE environment=?", (config.environment,)
    ).fetchone()
    return (
        (_decrypt(config, row["settings_ciphertext"]), row)
        if row
        else (_environment_settings(config), None)
    )


def effective_delivery_config(config):
    # Old installations and lightweight transport tests retain environment config.
    if not getattr(config, "database_url", None):
        return config
    connection = connect_database(config.database_url)
    try:
        if not table_exists(connection, "platform_smtp_settings"):
            return config
        settings, row = _read(connection, config)
        if row is None:
            return config
        return config.model_copy(
            update={
                "identity_challenge_delivery_mode": "smtp" if settings["enabled"] else "disabled",
                **{
                    "smtp_" + name: settings[name]
                    for name in (
                        "host",
                        "port",
                        "security",
                        "username",
                        "password",
                        "sender",
                        "timeout_seconds",
                    )
                },
            }
        )
    finally:
        connection.close()


def _invalid(message):
    raise ApiError("platform.request_invalid", message=message)


def _checked(payload, previous):
    if (
        type(payload.get("enabled")) is not bool
        or type(payload.get("revision")) is not int
        or payload["revision"] < 0
    ):
        _invalid("启用状态或配置修订号无效。")
    result = {
        key: payload.get(key)
        for key in ("enabled", "host", "port", "security", "username", "sender", "timeout_seconds")
    }
    for name, limit in (("host", 253), ("username", 320), ("sender", 320)):
        value = result[name]
        if (
            type(value) is not str
            or not value.strip()
            or len(value) > limit
            or any(ord(c) < 32 for c in value)
        ):
            _invalid(
                {
                    "host": "请填写有效的 SMTP 服务器地址。",
                    "username": "请填写 SMTP 登录账号。",
                    "sender": "请填写有效的发件邮箱。",
                }[name]
            )
        result[name] = value.strip()
    if any(c in result["host"] for c in "/:@ ") or result["host"].startswith("."):
        _invalid("SMTP 服务器只需填写主机名，不要包含协议、端口或路径。")
    if type(result["port"]) is not int or not 1 <= result["port"] <= 65535:
        _invalid("SMTP 端口须为 1–65535 的整数。")
    if type(result["security"]) is not str or result["security"] not in {"starttls", "tls"}:
        _invalid("请选择 STARTTLS 或 TLS 加密连接。")
    if (result["port"], result["security"]) in {(465, "starttls"), (587, "tls")}:
        _invalid("端口与加密方式不匹配：465 请使用 TLS，587 请使用 STARTTLS。")
    if type(result["timeout_seconds"]) is not int or not 1 <= result["timeout_seconds"] <= 30:
        _invalid("连接超时须为 1–30 秒的整数。")
    try:
        address = Address(addr_spec=result["sender"])
        if not address.username or not address.domain:
            raise ValueError()
    except Exception:
        _invalid("请填写有效的发件邮箱地址。")
    password = payload.get("password", "")
    if (
        type(password) is not str
        or len(password.encode("utf-8", errors="surrogatepass")) > 2048
        or any(0xD800 <= ord(c) <= 0xDFFF for c in password)
        or "\x00" in password
    ):
        _invalid("SMTP 密码或授权码格式无效。")
    if not password and (
        result["host"] != previous["host"] or result["username"] != previous["username"]
    ):
        _invalid("首次配置或更换 SMTP 服务器、账号时，请重新填写密码或授权码。")
    result["password"] = password or previous["password"]
    if not result["password"]:
        _invalid("请填写 SMTP 密码或邮箱授权码。")
    return result


def _public(settings, row):
    return {
        **{
            key: settings[key]
            for key in (
                "enabled",
                "host",
                "port",
                "security",
                "username",
                "sender",
                "timeout_seconds",
            )
        },
        "password_set": bool(settings["password"]),
        "revision": int(row["revision"]) if row else 0,
        "source": "platform" if row else "environment",
        "updated_at": row["updated_at"] if row else None,
    }


def platform_smtp_settings(
    config, *, session_token, csrf_token=None, payload=None, test=None, request_id, trace_id
):
    from metahub_control.identity.self_service import _connect, _db_time, _utcnow
    from metahub_control.platform_access.service import (
        _audit,
        authenticate_platform_session_in_transaction,
    )

    connection = _connect(config)
    try:
        connection.execute(
            "BEGIN IMMEDIATE" if payload is not None or test is not None else "BEGIN"
        )
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=session_token, csrf_token=csrf_token
        )
        if "platform.operator.manage" not in actor.permissions:
            raise ApiError("platform.permission_denied")
        if not table_exists(connection, "platform_smtp_settings"):
            raise ApiError("identity.delivery_unavailable")
        settings, row = _read(connection, config)
        revision = int(row["revision"]) if row else 0
        now = _utcnow()
        if payload is not None:
            if type(payload.get("revision")) is not int or payload["revision"] != revision:
                raise ApiError("platform.revision_conflict")
            settings = _checked(payload, settings)
            encrypted = _encrypt(config, settings)
            if row:
                result = connection.execute(
                    "UPDATE platform_smtp_settings SET settings_ciphertext=?,revision=revision+1,"
                    "updated_at=?,updated_by=?,connection_tested_at=NULL,email_tested_at=NULL "
                    "WHERE environment=? AND revision=?",
                    (encrypted, _db_time(now), actor.operator_id, config.environment, revision),
                )
                if result.rowcount != 1:
                    raise ApiError("platform.revision_conflict")
            else:
                connection.execute(
                    "INSERT INTO platform_smtp_settings"
                    "(environment,settings_ciphertext,revision,updated_at,updated_by)"
                    " VALUES(?,?,1,?,?) ON CONFLICT(environment) DO NOTHING",
                    (config.environment, encrypted, _db_time(now), actor.operator_id),
                )
                if (
                    connection.execute(
                        "SELECT revision,settings_ciphertext FROM platform_smtp_settings "
                        "WHERE environment=?",
                        (config.environment,),
                    ).fetchone()["settings_ciphertext"]
                    != encrypted
                ):
                    raise ApiError("platform.revision_conflict")
            revision += 1
            _audit(
                connection,
                actor_domain="platform",
                actor_id=actor.operator_id,
                action="platform.smtp.updated",
                object_type="platform_smtp_settings",
                object_id=config.environment,
                reason="mail_configuration",
                request_id=request_id,
                trace_id=trace_id,
                now=now,
                before={"revision": revision - 1},
                after={"revision": revision, "enabled": settings["enabled"]},
            )
            settings, row = _read(connection, config)
        if test is not None:
            if type(test.get("revision")) is not int or test["revision"] != revision:
                raise ApiError("platform.revision_conflict")
            if row is None or not settings["password"]:
                _invalid("请先保存 SMTP 配置，再进行测试。")
            kind = test.get("kind")
            if type(kind) is not str or kind not in {"connection", "email"}:
                _invalid("请选择连接测试或测试邮件。")
            target = test.get("recipient")
            if kind == "email":
                from metahub_control.identity.service import normalize_global_email

                if type(target) is not str or normalize_global_email(target) is None:
                    _invalid("请填写有效的测试收件邮箱。")
            elif target is not None:
                _invalid("连接测试不需要收件人。")
            column = "connection_tested_at" if kind == "connection" else "email_tested_at"
            updated = connection.execute(
                f"UPDATE platform_smtp_settings SET {column}=? WHERE environment=? AND revision=?"
                f" AND ({column} IS NULL OR {column}<?)",
                (
                    _db_time(now),
                    config.environment,
                    revision,
                    _db_time(now - timedelta(seconds=10)),
                ),
            )
            if updated.rowcount != 1:
                raise ApiError("identity.rate_limited")
            _audit(
                connection,
                actor_domain="platform",
                actor_id=actor.operator_id,
                action="platform.smtp.test_requested",
                object_type="platform_smtp_settings",
                object_id=config.environment,
                reason="mail_" + kind + "_test",
                request_id=request_id,
                trace_id=trace_id,
                now=now,
                after={"revision": revision, "kind": kind},
            )
        connection.commit()
        result = _public(settings, row)
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()
    if test is not None:
        result["test"] = _test_smtp(settings, test["kind"], test.get("recipient"))
    return {**result, "request_id": request_id, "trace_id": trace_id}


def _test_smtp(settings, kind, recipient):
    if (settings["port"], settings["security"]) in {(465, "starttls"), (587, "tls")}:
        return {
            "status": "failed", "kind": kind, "stage": "configuration",
            "message": "端口与加密方式不匹配：465 请使用 TLS，587 请使用 STARTTLS。修改后请先保存配置。",
        }
    stage = "connection"
    try:
        context = ssl.create_default_context()
        options = dict(
            host=settings["host"], port=settings["port"], timeout=settings["timeout_seconds"]
        )
        transport = (
            smtplib.SMTP_SSL(**options, context=context)
            if settings["security"] == "tls"
            else smtplib.SMTP(**options)
        )
        with transport as client:
            stage = "tls"
            client.ehlo()
            if settings["security"] == "starttls":
                client.starttls(context=context)
                client.ehlo()
            stage = "authentication"
            client.login(settings["username"], settings["password"])
            if kind == "email":
                stage = "delivery"
                message = EmailMessage()
                message["From"] = settings["sender"]
                message["To"] = recipient
                message["Subject"] = "MetaHub 邮件服务测试"
                message.set_content(
                    "这是一封由 SaaS 管理员主动发送的测试邮件。"
                    "收到此邮件表示当前发信配置已完成一次投递测试。"
                )
                if client.send_message(message, from_addr=settings["sender"], to_addrs=[recipient]):
                    raise ValueError()
        return {
            "status": "passed",
            "kind": kind,
            "message": "连接、加密和账号认证成功。"
            if kind == "connection"
            else "SMTP 服务已接受测试邮件，请检查收件箱及垃圾邮件；这不代表收件方已确认送达。",
        }
    except Exception as error:
        # Never expose server replies: they may contain credentials or recipients.
        if isinstance(error, (smtplib.SMTPServerDisconnected, ConnectionError, TimeoutError)):
            return {
                "status": "failed", "kind": kind, "stage": stage,
                "message": "SMTP 连接中断或超时，请检查服务器出站网络、邮件服务商访问限制后重试。",
            }
        messages = {
            "connection": "无法连接 SMTP 服务器，请检查地址、端口及服务器出站网络。",
            "tls": "加密连接失败，请检查加密方式、端口和服务器证书。",
            "authentication": "SMTP 认证失败，请检查账号、密码或邮箱授权码。",
            "delivery": "测试邮件被拒绝，请检查发件地址授权及收件地址。",
        }
        return {"status": "failed", "kind": kind, "stage": stage, "message": messages[stage]}
