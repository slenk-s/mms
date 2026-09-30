"""同步引擎模块
负责云端(MySQL)与本地数据的同步：拉取、推送、冲突检测

网络 IO 全部在后台线程中执行，不阻塞主线程 UI。
"""
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Dict, Callable
from PySide6.QtCore import QObject, Signal, QTimer, QThread, Slot
from pymysql.err import OperationalError as MySQLOperationalError

from config import (
    SYNC_INTERVAL_SECONDS, SYNC_RETRY_MAX, SYNC_BATCH_SIZE,
    FULL_SYNC_INTERVAL_MINUTES, LOCAL_DB_PATH,
    TABLE_MATERIALS, TABLE_BORROW_RECORDS, TABLE_FIXED_ASSETS,
    TABLE_CONFIG_ITEMS, TABLE_USERS, TABLE_EMPLOYEE_RECORDS,
)
from logger import get_logger

_log = get_logger(__name__)

SYNC_APPLIED = "applied"
SYNC_ALREADY_APPLIED = "already_applied"
SYNC_RETRYABLE_ERROR = "retryable_error"
SYNC_FAILED_ERROR = "failed_error"
SYNC_CONFLICT = "conflict"
SYNC_RETRY_BACKOFF_BASE_SECONDS = 5
SYNC_RETRY_BACKOFF_MAX_SECONDS = 300


def _as_datetime(value):
    if value is None:
        return datetime.now()
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _next_retry_at(item: Dict, now) -> str:
    """基于已记录的失败次数计算有上限的指数退避时间。"""
    attempts = max(int(item.get("attempt_count") or 0), 0)
    delay = min(
        SYNC_RETRY_BACKOFF_BASE_SECONDS * (2 ** attempts),
        SYNC_RETRY_BACKOFF_MAX_SECONDS,
    )
    return (_as_datetime(now) + timedelta(seconds=delay)).isoformat()


def confirm_sync_results(local_db, results: list, now=None) -> Dict[str, int]:
    """在主线程确认已领取的同步结果，不删除队列操作记录。"""
    summary = {"succeeded": 0, "retryable": 0, "failed": 0, "conflicts": []}
    for result in results or []:
        item = result.get("item", {})
        operation_id = item.get("operation_id")
        batch_id = item.get("batch_id")
        outcome = result.get("outcome")
        error = result.get("error") or outcome or "未知同步错误"
        if not operation_id or not batch_id:
            _log.error("忽略缺少 operation_id 或 batch_id 的同步结果: %r", result)
            continue

        if outcome == SYNC_CONFLICT:
            if local_db.mark_sync_succeeded(operation_id, batch_id):
                conflict_info = result.get("conflict_info")
                if conflict_info:
                    summary["conflicts"].append(conflict_info)
                summary["succeeded"] += 1
            continue

        if outcome in {SYNC_APPLIED, SYNC_ALREADY_APPLIED}:
            if local_db.mark_sync_succeeded(operation_id, batch_id):
                if item.get("operation") == "DELETE":
                    local_db.delete_snapshot(item["table_name"], item["record_id"])
                else:
                    try:
                        local_db.save_snapshot(
                            item["table_name"], item["record_id"],
                            json.loads(item["payload"]),
                        )
                    except (KeyError, TypeError, json.JSONDecodeError) as e:
                        _log.warning("同步成功但快照保存失败: %s", e)
                summary["succeeded"] += 1
            continue

        if outcome == SYNC_RETRYABLE_ERROR:
            retry_at = _next_retry_at(item, now)
            retryable = local_db.mark_sync_retryable(
                operation_id, batch_id, error, next_retry_at=retry_at,
                max_attempts=SYNC_RETRY_MAX,
            )
            summary["retryable" if retryable else "failed"] += 1
            continue

        local_db.mark_sync_failed(operation_id, batch_id, error)
        summary["failed"] += 1
    return summary


