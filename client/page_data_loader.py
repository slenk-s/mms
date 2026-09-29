"""非阻塞页面加载所需的只读 SQLite 快照读取器。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from PySide6.QtCore import QThread, Signal

from config import (
    TABLE_BORROW_RECORDS,
    TABLE_CONFIG_ITEMS,
    TABLE_EMPLOYEE_RECORDS,
    TABLE_FIXED_ASSETS,
    TABLE_MATERIALS,
    TABLE_USERS,
)


PAGE_IDS = frozenset({"inventory", "register", "asset", "config", "user_manage"})


@dataclass(frozen=True)
class PageLoadResult:
    """单次页面加载的不可变结果元数据。"""

    page_id: str
    request_id: int
    payload: dict[str, list[dict]] | None
    error: str | None


class PageLoadState:
    """主线程页面请求号与快照缓存状态，不持有任何 Qt Widget。"""

    def __init__(self):
        self._next_request_id = 0
        self._latest_request_ids: dict[str, int] = {}
        self._cache: dict[str, dict[str, list[dict]]] = {}

    def begin_request(self, page_id: str) -> int:
        if page_id not in PAGE_IDS:
            raise ValueError(f"Unsupported page: {page_id}")
        self._next_request_id += 1
        self._latest_request_ids[page_id] = self._next_request_id
        return self._next_request_id

    def accept(self, result: PageLoadResult) -> bool:
        if result.error is not None or result.payload is None:
            return False
        if self._latest_request_ids.get(result.page_id) != result.request_id:
            return False
        self._cache[result.page_id] = result.payload
        return True

    def get_cached(self, page_id: str) -> dict[str, list[dict]] | None:
        return self._cache.get(page_id)

    def invalidate(self, *page_ids: str) -> None:
        targets = page_ids or tuple(PAGE_IDS)
        for page_id in targets:
            if page_id not in PAGE_IDS:
                raise ValueError(f"Unsupported page: {page_id}")
            self._next_request_id += 1
            self._latest_request_ids[page_id] = self._next_request_id
            self._cache.pop(page_id, None)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@contextmanager
def open_readonly_connection(db_path: str) -> Iterator[sqlite3.Connection]:
    """在当前线程创建、配置并关闭一个独立的 SQLite 只读连接。"""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        connection.row_factory = sqlite3.Row
        connection.text_factory = str
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


def _fetch_all(connection: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def load_page_snapshot(
    page_id: str,
    workshop_db_path: str,
    shared_db_path: str,
    workshop: str,
) -> dict[str, list[dict]]:
    """读取一个页面所需的本地数据快照，不调用或共享 LocalDB。"""
    if page_id not in PAGE_IDS:
        raise ValueError(f"Unsupported page: {page_id}")

    if page_id == "inventory":
        with open_readonly_connection(workshop_db_path) as connection:
            return {
                "materials": _fetch_all(
                    connection,
                    f"SELECT * FROM {_quote(TABLE_MATERIALS)} "
                    "WHERE workshop = ? ORDER BY last_update DESC",
                    (workshop,),
                )
            }

    if page_id == "register":
        with open_readonly_connection(workshop_db_path) as connection:
            return {
                "materials": _fetch_all(
                    connection,
                    f"SELECT * FROM {_quote(TABLE_MATERIALS)} WHERE workshop = ?",
                    (workshop,),
                ),
                "borrow_records": _fetch_all(
                    connection,
                    f"SELECT * FROM {_quote(TABLE_BORROW_RECORDS)} "
                    "WHERE workshop = ? AND (is_archived IS NULL OR is_archived = 0) "
                    "ORDER BY created_at DESC",
                    (workshop,),
                ),
            }

    if page_id == "asset":
        with open_readonly_connection(workshop_db_path) as connection:
            return {
                "assets": _fetch_all(
                    connection,
                    f"SELECT * FROM {_quote(TABLE_FIXED_ASSETS)} "
                    "WHERE workshop = ? ORDER BY updated_at DESC",
                    (workshop,),
                )
            }

    if page_id == "config":
        with open_readonly_connection(workshop_db_path) as connection:
            return {
                "config_items": _fetch_all(
                    connection,
                    f"SELECT * FROM {_quote(TABLE_CONFIG_ITEMS)} ORDER BY sort_order",
                )
            }

    with open_readonly_connection(shared_db_path) as connection:
        return {
            "users": _fetch_all(
                connection,
                f"SELECT * FROM {_quote(TABLE_USERS)} ORDER BY username",
            ),
            "employees": _fetch_all(
                connection,
                f"SELECT * FROM {_quote(TABLE_EMPLOYEE_RECORDS)} ORDER BY employee_no",
            ),
        }


class PageDataLoadWorker(QThread):
    """在自身线程中读取页面快照，绝不持有 LocalDB 或 QWidget。"""

    loaded = Signal(object)

    def __init__(
        self,
        page_id: str,
        request_id: int,
        workshop_db_path: str,
        shared_db_path: str,
        workshop: str,
        parent=None,
    ):
        super().__init__(parent)
        self._page_id = page_id
        self._request_id = request_id
        self._workshop_db_path = workshop_db_path
        self._shared_db_path = shared_db_path
        self._workshop = workshop

    def run(self) -> None:
        try:
            payload = load_page_snapshot(
                self._page_id,
                self._workshop_db_path,
                self._shared_db_path,
                self._workshop,
            )
        except (OSError, sqlite3.Error, ValueError) as error:
            self.loaded.emit(PageLoadResult(self._page_id, self._request_id, None, str(error)))
            return
        except Exception as error:
            self.loaded.emit(PageLoadResult(self._page_id, self._request_id, None, str(error)))
            return

        self.loaded.emit(PageLoadResult(self._page_id, self._request_id, payload, None))
