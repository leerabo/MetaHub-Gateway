"""PRD-076 受信 0069 down 入口；禁止直接绕过外部 failure spool 门禁。"""

from response_cache_maintenance import run_cli

if __name__ == "__main__":
    raise SystemExit(run_cli("down"))
