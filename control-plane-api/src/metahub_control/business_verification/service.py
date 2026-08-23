"""实现企业认证与开票资料的加密持久化、双会话域授权、CAS、幂等和审核原子性。

Workspace 服务只接受 organization owner/finance_admin；Platform 服务只接受 verification 权限。
日志、事件、审计和 HTTP 响应均不得出现法定名称、证号、税号、银行资料、对象引用或密文。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import unicodedata
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.business_verification import (
    BusinessFieldOwner,
    BusinessVerificationCryptoError,
    blind_index,
    decrypt_business_field,
    encrypt_business_field,
    mask_sensitive_identifier,
    verify_evidence_scan_attestation,
)
from metahub_shared.db import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.notifications.service import enqueue_workspace_owners_in_transaction
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)

from .models import (
    BillingProfileResponse,
    BillingProfileSummary,
    BillingProfileUpdateRequest,
    BusinessVerificationSubmitRequest,
    ContractProfileSummary,
    EvidenceDownloadGrantResponse,
    EvidenceStatusSummary,
    LegalEntitySummary,
    PlatformEvidenceSummary,
    PlatformVerificationCaseSummary,
    PlatformVerificationListResponse,
    VerificationIntentSummary,
    VerificationReviewRequest,
    VerificationReviewResponse,
    WorkspaceBusinessVerificationResponse,
)

# 所有 owner 与幂等格式在开启事务前验证，错误响应不回显输入。
_WORKSPACE_ID_RE = re.compile(r"^wsp_[0-9a-f]{32}$")
_INTENT_ID_RE = re.compile(r"^bvi_[0-9a-f]{32}$")
_EVIDENCE_ID_RE = re.compile(r"^bve_[0-9a-f]{32}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_FINANCE_ROLES = frozenset({"owner", "finance_admin"})
_QUEUE_STATUSES = frozenset({"submitted", "in_review", "approved", "rejected", "expired"})


def submit_business_verification(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    idempotency_key: str,
    payload: BusinessVerificationSubmitRequest,
    request_id: str,
    trace_id: str,
) -> WorkspaceBusinessVerificationResponse:
    """创建或修订法定主体，并原子冻结一个不可变认证 intent 与证据 manifest。"""

    _validate_workspace_write(workspace_id, idempotency_key)
    secret = _business_secret(config)
    normalized = _normalized_submission(payload)
    request_sha256 = _private_digest(secret, {"workspace_id": workspace_id, **normalized})
    idempotency_sha256 = _domain_hmac(secret, "workspace-submit", idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        _require_workspace_finance(connection, actor, workspace_id)
        replay = connection.execute(
            """
            SELECT business_verification_intent_id,request_sha256
            FROM business_verification_intents
            WHERE workspace_id=? AND idempotency_sha256=?
            """,
            (workspace_id, idempotency_sha256),
        ).fetchone()
        if replay is not None:
            # 同一幂等 owner 只能重放完全相同的规范意图，不能覆盖另一份法定资料。
            if not hmac.compare_digest(str(replay["request_sha256"]), request_sha256):
                raise ApiError("business_verification.idempotency_conflict")
            result = _workspace_response(
                connection,
                config,
                workspace_id,
                intent_id=str(replay["business_verification_intent_id"]),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result

        for evidence in payload.evidence:
            values = evidence.model_dump(mode="json")
            attestation = values.pop("scan_attestation")
            # 浏览器不能自行声明 malware/DLP 通过；完整证据描述必须由独立扫描服务 HMAC 签发。
            if not verify_evidence_scan_attestation(
                attestation,
                secret=secret,
                workspace_id=workspace_id,
                **values,
            ):
                # 扫描证明无效时不区分字段或签名原因，避免形成证据属性探测通道。
                raise ApiError("business_verification.evidence_invalid")

        now = _utcnow()
        stamp = _db_time(now)
        profile = connection.execute(
            "SELECT * FROM legal_entity_profiles WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()
        profile_id = (
            str(profile["legal_entity_profile_id"]) if profile is not None else _id("lep_")
        )
        profile_revision = int(profile["revision"]) + 1 if profile is not None else 1
        legal_name_ciphertext = _encrypt(
            config,
            normalized["legal_name"],
            workspace_id,
            profile_id,
            "legal_name",
        )
        registration_ciphertext = _encrypt(
            config,
            normalized["registration_number"],
            workspace_id,
            profile_id,
            "registration_number",
        )
        registration_index = blind_index(
            normalized["registration_number"],
            secret=secret,
            workspace_id=workspace_id,
            field_name="registration_number",
        )
        profile_sha256 = _private_digest(
            secret,
            {
                "country_code": normalized["country_code"],
                "entity_type": normalized["entity_type"],
                "legal_name": normalized["legal_name"],
                "registration_number": normalized["registration_number"],
                "profile_revision": profile_revision,
            },
        )
        if profile is None:
            connection.execute(
                """
                INSERT INTO legal_entity_profiles(
                  legal_entity_profile_id,workspace_id,tenant_id,country_code,entity_type,
                  legal_name_ciphertext,registration_number_ciphertext,
                  registration_blind_index,key_version,revision,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,1,'active',?,?)
                """,
                (
                    profile_id,
                    workspace_id,
                    actor.tenant_id,
                    normalized["country_code"],
                    normalized["entity_type"],
                    legal_name_ciphertext,
                    registration_ciphertext,
                    registration_index,
                    config.business_verification_key_version,
                    stamp,
                    stamp,
                ),
            )
        else:
            changed = connection.execute(
                """
                UPDATE legal_entity_profiles
                SET country_code=?,entity_type=?,legal_name_ciphertext=?,
                  registration_number_ciphertext=?,registration_blind_index=?,
                  key_version=?,revision=?,status='active',updated_at=?
                WHERE legal_entity_profile_id=? AND revision=?
                """,
                (
                    normalized["country_code"],
                    normalized["entity_type"],
                    legal_name_ciphertext,
                    registration_ciphertext,
                    registration_index,
                    config.business_verification_key_version,
                    profile_revision,
                    stamp,
                    profile_id,
                    profile_revision - 1,
                ),
            ).rowcount
            # 法定资料 revision 必须单调递增，任何并发获胜都要求调用方重新提交新 intent。
            if changed != 1:
                raise ApiError("business_verification.revision_conflict")

            # 当前合同冻结的是旧 profile revision；已验证开票资料必须同步失效并推进 CAS。
            connection.execute(
                """
                UPDATE workspace_billing_profiles
                SET verification_status='stale',revision=revision+1,updated_at=?
                WHERE workspace_id=? AND verification_status='verified'
                """,
                (stamp, workspace_id),
            )

        intent_id = _id("bvi_")
        manifest = _manifest_from_submission(payload)
        manifest_sha256 = _private_digest(secret, manifest)
        expires_at = now + timedelta(days=90)
        connection.execute(
            """
            INSERT INTO business_verification_intents(
              business_verification_intent_id,workspace_id,tenant_id,
              legal_entity_profile_id,profile_revision,profile_sha256,
              evidence_manifest_sha256,country_code,entity_type,status,reason_code,
              idempotency_sha256,request_sha256,revision,submitted_at,expires_at,
              reviewed_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,'submitted',NULL,?,?,1,?,?,NULL,?)
            """,
            (
                intent_id,
                workspace_id,
                actor.tenant_id,
                profile_id,
                profile_revision,
                profile_sha256,
                manifest_sha256,
                normalized["country_code"],
                normalized["entity_type"],
                idempotency_sha256,
                request_sha256,
                stamp,
                _db_time(expires_at),
                stamp,
            ),
        )
        for evidence in payload.evidence:
            evidence_id = _id("bve_")
            connection.execute(
                """
                INSERT INTO verification_evidence(
                  verification_evidence_id,business_verification_intent_id,
                  workspace_id,tenant_id,evidence_type,object_reference_ciphertext,
                  content_sha256,mime_type,size_bytes,kms_key_version,encryption_key_version,
                  malware_status,dlp_status,status,created_at,tombstoned_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,NULL)
                """,
                (
                    evidence_id,
                    intent_id,
                    workspace_id,
                    actor.tenant_id,
                    evidence.evidence_type,
                    _encrypt(
                        config,
                        evidence.object_reference,
                        workspace_id,
                        evidence_id,
                        "object_reference",
                    ),
                    evidence.content_sha256,
                    evidence.mime_type,
                    evidence.size_bytes,
                    evidence.kms_key_version,
                    config.business_verification_key_version,
                    evidence.malware_status,
                    evidence.dlp_status,
                    stamp,
                ),
            )
        result = _workspace_response(
            connection,
            config,
            workspace_id,
            intent_id=intent_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    # 已分类领域错误回滚后原样保留固定错误码，不允许事务残留继续写入。
    except ApiError:
        _rollback(connection)
        raise
    except (
        sqlite3.Error,
        DbError,
        BusinessVerificationCryptoError,
        KeyError,
        TypeError,
        ValueError,
    ):
        _rollback(connection)
        # 数据库、密码学和解析异常统一失败关闭，禁止泄露密文、DSN 或底层异常正文。
        raise ApiError("business_verification.unavailable") from None
    finally:
        connection.close()


def get_workspace_business_verification(
    config: AppConfig,
    *,
    session_token: str,
    workspace_id: str,
    request_id: str,
    trace_id: str,
) -> WorkspaceBusinessVerificationResponse:
    """读取当前 Workspace 企业认证安全摘要，personal Workspace 固定返回不适用。"""

    # 非规范 Workspace 标识与不可见 owner 同形返回不存在，避免资源枚举。
    if _WORKSPACE_ID_RE.fullmatch(workspace_id) is None:
        raise ApiError("business_verification.not_found")
    _business_secret(config)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require_workspace_finance(connection, actor, workspace_id)
        result = _workspace_response(
            connection,
            config,
            workspace_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    # 权限、owner 或业务状态错误保持原领域码，同时确保只读事务也被正确收口。
    except ApiError:
        _rollback(connection)
        raise
    except (
        sqlite3.Error,
        DbError,
        BusinessVerificationCryptoError,
        KeyError,
        TypeError,
        ValueError,
    ):
        _rollback(connection)
        # 读取投影失败统一映射为不可用，不能把解密或驱动细节暴露给 Workspace。
        raise ApiError("business_verification.unavailable") from None
    finally:
        connection.close()


def update_workspace_billing_profile(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    expected_revision: int,
    idempotency_key: str,
    payload: BillingProfileUpdateRequest,
    request_id: str,
    trace_id: str,
) -> BillingProfileResponse:
    """以 CAS 和幂等语义创建或替换 Workspace 开票字段密文。"""

    _validate_workspace_write(workspace_id, idempotency_key)
    # 开票资料创建允许 revision 零，但任何负数都不是合法 CAS 起点。
    if expected_revision < 0:
        raise ApiError("business_verification.request_invalid")
    secret = _business_secret(config)
    normalized = _normalized_billing(payload)
    request_sha256 = _private_digest(secret, {"workspace_id": workspace_id, **normalized})
    idempotency_sha256 = _domain_hmac(secret, "billing-profile", idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        _require_workspace_finance(connection, actor, workspace_id)
        current = connection.execute(
            "SELECT * FROM workspace_billing_profiles WHERE workspace_id=?",
            (workspace_id,),
        ).fetchone()
        if current is not None and hmac.compare_digest(
            str(current["idempotency_sha256"]), idempotency_sha256
        ):
            # 幂等重放必须绑定同一规范正文，避免把旧键用于另一组银行或税务资料。
            if not hmac.compare_digest(str(current["request_sha256"]), request_sha256):
                raise ApiError("business_verification.idempotency_conflict")
            result = _billing_response(
                connection,
                config,
                workspace_id,
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        current_revision = int(current["revision"]) if current is not None else 0
        # 客户端 revision 必须精确匹配事务内当前值，并发写入不能静默覆盖敏感资料。
        if current_revision != expected_revision:
            raise ApiError("business_verification.revision_conflict")
        profile = connection.execute(
            "SELECT * FROM legal_entity_profiles WHERE workspace_id=? AND status='active'",
            (workspace_id,),
        ).fetchone()
        billing_id = (
            str(current["workspace_billing_profile_id"]) if current is not None else _id("wbp_")
        )
        revision = current_revision + 1
        fields = {
            name: (
                _encrypt(config, value, workspace_id, billing_id, name)
                if value is not None
                else None
            )
            for name, value in normalized.items()
            if name != "confirmed"
        }
        verification_status = _billing_verification_status(connection, workspace_id, profile)
        stamp = _db_time(_utcnow())
        tax_index = blind_index(
            normalized["tax_number"],
            secret=secret,
            workspace_id=workspace_id,
            field_name="tax_number",
        )
        parameters = (
            str(profile["legal_entity_profile_id"]) if profile is not None else None,
            fields["invoice_title"],
            fields["tax_number"],
            tax_index,
            fields["address"],
            fields["phone"],
            fields["bank_name"],
            fields["bank_account"],
            config.business_verification_key_version,
            idempotency_sha256,
            request_sha256,
            revision,
            verification_status,
            stamp,
        )
        if current is None:
            connection.execute(
                """
                INSERT INTO workspace_billing_profiles(
                  workspace_billing_profile_id,workspace_id,tenant_id,legal_entity_profile_id,
                  invoice_title_ciphertext,tax_number_ciphertext,tax_number_blind_index,
                  address_ciphertext,phone_ciphertext,bank_name_ciphertext,
                  bank_account_ciphertext,key_version,idempotency_sha256,request_sha256,
                  revision,verification_status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (billing_id, workspace_id, actor.tenant_id, *parameters, stamp),
            )
        else:
            changed = connection.execute(
                """
                UPDATE workspace_billing_profiles
                SET legal_entity_profile_id=?,invoice_title_ciphertext=?,
                  tax_number_ciphertext=?,tax_number_blind_index=?,address_ciphertext=?,
                  phone_ciphertext=?,bank_name_ciphertext=?,bank_account_ciphertext=?,
                  key_version=?,idempotency_sha256=?,request_sha256=?,revision=?,
                  verification_status=?,updated_at=?
                WHERE workspace_id=? AND revision=?
                """,
                (*parameters, workspace_id, current_revision),
            ).rowcount
            # UPDATE 再次绑定旧 revision，防止检查后到写入前的并发获胜被覆盖。
            if changed != 1:
                raise ApiError("business_verification.revision_conflict")
        result = _billing_response(
            connection,
            config,
            workspace_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    # 领域冲突、权限与幂等错误回滚后保持稳定错误码，供调用方安全重试。
    except ApiError:
        _rollback(connection)
        raise
    except (
        sqlite3.Error,
        DbError,
        BusinessVerificationCryptoError,
        KeyError,
        TypeError,
        ValueError,
    ):
        _rollback(connection)
        # 密文生成或数据库写入异常统一失败关闭，开票明文不得进入错误响应。
        raise ApiError("business_verification.unavailable") from None
    finally:
        connection.close()


def list_platform_business_verifications(
    config: AppConfig,
    *,
    session_token: str,
    status: str | None,
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
) -> PlatformVerificationListResponse:
    """返回 reviewer 最小队列投影，不 JOIN Workspace 名称、成员或客户通用 DTO。"""

    # 服务层独立复核队列枚举，不能只依赖 FastAPI 查询参数校验。
    if status is not None and status not in _QUEUE_STATUSES:
        raise ApiError("business_verification.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require_platform(actor, "platform.verification.read")
        where = "" if status is None else "WHERE status=?"
        values: tuple[object, ...] = () if status is None else (status,)
        total = int(
            connection.execute(
                f"SELECT COUNT(*) FROM business_verification_intents {where}", values
            ).fetchone()[0]
        )
        rows = connection.execute(
            f"""
            SELECT * FROM business_verification_intents {where}
            ORDER BY submitted_at,business_verification_intent_id LIMIT ? OFFSET ?
            """,
            (*values, limit, offset),
        ).fetchall()
        cases = tuple(_platform_case(connection, row) for row in rows)
        connection.commit()
        return PlatformVerificationListResponse(
            cases=cases,
            limit=limit,
            offset=offset,
            total=total,
            request_id=request_id,
            trace_id=trace_id,
        )
    # Platform 权限与会话错误必须原样返回，事务则统一回滚以释放读锁。
    except ApiError:
        _rollback(connection)
        raise
    # 非领域异常统一收敛，不允许 Platform 队列返回数据库或类型解析细节。
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        # 队列构造异常不能退化为包含客户字段的自由错误，只返回固定不可用码。
        raise ApiError("business_verification.unavailable") from None
    finally:
        connection.close()


def review_business_verification(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    intent_id: str,
    expected_revision: int,
    idempotency_key: str,
    payload: VerificationReviewRequest,
    request_id: str,
    trace_id: str,
) -> VerificationReviewResponse:
    """复核冻结主体与证据后，原子追加 review、状态、合同和平台审计。"""

    # 案件标识、CAS 和幂等键先在事务外做规范校验，原始错误值不进入日志。
    if (
        _INTENT_ID_RE.fullmatch(intent_id) is None
        or expected_revision < 1
        or _IDEMPOTENCY_RE.fullmatch(idempotency_key) is None
    ):
        # 任一审核标识不规范都返回固定请求错误，不回显案件或幂等键原值。
        raise ApiError("business_verification.request_invalid")
    secret = _business_secret(config)
    canonical = {"intent_id": intent_id, **payload.model_dump(mode="json")}
    request_sha256 = _private_digest(secret, canonical)
    idempotency_sha256 = _domain_hmac(secret, "platform-review", idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        _require_platform(actor, "platform.verification.review")
        replay = connection.execute(
            """
            SELECT review.*,intent.*
            FROM verification_reviews review
            JOIN business_verification_intents intent
              ON intent.business_verification_intent_id=review.business_verification_intent_id
            WHERE review.idempotency_sha256=?
            """,
            (idempotency_sha256,),
        ).fetchone()
        if replay is not None:
            # 审核幂等键必须同时绑定案件和规范决定，不能跨案件或跨决定复用。
            if (
                replay["business_verification_intent_id"] != intent_id
                or not hmac.compare_digest(str(replay["request_sha256"]), request_sha256)
            ):
                # 同键不同案件或决定属于幂等冲突，不能覆盖已落库审核事实。
                raise ApiError("business_verification.idempotency_conflict")
            result = _review_response(
                connection,
                replay,
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        intent = connection.execute(
            "SELECT * FROM business_verification_intents WHERE business_verification_intent_id=?",
            (intent_id,),
        ).fetchone()
        # 不存在和不可见 intent 使用同形 404，平台响应不暴露 Workspace owner。
        if intent is None:
            raise ApiError("business_verification.not_found")
        # 只有匹配当前 revision 的待审核状态可转换，历史页面不能覆盖新决定。
        if int(intent["revision"]) != expected_revision or intent["status"] not in {
            "submitted",
            "in_review",
        }:
            # revision 或状态不匹配统一视为审核冲突，调用方必须刷新案件快照。
            raise ApiError("business_verification.review_conflict")
        now = _utcnow()
        # 已过期 intent 即使 revision 匹配也不可审批，避免冻结资料无限期复用。
        if _datetime(intent["expires_at"]) <= now:
            raise ApiError("business_verification.review_conflict")
        profile = connection.execute(
            "SELECT * FROM legal_entity_profiles WHERE legal_entity_profile_id=?",
            (intent["legal_entity_profile_id"],),
        ).fetchone()
        # 审核必须绑定提交时冻结的主体 revision，资料变化后必须重新提交 intent。
        if profile is None or int(profile["revision"]) != int(intent["profile_revision"]):
            raise ApiError("business_verification.review_conflict")
        calculated_profile = _profile_digest_from_row(config, profile)
        # 解密重算摘要必须与冻结事实常量时间一致，任何密文替换都阻断审批。
        if not hmac.compare_digest(calculated_profile, str(intent["profile_sha256"])):
            raise ApiError("business_verification.review_conflict")
        evidence_rows = connection.execute(
            """
            SELECT * FROM verification_evidence
            WHERE business_verification_intent_id=?
            ORDER BY evidence_type,verification_evidence_id
            """,
            (intent_id,),
        ).fetchall()
        # 没有证据行的案件不能依靠空聚合通过企业认证。
        if not evidence_rows:
            raise ApiError("business_verification.evidence_invalid")
        calculated_manifest = _manifest_from_rows(config, evidence_rows)
        if not hmac.compare_digest(
            _private_digest(secret, calculated_manifest),
            str(intent["evidence_manifest_sha256"]),
        ):
            # 证据 manifest 摘要不一致说明对象集合或技术属性已变化，必须失败关闭。
            raise ApiError("business_verification.evidence_invalid")
        if payload.decision == "approve" and any(
            row["status"] != "active"
            or row["malware_status"] != "passed"
            or row["dlp_status"] != "passed"
            for row in evidence_rows
        ):
            # approve 必须证明全部扫描通过；pending、failed、blocked 或 tombstone 都失败关闭。
            raise ApiError("business_verification.evidence_not_ready")
        stamp = _db_time(now)
        review_id = _id("bvr_")
        connection.execute(
            """
            INSERT INTO verification_reviews(
              verification_review_id,business_verification_intent_id,
              reviewer_operator_id,decision,reason_code,evidence_manifest_sha256,
              profile_sha256,idempotency_sha256,request_sha256,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                review_id,
                intent_id,
                actor.operator_id,
                payload.decision,
                payload.reason_code,
                intent["evidence_manifest_sha256"],
                intent["profile_sha256"],
                idempotency_sha256,
                request_sha256,
                stamp,
            ),
        )
        next_status = "approved" if payload.decision == "approve" else "rejected"
        changed = connection.execute(
            """
            UPDATE business_verification_intents
            SET status=?,reason_code=?,revision=revision+1,reviewed_at=?,updated_at=?
            WHERE business_verification_intent_id=? AND revision=?
              AND status IN('submitted','in_review')
            """,
            (
                next_status,
                payload.reason_code,
                stamp,
                stamp,
                intent_id,
                expected_revision,
            ),
        ).rowcount
        # 最终状态写入再次执行 revision 与状态 CAS，防止并发 reviewer 双重决策。
        if changed != 1:
            raise ApiError("business_verification.review_conflict")
        contract_id = None
        if payload.decision == "approve":
            contract_id = _id("ctp_")
            version_reference = "contract-v1:" + _private_digest(
                secret,
                {
                    "intent_id": intent_id,
                    "profile_sha256": intent["profile_sha256"],
                    "manifest_sha256": intent["evidence_manifest_sha256"],
                    "review_id": review_id,
                },
            )
            connection.execute(
                """
                INSERT INTO contract_profiles(
                  contract_profile_id,workspace_id,tenant_id,
                  business_verification_intent_id,legal_entity_profile_id,
                  profile_revision,profile_sha256,signing_status,
                  immutable_version_reference,effective_at,expires_at,created_at)
                VALUES(?,?,?,?,?,?,?,'verified',?,?,NULL,?)
                """,
                (
                    contract_id,
                    intent["workspace_id"],
                    intent["tenant_id"],
                    intent_id,
                    intent["legal_entity_profile_id"],
                    intent["profile_revision"],
                    intent["profile_sha256"],
                    version_reference,
                    stamp,
                    stamp,
                ),
            )
            billing = connection.execute(
                "SELECT * FROM workspace_billing_profiles WHERE workspace_id=?",
                (intent["workspace_id"],),
            ).fetchone()
            if billing is not None:
                connection.execute(
                    """
                    UPDATE workspace_billing_profiles
                    SET legal_entity_profile_id=?,verification_status='verified',
                      revision=revision+1,updated_at=?
                    WHERE workspace_billing_profile_id=? AND revision=?
                    """,
                    (
                        intent["legal_entity_profile_id"],
                        stamp,
                        billing["workspace_billing_profile_id"],
                        billing["revision"],
                    ),
                )
        _platform_audit(
            connection,
            actor=actor,
            intent_id=intent_id,
            reason_code=payload.reason_code,
            request_id=request_id,
            trace_id=trace_id,
            before_sha256=_private_digest(secret, {"status": intent["status"]}),
            after_sha256=_private_digest(
                secret,
                {"status": next_status, "review_id": review_id, "contract_id": contract_id},
            ),
            now=now,
        )
        enqueue_workspace_owners_in_transaction(
            connection,
            config,
            workspace_id=str(intent["workspace_id"]),
            tenant_id=str(intent["tenant_id"]),
            event_type="verification.status_changed",
            source_owner_type="verification_review",
            source_owner_id=review_id,
            parameters={"status": next_status, "updated_at": stamp},
            now=now,
        )
        updated = connection.execute(
            "SELECT * FROM business_verification_intents WHERE business_verification_intent_id=?",
            (intent_id,),
        ).fetchone()
        result = _review_response(
            connection,
            updated,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    # 领域错误回滚完整事务，review、合同和审计必须保持全有或全无。
    except ApiError:
        _rollback(connection)
        raise
    except (
        sqlite3.Error,
        DbError,
        BusinessVerificationCryptoError,
        KeyError,
        TypeError,
        ValueError,
    ):
        _rollback(connection)
        # 数据库、解密或摘要异常统一为不可用，禁止泄露客户字段和内部表结构。
        raise ApiError("business_verification.unavailable") from None
    finally:
        connection.close()


def issue_evidence_download_token(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    intent_id: str,
    evidence_id: str,
    request_id: str,
    trace_id: str,
) -> EvidenceDownloadGrantResponse:
    """签发绑定当前 PlatformSession、intent 与 evidence 的一次性短 TTL token。"""

    # 非规范案件或证据标识与不可见资源同形返回不存在，避免平台侧枚举。
    if _INTENT_ID_RE.fullmatch(intent_id) is None or _EVIDENCE_ID_RE.fullmatch(evidence_id) is None:
        raise ApiError("business_verification.not_found")
    secret = _business_secret(config)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        _require_platform(actor, "platform.verification.review")
        row = connection.execute(
            """
            SELECT evidence.*,intent.status intent_status,intent.revision intent_revision,
              intent.expires_at intent_expires_at
            FROM verification_evidence evidence
            JOIN business_verification_intents intent
              ON intent.business_verification_intent_id=evidence.business_verification_intent_id
            WHERE evidence.business_verification_intent_id=?
              AND evidence.verification_evidence_id=?
            """,
            (intent_id, evidence_id),
        ).fetchone()
        now = _utcnow()
        if (
            row is None
            or row["status"] != "active"
            or row["intent_status"] not in {"submitted", "in_review"}
            or _datetime(row["intent_expires_at"]) <= now
        ):
            # 不存在、非 active review 和已过期证据统一同形 404，不透露案件存在性。
            raise ApiError("business_verification.not_found")
        raw_token = secrets.token_urlsafe(32)
        token_hmac = _domain_hmac(
            secret,
            "evidence-download",
            f"{actor.platform_session_id}\0{intent_id}\0{evidence_id}\0{raw_token}",
        )
        expires_at = now + timedelta(seconds=config.business_verification_download_ttl_seconds)
        connection.execute(
            """
            INSERT INTO verification_evidence_download_tokens(
              verification_evidence_download_token_id,business_verification_intent_id,
              verification_evidence_id,platform_session_id,token_hmac,status,
              expires_at,created_at,used_at)
            VALUES(?,?,?,?,?,'active',?,?,NULL)
            """,
            (
                _id("vdt_"),
                intent_id,
                evidence_id,
                actor.platform_session_id,
                token_hmac,
                _db_time(expires_at),
                _db_time(now),
            ),
        )
        revision = int(row["intent_revision"])
        if row["intent_status"] == "submitted":
            connection.execute(
                """
                UPDATE business_verification_intents
                SET status='in_review',revision=revision+1,updated_at=?
                WHERE business_verification_intent_id=? AND revision=? AND status='submitted'
                """,
                (_db_time(now), intent_id, revision),
            )
            revision += 1
        connection.commit()
        return EvidenceDownloadGrantResponse(
            verification_evidence_id=evidence_id,
            download_token=raw_token,
            expires_at=expires_at,
            intent_revision=revision,
            request_id=request_id,
            trace_id=trace_id,
        )
    # 权限、会话和案件状态错误回滚后保留固定领域码，不签发部分授权事实。
    except ApiError:
        _rollback(connection)
        raise
    # token 签发的驱动、类型和状态异常统一收敛，避免暴露对象存储内部事实。
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        # token 摘要或数据库写入异常统一失败关闭，原始 token 绝不进入错误响应。
        raise ApiError("business_verification.unavailable") from None
    finally:
        connection.close()


def _workspace_response(
    connection: sqlite3.Connection,
    config: AppConfig,
    workspace_id: str,
    *,
    intent_id: str | None = None,
    idempotent_replay: bool = False,
    request_id: str,
    trace_id: str,
) -> WorkspaceBusinessVerificationResponse:
    """从同一事务构造 Workspace 安全摘要，敏感字段仅在局部变量中解密并立即掩码。"""

    profile = connection.execute(
        "SELECT * FROM legal_entity_profiles WHERE workspace_id=?",
        (workspace_id,),
    ).fetchone()
    if intent_id is None:
        intent = connection.execute(
            """
            SELECT * FROM business_verification_intents WHERE workspace_id=?
            ORDER BY submitted_at DESC,business_verification_intent_id DESC LIMIT 1
            """,
            (workspace_id,),
        ).fetchone()
    else:
        intent = connection.execute(
            """
            SELECT * FROM business_verification_intents
            WHERE workspace_id=? AND business_verification_intent_id=?
            """,
            (workspace_id, intent_id),
        ).fetchone()
    contract = connection.execute(
        """
        SELECT * FROM contract_profiles WHERE workspace_id=?
        ORDER BY created_at DESC,contract_profile_id DESC LIMIT 1
        """,
        (workspace_id,),
    ).fetchone()
    billing = connection.execute(
        "SELECT * FROM workspace_billing_profiles WHERE workspace_id=?",
        (workspace_id,),
    ).fetchone()
    legal_summary = None
    if profile is not None:
        registration = _decrypt(
            config,
            profile["registration_number_ciphertext"],
            workspace_id,
            profile["legal_entity_profile_id"],
            "registration_number",
            int(profile["key_version"]),
        )
        legal_summary = LegalEntitySummary(
            country_code=profile["country_code"],
            entity_type=profile["entity_type"],
            legal_name_configured=True,
            registration_number_masked=mask_sensitive_identifier(registration),
            revision=int(profile["revision"]),
            updated_at=_datetime(profile["updated_at"]),
        )
    return WorkspaceBusinessVerificationResponse(
        workspace_id=workspace_id,
        legal_entity=legal_summary,
        verification=_intent_summary(connection, intent) if intent is not None else None,
        contract=_contract_summary(contract) if contract is not None else None,
        billing_profile=(
            _billing_summary(config, billing) if billing is not None else None
        ),
        idempotent_replay=idempotent_replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _billing_response(
    connection: sqlite3.Connection,
    config: AppConfig,
    workspace_id: str,
    *,
    idempotent_replay: bool = False,
    request_id: str,
    trace_id: str,
) -> BillingProfileResponse:
    """读取刚提交的开票资料并构造字段级掩码状态。"""

    row = connection.execute(
        "SELECT * FROM workspace_billing_profiles WHERE workspace_id=?", (workspace_id,)
    ).fetchone()
    # 事务内找不到刚写入的 owner 行说明状态不一致，统一表现为资源不存在。
    if row is None:
        raise ApiError("business_verification.not_found")
    return BillingProfileResponse(
        workspace_id=workspace_id,
        billing_profile=_billing_summary(config, row),
        idempotent_replay=idempotent_replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _billing_summary(config: AppConfig, row: sqlite3.Row) -> BillingProfileSummary:
    """只解密税号与银行账号用于末四位掩码，其余字段仅报告是否配置。"""

    workspace_id = str(row["workspace_id"])
    billing_id = str(row["workspace_billing_profile_id"])
    key_version = int(row["key_version"])
    tax_number = _decrypt(
        config,
        row["tax_number_ciphertext"],
        workspace_id,
        billing_id,
        "tax_number",
        key_version,
    )
    bank_account = None
    if row["bank_account_ciphertext"] is not None:
        bank_account = _decrypt(
            config,
            row["bank_account_ciphertext"],
            workspace_id,
            billing_id,
            "bank_account",
            key_version,
        )
    return BillingProfileSummary(
        invoice_title_configured=True,
        tax_number_masked=mask_sensitive_identifier(tax_number),
        address_configured=row["address_ciphertext"] is not None,
        phone_configured=row["phone_ciphertext"] is not None,
        bank_name_configured=row["bank_name_ciphertext"] is not None,
        bank_account_masked=(
            mask_sensitive_identifier(bank_account) if bank_account is not None else None
        ),
        verification_status=row["verification_status"],
        revision=int(row["revision"]),
        updated_at=_datetime(row["updated_at"]),
    )


def _review_response(
    connection: sqlite3.Connection,
    intent: sqlite3.Row,
    *,
    idempotent_replay: bool = False,
    request_id: str,
    trace_id: str,
) -> VerificationReviewResponse:
    """返回审核后的案件与可选合同安全摘要。"""

    current = connection.execute(
        "SELECT * FROM business_verification_intents WHERE business_verification_intent_id=?",
        (intent["business_verification_intent_id"],),
    ).fetchone()
    # 审核完成后案件必须仍存在；缺失时不能返回旧参数拼接出的伪响应。
    if current is None:
        raise ApiError("business_verification.not_found")
    contract = connection.execute(
        """
        SELECT * FROM contract_profiles WHERE business_verification_intent_id=?
        ORDER BY created_at DESC LIMIT 1
        """,
        (intent["business_verification_intent_id"],),
    ).fetchone()
    return VerificationReviewResponse(
        case=_platform_case(connection, current),
        contract=_contract_summary(contract) if contract is not None else None,
        idempotent_replay=idempotent_replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _platform_case(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> PlatformVerificationCaseSummary:
    """构造不含 Workspace、tenant、名称、证号和对象引用的审核案件投影。"""

    return PlatformVerificationCaseSummary(
        business_verification_intent_id=row["business_verification_intent_id"],
        country_code=row["country_code"],
        entity_type=row["entity_type"],
        status=row["status"],
        reason_code=row["reason_code"],
        profile_revision=int(row["profile_revision"]),
        profile_sha256=row["profile_sha256"],
        evidence_manifest_sha256=row["evidence_manifest_sha256"],
        evidence=_evidence_status(connection, row["business_verification_intent_id"]),
        evidence_items=_platform_evidence_items(
            connection, row["business_verification_intent_id"]
        ),
        revision=int(row["revision"]),
        submitted_at=_datetime(row["submitted_at"]),
        expires_at=_datetime(row["expires_at"]),
    )


def _platform_evidence_items(
    connection: sqlite3.Connection, intent_id: str
) -> tuple[PlatformEvidenceSummary, ...]:
    """构造平台证据操作 allowlist，不读取对象引用密文或 Workspace owner。"""

    rows = connection.execute(
        """
        SELECT verification_evidence_id,evidence_type,mime_type,size_bytes,
          malware_status,dlp_status
        FROM verification_evidence
        WHERE business_verification_intent_id=? AND status='active'
        ORDER BY evidence_type,verification_evidence_id
        """,
        (intent_id,),
    ).fetchall()
    return tuple(
        PlatformEvidenceSummary(
            verification_evidence_id=row["verification_evidence_id"],
            evidence_type=row["evidence_type"],
            mime_type=row["mime_type"],
            size_bytes=int(row["size_bytes"]),
            malware_status=row["malware_status"],
            dlp_status=row["dlp_status"],
        )
        for row in rows
    )


def _intent_summary(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> VerificationIntentSummary:
    """构造 Workspace intent 投影，审核内部 actor 和 review row 永不返回。"""

    return VerificationIntentSummary(
        business_verification_intent_id=row["business_verification_intent_id"],
        status=row["status"],
        reason_code=row["reason_code"],
        profile_revision=int(row["profile_revision"]),
        profile_sha256=row["profile_sha256"],
        evidence_manifest_sha256=row["evidence_manifest_sha256"],
        evidence=_evidence_status(connection, row["business_verification_intent_id"]),
        revision=int(row["revision"]),
        submitted_at=_datetime(row["submitted_at"]),
        expires_at=_datetime(row["expires_at"]),
        reviewed_at=(
            _datetime(row["reviewed_at"]) if row["reviewed_at"] is not None else None
        ),
    )


def _contract_summary(row: sqlite3.Row) -> ContractProfileSummary:
    """投影不可变合同引用和有效期，不返回 profile owner 或客户明文。"""

    return ContractProfileSummary(
        contract_profile_id=row["contract_profile_id"],
        signing_status=row["signing_status"],
        immutable_version_reference=row["immutable_version_reference"],
        effective_at=_datetime(row["effective_at"]),
        expires_at=_datetime(row["expires_at"]) if row["expires_at"] is not None else None,
    )


def _evidence_status(
    connection: sqlite3.Connection, intent_id: str
) -> EvidenceStatusSummary:
    """聚合证据扫描状态；blocked 优先于 failed，failed 优先于 pending。"""

    rows = connection.execute(
        """
        SELECT malware_status,dlp_status,status FROM verification_evidence
        WHERE business_verification_intent_id=? ORDER BY verification_evidence_id
        """,
        (intent_id,),
    ).fetchall()
    return EvidenceStatusSummary(
        evidence_count=len(rows),
        malware_status=_aggregate_status(rows, "malware_status"),
        dlp_status=_aggregate_status(rows, "dlp_status"),
    )


def _aggregate_status(rows: list[sqlite3.Row], field: str) -> str:
    """将多条证据状态压缩为固定严重度，不输出具体文件或对象标识。"""

    if not rows:
        return "none"
    values = {
        "failed" if row["status"] != "active" else str(row[field]) for row in rows
    }
    for status in ("blocked", "failed", "pending"):
        if status in values:
            return status
    return "passed"


def _normalized_submission(payload: BusinessVerificationSubmitRequest) -> dict[str, object]:
    """规范法定主体文本与证据 manifest，禁止控制符和 Unicode 双重表示。"""

    return {
        "country_code": payload.country_code,
        "entity_type": payload.entity_type,
        "legal_name": _clean_text(payload.legal_name, maximum=240),
        "registration_number": _clean_text(payload.registration_number, maximum=128),
        "evidence": _manifest_from_submission(payload),
        "reason_code": payload.reason_code,
        "confirmed": True,
    }


def _normalized_billing(payload: BillingProfileUpdateRequest) -> dict[str, object]:
    """规范开票字段；空可选字段保持 None，不允许空白字符串伪装成已配置。"""

    return {
        "invoice_title": _clean_text(payload.invoice_title, maximum=240),
        "tax_number": _clean_text(payload.tax_number, maximum=128),
        "address": _optional_clean(payload.address, maximum=500),
        "phone": _optional_clean(payload.phone, maximum=80),
        "bank_name": _optional_clean(payload.bank_name, maximum=240),
        "bank_account": _optional_clean(payload.bank_account, maximum=128),
        "confirmed": True,
    }


def _manifest_from_submission(
    payload: BusinessVerificationSubmitRequest,
) -> list[dict[str, object]]:
    """冻结证据 manifest 的业务字段，不把 attestation 本身作为长期事实。"""

    return sorted(
        [
            {
                "evidence_type": item.evidence_type,
                "object_reference": item.object_reference,
                "content_sha256": item.content_sha256,
                "mime_type": item.mime_type,
                "size_bytes": item.size_bytes,
                "kms_key_version": item.kms_key_version,
                "malware_status": item.malware_status,
                "dlp_status": item.dlp_status,
            }
            for item in payload.evidence
        ],
        key=lambda item: str(item["evidence_type"]),
    )


def _manifest_from_rows(
    config: AppConfig, rows: list[sqlite3.Row]
) -> list[dict[str, object]]:
    """从加密数据库事实重建 manifest，审核不信任原始提交正文。"""

    return [
        {
            "evidence_type": row["evidence_type"],
            "object_reference": _decrypt(
                config,
                row["object_reference_ciphertext"],
                row["workspace_id"],
                row["verification_evidence_id"],
                "object_reference",
                int(row["encryption_key_version"]),
            ),
            "content_sha256": row["content_sha256"],
            "mime_type": row["mime_type"],
            "size_bytes": int(row["size_bytes"]),
            "kms_key_version": int(row["kms_key_version"]),
            "malware_status": row["malware_status"],
            "dlp_status": row["dlp_status"],
        }
        for row in rows
    ]


def _profile_digest_from_row(config: AppConfig, row: sqlite3.Row) -> str:
    """从当前数据库密文重建 profile digest，证明 reviewer 审核的是冻结 revision。"""

    secret = _business_secret(config)
    workspace_id = str(row["workspace_id"])
    profile_id = str(row["legal_entity_profile_id"])
    key_version = int(row["key_version"])
    return _private_digest(
        secret,
        {
            "country_code": row["country_code"],
            "entity_type": row["entity_type"],
            "legal_name": _decrypt(
                config,
                row["legal_name_ciphertext"],
                workspace_id,
                profile_id,
                "legal_name",
                key_version,
            ),
            "registration_number": _decrypt(
                config,
                row["registration_number_ciphertext"],
                workspace_id,
                profile_id,
                "registration_number",
                key_version,
            ),
            "profile_revision": int(row["revision"]),
        },
    )


def _billing_verification_status(
    connection: sqlite3.Connection,
    workspace_id: str,
    profile: sqlite3.Row | None,
) -> str:
    """只有当前 profile revision 已形成合同，开票资料才标记 verified。"""

    latest = connection.execute(
        """
        SELECT * FROM contract_profiles WHERE workspace_id=?
        ORDER BY created_at DESC,contract_profile_id DESC LIMIT 1
        """,
        (workspace_id,),
    ).fetchone()
    if latest is None:
        return "unverified"
    if (
        profile is not None
        and latest["legal_entity_profile_id"] == profile["legal_entity_profile_id"]
        and int(latest["profile_revision"]) == int(profile["revision"])
    ):
        return "verified"
    return "stale"


def _require_workspace_finance(
    connection: sqlite3.Connection,
    actor: WorkspaceSessionContext,
    workspace_id: str,
) -> None:
    """要求路径等于 WorkspaceSession owner、角色实时为 finance 且 Workspace 为 organization。"""

    # 路径 owner 与会话 owner 不一致时同形返回不存在，阻止跨 Workspace 枚举。
    if actor.workspace_id != workspace_id:
        raise ApiError("business_verification.not_found")
    # 角色必须从当前事务恢复，已撤销 owner/finance 权限应在同一会话立即生效。
    if actor.membership_role not in _FINANCE_ROLES:
        raise ApiError("workspace_session.permission_denied")
    row = connection.execute(
        """
        SELECT kind,status FROM workspaces WHERE workspace_id=?
          AND EXISTS(SELECT 1 FROM workspace_tenant_bindings binding
            WHERE binding.workspace_id=workspaces.workspace_id
              AND binding.tenant_id=? AND binding.status='active')
        """,
        (workspace_id, actor.tenant_id),
    ).fetchone()
    # Workspace 必须仍处于 active 且绑定当前 tenant，失效绑定不能继续访问法定资料。
    if row is None or row["status"] != "active":
        raise ApiError("business_verification.not_found")
    # personal Workspace 没有企业法定主体，固定返回不适用而不是构造空模板。
    if row["kind"] != "organization":
        raise ApiError("business_verification.business_not_applicable")


def _require_platform(actor: PlatformContext, permission: str) -> None:
    """服务层在同一事务内再次复核精确平台权限。"""

    # 服务层再次检查动态权限，不能信任路由依赖或前端控件可见性。
    if permission not in actor.permissions:
        raise ApiError("platform.permission_denied")


def _platform_audit(
    connection: sqlite3.Connection,
    *,
    actor: PlatformContext,
    intent_id: str,
    reason_code: str,
    request_id: str,
    trace_id: str,
    before_sha256: str,
    after_sha256: str,
    now: datetime,
) -> None:
    """写入只含固定状态摘要的平台审计，不复制任何客户 PII 或证据引用。"""

    connection.execute(
        """
        INSERT INTO platform_audit_logs(
          audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
          request_id,trace_id,before_sha256,after_sha256,occurred_at)
        VALUES(?,'platform',?,'business_verification.reviewed',
          'business_verification_intent',?,?,?,?,?,?,?)
        """,
        (
            _id("pau_"),
            actor.operator_id,
            intent_id,
            reason_code,
            request_id,
            trace_id,
            before_sha256,
            after_sha256,
            _db_time(now),
        ),
    )


def _encrypt(
    config: AppConfig,
    value: object,
    workspace_id: object,
    object_id: object,
    field_name: str,
) -> str:
    """使用当前 key version 加密字段，AAD owner 全部来自服务端事务事实。"""

    return encrypt_business_field(
        value,
        secret=_business_secret(config),
        owner=BusinessFieldOwner(
            workspace_id=str(workspace_id),
            object_id=str(object_id),
            field_name=field_name,
            key_version=config.business_verification_key_version,
        ),
    )


def _decrypt(
    config: AppConfig,
    value: object,
    workspace_id: object,
    object_id: object,
    field_name: str,
    key_version: int,
) -> str:
    """按数据库记录的 key version 和 owner 解密，未知轮换版本失败关闭。"""

    return decrypt_business_field(
        value,
        secret=_business_secret(config),
        owner=BusinessFieldOwner(
            workspace_id=str(workspace_id),
            object_id=str(object_id),
            field_name=field_name,
            key_version=key_version,
        ),
    )


def _private_digest(secret: str, value: object) -> str:
    """对可能含低熵 PII 的规范对象计算 keyed digest，避免公共 SHA 字典枚举。"""

    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    return hmac.new(
        _domain_key(secret, "private-digest"), encoded, hashlib.sha256
    ).hexdigest()


def _domain_hmac(secret: str, domain: str, value: str) -> str:
    """为幂等键和下载 token 派生固定业务域摘要，原始值绝不落库。"""

    return hmac.new(
        _domain_key(secret, domain), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _domain_key(secret: str, domain: str) -> bytes:
    """以 HMAC 派生服务层摘要子密钥，避免与字段 AES/HKDF purpose 共用输出。"""

    return hmac.new(
        secret.encode("utf-8"),
        f"metahub/business-verification/service/v1/{domain}".encode("ascii"),
        hashlib.sha256,
    ).digest()


def _validate_workspace_write(workspace_id: str, idempotency_key: str) -> None:
    """在事务前拒绝非规范 path owner 与幂等键。"""

    # owner 和幂等键必须同时规范，失败响应不携带任何原始标识文本。
    if (
        _WORKSPACE_ID_RE.fullmatch(workspace_id) is None
        or _IDEMPOTENCY_RE.fullmatch(idempotency_key) is None
    ):
        # 非规范 owner 或幂等键只产生固定错误，不返回调用方原始输入。
        raise ApiError("business_verification.request_invalid")


def _clean_text(value: object, *, maximum: int) -> str:
    """执行 NFKC、trim 和控制符拒绝，防止同一资料出现多套 hash/blind-index 表示。"""

    # 敏感业务字段不做隐式字符串转换，避免数字和布尔产生另一套规范摘要。
    if type(value) is not str:
        raise ApiError("business_verification.request_invalid")
    normalized = unicodedata.normalize("NFKC", value).strip()
    # 规范化后的空值、超长值和控制字符都会破坏加密与摘要合同，必须拒绝。
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 32 for character in normalized)
    ):
        # 规范化结果越界时不回显敏感文本，只返回统一请求错误。
        raise ApiError("business_verification.request_invalid")
    return normalized


def _optional_clean(value: object, *, maximum: int) -> str | None:
    """规范可选字段，None 代表显式未配置且不会产生空密文。"""

    return None if value is None else _clean_text(value, maximum=maximum)


def _business_secret(config: AppConfig) -> str:
    """读取被配置对象排除的独立根密钥；缺失时对应业务固定失败关闭。"""

    secret = config.business_verification_encryption_secret
    # 独立根密钥缺失时业务失败关闭，不能退回 Identity 或其他安全域的密钥。
    if type(secret) is not str or not secret:
        raise ApiError("business_verification.unavailable")
    # 根密钥仅返回给当前服务调用栈，不允许写入响应、审计或数据库字段。
    return secret


def _connect(config: AppConfig) -> sqlite3.Connection:
    """连接数据库并把 DSN/驱动错误收敛为固定业务不可用。"""

    try:
        return connect_database(config.database_url)
    # 驱动与 DSN 异常统一为业务不可用，禁止把私有基础设施信息透出 API。
    except DbError:
        raise ApiError("business_verification.unavailable") from None


def _rollback(connection: sqlite3.Connection) -> None:
    """尽力回滚当前事务，不让回滚异常覆盖原领域错误。"""

    try:
        if connection.in_transaction:
            connection.rollback()
    except Exception:
        pass


def _id(prefix: str) -> str:
    """生成不可预测且由服务端拥有的固定前缀领域标识。"""

    return prefix + uuid4().hex


def _utcnow() -> datetime:
    """统一生成带 UTC 时区的事务时间。"""

    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    """规范化为毫秒精度 UTC 数据库文本。"""

    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _datetime(value: object) -> datetime:
    """兼容 SQLite 文本与驱动 datetime，并返回带时区时间。"""

    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
