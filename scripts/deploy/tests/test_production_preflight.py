"""Security boundary tests use synthetic config, with no live accounts or services."""
import importlib.util
from pathlib import Path
import secrets
import json
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("trial_service", ROOT / "infra/production/service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


def valid():
    result = {
        "METAHUB_ENV": "prod",
        "METAHUB_DATABASE_URL": "postgresql://user:password@postgres/db?sslmode=verify-full&sslrootcert=/run/tls/ca.crt",
        "METAHUB_PUBLIC_BASE_URL": "https://trial.example.com",
        "METAHUB_CONTROL_API_BASE_URL": "https://control-api:8001",
        "METAHUB_GATEWAY_API_BASE_URL": "https://gateway:8000",
        "METAHUB_ALLOWED_ORIGINS": "https://trial.example.com",
    }
    result.update({f"METAHUB_{name}_SECRET": secrets.token_hex(32) for name in service.SECRET_ROOTS})
    return result


def test_dedicated_postgresql_tls_configuration_is_accepted():
    service.validate_production(valid())


@pytest.mark.parametrize("field,value", [
    ("METAHUB_ENV", "dev"),
    ("METAHUB_DATABASE_URL", "sqlite:///.data/production.sqlite3"),
    ("METAHUB_DATABASE_URL", ""),
    ("METAHUB_DATABASE_URL", "postgresql://user:password@postgres/db?sslmode=require"),
    ("METAHUB_PUBLIC_BASE_URL", "http://trial.example.com"),
    ("METAHUB_CONTROL_API_BASE_URL", "http://control-api:8001"),
    ("METAHUB_ALLOWED_ORIGINS", "*"),
    ("METAHUB_DEV_PLATFORM_NO_MFA", "true"),
    ("METAHUB_PLATFORM_BOOTSTRAP_ENABLED", "true"),
    ("METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE", "test_capture"),
    ("METAHUB_KMS_PROVIDER", "test"),
    ("METAHUB_SEED_ADMIN_PASSWORD", "development-password"),
    ("METAHUB_AUTH_TOKEN_SECRET", "dev-only-change-me-metahub-auth-secret"),
])
def test_insecure_configuration_fails_closed(field, value):
    config = valid()
    config[field] = value
    with pytest.raises(ValueError):
        service.validate_production(config)


def test_authentication_and_encryption_roots_cannot_be_reused():
    config = valid()
    config["METAHUB_IDENTITY_PII_ENCRYPTION_SECRET"] = config["METAHUB_AUTH_TOKEN_SECRET"]
    with pytest.raises(ValueError):
        service.validate_production(config)


@pytest.mark.parametrize("role,port", [("control", 8001), ("gateway", 8000)])
@pytest.mark.parametrize("import_fails", [False, True])
def test_server_startup_milestones_are_safe_and_import_failure_is_closed(monkeypatch, capsys, role, port, import_fails):
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(str(ROOT / "shared-sdk/python"))
    from metahub_shared.runtime import config
    application = object()
    imports, runs = [], []
    monkeypatch.setattr(service, "load_secrets", lambda path: None)
    monkeypatch.setattr(service, "validate_production", lambda values: None)
    monkeypatch.setattr(config, "load_app_config", lambda name: None)
    monkeypatch.setattr(service, "warm_database_connection", lambda: None)
    monkeypatch.setenv("METAHUB_DATABASE_URL", "postgresql://private-password@database/secret")
    def load(module):
        imports.append(module)
        if import_fails:
            raise ModuleNotFoundError("private-password private-module-detail")
        return SimpleNamespace(app=application)
    monkeypatch.setattr(service, "importlib", SimpleNamespace(import_module=load))
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda *a, **kw: runs.append((a, kw))))
    monkeypatch.setattr(sys, "argv", ["service.py", role])
    assert service.main() == (1 if import_fails else 0)
    output = capsys.readouterr()
    events = [json.loads(line) for line in output.out.splitlines()]
    phases = ["production_config_validated", "application_config_loaded",
              "database_connection_started", "database_connection_ready", "asgi_import_started"]
    if not import_fails:
        phases += ["asgi_import_completed", "server_launch"]
    assert [event["phase"] for event in events] == phases
    for event in events:
        assert set(event) == {"status", "phase", "role", "elapsed_seconds"}
        assert event["status"] == "starting" and event["role"] == role
        assert event["elapsed_seconds"] >= 0
    assert imports == [f"metahub_{role}.app"]
    assert "private" not in output.out + output.err
    if import_fails:
        assert not runs
        assert json.loads(output.err) == {
            "status": "error", "phase": "production_preflight", "exception_type": "ModuleNotFoundError",
        }
    else:
        assert output.err == "" and len(runs) == 1
        args, kwargs = runs[0]
        assert args == (application,)
        assert kwargs["port"] == port and kwargs["access_log"] is False
        assert kwargs["reload"] is False and kwargs["ssl_keyfile"] == str(Path("/run/tls") / f"{role}.key")
        assert kwargs["ssl_certfile"] == str(Path("/run/tls") / f"{role}.crt")
        assert kwargs["forwarded_allow_ips"] == "172.30.94.0/24"


