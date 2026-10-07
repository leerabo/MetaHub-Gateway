"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：shared-sdk/python/metahub_shared/db/seed.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from ..rbac import ROLE_CODES
from .connection import connect_database
from .errors import DbError
from .schema import table_columns, table_exists
from .security import (
    configured_secret,
    hash_password,
    hash_secret,
    placeholder_encrypted_secret,
    secret_fingerprint,
    secret_prefix,
    verify_password,
)


@dataclass(frozen=True)
class SeedResult:
    """承载 数据库基础设施 的 SeedResult 数据结构，明确跨模块传递时允许出现的字段。

    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    run_id: str
    tenant_id: str
    admin_email: str
    provider_code: str
    model_alias: str
    virtual_key_prefix: str


def apply_seed_data(
    database_url: str | None = None,
    *,
    seed_file: Path | None = None,
) -> SeedResult:
    """写入默认租户、管理员、测试 Provider、模型和 Virtual Key。

    种子数据必须幂等：重复执行不能产生重复用户、Provider 或模型。所有业务数据都写入
    tenant_id，后续服务层才能强制按租户查询。
    """

    run_id = f"seed_{uuid4().hex}"
    seed = _load_seed(
        seed_file or Path(os.getenv("METAHUB_SEED_FILE", "infra/seeds/default_seed.json"))
    )
    tenant = seed["tenant"]
    admin = seed["admin"]
    provider = seed["provider"]
    model = seed["model"]
    virtual_key = configured_secret("METAHUB_SEED_VIRTUAL_KEY", seed["virtual_key"]["plain_value"])

    tenant_id = _stable_uuid(f"tenant:{tenant['slug']}")
    provider_id = _stable_uuid(f"provider:{tenant_id}:{provider['provider_code']}")
    model_id = _stable_uuid(f"model:{tenant_id}:{model['model_alias']}")
    user_id = _stable_uuid(f"user:{tenant_id}:{admin['email'].lower()}")
    virtual_key_id = _stable_uuid(f"virtual-key:{tenant_id}:{virtual_key}")
    credential_secret = configured_secret(
        "METAHUB_SEED_PROVIDER_API_KEY", provider["api_key_placeholder"]
    )
    credential_id = _stable_uuid(f"provider-credential:{tenant_id}:{provider_id}")

    connection = connect_database(database_url or os.getenv("METAHUB_DATABASE_URL"))
    try:
        tenant_created = _upsert_tenant(connection, tenant_id, tenant)
        _upsert_roles(connection, tenant_id)
        _upsert_user(connection, user_id, tenant_id, admin)
        _ensure_workspace_operational_states(
            connection, tenant_id, initialize_new_tenant=tenant_created,
        )
        _upsert_provider(connection, provider_id, tenant_id, provider)
        _upsert_provider_credential(
            connection, credential_id, tenant_id, provider_id, credential_secret
        )
        _upsert_model(connection, model_id, tenant_id, provider_id, model)
        _upsert_commercial_provider_cost_rate(
            connection, model_id, tenant_id, provider_id, model
        )
        _upsert_virtual_key(
            connection, virtual_key_id, tenant_id, virtual_key, seed["virtual_key"]["name"]
        )
        _record_seed_run(
            connection, run_id, tenant_id, seed_file or Path("infra/seeds/default_seed.json")
        )
        connection.commit()
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        raise DbError(
            "db.constraint_violation",
            f"Seed data violates a database constraint: {exc}",
            "Check seed values and unique constraints.",
        ) from exc
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except sqlite3.Error as exc:
        connection.rollback()
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        raise DbError(
            "db.seed_failed",
            f"Seed failed: {exc}",
            "Check migration state and seed input.",
        ) from exc
    finally:
        # Seed owns this connection; release the SQLite file handle on every path.
        connection.close()

    return SeedResult(
        run_id=run_id,
        tenant_id=tenant_id,
        admin_email=admin["email"],
        provider_code=provider["provider_code"],
        model_alias=model["model_alias"],
        virtual_key_prefix=secret_prefix(virtual_key),
    )


def _load_seed(path: Path) -> dict[str, dict[str, str]]:
    """按当前租户加载 数据库基础设施 的 load seed 目标对象，供后续写入或详情响应复用。

    安全：查询必须带 tenant_id 或可信上下文，避免跨租户读取。
    """

    if not path.exists():
        # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
        raise DbError(
            "db.seed_failed",
            f"Seed file does not exist: {path}",
            "Check infra/seeds/default_seed.json or METAHUB_SEED_FILE.",
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _stable_uuid(value: str) -> str:
    """在 数据库基础设施 内部复用 stable uuid 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    return str(uuid5(NAMESPACE_URL, f"https://metahub.local/{value}"))


