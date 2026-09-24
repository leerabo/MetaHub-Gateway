"""First-use progress must reflect usable configuration, not record existence."""

import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from metahub_control.workspace_navigation.catalog import ROLE_TASK_SPECS
from metahub_control.workspace_navigation.models import GettingStartedResponse, RoleTask
from metahub_control.workspace_navigation.service import _task_source_facts


@pytest.fixture
def facts():
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE providers(provider_id,tenant_id,status,health_status,auth_type);
        CREATE TABLE provider_credentials(provider_id,tenant_id,status,encrypted_key);
        CREATE TABLE models(model_id,tenant_id,provider_id,status);
        CREATE TABLE model_aliases(tenant_id,default_model_id,status);
        CREATE TABLE virtual_keys(virtual_key_id,tenant_id,status);
        CREATE TABLE virtual_key_consumers(virtual_key_id,tenant_id,consumer_id);
        CREATE TABLE workspace_consumers(consumer_id,tenant_id,status);
        CREATE TABLE organization_profiles(workspace_id,name);
        CREATE TABLE request_logs(tenant_id,status);
    """)
    actor = SimpleNamespace(workspace_id="workspace", tenant_id="tenant")

    def count(source):
        value, freshness = _task_source_facts(connection, actor, frozenset({source}))[source]
        assert freshness == "fresh"
        return value

    yield connection, count
    connection.close()


def test_provider_requires_active_credentials_and_successful_health(facts):
    connection, count = facts
    connection.execute("INSERT INTO providers VALUES('p','tenant','active','unknown','bearer')")
    assert count("providers") == 0
    connection.execute("UPDATE providers SET health_status='healthy'")
    assert count("providers") == 0
    connection.execute("INSERT INTO provider_credentials VALUES('p','other','active','ciphertext')")
    assert count("providers") == 0
    connection.execute(
        "INSERT INTO provider_credentials VALUES('p','tenant','active','ciphertext')"
    )
    assert count("providers") == 1
    connection.execute("UPDATE provider_credentials SET status='disabled'")
    assert count("providers") == 0
    connection.execute("UPDATE providers SET auth_type='none'")
    assert count("providers") == 1
    connection.execute("UPDATE providers SET status='disabled'")
    assert count("providers") == 0


def test_model_needs_an_enabled_route_to_an_enabled_model(facts):
    connection, count = facts
    connection.execute("INSERT INTO providers VALUES('p','tenant','active','healthy','none')")
    connection.execute("INSERT INTO models VALUES('m','tenant','p','active')")
    assert count("models") == 0
    connection.execute("INSERT INTO model_aliases VALUES('other','m','active')")
    assert count("models") == 0
    connection.execute("INSERT INTO model_aliases VALUES('tenant','m','active')")
    assert count("models") == 1
    connection.execute("UPDATE models SET status='disabled'")
    assert count("models") == 0


def test_model_needs_its_own_healthy_provider_and_usable_credential(facts):
    connection, count = facts
    connection.execute("INSERT INTO providers VALUES('p','tenant','active','healthy','bearer')")
    connection.execute("INSERT INTO models VALUES('m','tenant','p','active')")
    connection.execute("INSERT INTO model_aliases VALUES('tenant','m','active')")
    assert count("models") == 0
    connection.execute("INSERT INTO provider_credentials VALUES('other','tenant','active','key')")
    connection.execute("INSERT INTO provider_credentials VALUES('p','other','active','key')")
    assert count("models") == 0
    connection.execute("INSERT INTO provider_credentials VALUES('p','tenant','active','key')")
    assert count("models") == 1
    connection.execute("UPDATE providers SET health_status='unhealthy'")
    assert count("models") == 0
    connection.execute("UPDATE providers SET health_status='healthy'")
    connection.execute("UPDATE provider_credentials SET encrypted_key=''")
    assert count("models") == 0
    connection.execute("UPDATE providers SET auth_type='none'")
    assert count("models") == 1


def test_completed_task_keeps_review_action_without_becoming_next_task():
    completed = RoleTask(
        task_code="provider_connected", title_key="task.provider.title",
        description_key="task.provider.description", status="completed", priority=10,
        action_route_code="workspace.gateway.providers", action_enabled=True,
        source_freshness="fresh",
    )
    pending = completed.model_copy(update={"task_code": "model_catalog_ready", "status": "ready"})
    response = GettingStartedResponse(
        owner_ref="a" * 64, role_code="owner", task_generation=1, tasks=(completed, pending),
        next_task_code=pending.task_code, completed_count=1, needs_attention_count=0,
        source_freshness="fresh", evidence_digest="b" * 64,
        contract_snapshot_id="ncs_" + "c" * 32, contract_generation=1, navigation_epoch=1,
        membership_revision=1, permission_revision=1, permission_fingerprint="d" * 64,
        observed_at=datetime(2026, 9, 11, tzinfo=UTC), request_id="req_test", trace_id="trc_test",
    )
    assert response.tasks[0].action_enabled
    payload = response.model_dump()
    payload["next_task_code"] = completed.task_code
    with pytest.raises(ValueError):
        GettingStartedResponse.model_validate(payload)
    payload.update(tasks=(completed,), next_task_code=None)
    assert GettingStartedResponse.model_validate(payload).next_task_code is None
    with pytest.raises(ValueError):
        RoleTask.model_validate({**completed.model_dump(), "status": "blocked"})


def test_employee_key_requires_active_employee_and_key(facts):
    connection, count = facts
    connection.execute("INSERT INTO virtual_keys VALUES('k','tenant','active')")
    assert count("consumer_keys") == 0
    connection.execute("INSERT INTO workspace_consumers VALUES('c','tenant','active')")
    connection.execute("INSERT INTO virtual_key_consumers VALUES('k','tenant','c')")
    assert count("consumer_keys") == 1
    connection.execute("UPDATE virtual_keys SET status='disabled'")
    assert count("consumer_keys") == 0
    connection.execute("UPDATE virtual_keys SET status='active'")
    connection.execute("UPDATE workspace_consumers SET status='disabled'")
    assert count("consumer_keys") == 0


def test_existing_name_needs_no_confirmation_and_failed_call_is_not_completed(facts):
    connection, count = facts
    connection.execute("INSERT INTO organization_profiles VALUES('workspace','Test Organization')")
    assert count("workspace_profile") == 1
    connection.execute("INSERT INTO request_logs VALUES('tenant','failed')")
    connection.execute("INSERT INTO request_logs VALUES('other','success')")
    assert count("usage") == 0
    connection.execute("INSERT INTO request_logs VALUES('tenant','success')")
    assert count("usage") == 1


def test_key_and_call_tasks_link_to_actions_with_matching_permissions():
    tasks = {task.task_code: task for task in ROLE_TASK_SPECS}
    assert tasks["project_key_ready"].action_route_code == "workspace.gateway.virtual-keys"
    assert tasks["project_key_ready"].source_code == "consumer_keys"
    assert tasks["first_gateway_call"].action_route_code == "workspace.developer.sdk"
    assert tasks["first_gateway_call"].permissions == ("developer_portal.read",)
