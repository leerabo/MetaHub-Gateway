"""Permission projection remains live, sorted and isolated across all SQL sources."""

from contextlib import closing

from metahub_control.platform_access.service import _authority
from metahub_shared.db.connection import connect_database


def test_permission_union_deduplicates_and_rechecks_binding_and_extension_changes():
    with closing(connect_database("sqlite:///:memory:")) as connection:
        connection.executescript("""
            CREATE TABLE platform_operator_role_bindings(operator_id,role_code,status);
            CREATE TABLE platform_role_permissions(role_code,permission_code);
            CREATE TABLE platform_role_permission_extensions(role_code,permission_code);
            INSERT INTO platform_operator_role_bindings VALUES
              ('current','platform_owner','active'),
              ('current','platform_security','active'),
              ('current','platform_ops','revoked'),
              ('foreign','platform_ops','active');
            INSERT INTO platform_role_permissions VALUES
              ('platform_owner','z.write'),('platform_owner','shared.read'),
              ('platform_security','shared.read'),('platform_ops','foreign.only');
            INSERT INTO platform_role_permission_extensions VALUES
              ('platform_security','a.audit'),('platform_owner','shared.read');
        """)
        connection.execute("BEGIN")
        roles, permissions, initial = _authority(connection, "current")
        assert roles == ("platform_owner", "platform_security")
        assert permissions == ("a.audit", "shared.read", "z.write")
        assert _authority(connection, "foreign")[1] == ("foreign.only",)

        connection.execute("""
            UPDATE platform_operator_role_bindings SET status='revoked'
            WHERE operator_id='current' AND role_code='platform_owner'
        """)
        roles, permissions, revoked = _authority(connection, "current")
        assert roles == ("platform_security",)
        assert permissions == ("a.audit", "shared.read")
        assert revoked != initial

        connection.execute("DELETE FROM platform_role_permission_extensions")
        assert _authority(connection, "current")[1] == ("shared.read",)
        assert _authority(connection, "current")[2] != revoked
        assert _authority(connection, "unknown")[:2] == ((), ())
        connection.rollback()
