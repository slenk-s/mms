"""SQLite 可恢复同步队列状态机的行为测试。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))

from config import TABLE_MATERIALS, TABLE_USERS
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
    assert local_db.get_sync_queue_item(operation_id=item["operation_id"])["status"] == "succeeded"
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
    item = local_db.get_sync_queue_item(operation_id=first["operation_id"])
    assert item["status"] == "failed"
    assert item["attempt_count"] == 1
    assert item["last_error"] == "first failure"
    assert local_db.claim_sync_batch(now="2026-09-29T10:02:00") == []


def test_recover_in_progress_operations_returns_them_to_retryable(local_db: LocalDB):
    enqueue(local_db, "op-recover")
    claimed = local_db.claim_sync_batch(now="2026-09-29T10:00:00")

    assert local_db.recover_in_progress_operations(now="2026-09-29T10:01:00") == 1
    recovered = local_db.get_sync_queue_item(operation_id=claimed[0]["operation_id"])
    assert recovered["status"] == "retryable"
    assert recovered["batch_id"] is None
    assert recovered["next_retry_at"] == "2026-09-29T10:01:00"
    assert local_db.claim_sync_batch(now="2026-09-29T10:01:00")[0]["operation_id"] == "op-recover"


def test_status_confirmations_require_matching_in_progress_batch(local_db: LocalDB):
    enqueue(local_db, "op-conditional")
    claimed = local_db.claim_sync_batch(now="2026-09-29T10:00:00")[0]

    assert not local_db.mark_sync_succeeded(claimed["operation_id"], "wrong-batch")
    assert not local_db.mark_sync_failed(claimed["operation_id"], "wrong-batch", "permanent error")
    assert local_db.get_sync_queue_item(operation_id=claimed["operation_id"])["status"] == "in_progress"
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
        recovered = reopened_db.get_sync_queue_item(operation_id=claimed[0]["operation_id"])
        assert recovered["status"] == "retryable"
        assert recovered["batch_id"] is None
    finally:
        reopened_db.close_all()


def test_pending_record_ids_exclude_succeeded_operations(local_db: LocalDB):
    enqueue(local_db, "op-succeeded-id")
    claimed = local_db.claim_sync_batch(now="2026-09-29T10:00:00")[0]

    assert local_db.mark_sync_succeeded(claimed["operation_id"], claimed["batch_id"])
    assert local_db.get_pending_record_ids("MMS_Materials") == set()


def test_queue_count_excludes_succeeded_and_failed_operations(local_db: LocalDB):
    local_db.enqueue_sync_operation(
        table_name=TABLE_MATERIALS, operation="UPDATE", record_id="pending",
        payload={"id": "pending"}, operation_id="count-pending",
    )
    local_db.enqueue_sync_operation(
        table_name=TABLE_MATERIALS, operation="UPDATE", record_id="success",
        payload={"id": "success"}, operation_id="count-success",
    )
    local_db.enqueue_sync_operation(
        table_name=TABLE_MATERIALS, operation="UPDATE", record_id="failed",
        payload={"id": "failed"}, operation_id="count-failed",
    )
    claimed = {item["operation_id"]: item for item in local_db.claim_sync_batch(limit=10)}
    assert local_db.mark_sync_succeeded("count-success", claimed["count-success"]["batch_id"])
    assert local_db.mark_sync_failed(
        "count-failed", claimed["count-failed"]["batch_id"], "validation error",
    )

    assert local_db.get_sync_queue_count() == 1


def test_atomic_local_change_and_queue_commit_and_rollback(local_db: LocalDB):
    operation_id = "atomic-insert"
    local_db.apply_local_change_with_sync(
        TABLE_MATERIALS,
        "INSERT",
        "material-atomic",
        {"id": "material-atomic", "material_code": "ATOMIC", "material_name": "原子物料"},
        {"id": "material-atomic", "material_code": "ATOMIC", "material_name": "原子物料"},
        operation_id=operation_id,
    )

    saved = next(row for row in local_db.query(TABLE_MATERIALS)
                 if row["id"] == "material-atomic")
    assert saved["material_code"] == "ATOMIC"
    assert saved["workshop"] == "测试车间"
    queued = local_db.get_sync_queue(limit=10)
    assert [(item["operation_id"], item["operation"], item["record_id"])
            for item in queued] == [(operation_id, "INSERT", "material-atomic")]

    with pytest.raises(sqlite3.IntegrityError):
        local_db.apply_local_change_with_sync(
            TABLE_MATERIALS,
            "INSERT",
            "material-rollback",
            {"id": "material-rollback", "material_code": "ROLLBACK", "material_name": "不应提交"},
            {"id": "material-rollback", "material_code": "ROLLBACK", "material_name": "不应提交"},
            operation_id=operation_id,
        )

    assert not any(row["id"] == "material-rollback" for row in local_db.query(TABLE_MATERIALS))
    assert local_db.get_sync_queue_count() == 1


def test_atomic_update_and_delete_enqueue_in_same_transaction(local_db: LocalDB):
    local_db.insert(TABLE_MATERIALS, {
        "id": "material-update-delete", "material_code": "UPDATE-DELETE", "material_name": "旧名称",
    })

    local_db.apply_local_change_with_sync(
        TABLE_MATERIALS, "UPDATE", "material-update-delete",
        {"material_name": "新名称"},
        {"id": "material-update-delete", "material_name": "新名称"},
        operation_id="atomic-update",
    )
    assert next(row for row in local_db.query(TABLE_MATERIALS) if row["id"] == "material-update-delete")["material_name"] == "新名称"

    local_db.apply_local_change_with_sync(
        TABLE_MATERIALS, "DELETE", "material-update-delete", {}, {},
        operation_id="atomic-delete",
    )
    assert not any(row["id"] == "material-update-delete" for row in local_db.query(TABLE_MATERIALS))
    assert [(item["operation_id"], item["operation"])
            for item in local_db.get_sync_queue(limit=10)] == [
        ("atomic-update", "UPDATE"), ("atomic-delete", "DELETE"),
    ]


def test_atomic_shared_table_change_and_queue_use_one_transaction(local_db: LocalDB):
    local_db.apply_local_change_with_sync(
        TABLE_USERS,
        "INSERT",
        "atomic-user",
        {
            "id": "atomic-user", "username": "atomic-user", "password": "secret",
            "display_name": "Atomic User",
        },
        {"id": "atomic-user", "username": "atomic-user"},
        operation_id="atomic-shared-user",
    )

    assert local_db.query(TABLE_USERS, "id = ?", ("atomic-user",))[0]["username"] == "atomic-user"
    assert local_db.get_sync_queue(limit=10)[0]["operation_id"] == "atomic-shared-user"



def test_shared_outbox_and_business_write_roll_back_in_one_public_database_transaction(local_db: LocalDB):
    """共享表写入失败时，公共 outbox 与业务表必须一起回滚。"""
    shared_path = Path(local_db._shared_db_path)
    with sqlite3.connect(shared_path) as conn:
        conn.execute(
            """
            INSERT INTO shared_sync_queue (
                operation_id, table_name, operation, record_id, payload, status,
                attempt_count, status_updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?)
            """,
            ("shared-duplicate", TABLE_USERS, "INSERT", "existing-user", "{}", "2026-09-30T10:00:00"),
        )

    with pytest.raises(sqlite3.IntegrityError):
        local_db.apply_local_change_with_sync(
            TABLE_USERS,
            "INSERT",
            "rolled-back-user",
            {"id": "rolled-back-user", "username": "rolled-back-user", "password": "secret"},
            {"id": "rolled-back-user", "username": "rolled-back-user"},
            operation_id="shared-duplicate",
        )

    assert local_db.query(TABLE_USERS, "id = ?", ("rolled-back-user",)) == []
    with sqlite3.connect(shared_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM shared_sync_queue WHERE record_id = ?", ("rolled-back-user",)
        ).fetchone()[0] == 0


def test_existing_shared_queue_entries_migrate_to_public_outbox_without_loss(tmp_path: Path):
    db_path = tmp_path / "legacy-shared.db"
    first = LocalDB(str(db_path), workshop="测试车间")
    try:
        first.enqueue_sync_operation(
            TABLE_USERS, "UPDATE", "legacy-user", {"id": "legacy-user"}, operation_id="legacy-shared-op"
        )
    finally:
        first.close_all()

    db = LocalDB(str(db_path), workshop="测试车间")
    try:
        with sqlite3.connect(db_path) as workshop_conn:
            assert workshop_conn.execute(
                "SELECT COUNT(*) FROM sync_queue WHERE operation_id = ?", ("legacy-shared-op",)
            ).fetchone()[0] == 0
        with sqlite3.connect(db._shared_db_path) as shared_conn:
            row = shared_conn.execute(
                "SELECT operation_id, table_name, status FROM shared_sync_queue WHERE operation_id = ?",
                ("legacy-shared-op",),
            ).fetchone()
        assert row == ("legacy-shared-op", TABLE_USERS, "pending")
        assert [item["operation_id"] for item in db.get_sync_queue()] == ["legacy-shared-op"]
    finally:
        db.close_all()


def test_unified_queue_claims_and_confirms_workshop_and_shared_operations(local_db: LocalDB):
    local_db.enqueue_sync_operation(
        TABLE_MATERIALS, "UPDATE", "workshop-row", {"id": "workshop-row"}, operation_id="workshop-op"
    )
    local_db.enqueue_sync_operation(
        TABLE_USERS, "UPDATE", "shared-row", {"id": "shared-row"}, operation_id="shared-op"
    )

    claimed = local_db.claim_sync_batch(limit=10, now="2026-09-30T10:00:00")
    assert {item["operation_id"] for item in claimed} == {"workshop-op", "shared-op"}
    assert len({item["batch_id"] for item in claimed}) == 1
    assert local_db.get_pending_record_ids(TABLE_USERS) == {"shared-row"}
    for item in claimed:
        assert local_db.mark_sync_succeeded(item["operation_id"], item["batch_id"])
    assert local_db.get_sync_queue_count() == 0


def test_operation_id_is_unique_across_workshop_and_shared_queues(local_db: LocalDB):
    local_db.enqueue_sync_operation(
        TABLE_MATERIALS, "UPDATE", "workshop-row", {"id": "workshop-row"}, operation_id="global-operation-id"
    )

    with pytest.raises(sqlite3.IntegrityError):
        local_db.apply_local_change_with_sync(
            TABLE_USERS,
            "INSERT",
            "global-id-user",
            {"id": "global-id-user", "username": "global-id-user", "password": "secret"},
            {"id": "global-id-user", "username": "global-id-user"},
            operation_id="global-operation-id",
        )

    assert local_db.query(TABLE_USERS, "id = ?", ("global-id-user",)) == []



def test_shared_outbox_protects_public_table_during_full_replace(local_db: LocalDB):
    local_db.apply_local_change_with_sync(
        TABLE_USERS,
        "INSERT",
        "local-pending-user",
        {"id": "local-pending-user", "username": "local-pending-user", "password": "secret"},
        {"id": "local-pending-user", "username": "local-pending-user"},
        operation_id="public-pending-op",
    )

    local_db.replace_table_transactional(TABLE_USERS, [{
        "id": "cloud-user", "username": "cloud-user", "password": "cloud-secret",
    }])

    users = {user["id"] for user in local_db.query(TABLE_USERS)}
    assert users == {"local-pending-user", "cloud-user"}


def test_same_queue_id_in_both_outboxes_needs_source_or_operation_id(local_db: LocalDB):
    """两个 SQLite outbox 都从 id=1 开始，裸 id 绝不能跨库命中。"""
    workshop_id = local_db.enqueue_sync_operation(
        TABLE_MATERIALS, "UPDATE", "workshop-1", {"id": "workshop-1"},
        operation_id="workshop-id-1",
    )
    shared_id = local_db.enqueue_sync_operation(
        TABLE_USERS, "UPDATE", "shared-1", {"id": "shared-1"},
        operation_id="shared-id-1",
    )
    assert (workshop_id, shared_id) == (1, 1)

    with pytest.raises(ValueError, match="source"):
        local_db.get_sync_queue_item(1)
    with pytest.raises(ValueError, match="source"):
        local_db.remove_sync_queue(1)
    with pytest.raises(ValueError, match="source"):
        local_db.increment_retry(1)

    workshop = local_db.get_sync_queue_item(1, source="workshop")
    shared = local_db.get_sync_queue_item(operation_id="shared-id-1")
    assert workshop["operation_id"] == "workshop-id-1"
    assert workshop["queue_source"] == "workshop"
    assert shared["queue_source"] == "shared"

    assert local_db.increment_retry(1, source="shared")
    assert local_db.get_sync_queue_item(operation_id="shared-id-1")["attempt_count"] == 1
    assert local_db.get_sync_queue_item(operation_id="workshop-id-1")["attempt_count"] == 0
    assert local_db.remove_sync_queue(operation_id="workshop-id-1")
    assert local_db.get_sync_queue_item(operation_id="workshop-id-1") is None
    assert local_db.get_sync_queue_item(operation_id="shared-id-1") is not None
