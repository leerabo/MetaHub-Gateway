"""为 PRD-078 PAGE-108/109 实际 Browser 验收提供隔离运行环境。

Harness 只绑定回环地址，复用 PRD-078 Runtime 已验证的迁移、seed、RESP、Control、
Console 和精确进程 owner。密码、内部 Token、幂等键和各安全域 Secret 只在当前进程
内存与子进程环境中存在；最终只发布截图、布尔布局事实、聚合数据库计数和源码摘要。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import secrets
import shutil
import sys
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, UnidentifiedImageError

# Browser harness 的目录、回环端口、环境变量名和源码清单共同限定仓库内可复现验收边界。
ROOT = Path(__file__).resolve().parents[2]
WORK_ROOT = ROOT / ".tmp" / "prd078-browser"
ARTIFACT_ROOT = ROOT / ".artifacts" / "prd078-browser"
LOOPBACK = "127.0.0.1"
GATEWAY_PORT = 8000
HARNESS_PORT = 18881
OWNER_FILENAME = ".prd078-browser-owner"
ROLE_PASSWORD_ENV = "PRD078_BROWSER_ROLE_PASSWORD"
STOP_TOKEN_ENV = "PRD078_BROWSER_STOP_TOKEN"
MAX_COMPLETE_BYTES = 16 * 1024
SCREENSHOTS = (
    "finance-standard-modal-desktop-1280x720.png",
    "security-high-risk-modal-mobile-390x844.png",
    "security-high-risk-busy-mobile-390x844.png",
)
SOURCE_FILES = {
    "browser_harness": Path(__file__).resolve(),
    "browser_acceptance": ROOT / "scripts" / "acceptance" / "prd078-browser-acceptance.mjs",
    "runtime_harness": ROOT / "scripts" / "acceptance" / "prd078-runtime-acceptance.py",
    "console_main": ROOT / "console-web" / "src" / "app" / "main.js",
    "approval_api": ROOT / "console-web" / "src" / "lib" / "approvals.js",
    "approval_state": ROOT
    / "console-web"
    / "src"
    / "features"
    / "approvals"
    / "approvalPageState.js",
    "approval_binding": ROOT
    / "console-web"
    / "src"
    / "features"
    / "approvals"
    / "bindApprovals.js",
    "approval_render": ROOT
    / "console-web"
    / "src"
    / "features"
    / "approvals"
    / "renderApprovals.js",
    "approval_styles": ROOT / "console-web" / "src" / "styles" / "main.css",
}


def _load_runtime_module() -> Any:
    """加载 PRD-078 Runtime helper；导入不得启动端口或写文件。"""

    name = "_metahub_prd078_runtime_helpers_for_browser"
    current = sys.modules.get(name)
    if current is not None:
        return current
    path = ROOT / "scripts" / "acceptance" / "prd078-runtime-acceptance.py"
    specification = importlib.util.spec_from_file_location(name, path)
    if specification is None or specification.loader is None:
        # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
        raise RuntimeError("browser harness dependency unavailable")
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


# 浏览器验收复用 Runtime 的迁移、服务图和脱敏能力，导入时不得启动服务或占用端口。
RUNTIME = _load_runtime_module()
AcceptanceFailure = RUNTIME.AcceptanceFailure
SafeArgumentParser = RUNTIME.SafeArgumentParser
require = RUNTIME.require
from metahub_shared.rbac import permission_matrix_sha256  # noqa: E402


@dataclass(frozen=True, slots=True)
class BrowserSecrets:
    """保存 Browser 唯一外部凭据；repr 永不展开私有值。"""

    runtime: RUNTIME.RuntimeSecrets = field(repr=False)
    stop_token: str = field(repr=False)

    @classmethod
    def from_environment(cls) -> BrowserSecrets:
        """读取一次角色密码和停止 Token，其余安全域由进程内随机生成。"""

        role_password = os.environ.get(ROLE_PASSWORD_ENV, "")
        stop_token = os.environ.get(STOP_TOKEN_ENV, "")
        require(len(role_password) >= 32, "config.role_password", "invalid")
        require(len(stop_token) >= 43, "config.stop_token", "invalid")
        generated = RUNTIME.RuntimeSecrets.create()
        runtime = RUNTIME.RuntimeSecrets(
            admin_password=generated.admin_password,
            role_password=role_password,
            auth_secret=generated.auth_secret,
            provider_secret=generated.provider_secret,
            prompt_secret=generated.prompt_secret,
            file_secret=generated.file_secret,
            batch_secret=generated.batch_secret,
            cache_secret=generated.cache_secret,
            approval_secret=generated.approval_secret,
            session_risk_secret=generated.session_risk_secret,
            internal_token=generated.internal_token,
            provider_key=generated.provider_key,
            virtual_key=generated.virtual_key,
        )
        # 令牌只允许在短生命周期内做摘要和所有权校验；缺失、额外出现或不匹配均固定失败。
        return cls(runtime=runtime, stop_token=stop_token)

    def private_values(self, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
        """返回 work、staging 与 final 必须零明文命中的全部私有值。"""

        # 故障注入和拒绝分支必须产生确定的失败证据，且不得把虚构凭据或运行载荷发布到制品。
        return self.runtime.values((self.stop_token, *extra))


def _direct_child(root: Path, name: str, *, stage: str) -> Path:
    """只接受仓库内指定根的单个直属路径段。"""

    require(root.resolve().is_relative_to(ROOT.resolve()), stage, "root_outside_repository")
    require(Path(name).name == name and name not in {".", ".."}, stage, "invalid_name")
    target = root / name
    require(target.parent.resolve() == root.resolve(), stage, "outside_root")
    return target


def create_run_directories(run_id: str, owner: str) -> tuple[Path, Path, Path]:
    """排他创建 work/staging；已存在批次或 final 一律禁止覆盖。"""

    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    work = _direct_child(WORK_ROOT, run_id, stage="config.work")
    staging = _direct_child(ARTIFACT_ROOT, f".{run_id}.staging", stage="config.staging")
    final = _direct_child(ARTIFACT_ROOT, run_id, stage="config.artifact")
    require(not work.exists(), "config.work", "exists")
    require(not staging.exists(), "config.staging", "exists")
    require(not final.exists(), "config.artifact", "exists")
    work.mkdir()
    staging.mkdir()
    for directory in (work, staging):
        (directory / OWNER_FILENAME).write_text(f"{owner}\n", encoding="ascii")
    for name in ("logs", "temp", "spool"):
        (work / name).mkdir()
    return work, staging, final


def _remove_owned_directory(path: Path, root: Path, owner: str, *, stage: str) -> None:
    """只删除直属、非链接且 claim 匹配的本轮目录。"""

    require(path.parent.resolve() == root.resolve(), stage, "boundary")
    require(not path.is_symlink(), stage, "symlink")
    claim = path / OWNER_FILENAME
    require(claim.is_file(), stage, "claim_missing")
    require(claim.read_text(encoding="ascii").strip() == owner, stage, "claim")
    shutil.rmtree(path)
    require(not path.exists(), stage, "remaining")


def _source_provenance() -> dict[str, str]:
    """绑定本次实际执行的 harness、Runtime 与审批页面实现。"""

    result: dict[str, str] = {}
    for name, path in SOURCE_FILES.items():
        require(
            path.is_file() and path.resolve().is_relative_to(ROOT.resolve()),
            "evidence.source",
            "missing",
        )
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _validate_browser_facts(value: object) -> dict[str, object]:
    """只接纳真实浏览器生成的固定交互、生命周期、布局和网络事实。"""

    require(type(value) is dict, "browser.complete", "shape")
    expected = {"status", "desktop", "mobile", "lifecycle", "network", "screenshots"}
    require(set(value) == expected and value.get("status") == "passed", "browser.complete", "shape")
    desktop = value.get("desktop")
    mobile = value.get("mobile")
    lifecycle = value.get("lifecycle")
    network = value.get("network")
    require(
        type(desktop) is dict
        and set(desktop)
        == {
            "width",
            "height",
            "nonblank",
            "horizontal_overflow",
            "text_overflow",
            "overlap_count",
            "finance_budget_only",
            "native_modal",
            "background_blocked",
            "standard_initial_focus",
            "tab_forward_wrap",
            "tab_reverse_wrap",
            "idle_escape_closed",
            "focus_restored",
            "decision_completed",
        }
        and desktop.get("width") == 1280
        and desktop.get("height") == 720
        and desktop.get("nonblank") is True
        and desktop.get("horizontal_overflow") is False
        and desktop.get("text_overflow") is False
        and desktop.get("overlap_count") == 0
        and desktop.get("finance_budget_only") is True
        and desktop.get("native_modal") is True
        and desktop.get("background_blocked") is True
        and desktop.get("standard_initial_focus") is True
        and desktop.get("tab_forward_wrap") is True
        and desktop.get("tab_reverse_wrap") is True
        and desktop.get("idle_escape_closed") is True
        and desktop.get("focus_restored") is True
        and desktop.get("decision_completed") is True,
        "browser.complete",
        "desktop",
    )
    require(
        type(mobile) is dict
        and set(mobile)
        == {
            "width",
            "height",
            "nonblank",
            "horizontal_overflow",
            "text_overflow",
            "overlap_count",
            "security_risk_only",
            "native_modal",
            "high_risk_initial_focus",
            "idle_escape_closed",
            "idle_escape_password_cleared",
            "busy_native_modal",
            "busy_escape_blocked",
            "busy_cancel_blocked",
            "step_up_completed",
        }
        and mobile.get("width") == 390
        and mobile.get("height") == 844
        and mobile.get("nonblank") is True
        and mobile.get("horizontal_overflow") is False
        and mobile.get("text_overflow") is False
        and mobile.get("overlap_count") == 0
        and mobile.get("security_risk_only") is True
        and mobile.get("native_modal") is True
        and mobile.get("high_risk_initial_focus") is True
        and mobile.get("idle_escape_closed") is True
        and mobile.get("idle_escape_password_cleared") is True
        and mobile.get("busy_native_modal") is True
        and mobile.get("busy_escape_blocked") is True
        and mobile.get("busy_cancel_blocked") is True
        and mobile.get("step_up_completed") is True,
        "browser.complete",
        "mobile",
    )
    require(
        type(lifecycle) is dict
        and set(lifecycle)
        == {
            "owner_drift_closed",
            "owner_drift_password_cleared",
            "owner_drift_focus_not_restored",
            "late_success_discarded",
            "permission_revocation_closed",
            "permission_revocation_password_cleared",
            "permission_revocation_root_cleared",
            "late_denial_discarded",
            "logout_dialog_closed",
            "logout_password_cleared",
            "logout_root_cleared",
            "logout_storage_cleared",
        }
        and all(item is True for item in lifecycle.values()),
        "browser.complete",
        "lifecycle",
    )
    require(
        type(network) is dict
        and set(network)
        == {"external_requests", "unexpected_console_errors", "runtime_exceptions"}
        and network.get("external_requests") == 0
        and network.get("unexpected_console_errors") == 0
        and network.get("runtime_exceptions") == 0,
        "browser.complete",
        "network",
    )
    require(value.get("screenshots") == list(SCREENSHOTS), "browser.complete", "screenshots")
    return value


def _validate_png(path: Path, width: int, height: int) -> None:
    """完整解码 PNG 并验证尺寸、格式和非空像素，拒绝伪造 IHDR 占位。"""

    require(path.is_file() and path.stat().st_size >= 1024, "browser.screenshot", "missing")
    try:
        with Image.open(path) as source:
            require(source.format == "PNG" and source.size == (width, height), "browser.screenshot", "dimensions")
            source.verify()
        with Image.open(path) as source:
            image = source.convert("RGB")
            colors = image.getcolors(maxcolors=width * height)
    # 解码、格式或像素读取异常统一收敛，不把本地文件内容带入验收证据。
    except (OSError, UnidentifiedImageError, ValueError):
        raise AcceptanceFailure("browser.screenshot", "decode") from None
    require(colors is not None and len(colors) >= 32, "browser.screenshot", "blank")
    dominant = max(count for count, _color in colors)
    require(dominant * 200 < width * height * 199, "browser.screenshot", "blank")


class BrowserHarness:
    """持有本轮目录、服务、任务和一次性完成信号。"""

    def __init__(self, run_id: str, secrets_: BrowserSecrets) -> None:
        """``__init__`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
        self.run_id = RUNTIME.safe_run_id(run_id)
        self.secrets = secrets_
        self.owner = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        self.work, self.staging, self.final = create_run_directories(self.run_id, self.owner)
        self.processes = RUNTIME.OwnedProcesses()
        self.graph: RUNTIME.RuntimeGraph | None = None
        self.private_extra: tuple[str, ...] = ()
        self.browser_facts: dict[str, object] | None = None
        self.server: ThreadingHTTPServer | None = None
        self.control_lock = threading.Lock()
        self.security_revoked = False

    def prepare(self) -> None:
        """创建 0071 数据库、启动真实服务并写入六个待审批任务。"""

        RUNTIME.assert_ports_available()
        for port in (GATEWAY_PORT, HARNESS_PORT):
            probe = RUNTIME.socket.socket(RUNTIME.socket.AF_INET, RUNTIME.socket.SOCK_STREAM)
            try:
                probe.bind((LOOPBACK, port))
            # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
            except OSError:
                raise AcceptanceFailure("config.port", f"unavailable_{port}") from None
            finally:
                probe.close()
        self.graph = RUNTIME.prepare_database(self.work, self.secrets.runtime)
        environment = RUNTIME.service_environment(self.work, self.graph, self.secrets.runtime)
        self.processes.add(RUNTIME.start_resp(self.work, environment))
        gateway = self.processes.add(
            RUNTIME._BASE.start_service(
                "gateway",
                "metahub_gateway.app:app",
                GATEWAY_PORT,
                work=self.work,
                environment=environment,
            )
        )
        RUNTIME._BASE.wait_health(
            f"http://{LOOPBACK}:{GATEWAY_PORT}/health",
            gateway,
            stage="health.gateway",
        )
        control = self.processes.add(
            RUNTIME._BASE.start_service(
                "control",
                "metahub_control.app:app",
                RUNTIME.CONTROL_PORT,
                work=self.work,
                environment=environment,
            )
        )
        RUNTIME._BASE.wait_health(
            f"http://{LOOPBACK}:{RUNTIME.CONTROL_PORT}/health",
            control,
            stage="health.control",
        )
        console = self.processes.add(
            RUNTIME._BASE.start_console(work=self.work, environment=environment)
        )
        RUNTIME._BASE.wait_health(
            f"http://{LOOPBACK}:{RUNTIME.CONSOLE_PORT}/",
            console,
            stage="health.console",
        )
        self.private_extra = self._create_approval_tasks()

    def _create_approval_tasks(self) -> tuple[str, ...]:
        """通过真实 internal API 创建两条预算与两条高风险审批。"""

        require(self.graph is not None, "database.seed", "missing")
        keys = tuple(
            f"prd078-browser-{name}-{secrets.token_urlsafe(18)}"
            for name in (
                "approve",
                "reject",
                "revoke",
                "risk",
                "risk-logout",
                "risk-revoke",
            )
        )
        headers = {"X-Internal-Token": self.secrets.runtime.internal_token}
        with httpx.Client(
            base_url=f"http://{LOOPBACK}:{RUNTIME.CONTROL_PORT}",
            timeout=httpx.Timeout(15, connect=3),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            for name, key in zip(("approve", "reject", "timeout"), keys[:3], strict=True):
                response = client.post(
                    f"/internal/tenants/{self.graph.tenant_id}/approvals",
                    headers=headers,
                    json=RUNTIME._create_payload(
                        self.graph,
                        name,
                        key,
                        expires_in_seconds=3600,
                    ),
                )
                payload = RUNTIME._private(
                    response,
                    "X-Internal-Token",
                    stage=f"http.create_{name}",
                )
                require(
                    response.status_code == 201 and type(payload.get("approval")) is dict,
                    f"http.create_{name}",
                    "status",
                )
            risk = client.post(
                f"/internal/tenants/{self.graph.tenant_id}/approvals",
                headers=headers,
                json=RUNTIME._create_risk_payload(self.graph, keys[3])
                | {"expires_in_seconds": 3600},
            )
            payload = RUNTIME._private(risk, "X-Internal-Token", stage="http.create_risk")
            require(
                risk.status_code == 201 and type(payload.get("approval")) is dict,
                "http.create_risk",
                "status",
            )
            logout_risk = client.post(
                f"/internal/tenants/{self.graph.tenant_id}/approvals",
                headers=headers,
                json=RUNTIME._create_risk_payload(self.graph, keys[4])
                | {
                    "target_action": "freeze_virtual_key",
                    "expires_in_seconds": 3600,
                },
            )
            payload = RUNTIME._private(
                logout_risk,
                "X-Internal-Token",
                stage="http.create_risk_logout",
            )
            require(
                logout_risk.status_code == 201 and type(payload.get("approval")) is dict,
                "http.create_risk_logout",
                "status",
            )
            revoke_risk = client.post(
                f"/internal/tenants/{self.graph.tenant_id}/approvals",
                headers=headers,
                json=RUNTIME._create_risk_payload(self.graph, keys[5])
                | {
                    "target_action": "dismiss",
                    "target_reason_code": "false_positive",
                    "expires_in_seconds": 3600,
                },
            )
            payload = RUNTIME._private(
                revoke_risk,
                "X-Internal-Token",
                stage="http.create_risk_revoke",
            )
            require(
                revoke_risk.status_code == 201 and type(payload.get("approval")) is dict,
                "http.create_risk_revoke",
                "status",
            )
        return keys

    def revoke_security_approval_permissions(self) -> None:
        """一次性撤销 SecurityAdmin 的审批基础权限并同步 revision/hash。"""

        require(self.graph is not None, "browser.revoke", "database_missing")
        with self.control_lock:
            require(not self.security_revoked, "browser.revoke", "already_applied")
            connection = RUNTIME.connect_database(self.graph.database_url)
            try:
                connection.execute("BEGIN IMMEDIATE")
                role = connection.execute(
                    """
                    SELECT role_id,permission_revision FROM roles
                    WHERE tenant_id=? AND code='security_admin' AND status='active'
                    """,
                    (self.graph.tenant_id,),
                ).fetchone()
                require(role is not None, "browser.revoke", "role_missing")
                retained = tuple(
                    str(row["code"])
                    for row in connection.execute(
                        """
                        SELECT permission.code FROM role_permissions grant_row
                        JOIN permissions permission
                          ON permission.tenant_id=grant_row.tenant_id
                         AND permission.permission_id=grant_row.permission_id
                        WHERE grant_row.tenant_id=? AND grant_row.role_id=?
                          AND permission.code NOT IN('approval.read','approval.decide')
                        ORDER BY permission.code
                        """,
                        (self.graph.tenant_id, role["role_id"]),
                    ).fetchall()
                )
                removed = connection.execute(
                    """
                    DELETE FROM role_permissions
                    WHERE tenant_id=? AND role_id=? AND permission_id IN(
                      SELECT permission_id FROM permissions
                      WHERE tenant_id=? AND code IN('approval.read','approval.decide')
                    )
                    """,
                    (self.graph.tenant_id, role["role_id"], self.graph.tenant_id),
                ).rowcount
                require(removed == 2, "browser.revoke", "grant_count")
                updated = connection.execute(
                    """
                    UPDATE roles
                    SET permission_revision=permission_revision+1,
                        matrix_sha256=?,updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                    WHERE tenant_id=? AND role_id=? AND permission_revision=?
                    """,
                    (
                        permission_matrix_sha256(retained),
                        self.graph.tenant_id,
                        role["role_id"],
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
            self.security_revoked = True

    def serve(self) -> None:
        """提供只读健康和一次性完成端点，直到 Browser 提交固定验收事实。"""

        harness = self

        class Handler(BaseHTTPRequestHandler):
            """``Handler`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""

            def do_GET(self) -> None:  # noqa: N802
                """``do_GET`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
                if self.path != "/health":
                    self._reply(404, {"status": "not_found"})
                    return
                self._reply(200, {"status": "ready", "all_108_prds_complete": False})

            def do_POST(self) -> None:  # noqa: N802
                """``do_POST`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
                if self.path not in {"/abort", "/complete", "/revoke-security"}:
                    self._reply(404, {"status": "not_found"})
                    return
                supplied = self.headers.get("X-Harness-Token", "")
                # 令牌只允许在短生命周期内做摘要和所有权校验；缺失、额外出现或不匹配均固定失败。
                if not secrets.compare_digest(supplied, harness.secrets.stop_token):
                    self._reply(403, {"status": "denied"})
                    return
                if self.path == "/abort":
                    self._reply(200, {"status": "aborting"})
                    assert harness.server is not None
                    harness.server.shutdown()
                    return
                if self.path == "/revoke-security":
                    try:
                        harness.revoke_security_approval_permissions()
                    except AcceptanceFailure:
                        self._reply(409, {"status": "invalid"})
                        return
                    self._reply(200, {"status": "revoked"})
                    return
                try:
                    values = self.headers.get_all("Content-Length", failobj=[])
                    if len(values) != 1 or not values[0].isascii() or not values[0].isdecimal():
                        # 非规范长度统一映射为固定错误。
                        raise ValueError("length")
                    length = int(values[0])
                    if not 1 <= length <= MAX_COMPLETE_BYTES:
                        # 超出完成报告预算时立即拒绝。
                        raise ValueError("length")
                    payload = json.loads(self.rfile.read(length))
                    harness.browser_facts = _validate_browser_facts(payload)
                # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
                except (json.JSONDecodeError, ValueError, AcceptanceFailure):
                    self._reply(400, {"status": "invalid"})
                    return
                self._reply(200, {"status": "accepted"})
                assert harness.server is not None
                harness.server.shutdown()

            def _reply(self, status: int, payload: dict[str, object]) -> None:
                """``_reply`` 验证 PRD-078 对应场景，并确认租户隔离、失败关闭与脱敏证据可复现。"""
                encoded = json.dumps(payload, separators=(",", ":")).encode("ascii")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _format: str, *_arguments: object) -> None:
                """禁止验收接收端输出请求信息。"""
                return None

        self.server = ThreadingHTTPServer((LOOPBACK, HARNESS_PORT), Handler)
        try:
            self.server.serve_forever(poll_interval=0.2)
        finally:
            self.server.server_close()
            self.server = None

    def publish(self) -> Path:
        """关闭服务后核对数据库、截图与隐私，再原子发布本轮 final。"""

        require(self.graph is not None and self.browser_facts is not None, "browser", "incomplete")
        self.processes.close()
        RUNTIME.quiesce_database(self.graph)
        database = self._database_summary()
        _validate_png(self.staging / SCREENSHOTS[0], 1280, 720)
        _validate_png(self.staging / SCREENSHOTS[1], 390, 844)
        _validate_png(self.staging / SCREENSHOTS[2], 390, 844)
        private_values = self.secrets.private_values(self.private_extra)
        work_privacy = RUNTIME._BASE.verify_private_storage(self.work, private_values)
        staging_privacy = RUNTIME._BASE.verify_private_storage(self.staging, private_values)
        payload = {
            "prd_id": "PRD-078",
            "status": "passed",
            "run_id": self.run_id,
            "browser": self.browser_facts,
            "database": database,
            "privacy": {"work": work_privacy, "staging": staging_privacy},
            "processes": {
                "gateway": "owned_and_stopped",
                "control": "owned_and_stopped",
                "console": "owned_and_stopped",
                "resp": "owned_and_stopped",
                "harness": "owned_and_stopped",
            },
            "source_provenance": {"algorithm": "sha256", **_source_provenance()},
            "all_108_prds_complete": False,
        }
        RUNTIME.assert_safe_evidence(payload, private_values)
        target = self.staging / "browser-acceptance.json"
        target.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        RUNTIME._BASE.verify_private_storage(self.staging, private_values)
        entries = []
        for path in sorted(
            (item for item in self.staging.iterdir() if item.name != OWNER_FILENAME),
            key=lambda item: item.name,
        ):
            require(path.is_file() and not path.is_symlink(), "evidence.manifest", "entry")
            entries.append(
                {
                    "name": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        manifest = {
            "algorithm": "sha256",
            "files": entries,
            "all_108_prds_complete": False,
        }
        (self.staging / "sha256-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        RUNTIME._BASE.verify_private_storage(self.staging, private_values)
        os.replace(self.staging, self.final)
        RUNTIME._BASE.verify_private_storage(self.final, private_values)
        _remove_owned_directory(self.work, WORK_ROOT, self.owner, stage="cleanup.work")
        return self.final / "browser-acceptance.json"

    def _database_summary(self) -> dict[str, object]:
        """只返回聚合状态，审批 ID、actor、tenant 和幂等 HMAC 均不出库。"""

        assert self.graph is not None
        connection = RUNTIME.connect_database(self.graph.database_url)
        try:
            task_counts = {
                f"{row['target_type']}:{row['status']}": int(row["count"])
                for row in connection.execute(
                    """
                    SELECT target_type,status,COUNT(*) AS count FROM approval_tasks
                    GROUP BY target_type,status ORDER BY target_type,status
                    """
                ).fetchall()
            }
            decision_count = int(
                connection.execute("SELECT COUNT(*) FROM approval_decisions").fetchone()[0]
            )
            writeback_counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status,COUNT(*) AS count FROM approval_writebacks GROUP BY status"
                ).fetchall()
            }
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        finally:
            connection.close()
        require(
            task_counts
            == {
                "budget_block_release:approved": 1,
                "budget_block_release:pending": 1,
                "budget_block_release:rejected": 1,
                "session_risk_action:approved": 1,
                "session_risk_action:pending": 2,
            },
            "database.tasks",
            "counts",
        )
        require(decision_count == 3, "database.decisions", "count")
        # 预算批准、预算拒绝和高风险批准各形成一条 writeback；高风险 Redis
        # 投影由本轮真实 worker 在停服前收敛，因此最终三条均必须 completed。
        require(writeback_counts == {"completed": 3}, "database.writebacks", "counts")
        require(integrity == "ok", "database", "integrity")
        require(foreign_keys == 0, "database", "foreign_keys")
        return {
            "task_status_counts": task_counts,
            "decision_count": decision_count,
            "writeback_status_counts": writeback_counts,
            "integrity": integrity,
            "foreign_key_violations": foreign_keys,
        }

    def abort(self) -> None:
        """失败路径只停止本轮进程；诊断目录保持在 Git 忽略边界内。"""

        try:
            self.processes.close()
        finally:
            if self.server is not None:
                self.server.server_close()
                self.server = None


def main(argv: list[str] | None = None) -> int:
    """启动 harness；stdout 只输出 ready/final 相对路径或固定失败坐标。"""

    parser = SafeArgumentParser(description="Serve PRD-078 browser acceptance harness")
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
                    "url": f"http://{LOOPBACK}:{RUNTIME.CONSOLE_PORT}/#approvals",
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
            f"PRD-078 browser harness failed: {exc.stage}:{exc.code}",
            file=sys.stderr,
            flush=True,
        )
        return 1
    except Exception:
        if harness is not None:
            harness.abort()
        print("PRD-078 browser harness failed: harness:unexpected", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
