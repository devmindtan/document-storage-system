import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data.db"


with sqlite3.connect(DB_PATH) as conn:
    conn.row_factory = sqlite3.Row

    project = conn.execute("""
        SELECT id, label, project_code, status
        FROM projects
        WHERE lower(label) = lower(?)
        ORDER BY id DESC
        LIMIT 1
    """, ("SOLAR",)).fetchone()

    if not project:
        print("Không tìm thấy project SOLAR trong database.")
        raise SystemExit

    existing_code = conn.execute("""
        SELECT id, label, status
        FROM projects
        WHERE project_code = ?
          AND id != ?
    """, ("PROJ-SOL", project["id"])).fetchone()

    if existing_code:
        print("Không thể đổi sang PROJ-SOL vì mã này đang thuộc project khác:")
        print(dict(existing_code))
        raise SystemExit

    conn.execute("""
        UPDATE projects
        SET label = 'SOLAR',
            project_code = 'PROJ-SOL',
            status = 'APPROVED'
        WHERE id = ?
    """, (project["id"],))

    print("Đã sửa project SOLAR thành mã PROJ-SOL thành công.")