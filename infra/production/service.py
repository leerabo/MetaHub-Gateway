"""TLS-only production entrypoint with a fail-closed preflight before ASGI import."""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import sys
import ssl
import time
import urllib.request
from urllib.parse import parse_qs, urlsplit

SECRET_ROOTS = (
    "AUTH_TOKEN", "IDENTITY_SESSION", "IDENTITY_CHALLENGE", "IDENTITY_PII_ENCRYPTION",
    "IDENTITY_MFA_ENCRYPTION", "WORKSPACE_SESSION", "PLATFORM_SESSION", "SUPPORT_SESSION",
    "PLATFORM_BOOTSTRAP_RECOVERY", "PROVIDER_KEY_ENCRYPTION", "CACHE_AFFINITY_HMAC",
    "ENTERPRISE_SSO_HMAC", "ENTERPRISE_SSO_ENCRYPTION", "PROMPT_TEMPLATE_ENCRYPTION",
    "FILE_ENCRYPTION", "TENANT_DATA_ENCRYPTION", "EDGE_SNAPSHOT_SIGNING", "APPROVAL_HMAC",
)

# Stable operator diagnostics are useful; exception messages can contain DSNs,
# SQL values or credentials and must never be emitted by startup logging.
SAFE_DATABASE_ERROR_CODES = frozenset({
    "db.config_invalid", "db.config_missing", "db.connection_failed", "db.collation_unsupported",
    "db.driver_missing", "db.pool_exhausted", "db.destructive_migration_blocked",
    "db.migration_checksum_changed", "db.migration_failed", "db.migration_version_mismatch",
})


def load_secrets(path: Path) -> None:
    from dotenv import dotenv_values
    if not path.is_file():
        raise ValueError("Production runtime secret file is missing.")
    values = dotenv_values(path)
    for key, value in values.items():
        # Version identity belongs to the installed image, not an old state file.
        if value is not None and key not in {"METAHUB_VERSION", "METAHUB_BUILD_COMMIT"}:
            os.environ[key] = value


def validate_production(values: dict[str, str]) -> None:
    if values.get("METAHUB_ENV") != "prod":
        raise ValueError("Production entrypoint requires METAHUB_ENV=prod.")
    database = urlsplit(values.get("METAHUB_DATABASE_URL", ""))
    query = parse_qs(database.query)
    if database.scheme not in {"postgresql", "postgres"} or not database.hostname:
        raise ValueError("Production requires an explicit PostgreSQL database.")
    if query.get("sslmode") != ["verify-full"] or not query.get("sslrootcert"):
        raise ValueError("Production PostgreSQL requires verify-full and an explicit CA.")
    for name in ("METAHUB_PUBLIC_BASE_URL", "METAHUB_CONTROL_API_BASE_URL", "METAHUB_GATEWAY_API_BASE_URL"):
        url = urlsplit(values.get(name, ""))
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError(f"{name} requires a credential-free HTTPS origin.")
    origins = values.get("METAHUB_ALLOWED_ORIGINS", "").split(",")
    public = values["METAHUB_PUBLIC_BASE_URL"].rstrip("/")
    if origins != [public]:
        raise ValueError("Trial CORS must be restricted to the configured public origin.")
    if values.get("METAHUB_DEV_PLATFORM_NO_MFA", "false") != "false":
        raise ValueError("Development MFA bypass is forbidden in production.")
    if values.get("METAHUB_PLATFORM_BOOTSTRAP_ENABLED", "false") != "false":
        raise ValueError("Production first-owner initialization must use the offline terminal command.")
    if values.get("METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE", "disabled") not in {"disabled", "smtp"}:
        raise ValueError("Test challenge capture is forbidden in production.")
    if values.get("METAHUB_KMS_PROVIDER", "disabled") == "test" or values.get("METAHUB_KMS_TEST_MASTER_KEY"):
        raise ValueError("Test KMS is forbidden in production.")
    if any(key.startswith("METAHUB_SEED_") and value for key, value in values.items()):
        raise ValueError("Development seed credentials are forbidden in the service environment.")
    seen = set()
    for root in SECRET_ROOTS:
        name = "METAHUB_" + root + "_SECRET"
        value = values.get(name, "")
        if len(value) < 32 or value.lower().startswith(("dev-only", "replace-", "test-", "change-me")):
            raise ValueError(f"{name} requires a dedicated production secret.")
        if value in seen:
            raise ValueError("Production root secrets must be independent.")
        seen.add(value)


