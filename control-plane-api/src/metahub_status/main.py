"""Run the independent public status read plane."""

from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run("metahub_status.app:app", host="0.0.0.0", port=8002, access_log=False)


if __name__ == "__main__":
    main()