def _upsert_tenant(connection: sqlite3.Connection, tenant_id: str, tenant: dict[str, str]) -> bool:
    """在 数据库基础设施 内部复用 upsert tenant 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    inserted = connection.execute(
        """
        INSERT INTO tenants(tenant_id, slug, name, service_mode, status, created_at, updated_at)
        VALUES(?, ?, ?, ?, 'active', datetime('now'), datetime('now'))
        ON CONFLICT(slug) DO NOTHING
        """,
        (tenant_id, tenant["slug"], tenant["name"], tenant.get("service_mode", "internal_only")),
    )
    if inserted.rowcount == 1:
        return True
    connection.execute(
        """UPDATE tenants SET name=?,service_mode=?,status='active',updated_at=datetime('now')
           WHERE slug=?""",
        (tenant["name"], tenant.get("service_mode", "internal_only"), tenant["slug"]),
    )
    return False


def _ensure_workspace_operational_states(
    connection: sqlite3.Connection, tenant_id: str, *, initialize_new_tenant: bool = False,
) -> None:
    if not table_exists(connection, "workspace_operational_states"):
        return
    connection.execute(
        """
        INSERT INTO workspace_operational_states(
          workspace_operational_state_id,workspace_id,tenant_id,status,revision,risk_revision,
          source,suspended_reason_code,suspended_by_operator_id,suspended_at,created_at,updated_at)
        SELECT ?,workspace.workspace_id,binding.tenant_id,
          CASE WHEN workspace.status='active' AND binding.status='active'
              AND tenant.status='active' AND data_state.lifecycle_status='active'
            THEN 'active' ELSE 'migration_conflict' END,
          1,1,'workspace_creation',NULL,NULL,NULL,
          strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
        FROM workspace_tenant_bindings binding
        JOIN workspaces workspace ON workspace.workspace_id=binding.workspace_id
        JOIN tenants tenant ON tenant.tenant_id=binding.tenant_id
        JOIN tenant_data_states data_state ON data_state.tenant_id=binding.tenant_id
        WHERE binding.tenant_id=?
        ON CONFLICT(workspace_id) DO NOTHING
        """,
        ("wos_" + uuid4().hex, tenant_id),
    )
    if initialize_new_tenant:
        # Seed's synchronous triggers may create the state before all new bindings settle.
        connection.execute(
            """UPDATE workspace_operational_states
               SET status='active',revision=revision+1,
                   updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
               WHERE tenant_id=? AND status='migration_conflict' AND source='workspace_creation'
                 AND revision=1 AND risk_revision=1
                 AND suspended_reason_code IS NULL AND suspended_by_operator_id IS NULL
                 AND suspended_at IS NULL AND EXISTS(
                   SELECT 1 FROM workspace_tenant_bindings binding
                   JOIN workspaces workspace ON workspace.workspace_id=binding.workspace_id
                   JOIN tenants tenant ON tenant.tenant_id=binding.tenant_id
                   JOIN tenant_data_states data_state ON data_state.tenant_id=binding.tenant_id
                   WHERE binding.workspace_id=workspace_operational_states.workspace_id
                     AND binding.tenant_id=workspace_operational_states.tenant_id
                     AND binding.status='active' AND workspace.status='active'
                     AND tenant.status='active' AND data_state.lifecycle_status='active')""",
            (tenant_id,),
        )


def _upsert_roles(connection: sqlite3.Connection, tenant_id: str) -> None:
    """初始化或验证当前迁移版本对应的固定角色目录。

    0050 之前保留旧三角色 upsert，以支持历史 down/up 专项；0050 之后角色和默认矩阵由
    tenant 初始化触发器原子创建，这里只复核六角色，禁止 seed 更新不可变角色或恢复已撤销权限。
    """

    columns = table_columns(connection, "roles")
    if "matrix_sha256" in columns:
        _validate_permission_roles(connection, tenant_id)
        return

    legacy_roles = [
        (
            "admin",
            "管理员",
            "管理当前租户配置、用户、Provider 和 Key。",
        ),
        (
            "developer",
            "开发者",
            "接入和调试模型调用，不能修改系统级配置。",
        ),
        (
            "viewer",
            "只读用户",
            "查看控制台信息，不能创建 Key 或修改配置。",
        ),
    ]
    for code, name, description in legacy_roles:
        connection.execute(
            """
            INSERT INTO roles(role_id, tenant_id, code, name, description, status, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))
            ON CONFLICT(tenant_id, code) DO UPDATE SET
              name=excluded.name,
              description=excluded.description,
              status='active',
              updated_at=datetime('now')
            """,
            (_stable_uuid(f"role:{tenant_id}:{code}"), tenant_id, code, name, description),
        )


def _validate_permission_roles(connection: sqlite3.Connection, tenant_id: str) -> None:
    """复核 0050 新租户已原子生成完整六角色目录。

    参数：数据库连接和可信 seed tenant_id。返回值：无。
    约束：缺项、跨租户、禁用角色、非法 revision/hash 或 admin 可变均视为数据库完整性错误；
    此函数绝不修补部分目录，避免 seed 掩盖迁移或 trigger 损坏。
    """

    rows = connection.execute(
        """
        SELECT code,tenant_id,status,permission_revision,matrix_sha256,is_permission_immutable
        FROM roles WHERE tenant_id=? ORDER BY code
        """,
        (tenant_id,),
    ).fetchall()
    # seed 只复核迁移/tenant trigger 的完整目录，绝不补写缺失角色或恢复已撤销权限。
    if len(rows) != len(ROLE_CODES) or {row["code"] for row in rows} != set(ROLE_CODES):
        raise sqlite3.IntegrityError("rbac_seed_role_catalog_invalid")
    for row in rows:
        revision = row["permission_revision"]
        matrix_sha256 = row["matrix_sha256"]
        expected_immutable = 1 if row["code"] == "admin" else 0
        if (
            row["tenant_id"] != tenant_id
            or row["status"] != "active"
            or type(revision) is not int
            or revision < 1
            or type(matrix_sha256) is not str
            or len(matrix_sha256) != 64
            or any(character not in "0123456789abcdef" for character in matrix_sha256)
            or row["is_permission_immutable"] != expected_immutable
        ):
            # 单行 owner、revision、hash 或不可变标志异常都表明租户目录损坏，必须整体失败。
            raise sqlite3.IntegrityError("rbac_seed_role_catalog_invalid")


def _upsert_user(
    connection: sqlite3.Connection,
    user_id: str,
    tenant_id: str,
    admin: dict[str, str],
) -> None:
    """在 数据库基础设施 内部复用 upsert user 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    password = configured_secret("METAHUB_SEED_ADMIN_PASSWORD", admin["password"])
    email = os.getenv("METAHUB_SEED_ADMIN_EMAIL", admin["email"]).lower()
    existing = connection.execute(
        "SELECT password_hash FROM users WHERE tenant_id=? AND email=?",
        (tenant_id, email),
    ).fetchone()
    password_hash = (
        existing["password_hash"]
        if existing is not None and verify_password(password, existing["password_hash"])
        else hash_password(password)
    )
    connection.execute(
        """
        INSERT INTO users(user_id, tenant_id, email, display_name, password_hash, role, status, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, 'admin', 'active', datetime('now'), datetime('now'))
        ON CONFLICT(tenant_id, email) DO UPDATE SET
          display_name=excluded.display_name,
          password_hash=excluded.password_hash,
          role='admin',
          status='active',
          updated_at=datetime('now')
        """,
        (
            user_id,
            tenant_id,
            email,
            admin["display_name"],
            password_hash,
        ),
    )


