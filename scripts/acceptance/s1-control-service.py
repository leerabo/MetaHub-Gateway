"""Serve the isolated S1 browser control plane in its own process."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--frontend", type=Path, required=True)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [
        str(root / "shared-sdk/python"),
        str(root / "control-plane-api/src"),
        str(root / "gateway-data-plane/src"),
    ]

    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    from metahub_control.app import app as control
    from metahub_control.identity import registration_policy

    class CaptchaRandom:
        @staticmethod
        def choice(values):
            return values[0]

    # Keep the deterministic challenge limited to this isolated acceptance process.
    CaptchaRandom.randbelow = staticmethod(registration_policy.secrets.randbelow)
    registration_policy.secrets = CaptchaRandom

    manifest = __import__("json").loads(
        (root / "console-web/production-surface-manifest.json").read_text(encoding="utf-8")
    )
    static = StaticFiles(directory=args.frontend, html=True)
    browser = Starlette(routes=[
        Mount("/src", app=StaticFiles(directory=args.frontend / "src")),
        Route("/", endpoint=static),
        *(Route("/" + name, endpoint=static) for name in manifest["html_entrypoints"]),
        Mount("/", app=control),
    ])
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    uvicorn.run(
        browser,
        host="127.0.0.1",
        port=args.port,
        lifespan="off",
        loop="none",
        access_log=False,
        log_level="critical",
        proxy_headers=False,
        timeout_graceful_shutdown=5,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
