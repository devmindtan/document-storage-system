import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"

print("Đang dùng database:")
print(DB_PATH)

with sqlite3.connect(DB_PATH) as conn:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_category_permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, category)
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_user_category_permissions_user_id
        ON user_category_permissions(user_id)
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_user_category_permissions_category
        ON user_category_permissions(category)
        """
    )

    tables = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        ORDER BY name
        """
    ).fetchall()

print("Các bảng hiện có:")
for table in tables:
    print("-", table[0])

print("Đã tạo xong bảng user_category_permissions.")