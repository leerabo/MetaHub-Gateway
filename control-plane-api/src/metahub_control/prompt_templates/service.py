"""PRD-067 企业 Prompt 模板库的租户隔离事务服务。

本模块只在授权请求栈中接触模板正文、变量定义和变更说明。持久化层仅保存 owner-bound
AES-GCM 密文、固定计数和分域 HMAC；日志、AuditLog、Webhook 与普通查询均不得复制明文。
所有管理写入使用 ``BEGIN IMMEDIATE``，并在同一事务重新复核 Session 与实时权限矩阵，
避免路由鉴权通过后撤权、禁用或并发发布造成越权写入。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.prompt_templates import (
    PROMPT_TEMPLATE_SCHEMA_VERSION,
    PromptTemplateContractError,
    PromptTemplateDefinition,
    PromptTemplateDefinitionUnavailable,
    PromptTemplateOwner,
    PromptTemplateSecretUnavailable,
    canonical_definition_bytes,
    change_summary_hmac,
    decrypt_definition,
    encrypt_definition,
    prompt_template_intent_hmac,
    validate_definition,
    verify_prompt_template_intent_hmac,
)
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
from metahub_control.auth import AuthContext, require_transaction_permission

from .models import (
    CreatePromptTemplateRequest,
    PatchPromptTemplateRequest,
    PromptTemplateDefinitionInfo,
    PromptTemplateDefinitionResponse,
    PromptTemplateDetailResponse,
    PromptTemplateInfo,
    PromptTemplateListResponse,
    PromptTemplateModelAliasOption,
    PromptTemplateMutationResponse,
    PromptTemplateOptionsResponse,
    PromptTemplateProjectOption,
    PromptTemplateVariableInfo,
    PromptTemplateVersionInfo,
    PublishPromptTemplateVersionRequest,
)

logger = logging.getLogger("metahub.control.prompt_templates")

# 管理列表与自由文本采用固定上限，防止搜索/分页变成无界数据库扫描。
DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100
MAX_LIST_OFFSET = 1_000_000
MAX_NAME_BYTES = 360
MAX_DESCRIPTION_BYTES = 3_000
MAX_SEARCH_CHARS = 120
MAX_PROJECT_BINDINGS = 100

# request/trace ID 会进入不可变 owner 关系，只接受迁移允许的 ASCII 安全集合。
_CHAIN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")

_TEMPLATE_SELECT = """
SELECT
  template.prompt_template_id,template.tenant_id,template.name,template.description,
  template.status,template.current_version_id,template.revision,
  template.created_at,template.updated_at,
  version.version_number,version.project_scope,version.definition_size_bytes,
  version.variable_count,version.recommended_model_count,version.change_summary_hmac,
  (SELECT COUNT(*) FROM prompt_template_version_projects binding
   WHERE binding.tenant_id=version.tenant_id
     AND binding.prompt_template_version_id=version.prompt_template_version_id) AS project_count
FROM prompt_templates template
JOIN prompt_template_versions version
  ON version.tenant_id=template.tenant_id
 AND version.prompt_template_id=template.prompt_template_id
 AND version.prompt_template_version_id=template.current_version_id
 AND version.state='published'
