"""Tool setup must not present an SDK placeholder as a configured chat route."""

from types import SimpleNamespace

from metahub_control.developer_portal.service import _select_sdk_model_aliases


def resolve(rows):
    def execute(sql, parameters):
        assert "ma.tenant_id=?" in sql
        assert parameters == ("tenant", "dev")
        return SimpleNamespace(fetchall=lambda: rows)

    return _select_sdk_model_aliases(SimpleNamespace(execute=execute), "tenant", "dev")


def test_no_routes_are_not_a_configured_chat_model():
    assert resolve([]).chat_configured is False


def test_embedding_only_is_not_a_configured_chat_model():
    result = resolve([{"alias": "embedding", "capabilities": '{"embeddings":true}'}])
    assert result.chat_configured is False


def test_chat_configuration_uses_actual_alias():
    result = resolve([{"alias": "company-default", "capabilities": '{"chat":true}'}])
    assert result.chat_configured is True
    assert result.chat == "company-default"
