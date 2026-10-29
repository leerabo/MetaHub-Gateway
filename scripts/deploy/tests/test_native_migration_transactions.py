from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "shared-sdk/python"))
from metahub_shared.db.migrate import _require_transaction_body


def test_every_native_delta_and_rollback_keeps_runner_transaction_ownership():
    for path in (ROOT / "infra/migrations/postgresql").glob("*.sql"):
        if path.name[:4].isdigit() and path.name[:4] > "0139":
            _require_transaction_body(path.read_text(encoding="utf-8"))
