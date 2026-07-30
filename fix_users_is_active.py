import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"

with sqlite3.connect(DB_PATH) as conn:
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }

    if "is_active" not in columns:
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1
            """
        )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_users_is_active
        ON users(is_active)
        """
    )

print("Đã thêm cột is_active vào bảng users.")