"""


@dataclass(frozen=True, slots=True)
class _PromptTemplateEvent:
    """提交后派发 ``prompt_template.published`` 所需的最小安全事实。"""

    event_id: str
    tenant_id: str
    template_id: str
    version_id: str
    actor_user_id: str
    request_id: str
    trace_id: str
    payload: dict[str, object]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _RecoveredOperation:
    """幂等重放从不可变 Operation 恢复的版本与当前权威模板投影。"""

    template: PromptTemplateInfo
    version: PromptTemplateVersionInfo | None
    event_id: str | None


def list_prompt_templates(
    config: AppConfig,
    context: AuthContext,
    *,
    status: str | None,
    search: str | None,
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
) -> PromptTemplateListResponse:
    """分页列出当前租户模板的安全 metadata，不解密任何版本定义。

    参数：可信认证上下文、可选状态/搜索和有界分页。返回：稳定排序的模板列表。
    关键约束：tenant 始终取自 ``AuthContext``，列表 SQL 的第一过滤条件不可由 query 覆盖。
    """

    normalized_status = _normalize_optional_status(status)
    normalized_search = _normalize_search(search)
    normalized_limit = _normalize_limit(limit)
    normalized_offset = _normalize_offset(offset)
    clauses = ["template.tenant_id=?"]
    params: list[object] = [context.user.tenant_id]
    if normalized_status is not None:
        clauses.append("template.status=?")
        params.append(normalized_status)
    if normalized_search is not None:
        # LIKE 通配符被显式转义，搜索只能缩小当前 tenant 结果，不能改变 SQL 结构。
        clauses.append(
            "(template.name LIKE ? ESCAPE '\\' OR template.description LIKE ? ESCAPE '\\')"
        )
        needle = f"%{_escape_like(normalized_search)}%"
        params.extend((needle, needle))
    where_sql = " AND ".join(clauses)
    connection = _connect(config)
    try:
        total_row = connection.execute(
            f"SELECT COUNT(*) AS total FROM prompt_templates template WHERE {where_sql}",
            params,
        ).fetchone()
        rows = connection.execute(
            f"""
            {_TEMPLATE_SELECT}
            WHERE {where_sql}
            ORDER BY template.updated_at DESC,template.prompt_template_id
            LIMIT ? OFFSET ?
            """,
            (*params, normalized_limit, normalized_offset),
        ).fetchall()
        return PromptTemplateListResponse(
            prompt_templates=[_template_info(row) for row in rows],
            total=int(total_row["total"] if total_row is not None else 0),
            limit=normalized_limit,
            offset=normalized_offset,
            request_id=request_id,
            trace_id=trace_id,
        )
    # 已归一化的模板域错误必须原样保留，避免把 404/409/422 错折叠成依赖故障。
    # source event 已经随业务事务提交，连接失败只能记录安全告警，不能反向改变发布结果。
    except ApiError:
        raise
    # 数据库异常和损坏行都属于服务端依赖问题，公共响应不得暴露 SQL 或原始字段。
    except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
        # 数据库路径、SQL 和损坏行内容均不能进入公共错误，只返回模板域固定不可用码。
        raise ApiError("prompt_template.unavailable") from exc
    finally:
        connection.close()


def list_prompt_template_options(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> PromptTemplateOptionsResponse:
    """返回 PAGE-094 表单使用的 active Project/ModelAlias 最小目录。

    该接口不要求 Project 或路由管理权限，因此只投影绑定所需 ID、名称、环境和 active 状态；
    Virtual Key、Provider、Endpoint、价格与路由详情均不得从这里泄露。
    """

    connection = _connect(config)
    try:
        projects = connection.execute(
            """
            SELECT project_id,name FROM projects
            WHERE tenant_id=? AND status='active'
            ORDER BY name,project_id
            """,
            (context.user.tenant_id,),
        ).fetchall()
        aliases = connection.execute(
            """
            SELECT alias.model_alias_id,alias.alias,alias.description,environment.environment_id
            FROM model_aliases alias
            JOIN environments environment
              ON environment.tenant_id=alias.tenant_id
             AND (environment.environment_id=alias.environment_id
                  OR (alias.environment_id IS NULL AND environment.code=alias.environment))
            WHERE alias.tenant_id=? AND alias.status='active' AND environment.status='active'
            ORDER BY alias.alias,alias.model_alias_id
            """,
            (context.user.tenant_id,),
        ).fetchall()
        return PromptTemplateOptionsResponse(
            projects=[
                PromptTemplateProjectOption(
                    project_id=str(row["project_id"]),
                    name=str(row["name"]),
                )
                for row in projects
            ],
            model_aliases=[
                PromptTemplateModelAliasOption(
                    model_alias_id=str(row["model_alias_id"]),
                    alias=str(row["alias"]),
                    display_name=str(row["description"] or row["alias"]).strip(),
                    environment_id=str(row["environment_id"]),
                )
                for row in aliases
            ],
            request_id=request_id,
            trace_id=trace_id,
        )
    # 候选目录读取失败时隐藏数据库结构，只向已授权调用方返回模板域固定错误码。
    except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
        raise ApiError("prompt_template.unavailable") from exc
    finally:
        connection.close()


def get_prompt_template(
    config: AppConfig,
    context: AuthContext,
    template_id: str,
    *,
    request_id: str,
    trace_id: str,
) -> PromptTemplateDetailResponse:
    """读取当前租户模板及全部不可变版本的安全历史，不返回定义正文。"""

    normalized_template_id = _normalize_template_id(template_id)
    connection = _connect(config)
    try:
        template_row = _load_template_row(
            connection,
            context.user.tenant_id,
            normalized_template_id,
        )
        version_rows = connection.execute(
            """
            SELECT version.*,
              (SELECT COUNT(*) FROM prompt_template_version_projects binding
               WHERE binding.tenant_id=version.tenant_id
                 AND binding.prompt_template_version_id=version.prompt_template_version_id)
                AS project_count
            FROM prompt_template_versions version
            WHERE version.tenant_id=? AND version.prompt_template_id=?
              AND version.state='published'
            ORDER BY version.version_number DESC,version.prompt_template_version_id
            """,
            (context.user.tenant_id, normalized_template_id),
        ).fetchall()
        versions = [_version_info(connection, row) for row in version_rows]
        if (
            not versions
            or versions[0].prompt_template_version_id != template_row["current_version_id"]
        ):
            # head 不在完整历史或不是最高连续版本表示持久 owner 图已损坏，不能猜测返回。
            raise ApiError("prompt_template.integrity_failed")
        return PromptTemplateDetailResponse(
            template=_template_info(template_row),
            versions=versions,
            request_id=request_id,
            trace_id=trace_id,
        )
    # 资源不存在与持久化完整性错误已经具备精确语义，不能在外层重新分类。
    except ApiError:
        raise
    # 其他读取或解码异常统一隐藏底层数据库细节，防止错误正文成为枚举侧信道。
    except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
        raise ApiError("prompt_template.unavailable") from exc
    finally:
        connection.close()


def get_prompt_template_definition(
    config: AppConfig,
    context: AuthContext,
    template_id: str,
    version_id: str,
    *,
    request_id: str,
    trace_id: str,
) -> PromptTemplateDefinitionResponse:
    """解密并返回当前租户精确版本定义，明文仅存在于本次 no-store 响应栈。

    密钥缺失保持 503；格式、AAD、认证标签或 schema 损坏统一返回 500，不能把密码学细节、
    密文或 owner ID 拼入异常文本。
    """

    normalized_template_id = _normalize_template_id(template_id)
    normalized_version_id = _normalize_version_id(version_id)
    secret = _prompt_secret(config)
    connection = _connect(config)
    try:
        row = _load_version_row(
            connection,
            context.user.tenant_id,
            normalized_template_id,
            normalized_version_id,
        )
        version = _version_info(connection, row)
        definition = decrypt_definition(
            row["definition_ciphertext"],
            secret=secret,
            owner=PromptTemplateOwner(
                tenant_id=context.user.tenant_id,
                prompt_template_id=normalized_template_id,
                prompt_template_version_id=normalized_version_id,
                version_number=version.version_number,
            ),
        )
        _verify_definition_counts(row, definition)
        return PromptTemplateDefinitionResponse(
            prompt_template_id=normalized_template_id,
            prompt_template_version_id=normalized_version_id,
            version_number=version.version_number,
            definition=PromptTemplateDefinitionInfo(
                content=definition.content,
                variables=[_variable_info(item) for item in definition.variables],
                project_scope=version.project_scope,
                project_ids=version.project_ids,
                recommended_model_alias_ids=version.recommended_model_alias_ids,
                change_summary=definition.change_summary,
            ),
            request_id=request_id,
            trace_id=trace_id,
        )
    # 独立模板密钥缺失是可恢复配置故障，必须失败关闭且绝不回退其他业务密钥。
    except PromptTemplateSecretUnavailable:
        raise ApiError("prompt_template.unavailable") from None
    # 密文、AAD 或定义 schema 无法认证时隐藏密码学原因，统一视为定义不可用。
    except PromptTemplateDefinitionUnavailable:
        raise ApiError("prompt_template.definition_unavailable") from None
    # tenant 资源与完整性错误已由内层精确归类，外层不得破坏同形 404 约束。
    except ApiError:
        raise
    # 非密码学的数据库或行解码失败仍需隐藏底层实现，只公开固定依赖错误。
    except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
        raise ApiError("prompt_template.unavailable") from exc
    finally:
        connection.close()


def create_prompt_template(
    config: AppConfig,
    context: AuthContext,
    payload: CreatePromptTemplateRequest,
    *,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> PromptTemplateMutationResponse:
    """创建模板并在同一事务封印 v1、两条审计、source event 与幂等 Operation。

    参数：当前认证上下文、严格创建 DTO 和可信链路元数据。返回：当前模板与精确 v1 事实。
    关键约束：模板与版本的循环 owner 只借延迟四列 FK 解决，提交前必须完成 building ->
    published 封印；任一审计、事件或 Operation 失败都回滚全部业务数据。
    """

    normalized_request_id = _normalize_chain_id(request_id, maximum=128)
    normalized_trace_id = _normalize_chain_id(trace_id, maximum=160)
    secret = _prompt_secret(config)
    name = _normalize_name(payload.name)
    description = _normalize_description(payload.description)
    definition = _definition_from_payload(payload)
    project_ids = _normalize_project_bindings(payload.project_scope, payload.project_ids)
    model_alias_ids = _normalize_model_bindings(payload.recommended_model_alias_ids)
    intent = _create_intent(
        context,
        name=name,
        description=description,
        definition=definition,
        project_scope=payload.project_scope,
        project_ids=project_ids,
        model_alias_ids=model_alias_ids,
    )
    intent_hmac = prompt_template_intent_hmac(secret, intent)
    template_id = _new_id("ptpl_")
    version_id = _new_id("ptv_")
    now = _utcnow()
    now_db = _to_db_time(now)
    connection = _connect(config)
    created_audit: AuditRecord | None = None
    publication_audit: AuditRecord | None = None
    event: _PromptTemplateEvent | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        live_context = require_transaction_permission(
            connection,
            context,
            "prompt_template.write",
            unavailable_error_code="prompt_template.unavailable",
        )
        recovered = _recover_operation(
            connection,
            live_context,
            operation="create",
            request_id=normalized_request_id,
            intent=intent,
            secret=secret,
        )
        if recovered is not None:
            connection.rollback()
            return _replayed_response(
                recovered,
                request_id=normalized_request_id,
                trace_id=normalized_trace_id,
            )
        _validate_bindings(
            connection,
            live_context.user.tenant_id,
            project_ids=project_ids,
            model_alias_ids=model_alias_ids,
        )
        owner = PromptTemplateOwner(
            tenant_id=live_context.user.tenant_id,
            prompt_template_id=template_id,
            prompt_template_version_id=version_id,
            version_number=1,
        )
        ciphertext = encrypt_definition(definition, secret=secret, owner=owner)
        definition_bytes = canonical_definition_bytes(definition)
        summary_hmac = change_summary_hmac(secret, definition.change_summary)

        # 先插入 head，再插入引用它的 building version；只有 head 的四列 FK 被显式延迟，
        # 因此既能解决 v1 循环关系，也不会让不完整版本跨事务可见。
        connection.execute(
            """
            INSERT INTO prompt_templates(
              prompt_template_id,tenant_id,name,description,status,current_version_id,
              current_version_state,revision,created_by,updated_by,created_at,updated_at
            ) VALUES(?,?,?,?,'active',?,'published',1,?,?,?,?)
            """,
            (
                template_id,
                live_context.user.tenant_id,
                name,
                description,
                version_id,
                live_context.user.user_id,
                live_context.user.user_id,
                now_db,
                now_db,
            ),
        )
        _insert_building_version(
            connection,
            tenant_id=live_context.user.tenant_id,
            template_id=template_id,
            version_id=version_id,
            version_number=1,
            definition=definition,
            ciphertext=ciphertext,
            definition_size_bytes=len(definition_bytes),
            project_scope=payload.project_scope,
            model_alias_ids=model_alias_ids,
            change_summary_digest=summary_hmac,
            actor_user_id=live_context.user.user_id,
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
            now=now_db,
        )
        _insert_version_bindings(
            connection,
            tenant_id=live_context.user.tenant_id,
            template_id=template_id,
            version_id=version_id,
            project_ids=project_ids,
            model_alias_ids=model_alias_ids,
            now=now_db,
        )
        _seal_version(connection, live_context.user.tenant_id, version_id, now_db)
        template_row = _load_template_row(connection, live_context.user.tenant_id, template_id)
        version_row = _load_version_row(
            connection,
            live_context.user.tenant_id,
            template_id,
            version_id,
        )
        after_summary = _audit_summary(template_row)
        created_audit = record_audit_log(
            connection,
            live_context,
            action="prompt_template.created",
            object_type="prompt_template",
            object_id=template_id,
            object_name=name,
            before_summary={},
            after_summary=after_summary,
            request_meta=audit_request_meta,
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
            created_at=now,
        )
        publication_audit = record_audit_log(
            connection,
            live_context,
            action="prompt_template.version_published",
            object_type="prompt_template_version",
            object_id=version_id,
            object_name=name,
            before_summary={},
            after_summary=after_summary,
            request_meta=audit_request_meta,
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
            created_at=now,
        )
        # 两条审计都是发布事实的 owner，缺任一记录都不得提交模板或 source event。
        if created_audit is None or publication_audit is None:
            raise ApiError("prompt_template.integrity_failed")
        event = _insert_publication_event(
            connection,
            live_context,
            template_row=template_row,
            version_row=version_row,
            publication_audit=publication_audit,
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
            now=now_db,
        )
        _insert_operation(
            connection,
            live_context,
            operation="create",
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
            intent_hmac=intent_hmac,
            template_id=template_id,
            version_id=version_id,
            version_number=1,
            result_revision=1,
            result_audit_log_id=created_audit.audit_log_id,
            now=now_db,
        )
        commit_audited_transaction(connection)
        template = _template_info(template_row)
        version = _version_info(connection, version_row)
    # 加密阶段发现密钥不可用时回滚整笔创建，避免留下无正文或半封印版本。
    except PromptTemplateSecretUnavailable:
        connection.rollback()
        raise ApiError("prompt_template.unavailable") from None
    # 共享合同错误只返回目录化错误码，正文、变量和值均不得进入异常字符串。
    except PromptTemplateContractError as exc:
        connection.rollback()
        raise ApiError(exc.code) from None
    # 所有业务错误都先回滚，再仅映射跨域审计错误，保留其余确定业务语义。
    except ApiError as exc:
        connection.rollback()
        raise _map_write_error(exc) from exc
    # 唯一约束只公开可修正的同租户名称冲突，其他 owner 约束均按完整性失败处理。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        if _is_name_conflict(exc):
            # 精确的 tenant/name 唯一冲突可安全公开，帮助管理员修正名称。
            raise ApiError("prompt_template.name_conflict") from None
        # 其余约束可能涉及 owner 图，统一隐藏为完整性失败以免泄露关系。
        raise ApiError("prompt_template.integrity_failed") from None
    # 数据库忙、断连或 SQL 故障不能泄露路径和语句，统一折叠为暂时不可用。
    except sqlite3.Error as exc:
        connection.rollback()
        raise ApiError("prompt_template.unavailable") from exc
    finally:
        connection.close()

    # 外部派发严格位于提交后。失败不会回滚已发布版本，source event 会记录安全终态。
    dispatch_audit_recorded(
        config,
        context,
        created_audit,
        request_id=normalized_request_id,
        trace_id=normalized_trace_id,
    )
    dispatch_audit_recorded(
        config,
        context,
        publication_audit,
        request_id=normalized_request_id,
        trace_id=normalized_trace_id,
    )
    if event is not None:
        dispatch_prompt_template_event(config, event)
    return PromptTemplateMutationResponse(
        template=template,
        version=version,
        event_id=event.event_id if event is not None else None,
        changed=True,
        replayed=False,
        request_id=normalized_request_id,
        trace_id=normalized_trace_id,
    )


def patch_prompt_template(
    config: AppConfig,
    context: AuthContext,
    template_id: str,
    payload: PatchPromptTemplateRequest,
    *,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> PromptTemplateMutationResponse:
    """按 metadata revision CAS 修改模板名称、描述或启停状态。

    no-op 不推进 revision、不写 Operation/Audit；真实更新把权限复核、CAS、审计与 Operation
    放入同一写事务。禁用提交后，Enterprise Chat 的新 invocation 会在自己的写锁中失败。
    """

    normalized_template_id = _normalize_template_id(template_id)
    normalized_request_id = _normalize_chain_id(request_id, maximum=128)
    normalized_trace_id = _normalize_chain_id(trace_id, maximum=160)
    secret = _prompt_secret(config)
    supplied = payload.model_fields_set.intersection({"name", "description", "status"})
    normalized_values: dict[str, str] = {}
    if "name" in supplied:
        normalized_values["name"] = _normalize_name(payload.name)
    if "description" in supplied:
        normalized_values["description"] = _normalize_description(payload.description)
    if "status" in supplied:
        normalized_values["status"] = _normalize_status(payload.status)
    intent = {
        "operation": "patch",
        "actor_user_id": context.user.user_id,
        "prompt_template_id": normalized_template_id,
        "expected_revision": payload.expected_revision,
        "fields": {key: normalized_values[key] for key in sorted(normalized_values)},
    }
    intent_hmac = prompt_template_intent_hmac(secret, intent)
    now = _utcnow()
    now_db = _to_db_time(now)
    connection = _connect(config)
    audit_record: AuditRecord | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        live_context = require_transaction_permission(
            connection,
            context,
            "prompt_template.write",
            unavailable_error_code="prompt_template.unavailable",
        )
        recovered = _recover_operation(
            connection,
            live_context,
            operation="patch",
            request_id=normalized_request_id,
            intent=intent,
            secret=secret,
        )
        if recovered is not None:
            connection.rollback()
            return _replayed_response(
                recovered,
                request_id=normalized_request_id,
                trace_id=normalized_trace_id,
            )
        before_row = _load_template_row(
            connection,
            live_context.user.tenant_id,
            normalized_template_id,
        )
        # revision 必须在写锁内再次匹配，防止路由校验后发生并发覆盖。
        if int(before_row["revision"]) != payload.expected_revision:
            raise ApiError("prompt_template.revision_conflict")
        new_name = normalized_values.get("name", str(before_row["name"]))
        # SQLite 名称唯一约束使用 NOCASE；仅大小写变化不能绕过 no-op 规则制造虚假 revision。
        if new_name.casefold() == str(before_row["name"]).casefold():
            new_name = str(before_row["name"])
        new_description = normalized_values.get("description", str(before_row["description"]))
        new_status = normalized_values.get("status", str(before_row["status"]))
        changed = (
            new_name != before_row["name"]
            or new_description != before_row["description"]
            or new_status != before_row["status"]
        )
        if not changed:
            template = _template_info(before_row)
            connection.rollback()
            return PromptTemplateMutationResponse(
                template=template,
                version=None,
                event_id=None,
                changed=False,
                replayed=False,
                request_id=normalized_request_id,
                trace_id=normalized_trace_id,
            )
        cursor = connection.execute(
            """
            UPDATE prompt_templates
            SET name=?,description=?,status=?,revision=revision+1,
                updated_by=?,updated_at=?
            WHERE tenant_id=? AND prompt_template_id=? AND revision=?
            """,
            (
                new_name,
                new_description,
                new_status,
                live_context.user.user_id,
                now_db,
                live_context.user.tenant_id,
                normalized_template_id,
                payload.expected_revision,
            ),
        )
        # CAS 未命中表示 head 已漂移，不能把本次更新伪装成成功。
        if cursor.rowcount != 1:
            raise ApiError("prompt_template.revision_conflict")
        after_row = _load_template_row(
            connection,
            live_context.user.tenant_id,
            normalized_template_id,
        )
        audit_record = record_audit_log(
            connection,
            live_context,
            action="prompt_template.updated",
            object_type="prompt_template",
            object_id=normalized_template_id,
            object_name=new_name,
            before_summary=_audit_summary(before_row),
            after_summary=_audit_summary(after_row),
            request_meta=audit_request_meta,
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
            created_at=now,
        )
        # metadata 变更没有同事务审计事实时必须回滚，避免出现不可追踪写入。
        if audit_record is None:
            raise ApiError("prompt_template.integrity_failed")
        _insert_operation(
            connection,
            live_context,
            operation="patch",
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
            intent_hmac=intent_hmac,
            template_id=normalized_template_id,
            version_id=None,
            version_number=None,
            result_revision=int(after_row["revision"]),
            result_audit_log_id=audit_record.audit_log_id,
            now=now_db,
        )
        commit_audited_transaction(connection)
        template = _template_info(after_row)
    # 幂等 HMAC 所需独立密钥失效时回滚写入，禁止借用其他密钥继续执行。
    except PromptTemplateSecretUnavailable:
        connection.rollback()
        raise ApiError("prompt_template.unavailable") from None
    # 合同错误只透传固定目录码，不能携带正文或变量内容。
    except PromptTemplateContractError as exc:
        connection.rollback()
        raise ApiError(exc.code) from None
    # 已分类业务异常先回滚，再把审计依赖错误收敛到模板域。
    except ApiError as exc:
        connection.rollback()
        raise _map_write_error(exc) from exc
    # 仅同租户名称唯一冲突可公开，其他关系约束异常一律失败关闭。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        if _is_name_conflict(exc):
            # 同租户名称冲突是唯一允许公开的约束细节，便于用户改名重试。
            raise ApiError("prompt_template.name_conflict") from None
        # 其他复合外键或触发器失败均代表关系不可信，不向客户端枚举细节。
        raise ApiError("prompt_template.integrity_failed") from None
    # 底层数据库错误不应暴露 SQL、路径或 owner 标识，只返回固定不可用码。
    except sqlite3.Error as exc:
        connection.rollback()
        raise ApiError("prompt_template.unavailable") from exc
    finally:
        connection.close()

    dispatch_audit_recorded(
        config,
        context,
        audit_record,
        request_id=normalized_request_id,
        trace_id=normalized_trace_id,
    )
    return PromptTemplateMutationResponse(
        template=template,
        version=None,
        event_id=None,
        changed=True,
        replayed=False,
        request_id=normalized_request_id,
        trace_id=normalized_trace_id,
    )


def publish_prompt_template_version(
    config: AppConfig,
    context: AuthContext,
    template_id: str,
    payload: PublishPromptTemplateVersionRequest,
    *,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> PromptTemplateMutationResponse:
    """基于模板 revision CAS 发布下一份完整不可变版本。

    发布不支持局部定义更新，也不修改历史版本。项目集合以稳定排序冻结，推荐模型保留用户
    排序；版本 building 装配、head CAS、seal、审计、事件和 Operation 必须原子提交。
    """

    normalized_template_id = _normalize_template_id(template_id)
    normalized_request_id = _normalize_chain_id(request_id, maximum=128)
    normalized_trace_id = _normalize_chain_id(trace_id, maximum=160)
    secret = _prompt_secret(config)
    definition = _definition_from_payload(payload)
    project_ids = _normalize_project_bindings(payload.project_scope, payload.project_ids)
    model_alias_ids = _normalize_model_bindings(payload.recommended_model_alias_ids)
    intent = _publish_intent(
        context,
        template_id=normalized_template_id,
        expected_revision=payload.expected_revision,
        definition=definition,
        project_scope=payload.project_scope,
        project_ids=project_ids,
        model_alias_ids=model_alias_ids,
    )
    intent_hmac = prompt_template_intent_hmac(secret, intent)
    now = _utcnow()
    now_db = _to_db_time(now)
    connection = _connect(config)
    audit_record: AuditRecord | None = None
    event: _PromptTemplateEvent | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        live_context = require_transaction_permission(
            connection,
            context,
            "prompt_template.write",
            unavailable_error_code="prompt_template.unavailable",
        )
        recovered = _recover_operation(
            connection,
            live_context,
            operation="publish",
            request_id=normalized_request_id,
            intent=intent,
            secret=secret,
        )
        if recovered is not None:
            connection.rollback()
            return _replayed_response(
                recovered,
                request_id=normalized_request_id,
                trace_id=normalized_trace_id,
            )
        before_row = _load_template_row(
            connection,
            live_context.user.tenant_id,
            normalized_template_id,
        )
        # 发布依据的 revision 必须在写锁内仍为当前值，防止并发版本覆盖。
        if int(before_row["revision"]) != payload.expected_revision:
            raise ApiError("prompt_template.revision_conflict")
        _validate_bindings(
            connection,
            live_context.user.tenant_id,
            project_ids=project_ids,
            model_alias_ids=model_alias_ids,
        )
        version_number = _next_version_number(
            connection,
            live_context.user.tenant_id,
            normalized_template_id,
        )
        version_id = _new_id("ptv_")
        owner = PromptTemplateOwner(
            tenant_id=live_context.user.tenant_id,
            prompt_template_id=normalized_template_id,
            prompt_template_version_id=version_id,
            version_number=version_number,
        )
        ciphertext = encrypt_definition(definition, secret=secret, owner=owner)
        definition_bytes = canonical_definition_bytes(definition)
        summary_hmac = change_summary_hmac(secret, definition.change_summary)
        _insert_building_version(
            connection,
            tenant_id=live_context.user.tenant_id,
            template_id=normalized_template_id,
            version_id=version_id,
            version_number=version_number,
            definition=definition,
            ciphertext=ciphertext,
            definition_size_bytes=len(definition_bytes),
            project_scope=payload.project_scope,
            model_alias_ids=model_alias_ids,
            change_summary_digest=summary_hmac,
            actor_user_id=live_context.user.user_id,
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
            now=now_db,
        )
        _insert_version_bindings(
            connection,
            tenant_id=live_context.user.tenant_id,
            template_id=normalized_template_id,
            version_id=version_id,
            project_ids=project_ids,
            model_alias_ids=model_alias_ids,
            now=now_db,
        )
        cursor = connection.execute(
            """
            UPDATE prompt_templates
            SET current_version_id=?,revision=revision+1,updated_by=?,updated_at=?
            WHERE tenant_id=? AND prompt_template_id=? AND revision=?
            """,
            (
                version_id,
                live_context.user.user_id,
                now_db,
                live_context.user.tenant_id,
                normalized_template_id,
                payload.expected_revision,
            ),
        )
        # head CAS 未命中说明模板已被并发修改，本次 building 版本必须整体回滚。
        if cursor.rowcount != 1:
            raise ApiError("prompt_template.revision_conflict")
        _seal_version(connection, live_context.user.tenant_id, version_id, now_db)
        after_row = _load_template_row(
            connection,
            live_context.user.tenant_id,
            normalized_template_id,
        )
        version_row = _load_version_row(
            connection,
            live_context.user.tenant_id,
            normalized_template_id,
            version_id,
        )
        audit_record = record_audit_log(
            connection,
            live_context,
            action="prompt_template.version_published",
            object_type="prompt_template_version",
            object_id=version_id,
            object_name=str(after_row["name"]),
            before_summary=_audit_summary(before_row),
            after_summary=_audit_summary(after_row),
            request_meta=audit_request_meta,
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
            created_at=now,
        )
        # 发布事件必须精确引用同事务审计记录，缺失时不得封存为可用版本。
        if audit_record is None:
            raise ApiError("prompt_template.integrity_failed")
        event = _insert_publication_event(
            connection,
            live_context,
            template_row=after_row,
            version_row=version_row,
            publication_audit=audit_record,
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
            now=now_db,
        )
        _insert_operation(
            connection,
            live_context,
            operation="publish",
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
            intent_hmac=intent_hmac,
            template_id=normalized_template_id,
            version_id=version_id,
            version_number=version_number,
            result_revision=int(after_row["revision"]),
            result_audit_log_id=audit_record.audit_log_id,
            now=now_db,
        )
        commit_audited_transaction(connection)
        template = _template_info(after_row)
        version = _version_info(connection, version_row)
    # 发布加密或意图签名缺密钥时回滚全部 building/head 写入，保持失败关闭。
    except PromptTemplateSecretUnavailable:
        connection.rollback()
        raise ApiError("prompt_template.unavailable") from None
    # 定义合同错误只保留固定业务码，不让正文、变量或解析细节进入响应。
    except PromptTemplateContractError as exc:
        connection.rollback()
        raise ApiError(exc.code) from None
    # 已分类业务错误在回滚后保留语义，仅收敛跨域审计依赖失败。
    except ApiError as exc:
        connection.rollback()
        raise _map_write_error(exc) from exc
    # 发布关系约束失败意味着 owner 图不可信，不能猜测为普通名称冲突。
    except sqlite3.IntegrityError:
        connection.rollback()
        raise ApiError("prompt_template.integrity_failed") from None
    # 数据库忙或断连统一映射为暂时不可用，隐藏路径、SQL 和内部异常。
    except sqlite3.Error as exc:
        connection.rollback()
        raise ApiError("prompt_template.unavailable") from exc
    finally:
        connection.close()

    dispatch_audit_recorded(
        config,
        context,
        audit_record,
        request_id=normalized_request_id,
        trace_id=normalized_trace_id,
    )
    if event is not None:
        dispatch_prompt_template_event(config, event)
    return PromptTemplateMutationResponse(
        template=template,
        version=version,
        event_id=event.event_id if event is not None else None,
        changed=True,
        replayed=False,
        request_id=normalized_request_id,
        trace_id=normalized_trace_id,
    )


def dispatch_prompt_template_event(
    config: AppConfig,
    event: _PromptTemplateEvent,
) -> None:
    """提交后派发发布事件，并把 source event 推进到一个固定安全终态。

    参数：应用配置和已由数据库 owner trigger 复核的事件事实。返回值：无。
    关键约束：Endpoint/URL/接收端响应或异常文本都不得进入日志；网络失败不能撤销版本。
    """

    failed = False
    try:
        envelope = build_webhook_event(
            tenant_id=event.tenant_id,
            event_type="prompt_template.published",
            event_id=event.event_id,
            timestamp=event.created_at,
            actor_id=event.actor_user_id,
            object_id=event.version_id,
            trace_id=event.trace_id,
            source_request_id=event.request_id,
            payload=event.payload,
        )
        # 延迟导入避免模板域与 Webhook 服务在应用启动时形成循环依赖。
        from metahub_control.webhooks.service import dispatch_webhook_event

        deliveries = dispatch_webhook_event(
            config,
            event.tenant_id,
            envelope,
            request_id=event.request_id,
            trace_id=event.trace_id,
        )
        # Webhook 服务会把网络失败持久为 delivery 终态并正常返回；source event 也必须反映失败。
        failed = any(getattr(delivery, "status", "failed") != "success" for delivery in deliveries)
    except Exception:
        failed = True
        logger.error(
            "prompt_template.webhook_dispatch_failed",
            extra={
                "event": "prompt_template.webhook_dispatch_failed",
                "tenant_id": event.tenant_id,
                "actor_id": event.actor_user_id,
                "object_id": event.version_id,
                "request_id": event.request_id,
                "trace_id": event.trace_id,
                "error_code": "webhook.delivery_unavailable",
            },
        )
    _finish_source_event(config, event, failed=failed)


def _finish_source_event(
    config: AppConfig,
    event: _PromptTemplateEvent,
    *,
    failed: bool,
) -> None:
    """以单次 CAS 把 pending source event 标为 dispatched 或 failed。

    状态记录属于提交后的可追踪事实；若数据库本身不可用，只写固定安全日志，绝不把路径、
    SQL、Webhook 响应或模板内容带入日志，也不向已成功的创建/发布响应反向抛错。
    """

    try:
        connection = _connect(config)
    # source event 已经提交，连接失败只能记录安全终态告警，不能反向撤销发布。
    except ApiError:
        logger.error(
            "prompt_template.event_finalize_failed",
            extra={
                "event": "prompt_template.event_finalize_failed",
                "tenant_id": event.tenant_id,
                "object_id": event.version_id,
                "request_id": event.request_id,
                "trace_id": event.trace_id,
                "error_code": "prompt_template.unavailable",
            },
        )
        return
    try:
        now = _to_db_time(_utcnow())
        cursor = connection.execute(
            """
            UPDATE prompt_template_events
            SET delivery_status=?,last_error_code=?,updated_at=?,dispatched_at=?
            WHERE tenant_id=? AND prompt_template_event_id=? AND delivery_status='pending'
            """,
            (
                "failed" if failed else "dispatched",
                "webhook.delivery_unavailable" if failed else None,
                now,
                now,
                event.tenant_id,
                event.event_id,
            ),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return
        connection.commit()
    # 终态 CAS 的数据库异常不得携带 SQL 或模板内容，也不能向成功响应追加失败。
    except sqlite3.Error:
        connection.rollback()
        logger.error(
            "prompt_template.event_finalize_failed",
            extra={
                "event": "prompt_template.event_finalize_failed",
                "tenant_id": event.tenant_id,
                "object_id": event.version_id,
                "request_id": event.request_id,
                "trace_id": event.trace_id,
                "error_code": "prompt_template.unavailable",
            },
        )
    finally:
        connection.close()


def _definition_from_payload(
    payload: CreatePromptTemplateRequest | PublishPromptTemplateVersionRequest,
) -> PromptTemplateDefinition:
    """把严格 HTTP DTO 转换为共享定义合同，且不构造可被日志打印的中间正文对象。"""

    try:
        variables = [
            {
                "name": item.name,
                "label": item.label,
                "required": item.required,
                "max_chars": item.max_chars,
            }
            for item in payload.variables
        ]
        return validate_definition(
            content=payload.content,
            variables=variables,
            change_summary=payload.change_summary,
        )
    # 共享解析器的固定错误码可用于表单修正，但正文和变量值始终留在内存中。
    except PromptTemplateContractError as exc:
        raise ApiError(exc.code) from None


def _create_intent(
    context: AuthContext,
    *,
    name: str,
    description: str,
    definition: PromptTemplateDefinition,
    project_scope: str,
    project_ids: tuple[str, ...],
    model_alias_ids: tuple[str, ...],
) -> dict[str, object]:
    """构造创建 Operation 的完整规范意图；该对象仅用于分域 HMAC，不得持久化。"""

    return {
        "operation": "create",
        "actor_user_id": context.user.user_id,
        "prompt_template_id": None,
        "expected_revision": None,
        "name": name,
        "description": description,
        "status": "active",
        "definition": _definition_intent(definition),
        "project_scope": project_scope,
        "project_ids": list(project_ids),
        "recommended_model_alias_ids": list(model_alias_ids),
    }


def _publish_intent(
    context: AuthContext,
    *,
    template_id: str,
    expected_revision: int,
    definition: PromptTemplateDefinition,
    project_scope: str,
    project_ids: tuple[str, ...],
    model_alias_ids: tuple[str, ...],
) -> dict[str, object]:
    """构造发布 Operation 的完整规范意图，覆盖 CAS、定义与两类绑定顺序。"""

    return {
        "operation": "publish",
        "actor_user_id": context.user.user_id,
        "prompt_template_id": template_id,
        "expected_revision": expected_revision,
        "name": None,
        "description": None,
        "status": None,
        "definition": _definition_intent(definition),
        "project_scope": project_scope,
        "project_ids": list(project_ids),
        "recommended_model_alias_ids": list(model_alias_ids),
    }


def _definition_intent(definition: PromptTemplateDefinition) -> dict[str, object]:
    """把内存定义转为 HMAC 所需 JSON 基础类型；调用方不得记录返回对象。"""

    return {
        "content": definition.content,
        "variables": [
            {
                "name": item.name,
                "label": item.label,
                "required": item.required,
                "max_chars": item.max_chars,
            }
            for item in definition.variables
        ],
        "change_summary": definition.change_summary,
    }


def _normalize_name(value: object) -> str:
    """校验模板名称的字符、UTF-8 字节和控制字符边界，保留用户可见原文。"""

    # 禁止隐式字符串转换，避免对象表示或动态类型绕过名称边界。
    if type(value) is not str:
        raise ApiError("prompt_template.request_invalid")
    normalized = value.strip()
    if (
        normalized != value
        or not 1 <= len(normalized) <= 120
        or _utf8_size(normalized) > MAX_NAME_BYTES
        or _has_control_character(normalized)
    ):
        # 空白漂移、不可见字符或超限名称会破坏唯一性及控制台稳定展示。
        raise ApiError("prompt_template.request_invalid")
    return normalized


def _normalize_description(value: object) -> str:
    """校验可见描述长度和 UTF-8 编码，不把描述复制到审计或事件。"""

    # 描述只接受有界严格字符串，避免超大自由文本扩大数据库与响应暴露面。
    if type(value) is not str or len(value) > 1_000 or _utf8_size(value) > MAX_DESCRIPTION_BYTES:
        raise ApiError("prompt_template.request_invalid")
    return value


def _normalize_project_bindings(scope: object, values: object) -> tuple[str, ...]:
    """规范化无序 Project exact-set；``all`` 必须为空，``selected`` 必须非空。"""

    # scope 与集合类型必须精确匹配 wire 合同，禁止字符串迭代等隐式兼容。
    if scope not in {"all", "selected"} or type(values) is not list:
        raise ApiError("prompt_template.definition_invalid")
    # 数量和元素类型在访问数据库前收敛，避免无界占位符和模糊 ID。
    if len(values) > MAX_PROJECT_BINDINGS or any(
        type(item) is not str or not item for item in values
    ):
        # 超量或模糊元素会破坏不可变 exact-set，不能进入数据库绑定查询。
        raise ApiError("prompt_template.definition_invalid")
    # 重复 Project 会破坏 exact-set 与不可变计数，必须在排序前拒绝。
    if len(set(values)) != len(values):
        raise ApiError("prompt_template.definition_invalid")
    normalized = tuple(sorted(values))
    # all 必须对应空集合、selected 必须非空，否则适用范围语义不唯一。
    if (scope == "all" and normalized) or (scope == "selected" and not normalized):
        raise ApiError("prompt_template.definition_invalid")
    return normalized


def _normalize_model_bindings(values: object) -> tuple[str, ...]:
    """校验并保留推荐 ModelAlias 顺序；重复 ID 会破坏连续 display_order，必须拒绝。"""

    if (
        type(values) is not list
        or len(values) > 8
        or any(type(item) is not str or not item for item in values)
        or len(set(values)) != len(values)
    ):
        # 推荐列表必须有界、严格且无重复，才能冻结连续 display_order。
        raise ApiError("prompt_template.definition_invalid")
    return tuple(values)


def _normalize_optional_status(value: object | None) -> str | None:
    """校验列表可选状态；未知枚举统一归入模板请求错误。"""

    if value is None:
        return None
    return _normalize_status(value)


def _normalize_status(value: object) -> str:
    """校验模板 active/disabled 两态，不接受大小写或隐式字符串转换。"""

    # 状态不做大小写或对象转换，避免未知状态绕过启停检查。
    if type(value) is not str or value not in {"active", "disabled"}:
        raise ApiError("prompt_template.request_invalid")
    return value


def _normalize_search(value: object | None) -> str | None:
    """校验有界搜索文本；空白和控制字符不允许变成全量扫描别名。"""

    if value is None:
        return None
    # 搜索参数不接受动态类型，避免 repr 等内容进入 LIKE 查询。
    if type(value) is not str:
        raise ApiError("prompt_template.request_invalid")
    normalized = value.strip()
    # 空白、超长和不可见搜索词都可能退化为异常扫描，必须提前拒绝。
    if not normalized or len(normalized) > MAX_SEARCH_CHARS or _has_control_character(normalized):
        raise ApiError("prompt_template.request_invalid")
    return normalized


def _normalize_limit(value: object) -> int:
    """校验 1..100 严格整数分页上限，bool 不能借 int 子类通过。"""

    # 严格整数和固定上限共同防止 bool 混入或无界列表读取。
    if type(value) is not int or not 1 <= value <= MAX_LIST_LIMIT:
        raise ApiError("prompt_template.request_invalid")
    return value


def _normalize_offset(value: object) -> int:
    """校验非负有界 offset，避免超大扫描拖慢控制面。"""

    # offset 使用严格整数且设硬上限，避免 bool 或极大偏移拖慢控制面。
    if type(value) is not int or not 0 <= value <= MAX_LIST_OFFSET:
        raise ApiError("prompt_template.request_invalid")
    return value


def _normalize_template_id(value: object) -> str:
    """校验公开模板 ID；畸形与跨租户均保持同形 404，避免资源枚举。"""

    if (
        type(value) is not str
        or len(value) != 37
        or not value.startswith("ptpl_")
        or any(character not in "0123456789abcdef" for character in value[5:])
    ):
        # 畸形 ID 与不存在、跨租户资源保持同形 404，不能提供枚举差异。
        raise ApiError("prompt_template.not_found")
    return value


def _normalize_version_id(value: object) -> str:
    """校验公开版本 ID；不向调用者区分格式错误、不存在或另一租户 owner。"""

    if (
        type(value) is not str
        or len(value) != 36
        or not value.startswith("ptv_")
        or any(character not in "0123456789abcdef" for character in value[4:])
    ):
        # 版本 ID 的格式错误同样按资源不可见处理，避免泄露 owner 图。
        raise ApiError("prompt_template.not_found")
    return value


def _normalize_chain_id(value: object, *, maximum: int) -> str:
    """校验将进入不可变关系的 request/trace ID，不回显非法 Header。"""

    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or _CHAIN_ID_PATTERN.fullmatch(value) is None
    ):
        # 链路 ID 会进入不可变关系，非法字符或超限值不得持久化或回显。
        raise ApiError("prompt_template.request_invalid")
    return value


def _prompt_secret(config: AppConfig) -> str:
    """读取独立模板根密钥；缺失时失败关闭且绝不回退 Auth/Provider Secret。"""

    value = config.prompt_template_encryption_secret
    # 独立根密钥缺失时必须失败关闭，禁止回退 Auth、Provider 或其他领域密钥。
    if type(value) is not str or not value:
        raise ApiError("prompt_template.unavailable")
    return value


def _validate_bindings(
    connection: sqlite3.Connection,
    tenant_id: str,
    *,
    project_ids: tuple[str, ...],
    model_alias_ids: tuple[str, ...],
) -> None:
    """在当前写事务内验证两类绑定的同租户 owner 与 active 状态。

    不存在或跨租户统一 ``binding_not_found``；同租户存在但 inactive/环境无效属于定义错误。
    该顺序避免通过错误差异探测另一 tenant，同时让本租户管理员能修正已停用资源。
    """

    if project_ids:
        placeholders = ",".join("?" for _ in project_ids)
        rows = connection.execute(
            "SELECT project_id,status FROM projects "
            f"WHERE tenant_id=? AND project_id IN ({placeholders})",
            (tenant_id, *project_ids),
        ).fetchall()
        # tenant-first 查询缺项时统一同形 404，不能探测另一租户是否存在相同 ID。
        if {str(row["project_id"]) for row in rows} != set(project_ids):
            raise ApiError("prompt_template.binding_not_found")
        # 同租户已停用 Project 可被管理员识别为定义错误，但不得参与新版本发布。
        if any(row["status"] != "active" for row in rows):
            raise ApiError("prompt_template.definition_invalid")
    # 推荐模型也必须在同一写事务复核 owner 与环境，关闭发布前的状态竞态。
    if model_alias_ids:
        placeholders = ",".join("?" for _ in model_alias_ids)
        rows = connection.execute(
            f"""
            SELECT alias.model_alias_id,alias.status,environment.status AS environment_status
            FROM model_aliases alias
            LEFT JOIN environments environment
              ON environment.tenant_id=alias.tenant_id
             AND (environment.environment_id=alias.environment_id
                  OR (alias.environment_id IS NULL AND environment.code=alias.environment))
            WHERE alias.tenant_id=? AND alias.model_alias_id IN ({placeholders})
            """,
            (tenant_id, *model_alias_ids),
        ).fetchall()
        # tenant-first 结果缺项不区分不存在与跨租户，保持绑定资源不可枚举。
        if {str(row["model_alias_id"]) for row in rows} != set(model_alias_ids):
            raise ApiError("prompt_template.binding_not_found")
        # 模型或所属环境非 active 时定义不可执行，不能仅凭 ID 存在继续封印。
        if any(
            row["status"] != "active" or row["environment_status"] != "active"
            for row in rows
        ):
            # 同租户模型或环境停用会使模板不可执行，因此发布必须失败关闭。
            raise ApiError("prompt_template.definition_invalid")


def _insert_building_version(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    template_id: str,
    version_id: str,
    version_number: int,
    definition: PromptTemplateDefinition,
    ciphertext: str,
    definition_size_bytes: int,
    project_scope: str,
    model_alias_ids: tuple[str, ...],
    change_summary_digest: str,
    actor_user_id: str,
    request_id: str,
    trace_id: str,
    now: str,
) -> None:
    """插入仅在当前事务可见的 building version；调用方必须随后装配绑定并封印。"""

    connection.execute(
        """
        INSERT INTO prompt_template_versions(
          prompt_template_version_id,tenant_id,prompt_template_id,version_number,state,
          definition_schema_version,definition_ciphertext,definition_size_bytes,variable_count,
          project_scope,recommended_model_count,change_summary_hmac,created_by,created_at,
          published_at,request_id,trace_id
        ) VALUES(?,?,?,?,'building',?,?,?,?,?,?,?, ?,?,NULL,?,?)
        """,
        (
            version_id,
            tenant_id,
            template_id,
            version_number,
            PROMPT_TEMPLATE_SCHEMA_VERSION,
            ciphertext,
            definition_size_bytes,
            len(definition.variables),
            project_scope,
            len(model_alias_ids),
            change_summary_digest,
            actor_user_id,
            now,
            request_id,
            trace_id,
        ),
    )


def _insert_version_bindings(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    template_id: str,
    version_id: str,
    project_ids: tuple[str, ...],
    model_alias_ids: tuple[str, ...],
    now: str,
) -> None:
    """在 building 阶段写入不可变 Project exact-set 与有序 ModelAlias 推荐绑定。"""

    for project_id in project_ids:
        connection.execute(
            """
            INSERT INTO prompt_template_version_projects(
              version_project_id,tenant_id,prompt_template_id,
              prompt_template_version_id,project_id,created_at
            ) VALUES(?,?,?,?,?,?)
            """,
            (_new_id("ptvp_"), tenant_id, template_id, version_id, project_id, now),
        )
    for display_order, model_alias_id in enumerate(model_alias_ids):
        connection.execute(
            """
            INSERT INTO prompt_template_version_models(
              version_model_id,tenant_id,prompt_template_id,prompt_template_version_id,
              model_alias_id,display_order,created_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                _new_id("ptvm_"),
                tenant_id,
                template_id,
                version_id,
                model_alias_id,
                display_order,
                now,
            ),
        )


