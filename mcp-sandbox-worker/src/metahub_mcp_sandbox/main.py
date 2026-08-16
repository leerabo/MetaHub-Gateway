"""Process entry point for the isolated MCP Sandbox Worker."""

from __future__ import annotations

import uvicorn

from .app import create_sandbox_app


def main() -> None:
    uvicorn.run(
        create_sandbox_app(),
        host="0.0.0.0",  # noqa: S104 - container network exposure is controlled by Compose.
        port=8091,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
