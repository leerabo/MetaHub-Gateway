"""PRD-060 三服务邀请主流程与权限异常运行验收器。

脚本只访问本机 loopback 服务，管理员密码和受邀用户密码必须由环境变量传入。Session 与邀请
Token 仅在当前进程内存中短暂存在，输出只保留状态码、统一错误码、request_id 和角色结果。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


@dataclass(frozen=True)
class HttpResult:
    """保存一次本地 HTTP 调用的安全结果，不记录请求 URL、Bearer 或原始正文。"""

    status: int
    """HTTP 状态码。"""

    payload: dict[str, Any]
    """已解析 JSON；调用方必须先移除响应中的一次性 Secret 才能用于汇总。"""

    request_id: str
    """统一响应 Header 中的 request_id。"""


def _request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    bearer: str | None = None,
    request_id: str,
) -> HttpResult:
    """向白名单 loopback 服务发送 JSON，并把业务错误也规范化为安全结果。

    参数：base_url/path 由脚本固定；payload 为严格 JSON；bearer 只用于管理和登录后接口。
    返回：状态、JSON 和响应 request_id。
    约束：网络异常使用固定文本，不能把可能含 Token 的 path 拼入异常或输出。
    """

    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "X-Request-ID": request_id,
        "X-Trace-ID": f"trc_{request_id}",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    request = Request(f"{base_url}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - URL 已由 loopback 白名单校验。
            raw = response.read()
            return HttpResult(
                status=response.status,
                payload=_decode_json(raw),
                request_id=response.headers.get("X-Request-ID", ""),
            )
    # HTTPError 的字符串可能带 path credential，因此只读取响应，不传播原始异常文本。
    except HTTPError as error:
        return HttpResult(
            status=error.code,
            payload=_decode_json(error.read()),
            request_id=error.headers.get("X-Request-ID", ""),
        )
    # 网络异常也只返回固定分类，不能把可能含一次性 Token 的 URL 带入终端。
    except (OSError, URLError) as error:
        raise RuntimeError("本地验收服务不可用") from error


def _request_status(base_url: str, path: str, *, request_id: str) -> tuple[int, str]:
    """读取不要求 JSON 的 Console 页面，只返回状态和 request_id，不保存 HTML。"""

    request = Request(
        f"{base_url}{path}",
        headers={"X-Request-ID": request_id},
        method="GET",
    )
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - URL 已由 loopback 白名单校验。
            response.read(1)
            return response.status, response.headers.get("X-Request-ID", "")
    # Console 只验证可达性；底层异常文本可能包含环境信息，统一改写后再抛出。
    except (HTTPError, OSError, URLError) as error:
        raise RuntimeError("Console 本地验收不可用") from error


def _decode_json(raw: bytes) -> dict[str, Any]:
    """解析有界本地响应；非 object 响应视为运行合同失败。"""

    if len(raw) > 1024 * 1024:
        # 验收响应上限防止异常服务把无界正文带入当前进程。
        raise RuntimeError("本地验收响应超过上限")
    try:
        value = json.loads(raw.decode("utf-8"))
    # 原始解码异常可能回显响应片段，只向上层暴露固定合同失败文本。
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("本地验收响应不是有效 JSON") from error
    if type(value) is not dict:
        # 运行证据只接受统一 JSON object，数组或标量不能被宽松解释。
        raise RuntimeError("本地验收响应必须为 JSON object")
    return value


def _require(condition: bool, message: str) -> None:
    """执行固定断言；message 不得含邮箱、密码、Session 或邀请 Token。"""

    if not condition:
        # 调用点只能传入固定安全消息；统一出口避免临时断言泄漏凭据。
        raise RuntimeError(message)


def _loopback_base(value: str, expected_port: int) -> str:
    """限制验收目标为固定 localhost 端口，防止脚本意外向外部服务发送凭据。"""

    allowed = {f"http://localhost:{expected_port}", f"http://127.0.0.1:{expected_port}"}
    normalized = value.rstrip("/")
    if normalized not in allowed:
        # 失败关闭可以阻止管理员密码或邀请 Token 被误发往其他端口。
        raise ValueError(f"验收地址必须是本机 {expected_port} 端口")
    return normalized


def run_acceptance(args: argparse.Namespace) -> dict[str, object]:
    """执行健康、邀请、匿名接受、指定角色登录、重放与后端权限拒绝完整流程。"""

    gateway_url = _loopback_base(args.gateway_url, 8000)
    control_url = _loopback_base(args.control_url, 8001)
    console_url = _loopback_base(args.console_url, 5173)
    admin_password = os.environ.get("METAHUB_ACCEPTANCE_ADMIN_PASSWORD", "")
    invited_password = os.environ.get("METAHUB_ACCEPTANCE_INVITED_PASSWORD", "")
    _require(bool(admin_password), "缺少管理员验收密码环境变量")
    _require(len(invited_password) >= 8, "缺少合规的受邀用户验收密码环境变量")

    gateway_health = _request_json(
        gateway_url,
        "/health",
        request_id="req_prd060_runtime_gateway_health",
    )
    control_health = _request_json(
        control_url,
        "/health",
        request_id="req_prd060_runtime_control_health",
    )
    console_status, _ = _request_status(
        console_url,
        "/",
        request_id="req_prd060_runtime_console_health",
    )
    _require(gateway_health.status == 200 and bool(gateway_health.request_id), "Gateway 健康合同失败")
    _require(control_health.status == 200 and bool(control_health.request_id), "Control 健康合同失败")
    _require(console_status == 200, "Console 健康合同失败")

    anonymous_create = _request_json(
        control_url,
        "/invitations",
        method="POST",
        payload={"email": "anonymous@example.invalid", "role": "developer"},
        request_id="req_prd060_runtime_anonymous_create",
    )
    _require(
        anonymous_create.status == 401 and anonymous_create.payload.get("code") == "auth.token_missing",
        "匿名创建邀请未被后端拒绝",
    )

    admin_login = _request_json(
        control_url,
        "/auth/login",
        method="POST",
        payload={
            "tenant_slug": args.tenant_slug,
            "email": args.admin_email,
            "password": admin_password,
        },
        request_id="req_prd060_runtime_admin_login",
    )
    _require(admin_login.status == 200, "管理员真实登录失败")
    admin_token = str(admin_login.payload.pop("access_token", ""))
    _require(bool(admin_token), "管理员登录未返回 Session")

    invited_email = f"prd060-{uuid4().hex[:16]}@example.invalid"
    created = _request_json(
        control_url,
        "/invitations",
        method="POST",
        payload={"email": invited_email, "role": "developer", "expires_in_hours": 24},
        bearer=admin_token,
        request_id="req_prd060_runtime_create",
    )
    _require(created.status == 201, "管理员创建邀请失败")
    accept_token = str(created.payload.pop("accept_token", ""))
    invitation = created.payload.get("invitation", {})
    _require(
        len(accept_token) == 43
        and type(invitation) is dict
        and invitation.get("role") == "developer"
        and invitation.get("status") == "pending",
        "创建响应的一次性凭据或角色摘要无效",
    )

    # 公开接受调用不传 bearer；该代码路径同时证明验收客户端没有构造 Authorization/Cookie。
    accepted = _request_json(
        control_url,
        f"/invitations/{accept_token}/accept",
        method="POST",
        payload={"display_name": "PRD-060 Runtime User", "password": invited_password},
        request_id="req_prd060_runtime_accept",
    )
    _require(accepted.status == 201 and accepted.payload.get("accepted") is True, "匿名接受邀请失败")
    accepted_user = accepted.payload.get("user", {})
    _require(type(accepted_user) is dict and accepted_user.get("role") == "developer", "接受角色发生漂移")

    replay = _request_json(
        control_url,
        f"/invitations/{accept_token}/accept",
        method="POST",
        payload={"display_name": "Replay", "password": invited_password},
        request_id="req_prd060_runtime_replay",
    )
    _require(
        replay.status == 410 and replay.payload.get("code") == "invitation.not_available",
        "一次性 Token 重放未返回统一 410",
    )
    # 重放完成后立即释放原始 Token；后续汇总和登录流程都不再需要该凭据。
    accept_token = ""

    invited_login = _request_json(
        control_url,
        "/auth/login",
        method="POST",
        payload={
            "tenant_slug": args.tenant_slug,
            "email": invited_email,
            "password": invited_password,
        },
        request_id="req_prd060_runtime_invited_login",
    )
    _require(invited_login.status == 200, "受邀用户真实登录失败")
    invited_token = str(invited_login.payload.pop("access_token", ""))
    _require(bool(invited_token), "受邀用户登录未返回 Session")
    current_user = _request_json(
        control_url,
        "/auth/me",
        bearer=invited_token,
        request_id="req_prd060_runtime_invited_me",
    )
    current_user_payload = current_user.payload.get("user", {})
    _require(
        current_user.status == 200
        and type(current_user_payload) is dict
        and current_user_payload.get("role") == "developer",
        "受邀用户数据库角色验收失败",
    )
    permission_codes = set(current_user.payload.get("permission_codes", []))
    _require("user.manage" not in permission_codes, "Developer 意外获得 user.manage")

    denied_create = _request_json(
        control_url,
        "/invitations",
        method="POST",
        payload={"email": "denied@example.invalid", "role": "viewer"},
        bearer=invited_token,
        request_id="req_prd060_runtime_denied_create",
    )
    _require(
        denied_create.status == 403 and denied_create.payload.get("code") == "auth.permission_denied",
        "Developer 创建邀请未被后端权限拒绝",
    )

    invited_logout = _request_json(
        control_url,
        "/auth/logout",
        method="POST",
        bearer=invited_token,
        request_id="req_prd060_runtime_invited_logout",
    )
    admin_logout = _request_json(
        control_url,
        "/auth/logout",
        method="POST",
        bearer=admin_token,
        request_id="req_prd060_runtime_admin_logout",
    )
    _require(invited_logout.status == 200 and admin_logout.status == 200, "验收 Session 注销失败")
    invited_token = ""
    admin_token = ""
    invited_password = ""
    admin_password = ""

    return {
        "status": "passed",
        "health": {"gateway": gateway_health.status, "control": control_health.status, "console": console_status},
        "anonymous_create": {
            "status": anonymous_create.status,
            "code": anonymous_create.payload.get("code"),
            "request_id": anonymous_create.request_id,
        },
        "create": {"status": created.status, "request_id": created.request_id},
        "accept": {
            "status": accepted.status,
            "request_id": accepted.request_id,
            "authorization_sent": False,
            "cookie_sent": False,
        },
        "replay": {
            "status": replay.status,
            "code": replay.payload.get("code"),
            "request_id": replay.request_id,
        },
        "invited_user": {"role": current_user_payload.get("role"), "user_manage": False},
        "denied_create": {
            "status": denied_create.status,
            "code": denied_create.payload.get("code"),
            "request_id": denied_create.request_id,
        },
        "logout": {"invited": invited_logout.status, "admin": admin_logout.status},
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """解析本地服务地址和非敏感账号标识；密码只从环境变量读取。"""

    parser = argparse.ArgumentParser(description="PRD-060 local runtime acceptance")
    parser.add_argument("--gateway-url", default="http://localhost:8000")
    parser.add_argument("--control-url", default="http://localhost:8001")
    parser.add_argument("--console-url", default="http://localhost:5173")
    parser.add_argument("--tenant-slug", default="default")
    parser.add_argument("--admin-email", default="admin@metahub.local")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """运行验收并只向 stdout 输出脱敏 JSON；失败向 stderr 输出固定原因。"""

    try:
        summary = run_acceptance(_parse_args(list(sys.argv[1:] if argv is None else argv)))
    # 仅输出上游已固定的安全原因，不打印 traceback、请求 URL 或敏感正文。
    except (RuntimeError, ValueError) as error:
        print(json.dumps({"status": "failed", "reason": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
