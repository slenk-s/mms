"""Cython 编译版 - MySQL 客户端封装模块"""
from __future__ import print_function
from functools import wraps
import re
import time
from typing import List, Dict, Optional, Any, Callable
import pymysql
from pymysql.cursors import DictCursor
from pymysql.err import (
    DataError, IntegrityError, InternalError, OperationalError, ProgrammingError,
)

import config as _cfg
from .credential_manager import get_password as _get_db_pw_tuple, _get_password_raw as _get_db_pw

try:
    from logger import get_logger
except Exception:
    import logging
    get_logger = logging.getLogger

_log = get_logger(__name__)

GONE_AWAY_CODES = {2006, 2013}
HOST_BLOCKED_CODE = 1129
HOST_BLOCKED_COOLDOWN_SECONDS = 60


def _auto_retry(fn: Callable) -> Callable:
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        self._ensure_conn()
        if not self._conn:
            return None
        try:
            return fn(self, *args, **kwargs)
        except OperationalError as e:
            code = getattr(e, "args", (None,))[0]
            if code == HOST_BLOCKED_CODE:
                self._trigger_host_block_cooldown()
                return None
            if code in GONE_AWAY_CODES:
                _log.warning("MySQL 断线 (%s)，自动重试一次...", code)
                self._close_conn()
                self._connect()
                if self._conn:
                    return fn(self, *args, **kwargs)
                _log.error("MySQL 重连失败")
            raise
    return wrapper


_MYSQL_COL_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')


def _validate_mysql_columns(columns: str):
    if columns == "*":
        return
    for col in columns.split(","):
        col = col.strip()
        if not _MYSQL_COL_RE.match(col):
            raise ValueError("不安全的 MySQL 列名: %s" % col)


def _validate_mysql_order_by(order_by: str):
    if not _MYSQL_COL_RE.match(order_by):
        raise ValueError("不安全的 MySQL ORDER BY 列名: %s" % order_by)


_RECURSION_LIMIT = 3

from config import (
    TABLE_MATERIALS, TABLE_BORROW_RECORDS, TABLE_FIXED_ASSETS,
    TABLE_CONFIG_ITEMS, TABLE_USERS, TABLE_EMPLOYEE_RECORDS,
)
WORKSHOP_TABLES = {TABLE_MATERIALS, TABLE_BORROW_RECORDS, TABLE_FIXED_ASSETS}
SYNC_TABLES = WORKSHOP_TABLES | {
    TABLE_CONFIG_ITEMS, TABLE_USERS, TABLE_EMPLOYEE_RECORDS,
}
SYNC_OPERATION_LOG_TABLE = "MMS_SyncOperationLog"
RETRYABLE_MYSQL_CODES = {1040, 1205, 1213, 2002, 2003, 2006, 2013}
FAILED_MYSQL_OPERATIONAL_CODES = {1044, 1045, 1049, 1142, 1143, 1227}


