from datetime import datetime

from core.config import (
    CATEGORY_MAP,
    PROJECT_MAP,
    STORAGE_DIR,
    PENDING_DIR,
    REJECTED_DIR,
    DOWNLOADS_DIR,
)
from database.connection import get_connection
from services.projects import (
    get_canonical_category_project_id,
    seed_project_default_categories,
)


def initialize_projects():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL UNIQUE,
                folder TEXT NOT NULL UNIQUE,
                project_number INTEGER NOT NULL UNIQUE,
                project_code TEXT UNIQUE,
                status TEXT NOT NULL DEFAULT 'APPROVED',
                requested_by_user_id INTEGER,
                requested_by TEXT,
                requested_at TEXT NOT NULL,
                reviewed_by TEXT,
                reviewed_at TEXT,
                rejection_reason TEXT
            )
        """)

        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(projects)"
            ).fetchall()
        }

        if "project_code" not in columns:
            conn.execute("""
                ALTER TABLE projects
                ADD COLUMN project_code TEXT
            """)

        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_project_code
            ON projects(project_code)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_projects_status
            ON projects(status)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_projects_project_number
            ON projects(project_number)
        """)

        default_projects = {
            "1": {
                "label": "Project 1",
                "folder": "project_1",
                "project_code": "PROJ-001",
            },
            "2": {
                "label": "Project 2",
                "folder": "project_2",
                "project_code": "PROJ-002",
            },
            "3": {
                "label": "Project 3",
                "folder": "project_3",
                "project_code": "PROJ-003",
            },
        }

        for key, project in default_projects.items():
            conn.execute("""
                INSERT OR IGNORE INTO projects (
                    label,
                    folder,
                    project_number,
                    project_code,
                    status,
                    requested_by_user_id,
                    requested_by,
                    requested_at,
                    reviewed_by,
                    reviewed_at,
                    rejection_reason
                )
                VALUES (?, ?, ?, ?, 'APPROVED', NULL, 'SYSTEM', ?, 'SYSTEM', ?, NULL)
            """, (
                project["label"],
                project["folder"],
                int(key),
                project["project_code"],
                now,
                now,
            ))

        conn.execute("""
            UPDATE projects
            SET project_code = 'PROJ-001'
            WHERE label = 'Project 1'
              AND project_code IS NULL
        """)

        conn.execute("""
            UPDATE projects
            SET project_code = 'PROJ-002'
            WHERE label = 'Project 2'
              AND project_code IS NULL
        """)

        conn.execute("""
            UPDATE projects
            SET project_code = 'PROJ-003'
            WHERE label = 'Project 3'
              AND project_code IS NULL
        """)

        approved_projects = conn.execute("""
            SELECT label, folder
            FROM projects
            WHERE status = 'APPROVED'
            ORDER BY project_number ASC
        """).fetchall()

    for project in approved_projects:
        project_folder = STORAGE_DIR / project["folder"]
        project_folder.mkdir(parents=True, exist_ok=True)

        for category in CATEGORY_MAP.values():
            category_folder = project_folder / category["folder"]
            category_folder.mkdir(parents=True, exist_ok=True)


