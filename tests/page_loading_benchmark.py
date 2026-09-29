"""5,000 条页面数据快照的可重复基准。"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))

from config import TABLE_EMPLOYEE_RECORDS, TABLE_FIXED_ASSETS, TABLE_MATERIALS, TABLE_USERS
from local_db import LocalDB
from page_data_loader import PageLoadState, load_page_snapshot

COUNT = 5_000
ITERATIONS = 10
WORKSHOP = "压测车间"


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def percentile(values: list[int], p: int) -> float:
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * p / 100)] / 1_000_000


def seed(root: Path) -> tuple[str, str]:
    db_path = root / "local_cache_benchmark.db"
    db = LocalDB(str(db_path), workshop=WORKSHOP)
    shared_path = Path(db._shared_db_path)
    db.close_all()
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            f"INSERT INTO {q(TABLE_MATERIALS)} (id, workshop, material_code, material_name, stock_qty, last_update) VALUES (?, ?, ?, ?, ?, ?)",
            [(f"m{i}", WORKSHOP, f"M{i:05d}", f"物料{i}", i % 100, "2026-09-29") for i in range(COUNT)],
        )
        conn.executemany(
            f"INSERT INTO {q(TABLE_FIXED_ASSETS)} (id, workshop, asset_no, asset_name, status, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(f"a{i}", WORKSHOP, f"A{i:05d}", f"资产{i}", "在用", "2026-09-29") for i in range(COUNT)],
        )
    with sqlite3.connect(shared_path) as conn:
        conn.executemany(
            f"INSERT INTO {q(TABLE_USERS)} (id, username, password, display_name, role, is_active) VALUES (?, ?, ?, ?, ?, ?)",
            [(f"u{i}", f"u{i:05d}", "hash", f"用户{i}", "user", 1) for i in range(COUNT)],
        )
        conn.executemany(
            f"INSERT INTO {q(TABLE_EMPLOYEE_RECORDS)} (id, employee_no, name, is_active) VALUES (?, ?, ?, ?)",
            [(f"e{i}", f"E{i:05d}", f"员工{i}", 1) for i in range(COUNT)],
        )
    return str(db_path), str(shared_path)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mms-page-benchmark-") as temp:
        db_path, shared_path = seed(Path(temp))
        pages = ("inventory", "asset", "user_manage")
        first_load: dict[str, list[int]] = {page: [] for page in pages}
        for page in pages:
            for _ in range(ITERATIONS):
                start = time.perf_counter_ns()
                snapshot = load_page_snapshot(page, db_path, shared_path, WORKSHOP)
                first_load[page].append(time.perf_counter_ns() - start)
                assert snapshot
        state = PageLoadState()
        cache_hit: dict[str, list[int]] = {page: [] for page in pages}
        for page in pages:
            request_id = state.begin_request(page)
            payload = load_page_snapshot(page, db_path, shared_path, WORKSHOP)
            from page_data_loader import PageLoadResult
            assert state.accept(PageLoadResult(page, request_id, payload, None))
            for _ in range(ITERATIONS):
                start = time.perf_counter_ns()
                assert state.get_cached(page) is not None
                cache_hit[page].append(time.perf_counter_ns() - start)
        output = {
            "python": sys.version.split()[0], "records_per_type": COUNT, "iterations": ITERATIONS,
            "first_load_ms": {p: {"p50": percentile(v, 50), "p95": percentile(v, 95)} for p, v in first_load.items()},
            "cache_hit_ms": {p: {"p50": percentile(v, 50), "p95": percentile(v, 95)} for p, v in cache_hit.items()},
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
