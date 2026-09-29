"""页面快照读取器的回归测试。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLIENT_DIR = ROOT / "client"
if str(CLIENT_DIR) not in sys.path:
    sys.path.insert(0, str(CLIENT_DIR))

from config import (  # noqa: E402
    TABLE_EMPLOYEE_RECORDS,
    TABLE_FIXED_ASSETS,
    TABLE_MATERIALS,
    TABLE_USERS,
)
from local_db import LocalDB  # noqa: E402
from page_data_loader import load_page_snapshot, open_readonly_connection  # noqa: E402


RECORD_COUNT = 5_000
WORKSHOP = "压测车间"
OTHER_WORKSHOP = "其他车间"


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@pytest.fixture(scope="module")
def benchmark_database(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str, str]:
    root = tmp_path_factory.mktemp("page_loading")
    workshop_db_path = root / "local_cache_压测车间.db"
    local_db = LocalDB(db_path=str(workshop_db_path), workshop=WORKSHOP)
    shared_db_path = Path(local_db._shared_db_path)
    local_db.close_all()

    materials = [
        (
            f"material-{index:05d}", WORKSHOP, f"MAT-{index:05d}",
            f"物料 {index:05d}", index % 97, "2026-09-29T00:00:00",
        )
        for index in range(RECORD_COUNT)
    ]
    assets = [
        (
            f"asset-{index:05d}", WORKSHOP, f"ASSET-{index:05d}",
            f"资产 {index:05d}", "设备", "在用", "2026-09-29T00:00:00",
        )
        for index in range(RECORD_COUNT)
    ]
    users = [
        (
            f"user-{index:05d}", f"user-{index:05d}", "hash", f"用户 {index:05d}",
            "user", 1, "2026-09-29T00:00:00", "2026-09-29T00:00:00",
        )
        for index in range(RECORD_COUNT)
    ]
    employees = [
        (
            f"employee-{index:05d}", f"EMP-{index:05d}", f"员工 {index:05d}", "制造部",
            "", "", f"CARD-{index:05d}", 1, "2026-09-29T00:00:00", "2026-09-29T00:00:00",
        )
        for index in range(RECORD_COUNT)
    ]

    with sqlite3.connect(workshop_db_path) as connection:
        connection.executemany(
            f"INSERT INTO {_quote(TABLE_MATERIALS)} "
            "(id, workshop, material_code, material_name, stock_qty, last_update) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            materials,
        )
        connection.execute(
            f"INSERT INTO {_quote(TABLE_MATERIALS)} "
            "(id, workshop, material_code, material_name, stock_qty, last_update) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("other-material", OTHER_WORKSHOP, "OTHER-001", "其他车间物料", 1, "2026-09-29T00:00:00"),
        )
        connection.executemany(
            f"INSERT INTO {_quote(TABLE_FIXED_ASSETS)} "
            "(id, workshop, asset_no, asset_name, category, status, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            assets,
        )

    with sqlite3.connect(shared_db_path) as connection:
        connection.executemany(
            f"INSERT INTO {_quote(TABLE_USERS)} "
            "(id, username, password, display_name, role, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            users,
        )
        connection.executemany(
            f"INSERT INTO {_quote(TABLE_EMPLOYEE_RECORDS)} "
            "(id, employee_no, name, dept, phone, fingerprint_id, card_no, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            employees,
        )

    return str(workshop_db_path), str(shared_db_path), WORKSHOP


def test_inventory_snapshot_returns_only_current_workshop(benchmark_database):
    workshop_db_path, shared_db_path, workshop = benchmark_database

    snapshot = load_page_snapshot("inventory", workshop_db_path, shared_db_path, workshop)

    assert len(snapshot["materials"]) == RECORD_COUNT
    assert {row["workshop"] for row in snapshot["materials"]} == {WORKSHOP}
    assert snapshot["materials"][0]["material_code"] == "MAT-00000"


def test_user_manage_snapshot_reads_shared_database(benchmark_database):
    workshop_db_path, shared_db_path, workshop = benchmark_database

    snapshot = load_page_snapshot("user_manage", workshop_db_path, shared_db_path, workshop)

    assert len(snapshot["users"]) == RECORD_COUNT
    assert len(snapshot["employees"]) == RECORD_COUNT
    assert snapshot["users"][0]["username"] == "user-00000"
    assert snapshot["employees"][0]["employee_no"] == "EMP-00000"


def test_unknown_page_is_rejected(benchmark_database):
    workshop_db_path, shared_db_path, workshop = benchmark_database

    with pytest.raises(ValueError, match="Unsupported page"):
        load_page_snapshot("unknown", workshop_db_path, shared_db_path, workshop)


def test_readonly_connection_rejects_writes(benchmark_database):
    workshop_db_path, _, _ = benchmark_database

    with open_readonly_connection(workshop_db_path) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE must_not_be_created (id INTEGER)")

def test_newer_request_rejects_older_result():
    from page_data_loader import PageLoadResult, PageLoadState

    state = PageLoadState()
    first_request = state.begin_request("inventory")
    second_request = state.begin_request("inventory")

    assert not state.accept(PageLoadResult("inventory", first_request, {"materials": []}, None))
    assert state.get_cached("inventory") is None
    assert state.accept(PageLoadResult("inventory", second_request, {"materials": [{"id": "latest"}]}, None))
    assert state.get_cached("inventory") == {"materials": [{"id": "latest"}]}


def test_cache_invalidation_rejects_inflight_result():
    from page_data_loader import PageLoadResult, PageLoadState

    state = PageLoadState()
    request_id = state.begin_request("asset")
    state.invalidate("asset")

    assert not state.accept(PageLoadResult("asset", request_id, {"assets": []}, None))
    assert state.get_cached("asset") is None
