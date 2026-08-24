from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run("metahub_capacity_runner.app:app", host="0.0.0.0", port=8012, access_log=False)


if __name__ == "__main__":
    main()
