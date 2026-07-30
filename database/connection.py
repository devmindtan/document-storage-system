import sqlite3

from core.config import DB_PATH


def get_connection():
    """
    Mở kết nối SQLite.

    timeout=10 cho phép SQLite chờ tối đa 10 giây
    nếu database đang bận ghi dữ liệu.
    """

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row

    # Chờ tối đa 10 giây khi database đang bận.
    conn.execute("PRAGMA busy_timeout = 10000")

    return conn