@pytest.mark.parametrize("query_fails", [False, True])
def test_startup_connection_budget_is_local_and_handle_is_returned(monkeypatch, query_fails):
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(str(ROOT / "shared-sdk/python"))
    from metahub_shared import db
    from metahub_shared.db import connection as boundary
    observed, closed = [], []
    monkeypatch.setenv("METAHUB_DATABASE_URL", "postgresql://synthetic.invalid/db")
    monkeypatch.setenv("METAHUB_DATABASE_POOL_TIMEOUT_SECONDS", "3")

    def execute(sql):
        assert sql == "SELECT 1"
        if query_fails:
            raise RuntimeError("synthetic-query-failure")
        return SimpleNamespace(fetchone=lambda: (1,))

    handle = SimpleNamespace(execute=execute, close=lambda: closed.append(True))
    def connect(url):
        assert url == "postgresql://synthetic.invalid/db"
        observed.append(boundary._database_remaining_seconds())
        return handle
    monkeypatch.setattr(db, "connect_database", connect)
    assert boundary._database_remaining_seconds() is None
    if query_fails:
        with pytest.raises(RuntimeError, match="synthetic-query-failure"):
            service.warm_database_connection()
    else:
        service.warm_database_connection()
    assert len(observed) == 1 and 29 < observed[0] <= 30
    assert closed == [True]
    assert boundary._database_remaining_seconds() is None
    assert service.os.environ["METAHUB_DATABASE_POOL_TIMEOUT_SECONDS"] == "3"


@pytest.mark.parametrize("role", ["control", "gateway"])
def test_failed_database_startup_prevents_server_import_and_redacts_failure(monkeypatch, capsys, role):
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(str(ROOT / "shared-sdk/python"))
    from metahub_shared import db
    from metahub_shared.db import connection as boundary
    from metahub_shared.db.errors import DbError
    from metahub_shared.runtime import config
    monkeypatch.setattr(service, "load_secrets", lambda path: None)
    monkeypatch.setattr(service, "validate_production", lambda values: None)
    monkeypatch.setattr(config, "load_app_config", lambda name: None)
    monkeypatch.setenv("METAHUB_DATABASE_URL", "postgresql://private-password@database/secret")
    def fail(url):
        raise DbError("db.pool_exhausted", "private-password", "private-recovery-material")
    monkeypatch.setattr(db, "connect_database", fail)
    imports = []
    monkeypatch.setattr(service, "importlib", SimpleNamespace(import_module=lambda name: imports.append(name)))
    monkeypatch.setattr(sys, "argv", ["service.py", role])
    assert service.main() == 1
    output = capsys.readouterr()
    assert imports == [] and boundary._database_remaining_seconds() is None
    assert "private" not in output.out + output.err
    assert json.loads(output.err) == {
        "status": "error", "phase": "production_preflight", "exception_type": "DbError",
        "error_code": "db.pool_exhausted",
    }
    assert json.loads(output.out.splitlines()[-1])["phase"] == "database_connection_started"


