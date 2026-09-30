"""
本地 SQLite 数据库操作模块
负责本地缓存的增删改查，以及 sync_queue / sync_log 管理

v4.1 性能优化版：
- 增加数据库层面模糊搜索、分页查询、计数
- 增加更多索引加速查询
- 所有网络 IO 相关代码保持不变
- v4.2 新增: users 表及用户管理方法
- v19 新增: borrow_records 归还相关字段（return_person, return_qty, good_qty,
  damage_status, mixed_qty, mixed_remark）
"""
import sqlite3
import json
import os
import re
import threading
import uuid
from datetime import datetime
from typing import List, Dict, Optional, Any, Tuple, Set
from contextlib import contextmanager

from config import (
    DEFAULT_WORKSHOP, get_local_db_path,
    TABLE_MATERIALS, TABLE_BORROW_RECORDS, TABLE_FIXED_ASSETS,
    TABLE_CONFIG_ITEMS, TABLE_USERS, TABLE_EMPLOYEE_RECORDS,
)
from logger import get_logger

_log = get_logger(__name__)

ALLOWED_TABLES = frozenset({
    TABLE_MATERIALS, TABLE_BORROW_RECORDS, TABLE_FIXED_ASSETS,
    TABLE_CONFIG_ITEMS, TABLE_USERS, TABLE_EMPLOYEE_RECORDS,
    "model_power_map", "sync_queue", "sync_log", "sync_snapshots",
})

WORKSHOP_TABLES = frozenset({TABLE_MATERIALS, TABLE_BORROW_RECORDS, TABLE_FIXED_ASSETS})
# 跨车间共享表：用户台账、员工台账存储在独立公共数据库中，不随车间切换而隔离
SHARED_TABLES = frozenset({TABLE_USERS, TABLE_EMPLOYEE_RECORDS})

# 默认管理员 ID 常量（集中管理，避免硬编码分散）
DEFAULT_ADMIN_ID = "admin_default_001"


def _q(table: str) -> str:
    """为 SQLite 表名添加双引号（含中文时必需）"""
    return f'"{table}"'


def _add_workshop_filter(table: str, conditions: Optional[str], params: tuple, workshop: str) -> Tuple[str, tuple]:
    """对 WORKSHOP_TABLES 自动追加 workshop 过滤条件"""
    if table not in WORKSHOP_TABLES or not workshop:
        return conditions or "", params
    ws_cond = f"workshop = ?"
    ws_params = (workshop,) + tuple(params)
    if conditions:
        return f"{conditions} AND {ws_cond}", ws_params
    return ws_cond, ws_params


def _validate_table(table: str):
    if table not in ALLOWED_TABLES:
        raise ValueError(f"不安全的表名: {table}")

# 安全的列名正则：只允许字母、数字、下划线
_COLUMN_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')

def _validate_order_by(order_by: str):
    """验证 ORDER BY 子句，只允许安全的列名和 ASC/DESC"""
    if not order_by or not isinstance(order_by, str):
        return
    # 拆分为单个排序项，逐项验证
    for item in order_by.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split()
        if len(parts) > 2:
            raise ValueError(f"不安全的 ORDER BY: {order_by}")
        # 验证列名
        if not _COLUMN_RE.match(parts[0]):
            raise ValueError(f"不安全的列名: {parts[0]}")
        # 验证排序方向
        if len(parts) == 2 and parts[1].upper() not in ("ASC", "DESC"):
            raise ValueError(f"不安全的排序方向: {parts[1]}")

def _validate_columns(columns: List[str], table: str):
    """验证列名列表，每项必须是安全的列名"""
    for col in columns:
        if not _COLUMN_RE.match(col):
            raise ValueError(f"不安全的列名 '{col}' 在表 {table} 中")

# ==================== WHERE 条件安全验证 ====================
#
# 安全策略（双重防护）：
# 1. 黑名单前置：拒绝危险关键字（SELECT/FROM/JOIN 等）和危险字符（; -- # /*）
# 2. 逐 Token 白名单：将条件字符串按运算符和空白切分，每个 token 必须属于白名单
#    这样即使黑名单遗漏了某个关键字，白名单也会拦截（因为无法形成完整合法 token）
#
# 所有实际使用的 conditions 均为代码内部硬编码（如 "is_archived = 0"），
# 不包含用户输入，此处校验主要防范未来误改。

# 危险关键字（DML / DDL / DCL / 子查询 / 危险操作）
_DANGEROUS_KEYWORDS = re.compile(
    r'\b(DROP|UNION|INSERT|DELETE|UPDATE|CREATE|ALTER|ATTACH|DETACH|REINDEX|'
    r'REPLACE|TRUNCATE|EXEC|EXECUTE|IMPORT|LOAD|PRAGMA|'
    r'SELECT|FROM|WHERE|JOIN|INNER|LEFT|RIGHT|FULL|CROSS|'
    r'GROUP|HAVING|LIMIT|OFFSET|ORDER|VALUES|SET|CALL|SHOW|DESC)\b',
    re.IGNORECASE
)

# 危险字符：SQL 注释符、语句分隔符
_DANGEROUS_CHARS = re.compile(r'(--|#|/\*|;|\\x00)')

# 合法 Token 白名单正则：列名、运算符、占位符、数字、SQL 逻辑/比较关键字、常用函数、括号逗号
_TOKEN_RE = re.compile(
    r'^(?:'
    r'[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)?'     # 列名（含可选表前缀）
    r'|<=|>=|!=|<>|<|>|='                                        # 运算符
    r'|\?'                                                        # 参数占位符
    r'|\d+(?:\.\d+)?'                                            # 数字
    r'|AND|OR|NOT|IN|LIKE|IS|NULL|BETWEEN'                       # SQL 逻辑/比较关键字
    r'|CAST|COALESCE|IFNULL|ABS|ROUND|LENGTH|SUBSTR|TRIM|UPPER|LOWER'  # 常用函数
    r')\Z',
    re.IGNORECASE
)


def _tokenize_conditions(conditions: str) -> list:
    """将 WHERE 条件字符串按运算符和空白切分为 token 列表。
    运算符被拆成独立 token（如 "a<1" → ["a", "<", "1"]）。
    空白和括号逗号也被视为独立 token。
    """
    result = []
    remaining = conditions
    while remaining:
        m = re.search(r'(<=|>=|!=|<>|<|>|=|\s+|[(),])', remaining)
        if not m:
            result.append(remaining.strip())
            break
        start, end = m.span()
        prefix = remaining[:start].strip()
        sep = remaining[start:end]
        if prefix:
            result.append(prefix)
        if sep.strip():
            result.append(sep)
        remaining = remaining[end:]
    return result


def _validate_conditions(conditions: str):
    """验证 WHERE 条件片段，只允许安全的 SQL 表达式。
    三重防护：特殊字符 → 危险关键字 → 逐 token 白名单。
    空字符串/非字符串直接通过（上游 SQL 拼接前会做 None 判断）。
    分隔符 token（空白、括号、逗号）跳过验证，只验证实义 token。
    """
    if not conditions or not isinstance(conditions, str):
        return
    # 1. 黑名单特殊字符
    if _DANGEROUS_CHARS.search(conditions):
        raise ValueError(f"不安全的 SQL 条件（包含危险字符）: {conditions[:100]}")
    # 2. 黑名单危险关键字
    if _DANGEROUS_KEYWORDS.search(conditions):
        raise ValueError(f"不安全的 SQL 条件（包含危险关键字）: {conditions[:100]}")
    # 3. 逐 token 白名单（跳过分隔符：空白、括号、逗号）
    for token in _tokenize_conditions(conditions):
        if not token:
            continue
        if token in ("(", ")", ",", "", " ", "\t", "\n"):
            continue
        if not _TOKEN_RE.match(token):
            raise ValueError(f"不安全的 SQL 条件（非法 token）: '{token}' in {conditions[:100]}")