class SyncWorker(QObject):
    """后台工作线程：只执行网络 IO，不触碰主线程的 SQLite

    持有独立的 MySQLClient 实例（在 Worker 线程中初始化），
    所有网络请求都在工作线程中顺序执行，主线程保持响应。
    """

    # 结果信号（Worker → Engine，跨线程安全）
    health_result = Signal(bool)                 # 网络是否可达
    push_result = Signal(object, int, str)       # results, total_pushed, error
    pull_result = Signal(str, object, str)      # table, records, error
    full_sync_result = Signal(str, object, str)  # table, records, error

    def __init__(self):
        super().__init__()
        self._mysql = None
        self._sync_tables = [
            TABLE_MATERIALS, TABLE_BORROW_RECORDS, TABLE_FIXED_ASSETS,
            TABLE_CONFIG_ITEMS, TABLE_USERS, TABLE_EMPLOYEE_RECORDS,
        ]

    def _ensure_init(self):
        """懒加载：确保在 Worker 线程中初始化 MySQLClient"""
        if self._mysql is not None:
            return
        from mysql_client import MySQLClient
        self._mysql = MySQLClient()

    # ---------- 槽函数：由 Engine 通过信号触发（跨线程） ----------

    @Slot()
    def check_health(self):
        """检测网络及 MySQL 连通性（后台线程执行，不阻塞主线程）"""
        self._ensure_init()
        try:
            self._mysql._ensure_conn()  # 尝试建立/恢复连接
            ok = self._mysql.health_check()
            self.health_result.emit(ok)
        except (RuntimeError, OSError, ValueError) as e:
            _log.warning("健康检查失败: %s", e)
            self.health_result.emit(False)
        except Exception as e:
            _log.warning("健康检查未预期异常: %s", e)
            self.health_result.emit(False)

    @Slot(list)
    def push_items(self, queue_items: list):
        """推送队列项到云端(MySQL)"""
        self._ensure_init()
        results = [self._process_item(item) for item in queue_items]
        total = sum(
            result["outcome"] in {SYNC_APPLIED, SYNC_ALREADY_APPLIED, SYNC_CONFLICT}
            for result in results
        )
        self.push_result.emit(results, total, "")

    @Slot(str, str, str)
    def pull_table(self, table: str, last_sync: str, workshop: str):
        self._ensure_init()
        self._mysql.set_workshop(workshop)
        try:
            if last_sync:
                records = self._mysql.fetch_since(table, last_sync)
            else:
                records = self._mysql.fetch_all(table)
            self.pull_result.emit(table, records or [], "")
        except (RuntimeError, OSError, ValueError) as e:
            err_msg = str(e)
            _log.error("拉取 %s 失败: %s", table, err_msg)
            self.pull_result.emit(table, [], err_msg)
        except Exception as e:
            err_msg = str(e)
            _log.error("拉取 %s 未预期异常: %s", table, err_msg)
            self.pull_result.emit(table, [], err_msg)

    @Slot(str, str)
    def full_sync_table(self, table: str, workshop: str):
        self._ensure_init()
        self._mysql.set_workshop(workshop)
        try:
            records = self._mysql.fetch_all(table)
            self.full_sync_result.emit(table, records or [], "")
        except (RuntimeError, OSError, ValueError) as e:
            err_msg = str(e)
            _log.error("全量同步 %s 失败: %s", table, err_msg)
            self.full_sync_result.emit(table, [], err_msg)
        except Exception as e:
            err_msg = str(e)
            _log.error("全量同步 %s 未预期异常: %s", table, err_msg)
            self.full_sync_result.emit(table, [], err_msg)

    # ---------- 内部处理 ----------

    def _process_item(self, item: Dict) -> Dict:
        """在幂等写入前读取云端版本，保留既有冲突检测语义。"""
        table = item.get("table_name")
        operation = item.get("operation")
        record_id = item.get("record_id")
        try:
            payload = json.loads(item["payload"])
            effective_operation = operation
            effective_record_id = record_id
            effective_payload = payload
            allow_missing = False

            if operation == "INSERT":
                existing = self._mysql.fetch_by_id(table, record_id)
                if existing:
                    conflict = self._check_conflict(table, record_id, existing, payload)
                    if conflict:
                        return {
                            "item": item, "outcome": SYNC_CONFLICT,
                            "error": "", "conflict_info": conflict,
                        }
                    effective_operation = "UPDATE"
                else:
                    # 业务唯一字段查重（如 asset_no / material_code）。
                    biz_key, biz_col = self._get_biz_unique_key(table, payload)
                    if biz_key and biz_col:
                        duplicates = self._mysql.fetch_by_condition(table, biz_col, biz_key)
                        if duplicates:
                            duplicate_id = duplicates[0].get("id", record_id)
                            conflict = self._check_conflict(
                                table, duplicate_id, duplicates[0], payload
                            )
                            if conflict:
                                return {
                                    "item": item, "outcome": SYNC_CONFLICT,
                                    "error": "", "conflict_info": conflict,
                                }
                            effective_operation = "UPDATE"
                            effective_record_id = duplicate_id
                            effective_payload = dict(payload)
                            effective_payload["id"] = duplicate_id
            elif operation == "UPDATE":
                existing = self._mysql.fetch_by_id(table, record_id)
                if existing:
                    conflict = self._check_conflict(table, record_id, existing, payload)
                    if conflict:
                        return {
                            "item": item, "outcome": SYNC_CONFLICT,
                            "error": "", "conflict_info": conflict,
                        }
                elif self._mysql.is_connected():
                    # 保持旧行为：远端记录已不存在时不重建它；仍记录 operation_id。
                    allow_missing = True
                else:
                    effective_operation = "INSERT"
            elif operation != "DELETE":
                raise ValueError("不支持的同步操作: %s" % operation)

            outcome = self._mysql.apply_sync_operation(
                item["operation_id"], table, effective_operation,
                effective_record_id, effective_payload, allow_missing=allow_missing,
            )
            if outcome not in {
                SYNC_APPLIED, SYNC_ALREADY_APPLIED,
                SYNC_RETRYABLE_ERROR, SYNC_FAILED_ERROR,
            }:
                outcome = SYNC_FAILED_ERROR
                error = "MySQL 返回了未知同步结果"
            else:
                error = "" if outcome in {SYNC_APPLIED, SYNC_ALREADY_APPLIED} else outcome
            return {"item": item, "outcome": outcome, "error": error}
        except (MySQLOperationalError, OSError, TimeoutError, ConnectionError) as e:
            _log.warning("同步操作连接暂时失败: %s", e)
            return {"item": item, "outcome": SYNC_RETRYABLE_ERROR, "error": str(e)}
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
            _log.warning("同步操作不可自动处理: %s", e)
            return {"item": item, "outcome": SYNC_FAILED_ERROR, "error": str(e)}
        except Exception as e:
            _log.exception("同步操作未预期异常: %s", e)
            return {"item": item, "outcome": SYNC_FAILED_ERROR, "error": str(e)}

    @staticmethod
    def _get_biz_unique_key(table: str, payload: Dict) -> tuple:
        """获取业务唯一键（字段名, 值），用于 INSERT 前查重

        返回 (key_value, key_column) 或 (None, None)
        """
        biz_keys = {
            TABLE_FIXED_ASSETS: ("asset_no", "asset_no"),
            TABLE_MATERIALS: ("material_code", "material_code"),
            TABLE_CONFIG_ITEMS: ("config_name", "config_name"),
        }
        if table in biz_keys:
            col, alias = biz_keys[table]
            if col in payload and payload[col]:
                return (payload[col], alias)
        return (None, None)

    def _check_conflict(self, table: str, record_id: str,
                        cloud_record: Dict, local_payload: Dict) -> Optional[Dict]:
        """冲突检测：云端版本比本地更新时发生

        仅检测，不写入本地 SQLite（避免跨线程并发写）。
        冲突信息回传主线程后由 Engine 统一处理本地覆盖。
        """
        from utils.helpers import parse_datetime_flexible

        cloud_time = parse_datetime_flexible(cloud_record.get("updated_at") or cloud_record.get("created_at"))
        local_time = parse_datetime_flexible(local_payload.get("updated_at") or local_payload.get("created_at"))
        if cloud_time and local_time and cloud_time > local_time:
            conflict_fields = []
            for key in local_payload:
                if key in cloud_record and cloud_record[key] != local_payload[key]:
                    conflict_fields.append(key)
            if conflict_fields:
                return {
                    "table": table,
                    "record_id": record_id,
                    "cloud": cloud_record,
                    "local": local_payload,
                    "fields": conflict_fields,
                }
        return None


