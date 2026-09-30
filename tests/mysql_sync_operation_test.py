"""MySQLClient.apply_sync_operation 的无服务事务测试。"""
from __future__ import annotations

import importlib.util
import sys
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest
from pymysql.err import IntegrityError, OperationalError, ProgrammingError
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))

from config import TABLE_MATERIALS


def _load_source_client():
    name = "pyd.mysql_client_sync_test_source"
    if name in sys.modules:
        return sys.modules[name].MySQLClient
    credentials_name = "pyd.credential_manager"
    original_credentials = sys.modules.get(credentials_name)
    credentials = types.ModuleType(credentials_name)
    credentials.get_password = lambda: ("", False)
    credentials._get_password_raw = lambda: ""
    sys.modules[credentials_name] = credentials
    path = ROOT / "client" / "pyd" / "mysql_client.pyx"
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        loader.exec_module(module)
    finally:
        if original_credentials is None:
            sys.modules.pop(credentials_name, None)
        else:
            sys.modules[credentials_name] = original_credentials
    return module.MySQLClient


MySQLClient = _load_source_client()


class FakeCursor:
    def __init__(self, *, duplicate_log=False, business_error=None):
        self.duplicate_log = duplicate_log
        self.business_error = business_error
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        if "MMS_SyncOperationLog" in sql and self.duplicate_log:
            raise IntegrityError(1062, "duplicate operation_id")
        if sql.startswith("UPDATE") and self.business_error:
            raise self.business_error

    def fetchone(self):
        return {"exists": 1}


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_obj = cursor
        self.begin_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.closed = False

    def ping(self, reconnect=False):
        return None

    def begin(self):
        self.begin_count += 1

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.closed = True


def make_client(cursor):
    client = MySQLClient.__new__(MySQLClient)
    client._conn = FakeConnection(cursor)
    client._disabled = False
    client._warn_logged = False
    client._table_columns_cache = {}
    client._host_blocked_until = 0.0
    client._workshop = ""
    return client


def test_business_write_and_operation_log_commit_in_one_transaction():
    cursor = FakeCursor()
    client = make_client(cursor)

    result = client.apply_sync_operation(
        "op-1", TABLE_MATERIALS, "UPDATE", "material-1",
        {"id": "material-1", "name": "new"},
    )

    assert result == "applied"
    assert client._conn.begin_count == 1
    assert client._conn.commit_count == 1
    assert client._conn.rollback_count == 0
    assert "MMS_SyncOperationLog" in cursor.executed[0][0]
    assert any(sql.startswith("UPDATE") for sql, _ in cursor.executed)


def test_duplicate_operation_id_returns_already_applied_without_business_write():
    cursor = FakeCursor(duplicate_log=True)
    client = make_client(cursor)

    result = client.apply_sync_operation(
        "op-duplicate", TABLE_MATERIALS, "UPDATE", "material-1",
        {"id": "material-1", "name": "new"},
    )

    assert result == "already_applied"
    assert client._conn.commit_count == 0
    assert client._conn.rollback_count == 1
    assert len(cursor.executed) == 1


def test_business_error_rolls_back_transaction():
    cursor = FakeCursor(business_error=ProgrammingError(1146, "missing table"))
    client = make_client(cursor)

    result = client.apply_sync_operation(
        "op-business-error", TABLE_MATERIALS, "UPDATE", "material-1",
        {"id": "material-1", "name": "new"},
    )

    assert result == "failed_error"
    assert client._conn.commit_count == 0
    assert client._conn.rollback_count == 1


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (OperationalError(2003, "connection refused"), "retryable_error"),
        (OperationalError(1045, "access denied"), "failed_error"),
    ],
)
def test_operational_error_codes_are_classified(error, expected):
    cursor = FakeCursor(business_error=error)
    client = make_client(cursor)

    conn = client._conn
    result = client.apply_sync_operation(
        "op-classify-" + expected, TABLE_MATERIALS, "UPDATE", "material-1",
        {"id": "material-1", "name": "new"},
    )

    assert result == expected
    assert conn.rollback_count == 1


def test_missing_update_can_be_recorded_as_legacy_noop():
    cursor = FakeCursor()
    cursor.fetchone = lambda: None
    client = make_client(cursor)

    result = client.apply_sync_operation(
        "op-missing", TABLE_MATERIALS, "UPDATE", "material-1",
        {"id": "material-1", "name": "new"}, allow_missing=True,
    )

    assert result == "applied"
    assert client._conn.commit_count == 1
    assert not any(sql.startswith("UPDATE") for sql, _ in cursor.executed)
