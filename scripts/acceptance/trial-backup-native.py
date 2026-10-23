"""Real PG dump/restore and full business-table comparison on task-owned copies.

Uses the previously imported SQLite copy in our private Windows native cluster.
This does not prove the Ubuntu Compose/GPG backup command or public deployment.
"""
from datetime import UTC, datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared-sdk/python"))
import psycopg
from psycopg import sql
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def main():
    work = ROOT / ".tmp/trial-pg-import-4yycc3ou"
    pg = ROOT / ".tools/postgresql17/pgsql/bin"
    status = subprocess.run([str(pg / "pg_ctl.exe"), "-D", str(work / "data"), "status"], capture_output=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if status.returncode != 3:
        raise ValueError("Task-owned cluster must be confirmed stopped.")
    attempt = work / ("backup-proof-" + uuid4().hex)
    attempt.mkdir()
    def lifecycle(*args):
        with (attempt / "lifecycle.log").open("ab") as log:
            subprocess.run([str(pg / "pg_ctl.exe"), "-D", str(work / "data"), *args], check=True,
                           stdout=log, stderr=log, timeout=210, creationflags=subprocess.CREATE_NO_WINDOW)
    spec = importlib.util.spec_from_file_location("native_backup_fingerprint", ROOT / "scripts/deploy/database-fingerprint.py")
    proof = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proof)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"postgresql://acceptance@127.0.0.1:{port}/postgres"
    started = False
    began = time.monotonic()
    try:
        started = True
        lifecycle("-l", str(attempt / "server.log"), "-w", "-t", "180", "start", "-o",
                  f"-h 127.0.0.1 -p {port} -c timezone=UTC -c max_locks_per_transaction=2048 -c log_min_messages=panic -c log_min_error_statement=panic -c max_wal_size=128MB -c min_wal_size=16MB")
        with psycopg.connect(url) as connection:
            before = proof.fingerprint(connection)
        print(json.dumps({"phase": "fingerprinted_import_copy", "tables": len(before["tables"]),
                          "rows": sum(row["rows"] for row in before["tables"].values())}), flush=True)
        dumped = subprocess.run([str(pg / "pg_dump.exe"), "--format=custom", "--no-owner", url],
                                capture_output=True, check=True, timeout=600, creationflags=subprocess.CREATE_NO_WINDOW).stdout
        # Store only encrypted backup bytes. The independent test key exists only
        # in this process; production uses its separately retained GPG key file.
        key, nonce = secrets.token_bytes(32), secrets.token_bytes(12)
        encrypted = AESGCM(key).encrypt(nonce, dumped, b"metahub-native-backup-acceptance")
        (attempt / "database.gcm").write_bytes(nonce + encrypted)
        plain_hash = hashlib.sha256(dumped).digest()
        del dumped
        stored = (attempt / "database.gcm").read_bytes()
        restored_bytes = AESGCM(key).decrypt(stored[:12], stored[12:], b"metahub-native-backup-acceptance")
        assert hashlib.sha256(restored_bytes).digest() == plain_hash
        restored = "restore_" + datetime.now(UTC).strftime("%Y%m%d_%H%M%S_") + secrets.token_hex(6)
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(restored)))
        target = f"postgresql://acceptance@127.0.0.1:{port}/{restored}"
        subprocess.run([str(pg / "pg_restore.exe"), "--no-owner", "--exit-on-error", "--dbname", target],
                       input=restored_bytes, capture_output=True, check=True, timeout=900,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        del restored_bytes, key
        with psycopg.connect(target) as connection:
            after = proof.fingerprint(connection)
        assert before == after, "Restored business database differs from its read-only source snapshot."
        print(json.dumps({"phase": "full_dump_restored_and_matched", "tables": len(after["tables"])}), flush=True)
        with psycopg.connect(target) as connection:
            connection.execute("CREATE TABLE acceptance_restore_canary(id BIGSERIAL PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("INSERT INTO acceptance_restore_canary(value) VALUES('before')")
        with psycopg.connect(target) as connection:
            changed_schema = proof.fingerprint(connection)
        assert before != changed_schema
        with psycopg.connect(target) as connection:
            connection.execute("UPDATE acceptance_restore_canary SET value='after'")
        with psycopg.connect(target) as connection:
            changed_data = proof.fingerprint(connection)
        assert changed_schema["tables"]["acceptance_restore_canary"] != changed_data["tables"]["acceptance_restore_canary"]
        with psycopg.connect(url) as connection:
            source_after = proof.fingerprint(connection)
        assert before == source_after
        report = {"status": "passed", "platform": "Windows native PostgreSQL (supplementary)",
                  "business_tables": len(before["tables"]), "business_rows": sum(row["rows"] for row in before["tables"].values()),
                  "real_pg_dump_and_pg_restore": True, "schema_and_all_table_hashes_match": True,
                  "sequence_states_match": True, "schema_change_detected": True, "row_change_detected": True,
                  "source_business_schema_unchanged": True, "original_sqlite_modified": False,
                  "encrypted_backup_bytes": len(stored), "encryption": "AES-256-GCM; acceptance-only in-memory key",
                  "backup_key_retained": False, "ubuntu_compose_gpg_commands_verified": False,
                  "elapsed_seconds": round(time.monotonic() - began, 3)}
        (attempt / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        (ROOT / "output/deployment-ubuntu-20261014/backup-native-validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)
    except Exception as error:
        import traceback
        (attempt / "private-diagnostic.txt").write_text(traceback.format_exc(), encoding="utf-8")
        print(json.dumps({"status": "failed", "exception_type": type(error).__name__, "attempt": str(attempt)}), flush=True)
        return 1
    finally:
        if started:
            lifecycle("-w", "-t", "180", "stop", "-m", "fast")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
