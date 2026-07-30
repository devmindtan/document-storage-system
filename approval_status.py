import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"

print("Đang dùng database:")
print(DB_PATH)

with sqlite3.connect(DB_PATH) as conn:
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }

    if "approval_status" not in columns:
        conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'APPROVED'
            """
        )

        print("Đã thêm cột approval_status vào bảng users.")
    else:
        print("Cột approval_status đã tồn tại.")

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_users_approval_status
        ON users(approval_status)
        """
    )

    # Đảm bảo tài khoản cũ, đặc biệt tài khoản quản lý, vẫn được duyệt sẵn
    conn.execute(
        """
        UPDATE users
        SET approval_status = 'APPROVED'
        WHERE approval_status IS NULL OR approval_status = ''
        """
    )

    columns_after = conn.execute(
        "PRAGMA table_info(users)"
    ).fetchall()

print("\nCác cột hiện có trong bảng users:")
for column in columns_after:
    print("-", column[1])

print("\nĐã sửa xong approval_status.")