@pytest.mark.parametrize("role,port,status,expected", [
    ("control", 8001, 200, 0), ("gateway", 8000, 200, 0),
    ("control", 8001, 503, 1), ("gateway", 8000, None, 1),
])
def test_health_probe_needs_no_server_dependencies_and_keeps_verified_tls(role, port, status, expected):
    # Run a fresh interpreter so an earlier test cannot hide eager imports.
    # Simulate the HTTPS transport boundary, including a failed connection.
    program = r'''
import importlib.abc, runpy, ssl, sys, urllib.request
class NoServerDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'uvicorn', 'dotenv'}:
            raise AssertionError('healthcheck imported a server dependency')
sys.meta_path.insert(0, NoServerDependencies())
context = object()
def verified_context(*, cafile):
    assert cafile == '/run/tls/ca.crt'
    return context
ssl.create_default_context = verified_context
class Response:
    status = STATUS
    def __enter__(self): return self
    def __exit__(self, *args): pass
def request(url, *, context: object, timeout):
    assert url == URL and context is globals()['context'] and timeout == 8
    if STATUS is None: raise OSError('private transport detail')
    return Response()
urllib.request.urlopen = request
sys.argv = ['service.py', 'healthcheck', '--role', ROLE]
runpy.run_path(SERVICE, run_name='__main__')
'''
    values = {
        "STATUS": status, "URL": f"https://{'control-api' if role == 'control' else 'gateway'}:{port}/health/ready",
        "ROLE": role, "SERVICE": str(ROOT / "infra/production/service.py"),
    }
    preamble = "\n".join(f"{key} = {value!r}" for key, value in values.items())
    result = subprocess.run([sys.executable, "-I", "-c", preamble + "\n" + program], capture_output=True, text=True, timeout=15)
    assert result.returncode == expected, result.stderr
    assert result.stdout == result.stderr == ""


@pytest.mark.parametrize("missing_module", ["metahub_control.app", "metahub_gateway.app"])
def test_preflight_refuses_missing_application_dependencies_before_migration(monkeypatch, capsys, missing_module):
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(str(ROOT / "shared-sdk/python"))
    from metahub_shared import db
    from metahub_shared.db import migrate
    from metahub_shared.runtime import config
    calls = []
    monkeypatch.setattr(service, "load_secrets", lambda path: None)
    monkeypatch.setattr(service, "validate_production", lambda values: None)
    monkeypatch.setattr(config, "load_app_config", lambda name: None)
    monkeypatch.setattr(db, "connect_database", lambda url: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(db, "close_database_pools", lambda: None)
    monkeypatch.setattr(migrate, "_guard_production_postgresql_collation", lambda connection: None)
    monkeypatch.setenv("METAHUB_DATABASE_URL", "postgresql://private-password@database/secret")
    def load(module):
        calls.append(module)
        if module == missing_module:
            raise ModuleNotFoundError("private-dependency-diagnostic")
    monkeypatch.setattr(service, "importlib", SimpleNamespace(import_module=load))
    monkeypatch.setattr(sys, "argv", ["service.py", "preflight"])
    assert service.main() == 1
    assert calls[-1] == missing_module
    output = capsys.readouterr()
    assert json.loads(output.err) == {
        "status": "error", "phase": "production_preflight", "exception_type": "ModuleNotFoundError",
    }
    assert output.out == ""


@pytest.mark.parametrize("code,visible", [
    ("db.pool_exhausted", True),
    ("db.migration_failed", True),
    ("db.destructive_migration_blocked", True),
    ("db.collation_unsupported", True),
    ("unexpected-private-credential", False),
])
def test_migration_startup_reports_only_known_codes_without_secret_messages(monkeypatch, capsys, code, visible):
    monkeypatch.syspath_prepend(str(ROOT / "shared-sdk/python"))
    from metahub_shared import db
    from metahub_shared.db.errors import DbError
    from metahub_shared.runtime import config
    monkeypatch.setattr(service, "load_secrets", lambda path: None)
    monkeypatch.setattr(service, "validate_production", lambda values: None)
    monkeypatch.setattr(config, "load_app_config", lambda name: None)
    def fail():
        raise DbError(code, "postgresql://private-password@database/secret", "private-recovery-material")
    monkeypatch.setattr(db, "apply_migrations", fail)
    monkeypatch.setattr(sys, "argv", ["service.py", "migrate"])
    assert service.main() == 1
    output = capsys.readouterr()
    expected = {"status": "error", "phase": "production_preflight", "exception_type": "DbError"}
    if visible:
        expected["error_code"] = code
    assert json.loads(output.err) == expected
    assert output.out == ""


def test_application_configuration_accepts_the_production_template(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "shared-sdk/python"))
    monkeypatch.chdir(tmp_path)
    for line in (ROOT / "infra/production/production.env.example").read_text().splitlines():
        if line and not line.startswith("#"):
            name, value = line.split("=", 1)
            monkeypatch.setenv(name, value.replace("REPLACE_PASSWORD", secrets.token_hex(32)))
    for name, value in valid().items():
        monkeypatch.setenv(name, value)
    for name in ("INTERNAL_CONFIG_TOKEN", "INTERNAL_CHAT_TOKEN", "INTERNAL_BATCH_TOKEN", "BATCH_ENCRYPTION_SECRET", "BUSINESS_VERIFICATION_ENCRYPTION_SECRET", "PAYMENT_WEBHOOK_SECRET", "PAYMENT_RECEIPT_ENCRYPTION_SECRET", "EDGE_SUPPORT_BUNDLE_ENCRYPTION_SECRET"):
        monkeypatch.setenv("METAHUB_" + name, secrets.token_hex(32))
    from metahub_shared.runtime.config import load_app_config
    for name in ("control-plane-api", "gateway-data-plane"):
        result = load_app_config(name)
        assert result.environment == "prod"
        assert result.database_url.startswith("postgresql:")