def initialize_project_categories():
    """
    Tạo bảng file con / loại hồ sơ theo từng project.

    Mặc định mỗi project được seed các loại hồ sơ cũ trong CATEGORY_MAP.
    Sau đó quản lý có thể ẩn/xóa bớt hoặc thêm loại mới cho từng project.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS project_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                category_key TEXT NOT NULL,
                label TEXT NOT NULL,
                folder TEXT NOT NULL,
                code TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE(project_id, category_key)
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_project_categories_project_id
            ON project_categories(project_id)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_project_categories_active
            ON project_categories(is_active)
            """
        )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_project_categories_label
            ON project_categories(project_id, label)
            """
        )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_project_categories_folder
            ON project_categories(project_id, folder)
            """
        )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_project_categories_code
            ON project_categories(project_id, code)
            """
        )

        approved_projects = conn.execute(
            """
            SELECT id
            FROM projects
            WHERE status = 'APPROVED'
            """
        ).fetchall()

        for project in approved_projects:
            seed_project_default_categories(
                conn=conn,
                project_id=project["id"],
                created_at=now,
            )

        consolidate_categories_to_canonical_project(conn)


def consolidate_categories_to_canonical_project(conn):
    """
    "Loại hồ sơ" dùng chung cho toàn hệ thống được lưu dưới project_id ảo
    (xem get_canonical_category_project_id) — KHÔNG thuộc project thật nào,
    nên các dòng project_categories seed riêng cho từng project thật ở trên
    (phục vụ tạo thư mục lưu trữ vật lý của project đó, xem
    create_project_storage_folders) được giữ nguyên, không đụng tới.

    Lịch sử: bản trước dùng project có id nhỏ nhất làm canonical với giả
    định "project không có tính năng xóa" — giả định đó sai, xóa đúng
    project đó qua /projects/{id}/delete đã xóa sạch luôn toàn bộ danh sách
    Loại hồ sơ dùng chung. Idempotent — chỉ seed lại nếu project_id ảo chưa
    có dữ liệu, chạy lại mỗi lần khởi động không gây tác dụng phụ.
    """
    canonical_id = get_canonical_category_project_id(conn)

    canonical_has_rows = conn.execute(
        "SELECT COUNT(*) AS total FROM project_categories WHERE project_id = ?",
        (canonical_id,),
    ).fetchone()["total"] > 0

    if canonical_has_rows:
        return

    # Gộp trạng thái bật/tắt hiện có ở các project thật (nếu có, từ dữ liệu
    # cũ) trước khi seed mặc định, để không làm mất loại hồ sơ ai đó đã bật.
    active_keys = {
        row["category_key"]
        for row in conn.execute(
            "SELECT DISTINCT category_key FROM project_categories WHERE is_active = 1"
        ).fetchall()
    }

    seed_project_default_categories(conn=conn, project_id=canonical_id)

    if active_keys:
        conn.executemany(
            """
            UPDATE project_categories
            SET is_active = 1
            WHERE project_id = ? AND category_key = ?
            """,
            [(canonical_id, key) for key in active_keys],
        )


def initialize_audit_logs():
    """
    Tạo bảng nhật ký hoạt động nếu bảng chưa tồn tại.
    """

    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                action TEXT NOT NULL,
                document_id INTEGER,
                details TEXT,
                created_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_audit_logs_document_id
            ON audit_logs(document_id)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at
            ON audit_logs(created_at)
            """
        )


# Chạy khi website khởi động để tạo bảng audit_logs.


def initialize_user_status():
    """
    Thêm cột is_active vào bảng users nếu database cũ chưa có.

    is_active = 1: tài khoản đang hoạt động
    is_active = 0: tài khoản đã bị khóa
    """

    with get_connection() as conn:
        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(users)"
            ).fetchall()
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


def initialize_user_sessions():
    """
    Tạo bảng lưu session đăng nhập riêng cho từng máy/trình duyệt.
    Mỗi lần login sẽ tạo một session_token riêng.
    """

    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_token TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                username TEXT,
                created_at TEXT NOT NULL,
                last_seen_at TEXT,
                user_agent TEXT,
                client_ip TEXT
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_sessions_token
            ON user_sessions(session_token)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_sessions_user_id
            ON user_sessions(user_id)
        """)


def initialize_admin_status():
            """
            Thêm cột is_admin vào bảng users nếu database cũ chưa có.

            is_admin = 1: tài khoản admin
            is_admin = 0: tài khoản thường
            """

            with get_connection() as conn:
                columns = {
                    row["name"]
                    for row in conn.execute(
                        "PRAGMA table_info(users)"
                    ).fetchall()
                }

                if "is_admin" not in columns:
                    conn.execute(
                        """
                        ALTER TABLE users
                            ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0
                        """
                    )

                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_users_is_admin
                        ON users(is_admin)
                    """
                )


