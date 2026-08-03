"""The request-time profile schema bootstrap is additive and one-shot."""

from brasstacks.profile_schema import (
    LEGACY_OWNER_EMAIL,
    PROFILE_SCHEMA_STATEMENTS,
    ensure_profile_schema,
    reset_profile_schema_cache_for_tests,
)


class Cursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, statement):
        self.connection.statements.append(statement)
        self.connection.autocommit_seen.append(self.connection.autocommit)


class Connection:
    def __init__(self, *, autocommit=False):
        self.autocommit = autocommit
        self.statements = []
        self.autocommit_seen = []

    def cursor(self):
        return Cursor(self)


def test_profile_schema_runs_each_change_separately_in_autocommit_mode():
    reset_profile_schema_cache_for_tests()
    conn = Connection(autocommit=False)

    ensure_profile_schema(conn)

    assert conn.statements == list(PROFILE_SCHEMA_STATEMENTS)
    assert all(conn.autocommit_seen)
    assert conn.autocommit is False
    assert any(LEGACY_OWNER_EMAIL in statement for statement in conn.statements)


def test_profile_schema_runs_only_once_per_warm_process():
    reset_profile_schema_cache_for_tests()
    first = Connection(autocommit=True)
    second = Connection(autocommit=True)

    ensure_profile_schema(first)
    ensure_profile_schema(second)

    assert len(first.statements) == len(PROFILE_SCHEMA_STATEMENTS)
    assert second.statements == []
