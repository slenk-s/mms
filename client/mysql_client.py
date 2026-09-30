"""MySQL 客户端运行时入口，优先使用编译扩展并兼容旧扩展。"""
try:
    from pyd.mysql_client import MySQLClient as _CompiledMySQLClient
except ImportError:
    _CompiledMySQLClient = None

if _CompiledMySQLClient is not None and hasattr(_CompiledMySQLClient, "apply_sync_operation"):
    MySQLClient = _CompiledMySQLClient
else:
    from pyd.mysql_client_fallback import MySQLClient

__all__ = ["MySQLClient"]