class SyncEngine(QObject):
    """同步引擎 - 主线程控制器

    所有网络 IO 通过 SyncWorker 在后台线程执行，
    本地 SQLite 操作在主线程完成，避免并发写冲突。
    """

    # 对外信号（供 MainWindow 连接，保持不变）
    sync_started = Signal(str)
    sync_completed = Signal(str, int)
    sync_failed = Signal(str, str)
    sync_status_changed = Signal(str)
    batch_conflict_detected = Signal(object)
    queue_updated = Signal(int)

    STATUS_ONLINE = "online"
    STATUS_OFFLINE = "offline"
    STATUS_SYNCING = "syncing"

    # 内部调度信号（Engine → Worker，跨线程）
    _request_health_check = Signal()
    _request_push = Signal(object)
    _request_pull = Signal(str, str, str)
    _request_full_sync = Signal(str, str)

    def __init__(self, mysql_client, local_db, workshop: str = None):
        super().__init__()
        self._main_thread = QThread.currentThread()
        self.mysql_client = mysql_client
        self.local_db = local_db
        self._workshop = workshop or ""
        self._sync_status: str = self.STATUS_OFFLINE
        self._is_running: bool = False
        self._sync_interval_seconds = SYNC_INTERVAL_SECONDS
        self._full_sync_interval_minutes = FULL_SYNC_INTERVAL_MINUTES
        self._sync_batch_size = SYNC_BATCH_SIZE
        self._sync_retry_max = SYNC_RETRY_MAX
        self._timer: Optional[QTimer] = None
        self._full_sync_timer: Optional[QTimer] = None
        self._on_conflict_callback: Optional[Callable] = None
        self._sync_tables = [
            TABLE_MATERIALS, TABLE_BORROW_RECORDS, TABLE_FIXED_ASSETS,
            TABLE_CONFIG_ITEMS, TABLE_USERS, TABLE_EMPLOYEE_RECORDS,
        ]

        # 创建后台工作线程
        self._worker_thread = QThread()
        self._worker = SyncWorker()
        self._worker.moveToThread(self._worker_thread)

        # 连接 Worker 结果信号 → Engine 槽
        self._worker.health_result.connect(self._on_worker_health)
        self._worker.push_result.connect(self._on_worker_push_result)
        self._worker.pull_result.connect(self._on_worker_pull_result)
        self._worker.full_sync_result.connect(self._on_worker_full_sync_result)

        # 连接 Engine 调度信号 → Worker 槽
        self._request_health_check.connect(self._worker.check_health)
        self._request_push.connect(self._worker.push_items)
        self._request_pull.connect(self._worker.pull_table)
        self._request_full_sync.connect(self._worker.full_sync_table)

        self._worker_thread.start()

        # 同步流程控制状态
        self._pull_table_index = 0
        self._pull_total = 0
        self._full_sync_pending: set = set()  # 全量同步中待处理的表集合
        self._full_sync_total = 0
        self._full_sync_failed: list = []
        # 已全量拉取但云端返回空的表集合：避免对云端空表的重复全量拉取
        # 当本地有数据或全量同步触发时清空，重新尝试
        self._empty_pull_tables: set = set()

    def _assert_main_thread(self):
        """架构约束：所有 LocalDB 操作必须在主线程执行。
        SyncWorker（后台线程）只发射信号，由 Engine 的 Slot 在主线程中处理本地数据。
        生产环境无开销（QThread 比较是 O(1)）。
        """
        assert QThread.currentThread() == self._main_thread, \
            f"LocalDB 操作必须在主线程，当前线程: {QThread.currentThread()}"

    @property
    def sync_status(self) -> str:
        return self._sync_status

    def set_conflict_callback(self, callback: Callable):
        self._on_conflict_callback = callback

    def apply_settings(self, sync_interval_seconds: int, full_sync_interval_minutes: int,
                       sync_batch_size: int, sync_retry_max: int):
        """立即应用已保存的同步参数，并更新运行中的计时器。"""
        self._sync_interval_seconds = max(5, int(sync_interval_seconds))
        self._full_sync_interval_minutes = max(5, int(full_sync_interval_minutes))
        self._sync_batch_size = max(1, int(sync_batch_size))
        self._sync_retry_max = max(1, int(sync_retry_max))
        if self._timer and self._timer.isActive():
            self._timer.start(self._sync_interval_seconds * 1000)
        if self._full_sync_timer and self._full_sync_timer.isActive():
            self._full_sync_timer.start(self._full_sync_interval_minutes * 60 * 1000)
    def start(self):
        if self._is_running:
            return
        self._is_running = True
        recovered = self.local_db.recover_in_progress_operations()
        if recovered:
            _log.info("恢复 %d 条中断的同步操作", recovered)
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._sync_cycle)
        self._timer.start(self._sync_interval_seconds * 1000)
        if self._full_sync_timer is None:
            self._full_sync_timer = QTimer(self)
            self._full_sync_timer.timeout.connect(self._full_sync)
        self._full_sync_timer.start(self._full_sync_interval_minutes * 60 * 1000)
        self._sync_cycle()

    def pause_auto_sync(self):
        """暂停自动定时同步（半离线模式使用）
        停止定时器但保持 Worker 线程，force_sync() 仍可手动触发"""
        self._is_running = True
        if self._timer:
            self._timer.stop()
        if self._full_sync_timer:
            self._full_sync_timer.stop()
        self._set_status(self.STATUS_OFFLINE)

    def resume_auto_sync(self):
        """恢复自动定时同步（从半离线切回在线模式使用）"""
        if self._is_running:
            return
        self._is_running = True
        # 确保 Worker 线程仍在运行
        if not self._worker_thread.isRunning():
            self._worker_thread.start()
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._sync_cycle)
        self._timer.start(self._sync_interval_seconds * 1000)
        if self._full_sync_timer is None:
            self._full_sync_timer = QTimer(self)
            self._full_sync_timer.timeout.connect(self._full_sync)
        self._full_sync_timer.start(self._full_sync_interval_minutes * 60 * 1000)
        self._sync_cycle()

    def stop(self):
        """完全停止同步引擎（离线模式使用）"""
        self._is_running = True
        if self._timer:
            self._timer.stop()
            self._timer = None
        if self._full_sync_timer:
            self._full_sync_timer.stop()
            self._full_sync_timer = None
        # 确保 worker 线程中的事件处理完毕后再退出
        if self._worker_thread.isRunning():
            self._worker_thread.quit()
            if not self._worker_thread.wait(5000):
                self._worker_thread.terminate()
                self._worker_thread.wait(1000)

    def _set_status(self, status: str):
        if self._sync_status != status:
            self._sync_status = status
            self.sync_status_changed.emit(status)

    # ---------- 同步周期调度 ----------

    def _sync_cycle(self):
        """触发同步周期：先健康检查 → 推 → 拉"""
        self._request_health_check.emit()

    def refresh_pull(self):
        """刷新专用：只拉取不推送，不依赖定时器"""
        if not self._is_running:
            return
        self._set_status(self.STATUS_SYNCING)
        self._start_pull()

    def _on_worker_health(self, ok: bool):
        """Worker 健康检查完成 — Qt Slot，运行在主线程"""
        self._assert_main_thread()
        if not ok:
            self._set_status(self.STATUS_OFFLINE)
            return
        self._set_status(self.STATUS_SYNCING)
        # 先原子领取待同步队列；只确认当前批次中的 in_progress 项。
        queue = self.local_db.claim_sync_batch(self._sync_batch_size)
        if queue:
            self.sync_started.emit("push")
            self._request_push.emit(queue)
        else:
            # 无推送，直接拉取
            self._start_pull()

    def _start_pull(self):
        """开始拉取流程"""
        self._pull_table_index = 0
        self._pull_total = 0
        self.sync_started.emit("pull")
        self._pull_next_table()

    def _pull_next_table(self):
        """拉取下一张表（智能基准选择：本地为空→全量，否则→取本地最新时间为增量基准）"""
        if self._pull_table_index >= len(self._sync_tables):
            # 拉取完成
            self.local_db.add_sync_log("pull", "success", self._pull_total)
            self.sync_completed.emit("cycle", self._pull_total)
            self._set_status(self.STATUS_ONLINE)
            self.queue_updated.emit(self.local_db.get_sync_queue_count())
            return

        table = self._sync_tables[self._pull_table_index]

        # 智能基准选择
        local_count = self.local_db.get_table_count(table)
        if local_count == 0:
            # 本地为空，但上次全量拉取已确认云端也为空 → 跳过，避免无意义的重复请求
            if table in self._empty_pull_tables:
                _log.debug("表 %s 本地为空，云端上次已确认无数据，跳过拉取", table)
                self._pull_table_index += 1
                self._pull_next_table()
                return
            # 本地为空 → 强制全量拉取（避免 sync_log 有旧记录导致只拉增量）
            _log.info("表 %s 本地为空，强制全量拉取", table)
            self._request_pull.emit(table, "", self._workshop)
        else:
            # 本地有数据 → 从空表集合中移除（可能之前云端为空，现在有数据了）
            self._empty_pull_tables.discard(table)
            # 取两个时间中较早的一个作为基准，确保不漏数据：
            # - 本地表最新 updated_at（数据层面的基准，更准确）
            # - sync_log 上次成功拉取时间（流程层面的基准）
            local_latest = self.local_db.get_table_max_updated_at(table)
            sync_time = self.local_db.get_last_sync_time("pull")
            # 取较早的时间作为基准（宁可多拉，不可漏拉）
            if local_latest and sync_time:
                last_sync = local_latest if local_latest < sync_time else sync_time
            else:
                last_sync = local_latest or sync_time or ""
            _log.debug("表 %s 增量拉取基准: local=%s sync=%s 使用=%s",
                       table, local_latest, sync_time, last_sync)
            self._request_pull.emit(table, last_sync, self._workshop)

    def _on_worker_push_result(self, results: list, total: int, error: str):
        """确认 MySQL 幂等推送的结果，并调度下一阶段拉取。"""
        self._assert_main_thread()
        if not isinstance(results, list):
            _log.warning("push_result 收到非 list results: %r", type(results))
            results = []
        summary = confirm_sync_results(self.local_db, results)
        if error:
            self.sync_failed.emit("push", error)
            self.local_db.add_sync_log("push", "failed", 0, error)
        elif summary["failed"] or summary["retryable"]:
            detail = "失败 %d 条，可重试 %d 条" % (
                summary["failed"], summary["retryable"],
            )
            self.local_db.add_sync_log("push", "partial", summary["succeeded"], detail)
        elif summary["succeeded"]:
            self.local_db.add_sync_log("push", "success", summary["succeeded"])

        # 三路合并处理冲突：无基线或同字段冲突时保持云端优先。
        conflict_tables = set()
        for conflict in summary["conflicts"]:
            table = conflict["table"]
            record_id = conflict["record_id"]
            cloud_record = conflict["cloud"]
            local_payload = conflict["local"]
            base = self.local_db.get_snapshot(table, record_id)
            if base:
                merge_result = self._three_way_merge(base, local_payload, cloud_record)
                if merge_result["action"] == "merge":
                    merged = self._convert_record(merge_result["merged"])
                    self.local_db.update(table, record_id, merged)
                    self.local_db.save_snapshot(table, record_id, merged)
                    _log.info("三路合并自动合并: %s/%s", table, record_id)
                    continue
                if merge_result["action"] == "noop":
                    self.local_db.save_snapshot(
                        table, record_id, self._convert_record(cloud_record)
                    )
                    continue
                _log.info("检测到真正冲突，云端版本优先: %s/%s 冲突字段: %s",
                          table, record_id, merge_result.get("fields", []))

            conflict_tables.add(table)
            self.local_db.update(table, record_id, self._convert_record(cloud_record))
            self.local_db.save_snapshot(table, record_id, self._convert_record(cloud_record))

        if conflict_tables:
            self.batch_conflict_detected.emit({"tables": list(conflict_tables)})

        self.queue_updated.emit(self.local_db.get_sync_queue_count())
        self._start_pull()

    def _on_worker_pull_result(self, table: str, records: list, error: str):
        """Worker 单表拉取完成回调 — Qt Slot，运行在主线程"""
        self._assert_main_thread()
        # 防御：确保 records 为 list，避免 None 等异常值触发 Shiboken NoneType 转换
        if records is None:
            records = []
        elif not isinstance(records, list):
            _log.warning("pull_result 收到非 list records: %r", type(records))
            records = []

        if error:
            self.sync_failed.emit("pull", f"{table}: {error}")
            self.local_db.add_sync_log("pull", "failed", 0, f"{table}: {error}")
        elif records:
            converted = [self._convert_record(r) for r in records]
            self.local_db.batch_insert(table, converted)
            self.local_db.save_snapshots_batch(table, converted)
            self._pull_total += len(records)
        else:
            # 云端返回空列表：记录到空表集合，避免下次同步重复全量拉取
            local_count = self.local_db.get_table_count(table)
            if local_count == 0:
                self._empty_pull_tables.add(table)
                _log.info("表 %s 云端无数据，本地仍为空，下次同步将跳过", table)

        self._pull_table_index += 1
        self._pull_next_table()

    # ---------- 全量同步 ----------

    def _full_sync(self):
        """全量同步：逐表拉取并替换本地数据"""
        self._full_sync_pending = set(self._sync_tables)  # 跟踪未完成的表
        self._full_sync_total = 0
        self._full_sync_failed = []
        self._empty_pull_tables.clear()  # 清空空表记录，全量同步时重新尝试所有表
        self.sync_started.emit("full_sync")
        for table in self._sync_tables:
            self._request_full_sync.emit(table, self._workshop)

    def _on_worker_full_sync_result(self, table: str, records: list, error: str):
        """Worker 全量同步单表完成回调 — Qt Slot，运行在主线程"""
        self._assert_main_thread()
        # 防御：确保 records 为 list，避免 None 等异常值
        if records is None:
            records = []
        elif not isinstance(records, list):
            _log.warning("full_sync_result 收到非 list records: %r", type(records))
            records = []

        if error:
            _log.error("全量同步表 %s 失败，保留旧数据: %s", table, error)
            self._full_sync_failed.append(table)
        elif records:
            converted = [self._convert_record(r) for r in records]
            try:
                self.local_db.replace_table_transactional(table, converted)
                self.local_db.save_snapshots_batch(table, converted)
                self._full_sync_total += len(records)
            except (RuntimeError, sqlite3.Error, OSError, ValueError) as e:
                _log.error("全量同步表 %s 本地替换失败: %s", table, e)
                self._full_sync_failed.append(table)
            except Exception as e:
                _log.error("全量同步表 %s 本地替换未预期异常: %s", table, e)
                self._full_sync_failed.append(table)

        self._full_sync_pending.discard(table)  # 标记该表已完成
        if not self._full_sync_pending:  # 所有表都有了响应
            # 全部完成
            if self._full_sync_failed:
                self.local_db.add_sync_log(
                    "pull", "full_sync_partial", self._full_sync_total,
                    f"部分表失败: {','.join(self._full_sync_failed)}"
                )
            else:
                self.local_db.add_sync_log(
                    "pull", "full_sync", self._full_sync_total, "全量同步完成"
                )
            self.sync_completed.emit("full_sync", self._full_sync_total)

    # ---------- 本地操作（主线程，快速）----------

    def offline_insert(self, table: str, record_id: str, data: Dict,
                         add_to_queue: bool = True):
        self.local_db.insert(table, self._convert_record(data))
        if add_to_queue:
            self.local_db.add_sync_queue(table, "INSERT", record_id, data)
            self.queue_updated.emit(self.local_db.get_sync_queue_count())

    def offline_update(self, table: str, record_id: str, data: Dict,
                       add_to_queue: bool = True):
        self.local_db.update(table, record_id, self._convert_record(data))
        if add_to_queue:
            self.local_db.add_sync_queue(table, "UPDATE", record_id, data)
            self.queue_updated.emit(self.local_db.get_sync_queue_count())

    def offline_delete(self, table: str, record_id: str,
                       add_to_queue: bool = True):
        self.local_db.delete(table, record_id)
        if add_to_queue:
            self.local_db.add_sync_queue(table, "DELETE", record_id, {})
            self.queue_updated.emit(self.local_db.get_sync_queue_count())

    @staticmethod
    def _convert_record(record: Dict) -> Dict:
        """转换记录类型，保持数值语义；确保中文编码正确"""
        converted = {}
        for key, value in record.items():
            if value is None:
                converted[key] = None
            elif isinstance(value, bool):
                converted[key] = 1 if value else 0
            elif isinstance(value, (int, float)):
                converted[key] = value
            elif isinstance(value, bytes):
                # 处理 pymysql 可能返回的 bytes 类型（如编码异常时）
                try:
                    converted[key] = value.decode("utf-8")
                except UnicodeDecodeError:
                    converted[key] = value.decode("utf-8", errors="replace")
            else:
                converted[key] = str(value)
        return converted

    @staticmethod
    def _three_way_merge(base: Dict, local: Dict, cloud: Dict) -> Dict:
        """三路合并：比较 local/cloud 分别相对于 base 的变化

        Returns:
            {"action": "merge", "merged": {...}}  — 自动合并，无冲突
            {"action": "conflict", "fields": [...]} — 真正冲突（同一字段两边都变了）
            {"action": "noop"}                     — 三方一致，无需操作
        """
        _SKIP_KEYS = {"id", "created_at", "updated_at"}

        local_changes = {}
        for key, value in local.items():
            if key in _SKIP_KEYS:
                continue
            if key in base and value != base[key]:
                local_changes[key] = value
            elif key not in base and value is not None:
                local_changes[key] = value

        cloud_changes = {}
        for key, value in cloud.items():
            if key in _SKIP_KEYS:
                continue
            if key in base and value != base[key]:
                cloud_changes[key] = value
            elif key not in base and value is not None:
                cloud_changes[key] = value

        conflict_fields = set(local_changes.keys()) & set(cloud_changes.keys())
        if conflict_fields:
            return {"action": "conflict", "fields": list(conflict_fields)}

        if not local_changes and not cloud_changes:
            return {"action": "noop"}

        # 自动合并：基线 + 本地变更 + 云端变更（无交集，可安全合并）
        merged = dict(base)
        merged.update(local_changes)
        merged.update(cloud_changes)
        merged["updated_at"] = datetime.now().isoformat()
        return {"action": "merge", "merged": merged}

    def force_sync(self):
        if not self._is_running:
            return
        self._sync_cycle()