def _upsert_provider(
    connection: sqlite3.Connection,
    provider_id: str,
    tenant_id: str,
    provider: dict[str, str],
) -> None:
    """在 数据库基础设施 内部复用 upsert provider 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    base_url = os.getenv(
        f"METAHUB_PROVIDER_{provider['provider_code'].upper()}_BASE_URL",
        provider["base_url"],
    )
    connection.execute(
        """
        INSERT INTO providers(
          provider_id, tenant_id, provider_code, name, base_url, status,
          provider_type, auth_type, health_status, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, 'active', 'openai_compatible', 'bearer', 'unknown', datetime('now'), datetime('now'))
        ON CONFLICT(tenant_id, provider_code) DO UPDATE SET
          name=excluded.name,
          base_url=excluded.base_url,
          status='active',
          provider_type='openai_compatible',
          auth_type='bearer',
          updated_at=datetime('now')
        """,
        (provider_id, tenant_id, provider["provider_code"], provider["name"], base_url),
    )


def _upsert_provider_credential(
    connection: sqlite3.Connection,
    credential_id: str,
    tenant_id: str,
    provider_id: str,
    secret: str,
) -> None:
    """在 数据库基础设施 内部复用 upsert provider credential 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    connection.execute(
        """
        INSERT INTO provider_credentials(
          provider_credential_id, tenant_id, provider_id, key_fingerprint, encrypted_key, status, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))
        ON CONFLICT(provider_credential_id) DO UPDATE SET
          key_fingerprint=excluded.key_fingerprint,
          encrypted_key=excluded.encrypted_key,
          status='active',
          updated_at=datetime('now')
        """,
        (
            credential_id,
            tenant_id,
            provider_id,
            secret_fingerprint(secret),
            placeholder_encrypted_secret(secret),
        ),
    )


