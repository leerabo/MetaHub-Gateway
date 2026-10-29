"""Explicit test-only usage fixtures for disposable browser acceptance databases."""

import secrets
import sys
import threading
from contextlib import closing
from pathlib import Path


def create_usage_fixture(database: Path, source: Path, token: str):
    from metahub_shared.db import connect_database
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import JSONResponse

    workspace = Path(__file__).resolve().parents[2]
    database = database.resolve(strict=True)
    if (
        not database.is_relative_to(workspace / ".tmp")
        or database.name != "acceptance.sqlite3"
        or not database.parent.name.startswith("s1-browser-")
    ):
        raise ValueError("Fixture requires a disposable browser database")
    if len(token) < 32:
        raise ValueError("Fixture requires an ephemeral credential")
    sys.path.insert(0, str(source / "control-plane-api/tests/integration"))
    from test_workspace_usage import _usage

    fixtures = {}
    lock = threading.Lock()
    database_url = "sqlite:///" + database.as_posix()

    def seed(workspace_id):
        with lock:
            return seed_once(workspace_id)

    def seed_once(workspace_id):
        if workspace_id in fixtures:
            return fixtures[workspace_id]
        with closing(connect_database(database_url)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            keys = connection.execute(
                "SELECT k.virtual_key_id,c.consumer_id FROM workspaces w "
                "JOIN organization_profiles p ON p.workspace_id=w.workspace_id "
                "JOIN workspace_tenant_bindings b ON b.workspace_id=w.workspace_id "
                "JOIN virtual_keys k ON k.tenant_id=b.tenant_id "
                "JOIN virtual_key_consumers c ON c.tenant_id=k.tenant_id "
                "AND c.virtual_key_id=k.virtual_key_id "
                "WHERE w.workspace_id=? AND w.kind='organization' "
                "AND w.creation_source='user_created' ORDER BY k.name",
                (workspace_id,),
            ).fetchall()
            if len(keys) != 2 or keys[0][1] == keys[1][1]:
                raise ValueError("Expected two newly created employee credentials")
            first, second = ({"virtual_key": {"virtual_key_id": key[0]}} for key in keys)
            _, cny_request = _usage(database_url, first, connection=connection)
            _, usd_request = _usage(
                database_url,
                first,
                currency="USD",
                estimated=True,
                connection=connection,
            )
            _usage(database_url, second, connection=connection)
            connection.commit()
        fixture = {
            "source": "isolated_usage_fixture",
            "consumer_id": keys[0][1],
            "virtual_key_id": keys[0][0],
            "requests": [cny_request, usd_request],
        }
        fixtures[workspace_id] = fixture
        return fixture

    async def endpoint(request):
        if (
            request.client is None
            or request.client.host != "127.0.0.1"
            or not secrets.compare_digest(
                request.headers.get("x-acceptance-fixture", ""),
                token,
            )
        ):
            return JSONResponse({"code": "acceptance.denied"}, status_code=403)
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"workspace_id"}:
                raise ValueError("Invalid fixture request")
            workspace_id = body["workspace_id"]
            if (
                not isinstance(workspace_id, str)
                or len(workspace_id) != 36
                or not workspace_id.startswith("wsp_")
                or any(char not in "0123456789abcdef" for char in workspace_id[4:])
            ):
                raise ValueError("Invalid workspace")
            result = await run_in_threadpool(seed, workspace_id)
        except (TypeError, ValueError):
            return JSONResponse({"code": "acceptance.invalid"}, status_code=400)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    return endpoint