def _seal_version(
    connection: sqlite3.Connection,
    tenant_id: str,
    version_id: str,
    published_at: str,
) -> None:
    """执行唯一合法的 building -> published 状态跳转，并要求数据库 trigger 复核完整绑定。"""

    cursor = connection.execute(
        """
        UPDATE prompt_template_versions
        SET state='published',published_at=?
        WHERE tenant_id=? AND prompt_template_version_id=? AND state='building'
        """,
        (published_at, tenant_id, version_id),
    )
    # 非唯一状态跳转表示版本已被篡改或重复封印，不能把未知状态作为已发布返回。
    if cursor.rowcount != 1:
        raise ApiError("prompt_template.integrity_failed")


def _next_version_number(
    connection: sqlite3.Connection,
    tenant_id: str,
    template_id: str,
) -> int:
    """在写锁内计算连续下一版本号，并拒绝动态类型或缺失历史。"""

    row = connection.execute(
        """
        SELECT MAX(version_number) AS maximum,COUNT(*) AS total
        FROM prompt_template_versions
        WHERE tenant_id=? AND prompt_template_id=?
        """,
        (tenant_id, template_id),
    ).fetchone()
    # 聚合行缺失或动态类型异常表示版本历史不可信，禁止猜测下一版本号。
    if row is None or type(row["maximum"]) is not int or type(row["total"]) is not int:
        raise ApiError("prompt_template.integrity_failed")
    # 最大版本号必须等于历史条数，任何缺口都说明不可变版本链已损坏。
    if row["maximum"] != row["total"] or row["maximum"] < 1:
        raise ApiError("prompt_template.integrity_failed")
    return int(row["maximum"]) + 1


