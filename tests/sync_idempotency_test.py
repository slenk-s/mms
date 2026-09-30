"""MySQL 幂等同步队列状态处理测试（完全使用 fake MySQL）。"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))

from config import TABLE_MATERIALS
from local_db import LocalDB
from sync_engine import SyncEngine, SyncWorker, confirm_sync_results


class FakeMySQL:
    """只返回预设结果，不建立真实 MySQL 连接。"""

    def __init__(self, result: str):
        self.result = result
        self.calls = []

    def fetch_by_id(self, table_name, record_id):
        return None

    def fetch_by_condition(self, table_name, column, value):
        return []

    def is_connected(self):
        return True

    def apply_sync_operation(self, operation_id, table_name, operation, record_id, payload, **kwargs):
        self.calls.append((operation_id, table_name, operation, record_id, payload, kwargs))
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
        table_name=TABLE_MATERIALS,
        operation="UPDATE",
        record_id="material-1",
        payload={"id": "material-1", "material_name": "测试物料"},
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

    updated = local_db.get_sync_queue_item(operation_id=item["operation_id"])
    assert fake.calls == [(
        item["operation_id"], TABLE_MATERIALS, "UPDATE", "material-1",
        {"id": "material-1", "material_name": "测试物料"}, {"allow_missing": True},
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

    updated = local_db.get_sync_queue_item(operation_id=item["operation_id"])
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

    updated = local_db.get_sync_queue_item(operation_id=item["operation_id"])
    assert updated["status"] == "failed"
    assert updated["operation_id"] == "op-invalid"
    assert updated["attempt_count"] == 0


class ConflictFakeMySQL(FakeMySQL):
    def __init__(self):
        super().__init__("applied")
        self.cloud_record = {
            "id": "material-1", "material_name": "云端新名称",
            "updated_at": "2026-09-29T11:00:00",
        }

    def fetch_by_id(self, table_name, record_id):
        return self.cloud_record

    def is_connected(self):
        return True


def test_newer_cloud_update_returns_conflict_without_business_write(local_db: LocalDB):
    item = _claimed_item(local_db, "op-cloud-newer")
    payload = {"id": "material-1", "material_name": "本地旧名称", "updated_at": "2026-09-29T10:00:00"}
    with local_db._connect() as conn:
        conn.execute("UPDATE sync_queue SET payload = ? WHERE id = ?", (
            __import__("json").dumps(payload, ensure_ascii=False), item["id"],
        ))
    item = local_db.get_sync_queue_item(operation_id=item["operation_id"])
    worker = SyncWorker()
    fake = ConflictFakeMySQL()
    worker._mysql = fake

    result = worker._process_item(item)

    assert result["outcome"] == "conflict"
    assert result["conflict_info"]["cloud"] == fake.cloud_record
    assert fake.calls == []


def test_conflict_result_keeps_three_way_conflict_notification(local_db: LocalDB):
    app = QCoreApplication.instance() or QCoreApplication([])
    item = _claimed_item(local_db, "op-conflict-notification")
    worker = SyncWorker()
    fake = ConflictFakeMySQL()
    worker._mysql = fake
    payload = {"id": "material-1", "material_name": "本地旧名称", "updated_at": "2026-09-29T10:00:00"}
    with local_db._connect() as conn:
        conn.execute("UPDATE sync_queue SET payload = ? WHERE id = ?", (
            __import__("json").dumps(payload, ensure_ascii=False), item["id"],
        ))
    item = local_db.get_sync_queue_item(operation_id=item["operation_id"])
    result = worker._process_item(item)
    engine = SyncEngine(None, local_db)
    notifications = []
    engine.batch_conflict_detected.connect(notifications.append)
    engine._state = engine.RUNNING
    epoch = engine._begin_sync_run(False)
    engine._start_pull = lambda run_epoch: None
    try:
        engine._on_worker_push_result([result], 1, "", epoch)
        assert local_db.get_sync_queue_item(operation_id=item["operation_id"])["status"] == "succeeded"
        assert notifications == [{"tables": [TABLE_MATERIALS]}]
    finally:
        engine.stop()
    assert app is not None


def _run_conflict_result(local_db: LocalDB, item, conflict):
    """在真实 Qt Engine 中处理构造的冲突结果。"""
    app = QCoreApplication.instance() or QCoreApplication([])
    engine = SyncEngine(None, local_db)
    notifications = []
    engine.batch_conflict_detected.connect(notifications.append)
    engine._state = engine.RUNNING
    epoch = engine._begin_sync_run(False)
    engine._start_pull = lambda run_epoch: None
    try:
        engine._on_worker_push_result([
            {"item": item, "outcome": "conflict", "error": "", "conflict_info": conflict},
        ], 1, "", epoch)
    finally:
        engine.stop()
    assert app is not None
    return notifications


def test_conflict_three_way_merge_preserves_non_overlapping_local_and_cloud_changes(local_db: LocalDB):
    item = _claimed_item(local_db, "op-three-way-merge")
    base = {
        "id": "material-1", "material_code": "M-merge",
        "material_name": "基线名称", "location": "A",
        "updated_at": "2026-09-29T09:00:00",
    }
    local_payload = {**base, "material_name": "本地名称", "updated_at": "2026-09-29T10:00:00"}
    cloud_record = {**base, "location": "B", "updated_at": "2026-09-29T11:00:00"}
    local_db.insert(TABLE_MATERIALS, {**base, "workshop": "测试车间"})
    local_db.save_snapshot(TABLE_MATERIALS, "material-1", base)
    notifications = _run_conflict_result(local_db, item, {
        "table": TABLE_MATERIALS, "record_id": "material-1",
        "cloud": cloud_record, "local": local_payload,
    })

    snapshot = local_db.get_snapshot(TABLE_MATERIALS, "material-1")
    assert local_db.get_sync_queue_item(operation_id=item["operation_id"])["status"] == "succeeded"
    assert snapshot["material_name"] == "本地名称"
    assert snapshot["location"] == "B"
    assert notifications == []


def test_conflict_three_way_noop_refreshes_snapshot_without_notification(local_db: LocalDB):
    item = _claimed_item(local_db, "op-three-way-noop")
    base = {
        "id": "material-1", "material_code": "M-noop",
        "material_name": "基线名称", "location": "A",
        "updated_at": "2026-09-29T09:00:00",
    }
    local_db.save_snapshot(TABLE_MATERIALS, "material-1", base)
    notifications = _run_conflict_result(local_db, item, {
        "table": TABLE_MATERIALS, "record_id": "material-1",
        "cloud": dict(base), "local": dict(base),
    })

    assert local_db.get_sync_queue_item(operation_id=item["operation_id"])["status"] == "succeeded"
    assert local_db.get_snapshot(TABLE_MATERIALS, "material-1") == base
    assert notifications == []


def test_force_sync_works_when_paused_and_not_after_stop(local_db: LocalDB):
    app = QCoreApplication.instance() or QCoreApplication([])
    engine = SyncEngine(None, local_db)
    cycle_calls = []
    engine._sync_cycle = lambda: cycle_calls.append(True)
    try:
        engine.pause_auto_sync()
        assert engine._is_running is True
        engine.force_sync()
        assert cycle_calls == [True]

        engine.stop()
        assert engine._is_running is False
        engine.force_sync()
        assert cycle_calls == [True]
    finally:
        if engine._worker_thread.isRunning():
            engine.stop()
    assert app is not None


def test_resume_auto_sync_restarts_timers_after_pause_and_stop(local_db: LocalDB):
    app = QCoreApplication.instance() or QCoreApplication([])
    engine = SyncEngine(None, local_db)
    cycle_calls = []
    engine._sync_cycle = lambda: cycle_calls.append(True)
    try:
        engine.pause_auto_sync()
        engine.resume_auto_sync()
        assert engine._is_running is True
        assert engine._timer is not None and engine._timer.isActive()
        assert engine._full_sync_timer is not None and engine._full_sync_timer.isActive()
        assert cycle_calls == [True]

        engine.stop()
        engine.resume_auto_sync()
        assert engine._is_running is True
        assert engine._timer is not None and engine._timer.isActive()
        assert engine._full_sync_timer is not None and engine._full_sync_timer.isActive()
        assert cycle_calls == [True, True]
    finally:
        if engine._worker_thread.isRunning():
            engine.stop()
    assert app is not None



def test_paused_engine_ignores_a_health_callback_already_in_flight(local_db: LocalDB):
    """pause 后迟到的 health 回调不得领取、推送或拉取。"""
    app = QCoreApplication.instance() or QCoreApplication([])
    engine = SyncEngine(None, local_db)
    claims = []
    pushes = []
    pulls = []
    engine.local_db.claim_sync_batch = lambda *args, **kwargs: claims.append(True) or []
    engine._request_push.connect(lambda items: pushes.append(items))
    engine._start_pull = lambda: pulls.append(True)
    try:
        engine.start()
        engine.pause_auto_sync()
        assert engine.state == engine.PAUSED

        # 模拟 pause 前已发出、但 pause 后才送达主线程的 Worker health 信号。
        engine._on_worker_health(True)

        assert claims == []
        assert pushes == []
        assert pulls == []
    finally:
        engine.stop()
    assert app is not None


def test_start_recovers_worker_and_timers_after_stop(local_db: LocalDB):
    app = QCoreApplication.instance() or QCoreApplication([])
    engine = SyncEngine(None, local_db)
    engine._sync_cycle = lambda: None
    try:
        engine.stop()
        assert engine.state == engine.STOPPED
        assert not engine._worker_thread.isRunning()

        engine.start()
        assert engine.state == engine.RUNNING
        assert engine._worker_thread.isRunning()
        assert engine._timer is not None and engine._timer.isActive()
        assert engine._full_sync_timer is not None and engine._full_sync_timer.isActive()
    finally:
        engine.stop()
    assert app is not None


class _BlockingHealthMySQL:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def _ensure_conn(self):
        pass

    def health_check(self):
        self.started.set()
        assert self.release.wait(2)
        return True


def _drain_qt_events(app, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def test_pause_revokes_real_worker_callback_before_claim_or_followup(local_db: LocalDB):
    """真实 Worker 线程中的 health 回调在 pause 后不得进入本地同步流程。"""
    app = QCoreApplication.instance() or QCoreApplication([])
    engine = SyncEngine(None, local_db)
    mysql = _BlockingHealthMySQL()
    engine._worker._mysql = mysql
    claims = []
    pulls = []
    engine.local_db.claim_sync_batch = lambda *args, **kwargs: claims.append(True) or []
    engine._start_pull = lambda epoch: pulls.append(epoch)
    try:
        engine.start()
        assert mysql.started.wait(2)
        engine.pause_auto_sync()
        mysql.release.set()
        _drain_qt_events(app)

        assert engine.state == engine.PAUSED
        assert claims == []
        assert pulls == []
    finally:
        mysql.release.set()
        engine.stop()


def test_paused_epoch_rejects_late_push_pull_and_full_sync_writes(local_db: LocalDB):
    """同一轮授权被撤销后，三类迟到结果均不得确认、写表或继续下一表。"""
    app = QCoreApplication.instance() or QCoreApplication([])
    item = _claimed_item(local_db, "late-push")
    engine = SyncEngine(None, local_db)
    engine._state = engine.RUNNING
    epoch = engine._begin_sync_run(False)
    next_tables = []
    engine._pull_next_table = lambda run_epoch: next_tables.append(run_epoch)
    try:
        engine.pause_auto_sync()
        engine._on_worker_push_result([
            {"item": item, "outcome": "applied", "error": ""},
        ], 1, "", epoch)
        engine._on_worker_pull_result(TABLE_MATERIALS, [{
            "id": "late-pull", "material_code": "late",
        }], "", epoch)
        engine._on_worker_full_sync_result(TABLE_MATERIALS, [{
            "id": "late-full", "material_code": "late-full",
        }], "", epoch)

        assert local_db.get_sync_queue_item(operation_id="late-push")["status"] == "in_progress"
        assert local_db.query(TABLE_MATERIALS, "id IN (?, ?)", ("late-pull", "late-full")) == []
        assert next_tables == []
        assert engine.state == engine.PAUSED
    finally:
        engine.stop()
    assert app is not None