class MySQLClient:
    def __init__(self):
        self._conn = None
        self._disabled = False
        self._warn_logged = False
        self._table_columns_cache = {}
        self._host_blocked_until = 0.0
        self._workshop = ""
        self._check_disabled()

    def _is_host_blocked(self) -> bool:
        if self._host_blocked_until <= 0:
            return False
        if time.time() >= self._host_blocked_until:
            self._host_blocked_until = 0.0
            return False
        return True

    def _trigger_host_block_cooldown(self):
        self._close_conn()
        self._host_blocked_until = time.time() + HOST_BLOCKED_COOLDOWN_SECONDS
        if not self._warn_logged:
            _log.error(
                "MySQL Host %s 被封锁（连接错误数超限），已进入 %ds 冷却期。"
                "请联系服务器管理员执行：mysqladmin flush-hosts "
                "或 TRUNCATE TABLE performance_schema.host_cache",
                _cfg.MYSQL_HOST, HOST_BLOCKED_COOLDOWN_SECONDS
            )
            self._warn_logged = True

    def _check_disabled(self):
        pw = _get_db_pw()
        if not _cfg.MYSQL_HOST or not _cfg.MYSQL_USER or not pw:
            self._disabled = True
            if not self._warn_logged:
                if not pw:
                    _log.warning(
                        "MySQL 密码未配置，已禁用云端同步（离线模式）。"
                        "请在系统配置中设置密码，或运行迁移工具写入密钥库"
                    )
                else:
                    _log.warning(
                        "MySQL 配置缺失（HOST/USER），已禁用云端同步（离线模式）"
                    )
                self._warn_logged = True

    def recheck_disabled(self):
        self._disabled = False
        self._warn_logged = False
        self._host_blocked_until = 0.0
        self._check_disabled()
        if not self._disabled:
            _log.info("MySQL 配置已更新，重新启用云端同步")
            self._close_conn()

    def set_workshop(self, workshop: str):
        self._workshop = workshop or ""
        _log.debug("MySQL workshop set to: %s", self._workshop)

    def _workshop_clause(self, table: str, existing_where: str, existing_params):
        if table not in WORKSHOP_TABLES or not self._workshop:
            return existing_where, existing_params
        ws_cond = "`workshop` = %s"
        if existing_where:
            new_where = existing_where + " AND " + ws_cond
            new_params = tuple(existing_params) + (self._workshop,)
        else:
            new_where = ws_cond
            new_params = (self._workshop,)
        return new_where, new_params

    def _connect(self):
        if self._disabled:
            self._conn = None
            return
        if self._is_host_blocked():
            self._conn = None
            return
        try:
            _log.debug("正在连接 MySQL: %s:%s/%s", _cfg.MYSQL_HOST, _cfg.MYSQL_PORT, _cfg.MYSQL_DATABASE)
            self._conn = pymysql.connect(
                host=_cfg.MYSQL_HOST,
                port=_cfg.MYSQL_PORT,
                user=_cfg.MYSQL_USER,
                password=_get_db_pw(),
                database=_cfg.MYSQL_DATABASE,
                charset=_cfg.MYSQL_CHARSET,
                use_unicode=True,
                init_command="SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci",
                cursorclass=DictCursor,
                autocommit=True,
                connect_timeout=3,
                read_timeout=10,
                write_timeout=10,
            )
            _log.info("MySQL 连接成功")
            self._warn_logged = False
        except OperationalError as e:
            code = getattr(e, "args", (None,))[0]
            if code == HOST_BLOCKED_CODE:
                self._trigger_host_block_cooldown()
                return
            if not self._warn_logged:
                _log.error("MySQL 连接失败: %s", e)
                self._warn_logged = True
            self._conn = None
        except (pymysql.err.ProgrammingError,
                pymysql.err.InternalError, OSError, ValueError) as e:
            if not self._warn_logged:
                _log.error("MySQL 连接失败: %s", e)
                self._warn_logged = True
            self._conn = None
        except Exception as e:
            if not self._warn_logged:
                _log.error("MySQL 连接未预期异常: %s", e)
                self._warn_logged = True
            self._conn = None

    def _close_conn(self):
        if self._conn:
            try:
                self._conn.close()
            except (pymysql.err.Error, OSError, RuntimeError) as e:
                _log.debug("关闭 MySQL 连接异常（已忽略）: %s", e)
            except Exception as e:
                _log.debug("关闭 MySQL 连接未预期异常（已忽略）: %s", e)
            finally:
                self._conn = None

    def _ensure_conn(self):
        if self._disabled:
            return
        if self._is_host_blocked():
            return
        if self._conn is None:
            self._connect()
            return
        try:
            self._conn.ping(reconnect=True)
        except OperationalError as e:
            code = getattr(e, "args", (None,))[0]
            if code == HOST_BLOCKED_CODE:
                self._trigger_host_block_cooldown()
                return
            _log.warning("MySQL ping 失败 (%s)，强制清理并重新连接...", e)
            self._close_conn()
            self._connect()

    def is_connected(self) -> bool:
        if self._disabled or self._conn is None:
            return False
        try:
            self._conn.ping(reconnect=False)
            return True
        except Exception:
            self._close_conn()
            return False

    @_auto_retry
    def fetch_all(self, table: str, columns: str = "*",
                  order_by: Optional[str] = None,
                  ascending: bool = True) -> List[Dict]:
        _validate_mysql_columns(columns)
        if order_by:
            _validate_mysql_order_by(order_by)
        sql = "SELECT %s FROM `%s`" % (columns, table)
        where, params = self._workshop_clause(table, "", ())
        if where:
            sql += " WHERE " + where
        if order_by:
            direction = "ASC" if ascending else "DESC"
            sql += " ORDER BY `%s` %s" % (order_by, direction)
        try:
            with self._conn.cursor() as cur:
                if params:
                    cur.execute(sql, params)
                else:
                    cur.execute(sql)
                return cur.fetchall() or []
        except ProgrammingError as e:
            if e.args[0] == 1146:
                _log.warning("MySQL表 '%s' 不存在，返回空列表", table)
                return []
            raise

    @_auto_retry
    def fetch_by_id(self, table: str, record_id: str):
        where = "`id` = %s"
        params = (record_id,)
        where, params = self._workshop_clause(table, where, params)
        sql = "SELECT * FROM `%s` WHERE %s" % (table, where)
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()
        except ProgrammingError as e:
            if e.args[0] == 1146:
                _log.warning("MySQL表 '%s' 不存在，返回None", table)
                return None
            raise

    @_auto_retry
    def fetch_by_condition(self, table: str, column: str, value,
                           columns: str = "*") -> List[Dict]:
        where = "`%s` = %s" % (column, "%s")
        params = (value,)
        where, params = self._workshop_clause(table, where, params)
        sql = "SELECT %s FROM `%s` WHERE %s" % (columns, table, where)
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() or []

    @_auto_retry
    def fetch_since(self, table: str, since_time: str,
                    columns: str = "*") -> List[Dict]:
        where = "`updated_at` > %s OR `created_at` > %s"
        params = (since_time, since_time)
        where, params = self._workshop_clause(table, where, params)
        sql = "SELECT %s FROM `%s` WHERE %s" % (columns, table, where)
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall() or []
        except ProgrammingError as e:
            if e.args[0] == 1146:
                _log.warning("MySQL表 '%s' 不存在，返回空列表", table)
                return []
            raise

    @_auto_retry
    def insert(self, table: str, data: Dict, _retry_count: int = 0):
        if _retry_count >= _RECURSION_LIMIT:
            _log.warning("MySQL表 '%s' 插入重试达上限，放弃", table)
            return None
        if table in WORKSHOP_TABLES and self._workshop and "workshop" not in data:
            data = dict(data)
            data["workshop"] = self._workshop
        try:
            columns = ', '.join(["`%s`" % k for k in data.keys()])
            placeholders = ', '.join(["%s"] * len(data))
            sql = "INSERT INTO `%s` (%s) VALUES (%s)" % (table, columns, placeholders)
            with self._conn.cursor() as cur:
                cur.execute(sql, tuple(data.values()))
                return data
        except (pymysql.err.Error, OSError, RuntimeError) as e:
            code = getattr(e, 'args', (None,))[0]
            if code == 1146:
                _log.warning("MySQL表 '%s' 不存在，插入失败", table)
                return None
            if code == 1062:
                _log.debug("MySQL表 '%s' 插入遇重复键(1062)，转 upsert: %s", table, e)
                return self.upsert(table, data)
            if code == 1054:
                mysql_cols = self._table_columns_cache.get(table)
                if not mysql_cols:
                    mysql_cols = self._get_table_columns(table)
                    if mysql_cols:
                        self._table_columns_cache[table] = mysql_cols
                if mysql_cols:
                    filtered = {k: v for k, v in data.items() if k in mysql_cols}
                    if filtered:
                        return self.insert(table, filtered, _retry_count + 1)
                else:
                    unknown_col = self._parse_unknown_column(str(e))
                    if unknown_col and unknown_col in data:
                        filtered = {k: v for k, v in data.items() if k != unknown_col}
                        if filtered:
                            return self.insert(table, filtered, _retry_count + 1)
                _log.debug("MySQL表 '%s' 字段过滤后无可插入数据", table)
                return None
            raise
        except Exception as e:
            _log.warning("MySQL 插入未预期异常: %s", e)
            raise

    @_auto_retry
    def update(self, table: str, record_id: str, data: Dict, _retry_count: int = 0):
        if _retry_count >= _RECURSION_LIMIT:
            _log.warning("MySQL表 '%s' 更新重试达上限，放弃", table)
            return None
        if not data:
            return None
        if table in WORKSHOP_TABLES and self._workshop and "workshop" not in data:
            data = dict(data)
            data["workshop"] = self._workshop
        try:
            sets = ', '.join(["`%s` = %s" % (k, "%s") for k in data.keys()])
            where = "`id` = %s"
            where, params = self._workshop_clause(table, where, (record_id,))
            sql = "UPDATE `%s` SET %s WHERE %s" % (table, sets, where)
            with self._conn.cursor() as cur:
                cur.execute(sql, tuple(data.values()) + params)
                return data
        except (pymysql.err.Error, OSError, RuntimeError) as e:
            code = getattr(e, 'args', (None,))[0]
            if code == 1146:
                _log.warning("MySQL表 '%s' 不存在，更新失败", table)
                return None
            if code == 1054:
                mysql_cols = self._table_columns_cache.get(table)
                if not mysql_cols:
                    mysql_cols = self._get_table_columns(table)
                    if mysql_cols:
                        self._table_columns_cache[table] = mysql_cols
                if mysql_cols:
                    filtered = {k: v for k, v in data.items() if k in mysql_cols}
                    if filtered:
                        return self.update(table, record_id, filtered, _retry_count + 1)
                else:
                    unknown_col = self._parse_unknown_column(str(e))
                    if unknown_col and unknown_col in data:
                        filtered = {k: v for k, v in data.items() if k != unknown_col}
                        if filtered:
                            return self.update(table, record_id, filtered, _retry_count + 1)
                _log.debug("MySQL表 '%s' 字段过滤后无有效更新数据", table)
                return None
            raise
        except Exception as e:
            _log.warning("MySQL 更新未预期异常: %s", e)
            raise

    @_auto_retry
    def delete(self, table: str, record_id: str) -> bool:
        sql = "DELETE FROM `%s` WHERE `id` = %s" % (table, "%s")
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, (record_id,))
                return True
        except ProgrammingError as e:
            if e.args[0] == 1146:
                _log.warning("MySQL表 '%s' 不存在，删除失败", table)
                return False
            raise

    @_auto_retry
    def upsert(self, table: str, data: Dict):
        if table in WORKSHOP_TABLES and self._workshop and "workshop" not in data:
            data = dict(data)
            data["workshop"] = self._workshop
        columns = ', '.join(["`%s`" % k for k in data.keys()])
        placeholders = ', '.join(["%s"] * len(data))
        updates = ', '.join(["`%s` = VALUES(`%s`)" % (k, k) for k in data.keys()])
        sql = "INSERT INTO `%s` (%s) VALUES (%s) ON DUPLICATE KEY UPDATE %s" % (table, columns, placeholders, updates)
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, tuple(data.values()))
                return data
        except (pymysql.err.Error, OSError, RuntimeError) as e:
            code = getattr(e, 'args', (None,))[0]
            if code == 1146:
                _log.warning("MySQL表 '%s' 不存在，upsert失败", table)
                return None
            if code == 1054:
                mysql_cols = self._table_columns_cache.get(table)
                if not mysql_cols:
                    mysql_cols = self._get_table_columns(table)
                    if mysql_cols:
                        self._table_columns_cache[table] = mysql_cols
                if mysql_cols:
                    filtered = {k: v for k, v in data.items() if k in mysql_cols}
                    if filtered:
                        return self.upsert(table, filtered)
                else:
                    unknown_col = self._parse_unknown_column(str(e))
                    if unknown_col and unknown_col in data:
                        filtered = {k: v for k, v in data.items() if k != unknown_col}
                        if filtered:
                            return self.upsert(table, filtered)
                _log.debug("MySQL表 '%s' upsert 字段过滤后无有效数据", table)
                return None
            raise
        except Exception as e:
            _log.warning("MySQL upsert 未预期异常: %s", e)
            raise

    def apply_sync_operation(self, operation_id: str, table_name: str,
                             operation: str, record_id: str, payload: Dict,
                             allow_missing: bool = False) -> str:
        """原子应用一条同步操作，并以 ``operation_id`` 保证幂等。

        业务写入与 ``MMS_SyncOperationLog`` 的成功日志处于同一个 MySQL
        事务。日志主键冲突表示此前已经提交过该操作，因此不会再次执行
        业务写入。返回值固定为 ``applied``、``already_applied``、
        ``retryable_error`` 或 ``failed_error``。
        """
        try:
            self._validate_sync_operation(
                operation_id, table_name, operation, record_id, payload
            )
        except (TypeError, ValueError) as e:
            _log.warning("同步操作校验失败: %s", e)
            return "failed_error"

        self._ensure_conn()
        if not self._conn:
            return "retryable_error"

        try:
            self._conn.begin()
            with self._conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO `%s` "
                        "(`operation_id`, `table_name`, `operation`, `record_id`, "
                        "`processed_at`, `result`) VALUES (%%s, %%s, %%s, %%s, NOW(), %%s)" %
                        SYNC_OPERATION_LOG_TABLE,
                        (operation_id, table_name, operation, record_id, "applied"),
                    )
                except IntegrityError as e:
                    if getattr(e, "args", (None,))[0] == 1062:
                        self._conn.rollback()
                        return "already_applied"
                    raise

                self._execute_sync_business_write(
                    cur, table_name, operation, record_id, payload, allow_missing
                )
            self._conn.commit()
            return "applied"
        except OperationalError as e:
            self._rollback_sync_transaction()
            code = getattr(e, "args", (None,))[0]
            if code in FAILED_MYSQL_OPERATIONAL_CODES:
                _log.warning("同步操作权限或配置错误，不可自动处理 (%s): %s", code, e)
                return "failed_error"
            _log.warning("同步操作暂时失败，将重试 (%s): %s", code, e)
            self._close_conn()
            return "retryable_error"
        except (OSError, TimeoutError, ConnectionError) as e:
            self._rollback_sync_transaction()
            _log.warning("同步操作暂时失败，将重试: %s", e)
            self._close_conn()
            return "retryable_error"
        except (ProgrammingError, DataError, IntegrityError, InternalError, ValueError) as e:
            self._rollback_sync_transaction()
            _log.warning("同步操作不可自动处理: %s", e)
            return "failed_error"
        except pymysql.err.Error as e:
            self._rollback_sync_transaction()
            code = getattr(e, "args", (None,))[0]
            if code in RETRYABLE_MYSQL_CODES:
                _log.warning("同步操作 MySQL 暂时失败 (%s): %s", code, e)
                self._close_conn()
                return "retryable_error"
            _log.warning("同步操作 MySQL 不可自动处理 (%s): %s", code, e)
            return "failed_error"
        except Exception as e:
            self._rollback_sync_transaction()
            _log.exception("同步操作未预期异常: %s", e)
            return "failed_error"

    @staticmethod
    def _validate_sync_operation(operation_id: str, table_name: str,
                                 operation: str, record_id: str, payload: Dict):
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ValueError("operation_id 不能为空")
        if table_name not in SYNC_TABLES:
            raise ValueError("不允许同步的表: %s" % table_name)
        if operation not in {"INSERT", "UPDATE", "DELETE"}:
            raise ValueError("不支持的同步操作: %s" % operation)
        if not isinstance(record_id, str) or not record_id.strip():
            raise ValueError("record_id 不能为空")
        if not isinstance(payload, dict):
            raise ValueError("payload 必须是字典")
        if "id" in payload and str(payload["id"]) != record_id:
            raise ValueError("payload.id 与 record_id 不一致")
        for column in payload:
            if not isinstance(column, str) or not _MYSQL_COL_RE.match(column):
                raise ValueError("不安全的 MySQL 列名: %s" % column)

    def _execute_sync_business_write(self, cur, table_name: str, operation: str,
                                     record_id: str, payload: Dict,
                                     allow_missing: bool = False):
        """执行严格业务写入；不将 INSERT/UPDATE 自动转换为另一种操作。"""
        if operation == "INSERT":
            data = dict(payload)
            data.setdefault("id", record_id)
            columns = ", ".join("`%s`" % column for column in data)
            placeholders = ", ".join(["%s"] * len(data))
            cur.execute(
                "INSERT INTO `%s` (%s) VALUES (%s)" %
                (table_name, columns, placeholders),
                tuple(data.values()),
            )
            return

        where = "`id` = %s"
        where, params = self._workshop_clause(table_name, where, (record_id,))
        if operation == "UPDATE":
            if not payload:
                raise ValueError("UPDATE payload 不能为空")
            cur.execute(
                "SELECT 1 FROM `%s` WHERE %s LIMIT 1" % (table_name, where),
                params,
            )
            if cur.fetchone() is None:
                if allow_missing:
                    return
                raise ValueError("UPDATE 目标记录不存在，不能自动合并")
            sets = ", ".join("`%s` = %%s" % column for column in payload)
            cur.execute(
                "UPDATE `%s` SET %s WHERE %s" % (table_name, sets, where),
                tuple(payload.values()) + params,
            )
            return

        cur.execute("DELETE FROM `%s` WHERE %s" % (table_name, where), params)

    def _rollback_sync_transaction(self):
        try:
            self._conn.rollback()
        except Exception as e:
            _log.debug("回滚同步事务失败（已忽略）: %s", e)

    def _get_table_columns(self, table: str):
        try:
            with self._conn.cursor() as cur:
                cur.execute("SHOW COLUMNS FROM `%s`" % table)
                return {row["Field"] for row in cur.fetchall()}
        except Exception as e:
            _log.debug("获取表 '%s' 列信息失败: %s", table, e)
            return set()

    @staticmethod
    def _parse_unknown_column(err_msg: str):
        match = re.search(r"Unknown column [\'\"`](.+?)[\'\"`]", err_msg)
        return match.group(1) if match else None

    @_auto_retry
    def health_check(self) -> bool:
        if not self._conn:
            return False
        with self._conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True

    def close(self):
        self._close_conn()