def _validate_numeric(value: Any, field_name: str, max_value: int = 100000,
                      message: Optional[str] = None) -> int:
    """强制将值转为 int，校验范围；超出范围或非数字时抛出异常。
    用于限制 LIMIT / OFFSET / page_size 等 SQL 数值参数，防止注入。

    Args:
        value: 待校验的数值
        field_name: 字段名（用于错误信息）
        max_value: 允许的最大值（默认 100000）
        message: 自定义错误信息（可选，仅作为上下文参考，不影响拦截逻辑）
    """
    if value is None:
        raise ValueError(f"{field_name} 不能为空")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} 必须是整数: {value}")
    if n < 0:
        raise ValueError(f"{field_name} 不能为负数: {n}")
    if n > max_value:
        raise ValueError(f"{field_name} 超出最大值 {max_value}: {n}")
    return n


def _resolve_default_db_path() -> str:
    try:
        return get_local_db_path()
    except Exception as e:
        _log.error("解析本地数据库路径失败: %s", e)
        return get_local_db_path(DEFAULT_WORKSHOP)


class LocalDB:
    """本地 SQLite 数据库管理器

    架构约束：仅主线程调用
    ───────────────────────────────
    - SyncWorker（后台 QThread）只执行 MySQL 网络 IO，绝不持有或调用 LocalDB
    - SyncEngine（主线程 QObject）接收 Worker 信号后，在主线程回调中调用 LocalDB
    - 所有 SQLite 读写（insert/update/delete/query）必须在主线程中执行
    - WAL 模式下 SQLite 允许多读并发，但写串行；主线程独占写避免 database is locked

    违反此约束：SyncWorker 不应对 LocalDB 实例做任何操作，所有本地数据操作
    通过信号回传到主线程完成。
    """

    # 连接池最大容量，防止线程泄漏导致连接无限增长
    _MAX_POOL_SIZE = 8

    def __init__(self, db_path: str = None, workshop: str = None):
        if db_path is None:
            db_path = _resolve_default_db_path()
        self.db_path = db_path
        self._workshop = workshop or DEFAULT_WORKSHOP
        self._conn_pool: Dict[int, sqlite3.Connection] = {}
        self._pool_lock = threading.Lock()
        self._init_db()
        self._shared_db_path = os.path.join(os.path.dirname(db_path), "local_cache_common.db")
        self._shared_conn_pool: Dict[int, sqlite3.Connection] = {}
        self._shared_init_db()
        self._migrate_shared_from_workshop_db()
        self.recover_in_progress_operations()

    def _shared_init_db(self):
        conn = sqlite3.connect(self._shared_db_path, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.text_factory = str
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            cursor = conn.cursor()
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {_q(TABLE_USERS)} (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password TEXT NOT NULL,
                    display_name TEXT DEFAULT '',
                    role TEXT DEFAULT 'user' CHECK(role IN ('admin', 'user')),
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_users_username ON {_q(TABLE_USERS)}(username)")
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {_q(TABLE_EMPLOYEE_RECORDS)} (
                    id TEXT PRIMARY KEY,
                    employee_no TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    dept TEXT DEFAULT '',
                    phone TEXT DEFAULT '',
                    fingerprint_id TEXT DEFAULT '',
                    card_no TEXT DEFAULT '',
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_employee_card ON {_q(TABLE_EMPLOYEE_RECORDS)}(card_no)")
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_employee_fingerprint ON {_q(TABLE_EMPLOYEE_RECORDS)}(fingerprint_id)")
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_employee_no ON {_q(TABLE_EMPLOYEE_RECORDS)}(employee_no)")
            self._create_sync_queue(cursor, "shared_sync_queue", "idx_shared_sync_queue")
            conn.commit()
        finally:
            conn.close()

    def _migrate_shared_from_workshop_db(self):
        """迁移旧车间库中的共享表和共享表 outbox，复制成功后才删除旧队列项。"""
        try:
            src = sqlite3.connect(self.db_path, timeout=5.0)
            src.row_factory = sqlite3.Row
            src.text_factory = str
            dst = sqlite3.connect(self._shared_db_path, timeout=5.0)
            dst.row_factory = sqlite3.Row
            dst.text_factory = str
            try:
                for table in SHARED_TABLES:
                    row = src.execute(
                        f"SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    ).fetchone()
                    if row is None:
                        continue
                    rows = src.execute(f"SELECT * FROM {_q(table)}").fetchall()
                    if not rows:
                        continue
                    keys = list(rows[0].keys())
                    columns = ", ".join(keys)
                    qmarks = ", ".join(["?"] * len(keys))
                    for record in rows:
                        dst.execute(
                            f"INSERT OR IGNORE INTO {_q(table)} ({columns}) VALUES ({qmarks})",
                            tuple(record[key] for key in keys),
                        )
                    _log.info("从车间库迁移 %s %d 条记录到公共库", table, len(rows))

                legacy_items = src.execute(
                    "SELECT * FROM sync_queue WHERE table_name IN (?, ?)", tuple(SHARED_TABLES)
                ).fetchall()
                for item in legacy_items:
                    columns = list(item.keys())
                    dst.execute(
                        f"INSERT OR IGNORE INTO shared_sync_queue ({', '.join(columns)}) "
                        f"VALUES ({', '.join('?' for _ in columns)})",
                        tuple(item[column] for column in columns),
                    )
                # 先提交公共库副本，再清理旧项；清理失败最多是可去重副本，绝不丢失操作。
                dst.commit()
                if legacy_items:
                    src.executemany(
                        "DELETE FROM sync_queue WHERE operation_id = ?",
                        [(item["operation_id"],) for item in legacy_items],
                    )
                    src.commit()
            finally:
                src.close()
                dst.close()
        except Exception as e:
            _log.warning("公共库数据迁移失败: %s", e)

    @contextmanager
    def _shared_connect(self):
        conn = self._shared_get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _shared_get_conn(self) -> sqlite3.Connection:
        tid = threading.get_ident()
        with self._pool_lock:
            if tid in self._shared_conn_pool:
                conn = self._shared_conn_pool.pop(tid)
                self._shared_conn_pool[tid] = conn
                return conn
        conn = sqlite3.connect(self._shared_db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.text_factory = str
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        with self._pool_lock:
            self._shared_conn_pool[tid] = conn
        return conn

    @contextmanager
    def _connect(self):
        """获取数据库连接（优先复用连接池连接，提升性能）"""
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _get_conn(self) -> sqlite3.Connection:
        """获取或创建连接（线程安全，支持连接复用，带 LRU 淘汰）"""
        tid = threading.get_ident()
        with self._pool_lock:
            if tid in self._conn_pool:
                # 命中缓存：移到最新（保持 dict 插入顺序）
                conn = self._conn_pool.pop(tid)
                self._conn_pool[tid] = conn
                return conn

            # 连接池上限保护：LRU 淘汰最旧连接
            while len(self._conn_pool) >= self._MAX_POOL_SIZE:
                oldest_tid = next(iter(self._conn_pool))
                try:
                    self._conn_pool[oldest_tid].close()
                except (sqlite3.Error, OSError, RuntimeError) as e:
                    _log.debug("关闭连接池旧连接异常（已忽略）: %s", e)
                except Exception as e:
                    _log.debug("关闭连接池旧连接未预期异常（已忽略）: %s", e)
                del self._conn_pool[oldest_tid]

        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.text_factory = str
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        self._conn_pool[tid] = conn
        return conn

    def close_all(self):
        """关闭所有连接（程序退出时调用）"""
        for conn in self._conn_pool.values():
            try:
                conn.close()
            except (sqlite3.Error, OSError, RuntimeError) as e:
                _log.debug("关闭数据库连接异常（已忽略）: %s", e)
            except Exception as e:
                _log.debug("关闭数据库连接未预期异常（已忽略）: %s", e)
        self._conn_pool.clear()
        for conn in self._shared_conn_pool.values():
            try:
                conn.close()
            except Exception:
                pass
        self._shared_conn_pool.clear()

    def _init_db(self):
        with self._connect() as conn:
            cursor = conn.cursor()

            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {_q(TABLE_MATERIALS)} (
                    id TEXT PRIMARY KEY,
                    workshop TEXT DEFAULT '{DEFAULT_WORKSHOP}',
                    location TEXT DEFAULT '',
                    shelf_no TEXT DEFAULT '',
                    material_code TEXT NOT NULL UNIQUE,
                    material_name TEXT NOT NULL,
                    stock_qty INTEGER DEFAULT 0,
                    reserved_qty INTEGER DEFAULT 0,
                    unit TEXT DEFAULT 'PCS',
                    real_image TEXT,
                    last_update TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)

            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {_q(TABLE_BORROW_RECORDS)} (
                    id TEXT PRIMARY KEY,
                    workshop TEXT DEFAULT '{DEFAULT_WORKSHOP}',
                    record_no TEXT NOT NULL UNIQUE,
                    material_id TEXT NOT NULL,
                    material_code TEXT NOT NULL,
                    material_name TEXT NOT NULL,
                    qty INTEGER DEFAULT 1,
                    card_no TEXT NOT NULL,
                    dept TEXT,
                    user_name TEXT,
                    phone TEXT,
                    action_type TEXT DEFAULT '领用',
                    out_time TEXT,
                    operator TEXT,
                    in_time TEXT,
                    confirm_person TEXT,
                    return_person TEXT,
                    return_qty INTEGER DEFAULT 0,
                    good_qty INTEGER DEFAULT 0,
                    damage_qty INTEGER DEFAULT 0,
                    damage_status TEXT DEFAULT '',
                    mixed_qty INTEGER DEFAULT 0,
                    mixed_remark TEXT DEFAULT '',
                    is_returned INTEGER DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)

            # ---------- v4.3 表名迁移：旧版英文表名 → 新版中文表名 ----------
            self._migrate_table_rename(cursor)

            # ---------- v19 兼容迁移：已有数据库添加新字段 ----------
            self._migrate_borrow_records_v19(cursor)
            # v25: 添加 is_archived 字段，支持软删除归档
            self._migrate_borrow_records_archived(cursor)
            # v4306: 添加 workshop 字段，支持分车间管理
            self._migrate_workshop_column(cursor)

            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {_q(TABLE_FIXED_ASSETS)} (
                    id TEXT PRIMARY KEY,
                    workshop TEXT DEFAULT '{DEFAULT_WORKSHOP}',
                    asset_no TEXT NOT NULL UNIQUE,
                    asset_name TEXT NOT NULL,
                    category TEXT,
                    purchase_date TEXT,
                    status TEXT DEFAULT '在用',
                    location TEXT,
                    location_image TEXT,
                    value REAL,
                    remark TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)

            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {_q(TABLE_CONFIG_ITEMS)} (
                    id TEXT PRIMARY KEY,
                    config_name TEXT NOT NULL UNIQUE,
                    item_type TEXT,
                    content TEXT,
                    sort_order INTEGER DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS model_power_map (
                    id TEXT PRIMARY KEY,
                    model TEXT,
                    power_code TEXT,
                    power_name TEXT,
                    spec TEXT,
                    v0_code TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_mpm_model ON model_power_map(model)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_mpm_code ON model_power_map(power_code)")

            # ========== 用户台账 (v4.2 新增) ==========
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {_q(TABLE_USERS)} (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password TEXT NOT NULL,
                    display_name TEXT DEFAULT '',
                    role TEXT DEFAULT 'user' CHECK(role IN ('admin', 'user')),
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_users_username ON {_q(TABLE_USERS)}(username)")

            # ========== 员工台账表 ==========
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {_q(TABLE_EMPLOYEE_RECORDS)} (
                    id TEXT PRIMARY KEY,
                    employee_no TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    dept TEXT DEFAULT '',
                    phone TEXT DEFAULT '',
                    fingerprint_id TEXT DEFAULT '',
                    card_no TEXT DEFAULT '',
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_employee_card ON {_q(TABLE_EMPLOYEE_RECORDS)}(card_no)")
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_employee_fingerprint ON {_q(TABLE_EMPLOYEE_RECORDS)}(fingerprint_id)")
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_employee_no ON {_q(TABLE_EMPLOYEE_RECORDS)}(employee_no)")

            self._create_sync_queue(cursor, "sync_queue", "idx_sync_queue")

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sync_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    direction TEXT NOT NULL CHECK(direction IN ('pull','push')),
                    status TEXT NOT NULL,
                    records_count INTEGER,
                    detail TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sync_snapshots (
                    table_name TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    updated_at TEXT,
                    PRIMARY KEY (table_name, record_id)
                )
            """)

            # ========== 索引优化（批量执行，减少 WAL 写入次数）==========
            index_statements = [
                f"CREATE INDEX IF NOT EXISTS idx_materials_code ON {_q(TABLE_MATERIALS)}(material_code)",
                f"CREATE INDEX IF NOT EXISTS idx_materials_name ON {_q(TABLE_MATERIALS)}(material_name)",
                f"CREATE INDEX IF NOT EXISTS idx_materials_location ON {_q(TABLE_MATERIALS)}(location)",
                f"CREATE INDEX IF NOT EXISTS idx_borrow_card ON {_q(TABLE_BORROW_RECORDS)}(card_no)",
                f"CREATE INDEX IF NOT EXISTS idx_borrow_code ON {_q(TABLE_BORROW_RECORDS)}(material_code)",
                f"CREATE INDEX IF NOT EXISTS idx_borrow_name ON {_q(TABLE_BORROW_RECORDS)}(material_name)",
                f"CREATE INDEX IF NOT EXISTS idx_borrow_material ON {_q(TABLE_BORROW_RECORDS)}(material_id)",
                f"CREATE INDEX IF NOT EXISTS idx_borrow_returned ON {_q(TABLE_BORROW_RECORDS)}(is_returned)",
                f"CREATE INDEX IF NOT EXISTS idx_asset_no ON {_q(TABLE_FIXED_ASSETS)}(asset_no)",
                f"CREATE INDEX IF NOT EXISTS idx_asset_name ON {_q(TABLE_FIXED_ASSETS)}(asset_name)",
                f"CREATE INDEX IF NOT EXISTS idx_config_name ON {_q(TABLE_CONFIG_ITEMS)}(config_name)",
                "CREATE INDEX IF NOT EXISTS idx_sync_queue ON sync_queue(created_at)",
                "CREATE INDEX IF NOT EXISTS idx_sync_queue_claim ON sync_queue(status, next_retry_at, created_at)",
            ]
            for stmt in index_statements:
                cursor.execute(stmt)

            # ========== v20 修复：旧版错误创建的 admin 密码升级为哈希 ==========
            from utils.helpers import hash_password
            cursor.execute(
                f"UPDATE {_q(TABLE_USERS)} SET password = ? WHERE username = 'admin' AND password = 'admin'",
                (hash_password("admin"),)
            )

    def _create_sync_queue(self, cursor, queue_table: str, index_prefix: str):
        """创建或升级一个可恢复同步队列；公共 outbox 与共享业务库同库。"""
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {_q(queue_table)} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL UNIQUE,
                table_name TEXT NOT NULL,
                operation TEXT NOT NULL CHECK(operation IN ('INSERT','UPDATE','DELETE')),
                record_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                retry_count INTEGER DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'in_progress', 'retryable', 'succeeded', 'failed')),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                next_retry_at TEXT,
                batch_id TEXT,
                status_updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._migrate_sync_queue_state_machine(cursor, queue_table, index_prefix)
        cursor.execute(
            f"CREATE INDEX IF NOT EXISTS {index_prefix}_claim "
            f"ON {_q(queue_table)}(status, next_retry_at, created_at)"
        )

    def _migrate_sync_queue_state_machine(self, cursor, queue_table: str = "sync_queue",
                                          index_prefix: str = "idx_sync_queue"):
        """为旧版同步队列补齐可恢复状态机字段。"""
        existing_columns = {row[1] for row in cursor.execute(f"PRAGMA table_info({_q(queue_table)})")}
        new_columns = [
            ("operation_id", "TEXT"), ("status", "TEXT DEFAULT 'pending'"),
            ("attempt_count", "INTEGER DEFAULT 0"), ("last_error", "TEXT"),
            ("next_retry_at", "TEXT"), ("batch_id", "TEXT"),
            ("status_updated_at", "TEXT"),
        ]
        for column_name, column_type in new_columns:
            if column_name not in existing_columns:
                cursor.execute(f"ALTER TABLE {_q(queue_table)} ADD COLUMN {column_name} {column_type}")
        cursor.execute(
            f"UPDATE {_q(queue_table)} SET operation_id = 'legacy-' || id "
            "WHERE operation_id IS NULL OR operation_id = ''"
        )
        cursor.execute(
            f"UPDATE {_q(queue_table)} SET status = 'pending' "
            "WHERE status IS NULL OR status = ''"
        )
        if "attempt_count" not in existing_columns:
            cursor.execute(f"UPDATE {_q(queue_table)} SET attempt_count = COALESCE(retry_count, 0)")
        else:
            cursor.execute(
                f"UPDATE {_q(queue_table)} SET attempt_count = COALESCE(attempt_count, retry_count, 0) "
                "WHERE attempt_count IS NULL"
            )
        cursor.execute(
            f"UPDATE {_q(queue_table)} SET status_updated_at = "
            "COALESCE(status_updated_at, created_at, CURRENT_TIMESTAMP) "
            "WHERE status_updated_at IS NULL OR status_updated_at = ''"
        )
        cursor.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {index_prefix}_operation_id "
            f"ON {_q(queue_table)}(operation_id)"
        )

    def _migrate_borrow_records_v19(self, cursor):
        """v19 迁移：为已有 borrow_records 表添加归还相关新字段"""
        new_columns = [
            ("return_person", "TEXT"),
            ("return_qty", "INTEGER DEFAULT 0"),
            ("good_qty", "INTEGER DEFAULT 0"),
            ("damage_status", "TEXT DEFAULT ''"),
            ("mixed_qty", "INTEGER DEFAULT 0"),
            ("mixed_remark", "TEXT DEFAULT ''"),
        ]
        for col_name, col_type in new_columns:
            try:
                cursor.execute(
                    f"ALTER TABLE {_q(TABLE_BORROW_RECORDS)} ADD COLUMN {col_name} {col_type}"
                )
            except sqlite3.OperationalError as e:
                if "duplicate column name" in str(e).lower():
                    pass
                else:
                    raise

    def _migrate_borrow_records_archived(self, cursor):
        """v25 迁移：为 borrow_records 添加 is_archived 字段，支持软删除归档"""
        try:
            cursor.execute(
                f"ALTER TABLE {_q(TABLE_BORROW_RECORDS)} ADD COLUMN is_archived INTEGER DEFAULT 0"
            )
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                pass
            else:
                raise

    def _migrate_workshop_column(self, cursor):
        """v4306 迁移：为三张核心表添加 workshop 字段，支持分车间管理"""
        workshop_tables = [TABLE_MATERIALS, TABLE_BORROW_RECORDS, TABLE_FIXED_ASSETS]
        for table in workshop_tables:
            try:
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                )
                if cursor.fetchone() is None:
                    continue
                cursor.execute(
                    f"ALTER TABLE {_q(table)} ADD COLUMN workshop TEXT DEFAULT '{DEFAULT_WORKSHOP}'"
                )
                cursor.execute(
                    f"UPDATE {_q(table)} SET workshop = '{DEFAULT_WORKSHOP}' WHERE workshop IS NULL OR workshop = ''"
                )
                cursor.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{table.replace('MMS_', '')}_workshop ON {_q(table)}(workshop)"
                )
                _log.info("表 %s 已添加 workshop 字段", table)
            except sqlite3.OperationalError as e:
                if "duplicate column name" in str(e).lower():
                    cursor.execute(
                        f"UPDATE {_q(table)} SET workshop = '{DEFAULT_WORKSHOP}' WHERE workshop IS NULL OR workshop = ''"
                    )
                    cursor.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{table.replace('MMS_', '')}_workshop ON {_q(table)}(workshop)"
                    )
                else:
                    raise

    def _migrate_table_rename(self, cursor):
        """v4.3 迁移：将旧版英文表名重命名为新版中文表名"""
        rename_map = {
            "materials": TABLE_MATERIALS,
            "borrow_records": TABLE_BORROW_RECORDS,
            "fixed_assets": TABLE_FIXED_ASSETS,
            "config_items": TABLE_CONFIG_ITEMS,
            "users": TABLE_USERS,
            "employee_records": TABLE_EMPLOYEE_RECORDS,
        }
        for old_name, new_name in rename_map.items():
            try:
                cursor.execute(f"ALTER TABLE {_q(old_name)} RENAME TO {_q(new_name)}")
                _log.info("数据库表已重命名: %s -> %s", old_name, new_name)
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "does not exist" in msg or "no such table" in msg:
                    pass
                elif "already exists" in str(e).lower():
                    try:
                        cursor.execute(f"DROP TABLE {_q(old_name)}")
                        _log.warning("旧表 %s 已删除（新表已存在）", old_name)
                    except sqlite3.OperationalError:
                        pass
                else:
                    _log.warning("表重命名 %s -> %s 失败: %s", old_name, new_name, e)

    # ---------- 用户管理 (v4.2 新增) ----------

    def get_user_by_username(self, username: str) -> Optional[Dict]:
        """根据用户名查询用户"""
        sql = f"SELECT * FROM {_q(TABLE_USERS)} WHERE username = ?"
        with self._shared_connect() as conn:
            cursor = conn.execute(sql, (username,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_all_users(self) -> List[Dict]:
        """获取所有用户"""
        sql = f"SELECT * FROM {_q(TABLE_USERS)} ORDER BY created_at DESC"
        with self._shared_connect() as conn:
            cursor = conn.execute(sql)
            return [dict(row) for row in cursor.fetchall()]

    def user_exists(self, username: str) -> bool:
        """检查用户名是否已存在"""
        sql = f"SELECT 1 FROM {_q(TABLE_USERS)} WHERE username = ? LIMIT 1"
        with self._shared_connect() as conn:
            cursor = conn.execute(sql, (username,))
            return cursor.fetchone() is not None

    def create_default_admin(self) -> Optional[str]:
        """确保管理员账号存在，判断是否首次登录

        只要用户从未完成过登录（没有 .login_completed.json），
        就生成随机密码并弹窗显示，无论 admin 是否已存在于数据库中。

        Returns:
            生成的随机密码（首次登录状态），如果已登录过则返回 None
        """
        flag_path = os.path.join(os.path.dirname(self._shared_db_path), ".login_completed.json")
        login_completed = os.path.isfile(flag_path)

        if login_completed:
            return None  # 已登录过，无需弹窗

        # 首次登录：确保 admin 账号存在且密码为随机值
        from utils.helpers import hash_password
        import secrets
        random_pwd = secrets.token_hex(8)  # 16 位十六进制随机密码
        now = datetime.now().isoformat()

        if not self.user_exists("admin"):
            _log.warning("=" * 50)
            _log.warning("创建默认管理员账号")
            _log.warning("用户名: admin")
            _log.warning("默认密码: %s", random_pwd)
            _log.warning("请登录后立即修改密码！")
            _log.warning("=" * 50)
            data = {
                "id": DEFAULT_ADMIN_ID,
                "username": "admin",
                "password": hash_password(random_pwd),
                "display_name": "ROOT",
                "role": "admin",
                "is_active": 1,
                "created_at": now,
                "updated_at": now,
            }
            self.insert(TABLE_USERS, data)
        else:
            _log.warning("首次登录：重置管理员密码")
            _log.warning("新密码: %s", random_pwd)
            self.update(TABLE_USERS, DEFAULT_ADMIN_ID, {
                "password": hash_password(random_pwd),
                "updated_at": now,
            })
        return random_pwd

    # ---------- 员工管理 ----------

    def get_employee_by_card_no(self, card_no: str) -> Optional[Dict]:
        sql = f"SELECT * FROM {_q(TABLE_EMPLOYEE_RECORDS)} WHERE card_no = ?"
        with self._shared_connect() as conn:
            cursor = conn.execute(sql, (card_no,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_employee_by_fingerprint_id(self, fingerprint_id: str) -> Optional[Dict]:
        sql = f"SELECT * FROM {_q(TABLE_EMPLOYEE_RECORDS)} WHERE fingerprint_id = ?"
        with self._shared_connect() as conn:
            cursor = conn.execute(sql, (fingerprint_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_all_employees(self) -> List[Dict]:
        sql = f"SELECT * FROM {_q(TABLE_EMPLOYEE_RECORDS)} ORDER BY created_at DESC"
        with self._shared_connect() as conn:
            cursor = conn.execute(sql)
            return [dict(row) for row in cursor.fetchall()]

    def create_employee(self, data: Dict[str, Any]) -> None:
        _validate_table(TABLE_EMPLOYEE_RECORDS)
        now = datetime.now().isoformat()
        if "created_at" not in data:
            data["created_at"] = now
        if "updated_at" not in data:
            data["updated_at"] = now
        self.insert(TABLE_EMPLOYEE_RECORDS, data)

    def update_employee(self, record_id: str, data: Dict[str, Any]) -> None:
        now = datetime.now().isoformat()
        data["updated_at"] = now
        self.update(TABLE_EMPLOYEE_RECORDS, record_id, data)

    def delete_employee(self, record_id: str) -> None:
        self.delete(TABLE_EMPLOYEE_RECORDS, record_id)

    def employee_exists(self, employee_no: str) -> bool:
        sql = f"SELECT 1 FROM {_q(TABLE_EMPLOYEE_RECORDS)} WHERE employee_no = ? LIMIT 1"
        with self._shared_connect() as conn:
            cursor = conn.execute(sql, (employee_no,))
            return cursor.fetchone() is not None


    # ---------- 基础 CRUD ----------

    def insert(self, table: str, data: Dict[str, Any]) -> None:
        _validate_table(table)
        if table in WORKSHOP_TABLES and "workshop" not in data:
            data = {**data, "workshop": self._workshop}
        columns = ', '.join(data.keys())
        placeholders = ', '.join(['?' for _ in data])
        sql = f"INSERT OR REPLACE INTO {_q(table)} ({columns}) VALUES ({placeholders})"
        with (self._shared_connect() if table in SHARED_TABLES else self._connect()) as conn:
            conn.execute(sql, tuple(data.values()))

    def update(self, table: str, record_id: str, data: Dict[str, Any]) -> None:
        _validate_table(table)
        if table in WORKSHOP_TABLES and "workshop" not in data:
            data = {**data, "workshop": self._workshop}
        columns = ', '.join([f"{k} = ?" for k in data.keys()])
        sql = f"UPDATE {_q(table)} SET {columns} WHERE id = ?"
        with (self._shared_connect() if table in SHARED_TABLES else self._connect()) as conn:
            conn.execute(sql, tuple(data.values()) + (record_id,))

    def delete(self, table: str, record_id: str) -> None:
        _validate_table(table)
        sql = f"DELETE FROM {_q(table)} WHERE id = ?"
        with (self._shared_connect() if table in SHARED_TABLES else self._connect()) as conn:
            conn.execute(sql, (record_id,))

    def query(self, table: str, conditions: Optional[str] = None,
              params: tuple = (), order_by: Optional[str] = None,
              limit: Optional[int] = None) -> List[Dict]:
        _validate_table(table)
        if conditions:
            _validate_conditions(conditions)
        if order_by:
            _validate_order_by(order_by)
        cond, wparams = _add_workshop_filter(table, conditions, params, self._workshop)
        sql = f"SELECT * FROM {_q(table)}"
        if cond:
            sql += f" WHERE {cond}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit:
            limit = _validate_numeric(limit, "limit")
            sql += f" LIMIT {limit}"
        with (self._shared_connect() if table in SHARED_TABLES else self._connect()) as conn:
            cursor = conn.execute(sql, wparams)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def query_one(self, table: str, record_id: str) -> Optional[Dict]:
        _validate_table(table)
        cond, wparams = _add_workshop_filter(table, "id = ?", (record_id,), self._workshop)
        sql = f"SELECT * FROM {_q(table)} WHERE {cond}"
        with self._connect() as conn:
            cursor = conn.execute(sql, wparams)
            row = cursor.fetchone()
            return dict(row) if row else None

    # ---------- 分页查询（v4.1 新增）----------

    def query_paged(self, table: str, page: int = 1, page_size: int = 50,
                    conditions: Optional[str] = None,
                    params: tuple = (), order_by: Optional[str] = None) -> Tuple[List[Dict], int]:
        """分页查询，返回 (数据列表, 总条数)

        Args:
            page: 页码（从1开始）
            page_size: 每页条数
            conditions: WHERE 条件（不含 WHERE 关键字）
            params: 条件参数
            order_by: ORDER BY 子句（不含 ORDER BY 关键字）
        """
        _validate_table(table)
        if conditions:
            _validate_conditions(conditions)
        if order_by:
            _validate_order_by(order_by)

        cond, wparams = _add_workshop_filter(table, conditions, params, self._workshop)

        count_sql = f"SELECT COUNT(*) as count FROM {_q(table)}"
        if cond:
            count_sql += f" WHERE {cond}"
        with self._connect() as conn:
            total = conn.execute(count_sql, wparams).fetchone()["count"]

        sql = f"SELECT * FROM {_q(table)}"
        if cond:
            sql += f" WHERE {cond}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        offset = max(0, (page - 1) * page_size)
        sql += f" LIMIT {page_size} OFFSET {offset}"

        with self._connect() as conn:
            cursor = conn.execute(sql, wparams)
            rows = cursor.fetchall()
            return [dict(row) for row in rows], total

    # ---------- 模糊搜索（v4.1 新增）----------

    def fuzzy_search(self, table: str, keyword: str,
                     search_columns: List[str],
                     limit: int = 50,
                     order_by: Optional[str] = None) -> List[Dict]:
        _validate_table(table)
        if not keyword or not search_columns:
            return []
        _validate_columns(search_columns, table)
        if order_by:
            _validate_order_by(order_by)

        like_pattern = f"%{keyword}%"
        conditions = " OR ".join([f"{col} LIKE ?" for col in search_columns])
        params = tuple([like_pattern] * len(search_columns))
        cond, wparams = _add_workshop_filter(table, conditions, params, self._workshop)
        limit = _validate_numeric(limit, "limit")

        sql = f"SELECT * FROM {_q(table)} WHERE {cond}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        sql += f" LIMIT {limit}"

        with self._connect() as conn:
            cursor = conn.execute(sql, wparams)
            return [dict(row) for row in cursor.fetchall()]

    def count(self, table: str, conditions: Optional[str] = None,
              params: tuple = ()) -> int:
        """查询表记录总数"""
        _validate_table(table)
        if conditions:
            _validate_conditions(conditions)
        cond, wparams = _add_workshop_filter(table, conditions, params, self._workshop)
        sql = f"SELECT COUNT(*) as count FROM {_q(table)}"
        if cond:
            sql += f" WHERE {cond}"
        with self._connect() as conn:
            return conn.execute(sql, wparams).fetchone()["count"]

    # ---------- 同步队列 ----------

    def _queue_connections(self):
        """统一暴露车间队列与公共 outbox；调用方无需感知数据来源。"""
        return (
            ("workshop", "sync_queue", self._connect),
            ("shared", "shared_sync_queue", self._shared_connect),
        )

    @staticmethod
    def _with_queue_source(rows, source: str) -> List[Dict]:
        """为跨 outbox 返回项附加不可歧义的队列来源。"""
        return [
            {**dict(row), "queue_source": source, "_queue_source": source}
            for row in rows
        ]

    def _queue_for_source(self, source: str):
        """按显式来源定位唯一 outbox，禁止按裸 queue_id 跨库操作。"""
        for candidate, queue_table, connect in self._queue_connections():
            if candidate == source:
                return queue_table, connect
        raise ValueError("同步队列来源必须是 'workshop' 或 'shared'")

    def _queue_item_by_operation_id(self, operation_id: str) -> Optional[Dict]:
        """operation_id 在两个 outbox 全局唯一，是状态 API 的主标识。"""
        for source, queue_table, connect in self._queue_connections():
            with connect() as conn:
                row = conn.execute(
                    f"SELECT * FROM {_q(queue_table)} WHERE operation_id = ?", (operation_id,)
                ).fetchone()
            if row:
                return self._with_queue_source([row], source)[0]
        return None

    def _assert_operation_id_available(self, operation_id: str, exclude_source: Optional[str] = None) -> None:
        """在另一库检查 operation_id，避免跨两个 outbox 的标识冲突。"""
        for source, queue_table, connect in self._queue_connections():
            if source == exclude_source:
                continue
            with connect() as conn:
                if conn.execute(
                    f"SELECT 1 FROM {_q(queue_table)} WHERE operation_id = ?", (operation_id,)
                ).fetchone():
                    raise sqlite3.IntegrityError(f"重复的同步 operation_id: {operation_id}")

    def apply_local_change_with_sync(self, table: str, operation: str,
                                     record_id: str, data: Dict[str, Any],
                                     payload: Dict[str, Any],
                                     operation_id: Optional[str] = None) -> int:
        """在业务表所属数据库中，与对应 outbox 同一事务提交。"""
        _validate_table(table)
        if operation not in {"INSERT", "UPDATE", "DELETE"}:
            raise ValueError(f"不支持的本地同步操作: {operation}")
        operation_id = operation_id or str(uuid.uuid4())
        payload_json = json.dumps(payload, ensure_ascii=False)
        if table in SHARED_TABLES:
            return self._apply_shared_change_with_sync(
                table, operation, record_id, data, payload_json, operation_id,
            )

        self._assert_operation_id_available(operation_id, exclude_source="workshop")
        conn = self._get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._execute_local_change(conn, table, operation, record_id, data)
            queue_id = self._enqueue_sync_operation(
                conn, table, operation, record_id, payload_json, operation_id,
            )
            conn.commit()
            return queue_id
        except Exception:
            conn.rollback()
            raise

    def _apply_shared_change_with_sync(self, table: str, operation: str,
                                       record_id: str, data: Dict[str, Any],
                                       payload_json: str, operation_id: str) -> int:
        """共享表和 shared_sync_queue 同库同事务，绝不依赖 ATTACH/WAL 跨库原子性。"""
        self._assert_operation_id_available(operation_id, exclude_source="shared")
        conn = self._shared_get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._execute_local_change(conn, table, operation, record_id, data)
            queue_id = self._enqueue_sync_operation(
                conn, table, operation, record_id, payload_json, operation_id,
                queue_table="shared_sync_queue",
            )
            conn.commit()
            return queue_id
        except Exception:
            conn.rollback()
            raise

    def _execute_local_change(self, conn: sqlite3.Connection, table: str,
                              operation: str, record_id: str,
                              data: Dict[str, Any]) -> None:
        """在调用方持有的连接中执行单个本地业务变更。"""
        if operation == "INSERT":
            values = dict(data)
            if table in WORKSHOP_TABLES and "workshop" not in values:
                values["workshop"] = self._workshop
            columns = ", ".join(values.keys())
            placeholders = ", ".join("?" for _ in values)
            conn.execute(
                f"INSERT OR REPLACE INTO {_q(table)} ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
        elif operation == "UPDATE":
            values = dict(data)
            if table in WORKSHOP_TABLES and "workshop" not in values:
                values["workshop"] = self._workshop
            columns = ", ".join(f"{key} = ?" for key in values)
            conn.execute(
                f"UPDATE {_q(table)} SET {columns} WHERE id = ?",
                tuple(values.values()) + (record_id,),
            )
        else:
            conn.execute(f"DELETE FROM {_q(table)} WHERE id = ?", (record_id,))

    def _enqueue_sync_operation(self, conn: sqlite3.Connection, table_name: str,
                                operation: str, record_id: str,
                                payload_json: str, operation_id: str,
                                queue_table: str = "sync_queue") -> int:
        return conn.execute(
            f"""
            INSERT INTO {_q(queue_table)} (
                operation_id, table_name, operation, record_id, payload, status,
                attempt_count, status_updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?)
            """,
            (operation_id, table_name, operation, record_id, payload_json, datetime.now().isoformat()),
        ).lastrowid

    def add_sync_queue(self, table_name: str, operation: str,
                       record_id: str, payload: Dict) -> None:
        """兼容旧调用方的入队接口。"""
        self.enqueue_sync_operation(table_name, operation, record_id, payload)

    def enqueue_sync_operation(self, table_name: str, operation: str,
                               record_id: str, payload: Dict,
                               operation_id: Optional[str] = None,
                               conn: Optional[sqlite3.Connection] = None) -> int:
        """入队到业务表所属 outbox；operation_id 在两个队列范围内唯一。"""
        operation_id = operation_id or str(uuid.uuid4())
        payload_json = json.dumps(payload, ensure_ascii=False)
        if conn is not None:
            self._assert_operation_id_available(operation_id, exclude_source="workshop")
            return self._enqueue_sync_operation(conn, table_name, operation, record_id, payload_json, operation_id)
        source = "shared" if table_name in SHARED_TABLES else "workshop"
        queue_table = "shared_sync_queue" if source == "shared" else "sync_queue"
        self._assert_operation_id_available(operation_id, exclude_source=source)
        connect = self._shared_connect if source == "shared" else self._connect
        with connect() as own_conn:
            return self._enqueue_sync_operation(
                own_conn, table_name, operation, record_id, payload_json, operation_id, queue_table,
            )

    def _claim_queue_batch(self, source: str, queue_table: str, connect, limit: int,
                           now: str, batch_id: str) -> List[Dict]:
        if limit <= 0:
            return []
        eligible = "(status = 'pending' OR (status = 'retryable' AND (next_retry_at IS NULL OR next_retry_at <= ?)))"
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"SELECT id FROM {_q(queue_table)} WHERE {eligible} ORDER BY created_at, id LIMIT ?",
                (now, limit),
            ).fetchall()
            if not rows:
                return []
            ids = [row["id"] for row in rows]
            placeholders = ", ".join("?" for _ in ids)
            conn.execute(
                f"UPDATE {_q(queue_table)} SET status = 'in_progress', batch_id = ?, status_updated_at = ? "
                f"WHERE id IN ({placeholders}) AND {eligible}",
                (batch_id, now, *ids, now),
            )
            claimed = conn.execute(
                f"SELECT * FROM {_q(queue_table)} WHERE id IN ({placeholders}) "
                "AND status = 'in_progress' AND batch_id = ? ORDER BY created_at, id",
                (*ids, batch_id),
            ).fetchall()
            return self._with_queue_source(claimed, source)

    def claim_sync_batch(self, limit: int = 50, now: Optional[str] = None,
                         batch_id: Optional[str] = None) -> List[Dict]:
        """统一原子领取两个 outbox 中的待处理项，并使用同一 batch_id。"""
        limit = _validate_numeric(limit, "同步领取数量")
        if limit == 0:
            return []
        now = now or datetime.now().isoformat()
        batch_id = batch_id or str(uuid.uuid4())
        claimed = []
        for source, queue_table, connect in self._queue_connections():
            claimed.extend(self._claim_queue_batch(source, queue_table, connect, limit - len(claimed), now, batch_id))
            if len(claimed) >= limit:
                break
        return claimed

    def _update_queue_item(self, sql_template: str, params: tuple) -> bool:
        updated = 0
        for _, queue_table, connect in self._queue_connections():
            with connect() as conn:
                result = conn.execute(sql_template.format(queue_table=_q(queue_table)), params)
                updated += result.rowcount
        return updated == 1

    def mark_sync_succeeded(self, operation_id: str, batch_id: str) -> bool:
        now = datetime.now().isoformat()
        return self._update_queue_item(
            "UPDATE {queue_table} SET status = 'succeeded', batch_id = NULL, last_error = NULL, "
            "next_retry_at = NULL, status_updated_at = ? "
            "WHERE operation_id = ? AND batch_id = ? AND status = 'in_progress'",
            (now, operation_id, batch_id),
        )

    def mark_sync_retryable(self, operation_id: str, batch_id: str, error: str,
                            next_retry_at: Optional[str] = None,
                            max_attempts: int = 3) -> bool:
        max_attempts = _validate_numeric(max_attempts, "最大重试次数")
        if max_attempts == 0:
            raise ValueError("最大重试次数必须至少为 1")
        now = datetime.now().isoformat()
        retry_at = next_retry_at or now
        updated = self._update_queue_item(
            "UPDATE {queue_table} SET attempt_count = attempt_count + 1, last_error = ?, "
            "status = CASE WHEN attempt_count + 1 >= ? THEN 'failed' ELSE 'retryable' END, "
            "batch_id = NULL, next_retry_at = CASE WHEN attempt_count + 1 >= ? THEN NULL ELSE ? END, "
            "status_updated_at = ? WHERE operation_id = ? AND batch_id = ? AND status = 'in_progress'",
            (error, max_attempts, max_attempts, retry_at, now, operation_id, batch_id),
        )
        if not updated:
            return False
        for _, queue_table, connect in self._queue_connections():
            with connect() as conn:
                row = conn.execute(
                    f"SELECT status FROM {_q(queue_table)} WHERE operation_id = ?", (operation_id,)
                ).fetchone()
            if row:
                return row["status"] == "retryable"
        return False

    def mark_sync_failed(self, operation_id: str, batch_id: str, error: str) -> bool:
        now = datetime.now().isoformat()
        return self._update_queue_item(
            "UPDATE {queue_table} SET status = 'failed', batch_id = NULL, last_error = ?, "
            "next_retry_at = NULL, status_updated_at = ? "
            "WHERE operation_id = ? AND batch_id = ? AND status = 'in_progress'",
            (error, now, operation_id, batch_id),
        )

    def recover_in_progress_operations(self, now: Optional[str] = None) -> int:
        now = now or datetime.now().isoformat()
        recovered = 0
        for _, queue_table, connect in self._queue_connections():
            with connect() as conn:
                recovered += conn.execute(
                    f"UPDATE {_q(queue_table)} SET status = 'retryable', batch_id = NULL, next_retry_at = ?, "
                    "status_updated_at = ? WHERE status = 'in_progress'",
                    (now, now),
                ).rowcount
        return recovered

    def get_sync_queue(self, limit: int = 50) -> List[Dict]:
        limit = _validate_numeric(limit, "同步队列查询数量")
        rows = []
        for source, queue_table, connect in self._queue_connections():
            with connect() as conn:
                rows.extend(self._with_queue_source(conn.execute(
                    f"SELECT * FROM {_q(queue_table)} ORDER BY created_at, id LIMIT ?", (limit,)
                ).fetchall(), source))
        rows.sort(key=lambda item: (item.get("created_at") or "", item["_queue_source"], item["id"]))
        return rows[:limit]

    def remove_sync_queue(self, queue_id: Optional[int] = None, *, source: Optional[str] = None,
                          operation_id: Optional[str] = None) -> bool:
        """删除一个 outbox 项；裸 queue_id 必须同时给出 queue source。"""
        if operation_id is not None:
            item = self._queue_item_by_operation_id(operation_id)
            if not item:
                return False
            queue_id, source = item["id"], item["queue_source"]
        if queue_id is None or source is None:
            raise ValueError("queue_id 操作必须提供 source，或改用 operation_id")
        queue_table, connect = self._queue_for_source(source)
        with connect() as conn:
            return conn.execute(
                f"DELETE FROM {_q(queue_table)} WHERE id = ?", (queue_id,)
            ).rowcount == 1

    def increment_retry(self, queue_id: Optional[int] = None, *, source: Optional[str] = None,
                        operation_id: Optional[str] = None) -> bool:
        """增加唯一 outbox 项的重试计数，绝不触碰另一队列的同 id 项。"""
        if operation_id is not None:
            item = self._queue_item_by_operation_id(operation_id)
            if not item:
                return False
            queue_id, source = item["id"], item["queue_source"]
        if queue_id is None or source is None:
            raise ValueError("queue_id 操作必须提供 source，或改用 operation_id")
        queue_table, connect = self._queue_for_source(source)
        with connect() as conn:
            return conn.execute(
                f"UPDATE {_q(queue_table)} SET retry_count = retry_count + 1, "
                f"attempt_count = attempt_count + 1 WHERE id = ?", (queue_id,)
            ).rowcount == 1

    def get_sync_queue_count(self, statuses: Optional[Set[str]] = None) -> int:
        statuses = statuses or {"pending", "retryable", "in_progress"}
        placeholders = ", ".join("?" for _ in statuses)
        total = 0
        for _, queue_table, connect in self._queue_connections():
            with connect() as conn:
                total += conn.execute(
                    f"SELECT COUNT(*) AS count FROM {_q(queue_table)} WHERE status IN ({placeholders})", tuple(statuses)
                ).fetchone()["count"]
        return total

    def get_sync_queue_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for _, queue_table, connect in self._queue_connections():
            with connect() as conn:
                rows = conn.execute(f"SELECT status, COUNT(*) AS count FROM {_q(queue_table)} GROUP BY status").fetchall()
            for row in rows:
                counts[row["status"]] = counts.get(row["status"], 0) + row["count"]
        return counts

    def get_sync_queue_item(self, queue_id: Optional[int] = None, *, source: Optional[str] = None,
                            operation_id: Optional[str] = None) -> Optional[Dict]:
        """读取一个 outbox 项；operation_id 优先，queue_id 必须带明确来源。"""
        if operation_id is not None:
            return self._queue_item_by_operation_id(operation_id)
        if queue_id is None or source is None:
            raise ValueError("queue_id 查询必须提供 source，或改用 operation_id")
        queue_table, connect = self._queue_for_source(source)
        with connect() as conn:
            row = conn.execute(f"SELECT * FROM {_q(queue_table)} WHERE id = ?", (queue_id,)).fetchone()
        return self._with_queue_source([row], source)[0] if row else None

    def get_pending_record_ids(self, table: str) -> set:
        """公共 outbox 也参与 pull 覆盖保护。"""
        pending_ids = set()
        for _, queue_table, connect in self._queue_connections():
            with connect() as conn:
                rows = conn.execute(
                    f"SELECT record_id FROM {_q(queue_table)} WHERE table_name = ? "
                    "AND status IN ('pending', 'retryable', 'in_progress')",
                    (table,),
                ).fetchall()
            pending_ids.update(row["record_id"] for row in rows)
        return pending_ids

    def replace_table_transactional(self, table: str, records: List[Dict]) -> None:
        _validate_table(table)
        if not records:
            _log.warning("replace_table_transactional 收到空数据，跳过表 %s 的清空操作", table)
            return
        # 保护待同步记录，防止全量同步覆盖本地未上传的修改
        pending_ids = self.get_pending_record_ids(table)
        with (self._shared_connect() if table in SHARED_TABLES else self._connect()) as conn:
            if table in WORKSHOP_TABLES:
                ws_clause = f"workshop = {repr(self._workshop)}"
                if pending_ids:
                    placeholders = ', '.join(['?' for _ in pending_ids])
                    conn.execute(f"DELETE FROM {_q(table)} WHERE {ws_clause} AND id NOT IN ({placeholders})", list(pending_ids))
                else:
                    conn.execute(f"DELETE FROM {_q(table)} WHERE {ws_clause}")
            else:
                if pending_ids:
                    placeholders = ', '.join(['?' for _ in pending_ids])
                    conn.execute(f"DELETE FROM {_q(table)} WHERE id NOT IN ({placeholders})", list(pending_ids))
                else:
                    conn.execute(f"DELETE FROM {_q(table)}")
            if records:
                if pending_ids:
                    records = [r for r in records if r.get("id") not in pending_ids]
                if records:
                    columns = ', '.join(records[0].keys())
                    placeholders = ', '.join(['?' for _ in records[0]])
                    sql = f"INSERT OR REPLACE INTO {_q(table)} ({columns}) VALUES ({placeholders})"
                    conn.executemany(sql, [tuple(r.values()) for r in records])

    # ---------- 三路合并：快照管理 ----------

    def save_snapshot(self, table: str, record_id: str, data: Dict) -> None:
        """保存同步快照（记录上次同步成功时的完整状态）"""
        now = datetime.now().isoformat()
        sql = "INSERT OR REPLACE INTO sync_snapshots (table_name, record_id, snapshot, updated_at) VALUES (?, ?, ?, ?)"
        with self._connect() as conn:
            conn.execute(sql, (table, record_id, json.dumps(data, ensure_ascii=False), now))

    def get_snapshot(self, table: str, record_id: str) -> Optional[Dict]:
        """获取同步快照，不存在返回 None"""
        sql = "SELECT snapshot FROM sync_snapshots WHERE table_name = ? AND record_id = ?"
        with self._connect() as conn:
            cursor = conn.execute(sql, (table, record_id))
            row = cursor.fetchone()
            if row:
                return json.loads(row["snapshot"])
            return None

    def delete_snapshot(self, table: str, record_id: str) -> None:
        """删除同步快照"""
        sql = "DELETE FROM sync_snapshots WHERE table_name = ? AND record_id = ?"
        with self._connect() as conn:
            conn.execute(sql, (table, record_id))

    def save_snapshots_batch(self, table: str, records: List[Dict]) -> None:
        """批量保存快照（用于拉取/全量同步后）"""
        now = datetime.now().isoformat()
        sql = "INSERT OR REPLACE INTO sync_snapshots (table_name, record_id, snapshot, updated_at) VALUES (?, ?, ?, ?)"
        with self._connect() as conn:
            for record in records:
                rid = record.get("id")
                if rid:
                    conn.execute(sql, (table, rid, json.dumps(record, ensure_ascii=False), now))

    def add_sync_log(self, direction: str, status: str,
                     records_count: int = 0, detail: str = "") -> None:
        sql = """
            INSERT INTO sync_log (direction, status, records_count, detail)
            VALUES (?, ?, ?, ?)
        """
        with self._connect() as conn:
            conn.execute(sql, (direction, status, records_count, detail))

    def get_last_sync_time(self, direction: str) -> Optional[str]:
        sql = """
            SELECT created_at FROM sync_log
            WHERE direction = ? AND status = 'success'
            ORDER BY created_at DESC LIMIT 1
        """
        with self._connect() as conn:
            cursor = conn.execute(sql, (direction,))
            row = cursor.fetchone()
            return row["created_at"] if row else None

    def get_table_max_updated_at(self, table: str) -> Optional[str]:
        """获取本地表中最新的 updated_at 时间（作为增量拉取的更精确基准）"""
        _validate_table(table)
        try:
            cond, wparams = _add_workshop_filter(table, None, (), self._workshop)
            sql = f"SELECT MAX(updated_at) AS t FROM {_q(table)}"
            if cond:
                sql += f" WHERE {cond}"
            with self._connect() as conn:
                row = conn.execute(sql, wparams).fetchone()
                return row["t"] if row and row["t"] else None
        except Exception:
            return None

    def get_table_count(self, table: str) -> int:
        """获取本地表的记录数"""
        _validate_table(table)
        try:
            cond, wparams = _add_workshop_filter(table, None, (), self._workshop)
            sql = f"SELECT COUNT(*) AS c FROM {_q(table)}"
            if cond:
                sql += f" WHERE {cond}"
            with self._connect() as conn:
                row = conn.execute(sql, wparams).fetchone()
                return row["c"] if row else 0
        except Exception:
            return 0

    def batch_insert(self, table: str, records: List[Dict]) -> None:
        _validate_table(table)
        if not records:
            return
        pending_ids = self.get_pending_record_ids(table)
        if pending_ids:
            records = [r for r in records if r.get("id") not in pending_ids]
        if not records:
            return
        if table in WORKSHOP_TABLES:
            records = [r for r in records if r.get("workshop") == self._workshop]
            if not records:
                return
        columns = ', '.join(records[0].keys())
        placeholders = ', '.join(['?' for _ in records[0]])
        sql = f"INSERT OR REPLACE INTO {_q(table)} ({columns}) VALUES ({placeholders})"
        with self._connect() as conn:
            conn.executemany(sql, [tuple(r.values()) for r in records])

    def clear_table(self, table: str) -> None:
        _validate_table(table)
        if table in WORKSHOP_TABLES:
            sql = f"DELETE FROM {_q(table)} WHERE workshop = {repr(self._workshop)}"
        else:
            sql = f"DELETE FROM {_q(table)}"
        with self._connect() as conn:
            conn.execute(sql)


