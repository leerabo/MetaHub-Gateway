"""Real native PG security checks; supplemental to the Ubuntu interactive flow."""
from datetime import UTC, datetime
import base64
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
for directory in ("shared-sdk/python", "control-plane-api/src", "gateway-data-plane/src"):
    sys.path.insert(0, str(ROOT / directory))
import psycopg
from psycopg import sql
from metahub_shared.db import apply_migrations, close_database_pools, connect_database
from metahub_shared.db.security import verify_password
from metahub_shared.identity import generate_totp_secret, decrypt_mfa_secret, MfaSecretOwner


def main():
    work = ROOT / ".tmp/trial-pg-import-4yycc3ou"
    if (work / "data/postmaster.pid").exists():
        raise ValueError("Task-owned native cluster is already running.")
    attempt = work / ("first-owner-" + uuid4().hex)
    # The shared native-PG fixture is intentionally disposable and may have
    # been cleaned after an earlier run; recreate its parent on demand.
    attempt.mkdir(parents=True)
    pg = ROOT / ".tools/postgresql17/pgsql/bin"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    def lifecycle(*args):
        with (attempt / "lifecycle.log").open("ab") as log:
            subprocess.run([str(pg / "pg_ctl.exe"), "-D", str(work / "data"), *args],
                           check=True, stdout=log, stderr=log, timeout=90,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    started = False
    began = time.monotonic()
    try:
        if not (work / "data" / "PG_VERSION").is_file():
            (work / "data").parent.mkdir(parents=True, exist_ok=True)
            with (attempt / "initdb.log").open("ab") as log:
                subprocess.run([
                    str(pg / "initdb.exe"), "-D", str(work / "data"),
                    "-A", "trust", "-U", "acceptance", "--no-locale", "-E", "UTF8",
                    "--wal-segsize=1",
                ], check=True, stdout=log, stderr=log, timeout=120,
                    creationflags=subprocess.CREATE_NO_WINDOW)
        started = True
        lifecycle("-l", str(attempt / "server.log"), "-w", "start", "-o",
                  f"-h 127.0.0.1 -p {port} -c timezone=UTC -c max_locks_per_transaction=2048 -c log_min_messages=panic -c log_min_error_statement=panic -c max_wal_size=128MB -c min_wal_size=16MB")
        admin_url = f"postgresql://acceptance@127.0.0.1:{port}/postgres"
        database = "first_owner_" + uuid4().hex
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        url = f"postgresql://acceptance@127.0.0.1:{port}/{database}"
        result = apply_migrations(url)
        print(json.dumps({"phase": "first_owner_native_schema", "applied_count": len(result.applied)}), flush=True)
        from metahub_control.identity import mfa_service
        metadata = connect_database(url)
        try:
            mfa_service._require_capability(metadata)
            assert mfa_service._column_exists(metadata, "global_sessions", "assurance_level")
            assert not mfa_service._column_exists(metadata, "global_sessions", "unknown_acceptance_column")
        finally:
            metadata.close()
        spec = importlib.util.spec_from_file_location("native_first_owner", ROOT / "scripts/deploy/bootstrap-owner.py")
        owner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(owner)
        secret = generate_totp_secret()
        root = hashlib.sha256(uuid4().bytes).hexdigest()
        password = "IndependentAcceptance!" + uuid4().hex
        def credentials():
            now = datetime.now(UTC)
            digest = hmac.new(base64.b32decode(secret), struct.pack(">Q", int(now.timestamp()) // 30), hashlib.sha1).digest()
            offset = digest[-1] & 15
            code = f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7fffffff) % 1000000:06d}"
            return dict(email="native-owner@example.invalid", name="Native acceptance owner",
                        password=password, secret=secret, code=code, encryption_root=root, now=now)
        with psycopg.connect(url, autocommit=True) as connection:
            # A late database exception must roll back the entire initialization.
            connection.execute("CREATE FUNCTION acceptance_reject_owner_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'acceptance failure injection'; END $$")
            connection.execute("CREATE TRIGGER acceptance_reject_owner_audit BEFORE INSERT ON platform_audit_logs FOR EACH ROW EXECUTE FUNCTION acceptance_reject_owner_audit()")
            try:
                owner.provision(connection, **credentials())
            except psycopg.Error:
                pass
            else:
                raise AssertionError("Failure injection did not reject initialization.")
            for table in ("global_accounts", "account_credentials", "account_mfa_factors", "platform_operators", "platform_operator_role_bindings", "platform_audit_logs"):
                assert connection.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))).fetchone()[0] == 0
            assert connection.execute("SELECT status FROM platform_bootstrap_state WHERE singleton_id=1").fetchone()[0] == "open"
            connection.execute("DROP TRIGGER acceptance_reject_owner_audit ON platform_audit_logs")
            connection.execute("DROP FUNCTION acceptance_reject_owner_audit()")
            installed = owner.provision(connection, **credentials())
            hashed = connection.execute("SELECT secret_hash FROM account_credentials").fetchone()[0]
            assert verify_password(password, hashed)
            assert not verify_password("wrong-password", hashed)
            factor_id, ciphertext, counter = connection.execute("SELECT factor_id,secret_ciphertext,last_accepted_counter FROM account_mfa_factors WHERE status='active'").fetchone()
            assert decrypt_mfa_secret(ciphertext, secret=root, owner=MfaSecretOwner(installed["account_id"], factor_id)) == secret
            assert counter is not None
            try:
                owner.provision(connection, **credentials())
            except ValueError:
                pass
            else:
                raise AssertionError("Repeated initialization was not refused.")
            assert connection.execute("SELECT count(*) FROM global_accounts").fetchone()[0] == 1
            assert connection.execute("SELECT role_code FROM platform_operator_role_bindings WHERE status='active'").fetchone()[0] == "platform_owner"
            assert connection.execute("SELECT status FROM platform_bootstrap_state WHERE singleton_id=1").fetchone()[0] == "completed"
        report = {"status": "passed", "platform": "Windows native PostgreSQL (supplementary)",
                  "migrations": len(result.applied), "late_failure_atomic_rollback": True,
                  "real_totp_verified_and_counter_consumed": True, "encrypted_mfa_roundtrip": True,
                  "argon2_password_verified": True, "repeat_initialization_refused": True,
                  "mfa_postgresql_schema_inspection_verified": True,
                  "original_data_modified": False, "ubuntu_interactive_flow_verified": False,
                  "elapsed_seconds": round(time.monotonic()-began, 3)}
        (attempt / "result.json").write_text(json.dumps(report, indent=2))
        (ROOT / "output/deployment-ubuntu-20261014/first-owner-native-validation.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
    except Exception as error:
        import traceback
        (attempt / "private-diagnostic.txt").write_text(traceback.format_exc())
        print(json.dumps({"status": "failed", "exception_type": type(error).__name__, "attempt": str(attempt)}), flush=True)
        return 1
    finally:
        close_database_pools()
        if started:
            lifecycle("-w", "stop", "-m", "fast")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