def _load_template_row(
    connection: sqlite3.Connection,
    tenant_id: str,
    template_id: str,
) -> sqlite3.Row:
    """按 tenant-qualified owner 读取模板当前 head；跨租户与不存在保持同形 404。"""

    rows = connection.execute(
        f"""
        {_TEMPLATE_SELECT}
        WHERE template.tenant_id=? AND template.prompt_template_id=?
        """,
        (tenant_id, template_id),
    ).fetchall()
    # tenant-qualified 查询无结果时与跨租户保持同形 404，避免资源枚举。
    if not rows:
        raise ApiError("prompt_template.not_found")
    # 主键查询出现多行说明 owner 约束损坏，不能任取一行继续处理。
    if len(rows) != 1:
        raise ApiError("prompt_template.integrity_failed")
    return rows[0]


def _load_version_row(
    connection: sqlite3.Connection,
    tenant_id: str,
    template_id: str,
    version_id: str,
) -> sqlite3.Row:
    """读取同 tenant、同模板的精确 published 版本，不允许从独立弱关系拼 owner。"""

    rows = connection.execute(
        """
        SELECT version.*,
          (SELECT COUNT(*) FROM prompt_template_version_projects binding
           WHERE binding.tenant_id=version.tenant_id
             AND binding.prompt_template_version_id=version.prompt_template_version_id)
            AS project_count
        FROM prompt_template_versions version
        WHERE version.tenant_id=? AND version.prompt_template_id=?
          AND version.prompt_template_version_id=? AND version.state='published'
        """,
        (tenant_id, template_id, version_id),
    ).fetchall()
    # 精确 tenant/template/version owner 无结果时统一按不可见资源处理。
    if not rows:
        raise ApiError("prompt_template.not_found")
    # 精确 owner 查询必须唯一，多行结果属于数据库完整性故障。
    if len(rows) != 1:
        raise ApiError("prompt_template.integrity_failed")
    return rows[0]


