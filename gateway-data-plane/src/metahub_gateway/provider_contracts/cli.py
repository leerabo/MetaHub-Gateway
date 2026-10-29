"""模块职责：提供 PRD-064 `provider-adapter-contract-test` 独立命令行入口。

维护边界：命令只输出安全摘要和固定错误码，报告只允许原子写入工作区 `.artifacts`。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from .matrix import ContractMatrixError
from .probe import ProbeConfigError, load_gateway_probe_config, run_gateway_probe_sync
from .report import report_hash, write_report_atomic
from .runner import contract_source_revision_hash, run_merge_gate_sync
from .upload import ControlUploadError, build_control_upload_config, upload_report


def build_parser() -> argparse.ArgumentParser:
    """构造严格 CLI；M1 仅默认启用 mock，真实 probe 需要后续显式配置。"""

    parser = argparse.ArgumentParser(prog="provider-adapter-contract-test")
    parser.add_argument("--mode", choices=("mock", "gateway_probe"), default="mock")
    parser.add_argument(
        "--tenant-id",
        default=None,
    )
    parser.add_argument("--request-id", default=None)
    parser.add_argument("--trace-id", default=None)
    parser.add_argument("--probe-config", type=Path, default=None)
    parser.add_argument(
        "--control-url",
        default=os.environ.get("METAHUB_CONTRACT_CONTROL_URL"),
    )
    parser.add_argument(
        "--control-token-env",
        default="METAHUB_INTERNAL_CONFIG_TOKEN",
    )
    parser.add_argument("--allow-loopback-control-http", action="store_true")
    parser.add_argument("--control-timeout-seconds", type=float, default=10.0)
    parser.add_argument("--workspace-root", type=Path, default=_default_workspace_root())
    parser.add_argument("--output", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行合同、原子写报告并返回稳定退出码。

    stdout 只输出安全 JSON 摘要；任何异常只输出固定 code，不打印异常字符串、路径或响应正文。
    """

    args = build_parser().parse_args(argv)
    try:
        workspace_root = args.workspace_root.resolve(strict=True)
        # 默认链路 ID 沿用项目 middleware 的统一前缀，避免 CI 报告形成第三套身份格式。
        request_id = args.request_id or f"req_{uuid4().hex}"
        trace_id = args.trace_id or f"trc_{uuid4().hex}"
        source_revision_hash = contract_source_revision_hash()
        if args.mode == "gateway_probe":
            # 真实探测没有配置文件时不能回退到 mock 或默认 Provider，必须返回专用配置码。
            if args.probe_config is None:
                raise ProbeConfigError
            probe_config = load_gateway_probe_config(
                args.probe_config,
                workspace_root=workspace_root,
            )
            # 配置 tenant 是探测与报告 owner 的唯一事实，CLI 第二个值只能用于精确交叉核对。
            if args.tenant_id is not None and args.tenant_id != probe_config.tenant_id:
                raise ProbeConfigError
            report = run_gateway_probe_sync(
                config=probe_config,
                request_id=request_id,
                trace_id=trace_id,
                source_revision_hash=source_revision_hash,
            )
        else:
            if args.probe_config is not None:
                # mock 不读取 probe 文件，拒绝混用能防止调用者误以为在线探测已经执行。
                raise ValueError("adapter.contract_report_invalid")
            tenant_id = (
                args.tenant_id
                or os.environ.get("METAHUB_CONTRACT_TENANT_ID")
                or "ci-contract"
            )
            report = run_merge_gate_sync(
                tenant_id=tenant_id,
                request_id=request_id,
                trace_id=trace_id,
                source_revision_hash=source_revision_hash,
            )
        output = args.output or (
            workspace_root / ".artifacts" / "provider-adapter-contract" / "report.json"
        )
        written = write_report_atomic(report, output, workspace_root=workspace_root)
    # probe 文件、Gateway origin 或 Virtual Key 缺失属于显式探测配置错误，固定返回 3。
    except ProbeConfigError as exc:
        print(exc.code, file=sys.stderr)
        return 3
    # 夹具或 exact-set 漂移属于提交结构错误，使用退出码 2 阻断且只回显固定 code。
    except ContractMatrixError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    # 文件、源码身份或报告校验异常可能携带机器路径，统一折叠后再写 stderr。
    except (OSError, RuntimeError, ValueError):
        print("adapter.contract_report_invalid", file=sys.stderr)
        return 2

    upload_failed = False
    uploaded = False
    if args.control_url:
        try:
            upload_config = build_control_upload_config(
                args.control_url,
                token_env=args.control_token_env,
                allow_loopback_http=args.allow_loopback_control_http,
                timeout_seconds=args.control_timeout_seconds,
            )
            upload_report(report, upload_config)
            uploaded = True
        # 上报发生在本地报告落盘后；失败只影响退出码，不得删除或改写原合同证据。
        except ControlUploadError:
            upload_failed = True
    summary = {
        "catalog_hash": report.catalog_hash,
        "catalog_version": report.catalog_version,
        "failed_cases": report.failed_cases,
        "failure_codes": list(report.failure_codes),
        "mode": report.mode,
        "report_hash": report_hash(report),
        # 只输出相对 artifacts 路径，避免 CI 日志披露开发机绝对目录。
        "report_path": written.relative_to(workspace_root).as_posix(),
        "status": report.status,
        "suite_kind": report.suite_kind,
        "total_cases": report.total_cases,
        "uploaded": uploaded,
    }
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    if upload_failed:
        print(ControlUploadError.code, file=sys.stderr)
    # 原合同失败优先保持退出码 1；通过报告的强制上报失败才使用专用退出码 4。
    if report.status == "failed":
        return 1
    return 4 if upload_failed else 0


def _default_workspace_root() -> Path:
    """从已安装源码位置定位仓库根，使 CLI 可从仓库外 cwd 启动。"""

    return Path(__file__).resolve().parents[4]


if __name__ == "__main__":
    raise SystemExit(main())
