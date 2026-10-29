"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：shared-sdk/python/metahub_shared/db/cli.py
"""

from __future__ import annotations

import argparse
import json
import sys

from .errors import DbError
from .migrate import apply_migrations, rollback_last_migration
from .seed import apply_seed_data


def main(argv: list[str] | None = None) -> int:
    """数据库命令行入口。

    统一承载 migrate/seed/rollback 三类本地和 CI 命令，所有结果都输出 JSON，
    便于脚本解析状态和错误码。
    """

    parser = argparse.ArgumentParser(prog="metahub-db")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("migrate")
    subparsers.add_parser("seed")
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--confirm-prod", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.command == "migrate":
            # 迁移必须先于 seed 执行，保证后续 PRD 表结构已经存在。
            result = apply_migrations()
            _print_json({"status": "ok", **result.__dict__})
            return 0
        if args.command == "seed":
            result = apply_seed_data()
            _print_json({"status": "ok", **result.__dict__})
            return 0
        if args.command == "rollback":
            # rollback 只回滚最后一个已记录迁移，生产环境需要显式确认。
            result = rollback_last_migration(confirm_prod=args.confirm_prod)
            _print_json({"status": "ok", **result.__dict__})
            return 0
    # 错误分支统一收敛为标准错误码，避免泄露上游原始敏感信息。
    except DbError as exc:
        _print_json(
            {
                "status": "error",
                "code": exc.code,
                "message": exc.message,
                "suggestion": exc.suggestion,
            },
            stream=sys.stderr,
        )
        return 1
    return 1


def _print_json(payload: dict[str, object], *, stream=sys.stdout) -> None:
    """在 数据库基础设施 内部复用 print json 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    print(json.dumps(payload, ensure_ascii=False, default=str), file=stream)


if __name__ == "__main__":
    raise SystemExit(main())
