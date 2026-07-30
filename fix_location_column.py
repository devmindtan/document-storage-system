from pathlib import Path
import sqlite3

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"

with sqlite3.connect(DB_PATH) as conn:
    conn.row_factory = sqlite3.Row

    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(documents)").fetchall()
    }

    if "location" not in columns:
        conn.execute("ALTER TABLE documents ADD COLUMN location TEXT")
        print("OK: Đã thêm cột location vào bảng documents.")
    else:
        print("OK: Cột location đã tồn tại, không cần thêm nữa.")