def smtp_environment(monkeypatch, tmp_path):
    test_application_configuration_accepts_the_production_template(monkeypatch, tmp_path)
    settings = {
        "METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE": "smtp", "METAHUB_SMTP_HOST": "smtp.example.invalid",
        "METAHUB_SMTP_USERNAME": "smtp-acceptance", "METAHUB_SMTP_PASSWORD": "private-mail-credential",
        "METAHUB_SMTP_SENDER": "security@example.invalid", "METAHUB_SMTP_SECURITY": "starttls",
        "METAHUB_SMTP_PORT": "587", "METAHUB_SMTP_TIMEOUT_SECONDS": "10",
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)


def test_authenticated_smtp_config_excludes_credentials(monkeypatch, tmp_path):
    smtp_environment(monkeypatch, tmp_path)
    from metahub_shared.runtime.config import load_app_config
    value = load_app_config("control-plane-api")
    assert value.identity_challenge_delivery_mode == "smtp"
    assert "private-mail-credential" not in repr(value)
    assert "smtp_password" not in value.model_dump() and "smtp_username" not in value.model_dump()
    summaries = {item.key: item for item in value.config_items}
    assert summaries["METAHUB_SMTP_USERNAME"].value_preview == "***MASKED***"


@pytest.mark.parametrize("name,value", [
    ("METAHUB_SMTP_HOST", ""), ("METAHUB_SMTP_HOST", "https://smtp.example.invalid"),
    ("METAHUB_SMTP_USERNAME", ""), ("METAHUB_SMTP_PASSWORD", ""),
    ("METAHUB_SMTP_SENDER", "bad\r\nBcc: other@example.invalid"),
    ("METAHUB_SMTP_SECURITY", "none"), ("METAHUB_SMTP_PORT", "65536"),
    ("METAHUB_SMTP_TIMEOUT_SECONDS", "61"),
])
def test_incomplete_or_plaintext_smtp_is_rejected(monkeypatch, tmp_path, name, value):
    smtp_environment(monkeypatch, tmp_path)
    monkeypatch.setenv(name, value)
    from metahub_shared.runtime.config import load_app_config
    from metahub_shared.runtime.config import ConfigError
    with pytest.raises(ConfigError):
        load_app_config("control-plane-api")