def _template_info(row: sqlite3.Row) -> PromptTemplateInfo:
    """把已连接 current published version 的数据库行解码为安全模板 DTO。"""

    try:
        status = str(row["status"])
        revision = row["revision"]
        version_number = row["version_number"]
        project_count = row["project_count"]
        recommended_count = row["recommended_model_count"]
        if (
            status not in {"active", "disabled"}
            or type(revision) is not int
            or type(version_number) is not int
            or type(project_count) is not int
            or type(recommended_count) is not int
            or revision < 1
            or version_number < 1
            or not 0 <= project_count <= MAX_PROJECT_BINDINGS
            or not 0 <= recommended_count <= 8
        ):
            # head 元数据类型、范围或状态异常时不得构造看似合法的管理 DTO。
            raise ValueError
        return PromptTemplateInfo(
            prompt_template_id=str(row["prompt_template_id"]),
            name=str(row["name"]),
            description=str(row["description"]),
            status=status,
            metadata_revision=revision,
            current_version_id=str(row["current_version_id"]),
            current_version_number=version_number,
            project_count=project_count,
            recommended_model_count=recommended_count,
            created_at=_parse_db_time(row["created_at"]),
            updated_at=_parse_db_time(row["updated_at"]),
        )
    # 行缺字段、类型漂移或范围异常统一失败关闭，不向客户端暴露原始持久值。
    except (KeyError, TypeError, ValueError):
        raise ApiError("prompt_template.integrity_failed") from None


