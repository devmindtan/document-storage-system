import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"

print("Đang dùng database:")
print(DB_PATH)

with sqlite3.connect(DB_PATH) as conn:
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(documents)").fetchall()
    }

    if "document_code" not in columns:
        conn.execute(
            """
            ALTER TABLE documents
            ADD COLUMN document_code TEXT
            """
        )
        print("Đã thêm cột document_code vào bảng documents.")
    else:
        print("Cột document_code đã tồn tại.")

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_document_code
        ON documents(document_code)
        """
    )

    columns_after = conn.execute(
        "PRAGMA table_info(documents)"
    ).fetchall()

print("\nCác cột hiện có trong bảng documents:")
for column in columns_after:
    print("-", column[1])

print("\nĐã sửa xong document_code.")