def _upsert_model(
    connection: sqlite3.Connection,
    model_id: str,
    tenant_id: str,
    provider_id: str,
    model: dict[str, str],
) -> None:
    """在 数据库基础设施 内部复用 upsert model 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    capabilities = json.dumps({"chat": True, "stream": True}, ensure_ascii=False)
    provider_model_id = model.get("provider_model_id") or model["upstream_model_name"]
    input_price = float(model.get("input_price_per_1k") or 0)
    output_price = float(model.get("output_price_per_1k") or 0)
    cache_price = float(
        model.get("cache_price_per_1k", model.get("input_price_per_1k") or 0) or 0
    )
    currency = (model.get("currency") or "CNY").upper()
    price_schema_version = model.get("price_schema_version") or "seed-v1"
    # Historical migration fixtures intentionally stop before the cache-price
    # column exists. Keep seed replay compatible with those schemas while
    # populating the cache price whenever the current column is available.
    has_cache_price = "cache_price_per_1k" in table_columns(connection, "models")
    cache_columns = ", cache_price_per_1k" if has_cache_price else ""
    cache_values = ", ?" if has_cache_price else ""
    cache_update = ", cache_price_per_1k=excluded.cache_price_per_1k" if has_cache_price else ""
    sql = f"""
        INSERT INTO models(
          model_id, tenant_id, provider_id, model_alias, upstream_model_name, provider_model_id,
          display_name, capabilities, context_window, input_price_per_1k, output_price_per_1k{cache_columns},
          currency, price_schema_version, status, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?{cache_values}, ?, ?, 'active', datetime('now'), datetime('now'))
        ON CONFLICT(tenant_id, model_alias) DO UPDATE SET
          upstream_model_name=excluded.upstream_model_name,
          provider_model_id=excluded.provider_model_id,
          display_name=excluded.display_name,
          capabilities=excluded.capabilities,
          context_window=excluded.context_window,
          input_price_per_1k=excluded.input_price_per_1k,
          output_price_per_1k=excluded.output_price_per_1k{cache_update},
          currency=excluded.currency,
          price_schema_version=excluded.price_schema_version,
          status='active',
          updated_at=datetime('now')
        """
    parameters = [
        model_id,
        tenant_id,
        provider_id,
        model["model_alias"],
        provider_model_id,
        provider_model_id,
        model.get("display_name") or model["model_alias"],
        capabilities,
        int(model.get("context_window") or 0),
        input_price,
        output_price,
    ]
    if has_cache_price:
        parameters.append(cache_price)
    parameters.extend([currency, price_schema_version])
    connection.execute(sql, parameters)


def _upsert_commercial_provider_cost_rate(
    connection: sqlite3.Connection,
    model_id: str,
    tenant_id: str,
    provider_id: str,
    model: dict[str, str],
) -> None:
    if not table_exists(connection, "workspace_provider_cost_rates"):
        return
    owner = connection.execute(
        """
        SELECT binding.workspace_id,membership.workspace_membership_id
        FROM workspace_tenant_bindings binding
        JOIN workspace_memberships membership
          ON membership.workspace_id=binding.workspace_id
         AND membership.tenant_id=binding.tenant_id
        WHERE binding.tenant_id=? AND binding.status='active'
          AND membership.role_code='owner' AND membership.status='active'
        ORDER BY membership.created_at,membership.workspace_membership_id
        LIMIT 2
        """,
        (tenant_id,),
    ).fetchall()
    if len(owner) != 1:
        raise sqlite3.IntegrityError("seed commercial rate owner invalid")
    currency = (model.get("currency") or "CNY").upper()
    existing = connection.execute(
        """
        SELECT 1 FROM workspace_provider_cost_rates
        WHERE workspace_id=? AND tenant_id=? AND provider_id=? AND model_id=?
          AND currency=?
        """,
        (owner[0]["workspace_id"], tenant_id, provider_id, model_id, currency),
    ).fetchone()
    if existing is not None:
        return
    try:
        input_scaled = Decimal(model.get("input_price_per_1k") or "0") * Decimal(
            1_000_000
        )
        output_scaled = Decimal(model.get("output_price_per_1k") or "0") * Decimal(
            1_000_000
        )
        cache_scaled = Decimal(
            model.get("cache_price_per_1k", model.get("input_price_per_1k") or "0") or "0"
        ) * Decimal(1_000_000)
    except (InvalidOperation, TypeError, ValueError):
        raise sqlite3.IntegrityError("seed commercial rate invalid") from None
    if (
        input_scaled != input_scaled.to_integral_value()
        or output_scaled != output_scaled.to_integral_value()
        or cache_scaled != cache_scaled.to_integral_value()
    ):
        raise sqlite3.IntegrityError("seed commercial rate precision invalid")
    input_micros = int(input_scaled)
    output_micros = int(output_scaled)
    cache_micros = int(cache_scaled)
    workspace_id = str(owner[0]["workspace_id"])
    membership_id = str(owner[0]["workspace_membership_id"])
    rate_id = "pcr_" + hashlib.sha256(
        (
            f"provider-rate|{workspace_id}|{tenant_id}|{provider_id}|{model_id}|{currency}"
        ).encode()
    ).hexdigest()[:32]
    version_id = "pcrv_" + hashlib.sha256(
        f"legacy-model|{model_id}".encode()
    ).hexdigest()[:32]
    source_sha = hashlib.sha256(
        json.dumps(
            {
                "model_id": model_id,
                "source_type": "legacy_model",
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    content_sha = hashlib.sha256(
        json.dumps(
            {
                "currency": currency,
                "input_rate_micros_per_1k": input_micros,
                "model_id": model_id,
                "output_rate_micros_per_1k": output_micros,
                "cache_rate_micros_per_1k": cache_micros,
                "provider_id": provider_id,
                "source_type": "legacy_model",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    connection.execute(
        """
        INSERT INTO workspace_provider_cost_rates(
          workspace_provider_cost_rate_id,workspace_id,tenant_id,provider_id,model_id,
          currency,created_at)
        VALUES(?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        """,
        (rate_id, workspace_id, tenant_id, provider_id, model_id, currency),
    )
    connection.execute(
        """
        INSERT INTO workspace_provider_cost_rate_versions(
          workspace_provider_cost_rate_version_id,workspace_provider_cost_rate_id,
          version_number,input_rate_micros_per_1k,output_rate_micros_per_1k,
          cache_rate_micros_per_1k,
          effective_from,effective_until,source_type,source_id,source_sha256,status,
          revision,content_sha256,created_by_membership_id,created_at)
        VALUES(?,?,1,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'),NULL,
          'legacy_model',?,?,'active',1,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        """,
        (
            version_id,
            rate_id,
            input_micros,
            output_micros,
            cache_micros,
            model_id,
            source_sha,
            content_sha,
            membership_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO workspace_provider_cost_rate_heads(
          workspace_provider_cost_rate_id,workspace_provider_cost_rate_version_id,
          head_revision,adoption_state,updated_at)
        VALUES(?,?,1,'active',strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        """,
        (rate_id, version_id),
    )
    connection.execute(
        """
        UPDATE commercial_accounting_migration_cursors
        SET owner_coverage_count=(
          SELECT count(*) FROM workspace_provider_cost_rate_versions),
          updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
        WHERE source_kind='provider_rates'
        """
    )


def _upsert_virtual_key(
    connection: sqlite3.Connection,
    virtual_key_id: str,
    tenant_id: str,
    virtual_key: str,
    name: str,
) -> None:
    """在 数据库基础设施 内部复用 upsert virtual key 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    environment = connection.execute(
        """
        SELECT environment_id
        FROM environments
        WHERE tenant_id=? AND code='dev' AND status='active'
        """,
        (tenant_id,),
    ).fetchone()
    if environment is None:
        # 0021 以后 Virtual Key 的 Environment 是权威关系；缺失时不能再写入半绑定种子数据。
        raise sqlite3.IntegrityError("seed active dev environment missing")
    connection.execute(
        """
        INSERT INTO virtual_keys(
          virtual_key_id, tenant_id, name, key_prefix, key_hash, status,
          environment, environment_id, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, 'active', 'dev', ?, datetime('now'), datetime('now'))
        ON CONFLICT(key_hash) DO UPDATE SET
          name=excluded.name,
          status='active',
          environment='dev',
          environment_id=excluded.environment_id,
          updated_at=datetime('now')
        """,
        (
            virtual_key_id,
            tenant_id,
            name,
            secret_prefix(virtual_key),
            hash_secret(virtual_key),
            environment["environment_id"],
        ),
    )


def _record_seed_run(
    connection: sqlite3.Connection,
    run_id: str,
    tenant_id: str,
    seed_file: Path,
) -> None:
    """在 数据库基础设施 内部复用 record seed run 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    checksum_source = (
        seed_file.read_text(encoding="utf-8") if seed_file.exists() else str(seed_file)
    )
    checksum = hash_secret(checksum_source)
    connection.execute(
        """
        INSERT INTO seed_runs(seed_run_id, tenant_id, seed_name, checksum, status, created_at, updated_at)
        VALUES(?, ?, ?, ?, 'applied', datetime('now'), datetime('now'))
        """,
        (run_id, tenant_id, seed_file.name, checksum),
    )
