import pytest
from httpx import ASGITransport, AsyncClient
from metahub_status.app import StatusPageConfig, create_status_app


@pytest.mark.anyio
async def test_public_status_cors_does_not_enable_credentials_or_fake_health(tmp_path):
    app = create_status_app(StatusPageConfig(bundle_root=tmp_path, trusted_keys={}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://status") as client:
        result = await client.get("/public/status", headers={"Origin": "http://localhost:5173"})
    assert result.status_code == 503
    assert result.json()["code"] == "status.unavailable"
    assert result.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in result.headers
