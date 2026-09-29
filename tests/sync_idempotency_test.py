"""MySQL 幂等同步队列状态处理测试（完全使用 fake MySQL）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))

from local_db import LocalDB
from sync_engine import SyncWorker, confirm_sync_results


class FakeMySQL:
    """只返回预设结果，不建立真实 MySQL 连接。"""

    def __init__(self, result: str):
        self.result = result
        self.calls = []

    def apply_sync_operation(self, operation_id, table_name, operation, record_id, payload):
        self.calls.append((operation_id, table_name, operation, record_id, payload))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def local_db(tmp_path: Path):
    db = LocalDB(str(tmp_path / "idempotency.db"), workshop="测试车间")
    try:
        yield db
    finally:
        db.close_all()


def _claimed_item(db: LocalDB, operation_id: str):
    db.enqueue_sync_operation(
        table_name="MMS_Materials",
        operation="UPDATE",
        record_id="material-1",
        payload={"id": "material-1", "name": "测试物料"},
        operation_id=operation_id,
    )
    return db.claim_sync_batch(now="2026-09-29T10:00:00")[0]


@pytest.mark.parametrize(
    ("mysql_result", "expected_status"),
    [("applied", "succeeded"), ("already_applied", "succeeded")],
)
def test_applied_results_confirm_success_without_changing_operation_id(
    local_db: LocalDB, mysql_result: str, expected_status: str,
):
    item = _claimed_item(local_db, "op-success-" + mysql_result)
    fake = FakeMySQL(mysql_result)
    worker = SyncWorker()
    worker._mysql = fake

    result = worker._process_item(item)
    confirm_sync_results(local_db, [result], now="2026-09-29T10:00:00")

    updated = local_db.get_sync_queue_item(item["id"])
    assert fake.calls == [(
        item["operation_id"], "MMS_Materials", "UPDATE", "material-1",
        {"id": "material-1", "name": "测试物料"},
    )]
    assert updated["status"] == expected_status
    assert updated["operation_id"] == item["operation_id"]
    assert updated["attempt_count"] == 0


def test_connection_timeout_becomes_retryable_with_backoff(local_db: LocalDB):
    item = _claimed_item(local_db, "op-timeout")
    worker = SyncWorker()
    worker._mysql = FakeMySQL(TimeoutError("connection timed out"))

    result = worker._process_item(item)
    confirm_sync_results(local_db, [result], now="2026-09-29T10:00:00")

    updated = local_db.get_sync_queue_item(item["id"])
    assert updated["status"] == "retryable"
    assert updated["operation_id"] == "op-timeout"
    assert updated["attempt_count"] == 1
    assert updated["next_retry_at"] == "2026-09-29T10:00:05"


def test_non_retryable_error_becomes_failed_without_changing_operation_id(local_db: LocalDB):
    item = _claimed_item(local_db, "op-invalid")
    worker = SyncWorker()
    worker._mysql = FakeMySQL(ValueError("invalid payload"))

    result = worker._process_item(item)
    confirm_sync_results(local_db, [result], now="2026-09-29T10:00:00")

    updated = local_db.get_sync_queue_item(item["id"])
    assert updated["status"] == "failed"
    assert updated["operation_id"] == "op-invalid"
    assert updated["attempt_count"] == 0
