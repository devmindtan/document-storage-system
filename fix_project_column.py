import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"

STORAGE_DIR = BASE_DIR / "storage"

PROJECT_MAP = {
    "1": {
        "label": "Project 1",
        "folder": "project_1",
    },
    "2": {
        "label": "Project 2",
        "folder": "project_2",
    },
    "3": {
        "label": "Project 3",
        "folder": "project_3",
    },
}

CATEGORY_MAP = {
    "1": {
        "label": "File loại 1",
        "folder": "file_loai_1",
    },
    "2": {
        "label": "File loại 2",
        "folder": "file_loai_2",
    },
    "3": {
        "label": "File loại 3",
        "folder": "file_loai_3",
    },
}

print("Đang dùng database:")
print(DB_PATH)

# Tạo thư mục project/file loại nếu chưa có
for project in PROJECT_MAP.values():
    for category in CATEGORY_MAP.values():
        folder = STORAGE_DIR / project["folder"] / category["folder"]
        folder.mkdir(parents=True, exist_ok=True)

with sqlite3.connect(DB_PATH) as conn:
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(documents)").fetchall()
    }

    if "project" not in columns:
        conn.execute(
            """
            ALTER TABLE documents
            ADD COLUMN project TEXT DEFAULT 'Project 1'
            """
        )
        print("Đã thêm cột project vào bảng documents.")
    else:
        print("Cột project đã tồn tại.")

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_documents_project
        ON documents(project)
        """
    )

    # Gán Project 1 cho các hồ sơ cũ nếu bị trống
    conn.execute(
        """
        UPDATE documents
        SET project = 'Project 1'
        WHERE project IS NULL OR project = ''
        """
    )

    columns_after = conn.execute(
        "PRAGMA table_info(documents)"
    ).fetchall()

print("\nCác cột hiện có trong bảng documents:")
for column in columns_after:
    print("-", column[1])

print("\nĐã sửa xong cột project.")