def _version_info(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> PromptTemplateVersionInfo:
    """复核不可变版本计数与绑定顺序，并构造不含定义正文的安全 DTO。"""

    try:
        tenant_id = str(row["tenant_id"])
        template_id = str(row["prompt_template_id"])
        version_id = str(row["prompt_template_version_id"])
        version_number = row["version_number"]
        project_scope = str(row["project_scope"])
        project_count = row["project_count"]
        recommended_count = row["recommended_model_count"]
        variable_count = row["variable_count"]
        definition_size = row["definition_size_bytes"]
        if (
            row["state"] != "published"
            or row["definition_schema_version"] != PROMPT_TEMPLATE_SCHEMA_VERSION
            or type(version_number) is not int
            or type(project_count) is not int
            or type(recommended_count) is not int
            or type(variable_count) is not int
            or type(definition_size) is not int
            or project_scope not in {"all", "selected"}
            or version_number < 1
            or not 0 <= project_count <= MAX_PROJECT_BINDINGS
            or not 0 <= recommended_count <= 8
            or not 0 <= variable_count <= 32
            or not 1 <= definition_size <= 131_072
        ):
            # published header 必须满足冻结 schema 与所有计数边界，否则版本不可被调用。
            raise ValueError
        project_rows = connection.execute(
            """
            SELECT project_id FROM prompt_template_version_projects
            WHERE tenant_id=? AND prompt_template_id=? AND prompt_template_version_id=?
            ORDER BY project_id
            """,
            (tenant_id, template_id, version_id),
        ).fetchall()
        model_rows = connection.execute(
            """
            SELECT model_alias_id,display_order FROM prompt_template_version_models
            WHERE tenant_id=? AND prompt_template_id=? AND prompt_template_version_id=?
            ORDER BY display_order
            """,
            (tenant_id, template_id, version_id),
        ).fetchall()
        project_ids = [str(item["project_id"]) for item in project_rows]
        model_alias_ids = [str(item["model_alias_id"]) for item in model_rows]
        if (
            len(project_ids) != project_count
            or len(model_alias_ids) != recommended_count
            or [item["display_order"] for item in model_rows] != list(range(recommended_count))
            or (project_scope == "all" and project_ids)
            or (project_scope == "selected" and not project_ids)
            or project_ids != sorted(set(project_ids))
            or len(set(model_alias_ids)) != len(model_alias_ids)
        ):
            # 绑定数量、集合语义和推荐顺序必须与不可变 header 完全一致。
            raise ValueError
        published_at = _parse_db_time(row["published_at"])
        created_at = _parse_db_time(row["created_at"])
        # 发布时间早于创建时间表示不可变事实被篡改，不能继续返回该版本。
        if published_at < created_at:
            raise ValueError
        return PromptTemplateVersionInfo(
            prompt_template_version_id=version_id,
            prompt_template_id=template_id,
            version_number=version_number,
            project_scope=project_scope,
            project_ids=project_ids,
            recommended_model_alias_ids=model_alias_ids,
            variable_count=variable_count,
            project_count=project_count,
            recommended_model_count=recommended_count,
            definition_size_bytes=definition_size,
            created_at=created_at,
            published_at=published_at,
        )
    # 时间解析等内层完整性错误已具备固定语义，必须原样保留。
    except ApiError:
        raise
    # 任意行、绑定或数据库解码异常都收敛为同一个完整性错误，隐藏内部数据。
    except (KeyError, TypeError, ValueError, sqlite3.Error):
        raise ApiError("prompt_template.integrity_failed") from None


def _verify_definition_counts(
    row: sqlite3.Row,
    definition: PromptTemplateDefinition,
) -> None:
    """解密后复核 canonical 大小、变量数与变更说明 HMAC 之外的结构计数。"""

    try:
        if (
            len(definition.variables) != row["variable_count"]
            or len(canonical_definition_bytes(definition)) != row["definition_size_bytes"]
        ):
            # 解密定义必须与不可变 header 的变量数和 canonical 字节数完全匹配。
            raise ValueError
    # 计数、canonical 编码或字段类型不一致说明认证后的定义仍不可安全使用。
    except (KeyError, TypeError, ValueError, PromptTemplateContractError):
        raise ApiError("prompt_template.definition_unavailable") from None


def _variable_info(value: object) -> PromptTemplateVariableInfo:
    """把共享不可变变量声明转换为授权页面/Chat 可复用的安全字段定义。"""

    try:
        return PromptTemplateVariableInfo(
            name=value.name,
            label=value.label,
            required=value.required,
            max_chars=value.max_chars,
        )
    # 共享变量对象形态漂移时拒绝生成 UI DTO，避免把不完整 schema 暴露给调用方。
    except (AttributeError, TypeError, ValueError):
        raise ApiError("prompt_template.definition_unavailable") from None


def _audit_summary(row: sqlite3.Row) -> dict[str, object]:
    """构造 AuditLog 允许的九字段安全摘要，不复制名称以外的自由文本或绑定清单。"""

    try:
        return {
            "status": str(row["status"]),
            "revision": int(row["revision"]),
            "version_number": int(row["version_number"]),
            "project_scope": str(row["project_scope"]),
            "project_count": int(row["project_count"]),
            "recommended_model_count": int(row["recommended_model_count"]),
            "definition_size_bytes": int(row["definition_size_bytes"]),
            "variable_count": int(row["variable_count"]),
            "change_summary_hmac": str(row["change_summary_hmac"]),
        }
    # 审计摘要字段缺失或类型异常时必须阻止业务提交，不能记录失真的治理事实。
    except (KeyError, TypeError, ValueError):
        raise ApiError("prompt_template.integrity_failed") from None


def _insert_publication_event(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    template_row: sqlite3.Row,
    version_row: sqlite3.Row,
    publication_audit: AuditRecord,
    request_id: str,
    trace_id: str,
    now: str,
) -> _PromptTemplateEvent:
    """在发布事务内写七字段安全 source event，并从数据库回读可信派发事实。"""

    event_id = _new_id("ptev_")
    payload: dict[str, object] = {
        "template_id": str(template_row["prompt_template_id"]),
        "version_id": str(version_row["prompt_template_version_id"]),
        "version_number": int(version_row["version_number"]),
        "template_revision": int(template_row["revision"]),
        "project_scope": str(version_row["project_scope"]),
        "project_count": int(version_row["project_count"]),
        "recommended_model_count": int(version_row["recommended_model_count"]),
    }
    payload_json = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    connection.execute(
        """
        INSERT INTO prompt_template_events(
          prompt_template_event_id,tenant_id,prompt_template_id,
          prompt_template_version_id,version_number,event_type,actor_user_id,
          publication_audit_log_id,request_id,trace_id,payload_json,delivery_status,
          last_error_code,created_at,updated_at,dispatched_at
        ) VALUES(?,?,?,?,?,'prompt_template.published',?,?,?,?,?,'pending',NULL,?,?,NULL)
        """,
        (
            event_id,
            context.user.tenant_id,
            payload["template_id"],
            payload["version_id"],
            payload["version_number"],
            context.user.user_id,
            publication_audit.audit_log_id,
            request_id,
            trace_id,
            payload_json,
            now,
            now,
        ),
    )
    row = connection.execute(
        """
        SELECT * FROM prompt_template_events
        WHERE tenant_id=? AND prompt_template_event_id=?
        """,
        (context.user.tenant_id, event_id),
    ).fetchone()
    # source event 插入后必须能按同租户 owner 精确回读，否则发布事务不可提交。
    if row is None:
        raise ApiError("prompt_template.integrity_failed")
    return _event_from_row(row)


def _event_from_row(row: sqlite3.Row) -> _PromptTemplateEvent:
    """严格解码 source event；任意 payload 字段漂移都按完整性错误失败关闭。"""

    try:
        payload = json.loads(str(row["payload_json"]))
        expected_keys = {
            "template_id",
            "version_id",
            "version_number",
            "template_revision",
            "project_scope",
            "project_count",
            "recommended_model_count",
        }
        # Webhook payload 必须恰好七字段，额外字段可能复制敏感正文或扩大兼容面。
        if type(payload) is not dict or set(payload) != expected_keys:
            raise ValueError
        if (
            row["event_type"] != "prompt_template.published"
            or payload["template_id"] != row["prompt_template_id"]
            or payload["version_id"] != row["prompt_template_version_id"]
            or payload["version_number"] != row["version_number"]
        ):
            # payload 身份必须与 source row 完全一致，禁止交换版本或伪造发布事实。
            raise ValueError
        return _PromptTemplateEvent(
            event_id=str(row["prompt_template_event_id"]),
            tenant_id=str(row["tenant_id"]),
            template_id=str(row["prompt_template_id"]),
            version_id=str(row["prompt_template_version_id"]),
            actor_user_id=str(row["actor_user_id"]),
            request_id=str(row["request_id"]),
            trace_id=str(row["trace_id"]),
            payload=payload,
            created_at=_parse_db_time(row["created_at"]),
        )
    # 任意 JSON、字段或 owner 漂移都按内部完整性错误失败关闭，不返回原始 payload。
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ApiError("prompt_template.integrity_failed") from None


def _insert_operation(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    operation: Literal["create", "patch", "publish"],
    request_id: str,
    trace_id: str,
    intent_hmac: str,
    template_id: str,
    version_id: str | None,
    version_number: int | None,
    result_revision: int,
    result_audit_log_id: str,
    now: str,
) -> None:
    """写入不含明文的不可变幂等事实；Operation 与版本字段关系由数据库再次约束。"""

    connection.execute(
        """
        INSERT INTO prompt_template_operations(
          prompt_template_operation_id,tenant_id,actor_user_id,request_id,trace_id,
          operation,intent_hmac,prompt_template_id,prompt_template_version_id,
          version_number,result_template_revision,result_audit_log_id,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _new_id("ptop_"),
            context.user.tenant_id,
            context.user.user_id,
            request_id,
            trace_id,
            operation,
            intent_hmac,
            template_id,
            version_id,
            version_number,
            result_revision,
            result_audit_log_id,
            now,
        ),
    )


def _recover_operation(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    operation: Literal["create", "patch", "publish"],
    request_id: str,
    intent: dict[str, object],
    secret: str,
) -> _RecoveredOperation | None:
    """按 tenant+request ID 恢复幂等事实，并以常量时间核对 actor/action/完整意图。

    Operation 只提供原 version 身份和当前权威模板；它不保存响应快照，因此模板后续更新后
    也不会伪造历史时点 DTO。任何 actor、action 或 HMAC 不一致固定返回 409。
    """

    row = connection.execute(
        """
        SELECT * FROM prompt_template_operations
        WHERE tenant_id=? AND request_id=?
        """,
        (context.user.tenant_id, request_id),
    ).fetchone()
    if row is None:
        return None
    # request ID 只能重放同 actor、同操作和同完整意图，防止跨用户或变更内容复用。
    if (
        row["actor_user_id"] != context.user.user_id
        or row["operation"] != operation
        or not verify_prompt_template_intent_hmac(secret, intent, row["intent_hmac"])
    ):
        # actor、动作或完整意图任一变化都禁止复用 request ID，防止越权重放。
        raise ApiError("prompt_template.idempotency_conflict")
    template_id = str(row["prompt_template_id"])
    template = _template_info(
        _load_template_row(connection, context.user.tenant_id, template_id)
    )
    version: PromptTemplateVersionInfo | None = None
    event_id: str | None = None
    if operation in {"create", "publish"}:
        version_id = row["prompt_template_version_id"]
        version_number = row["version_number"]
        # create/publish Operation 必须冻结精确版本身份，动态或缺失字段表示事实损坏。
        if type(version_id) is not str or type(version_number) is not int:
            raise ApiError("prompt_template.integrity_failed")
        version_row = _load_version_row(
            connection,
            context.user.tenant_id,
            template_id,
            version_id,
        )
        # Operation 与不可变版本号必须一致，禁止重放时拼接到另一版本事实。
        if version_row["version_number"] != version_number:
            raise ApiError("prompt_template.integrity_failed")
        version = _version_info(connection, version_row)
        event_rows = connection.execute(
            """
            SELECT prompt_template_event_id FROM prompt_template_events
            WHERE tenant_id=? AND prompt_template_version_id=?
              AND event_type='prompt_template.published'
            """,
            (context.user.tenant_id, version_id),
        ).fetchall()
        # 每个已发布版本只能有一个 source event，缺失或重复都不可重建确定响应。
        if len(event_rows) != 1:
            raise ApiError("prompt_template.integrity_failed")
        event_id = str(event_rows[0]["prompt_template_event_id"])
    # metadata patch 不得伪装成版本操作；出现版本字段说明 Operation 关系被破坏。
    elif row["prompt_template_version_id"] is not None or row["version_number"] is not None:
        raise ApiError("prompt_template.integrity_failed")
    return _RecoveredOperation(template=template, version=version, event_id=event_id)


def _replayed_response(
    recovered: _RecoveredOperation,
    *,
    request_id: str,
    trace_id: str,
) -> PromptTemplateMutationResponse:
    """把 Operation 恢复结果编码为当前请求的 no-store 重放响应，不再次写审计或事件。"""

    return PromptTemplateMutationResponse(
        template=recovered.template,
        version=recovered.version,
        event_id=recovered.event_id,
        changed=True,
        replayed=True,
        request_id=request_id,
        trace_id=trace_id,
    )


def _map_write_error(error: ApiError) -> ApiError:
    """把跨域审计存储错误折叠为模板域固定错误，其余业务码保持原样。"""

    # 审计存储细节属于跨域依赖，只映射为模板暂不可用并隐藏内部错误分类。
    if error.code in {"audit_log.record_unavailable", "audit_log.summary_invalid"}:
        return ApiError("prompt_template.unavailable")
    return error


def _is_name_conflict(error: sqlite3.IntegrityError) -> bool:
    """只把明确的 tenant/name 唯一冲突公开为可修正 409，其他约束保持内部化。"""

    text = str(error)
    return (
        "prompt_templates.tenant_id, prompt_templates.name" in text
        or "UNIQUE constraint failed: prompt_templates.tenant_id, prompt_templates.name" in text
    )


def _new_id(prefix: str) -> str:
    """生成迁移冻结前缀加 32 位小写 hex 的稳定领域 ID。"""

    return f"{prefix}{uuid4().hex}"


def _escape_like(value: str) -> str:
    """转义 SQLite LIKE 元字符，使用户搜索文本不扩大筛选语义。"""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _has_control_character(value: str) -> bool:
    """识别 Unicode control/format/surrogate 等不可见字符，保护名称与搜索展示边界。"""

    return any(unicodedata.category(character).startswith("C") for character in value)


def _utf8_size(value: str) -> int:
    """返回严格 UTF-8 字节数；孤立代理项统一按请求非法处理。"""

    try:
        return len(value.encode("utf-8", errors="strict"))
    # 孤立代理项无法形成稳定 canonical 字节，统一按请求非法且不回显原值。
    except UnicodeEncodeError:
        raise ApiError("prompt_template.request_invalid") from None


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建启用复合外键的数据库连接，并隐藏路径和驱动错误。"""

    try:
        return connect_database(config.database_url)
    # 连接错误可能包含数据库路径或驱动信息，必须折叠为模板域固定不可用码。
    except DbError:
        raise ApiError("prompt_template.unavailable") from None


def _utcnow() -> datetime:
    """返回去微秒 UTC，保证 Audit/version/event/Operation 使用完全相同的数据库时间。"""

    return datetime.now(UTC).replace(microsecond=0)


def _to_db_time(value: datetime) -> str:
    """把时区时间编码为 SQLite owner trigger 可比较的 canonical UTC 秒文本。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_db_time(value: object) -> datetime:
    """严格解析数据库 UTC 时间；非法持久值按模板完整性错误失败关闭。"""

    # 持久时间必须是 canonical 字符串，禁止对动态类型做隐式解析。
    if type(value) is not str:
        raise ApiError("prompt_template.integrity_failed")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 非法时间文本属于持久化完整性故障，原始值不得进入公共异常。
    except ValueError:
        raise ApiError("prompt_template.integrity_failed") from None
    # 无时区时间无法证明 UTC owner 顺序，必须失败关闭而非猜测本地时区。
    if parsed.tzinfo is None:
        raise ApiError("prompt_template.integrity_failed")
    return parsed.astimezone(UTC)
