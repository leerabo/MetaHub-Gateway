"""PRD-076 停服 Key 轮换入口；所有实现与安全错误映射位于同目录维护核心。"""

from response_cache_maintenance import run_cli

if __name__ == "__main__":
    raise SystemExit(run_cli("rotate"))
