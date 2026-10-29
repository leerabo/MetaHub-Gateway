"""为 PRD-085 PAGE-117 提供具备目录归属保护的浏览器验收服务。

该服务管理空白 0078 SQLite 数据库及真实控制台进程；正式证据只保留固定事实、
计数、状态和来源哈希，任何请求响应正文及私密值都不得进入验收产物。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import secrets
import socket
import sys
import threading
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx

# 仓库根目录是动态加载、运行目录和来源证明共同使用的可信文件边界。
ROOT = Path(__file__).resolve().parents[2]


def _load_base() -> Any:
    """加载并缓存 PRD-083 浏览器基础设施，复用其目录和进程安全能力。"""
    name = "_metahub_prd083_browser_for_prd085"
    current = sys.modules.get(name)
    if current is not None:
        return current
    path = ROOT / "scripts" / "acceptance" / "prd083-browser-harness.py"
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        # 基础模块不可验证时立即终止，避免退化到缺少归属保护的执行路径。
        raise RuntimeError("browser harness base unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


# 基础模块导出的运行、失败和参数工具构成当前验收服务的公共安全契约。
BASE = _load_base()
RUNTIME = BASE.RUNTIME
AcceptanceFailure = BASE.AcceptanceFailure
SafeArgumentParser = BASE.SafeArgumentParser
require = BASE.require

from metahub_shared.db import (  # noqa: E402
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.security import hash_password  # noqa: E402
from metahub_shared.rbac import permission_matrix_sha256  # noqa: E402

# 固定目录、端口、凭据变量、迁移版本和证据白名单约束本次验收的全部外部边界。
WORK_ROOT = ROOT / ".tmp" / "prd085-browser-harness"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd085-browser"
LOOPBACK = "127.0.0.1"
HARNESS_PORT = 18887
OWNER_FILENAME = ".prd085-browser-owner"
ADMIN_PASSWORD_ENV = "PRD085_BROWSER_ADMIN_PASSWORD"
ROLE_PASSWORD_ENV = "PRD085_BROWSER_ROLE_PASSWORD"
STOP_TOKEN_ENV = "PRD085_BROWSER_STOP_TOKEN"
EXPECTED_MIGRATION_COUNT = 78
EXPECTED_MIGRATION_VERSION = "0078"
PREVIOUS_MIGRATION_VERSION = "0077"
OPS_EMAIL = "ops-prd085@local.invalid"
OPS_USER_ID = "usr_prd085_browser_ops"
DEVELOPER_EMAIL = "developer-prd085@local.invalid"
DEVELOPER_USER_ID = "usr_prd085_browser_developer"
SECONDARY_TENANT_ID = "tenant_prd085_browser_secondary"
SECONDARY_TENANT_SLUG = "prd085-secondary"
SECONDARY_EMAIL = "secondary-prd085@local.invalid"
SECONDARY_USER_ID = "usr_prd085_browser_secondary_admin"
SCREENSHOTS = (
    "page-117-growth-analytics-desktop-1280x720.png",
    "page-117-growth-analytics-mobile-390x844.png",
    "page-117-initial-state-desktop-1280x720.png",
    "page-117-tenant-isolation-mobile-390x844.png",
)
ALLOWED_ARTIFACTS = frozenset((*SCREENSHOTS, "browser-acceptance.json", "sha256-manifest.json"))
SOURCE_FILES = {
    "browser_harness": Path(__file__).resolve(),
    "browser_driver": ROOT / "scripts" / "acceptance" / "prd085-browser-acceptance.mjs",
    "browser_base_prd083": ROOT / "scripts" / "acceptance" / "prd083-browser-harness.py",
    "browser_helper_prd080": ROOT / "scripts" / "acceptance" / "prd080-browser-acceptance.mjs",
    "browser_helper_prd066": ROOT / "scripts" / "acceptance" / "prd066-browser-acceptance.mjs",
    "browser_helper_prd056": ROOT / "scripts" / "acceptance" / "prd056-browser-acceptance.mjs",
    "runtime_helper_prd078": ROOT / "scripts" / "acceptance" / "prd078-runtime-acceptance.py",
    "harness_contract": ROOT
    / "scripts"
    / "acceptance"
    / "tests"
    / "test_prd085_browser_harness.py",
    "driver_contract": ROOT
    / "console-web"
    / "tests"
    / "unit"
    / "prd085BrowserAcceptance.test.mjs",
    "migration_up": ROOT / "infra" / "migrations" / "0078_product_analytics_activation_funnel.sql",
    "migration_down": ROOT
    / "infra"
    / "migrations"
    / "0078_product_analytics_activation_funnel.down.sql",
    "console_main": ROOT / "console-web" / "src" / "app" / "main.js",
    "console_client": ROOT / "console-web" / "src" / "lib" / "productAnalytics.js",
    "console_state": ROOT
    / "console-web"
    / "src"
    / "features"
    / "product-analytics"
    / "productAnalyticsPageState.js",
    "console_renderer": ROOT
    / "console-web"
    / "src"
    / "features"
    / "product-analytics"
    / "renderProductAnalytics.js",
    "control_routes": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "product_analytics"
    / "routes.py",
    "control_service": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "product_analytics"
    / "service.py",
    "control_app": ROOT / "control-plane-api" / "src" / "metahub_control" / "app.py",
    "auth_dependencies": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "auth"
    / "dependencies.py",
    "auth_service": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "auth"
    / "service.py",
    "auth_tokens": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "auth"
    / "tokens.py",
    "console_service": ROOT
    / "control-plane-api"
    / "src"
    / "metahub_control"
    / "console"
    / "service.py",
    "shared_rbac": ROOT / "shared-sdk" / "python" / "metahub_shared" / "rbac.py",
    "shared_errors": ROOT
    / "shared-sdk"
    / "python"
    / "metahub_shared"
    / "runtime"
    / "errors.py",
}


@dataclass(frozen=True, slots=True)
class BrowserSecrets:
    """封装浏览器验收所需私密值，并禁止数据类表示输出其真实内容。"""

    runtime: RUNTIME.RuntimeSecrets = field(repr=False)
    stop_token: str = field(repr=False)

    @classmethod
    def from_environment(cls) -> BrowserSecrets:
        """从受控环境读取外部凭据，并补齐运行验收内部生成的其他私密值。"""
        admin_password = os.environ.get(ADMIN_PASSWORD_ENV, "")
        role_password = os.environ.get(ROLE_PASSWORD_ENV, "")
        stop_token = os.environ.get(STOP_TOKEN_ENV, "")
        require(len(admin_password) >= 32, "config.admin_password", "invalid")
        require(len(role_password) >= 32, "config.role_password", "invalid")
        require(len(stop_token) >= 43, "config.stop_token", "invalid")
        generated = RUNTIME.RuntimeSecrets.create()
        return cls(
            runtime=replace(generated, admin_password=admin_password, role_password=role_password),
            stop_token=stop_token,
        )

    def values(self, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
        """汇总隐私扫描必须拦截的运行凭据、停止令牌和附加会话值。"""

        # 停止令牌与会话值只能作为扫描针值使用，调用方不得将其写入证据。
        return self.runtime.values((self.stop_token, *extra))


@dataclass(frozen=True, slots=True)
class BrowserGraph:
    """保存浏览器验收数据库、租户角色和可逆迁移结果的内部运行图。"""

    database: Path = field(repr=False)
    database_url: str = field(repr=False)
    tenant_id: str
    authorized_role_id: str
    unauthorized_role_id: str
    migration: dict[str, object]


def _database_url(path: Path) -> str:
    """把仓库内数据库文件转换成共享数据库层接受的 SQLite 连接地址。"""
    return f"sqlite:///{path.as_posix()}"


def _migration_state(database_url: str) -> dict[str, object]:
    """读取已应用迁移版本、完整性结果和外键违规数形成可比较快照。"""
    with closing(connect_database(database_url)) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS item_count,MAX(version) AS version "
            "FROM schema_migrations WHERE status='applied'"
        ).fetchone()
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    return {
        "count": int(row["item_count"]),
        "version": str(row["version"]),
        "integrity": integrity,
        "foreign_key_violations": foreign_keys,
    }


def _expected_state(count: int, version: str) -> dict[str, object]:
    """构造指定迁移数量与版本对应的零损坏、零外键违规预期状态。"""
    return {
        "count": count,
        "version": version,
        "integrity": "ok",
        "foreign_key_violations": 0,
    }


def prepare_database(work: Path, runtime: RUNTIME.RuntimeSecrets) -> BrowserGraph:
    """从空库验证 0078 往返迁移并播种双租户 PAGE-117 验收数据。"""
    database = BASE.BASE._direct_child(work, "prd085-browser.sqlite3", stage="database.path")
    database_url = _database_url(database)
    fresh = apply_migrations(database_url)
    require(
        len(fresh.applied) == EXPECTED_MIGRATION_COUNT
        and fresh.applied[-1:] == [EXPECTED_MIGRATION_VERSION],
        "migration.fresh",
        "result",
    )
    fresh_state = _migration_state(database_url)
    require(fresh_state == _expected_state(78, "0078"), "migration.fresh", "state")
    repeat = apply_migrations(database_url)
    require(not repeat.applied and len(repeat.skipped) == 78, "migration.repeat", "result")
    repeat_state = _migration_state(database_url)
    require(repeat_state == fresh_state, "migration.repeat", "state")
    down = rollback_last_migration(database_url, expected_version=EXPECTED_MIGRATION_VERSION)
    require(down.rolled_back == EXPECTED_MIGRATION_VERSION, "migration.down", "result")
    down_state = _migration_state(database_url)
    require(down_state == _expected_state(77, "0077"), "migration.down", "state")
    reup = apply_migrations(database_url)
    require(reup.applied == [EXPECTED_MIGRATION_VERSION], "migration.reup", "result")
    reup_state = _migration_state(database_url)
    require(reup_state == fresh_state, "migration.reup", "state")

    with RUNTIME.temporary_environment(
        {
            "METAHUB_SEED_ADMIN_PASSWORD": runtime.admin_password,
            "METAHUB_SEED_PROVIDER_API_KEY": runtime.provider_key,
            "METAHUB_SEED_VIRTUAL_KEY": runtime.virtual_key,
        }
    ):
        seed = apply_seed_data(database_url)

    connection = connect_database(database_url)
    try:
        roles = {
            str(row["code"]): str(row["role_id"])
            for row in connection.execute(
                "SELECT role_id,code FROM roles WHERE tenant_id=? "
                "AND code IN('ops_admin','developer')",
                (seed.tenant_id,),
            ).fetchall()
        }
        require(set(roles) == {"ops_admin", "developer"}, "database.seed", "roles")
        now = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        connection.executemany(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'active',?,?)
            """,
            (
                (
                    OPS_USER_ID,
                    seed.tenant_id,
                    OPS_EMAIL,
                    "PRD-085 Ops",
                    hash_password(runtime.role_password),
                    "ops_admin",
                    now,
                    now,
                ),
                (
                    DEVELOPER_USER_ID,
                    seed.tenant_id,
                    DEVELOPER_EMAIL,
                    "PRD-085 Developer",
                    hash_password(runtime.role_password),
                    "developer",
                    now,
                    now,
                ),
            ),
        )
        # 主租户增加一条无敏感字段的真实 Provider 审计来源，使其漏斗 2/7 与次租户 1/7 可区分。
        connection.execute(
            """
            INSERT INTO audit_logs(
              audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,object_type,
              object_id,object_name,before_summary_json,after_summary_json,changed_fields_json,
              client_ip,user_agent,request_id,trace_id,created_at
            ) VALUES(
              'aud_08500000000000000000000000000001',?,?,?,'ops_admin','provider.created',
              'provider','provider_prd085_browser',NULL,'{}','{"status":"active"}',
              '["status"]',NULL,NULL,'req_prd085_browser_provider',
              'trc_prd085_browser_provider',?
            )
            """,
            (seed.tenant_id, OPS_USER_ID, OPS_EMAIL, now),
        )
        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES(?,?,?,'internal_only','active',?,?)
            """,
            (SECONDARY_TENANT_ID, SECONDARY_TENANT_SLUG, "PRD-085 Secondary", now, now),
        )
        connection.execute(
            """
            INSERT INTO seed_runs(
              seed_run_id,tenant_id,seed_name,checksum,status,created_at,updated_at
            ) VALUES(?,?,?,?,'applied',?,?)
            """,
            (
                "seed_prd085_browser_secondary",
                SECONDARY_TENANT_ID,
                "prd085-browser-secondary",
                hashlib.sha256(b"prd085-browser-secondary").hexdigest(),
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,'admin','active',?,?)
            """,
            (
                SECONDARY_USER_ID,
                SECONDARY_TENANT_ID,
                SECONDARY_EMAIL,
                "PRD-085 Secondary Admin",
                hash_password(runtime.admin_password),
                now,
                now,
            ),
        )
        connection.commit()

        grants = dict(
            connection.execute(
                """
                SELECT role.code,COUNT(permission.permission_id) FROM roles role
                LEFT JOIN role_permissions relation ON relation.tenant_id=role.tenant_id
                  AND relation.role_id=role.role_id
                LEFT JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                  AND permission.permission_id=relation.permission_id
                  AND permission.code='product_analytics.read'
                WHERE role.tenant_id=? AND role.code IN('ops_admin','developer')
                GROUP BY role.code
                """,
                (seed.tenant_id,),
            ).fetchall()
        )
        secondary_grant = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM role_permissions relation
                JOIN roles role ON role.tenant_id=relation.tenant_id
                  AND role.role_id=relation.role_id
                JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                  AND permission.permission_id=relation.permission_id
                WHERE role.tenant_id=? AND role.code='admin'
                  AND permission.code='product_analytics.read' AND permission.catalog_revision=20
                """,
                (SECONDARY_TENANT_ID,),
            ).fetchone()[0]
        )
        secondary_events = int(
            connection.execute(
                "SELECT COUNT(*) FROM product_events WHERE tenant_id=?",
                (SECONDARY_TENANT_ID,),
            ).fetchone()[0]
        )
        require(grants == {"developer": 0, "ops_admin": 1}, "database.seed", "grants")
        require(secondary_grant == 1 and secondary_events == 1, "database.seed", "secondary")
        require(
            connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            and not connection.execute("PRAGMA foreign_key_check").fetchall(),
            "database.seed",
            "integrity",
        )
    finally:
        connection.close()
    return BrowserGraph(
        database=database,
        database_url=database_url,
        tenant_id=seed.tenant_id,
        authorized_role_id=roles["ops_admin"],
        unauthorized_role_id=roles["developer"],
        migration={
            "fresh": fresh_state,
            "repeat": repeat_state,
            "down": down_state,
            "reup": reup_state,
        },
    )


# 桌面、移动端、权限生命周期与网络事实集合定义浏览器证据的精确模式。
DESKTOP_FACT_KEYS = {
    "width",
    "height",
    "nonblank",
    "page_117",
    "seven_stages",
    "backend_counts_visible",
    "backend_rates_visible",
    "initial_state_visible",
    "horizontal_overflow_free",
    "content_bounded",
    "text_overflow_free",
    "overlap_count",
    "no_sensitive_dom",
}
MOBILE_FACT_KEYS = {
    "width",
    "height",
    "nonblank",
    "page_117",
    "seven_stages",
    "backend_counts_visible",
    "backend_rates_visible",
    "tenant_isolation_visible",
    "horizontal_overflow_free",
    "content_bounded",
    "text_overflow_free",
    "overlap_count",
    "no_sensitive_dom",
}
LIFECYCLE_FACT_KEYS = {
    "authorized_navigation_visible",
    "authorized_page_visible",
    "unauthorized_navigation_absent",
    "unauthorized_page_absent",
    "unauthorized_api_prefetch_absent",
    "backend_permission_denied",
    "dynamic_revocation_root_cleared",
    "dynamic_revocation_navigation_cleared",
    "dynamic_revocation_dom_cleared",
    "late_response_discarded",
    "permission_denied_dom_cleared",
    "session_expired_dom_cleared",
    "late_auth_failure_discarded",
    "secondary_navigation_visible",
    "secondary_tenant_independent",
    "previous_tenant_dom_cleared",
    "registered_private_values",
    "logout_storage_cleared",
}
NETWORK_FACT_KEYS = {
    "external_requests",
    "forbidden_queries",
    "console_errors",
    "page_errors",
    "login_statuses",
    "logout_statuses",
    "funnel_get_count",
    "funnel_statuses",
    "no_store_responses",
    "request_contract_exact",
    "no_response_body_capture",
}


def _boolean_facts(value: object, expected: set[str], *, stage: str) -> dict[str, object]:
    """校验一个浏览器事实分区字段精确，并要求除尺寸计数外全部为真。"""
    require(type(value) is dict and set(value) == expected, stage, "shape")
    for name, item in value.items():
        if name not in {"width", "height", "overlap_count"}:
            require(item is True, stage, name)
    return value


def validate_browser_facts(value: object) -> dict[str, object]:
    """校验浏览器提交的完整事实结构、状态码序列、截图和全局状态。"""
    require(type(value) is dict, "browser.complete", "shape")
    require(
        set(value)
        == {
            "status",
            "desktop",
            "mobile",
            "lifecycle",
            "network",
            "screenshots",
            "all_108_prds_complete",
        },
        "browser.complete",
        "shape",
    )
    require(
        value["status"] == "passed" and value["all_108_prds_complete"] is False,
        "browser.complete",
        "status",
    )
    desktop = _boolean_facts(value["desktop"], DESKTOP_FACT_KEYS, stage="browser.desktop")
    mobile = _boolean_facts(value["mobile"], MOBILE_FACT_KEYS, stage="browser.mobile")
    require(
        (desktop["width"], desktop["height"], desktop["overlap_count"])
        == (1280, 720, 0),
        "browser.desktop",
        "layout",
    )
    require(
        (mobile["width"], mobile["height"], mobile["overlap_count"])
        == (390, 844, 0),
        "browser.mobile",
        "layout",
    )
    _boolean_facts(value["lifecycle"], LIFECYCLE_FACT_KEYS, stage="browser.lifecycle")
    network = value["network"]
    require(type(network) is dict and set(network) == NETWORK_FACT_KEYS, "browser.network", "shape")
    require(
        network["external_requests"] == network["forbidden_queries"] == 0
        and network["console_errors"] == network["page_errors"] == 0
        and network["login_statuses"] == [200, 200, 200, 200, 200, 200]
        and network["logout_statuses"] == [200, 200, 401, 200, 200]
        and network["funnel_get_count"] == 9
        and network["funnel_statuses"] == [200, 200, 200, 403, 200, 401, 200, 401, 200]
        and network["no_store_responses"] is True
        and network["request_contract_exact"] is True
        and network["no_response_body_capture"] is True,
        "browser.network",
        "facts",
    )
    require(value["screenshots"] == list(SCREENSHOTS), "browser.complete", "screenshots")
    return value


def _source_provenance() -> dict[str, str]:
    """计算仓库内固定来源文件的 SHA-256，拒绝链接或边界外文件。"""
    result: dict[str, str] = {}
    for name, path in SOURCE_FILES.items():
        require(
            path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(ROOT),
            "evidence.source",
            "missing",
        )
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _assert_source_snapshot(expected: dict[str, str]) -> None:
    """确认验收全过程来源文件未发生漂移，防止证据对应不同代码快照。"""
    require(
        set(expected) == set(SOURCE_FILES) and _source_provenance() == expected,
        "evidence.source",
        "changed",
    )


def _event_counts(connection: Any, tenant_id: str) -> tuple[int, int]:
    """按租户统计产品事件总数与不同漏斗阶段数，验证隔离后的进度。"""
    row = connection.execute(
        "SELECT COUNT(*) AS event_count,COUNT(DISTINCT event_type) AS stage_count "
        "FROM product_events WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    return int(row["event_count"]), int(row["stage_count"])


# The owner helpers are defined by the PRD-082 base and resolve module globals
# at call time. Bind those globals to this harness before exposing the helpers.
for _name, _value in {
    "WORK_ROOT": WORK_ROOT,
    "ARTIFACT_ROOT": ARTIFACT_ROOT,
    "HARNESS_PORT": HARNESS_PORT,
    "OWNER_FILENAME": OWNER_FILENAME,
    "SCREENSHOTS": SCREENSHOTS,
    "ALLOWED_ARTIFACTS": ALLOWED_ARTIFACTS,
    "SOURCE_FILES": SOURCE_FILES,
    "validate_browser_facts": validate_browser_facts,
    "_source_provenance": _source_provenance,
    "_assert_source_snapshot": _assert_source_snapshot,
}.items():
    setattr(BASE.BASE, _name, _value)


create_run_directories = BASE.create_run_directories
_remove_owned_directory = BASE._remove_owned_directory


class BrowserHarness:
    """协调真实服务、浏览器控制端、权限漂移、隐私扫描与证据发布。"""

    def __init__(self, run_id: str, secrets_: BrowserSecrets) -> None:
        """建立本次运行的所有者、来源快照、目录和可变验收状态。"""
        self.run_id = RUNTIME.safe_run_id(run_id)
        self.secrets = secrets_
        self.owner = hashlib.sha256(secrets.token_bytes(48)).hexdigest()
        self.source_snapshot = _source_provenance()
        self.work, self.staging, self.final = create_run_directories(self.run_id, self.owner)
        self.processes = RUNTIME.OwnedProcesses()
        self.graph: BrowserGraph | None = None
        self.browser_facts: dict[str, object] | None = None
        self.private_extra: tuple[str, ...] = ()
        self.private_extra_labels: tuple[str, ...] = ()
        self.backend_permission_denied = False
        self.backend_secondary_isolated = False
        self.permissions_revoked = False
        self.permission_revocation_count = 0
        self.permission_restore_count = 0
        self.session_invalidation_count = 0
        self.private_registered = False
        self.server: ThreadingHTTPServer | None = None
        self.lock = threading.Lock()

    def _assert_ports_available(self) -> None:
        """启动服务前独占探测固定回环端口，避免连接到非本次运行进程。"""
        for port in (RUNTIME.CONTROL_PORT, RUNTIME.CONSOLE_PORT, HARNESS_PORT):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind((LOOPBACK, port))
            # 端口所有者无法证明时失败关闭，不能复用未知进程完成验收。
            except OSError:
                raise AcceptanceFailure("config.port", f"unavailable_{port}") from None
            finally:
                probe.close()

    def prepare(self) -> None:
        """准备数据库并启动真实控制面和控制台，再验证后端租户权限边界。"""
        self._assert_ports_available()
        self.graph = prepare_database(self.work, self.secrets.runtime)
        environment = RUNTIME.service_environment(self.work, self.graph, self.secrets.runtime)
        environment.update(
            {
                "METAHUB_APPROVAL_SCHEDULER_ENABLED": "false",
                "METAHUB_APPROVAL_OUTBOX_ENABLED": "false",
                "METAHUB_APPROVAL_WRITEBACK_ENABLED": "false",
                "METAHUB_MCP_ENABLED": "false",
                "METAHUB_PRODUCT_EVENT_OUTBOX_ENABLED": "false",
                "METAHUB_SESSION_TTL_SECONDS": "3600",
            }
        )
        control = self.processes.add(
            RUNTIME._BASE.start_process(
                "control",
                [
                    str(RUNTIME._BASE.PYTHON),
                    "-m",
                    "uvicorn",
                    "metahub_control.app:app",
                    "--host",
                    LOOPBACK,
                    "--port",
                    str(RUNTIME.CONTROL_PORT),
                    "--no-access-log",
                    "--log-level",
                    "error",
                    "--lifespan",
                    "off",
                ],
                work=self.work,
                environment=environment,
            )
        )
        RUNTIME._BASE.wait_health(
            f"http://{LOOPBACK}:{RUNTIME.CONTROL_PORT}/health", control, stage="health.control"
        )
        console = self.processes.add(
            RUNTIME._BASE.start_console(work=self.work, environment=environment)
        )
        RUNTIME._BASE.wait_health(
            f"http://{LOOPBACK}:{RUNTIME.CONSOLE_PORT}/", console, stage="health.console"
        )
        self._verify_backend_boundaries()

    @staticmethod
    def _permission_error(response: httpx.Response) -> bool:
        """识别后端返回的统一权限拒绝结构，并要求携带请求标识及禁缓存头。"""
        try:
            payload = response.json()
        # 无法解析的权限响应不符合统一错误契约，不能视为预期拒绝。
        except ValueError:
            return False
        return (
            response.status_code == 403
            and payload.get("code") == "auth.permission_denied"
            and isinstance(payload.get("request_id"), str)
            and response.headers.get("cache-control", "").startswith("no-store")
        )

    def _login(self, client: httpx.Client, tenant: str, email: str, password: str) -> str:
        """以指定租户身份登录并只在内存返回经过长度验证的会话令牌。"""
        response = client.post(
            "/auth/login", json={"tenant_slug": tenant, "email": email, "password": password}
        )
        require(response.status_code == 200, "http.login", "status")
        token = str(response.json().get("access_token", ""))
        require(len(token) >= 32, "http.login", "token")
        # 会话令牌仅交给边界校验调用方，并在随后纳入私密值扫描集合。
        return token

    def _verify_backend_boundaries(self) -> None:
        """直接验证未授权角色被拒绝、次租户隔离且会话均完成注销。"""
        private: list[str] = []
        with httpx.Client(
            base_url=f"http://{LOOPBACK}:{RUNTIME.CONTROL_PORT}",
            timeout=httpx.Timeout(20, connect=3),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            developer = self._login(
                client, "default", DEVELOPER_EMAIL, self.secrets.runtime.role_password
            )
            developer_headers = {"Authorization": f"Bearer {developer}"}
            denied = client.get("/product-analytics/funnel", headers=developer_headers)
            require(self._permission_error(denied), "http.developer", "permission")
            require(
                client.post("/auth/logout", headers=developer_headers).status_code == 200,
                "http.developer",
                "logout",
            )

            secondary = self._login(
                client, SECONDARY_TENANT_SLUG, SECONDARY_EMAIL, self.secrets.runtime.admin_password
            )
            secondary_headers = {"Authorization": f"Bearer {secondary}"}
            isolated = client.get("/product-analytics/funnel", headers=secondary_headers)
            try:
                payload = isolated.json()
            # 次租户响应不可解析时使用空对象，使隔离契约明确失败关闭。
            except ValueError:
                payload = {}
            require(
                isolated.status_code == 200
                and isolated.headers.get("cache-control", "").startswith("no-store")
                and payload.get("tenant_id") == SECONDARY_TENANT_ID
                and len(payload.get("stages", [])) == 7
                and payload.get("summary", {}).get("completed_stage_count") == 1,
                "http.secondary",
                "isolation",
            )
            require(
                client.post("/auth/logout", headers=secondary_headers).status_code == 200,
                "http.secondary",
                "logout",
            )
            private.extend((developer, secondary))
        self.private_extra = tuple(private)
        self.private_extra_labels = ("backend_session", "backend_session")
        self.backend_permission_denied = True
        self.backend_secondary_isolated = True

    def revoke_product_analytics_read(self) -> None:
        """事务性撤销主租户运维角色的分析读取权限并更新权限矩阵版本。"""
        assert self.graph is not None
        with self.lock:
            require(not self.permissions_revoked, "browser.revoke", "already_applied")
            connection = connect_database(self.graph.database_url)
            try:
                connection.execute("BEGIN IMMEDIATE")
                role = connection.execute(
                    "SELECT permission_revision FROM roles WHERE tenant_id=? AND role_id=? "
                    "AND code='ops_admin'",
                    (self.graph.tenant_id, self.graph.authorized_role_id),
                ).fetchone()
                require(role is not None, "browser.revoke", "role")
                removed = connection.execute(
                    """
                    DELETE FROM role_permissions WHERE tenant_id=? AND role_id=?
                      AND permission_id=(SELECT permission_id FROM permissions
                        WHERE tenant_id=? AND code='product_analytics.read')
                    """,
                    (
                        self.graph.tenant_id,
                        self.graph.authorized_role_id,
                        self.graph.tenant_id,
                    ),
                ).rowcount
                require(removed == 1, "browser.revoke", "grant")
                retained = tuple(
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT permission.code FROM role_permissions relation
                        JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                          AND permission.permission_id=relation.permission_id
                        WHERE relation.tenant_id=? AND relation.role_id=? ORDER BY permission.code
                        """,
                        (self.graph.tenant_id, self.graph.authorized_role_id),
                    ).fetchall()
                )
                updated = connection.execute(
                    """
                    UPDATE roles SET permission_revision=permission_revision+1,matrix_sha256=?,
                      updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                    WHERE tenant_id=? AND role_id=? AND permission_revision=?
                    """,
                    (
                        permission_matrix_sha256(retained),
                        self.graph.tenant_id,
                        self.graph.authorized_role_id,
                        role["permission_revision"],
                    ),
                ).rowcount
                require(updated == 1, "browser.revoke", "revision")
                connection.commit()
            except AcceptanceFailure:
                connection.rollback()
                raise
            except Exception:
                connection.rollback()
                raise AcceptanceFailure("browser.revoke", "database") from None
            finally:
                connection.close()
            self.permissions_revoked = True
            self.permission_revocation_count += 1

    def restore_product_analytics_read(self) -> None:
        """事务性恢复主租户运维角色的分析读取权限，供后续 401 场景重新登录。"""
        assert self.graph is not None
        with self.lock:
            require(self.permissions_revoked, "browser.restore", "not_revoked")
            connection = connect_database(self.graph.database_url)
            try:
                connection.execute("BEGIN IMMEDIATE")
                role = connection.execute(
                    "SELECT permission_revision FROM roles WHERE tenant_id=? AND role_id=? "
                    "AND code='ops_admin'",
                    (self.graph.tenant_id, self.graph.authorized_role_id),
                ).fetchone()
                require(role is not None, "browser.restore", "role")
                inserted = connection.execute(
                    """
                    INSERT INTO role_permissions(
                      role_permission_id,tenant_id,role_id,permission_id,created_at,updated_at
                    )
                    SELECT 'rperm_'||lower(hex(randomblob(16))),?,?,permission_id,
                      strftime('%Y-%m-%dT%H:%M:%SZ','now'),strftime('%Y-%m-%dT%H:%M:%SZ','now')
                    FROM permissions WHERE tenant_id=? AND code='product_analytics.read'
                    """,
                    (
                        self.graph.tenant_id,
                        self.graph.authorized_role_id,
                        self.graph.tenant_id,
                    ),
                ).rowcount
                require(inserted == 1, "browser.restore", "grant")
                retained = tuple(
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT permission.code FROM role_permissions relation
                        JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                          AND permission.permission_id=relation.permission_id
                        WHERE relation.tenant_id=? AND relation.role_id=? ORDER BY permission.code
                        """,
                        (self.graph.tenant_id, self.graph.authorized_role_id),
                    ).fetchall()
                )
                updated = connection.execute(
                    """
                    UPDATE roles SET permission_revision=permission_revision+1,matrix_sha256=?,
                      updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                    WHERE tenant_id=? AND role_id=? AND permission_revision=?
                    """,
                    (
                        permission_matrix_sha256(retained),
                        self.graph.tenant_id,
                        self.graph.authorized_role_id,
                        role["permission_revision"],
                    ),
                ).rowcount
                require(updated == 1, "browser.restore", "revision")
                connection.commit()
            except AcceptanceFailure:
                connection.rollback()
                raise
            except Exception:
                connection.rollback()
                raise AcceptanceFailure("browser.restore", "database") from None
            finally:
                connection.close()
            self.permissions_revoked = False
            self.permission_restore_count += 1

    def invalidate_authorized_sessions(self) -> None:
        """只撤销当前主租户运维用户的一条活动 Session，以产生真实统一 401。"""
        assert self.graph is not None
        with self.lock:
            connection = connect_database(self.graph.database_url)
            try:
                connection.execute("BEGIN IMMEDIATE")
                updated = connection.execute(
                    """
                    UPDATE sessions SET status='revoked',
                      revoked_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'),
                      updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                    WHERE tenant_id=? AND user_id=? AND status='active'
                    """,
                    (self.graph.tenant_id, OPS_USER_ID),
                ).rowcount
                require(updated == 1, "browser.invalidate", "session")
                connection.commit()
            except AcceptanceFailure:
                connection.rollback()
                raise
            except Exception:
                connection.rollback()
                raise AcceptanceFailure("browser.invalidate", "database") from None
            finally:
                connection.close()
            self.session_invalidation_count += 1

    def register_private(self, payload: object) -> None:
        """验证并一次性登记驱动所有者和浏览器会话，供后续隐私扫描使用。"""
        require(type(payload) is dict and set(payload) == {"values"}, "privacy.register", "shape")
        values = payload["values"]
        require(
            type(values) is list
            and 4 <= len(values) <= 32
            and all(
                type(item) is str
                and 8 <= len(item) <= 4096
                and all(33 <= ord(character) <= 126 for character in item)
                for item in values
            )
            and len(set(values)) == len(values)
            and not set(values).intersection(self.private_extra),
            "privacy.register",
            "values",
        )
        with self.lock:
            require(not self.private_registered, "privacy.register", "duplicate")
            self.private_extra = (*self.private_extra, *tuple(values))
            labels = ("driver_owner", *("browser_session" for _item in values[1:]))
            require(len(labels) == len(values), "privacy.register", "labels")
            self.private_extra_labels = (*self.private_extra_labels, *labels)
            self.private_registered = True

    def serve(self) -> None:
        """在固定回环端口提供带停止令牌鉴权的最小浏览器协调接口。"""

        harness = self

        class Handler(BaseHTTPRequestHandler):
            """处理健康检查和受保护的浏览器协调命令，且不记录访问日志。"""

            def do_GET(self) -> None:  # noqa: N802
                """只公开不含私密数据的就绪状态，其余读取请求统一返回未找到。"""
                if self.path == "/health":
                    self._reply(200, {"status": "ready", "all_108_prds_complete": False})
                else:
                    self._reply(404, {"status": "not_found"})

            def do_POST(self) -> None:  # noqa: N802
                """校验协调令牌与有限路由，再执行登记、撤权、完成或中止命令。"""
                if self.path not in {
                    "/abort",
                    "/complete",
                    "/invalidate-authorized-sessions",
                    "/register-private",
                    "/revoke-product-analytics-read",
                    "/restore-product-analytics-read",
                }:
                    self._reply(404, {"status": "not_found"})
                    return
                if not secrets.compare_digest(
                    self.headers.get("X-Harness-Token", ""), harness.secrets.stop_token
                ):
                    self._reply(403, {"status": "denied"})
                    return
                if self.path == "/abort":
                    self._reply(200, {"status": "aborting"})
                    assert harness.server is not None
                    harness.server.shutdown()
                    return
                try:
                    payload = self._read_json(
                        BASE.BASE.MAX_COMPLETE_BYTES
                        if self.path == "/complete"
                        else BASE.BASE.MAX_PRIVATE_BYTES
                    )
                    if self.path == "/register-private":
                        harness.register_private(payload)
                    elif self.path == "/revoke-product-analytics-read":
                        require(payload == {}, "browser.revoke", "body")
                        harness.revoke_product_analytics_read()
                    elif self.path == "/restore-product-analytics-read":
                        require(payload == {}, "browser.restore", "body")
                        harness.restore_product_analytics_read()
                    # 会话失效命令只接受空对象，避免协调输入携带或选择任意 Session 标识。
                    elif self.path == "/invalidate-authorized-sessions":
                        require(payload == {}, "browser.invalidate", "body")
                        harness.invalidate_authorized_sessions()
                    else:
                        require(harness.private_registered, "browser.complete", "private_values")
                        harness.browser_facts = validate_browser_facts(payload)
                    self._reply(200, {"status": "accepted"})
                    if self.path == "/complete":
                        assert harness.server is not None
                        harness.server.shutdown()
                # 输入、权限或证据错误统一收敛为无细节响应，避免泄露内部值。
                except (AcceptanceFailure, UnicodeError, ValueError, json.JSONDecodeError):
                    self._reply(409, {"status": "invalid"})

            def _read_json(self, maximum: int) -> object:
                """严格校验唯一内容长度及上限后，以 UTF-8 解码一份 JSON 请求。"""
                lengths = self.headers.get_all("Content-Length", failobj=[])
                if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                    # 模糊或非十进制长度可能绕过边界检查，必须在读取正文前拒绝。
                    raise ValueError("length")
                length = int(lengths[0])
                if not 1 <= length <= maximum:
                    # 空正文和超限正文都不得进入解析器，以限制协调接口资源占用。
                    raise ValueError("length")
                return json.loads(self.rfile.read(length).decode("utf-8", errors="strict"))

            def _reply(self, status: int, payload: dict[str, object]) -> None:
                """发送禁缓存的 ASCII JSON 响应，避免协调状态被浏览器或代理保存。"""
                body = json.dumps(payload, separators=(",", ":")).encode("ascii")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_arguments: object) -> None:
                """关闭默认访问日志，防止协调请求元数据或令牌意外进入日志。"""
                return None

        self.server = ThreadingHTTPServer((LOOPBACK, HARNESS_PORT), Handler)
        self.server.daemon_threads = True
        try:
            self.server.serve_forever(poll_interval=0.1)
        finally:
            self.server.server_close()
            self.server = None

    def _database_summary(self) -> dict[str, object]:
        """汇总最终迁移、租户事件和权限结果，并再次验证数据库完整性。"""
        assert self.graph is not None
        connection = connect_database(self.graph.database_url)
        try:
            state = _migration_state(self.graph.database_url)
            primary_events, primary_stages = _event_counts(connection, self.graph.tenant_id)
            secondary_events, secondary_stages = _event_counts(connection, SECONDARY_TENANT_ID)
            revoked_grants = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM role_permissions relation
                    JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                      AND permission.permission_id=relation.permission_id
                    WHERE relation.tenant_id=? AND relation.role_id=?
                      AND permission.code='product_analytics.read'
                    """,
                    (self.graph.tenant_id, self.graph.authorized_role_id),
                ).fetchone()[0]
            )
            unauthorized_grants = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM role_permissions relation
                    JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                      AND permission.permission_id=relation.permission_id
                    WHERE relation.tenant_id=? AND relation.role_id=?
                      AND permission.code='product_analytics.read'
                    """,
                    (self.graph.tenant_id, self.graph.unauthorized_role_id),
                ).fetchone()[0]
            )
            secondary_grants = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM role_permissions relation
                    JOIN roles role ON role.tenant_id=relation.tenant_id
                      AND role.role_id=relation.role_id
                    JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                      AND permission.permission_id=relation.permission_id
                    WHERE role.tenant_id=? AND role.code='admin'
                      AND permission.code='product_analytics.read'
                    """,
                    (SECONDARY_TENANT_ID,),
                ).fetchone()[0]
            )
            catalog_revisions = {
                str(row["tenant_id"]): int(row["catalog_revision"])
                for row in connection.execute(
                    """
                    SELECT tenant_id,catalog_revision FROM permissions
                    WHERE tenant_id IN(?,?) AND code='product_analytics.read'
                    """,
                    (self.graph.tenant_id, SECONDARY_TENANT_ID),
                ).fetchall()
            }
            require(state == _expected_state(78, "0078"), "database.final", "migration")
            require(
                primary_events == primary_stages == 2
                and secondary_events == secondary_stages == 1,
                "database.final",
                "events",
            )
            require(
                revoked_grants == unauthorized_grants == 0
                and secondary_grants == 1
                and catalog_revisions
                == {self.graph.tenant_id: 20, SECONDARY_TENANT_ID: 20},
                "database.final",
                "rbac",
            )
            require(
                connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                and not connection.execute("PRAGMA foreign_key_check").fetchall(),
                "database.final",
                "integrity",
            )
        finally:
            connection.close()
        return {
            "migration": state,
            "primary_event_count": primary_events,
            "primary_stage_count": primary_stages,
            "secondary_event_count": secondary_events,
            "secondary_stage_count": secondary_stages,
            "revoked_read_grants": revoked_grants,
            "unauthorized_read_grants": unauthorized_grants,
            "secondary_read_grants": secondary_grants,
            "permission_catalog_revision": 20,
        }

    def publish(self) -> Path:
        """停止服务、执行隐私与来源门禁，再原子发布白名单内正式证据。"""
        require(
            self.graph is not None
            and self.browser_facts is not None
            and self.backend_permission_denied
            and self.backend_secondary_isolated
            and self.permissions_revoked
            and self.permission_revocation_count == 3
            and self.permission_restore_count == 2
            and self.session_invalidation_count == 2
            and self.private_registered,
            "browser",
            "incomplete",
        )
        _assert_source_snapshot(self.source_snapshot)
        self.processes.close()
        RUNTIME.quiesce_database(self.graph)
        for name, size in zip(
            SCREENSHOTS, ((1280, 720), (390, 844), (1280, 720), (390, 844)), strict=True
        ):
            BASE.BASE._validate_png(self.staging / name, *size)
        private_values = self._private_values_for_scope("staging")
        self._diagnose_private_storage(self.work, "work")
        self._diagnose_private_storage(self.staging, "staging")
        work_privacy = RUNTIME._BASE.verify_private_storage(self.work, private_values)
        staging_privacy = RUNTIME._BASE.verify_private_storage(self.staging, private_values)
        require(not self.database_sidecars(), "cleanup.sqlite", "sidecar")
        payload = {
            "prd_id": "PRD-085",
            "status": "passed",
            "run_id": self.run_id,
            "browser": self.browser_facts,
            "database": self._database_summary(),
            "lifecycle_operations": {
                "permission_revocations": self.permission_revocation_count,
                "permission_restores": self.permission_restore_count,
                "session_invalidations": self.session_invalidation_count,
            },
            "privacy": {"work": work_privacy, "staging": staging_privacy},
            "processes": {
                "control": "owned_and_stopped",
                "console": "owned_and_stopped",
                "browser": "owned_and_stopped",
                "sqlite_sidecars": 0,
            },
            "source_provenance": {"algorithm": "sha256", **self.source_snapshot},
            "all_108_prds_complete": False,
        }
        RUNTIME.assert_safe_evidence(payload, private_values)
        evidence = self.staging / "browser-acceptance.json"
        evidence.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        entries = []
        for path in sorted(
            (item for item in self.staging.iterdir() if item.name != OWNER_FILENAME),
            key=lambda item: item.name,
        ):
            require(
                path.name in ALLOWED_ARTIFACTS - {"sha256-manifest.json"}
                and path.is_file()
                and not path.is_symlink(),
                "evidence.manifest",
                "entry",
            )
            entries.append(
                {
                    "name": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        manifest = {"algorithm": "sha256", "files": entries, "all_108_prds_complete": False}
        (self.staging / "sha256-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        require(
            {item.name for item in self.staging.iterdir()} == ALLOWED_ARTIFACTS | {OWNER_FILENAME},
            "evidence.whitelist",
            "entries",
        )
        RUNTIME._BASE.verify_private_storage(self.staging, private_values)
        _assert_source_snapshot(self.source_snapshot)
        os.replace(self.staging, self.final)
        RUNTIME._BASE.verify_private_storage(self.final, private_values)
        _remove_owned_directory(self.work, WORK_ROOT, self.owner, stage="cleanup.work")
        (self.final / OWNER_FILENAME).unlink()
        return self.final / "browser-acceptance.json"

    def _private_values_for_scope(self, scope: str) -> tuple[str, ...]:
        """校验扫描范围并返回该范围必须检测的全部运行私密值。"""

        # 返回值只用于字节级泄露检测，严禁序列化到正式证据或清单。
        require(scope in {"work", "staging"}, "privacy.scope", "invalid")
        return self.secrets.values(self.private_extra)

    def _diagnose_private_storage(self, root: Path, scope: str) -> None:
        """按文件类别定位私密值泄露，仅在失败码中报告范围与值类别。"""
        require(scope in {"work", "staging"}, "privacy.diagnostic", "scope")
        labeled = (
            *(("runtime", value) for value in self.secrets.runtime.values()),
            ("stop", self.secrets.stop_token),
            *zip(self.private_extra_labels, self.private_extra, strict=True),
        )
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            content = path.read_bytes()
            suffix = path.suffix.casefold()
            category = (
                "database"
                if suffix in {".db", ".sqlite", ".sqlite3"}
                else "log"
                if suffix == ".log"
                else "png"
                if suffix == ".png"
                else "json"
                if suffix == ".json"
                else "other"
            )
            for label, value in labeled:
                if any(encoded in content for encoded in RUNTIME._BASE.sensitive_encodings(value)):
                    raise AcceptanceFailure(
                        "privacy.diagnostic", f"{scope}.{category}.{label}"
                    )

    def database_sidecars(self) -> tuple[Path, ...]:
        """列出数据库仍存在的 WAL、共享内存或回滚日志伴随文件。"""
        assert self.graph is not None
        return tuple(
            path
            for path in (
                Path(f"{self.graph.database}-wal"),
                Path(f"{self.graph.database}-shm"),
                Path(f"{self.graph.database}-journal"),
            )
            if path.exists()
        )

    def abort(self) -> None:
        """失败时停止自有进程、静置数据库并清理具备正确归属的运行目录。"""
        try:
            self.processes.close()
        except Exception:
            pass
        if self.graph is not None:
            try:
                RUNTIME.quiesce_database(self.graph)
            except Exception:
                pass
        # 每个归属目录独立清理；一个 Windows 锁不能阻止其余目录和 HTTP socket 关闭。
        for path, root, stage in (
            (self.final, ARTIFACT_ROOT, "cleanup.final"),
            (self.staging, ARTIFACT_ROOT, "cleanup.staging"),
            (self.work, WORK_ROOT, "cleanup.work"),
        ):
            try:
                if path.exists() and (path / OWNER_FILENAME).is_file():
                    _remove_owned_directory(path, root, self.owner, stage=stage)
            except Exception:
                pass
        if self.server is not None:
            try:
                self.server.server_close()
            except Exception:
                pass
            finally:
                self.server = None


def main(argv: list[str] | None = None) -> int:
    """解析运行标识，启动浏览器协调服务并在成功后发布正式证据。"""
    parser = SafeArgumentParser(description="Serve PRD-085 PAGE-117 browser acceptance")
    parser.add_argument("--run-id", required=True)
    harness: BrowserHarness | None = None
    try:
        arguments = parser.parse_args(argv)
        harness = BrowserHarness(arguments.run_id, BrowserSecrets.from_environment())
        harness.prepare()
        print(
            json.dumps(
                {
                    "status": "ready",
                    "url": f"http://{LOOPBACK}:{RUNTIME.CONSOLE_PORT}/#growth-analytics",
                    "all_108_prds_complete": False,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        harness.serve()
        target = harness.publish()
        print(target.relative_to(ROOT).as_posix(), flush=True)
        return 0
    except AcceptanceFailure as exc:
        if harness is not None:
            harness.abort()
        print(
            f"PRD-085 browser harness failed: {exc.stage}:{exc.code}", file=sys.stderr, flush=True
        )
        return 1
    except Exception as exc:
        if harness is not None:
            harness.abort()
        traceback = exc.__traceback__
        while traceback is not None and traceback.tb_next is not None:
            traceback = traceback.tb_next
        line = traceback.tb_lineno if traceback is not None else 0
        code = f"{type(exc).__name__.lower()}_l{line}"
        print(
            f"PRD-085 browser harness failed: harness.unexpected:{code}",
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