def warm_database_connection() -> None:
    """Establish verified PostgreSQL TLS before startup work begins using the pool."""
    from metahub_shared.db import connect_database
    from metahub_shared.db.connection import database_operation_deadline

    # A first connection includes DNS, TLS and authentication. Give only this
    # startup operation its own budget; later request checkouts keep their
    # configured timeout. Return the handle so lifespan recovery can reuse it.
    with database_operation_deadline(time.monotonic() + 30):
        connection = connect_database(os.environ["METAHUB_DATABASE_URL"])
        try:
            connection.execute("SELECT 1").fetchone()
        finally:
            connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=("control", "gateway", "migrate", "preflight", "healthcheck"))
    parser.add_argument("--role", dest="health_role", choices=("control", "gateway"))
    parser.add_argument("--secret-file", type=Path, default=Path("/run/secrets/runtime.env"))
    args = parser.parse_args()
    if args.role == "healthcheck":
        context = ssl.create_default_context(cafile="/run/tls/ca.crt")
        host, port = ("control-api", 8001) if args.health_role == "control" else ("gateway", 8000)
        try:
            with urllib.request.urlopen(f"https://{host}:{port}/health/ready", context=context, timeout=8) as response:
                return 0 if response.status == 200 else 1
        except Exception:
            return 1
    started = time.monotonic()

    def startup_progress(phase):
        if args.role in {"control", "gateway"}:
            print(json.dumps({"status": "starting", "phase": phase, "role": args.role,
                              "elapsed_seconds": round(time.monotonic() - started, 3)}), flush=True)

    try:
        load_secrets(args.secret_file)
        validate_production(dict(os.environ))
        startup_progress("production_config_validated")
        if args.role in {"preflight", "migrate"}:
            # Offline startup tools can wait for a cold TLS connection without
            # changing the running API's bounded request/pool timeout.
            os.environ.setdefault("METAHUB_DATABASE_POOL_TIMEOUT_SECONDS", "30")
        from metahub_shared.runtime.config import load_app_config
        for name in ("control-plane-api", "gateway-data-plane"):
            load_app_config(name)
        startup_progress("application_config_loaded")
        if args.role == "preflight":
            from metahub_shared.db import connect_database, close_database_pools
            from metahub_shared.db.migrate import _guard_production_postgresql_collation
            connection = connect_database(os.environ["METAHUB_DATABASE_URL"])
            try:
                _guard_production_postgresql_collation(connection)
            finally:
                connection.close()
                close_database_pools()
            # Validate the actual ASGI dependency closure before any migration or
            # downtime. Importing an application does not enter its lifespan.
            for module in ("metahub_control.app", "metahub_gateway.app"):
                importlib.import_module(module)
            print(json.dumps({"status": "ok", "environment": "prod", "database": "postgresql", "version": os.environ["METAHUB_VERSION"]}))
            return 0
        if args.role == "migrate":
            from metahub_shared.db import apply_migrations
            result = apply_migrations()
            print(json.dumps({"status": "ok", "applied": result.applied, "skipped": result.skipped}))
            return 0
        startup_progress("database_connection_started")
        warm_database_connection()
        startup_progress("database_connection_ready")
        startup_progress("asgi_import_started")
        application = importlib.import_module(
            f"metahub_{'control' if args.role == 'control' else 'gateway'}.app",
        ).app
        startup_progress("asgi_import_completed")
    except Exception as error:
        # Startup errors must not print connection strings, passwords or key material.
        failure={"status":"error","phase":"production_preflight","exception_type":type(error).__name__}
        code = getattr(error, "code", None)
        if isinstance(code, str) and code in SAFE_DATABASE_ERROR_CODES:
            failure["error_code"] = code
        print(json.dumps(failure),file=sys.stderr)
        return 1
    role = args.role
    cert_root = Path("/run/tls")
    import uvicorn
    startup_progress("server_launch")
    uvicorn.run(
        application,
        host="0.0.0.0", port=8001 if role == "control" else 8000,
        reload=False, access_log=False,
        # Only the isolated ingress subnet can supply forwarded addresses.
        proxy_headers=True, forwarded_allow_ips="172.30.94.0/24",
        ssl_keyfile=str(cert_root / f"{role}.key"), ssl_certfile=str(cert_root / f"{role}.crt"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
