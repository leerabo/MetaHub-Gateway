"""Read-only invitation disclosure is bound to a current, unconsumed invitation."""
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
from metahub_control.invitations import service
from metahub_shared.db.security import hash_token
from metahub_shared.runtime.errors import ApiError


@pytest.fixture
def preview_database(tmp_path, monkeypatch):
    path = tmp_path / "preview.sqlite3"

    def connect(_config=None):
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        return connection

    with closing(connect()) as db:
        db.executescript("""
          CREATE TABLE tenants(tenant_id TEXT,name TEXT,status TEXT);
          CREATE TABLE users(tenant_id TEXT,user_id TEXT,role TEXT,status TEXT);
          CREATE TABLE roles(tenant_id TEXT,role_id TEXT,code TEXT,status TEXT);
          CREATE TABLE permissions(tenant_id TEXT,permission_id TEXT,code TEXT,status TEXT);
          CREATE TABLE role_permissions(tenant_id TEXT,role_id TEXT,permission_id TEXT);
          CREATE TABLE invitations(invitation_id TEXT,tenant_id TEXT,email TEXT,role_code TEXT,
            token_hash TEXT,status TEXT,invited_by TEXT,expires_at TEXT);
          CREATE TABLE membership_invitations(legacy_invitation_id TEXT,status TEXT,
            inviter_membership_id TEXT,workspace_id TEXT,inviter_membership_revision INTEGER,
            expires_at TEXT);
          CREATE TABLE workspace_memberships(workspace_membership_id TEXT,workspace_id TEXT,
            status TEXT,role_code TEXT,revision INTEGER);
          CREATE TABLE workspaces(workspace_id TEXT,status TEXT);
          INSERT INTO tenants VALUES('tenant','Trial organization','active');
          INSERT INTO users VALUES('tenant','inviter','admin','active');
          INSERT INTO roles VALUES('tenant','admin-role','admin','active');
          INSERT INTO roles VALUES('tenant','developer-role','developer','active');
          INSERT INTO permissions VALUES('tenant','manage','user.manage','active');
          INSERT INTO role_permissions VALUES('tenant','admin-role','manage');
          INSERT INTO workspace_memberships VALUES('member','workspace','active','owner',1);
          INSERT INTO workspaces VALUES('workspace','active');
          INSERT INTO membership_invitations
            VALUES('invitation','pending','member','workspace',1,'2099-01-01T00:00:00Z');
        """)
        db.execute("INSERT INTO invitations VALUES(?,?,?,?,?,?,?,?)", (
            "invitation", "tenant", "invited@example.test", "developer", hash_token("A" * 43),
            "pending", "inviter", "2099-01-01T00:00:00Z",
        ))
        db.commit()
    monkeypatch.setattr(service, "_connect", connect)
    monkeypatch.setattr(service, "workspace_access_schema_available_in_transaction", lambda _: True)
    return connect


def test_preview_returns_only_bound_recipient_and_never_consumes(preview_database):
    for _ in range(2):
        result = service.preview_invitation(SimpleNamespace(), "A" * 43)
        assert result.email == "invited@example.test"
        assert result.workspace_name == "Trial organization"
        assert set(result.model_dump()) == {"email", "workspace_name", "role", "expires_at"}
    with closing(preview_database()) as db:
        assert db.execute("SELECT status FROM invitations").fetchone()[0] == "pending"
        assert db.execute("SELECT status FROM membership_invitations").fetchone()[0] == "pending"


@pytest.mark.parametrize("change", [
    "UPDATE invitations SET status='revoked'",
    "UPDATE invitations SET status='accepted'",
    "UPDATE invitations SET expires_at='2000-01-01T00:00:00Z'",
    "UPDATE tenants SET status='disabled'",
    "UPDATE users SET status='disabled'",
    "UPDATE roles SET status='disabled' WHERE code='developer'",
    "DELETE FROM role_permissions",
    "UPDATE workspace_memberships SET status='disabled'",
    "UPDATE workspace_memberships SET revision=2",
    "UPDATE workspace_memberships SET role_code='viewer'",
    "UPDATE membership_invitations SET status='revoked'",
    "UPDATE membership_invitations SET expires_at='2000-01-01T00:00:00Z'",
    "UPDATE workspaces SET status='disabled'",
])
def test_inactive_invitation_or_authority_is_not_disclosed(preview_database, change):
    with closing(preview_database()) as db:
        db.execute(change)
        db.commit()
    with pytest.raises(ApiError) as error:
        service.preview_invitation(SimpleNamespace(), "A" * 43)
    assert error.value.code == "invitation.not_available"


@pytest.mark.parametrize("token", ["B" * 43, "short", "", "A" * 128])
def test_unknown_and_malformed_tokens_have_same_error(preview_database, token):
    with pytest.raises(ApiError) as error:
        service.preview_invitation(SimpleNamespace(), token)
    assert error.value.code == "invitation.not_available"