def initialize_user_approval_status():
    """
    Thêm cột approval_status vào bảng users nếu database cũ chưa có.

    approval_status:
    - PENDING: chờ quản lý duyệt
    - APPROVED: đã được duyệt
    - REJECTED: bị từ chối
    """

    with get_connection() as conn:
        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(users)"
            ).fetchall()
        }

        if "approval_status" not in columns:
            conn.execute(
                """
                ALTER TABLE users
                ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'APPROVED'
                """
            )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_users_approval_status
            ON users(approval_status)
            """
        )


def initialize_user_category_permissions():
    """
    Thêm cột category_permissions_enabled vào bảng users và tạo bảng
    user_category_permissions nếu database cũ chưa có.

    category_permissions_enabled = 0 (mặc định): không giới hạn, nhân
    viên upload được vào mọi loại hồ sơ như trước giờ.
    category_permissions_enabled = 1: chỉ được upload vào các loại hồ
    sơ có trong user_category_permissions (có thể là rỗng = bị chặn
    hoàn toàn, do admin chủ động chọn).
    """

    with get_connection() as conn:
        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(users)"
            ).fetchall()
        }

        if "category_permissions_enabled" not in columns:
            conn.execute(
                """
                ALTER TABLE users
                ADD COLUMN category_permissions_enabled INTEGER NOT NULL DEFAULT 0
                """
            )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_category_permissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category_label TEXT NOT NULL,
                UNIQUE(user_id, category_label)
            )
        """)


def initialize_storage_and_documents():
    """
    Tạo thư mục lưu file và bảng documents nếu chưa tồn tại.

    Hàm này chạy an toàn nhiều lần.
    """

    PENDING_DIR.mkdir(exist_ok=True)
    STORAGE_DIR.mkdir(exist_ok=True)
    REJECTED_DIR.mkdir(exist_ok=True)
    DOWNLOADS_DIR.mkdir(exist_ok=True)

    for project in PROJECT_MAP.values():
        project_folder = STORAGE_DIR / project["folder"]
        project_folder.mkdir(parents=True, exist_ok=True)

        for category in CATEGORY_MAP.values():
            category_folder = project_folder / category["folder"]
            category_folder.mkdir(parents=True, exist_ok=True)

    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                status TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                submitted_by TEXT NOT NULL,
                submitted_by_user_id INTEGER,
                reviewed_at TEXT,
                reviewed_by TEXT,
                rejection_reason TEXT
            )
            """
        )

        # Nếu database cũ chưa có cột này, hệ thống tự bổ sung.
        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(documents)"
            ).fetchall()
        }

        if "submitted_by_user_id" not in columns:
            conn.execute(
                """
                ALTER TABLE documents
                ADD COLUMN submitted_by_user_id INTEGER
                """
            )

        if "project" not in columns:
            conn.execute(
                """
                ALTER TABLE documents
                    ADD COLUMN project TEXT DEFAULT 'Project 1'
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_documents_project
                    ON documents(project)
                """
            )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_documents_status
            ON documents(status)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_documents_submitted_by_user_id
            ON documents(submitted_by_user_id)
            """
        )



        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at
            ON audit_logs(created_at)
            """
        )
        if "document_code" not in columns:
            conn.execute(
                """
                ALTER TABLE documents
                    ADD COLUMN document_code TEXT
                """
            )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_documents_project
                ON documents(project)
            """
        )

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_document_code
                ON documents(document_code)
            """
        )
        if "description" not in columns:
            conn.execute(
                """
                ALTER TABLE documents
                ADD COLUMN description TEXT
                """
            )

        if "location" not in columns:
            conn.execute(
                """
                ALTER TABLE documents
                ADD COLUMN location TEXT
                """
            )


        if "requested_new_project" not in columns:
            conn.execute(
                """
                ALTER TABLE documents
                ADD COLUMN requested_new_project INTEGER NOT NULL DEFAULT 0
                """
            )

        if "requested_new_category" not in columns:
            conn.execute(
                """
                ALTER TABLE documents
                ADD COLUMN requested_new_category INTEGER NOT NULL DEFAULT 0
                """
            )

        if "requested_category_code" not in columns:
            conn.execute(
                """
                ALTER TABLE documents
                ADD COLUMN requested_category_code TEXT
                """
            )


