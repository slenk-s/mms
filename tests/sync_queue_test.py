"""SQLite 可恢复同步队列状态机的行为测试。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))

from local_db import LocalDB


@pytest.fixture
def local_db(tmp_path: Path):
    db = LocalDB(str(tmp_path / "local_cache_test.db"), workshop="测试车间")
    try:
        yield db
    finally:
        db.close_all()


def enqueue(db: LocalDB, operation_id: str) -> int:
    return db.enqueue_sync_operation(
        table_name="MMS_Materials",
        operation="UPDATE",
        record_id=operation_id,
        payload={"id": operation_id},
        operation_id=operation_id,
    )


def test_legacy_sync_queue_is_migrated_with_state_machine_columns(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE sync_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                table_name TEXT NOT NULL,
                operation TEXT NOT NULL,
                record_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                retry_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO sync_queue (table_name, operation, record_id, payload, retry_count)
            VALUES ('MMS_Materials', 'UPDATE', 'legacy-1', '{}', 2)
            """
        )

    db = LocalDB(str(db_path), workshop="测试车间")
    try:
        with sqlite3.connect(db_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(sync_queue)")}
            indexes = {row[1] for row in conn.execute("PRAGMA index_list(sync_queue)")}
            row = conn.execute(
                "SELECT operation_id, status, attempt_count, status_updated_at FROM sync_queue"
            ).fetchone()
        assert {
            "operation_id", "status", "attempt_count", "last_error",
            "next_retry_at", "batch_id", "status_updated_at",
        } <= columns
        assert "idx_sync_queue_claim" in indexes
        assert row[0] == "legacy-1"
        assert row[1] == "pending"
        assert row[2] == 2
        assert row[3]
    finally:
        db.close_all()


def test_claim_marks_eligible_operations_in_progress_with_one_batch(local_db: LocalDB):
    enqueue(local_db, "op-1")
    enqueue(local_db, "op-2")

    claimed = local_db.claim_sync_batch(limit=10, now="2026-09-29T10:00:00")

    assert {item["operation_id"] for item in claimed} == {"op-1", "op-2"}
    assert {item["status"] for item in claimed} == {"in_progress"}
    assert len({item["batch_id"] for item in claimed}) == 1
    assert local_db.claim_sync_batch(limit=10, now="2026-09-29T10:00:01") == []


def test_succeeded_operation_is_not_claimed_again(local_db: LocalDB):
    enqueue(local_db, "op-success")
    claimed = local_db.claim_sync_batch(now="2026-09-29T10:00:00")
    item = claimed[0]

    assert local_db.mark_sync_succeeded(item["operation_id"], item["batch_id"])
    assert local_db.get_sync_queue_item(item["id"])["status"] == "succeeded"
    assert local_db.claim_sync_batch(now="2026-09-29T10:01:00") == []


def test_retryable_operation_is_reclaimed_only_after_its_retry_time(local_db: LocalDB):
    enqueue(local_db, "op-retry")
    first = local_db.claim_sync_batch(now="2026-09-29T10:00:00")[0]

    assert local_db.mark_sync_retryable(
        first["operation_id"], first["batch_id"], "temporary network error",
        next_retry_at="2026-09-29T10:05:00", max_attempts=3,
    )
    assert local_db.claim_sync_batch(now="2026-09-29T10:04:59") == []

    retried = local_db.claim_sync_batch(now="2026-09-29T10:05:00")
    assert len(retried) == 1
    assert retried[0]["operation_id"] == "op-retry"
    assert retried[0]["attempt_count"] == 1


def test_retry_limit_marks_operation_failed(local_db: LocalDB):
    enqueue(local_db, "op-limit")
    first = local_db.claim_sync_batch(now="2026-09-29T10:00:00")[0]

    assert not local_db.mark_sync_retryable(
        first["operation_id"], first["batch_id"], "first failure",
        next_retry_at="2026-09-29T10:01:00", max_attempts=1,
    )
    item = local_db.get_sync_queue_item(first["id"])
    assert item["status"] == "failed"
    assert item["attempt_count"] == 1
    assert item["last_error"] == "first failure"
    assert local_db.claim_sync_batch(now="2026-09-29T10:02:00") == []


def test_recover_in_progress_operations_returns_them_to_retryable(local_db: LocalDB):
    enqueue(local_db, "op-recover")
    claimed = local_db.claim_sync_batch(now="2026-09-29T10:00:00")

    assert local_db.recover_in_progress_operations(now="2026-09-29T10:01:00") == 1
    recovered = local_db.get_sync_queue_item(claimed[0]["id"])
    assert recovered["status"] == "retryable"
    assert recovered["batch_id"] is None
    assert recovered["next_retry_at"] == "2026-09-29T10:01:00"
    assert local_db.claim_sync_batch(now="2026-09-29T10:01:00")[0]["operation_id"] == "op-recover"


def test_status_confirmations_require_matching_in_progress_batch(local_db: LocalDB):
    enqueue(local_db, "op-conditional")
    claimed = local_db.claim_sync_batch(now="2026-09-29T10:00:00")[0]

    assert not local_db.mark_sync_succeeded(claimed["operation_id"], "wrong-batch")
    assert not local_db.mark_sync_failed(claimed["operation_id"], "wrong-batch", "permanent error")
    assert local_db.get_sync_queue_item(claimed["id"])["status"] == "in_progress"
    assert local_db.mark_sync_failed(
        claimed["operation_id"], claimed["batch_id"], "permanent error"
    )
    assert not local_db.mark_sync_succeeded(claimed["operation_id"], claimed["batch_id"])


def test_reopening_local_db_recovers_in_progress_operations(tmp_path: Path):
    db_path = tmp_path / "reopen.db"
    first_db = LocalDB(str(db_path), workshop="测试车间")
    try:
        enqueue(first_db, "op-reopen")
        claimed = first_db.claim_sync_batch(now="2026-09-29T10:00:00")
        assert claimed[0]["status"] == "in_progress"
    finally:
        first_db.close_all()

    reopened_db = LocalDB(str(db_path), workshop="测试车间")
    try:
        recovered = reopened_db.get_sync_queue_item(claimed[0]["id"])
        assert recovered["status"] == "retryable"
        assert recovered["batch_id"] is None
    finally:
        reopened_db.close_all()


def test_pending_record_ids_exclude_succeeded_operations(local_db: LocalDB):
    enqueue(local_db, "op-succeeded-id")
    claimed = local_db.claim_sync_batch(now="2026-09-29T10:00:00")[0]

    assert local_db.mark_sync_succeeded(claimed["operation_id"], claimed["batch_id"])
    assert local_db.get_pending_record_ids("MMS_Materials") == set()
