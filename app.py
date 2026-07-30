from pathlib import Path
from typing import List
import sqlite3
import shutil
import uuid
import json
import secrets
import re
import unicodedata
import mimetypes
import os
import stat
import time
from urllib.parse import quote
from datetime import datetime

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from core.config import (
    BASE_DIR,
    DB_PATH,
    TEMPLATES_DIR,
    STATIC_DIR,
    LOCAL_STORAGE_DIR,
    PENDING_DIR,
    STORAGE_DIR,
    REJECTED_DIR,
    DOWNLOADS_DIR,
    MAX_FILE_SIZE_MB,
    ALLOWED_EXTENSIONS,
    CATEGORY_MAP,
    PROJECT_MAP,
    PASSWORD_ITERATIONS,
    ROLE_EMPLOYEE,
    ROLE_MANAGER,
    NEW_PROJECT_KEY,
    NEW_CATEGORY_KEY,
    DEFAULT_CATEGORY_PREFIX,
    SESSION_SECRET_KEY,
)
from database.connection import get_connection
from core.security import hash_password, verify_password


# ============================================================
# KHỞI TẠO FASTAPI
# ============================================================

app = FastAPI()

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
)

app.mount(
    "/static",
    StaticFiles(directory=STATIC_DIR),
    name="static"
)

templates = Jinja2Templates(directory=TEMPLATES_DIR)


def make_project_code_from_project_name(project_name):
    """
    Tạo mã project từ 3 ký tự đầu của tên project.

    Ví dụ:
    Alpha Project -> PROJ-ALP
    Business Expansion -> PROJ-BUS
    """

    text = project_name.strip().upper()

    normalized = unicodedata.normalize("NFD", text)

    text_without_accents = "".join(
        char
        for char in normalized
        if unicodedata.category(char) != "Mn"
    )

    only_letters_numbers = re.sub(
        r"[^A-Z0-9]+",
        "",
        text_without_accents,
    )

    if len(only_letters_numbers) < 3:
        only_letters_numbers = only_letters_numbers.ljust(3, "X")

    code = only_letters_numbers[:3]

    return f"PROJ-{code}"


def check_project_code_is_available(conn, project_code):
    """
    Kiểm tra mã project đã tồn tại chưa.

    Nếu mã đang nằm trong project đã bị xóa mềm từ phiên bản cũ
    thì tự gỡ project_code để mã đó được dùng lại.
    """

    conn.execute(
        """
        UPDATE projects
        SET project_code = NULL
        WHERE project_code = ?
          AND status = 'DELETED'
        """,
        (project_code,),
    )

    existing = conn.execute(
        """
        SELECT id
        FROM projects
        WHERE project_code = ?
          AND status != 'DELETED'
        """,
        (project_code,),
    ).fetchone()

    return existing is None
def make_folder_name_from_project_name(project_name):
    text = project_name.strip().lower()

    normalized = unicodedata.normalize("NFD", text)

    text_without_accents = "".join(
        char
        for char in normalized
        if unicodedata.category(char) != "Mn"
    )

    folder_name = re.sub(
        r"[^a-zA-Z0-9]+",
        "_",
        text_without_accents,
    )

    folder_name = folder_name.strip("_").lower()

    if not folder_name:
        folder_name = f"project_{uuid.uuid4().hex[:8]}"

    return folder_name


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

def get_approved_projects():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT
                id,
                label,
                folder,
                project_number,
                project_code
            FROM projects
            WHERE status = 'APPROVED'
            ORDER BY project_number ASC
        """).fetchall()

    projects = {}

    for row in rows:
        projects[str(row["id"])] = {
            "label": row["label"],
            "folder": row["folder"],
            "project_number": row["project_number"],
            "project_code": row["project_code"],
        }

    return projects


def get_approved_project_by_key(project_key):
    try:
        project_id = int(project_key)
    except ValueError:
        return None

    with get_connection() as conn:
        project = conn.execute("""
            SELECT
                id,
                label,
                folder,
                project_number,
                project_code
            FROM projects
            WHERE id = ?
              AND status = 'APPROVED'
        """, (project_id,)).fetchone()

    return project



def get_default_project_key(projects: dict) -> str:
    """
    Lấy project đầu tiên để làm mặc định cho dropdown upload.
    """
    for key in projects.keys():
        return key
    return ""


def get_default_category_key(categories: dict) -> str:
    """
    Lấy file con đầu tiên để làm mặc định cho dropdown loại hồ sơ.
    """
    for key in categories.keys():
        return key
    return ""


def make_category_folder_from_label(category_label: str) -> str:
    """
    Tạo tên folder an toàn từ tên file con / loại hồ sơ.
    """
    text = category_label.strip().lower()
    normalized = unicodedata.normalize("NFD", text)

    text_without_accents = "".join(
        char
        for char in normalized
        if unicodedata.category(char) != "Mn"
    )

    folder_name = re.sub(
        r"[^a-zA-Z0-9]+",
        "_",
        text_without_accents,
    )

    folder_name = folder_name.strip("_").lower()

    if not folder_name:
        folder_name = f"category_{uuid.uuid4().hex[:8]}"

    return folder_name


def make_category_code_from_label(category_label: str) -> str:
    """
    Tạo mã file con từ tên file con.

    Ví dụ:
    Inspection Report -> IR
    Quality Check -> QC
    """
    text = category_label.strip().upper()
    normalized = unicodedata.normalize("NFD", text)

    text_without_accents = "".join(
        char
        for char in normalized
        if unicodedata.category(char) != "Mn"
    )

    words = re.findall(r"[A-Z0-9]+", text_without_accents)

    if not words:
        return f"C{uuid.uuid4().hex[:4].upper()}"

    if len(words) >= 2:
        code = "".join(word[0] for word in words)
    else:
        code = words[0][:6]

    code = re.sub(r"[^A-Z0-9]+", "", code)

    if len(code) < 2:
        code = code.ljust(2, "X")

    return code[:10]


def make_unique_category_folder(conn, project_id: int, category_label: str) -> str:
    """
    Tạo folder không trùng trong cùng project.
    """
    base_folder = make_category_folder_from_label(category_label)
    folder_name = base_folder
    counter = 2

    while True:
        existing = conn.execute(
            """
            SELECT id
            FROM project_categories
            WHERE project_id = ?
              AND folder = ?
            """,
            (project_id, folder_name),
        ).fetchone()

        if not existing:
            return folder_name

        folder_name = f"{base_folder}_{counter}"
        counter += 1


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


def seed_project_default_categories(conn, project_id: int, created_at: str | None = None):
    """
    Seed danh sách file con mặc định cho một project.
    """
    if created_at is None:
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for category_key, category in CATEGORY_MAP.items():
        conn.execute(
            """
            INSERT OR IGNORE INTO project_categories (
                project_id,
                category_key,
                label,
                folder,
                code,
                is_active,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (
                project_id,
                category_key,
                category["label"],
                category["folder"],
                category["code"],
                created_at,
            ),
        )


def get_active_categories_for_project_key(project_key: str) -> dict:
    """
    Lấy danh sách file con đang bật của một project.
    Key trả về là id của project_categories.
    """
    try:
        project_id = int(project_key)
    except (TypeError, ValueError):
        return {}

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                category_key,
                label,
                folder,
                code
            FROM project_categories
            WHERE project_id = ?
              AND is_active = 1
            ORDER BY label ASC
            """,
            (project_id,),
        ).fetchall()

    categories = {}

    for row in rows:
        categories[str(row["id"])] = {
            "id": row["id"],
            "category_key": row["category_key"],
            "label": row["label"],
            "folder": row["folder"],
            "code": row["code"],
        }

    return categories


def get_all_active_categories() -> dict:
    """
    Lấy tất cả tên file con đang bật để dùng ở trang lọc Approved Documents.
    Loại trùng tên sẽ chỉ hiện một lần.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT label
            FROM project_categories
            WHERE is_active = 1
            ORDER BY label ASC
            """
        ).fetchall()

    categories = {}

    for row in rows:
        categories[row["label"]] = {
            "label": row["label"],
        }

    return categories


def get_project_category_by_key(category_key: str, project_id: int):
    """
    Lấy file con theo id và project.
    Chỉ dùng file con đang active khi upload.
    """
    try:
        category_id = int(category_key)
    except (TypeError, ValueError):
        return None

    with get_connection() as conn:
        category = conn.execute(
            """
            SELECT
                id,
                project_id,
                category_key,
                label,
                folder,
                code,
                is_active
            FROM project_categories
            WHERE id = ?
              AND project_id = ?
              AND is_active = 1
            """,
            (category_id, project_id),
        ).fetchone()

    return category


def get_project_category_json_list(project_id: int) -> list[dict]:
    """
    Dữ liệu JSON cho dropdown loại hồ sơ theo project.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                label,
                folder,
                code
            FROM project_categories
            WHERE project_id = ?
              AND is_active = 1
            ORDER BY label ASC
            """,
            (project_id,),
        ).fetchall()

    return [
        {
            "id": row["id"],
            "label": row["label"],
            "folder": row["folder"],
            "code": row["code"],
        }
        for row in rows
    ]


def get_category_management_rows(project_key: str) -> list:
    """
    Lấy file con đang active để hiển thị trong khu vực quản lý file con.
    """
    try:
        project_id = int(project_key)
    except (TypeError, ValueError):
        return []

    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                label,
                folder,
                code
            FROM project_categories
            WHERE project_id = ?
              AND is_active = 1
            ORDER BY label ASC
            """,
            (project_id,),
        ).fetchall()

    return rows


def create_project_category_for_manager(
    project_key: str,
    category_label: str,
    category_code: str,
    current_user
):
    """
    Quản lý thêm file con / loại hồ sơ mới vào một project.
    """
    try:
        project_id = int(project_key)
    except (TypeError, ValueError):
        return False, "Project không hợp lệ."

    category_label = category_label.strip()
    category_code = category_code.strip().upper()

    if not category_label:
        return False, "Tên file con không được để trống."

    if len(category_label) > 50:
        return False, "Tên file con không được vượt quá 50 ký tự."

    if category_code:
        category_code = re.sub(r"[^A-Z0-9]+", "", category_code)

    if not category_code:
        category_code = make_category_code_from_label(category_label)

    if len(category_code) > 10:
        return False, "Mã file con không được vượt quá 10 ký tự."

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as conn:
        project = conn.execute(
            """
            SELECT
                id,
                label,
                folder
            FROM projects
            WHERE id = ?
              AND status = 'APPROVED'
            """,
            (project_id,),
        ).fetchone()

        if not project:
            return False, "Không tìm thấy project đang hoạt động."

        existing_label = conn.execute(
            """
            SELECT id, is_active
            FROM project_categories
            WHERE project_id = ?
              AND lower(label) = lower(?)
            """,
            (project_id, category_label),
        ).fetchone()

        if existing_label:
            if existing_label["is_active"]:
                return False, "File con này đã tồn tại trong project."

            conn.execute(
                """
                UPDATE project_categories
                SET is_active = 1,
                    code = ?
                WHERE id = ?
                """,
                (category_code, existing_label["id"]),
            )

            write_audit_log(
                user=current_user,
                action="RESTORE_PROJECT_CATEGORY",
                details=(
                    f"Bật lại file con '{category_label}' trong project "
                    f"'{project['label']}'."
                ),
            )

            create_project_storage_folders(project["folder"])

            return True, "Đã bật lại file con thành công."

        existing_code = conn.execute(
            """
            SELECT id
            FROM project_categories
            WHERE project_id = ?
              AND code = ?
            """,
            (project_id, category_code),
        ).fetchone()

        if existing_code:
            return False, "Mã file con này đã tồn tại trong project."

        folder_name = make_unique_category_folder(
            conn=conn,
            project_id=project_id,
            category_label=category_label,
        )

        category_key = f"CUSTOM_{uuid.uuid4().hex[:12]}"

        cursor = conn.execute(
            """
            INSERT INTO project_categories (
                project_id,
                category_key,
                label,
                folder,
                code,
                is_active,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (
                project_id,
                category_key,
                category_label,
                folder_name,
                category_code,
                now,
            ),
        )

        category_id = cursor.lastrowid

    create_project_storage_folders(project["folder"])

    write_audit_log(
        user=current_user,
        action="CREATE_PROJECT_CATEGORY",
        details=(
            f"Thêm file con '{category_label}' "
            f"mã '{category_code}' vào project '{project['label']}'."
        ),
    )

    return True, f"Đã thêm file con '{category_label}' thành công."


def delete_project_category_for_manager(
    category_id: int,
    project_key: str,
    current_user
):
    """
    Quản lý xóa/ẩn file con khỏi đúng một project.

    Bắt buộc kiểm tra cả category_id và project_key để tránh trường hợp
    xóa nhầm file con có cùng tên/mã ở project khác.
    """
    try:
        project_id_from_form = int(project_key)
    except (TypeError, ValueError):
        return False, "Vui lòng chọn project trước khi xóa file con.", ""

    with get_connection() as conn:
        category = conn.execute(
            """
            SELECT
                pc.id,
                pc.label,
                pc.project_id,
                pc.is_active,
                p.label AS project_label
            FROM project_categories pc
            JOIN projects p ON p.id = pc.project_id
            WHERE pc.id = ?
              AND pc.project_id = ?
              AND p.status = 'APPROVED'
            """,
            (category_id, project_id_from_form),
        ).fetchone()

        if not category:
            return False, "File con không thuộc project đang chọn hoặc đã bị xóa.", str(project_id_from_form)

        active_count = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM project_categories
            WHERE project_id = ?
              AND is_active = 1
            """,
            (project_id_from_form,),
        ).fetchone()["total"]

        if active_count <= 1:
            return False, "Project phải còn ít nhất 1 file con.", str(project_id_from_form)

        conn.execute(
            """
            UPDATE project_categories
            SET is_active = 0
            WHERE id = ?
              AND project_id = ?
            """,
            (category_id, project_id_from_form),
        )

    write_audit_log(
        user=current_user,
        action="DELETE_PROJECT_CATEGORY",
        details=(
            f"Ẩn file con '{category['label']}' khỏi project "
            f"'{category['project_label']}'."
        ),
    )

    return True, "Đã xóa file con khỏi project đang chọn.", str(project_id_from_form)


def get_default_categories_for_new_project() -> dict:
    """
    Danh sách file con mặc định dùng khi user chọn tạo project mới.
    Key trả về có prefix đặc biệt để backend nhận biết đây là file con mặc định.
    """

    categories = {}

    for category_key, category in CATEGORY_MAP.items():
        option_key = f"{DEFAULT_CATEGORY_PREFIX}{category_key}"

        categories[option_key] = {
            "id": option_key,
            "category_key": category_key,
            "label": category["label"],
            "folder": category["folder"],
            "code": category["code"],
        }

    return categories


def build_upload_context(
    current_user,
    error=None,
    success=None,
    selected_project_key: str = "",
    selected_category_key: str = "",
    description_value: str = "",
    location_value: str = "",
    new_project_name_value: str = "",
    new_category_name_value: str = "",
    new_category_code_value: str = "",
):
    """
    Context chung cho trang upload nhân viên.
    """
    projects = get_approved_projects()

    if selected_project_key == NEW_PROJECT_KEY:
        categories = get_default_categories_for_new_project()
    else:
        if not selected_project_key or selected_project_key not in projects:
            selected_project_key = get_default_project_key(projects)

        categories = get_active_categories_for_project_key(selected_project_key)

    if selected_category_key == NEW_CATEGORY_KEY:
        pass
    elif not selected_category_key or selected_category_key not in categories:
        selected_category_key = get_default_category_key(categories)

    return {
        "user": current_user,
        "projects": projects,
        "categories": categories,
        "error": error,
        "success": success,
        "selected_project_key": selected_project_key,
        "selected_category_key": selected_category_key,
        "description_value": description_value or "",
        "location_value": location_value or "",
        "new_project_name_value": new_project_name_value or "",
        "new_category_name_value": new_category_name_value or "",
        "new_category_code_value": new_category_code_value or "",
        "new_project_key": NEW_PROJECT_KEY,
        "new_category_key": NEW_CATEGORY_KEY,
        "default_category_prefix": DEFAULT_CATEGORY_PREFIX,
    }


def build_manager_upload_context(
    current_user,
    error=None,
    success=None,
    selected_project_key: str = "",
    selected_category_key: str = "",
    description_value: str = "",
    location_value: str = "",
    new_project_name_value: str = "",
    new_category_name_value: str = "",
    new_category_code_value: str = "",
    manage_project_key: str = "",
):
    """
    Context cho trang upload quản lý, gồm thêm khu vực quản lý file con.
    """
    context = build_upload_context(
        current_user=current_user,
        error=error,
        success=success,
        selected_project_key=selected_project_key,
        selected_category_key=selected_category_key,
        description_value=description_value,
        location_value=location_value,
        new_project_name_value=new_project_name_value,
        new_category_name_value=new_category_name_value,
        new_category_code_value=new_category_code_value,
    )

    projects = context["projects"]

    if manage_project_key == NEW_PROJECT_KEY:
        manage_project_key = ""

    if not manage_project_key or manage_project_key not in projects:
        manage_project_key = get_default_project_key(projects)

    context["category_management_project_key"] = manage_project_key
    context["project_category_rows"] = get_category_management_rows(manage_project_key)

    return context


def get_next_project_number(conn):
    row = conn.execute("""
        SELECT COALESCE(MAX(project_number), 0) AS max_project_number
        FROM projects
    """).fetchone()

    return int(row["max_project_number"]) + 1


def make_unique_project_folder(conn, project_name):
    base_folder = make_folder_name_from_project_name(project_name)
    folder_name = base_folder
    counter = 2

    while True:
        existing = conn.execute("""
            SELECT id
            FROM projects
            WHERE folder = ?
        """, (folder_name,)).fetchone()

        if not existing:
            return folder_name

        folder_name = f"{base_folder}_{counter}"
        counter += 1


def create_project_storage_folders(project_folder_name):
    project_folder = STORAGE_DIR / project_folder_name
    project_folder.mkdir(parents=True, exist_ok=True)

    category_rows = []

    try:
        with get_connection() as conn:
            project = conn.execute(
                """
                SELECT id
                FROM projects
                WHERE folder = ?
                """,
                (project_folder_name,),
            ).fetchone()

            if project:
                seed_project_default_categories(
                    conn=conn,
                    project_id=project["id"],
                )

                category_rows = conn.execute(
                    """
                    SELECT folder
                    FROM project_categories
                    WHERE project_id = ?
                      AND is_active = 1
                    """,
                    (project["id"],),
                ).fetchall()
    except sqlite3.OperationalError:
        category_rows = []

    if category_rows:
        for category in category_rows:
            category_folder = project_folder / category["folder"]
            category_folder.mkdir(parents=True, exist_ok=True)
    else:
        for category in CATEGORY_MAP.values():
            category_folder = project_folder / category["folder"]
            category_folder.mkdir(parents=True, exist_ok=True)

def request_new_project_from_employee(project_name, current_user):
    project_name = project_name.strip().upper()

    if not project_name:
        return False, "Tên project không được để trống."

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as conn:
        project_code = make_project_code_from_project_name(project_name)

        cleanup_deleted_project_records(
            conn=conn,
            project_name=project_name,
            project_code=project_code,
        )

        existing = conn.execute("""
            SELECT id, status
            FROM projects
            WHERE lower(label) = lower(?)
        """, (project_name,)).fetchone()

        if existing:
            if existing["status"] == "APPROVED":
                return False, "Project này đã tồn tại và đã được duyệt."

            if existing["status"] == "PENDING":
                return False, "Project này đang chờ quản lý duyệt."

            if existing["status"] == "REJECTED":
                return False, "Project này đã từng bị từ chối."

        if not check_project_code_is_available(conn, project_code):
            return False, (
                f"Mã project {project_code} đã tồn tại. "
                f"Vui lòng đổi tên project để tạo mã khác."
            )

        folder_name = make_unique_project_folder(conn, project_name)
        project_number = get_next_project_number(conn)

        conn.execute("""
                     INSERT INTO projects (label,
                                           folder,
                                           project_number,
                                           project_code,
                                           status,
                                           requested_by_user_id,
                                           requested_by,
                                           requested_at,
                                           reviewed_by,
                                           reviewed_at,
                                           rejection_reason)
                     VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?, NULL, NULL, NULL)
                     """, (
                         project_name,
                         folder_name,
                         project_number,
                         project_code,
                         current_user["id"],
                         current_user["full_name"],
                         now,
                     ))

    write_audit_log(
        user=current_user,
        action="REQUEST_PROJECT",
        details=f"Yêu cầu tạo project mới: '{project_name}'.",
    )

    return True, "Đã gửi yêu cầu tạo project mới. Vui lòng chờ quản lý duyệt."

def create_project_immediately_by_manager(project_name, current_user):
    project_name = project_name.strip().upper()

    if not project_name:
        return False, "Tên project không được để trống."

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as conn:
        project_code = make_project_code_from_project_name(project_name)

        cleanup_deleted_project_records(
            conn=conn,
            project_name=project_name,
            project_code=project_code,
        )

        existing = conn.execute("""
            SELECT id, status
            FROM projects
            WHERE lower(label) = lower(?)
        """, (project_name,)).fetchone()

        if existing:
            if existing["status"] == "APPROVED":
                return False, "Project này đã tồn tại."

            if existing["status"] == "PENDING":
                return False, "Project này đang chờ duyệt."

            if existing["status"] == "REJECTED":
                return False, "Project này đã từng bị từ chối."

        if not check_project_code_is_available(conn, project_code):
            return False, (
                f"Mã project {project_code} đã tồn tại. "
                f"Vui lòng đổi tên project để tạo mã khác."
            )

        folder_name = make_unique_project_folder(conn, project_name)
        project_number = get_next_project_number(conn)

        conn.execute("""
                     INSERT INTO projects (label,
                                           folder,
                                           project_number,
                                           project_code,
                                           status,
                                           requested_by_user_id,
                                           requested_by,
                                           requested_at,
                                           reviewed_by,
                                           reviewed_at,
                                           rejection_reason)
                     VALUES (?, ?, ?, ?, 'APPROVED', ?, ?, ?, ?, ?, NULL)
                     """, (
                         project_name,
                         folder_name,
                         project_number,
                         project_code,
                         current_user["id"],
                         current_user["full_name"],
                         now,
                         current_user["full_name"],
                         now,
                     ))

    create_project_storage_folders(folder_name)

    write_audit_log(
        user=current_user,
        action="CREATE_PROJECT",
        details=f"Quản lý tạo project mới: '{project_name}'.",
    )

    return True, f"Đã tạo project '{project_name}' thành công."

def normalize_project_label_for_upload(project_name: str) -> str:
    """
    Chuẩn hóa tên project khi user nhập project mới.
    Lưu dạng IN HOA để đồng bộ mã project.
    """

    return project_name.strip().upper()


def get_default_category_from_special_key(category_key: str):
    """
    Lấy file con mặc định khi category_key có dạng:
    __default_category__:BC
    """

    if not category_key.startswith(DEFAULT_CATEGORY_PREFIX):
        return None

    raw_key = category_key.replace(DEFAULT_CATEGORY_PREFIX, "", 1)

    category = CATEGORY_MAP.get(raw_key)

    if not category:
        return None

    return {
        "label": category["label"],
        "folder": category["folder"],
        "code": category["code"],
    }


def validate_new_category_input(category_label: str, category_code: str):
    """
    Kiểm tra tên/mã file con mới.
    Trả về: success, label, code, error_message
    """

    category_label = category_label.strip()
    category_code = category_code.strip().upper()

    if not category_label:
        return False, "", "", "Vui lòng nhập tên file con mới."

    if len(category_label) > 50:
        return False, "", "", "Tên file con không được vượt quá 50 ký tự."

    if category_code:
        category_code = re.sub(r"[^A-Z0-9]+", "", category_code)

    if not category_code:
        category_code = make_category_code_from_label(category_label)

    if len(category_code) > 10:
        return False, "", "", "Mã file con không được vượt quá 10 ký tự."

    return True, category_label, category_code, ""


def get_or_create_approved_project_by_label(
    conn,
    project_label: str,
    current_user,
):
    """
    Lấy project APPROVED theo tên.
    Nếu chưa có thì tạo mới và APPROVED ngay.

    Dùng cho:
    - Manager upload trực tiếp với project mới.
    - Manager duyệt hồ sơ pending có project mới.
    """

    project_label = normalize_project_label_for_upload(project_label)

    if not project_label:
        raise ValueError("Tên project mới không được để trống.")

    if len(project_label) > 50:
        raise ValueError("Tên project mới không được vượt quá 50 ký tự.")

    project_code = make_project_code_from_project_name(project_label)

    cleanup_deleted_project_records(
        conn=conn,
        project_name=project_label,
        project_code=project_code,
    )

    existing = conn.execute(
        """
        SELECT *
        FROM projects
        WHERE lower(label) = lower(?)
        """,
        (project_label,),
    ).fetchone()

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if existing:
        if existing["status"] == "APPROVED":
            return existing, False

        if existing["status"] == "PENDING":
            conn.execute(
                """
                UPDATE projects
                SET status = 'APPROVED',
                    reviewed_by = ?,
                    reviewed_at = ?,
                    rejection_reason = NULL
                WHERE id = ?
                """,
                (
                    current_user["full_name"],
                    now,
                    existing["id"],
                ),
            )

            return conn.execute(
                """
                SELECT *
                FROM projects
                WHERE id = ?
                """,
                (existing["id"],),
            ).fetchone(), True

        if existing["status"] == "REJECTED":
            raise ValueError("Project này đã từng bị từ chối. Vui lòng dùng tên project khác.")

    if not check_project_code_is_available(conn, project_code):
        raise ValueError(
            f"Mã project {project_code} đã tồn tại. "
            "Vui lòng đổi tên project để tạo mã khác."
        )

    folder_name = make_unique_project_folder(conn, project_label)
    project_number = get_next_project_number(conn)

    cursor = conn.execute(
        """
        INSERT INTO projects (
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
        VALUES (?, ?, ?, ?, 'APPROVED', ?, ?, ?, ?, ?, NULL)
        """,
        (
            project_label,
            folder_name,
            project_number,
            project_code,
            current_user["id"],
            current_user["full_name"],
            now,
            current_user["full_name"],
            now,
        ),
    )

    project_id = cursor.lastrowid

    project = conn.execute(
        """
        SELECT *
        FROM projects
        WHERE id = ?
        """,
        (project_id,),
    ).fetchone()

    return project, True


def get_or_create_active_category_by_label(
    conn,
    project_id: int,
    project_label: str,
    category_label: str,
    category_code: str,
):
    """
    Lấy file con active theo tên trong project.
    Nếu chưa có thì tạo mới.
    """

    category_label = category_label.strip()

    if not category_label:
        raise ValueError("Tên file con không được để trống.")

    if len(category_label) > 50:
        raise ValueError("Tên file con không được vượt quá 50 ký tự.")

    category_code = category_code.strip().upper()

    if category_code:
        category_code = re.sub(r"[^A-Z0-9]+", "", category_code)

    if not category_code:
        category_code = make_category_code_from_label(category_label)

    if len(category_code) > 10:
        raise ValueError("Mã file con không được vượt quá 10 ký tự.")

    existing_label = conn.execute(
        """
        SELECT *
        FROM project_categories
        WHERE project_id = ?
          AND lower(label) = lower(?)
        """,
        (project_id, category_label),
    ).fetchone()

    if existing_label:
        if not existing_label["is_active"]:
            conn.execute(
                """
                UPDATE project_categories
                SET is_active = 1,
                    code = ?
                WHERE id = ?
                """,
                (
                    category_code,
                    existing_label["id"],
                ),
            )

            return conn.execute(
                """
                SELECT *
                FROM project_categories
                WHERE id = ?
                """,
                (existing_label["id"],),
            ).fetchone(), True

        return existing_label, False

    existing_code = conn.execute(
        """
        SELECT id
        FROM project_categories
        WHERE project_id = ?
          AND code = ?
        """,
        (project_id, category_code),
    ).fetchone()

    if existing_code:
        raise ValueError(
            f"Mã file con {category_code} đã tồn tại trong project {project_label}."
        )

    folder_name = make_unique_category_folder(
        conn=conn,
        project_id=project_id,
        category_label=category_label,
    )

    category_key = f"CUSTOM_{uuid.uuid4().hex[:12]}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cursor = conn.execute(
        """
        INSERT INTO project_categories (
            project_id,
            category_key,
            label,
            folder,
            code,
            is_active,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, 1, ?)
        """,
        (
            project_id,
            category_key,
            category_label,
            folder_name,
            category_code,
            now,
        ),
    )

    category_id = cursor.lastrowid

    category = conn.execute(
        """
        SELECT *
        FROM project_categories
        WHERE id = ?
        """,
        (category_id,),
    ).fetchone()

    return category, True


def ensure_approved_project_and_category_for_document(
    project_label: str,
    category_label: str,
    requested_category_code: str,
    current_user,
    exact_project_id: int | None = None,
):
    """
    Đảm bảo project và file con đã tồn tại ở trạng thái APPROVED/active.

    Quan trọng:
    - Nếu exact_project_id có giá trị, file con chỉ được tạo/bật lại trong đúng project đó.
    - Không dùng tên file con để tác động lên toàn bộ project khác.
    """

    with get_connection() as conn:
        if exact_project_id is not None:
            project = conn.execute(
                """
                SELECT *
                FROM projects
                WHERE id = ?
                  AND status = 'APPROVED'
                """,
                (exact_project_id,),
            ).fetchone()

            if not project:
                raise ValueError("Project không hợp lệ hoặc chưa được duyệt.")

            created_project = False
        else:
            project, created_project = get_or_create_approved_project_by_label(
                conn=conn,
                project_label=project_label,
                current_user=current_user,
            )

        # Chỉ seed file con mặc định cho đúng project đang xử lý.
        seed_project_default_categories(
            conn=conn,
            project_id=project["id"],
        )

        category, created_category = get_or_create_active_category_by_label(
            conn=conn,
            project_id=project["id"],
            project_label=project["label"],
            category_label=category_label,
            category_code=requested_category_code or category_code_from_label(category_label, project["label"]),
        )

        project_id = project["id"]
        project_folder = project["folder"]
        project_label_final = project["label"]
        category_id = category["id"]
        category_label_final = category["label"]
        category_code_final = category["code"]

    create_project_storage_folders(project_folder)

    return {
        "project_id": project_id,
        "project_label": project_label_final,
        "project_folder": project_folder,
        "category_id": category_id,
        "category_label": category_label_final,
        "category_code": category_code_final,
        "created_project": created_project,
        "created_category": created_category,
    }


def resolve_upload_selection(
    project_key: str,
    category_key: str,
    new_project_name: str,
    new_category_name: str,
    new_category_code: str,
    current_user,
    create_immediately: bool = False,
):
    """
    Chuyển lựa chọn trên form upload thành project_label/category_label.

    create_immediately = False:
        Nhân viên upload. Project/file con mới chỉ lưu text vào document PENDING.
        Khi quản lý duyệt, hệ thống mới tạo project/file con.

    create_immediately = True:
        Quản lý upload. Project/file con mới được tạo ngay.
    """

    requested_new_project = 0
    requested_new_category = 0
    requested_category_code = ""
    selected_project_id = None

    project_key = (project_key or "").strip()
    category_key = (category_key or "").strip()

    if project_key == NEW_PROJECT_KEY:
        project_label = normalize_project_label_for_upload(new_project_name)

        if not project_label:
            return False, None, "Vui lòng nhập tên project mới."

        if len(project_label) > 50:
            return False, None, "Tên project mới không được vượt quá 50 ký tự."

        if not create_immediately:
            project_code = make_project_code_from_project_name(project_label)

            with get_connection() as conn:
                cleanup_deleted_project_records(
                    conn=conn,
                    project_name=project_label,
                    project_code=project_code,
                )

                existing = conn.execute(
                    """
                    SELECT id, status
                    FROM projects
                    WHERE lower(label) = lower(?)
                    """,
                    (project_label,),
                ).fetchone()

                if existing and existing["status"] == "APPROVED":
                    return False, None, "Project này đã tồn tại. Vui lòng chọn project có sẵn trong danh sách."

                if existing and existing["status"] == "REJECTED":
                    return False, None, "Project này đã từng bị từ chối. Vui lòng dùng tên project khác."

                if not check_project_code_is_available(conn, project_code):
                    return False, None, (
                        f"Mã project {project_code} đã tồn tại. "
                        "Vui lòng đổi tên project để tạo mã khác."
                    )

        requested_new_project = 1

    else:
        selected_project = get_approved_project_by_key(project_key)

        if not selected_project:
            return False, None, "Project không hợp lệ hoặc chưa được quản lý duyệt."

        project_label = selected_project["label"]
        selected_project_id = selected_project["id"]

    default_category = get_default_category_from_special_key(category_key)

    if category_key == NEW_CATEGORY_KEY:
        ok, category_label, category_code, error_message = validate_new_category_input(
            category_label=new_category_name,
            category_code=new_category_code,
        )

        if not ok:
            return False, None, error_message

        requested_new_category = 1
        requested_category_code = category_code

    elif default_category:
        category_label = default_category["label"]
        requested_category_code = default_category["code"]

    else:
        if project_key == NEW_PROJECT_KEY:
            return False, None, "Vui lòng chọn file con hợp lệ."

        try:
            project_id = int(project_key)
        except (TypeError, ValueError):
            return False, None, "Project không hợp lệ."

        selected_category = get_project_category_by_key(
            category_key=category_key,
            project_id=project_id,
        )

        if not selected_category:
            return False, None, "File con / loại hồ sơ không hợp lệ hoặc đã bị xóa khỏi project."

        category_label = selected_category["label"]
        requested_category_code = selected_category["code"]

    if create_immediately:
        try:
            ensured = ensure_approved_project_and_category_for_document(
                project_label=project_label,
                category_label=category_label,
                requested_category_code=requested_category_code,
                current_user=current_user,
                exact_project_id=selected_project_id,
            )
        except Exception as error:
            return False, None, str(error)

        project_label = ensured["project_label"]
        category_label = ensured["category_label"]
        requested_category_code = ensured["category_code"]
        requested_new_project = 1 if ensured["created_project"] else requested_new_project
        requested_new_category = 1 if ensured["created_category"] else requested_new_category

    return True, {
        "project_label": project_label,
        "category_label": category_label,
        "requested_new_project": requested_new_project,
        "requested_new_category": requested_new_category,
        "requested_category_code": requested_category_code,
    }, ""


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

def create_login_session(user, request: Request) -> str:
    """
    Tạo session_token riêng cho máy/trình duyệt hiện tại.
    """

    session_token = secrets.token_urlsafe(48)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    user_agent = request.headers.get("user-agent", "")
    client_ip = request.client.host if request.client else ""

    with get_connection() as conn:
        conn.execute("""
            INSERT INTO user_sessions (
                session_token,
                user_id,
                username,
                created_at,
                last_seen_at,
                user_agent,
                client_ip
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            session_token,
            user["id"],
            user["username"],
            now,
            now,
            user_agent,
            client_ip,
        ))

    return session_token

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
@app.on_event("startup")
def startup():
    initialize_audit_logs()
    initialize_projects()
    initialize_project_categories()
    initialize_storage_and_documents()
    initialize_user_status()
    initialize_user_approval_status()
    initialize_admin_status()
    initialize_user_sessions()

def create_employee_user(username: str, full_name: str, password: str):
    """
    Tạo tài khoản nhân viên từ trang web đăng ký.

    Người dùng tự đăng ký chỉ được role EMPLOYEE.
    """

    username = username.strip().lower()
    full_name = full_name.strip()

    if not username:
        return False, "Username không được để trống."

    if not full_name:
        return False, "Họ tên không được để trống."

    if len(password) < 8:
        return False, "Mật khẩu cần có ít nhất 8 ký tự."

    password_salt, password_hash = hash_password(password)

    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO users (
    username,
    full_name,
    role,
    password_salt,
    password_hash,
    created_at,
    is_active,
    approval_status
)
VALUES (?, ?, ?, ?, ?, ?, 0, 'PENDING')
                """,
                (
                    username,
                    full_name,
                    ROLE_EMPLOYEE,
                    password_salt,
                    password_hash,
                    created_at,
                ),
            )

        return True, "Đăng ký thành công. Vui lòng chờ quản lý phê duyệt tài khoản."

    except sqlite3.IntegrityError:
        return False, "Username này đã tồn tại. Vui lòng chọn username khác."

def get_current_user(request: Request):
    """
    Lấy user hiện tại theo session_token riêng của từng máy/trình duyệt.

    Không dùng user_id trực tiếp trong cookie nữa.
    Điều này tránh lỗi máy khác tự vào tài khoản đang login ở máy này.
    """

    session_token = request.session.get("session_token")

    if not session_token:
        request.session.clear()
        return None

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as conn:
        row = conn.execute("""
            SELECT
                us.session_token,
                u.id,
                u.username,
                u.full_name,
                u.role,
                COALESCE(u.is_admin, 0) AS is_admin,
                COALESCE(u.is_active, 1) AS is_active,
                COALESCE(u.approval_status, 'APPROVED') AS approval_status
            FROM user_sessions us
            JOIN users u ON u.id = us.user_id
            WHERE us.session_token = ?
        """, (session_token,)).fetchone()

        if not row:
            request.session.clear()
            return None

        if not row["is_active"] or row["approval_status"] != "APPROVED":
            conn.execute("""
                DELETE FROM user_sessions
                WHERE session_token = ?
            """, (session_token,))

            request.session.clear()
            return None

        conn.execute("""
            UPDATE user_sessions
            SET last_seen_at = ?
            WHERE session_token = ?
        """, (now, session_token))

    return row

def write_audit_log(
    user,
    action: str,
    document_id: int | None = None,
    details: str = ""
):
    """
    Ghi lại hoạt động quan trọng vào bảng audit_logs.

    user:
        Tài khoản hiện đang đăng nhập.

    action:
        Ví dụ: UPLOAD, APPROVE, REJECT, DOWNLOAD.

    document_id:
        Mã hồ sơ liên quan, có thể để None.

    details:
        Mô tả ngắn về hành động.
    """

    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO audit_logs (
                user_id,
                username,
                full_name,
                action,
                document_id,
                details,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user["id"],
                user["username"],
                user["full_name"],
                action,
                document_id,
                details,
                created_at,
            ),
        )
def remove_readonly_and_retry(func, path, exc_info):
    """
    Xử lý file/folder bị Read-only trên Windows/OneDrive.
    """

    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except Exception:
        raise


def force_delete_file(file_path: Path) -> bool:
    """
    Xóa file có retry để tránh lỗi OneDrive/Windows đang khóa tạm thời.

    Trả về:
    - True: đã xóa được file
    - False: không xóa được hoặc file không tồn tại
    """

    if not file_path.exists() or not file_path.is_file():
        return False

    last_error = None

    for _ in range(5):
        try:
            os.chmod(file_path, stat.S_IWRITE | stat.S_IREAD)
            file_path.unlink()
            return True
        except PermissionError as error:
            last_error = error
            time.sleep(1)
        except OSError as error:
            last_error = error
            time.sleep(1)

    print(f"Cannot delete file: {file_path}. Error: {last_error}")
    return False


def try_delete_folder(folder_path: Path) -> bool:
    """
    Cố gắng xóa folder OneDrive.

    Nếu OneDrive đang khóa folder thì không làm web lỗi.
    Project vẫn được xóa khỏi database để giải phóng tên project và mã project.
    """

    if not folder_path.exists():
        return True

    if not folder_path.is_dir():
        return False

    last_error = None

    for _ in range(5):
        try:
            shutil.rmtree(
                folder_path,
                onerror=remove_readonly_and_retry,
            )
            return True
        except PermissionError as error:
            last_error = error
            time.sleep(1)
        except OSError as error:
            last_error = error
            time.sleep(1)

    print(f"Cannot delete folder: {folder_path}. Error: {last_error}")
    return False


def hard_delete_project_metadata(conn, project_id: int, project_label: str):
    """
    Xóa hẳn dữ liệu project khỏi database.

    Hàm này xóa luôn project_code vì dòng project bị DELETE khỏi bảng projects.
    Nhờ vậy có thể tạo lại project cùng tên/mã sau khi xóa.
    """

    conn.execute(
        """
        DELETE FROM documents
        WHERE project = ?
        """,
        (project_label,),
    )

    conn.execute(
        """
        DELETE FROM project_categories
        WHERE project_id = ?
        """,
        (project_id,),
    )

    conn.execute(
        """
        DELETE FROM projects
        WHERE id = ?
        """,
        (project_id,),
    )


def cleanup_deleted_project_records(conn, project_name: str, project_code: str):
    """
    Dọn các project đã bị xóa mềm từ phiên bản cũ.

    Lý do: bản cũ chỉ đổi status = DELETED nên project_code vẫn còn trong database,
    làm tạo project mới bị báo mã project đã tồn tại.
    """

    rows = conn.execute(
        """
        SELECT id, label
        FROM projects
        WHERE status = 'DELETED'
          AND (
              lower(label) = lower(?)
              OR project_code = ?
          )
        """,
        (project_name, project_code),
    ).fetchall()

    for row in rows:
        hard_delete_project_metadata(
            conn=conn,
            project_id=row["id"],
            project_label=row["label"],
        )


def storage_folder_from_project_and_category(
    project_label: str,
    category_label: str
) -> Path:
    """
    Tìm thư mục lưu file theo Project và file con / loại hồ sơ.

    Ưu tiên lấy từ bảng project_categories để mỗi project có danh sách file con riêng.
    """
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
                p.folder AS project_folder,
                pc.folder AS category_folder
            FROM projects p
            JOIN project_categories pc ON pc.project_id = p.id
            WHERE p.label = ?
              AND p.status = 'APPROVED'
              AND pc.label = ?
            LIMIT 1
            """,
            (project_label, category_label),
        ).fetchone()

    if row:
        return STORAGE_DIR / row["project_folder"] / row["category_folder"]

    project_folder_name = None
    category_folder_name = None

    with get_connection() as conn:
        project = conn.execute(
            """
            SELECT folder
            FROM projects
            WHERE label = ?
              AND status = 'APPROVED'
            """,
            (project_label,),
        ).fetchone()

    if project:
        project_folder_name = project["folder"]

    if not project_folder_name:
        for project in PROJECT_MAP.values():
            if project["label"] == project_label:
                project_folder_name = project["folder"]
                break

    for category in CATEGORY_MAP.values():
        if category["label"] == category_label:
            category_folder_name = category["folder"]
            break

    if not project_folder_name:
        raise ValueError("Không tìm thấy project tương ứng.")

    if not category_folder_name:
        raise ValueError("Không tìm thấy file con / loại hồ sơ tương ứng.")

    return STORAGE_DIR / project_folder_name / category_folder_name

def is_admin_user(user) -> bool:
    """
    Kiểm tra tài khoản hiện tại có phải admin không.
    """

    return bool(user and user["is_admin"])


def project_code_from_label(project_label: str) -> str:
    """
    Lấy mã project từ bảng projects.

    Ví dụ:
    Project Alpha -> PROJ-ALP
    """

    with get_connection() as conn:
        project = conn.execute(
            """
            SELECT project_code
            FROM projects
            WHERE label = ?
              AND status = 'APPROVED'
            """,
            (project_label,),
        ).fetchone()

    if project and project["project_code"]:
        return project["project_code"]

    legacy_project_codes = {
        "Project 1": "PROJ-001",
        "Project 2": "PROJ-002",
        "Project 3": "PROJ-003",
    }

    if project_label in legacy_project_codes:
        return legacy_project_codes[project_label]

    raise ValueError("Không tìm thấy mã project tương ứng.")


def category_code_from_label(category_label: str, project_label: str | None = None) -> str:
    """
    Lấy mã file con / loại hồ sơ.

    Nếu có project_label, ưu tiên lấy mã riêng của file con trong project đó.
    """
    if project_label:
        with get_connection() as conn:
            row = conn.execute(
                """
                SELECT pc.code
                FROM project_categories pc
                JOIN projects p ON p.id = pc.project_id
                WHERE p.label = ?
                  AND pc.label = ?
                LIMIT 1
                """,
                (project_label, category_label),
            ).fetchone()

        if row and row["code"]:
            return row["code"]

    for category in CATEGORY_MAP.values():
        if category["label"] == category_label:
            return category["code"]

    legacy_category_codes = {
        "File loại 1": "LEGACY1",
        "File loại 2": "LEGACY2",
        "File loại 3": "LEGACY3",
    }

    if category_label in legacy_category_codes:
        return legacy_category_codes[category_label]

    return make_category_code_from_label(category_label)

def generate_document_code(
    conn,
    project_label: str,
    category_label: str
) -> str:
    """
    Tạo mã hồ sơ tự động.

    Format:
    AMT_ENG_PROJ-1_filetype-2_00001
    """

    project_code = project_code_from_label(project_label)
    category_code = category_code_from_label(category_label, project_label)

    prefix = (
        f"AMT_ENG_{project_code}"
        f"_{category_code}_"
    )


    latest = conn.execute(
        """
        SELECT document_code
        FROM documents
        WHERE document_code LIKE ?
        ORDER BY document_code DESC
        LIMIT 1
        """,
        (f"{prefix}%",),
    ).fetchone()

    if latest and latest["document_code"]:
        last_code = latest["document_code"]
        last_number_text = last_code.replace(prefix, "")

        try:
            next_number = int(last_number_text) + 1
        except ValueError:
            next_number = 1
    else:
        next_number = 1

    return f"{prefix}{next_number:05d}"
async def save_upload_file_to_path(
    uploaded_file: UploadFile,
    destination_path: Path
) -> int:
    """
    Lưu 1 file upload xuống ổ đĩa.

    Trả về dung lượng file theo byte.
    Nếu file vượt quá MAX_FILE_SIZE_MB thì tự xóa file đang ghi dở.
    """

    max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024
    total_size = 0

    with destination_path.open("wb") as output_file:
        while True:
            chunk = await uploaded_file.read(1024 * 1024)

            if not chunk:
                break

            total_size += len(chunk)

            if total_size > max_bytes:
                output_file.close()

                if destination_path.exists():
                    destination_path.unlink()

                raise ValueError(
                    f"File vượt quá dung lượng tối đa {MAX_FILE_SIZE_MB} MB."
                )

            output_file.write(chunk)

    return total_size
# ============================================================
# TRANG ĐĂNG NHẬP
# ============================================================

@app.get("/", response_class=HTMLResponse)
def login_page(
    request: Request,
    message: str = ""
):
    """
    Hiển thị trang đăng nhập.
    Nếu đã đăng nhập thì chuyển thẳng về dashboard.
    """

    current_user = get_current_user(request)

    if current_user:
        if current_user["role"] == ROLE_MANAGER:
            return RedirectResponse("/manager", status_code=303)

        return RedirectResponse("/employee", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": None,
            "message": message,
        }
    )


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    """
    Nhận username + password từ form HTML,
    kiểm tra bảng users và lưu user_id vào session.
    """

    username = username.strip().lower()

    with get_connection() as conn:
        user = conn.execute(
            """
            SELECT
                id,
                username,
                full_name,
                role,
                password_salt,
                password_hash,
                COALESCE(is_active, 1) AS is_active,
                COALESCE(approval_status, 'APPROVED') AS approval_status,
                COALESCE(is_admin, 0) AS is_admin
            FROM users
            WHERE lower(username) = lower(?)
            """,
            (username,),
        ).fetchone()

    if not user:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "Không tìm thấy tài khoản này.",
                "message": "",
            },
            status_code=401,
        )

    if user["approval_status"] == "PENDING":
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": (
                    "Tài khoản của bạn đang chờ quản lý phê duyệt. "
                    "Vui lòng thử lại sau."
                ),
                "message": "",
            },
            status_code=403,
        )

    if user["approval_status"] == "REJECTED":
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": (
                    "Yêu cầu đăng ký tài khoản của bạn đã bị từ chối. "
                    "Vui lòng liên hệ quản lý."
                ),
                "message": "",
            },
            status_code=403,
        )

    if not user["is_active"]:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": (
                    "Tài khoản của bạn đã bị khóa. "
                    "Vui lòng liên hệ quản lý."
                ),
                "message": "",
            },
            status_code=403,
        )

    is_correct_password = verify_password(
        password,
        user["password_salt"],
        user["password_hash"],
    )

    if not is_correct_password:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "Mật khẩu không đúng.",
                "message": "",
            },
            status_code=401,
        )

    request.session.clear()

    session_token = create_login_session(
        user=user,
        request=request,
    )

    request.session["session_token"] = session_token

    if user["is_admin"]:
        return RedirectResponse("/admin", status_code=303)

    if user["role"] == ROLE_MANAGER:
        return RedirectResponse("/manager", status_code=303)

    return RedirectResponse("/employee", status_code=303)

# ============================================================
# ĐĂNG KÝ TÀI KHOẢN NHÂN VIÊN
# ============================================================

@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    """
    Hiển thị form đăng ký tài khoản nhân viên.
    """

    current_user = get_current_user(request)

    if current_user:
        if current_user["role"] == ROLE_MANAGER:
            return RedirectResponse("/manager", status_code=303)

        return RedirectResponse("/employee", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={
            "error": None,
        }
    )


@app.post("/register", response_class=HTMLResponse)
def register_user(
    request: Request,
    username: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...)
):
    """
    Nhận dữ liệu đăng ký từ web và tạo tài khoản nhân viên.
    """

    username = username.strip().lower()
    full_name = full_name.strip()

    if password != confirm_password:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": "Hai mật khẩu không khớp.",
            },
            status_code=400,
        )

    success, message = create_employee_user(
        username=username,
        full_name=full_name,
        password=password,
    )

    if not success:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": message,
            },
            status_code=400,
        )

    return RedirectResponse(
        "/?message=Đăng+ký+thành+công.+Vui+lòng+chờ+quản+lý+phê+duyệt+tài+khoản.",
        status_code=303,
    )
# ============================================================
# DASHBOARD NHÂN VIÊN
# ============================================================

@app.get("/employee", response_class=HTMLResponse)
def employee_dashboard(request: Request):
    """
    Trang dành cho nhân viên.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_EMPLOYEE:
        return RedirectResponse("/manager", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="employee_dashboard.html",
        context={
            "user": current_user
        }
    )

@app.get("/employee/my-documents", response_class=HTMLResponse)
def employee_my_documents(request: Request):
    """
    Hiển thị tất cả hồ sơ do nhân viên đang đăng nhập gửi.

    Bao gồm:
    - PENDING: Chờ quản lý duyệt
    - APPROVED: Đã được duyệt
    - REJECTED: Bị từ chối
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_EMPLOYEE:
        return RedirectResponse("/manager", status_code=303)

    with get_connection() as conn:
        documents = conn.execute(
            """
            SELECT *
            FROM documents
            WHERE submitted_by_user_id = ?
            ORDER BY id DESC
            """,
            (current_user["id"],),
        ).fetchall()

    return templates.TemplateResponse(
        request=request,
        name="my_documents.html",
        context={
            "user": current_user,
            "documents": documents,
        }
    )
@app.get("/employee/upload", response_class=HTMLResponse)
def employee_upload_page(request: Request):
    """
    Hiển thị trang upload cho nhân viên.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_EMPLOYEE:
        return RedirectResponse("/manager", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="upload.html",
        context=build_upload_context(
            current_user=current_user,
        ),
    )

@app.post("/employee/upload", response_class=HTMLResponse)
async def employee_upload_file(
    request: Request,
    project_key: str = Form(...),
    category_key: str = Form(...),
    new_project_name: str = Form(""),
    new_category_name: str = Form(""),
    new_category_code: str = Form(""),
    description: str = Form(""),
    location: str = Form(""),
    uploaded_files: List[UploadFile] = File(...)
):
    """
    Nhân viên upload nhiều file cùng lúc.

    Tất cả file được lưu ở pending/.
    Database lưu trạng thái PENDING.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_EMPLOYEE:
        return RedirectResponse("/manager", status_code=303)

    def show_upload_page(error=None, success=None):
        return templates.TemplateResponse(
            request=request,
            name="upload.html",
            context=build_upload_context(
                current_user=current_user,
                error=error,
                success=success,
                selected_project_key=project_key,
                selected_category_key=category_key,
                description_value=description,
                location_value=location,
                new_project_name_value=new_project_name,
                new_category_name_value=new_category_name,
                new_category_code_value=new_category_code,
            ),
        )

    description = description.strip()
    location = location.strip()

    if not description:
        return show_upload_page(
            error="Vui lòng nhập mô tả hồ sơ trước khi upload."
        )

    if not location:
        return show_upload_page(
            error="Vui lòng nhập vị trí hồ sơ trước khi upload."
        )

    if len(description) > 50:
        return show_upload_page(
            error="Mô tả hồ sơ không được vượt quá 50 ký tự."
        )

    if len(location) > 50:
        return show_upload_page(
            error="Vị trí không được vượt quá 50 ký tự."
        )

    ok, selection, selection_error = resolve_upload_selection(
        project_key=project_key,
        category_key=category_key,
        new_project_name=new_project_name,
        new_category_name=new_category_name,
        new_category_code=new_category_code,
        current_user=current_user,
        create_immediately=False,
    )

    if not ok:
        return show_upload_page(error=selection_error)

    project_label = selection["project_label"]
    category_label = selection["category_label"]
    requested_new_project = selection["requested_new_project"]
    requested_new_category = selection["requested_new_category"]
    requested_category_code = selection["requested_category_code"]

    success_messages = []
    error_messages = []

    for uploaded_file in uploaded_files:
        pending_path = None

        try:
            original_name = Path(uploaded_file.filename or "").name

            if not original_name:
                error_messages.append("Có 1 file không có tên, hệ thống đã bỏ qua.")
                continue

            extension = Path(original_name).suffix.lower()

            if extension not in ALLOWED_EXTENSIONS:
                allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
                error_messages.append(
                    f"{original_name}: định dạng {extension or '(không có đuôi)'} "
                    f"không được phép. Cho phép: {allowed}"
                )
                continue

            with get_connection() as conn:
                existing = conn.execute(
                    """
                    SELECT id, document_code
                    FROM documents
                    WHERE lower(original_name) = lower(?)
                      AND project = ?
                      AND category = ?
                      AND status IN ('PENDING', 'APPROVED')
                    """,
                    (
                        original_name,
                        project_label,
                        category_label,
                    ),
                ).fetchone()

            if existing:
                existing_code = existing["document_code"] or existing["id"]
                error_messages.append(
                    f"{original_name}: đã có hồ sơ cùng tên trong "
                    f"{project_label} - {category_label}. "
                    f"Mã hồ sơ hiện có: {existing_code}."
                )
                continue

            stored_name = f"{uuid.uuid4().hex}{extension}"
            pending_path = PENDING_DIR / stored_name

            total_size = await save_upload_file_to_path(
                uploaded_file=uploaded_file,
                destination_path=pending_path,
            )

            submitted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            with get_connection() as conn:
                if requested_new_project or requested_new_category:
                    document_code = None
                else:
                    document_code = generate_document_code(
                        conn,
                        project_label,
                        category_label,
                    )

                cursor = conn.execute(
                    """
                    INSERT INTO documents (
                        document_code,
                        original_name,
                        description,
                        location,
                        stored_name,
                        project,
                        category,
                        file_path,
                        file_size,
                        status,
                        submitted_at,
                        submitted_by,
                        submitted_by_user_id,
                        requested_new_project,
                        requested_new_category,
                        requested_category_code
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_code,
                        original_name,
                        description,
                        location,
                        stored_name,
                        project_label,
                        category_label,
                        str(pending_path),
                        total_size,
                        submitted_at,
                        current_user["full_name"],
                        current_user["id"],
                        requested_new_project,
                        requested_new_category,
                        requested_category_code,
                    ),
                )

                document_id = cursor.lastrowid

            write_audit_log(
                user=current_user,
                action="UPLOAD",
                document_id=document_id,
                details=(
                    f"Gửi hồ sơ '{original_name}' thuộc "
                    f"{project_label} - {category_label} chờ quản lý duyệt."
                ),
            )

            if document_code:
                success_messages.append(
                    f"{original_name}: upload thành công. Mã hồ sơ: {document_code}."
                )
            else:
                success_messages.append(
                    f"{original_name}: upload thành công và đang chờ quản lý duyệt."
                )

        except Exception as error:
            if pending_path and pending_path.exists():
                pending_path.unlink()

            error_messages.append(
                f"{uploaded_file.filename}: không thể upload. Lỗi: {error}"
            )

        finally:
            await uploaded_file.close()

    if not success_messages and error_messages:
        return show_upload_page(
            error="\n".join(error_messages)
        )

    return show_upload_page(
        success="\n".join(success_messages) if success_messages else None,
        error="\n".join(error_messages) if error_messages else None,
    )

def get_last_12_month_keys():
    now = datetime.now()
    year = now.year
    month = now.month

    month_keys = []

    for _ in range(12):
        month_keys.append(f"{year:04d}-{month:02d}")

        month -= 1
        if month == 0:
            month = 12
            year -= 1

    month_keys.reverse()
    return month_keys


@app.get("/statistics", response_class=HTMLResponse)
def statistics_page(request: Request):
    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    month_keys = get_last_12_month_keys()
    placeholders = ",".join("?" for _ in month_keys)

    with get_connection() as conn:
        monthly_rows = conn.execute(f"""
            SELECT 
                substr(submitted_at, 1, 7) AS upload_month,
                COUNT(*) AS total_documents
            FROM documents
            WHERE submitted_at IS NOT NULL
              AND submitted_at != ''
              AND substr(submitted_at, 1, 7) IN ({placeholders})
            GROUP BY substr(submitted_at, 1, 7)
            ORDER BY upload_month ASC
        """, month_keys).fetchall()

        user_rows = conn.execute("""
            SELECT
                COALESCE(NULLIF(u.username, ''), '') AS username,
                COALESCE(NULLIF(u.full_name, ''), NULLIF(d.submitted_by, ''), 'Unknown') AS full_name,
                COUNT(*) AS total_documents,
                SUM(CASE WHEN d.status = 'APPROVED' THEN 1 ELSE 0 END) AS approved_documents,
                SUM(CASE WHEN d.status = 'PENDING' THEN 1 ELSE 0 END) AS pending_documents,
                SUM(CASE WHEN d.status = 'REJECTED' THEN 1 ELSE 0 END) AS rejected_documents
            FROM documents d
            LEFT JOIN users u ON u.id = d.submitted_by_user_id
            GROUP BY
                COALESCE(u.id, d.submitted_by_user_id, d.submitted_by),
                COALESCE(NULLIF(u.username, ''), ''),
                COALESCE(NULLIF(u.full_name, ''), NULLIF(d.submitted_by, ''), 'Unknown')
            ORDER BY total_documents DESC, full_name ASC
        """).fetchall()

        total_uploaded_documents = conn.execute("""
            SELECT COUNT(*) AS total
            FROM documents
        """).fetchone()["total"]

    monthly_count_map = {
        row["upload_month"]: row["total_documents"]
        for row in monthly_rows
    }

    monthly_labels = [
        f"{month_key[5:7]}/{month_key[2:4]}"
        for month_key in month_keys
    ]

    monthly_values = [
        monthly_count_map.get(month_key, 0)
        for month_key in month_keys
    ]

    dashboard_url = "/employee"

    if current_user["role"] == ROLE_MANAGER:
        if is_admin_user(current_user):
            dashboard_url = "/admin"
        else:
            dashboard_url = "/manager"

    return templates.TemplateResponse(
        request=request,
        name="statistics.html",
        context={
            "user": current_user,
            "dashboard_url": dashboard_url,
            "monthly_labels_json": json.dumps(monthly_labels, ensure_ascii=False),
            "monthly_values_json": json.dumps(monthly_values, ensure_ascii=False),
            "user_rows": user_rows,
            "total_uploaded_documents": total_uploaded_documents,
        },
    )
@app.post("/employee/projects/request", response_class=HTMLResponse)
def employee_request_project(
    request: Request,
    project_name: str = Form(...)
):
    """
    Nhân viên gửi yêu cầu tạo project mới.
    Project phải chờ quản lý duyệt.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_EMPLOYEE:
        return RedirectResponse("/manager", status_code=303)

    success, message = request_new_project_from_employee(
        project_name=project_name,
        current_user=current_user,
    )

    return templates.TemplateResponse(
        request=request,
        name="upload.html",
        context=build_upload_context(
            current_user=current_user,
            error=None if success else message,
            success=message if success else None,
        )
    )

# ============================================================
# DASHBOARD QUẢN LÝ
# ============================================================

@app.get("/manager", response_class=HTMLResponse)
def manager_dashboard(request: Request):
    """
    Trang dành cho quản lý.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_MANAGER:
        return RedirectResponse("/employee", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="manager_dashboard.html",
        context={
            "user": current_user
        }
    )
@app.get("/manager/my-documents", response_class=HTMLResponse)
def manager_my_documents(request: Request):
    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_MANAGER:
        return RedirectResponse("/", status_code=303)

    with get_connection() as conn:
        documents = conn.execute(
            """
            SELECT
                id,
                document_code,
                original_name,
                project,
                category,
                submitted_at,
                status,
                reviewed_by,
                rejection_reason
            FROM documents
            WHERE submitted_by_user_id = ?
            ORDER BY id DESC
            """,
            (current_user["id"],),
        ).fetchall()

    return templates.TemplateResponse(
        request=request,
        name="manager_my_documents.html",
        context={
            "user": current_user,
            "documents": documents,
        },
    )
@app.get("/manager/upload", response_class=HTMLResponse)
def manager_upload_page(
    request: Request,
    manage_project_id: str = "",
    message: str = "",
    error: str = "",
):
    """
    Trang upload dành cho quản lý.

    Quản lý upload hồ sơ thì hồ sơ được duyệt ngay,
    không cần qua quy trình chờ duyệt.
    Đồng thời quản lý có thể thêm/xóa file con theo từng project.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_MANAGER:
        return RedirectResponse("/employee", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="manager_upload.html",
        context=build_manager_upload_context(
            current_user=current_user,
            error=error or None,
            success=message or None,
            manage_project_key=manage_project_id,
        ),
    )

@app.post("/manager/upload", response_class=HTMLResponse)
async def manager_upload_file(
    request: Request,
    project_key: str = Form(...),
    category_key: str = Form(...),
    new_project_name: str = Form(""),
    new_category_name: str = Form(""),
    new_category_code: str = Form(""),
    description: str = Form(""),
    location: str = Form(""),
    uploaded_files: List[UploadFile] = File(...)
):
    """
    Quản lý upload nhiều file cùng lúc.

    File được lưu thẳng vào storage.
    Trạng thái trong database là APPROVED.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_MANAGER:
        return RedirectResponse("/employee", status_code=303)

    def show_manager_upload_page(error=None, success=None):
        return templates.TemplateResponse(
            request=request,
            name="manager_upload.html",
            context=build_manager_upload_context(
                current_user=current_user,
                error=error,
                success=success,
                selected_project_key=project_key,
                selected_category_key=category_key,
                description_value=description,
                location_value=location,
                new_project_name_value=new_project_name,
                new_category_name_value=new_category_name,
                new_category_code_value=new_category_code,
                manage_project_key=project_key,
            ),
        )

    description = description.strip()
    location = location.strip()

    if not description:
        return show_manager_upload_page(
            error="Vui lòng nhập mô tả hồ sơ trước khi upload."
        )

    if not location:
        return show_manager_upload_page(
            error="Vui lòng nhập vị trí hồ sơ trước khi upload."
        )

    if len(description) > 50:
        return show_manager_upload_page(
            error="Mô tả hồ sơ không được vượt quá 50 ký tự."
        )

    if len(location) > 50:
        return show_manager_upload_page(
            error="Vị trí không được vượt quá 50 ký tự."
        )

    ok, selection, selection_error = resolve_upload_selection(
        project_key=project_key,
        category_key=category_key,
        new_project_name=new_project_name,
        new_category_name=new_category_name,
        new_category_code=new_category_code,
        current_user=current_user,
        create_immediately=True,
    )

    if not ok:
        return show_manager_upload_page(error=selection_error)

    project_label = selection["project_label"]
    category_label = selection["category_label"]

    success_messages = []
    error_messages = []

    for uploaded_file in uploaded_files:
        destination_path = None

        try:
            original_name = Path(uploaded_file.filename or "").name

            if not original_name:
                error_messages.append("Có 1 file không có tên, hệ thống đã bỏ qua.")
                continue

            extension = Path(original_name).suffix.lower()

            if extension not in ALLOWED_EXTENSIONS:
                allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
                error_messages.append(
                    f"{original_name}: định dạng {extension or '(không có đuôi)'} "
                    f"không được phép. Cho phép: {allowed}"
                )
                continue

            with get_connection() as conn:
                existing = conn.execute(
                    """
                    SELECT id, document_code
                    FROM documents
                    WHERE lower(original_name) = lower(?)
                      AND project = ?
                      AND category = ?
                      AND status IN ('PENDING', 'APPROVED')
                    """,
                    (
                        original_name,
                        project_label,
                        category_label,
                    ),
                ).fetchone()

            if existing:
                existing_code = existing["document_code"] or existing["id"]
                error_messages.append(
                    f"{original_name}: đã có hồ sơ cùng tên trong "
                    f"{project_label} - {category_label}. "
                    f"Mã hồ sơ hiện có: {existing_code}."
                )
                continue

            stored_name = f"{uuid.uuid4().hex}{extension}"

            destination_folder = storage_folder_from_project_and_category(
                project_label,
                category_label,
            )

            destination_folder.mkdir(parents=True, exist_ok=True)
            destination_path = destination_folder / stored_name

            total_size = await save_upload_file_to_path(
                uploaded_file=uploaded_file,
                destination_path=destination_path,
            )

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            with get_connection() as conn:
                document_code = generate_document_code(
                    conn,
                    project_label,
                    category_label,
                )

                cursor = conn.execute(
                    """
                    INSERT INTO documents (
    document_code,
    original_name,
    description,
    location,
    stored_name,
    project,
    category,
    file_path,
    file_size,
    status,
    submitted_at,
    submitted_by,
    submitted_by_user_id,
    reviewed_at,
    reviewed_by,
    rejection_reason
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'APPROVED', ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        document_code,
                        original_name,
                        description,
                        location,
                        stored_name,
                        project_label,
                        category_label,
                        str(destination_path),
                        total_size,
                        now,
                        current_user["full_name"],
                        current_user["id"],
                        now,
                        current_user["full_name"],
                    ),
                )

                document_id = cursor.lastrowid

            write_audit_log(
                user=current_user,
                action="MANAGER_UPLOAD",
                document_id=document_id,
                details=(
                    f"Quản lý upload trực tiếp hồ sơ '{original_name}' "
                    f"vào {project_label} - {category_label}."
                ),
            )

            success_messages.append(
                f"{original_name}: upload thành công. Mã hồ sơ: {document_code}."
            )

        except Exception as error:
            if destination_path and destination_path.exists():
                destination_path.unlink()

            error_messages.append(
                f"{uploaded_file.filename}: không thể upload. Lỗi: {error}"
            )

        finally:
            await uploaded_file.close()

    if not success_messages and error_messages:
        return show_manager_upload_page(
            error="\n".join(error_messages)
        )

    return show_manager_upload_page(
        success="\n".join(success_messages) if success_messages else None,
        error="\n".join(error_messages) if error_messages else None,
    )
@app.post("/manager/projects/create", response_class=HTMLResponse)
def manager_create_project(
    request: Request,
    project_name: str = Form(...)
):
    """
    Quản lý tạo project mới.
    Project được duyệt ngay.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_MANAGER:
        return RedirectResponse("/employee", status_code=303)

    success, message = create_project_immediately_by_manager(
        project_name=project_name,
        current_user=current_user,
    )

    return templates.TemplateResponse(
        request=request,
        name="manager_upload.html",
        context=build_manager_upload_context(
            current_user=current_user,
            error=None if success else message,
            success=message if success else None,
        )
    )


@app.get("/project-categories/{project_id}")
def project_categories_by_project(
    request: Request,
    project_id: int
):
    """
    Trả danh sách file con theo project để dropdown tự cập nhật.
    """

    current_user = get_current_user(request)

    if not current_user:
        return JSONResponse(
            {
                "success": False,
                "message": "Not logged in",
                "categories": [],
            },
            status_code=401,
        )

    with get_connection() as conn:
        project = conn.execute(
            """
            SELECT id
            FROM projects
            WHERE id = ?
              AND status = 'APPROVED'
            """,
            (project_id,),
        ).fetchone()

    if not project:
        return {
            "success": False,
            "message": "Project không hợp lệ.",
            "categories": [],
        }

    return {
        "success": True,
        "categories": get_project_category_json_list(project_id),
    }


@app.post("/manager/project-categories/add")
def manager_add_project_category(
    request: Request,
    project_key: str = Form(...),
    category_label: str = Form(...),
    category_code: str = Form(""),
):
    """
    Quản lý thêm file con vào project.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_MANAGER:
        return RedirectResponse("/employee", status_code=303)

    success, message = create_project_category_for_manager(
        project_key=project_key,
        category_label=category_label,
        category_code=category_code,
        current_user=current_user,
    )

    query_name = "message" if success else "error"

    return RedirectResponse(
        f"/manager/upload?manage_project_id={project_key}&{query_name}={quote(message)}",
        status_code=303,
    )


@app.post("/manager/project-categories/{category_id}/delete")
def manager_delete_project_category(
    request: Request,
    category_id: int,
    project_key: str = Form(""),
):
    """
    Quản lý xóa/ẩn file con khỏi project.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_MANAGER:
        return RedirectResponse("/employee", status_code=303)

    success, message, fallback_project_key = delete_project_category_for_manager(
        category_id=category_id,
        project_key=project_key,
        current_user=current_user,
    )

    if not project_key:
        project_key = fallback_project_key

    query_name = "message" if success else "error"

    return RedirectResponse(
        f"/manager/upload?manage_project_id={project_key}&{query_name}={quote(message)}",
        status_code=303,
    )

@app.get("/manager/pending", response_class=HTMLResponse)
def manager_pending_documents(
    request: Request,
    message: str | None = None,
    error: str | None = None
):
    """
    Hiển thị toàn bộ hồ sơ đang chờ quản lý duyệt.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_MANAGER:
        return RedirectResponse("/employee", status_code=303)

    with get_connection() as conn:
        documents = conn.execute(
            """
            SELECT *
            FROM documents
            WHERE status = 'PENDING'
            ORDER BY id ASC
            """
        ).fetchall()

    return templates.TemplateResponse(
        request=request,
        name="manager_pending.html",
        context={
            "user": current_user,
            "documents": documents,
            "message": message,
            "error": error,
        }
    )
@app.get("/manager/project-requests", response_class=HTMLResponse)
def manager_project_requests(
    request: Request,
    message: str | None = None,
    error: str | None = None
):
    """
    Quản lý xem các project đang chờ duyệt.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_MANAGER:
        return RedirectResponse("/employee", status_code=303)

    with get_connection() as conn:
        project_requests = conn.execute(
            """
            SELECT *
            FROM projects
            WHERE status = 'PENDING'
            ORDER BY id ASC
            """
        ).fetchall()

    return templates.TemplateResponse(
        request=request,
        name="manager_project_requests.html",
        context={
            "user": current_user,
            "project_requests": project_requests,
            "message": message,
            "error": error,
        }
    )
@app.post("/manager/projects/{project_id}/approve")
def approve_project_request(
    request: Request,
    project_id: int
):
    """
    Quản lý duyệt project mới do nhân viên yêu cầu.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_MANAGER:
        return RedirectResponse("/employee", status_code=303)

    reviewed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as conn:
        project = conn.execute(
            """
            SELECT *
            FROM projects
            WHERE id = ?
              AND status = 'PENDING'
            """,
            (project_id,),
        ).fetchone()

        if not project:
            return RedirectResponse(
                "/manager/project-requests?error=Không+tìm+thấy+project+chờ+duyệt.",
                status_code=303,
            )

        conn.execute(
            """
            UPDATE projects
            SET status = 'APPROVED',
                reviewed_by = ?,
                reviewed_at = ?,
                rejection_reason = NULL
            WHERE id = ?
            """,
            (
                current_user["full_name"],
                reviewed_at,
                project_id,
            ),
        )

    create_project_storage_folders(project["folder"])

    write_audit_log(
        user=current_user,
        action="APPROVE_PROJECT",
        details=f"Duyệt project mới: '{project['label']}'.",
    )

    return RedirectResponse(
        "/manager/project-requests?message=Đã+duyệt+project+thành+công.",
        status_code=303,
    )
@app.post("/manager/projects/{project_id}/reject")
def reject_project_request(
    request: Request,
    project_id: int,
    rejection_reason: str = Form("")
):
    """
    Quản lý từ chối project mới do nhân viên yêu cầu.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_MANAGER:
        return RedirectResponse("/employee", status_code=303)

    reason = rejection_reason.strip()

    if not reason:
        reason = "Quản lý từ chối nhưng chưa ghi lý do."

    reviewed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as conn:
        project = conn.execute(
            """
            SELECT *
            FROM projects
            WHERE id = ?
              AND status = 'PENDING'
            """,
            (project_id,),
        ).fetchone()

        if not project:
            return RedirectResponse(
                "/manager/project-requests?error=Không+tìm+thấy+project+chờ+duyệt.",
                status_code=303,
            )

        conn.execute(
            """
            UPDATE projects
            SET status = 'REJECTED',
                reviewed_by = ?,
                reviewed_at = ?,
                rejection_reason = ?
            WHERE id = ?
            """,
            (
                current_user["full_name"],
                reviewed_at,
                reason,
                project_id,
            ),
        )

    write_audit_log(
        user=current_user,
        action="REJECT_PROJECT",
        details=f"Từ chối project '{project['label']}'. Lý do: {reason}",
    )

    return RedirectResponse(
        "/manager/project-requests?message=Đã+từ+chối+project.",
        status_code=303,
    )
# ============================================================
# QUẢN LÝ TÀI KHOẢN
# ============================================================

@app.get("/manager/users", response_class=HTMLResponse)
def manager_users_page(request: Request):
    """
    Trang chỉ dành cho quản lý.

    Hiển thị toàn bộ tài khoản nhân viên và quản lý.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if not is_admin_user(current_user):
        return RedirectResponse("/manager", status_code=303)

    with get_connection() as conn:
        users = conn.execute(
            """
            SELECT
    id,
    username,
    full_name,
    role,
    created_at,
    COALESCE(is_active, 1) AS is_active,
    COALESCE(approval_status, 'APPROVED') AS approval_status
FROM users
            ORDER BY
    CASE
        WHEN approval_status = 'PENDING' THEN 0
        WHEN role = 'MANAGER' THEN 1
        ELSE 2
    END,
    id DESC
            """
        ).fetchall()

    return templates.TemplateResponse(
        request=request,
        name="manager_users.html",
        context={
            "user": current_user,
            "users": users,
        }
    )
@app.post("/manager/users/{user_id}/toggle-status")
def toggle_user_status(
    request: Request,
    user_id: int
):
    """
    Quản lý khóa hoặc mở lại tài khoản.

    Quản lý không được tự khóa chính tài khoản mình đang dùng.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if not is_admin_user(current_user):
        return RedirectResponse("/manager", status_code=303)

    if user_id == current_user["id"]:
        return RedirectResponse(
            "/manager/users",
            status_code=303,
        )

    with get_connection() as conn:
        target_user = conn.execute(
            """
            SELECT
                id,
                username,
                full_name,
                role,
                COALESCE(is_active, 1) AS is_active
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()

        if not target_user:
            return RedirectResponse(
                "/manager/users",
                status_code=303,
            )

        new_status = 0 if target_user["is_active"] else 1

        conn.execute(
            """
            UPDATE users
            SET is_active = ?
            WHERE id = ?
            """,
            (new_status, user_id),
        )

    action_text = "MỞ KHÓA" if new_status else "KHÓA"

    write_audit_log(
        user=current_user,
        action="USER_STATUS_CHANGE",
        details=(
            f"{action_text} tài khoản "
            f"'{target_user['username']}' "
            f"({target_user['full_name']})."
        ),
    )

    return RedirectResponse(
        "/manager/users",
        status_code=303,
    )
@app.post("/manager/users/{user_id}/approve")
def approve_user_account(
    request: Request,
    user_id: int
):
    """
    Quản lý duyệt tài khoản nhân viên mới đăng ký.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if not is_admin_user(current_user):
        return RedirectResponse("/manager", status_code=303)

    with get_connection() as conn:
        target_user = conn.execute(
            """
            SELECT
                id,
                username,
                full_name,
                role,
                COALESCE(approval_status, 'APPROVED') AS approval_status
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()

        if not target_user:
            return RedirectResponse("/manager/users", status_code=303)

        if target_user["role"] == ROLE_MANAGER:
            return RedirectResponse("/manager/users", status_code=303)

        conn.execute(
            """
            UPDATE users
            SET approval_status = 'APPROVED',
                is_active = 1
            WHERE id = ?
            """,
            (user_id,),
        )

    write_audit_log(
        user=current_user,
        action="APPROVE_USER",
        details=(
            f"Duyệt tài khoản '{target_user['username']}' "
            f"({target_user['full_name']})."
        ),
    )

    return RedirectResponse("/manager/users", status_code=303)
@app.post("/manager/users/{user_id}/reject")
def reject_user_account(
    request: Request,
    user_id: int
):
    """
    Quản lý từ chối tài khoản nhân viên mới đăng ký.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if not is_admin_user(current_user):
        return RedirectResponse("/manager", status_code=303)

    with get_connection() as conn:
        target_user = conn.execute(
            """
            SELECT
                id,
                username,
                full_name,
                role,
                COALESCE(approval_status, 'APPROVED') AS approval_status
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()

        if not target_user:
            return RedirectResponse("/manager/users", status_code=303)

        if target_user["role"] == ROLE_MANAGER:
            return RedirectResponse("/manager/users", status_code=303)

        conn.execute(
            """
            UPDATE users
            SET approval_status = 'REJECTED',
                is_active = 0
            WHERE id = ?
            """,
            (user_id,),
        )

    write_audit_log(
        user=current_user,
        action="REJECT_USER",
        details=(
            f"Từ chối tài khoản '{target_user['username']}' "
            f"({target_user['full_name']})."
        ),
    )

    return RedirectResponse("/manager/users", status_code=303)

@app.post("/manager/documents/{document_id}/approve")
def approve_document_on_web(
    request: Request,
    document_id: int
):
    """
    Quản lý duyệt hồ sơ:

    pending/
    → storage/file_loai_1, file_loai_2 hoặc file_loai_3/

    PENDING
    → APPROVED
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_MANAGER:
        return RedirectResponse("/employee", status_code=303)

    with get_connection() as conn:
        document = conn.execute(
            """
            SELECT *
            FROM documents
            WHERE id = ?
              AND status = 'PENDING'
            """,
            (document_id,),
        ).fetchone()

    if not document:
        return RedirectResponse(
            "/manager/pending?error=Không+tìm+thấy+hồ+sơ+chờ+duyệt.",
            status_code=303,
        )

    source = Path(document["file_path"])

    if not source.exists():
        return RedirectResponse(
            "/manager/pending?error=Không+tìm+thấy+file+thật+trong+pending.",
            status_code=303,
        )

    try:
        ensured = ensure_approved_project_and_category_for_document(
            project_label=document["project"],
            category_label=document["category"],
            requested_category_code=document["requested_category_code"] or "",
            current_user=current_user,
        )
    except Exception as error:
        return RedirectResponse(
            f"/manager/pending?error=Không+thể+tạo+project/file+con+khi+duyệt:+{quote(str(error))}",
            status_code=303,
        )

    destination_folder = storage_folder_from_project_and_category(
        ensured["project_label"],
        ensured["category_label"]
    )

    destination_folder.mkdir(parents=True, exist_ok=True)
    destination = destination_folder / document["stored_name"]

    reviewed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        # Chuyển file thật từ pending sang storage.
        shutil.move(str(source), str(destination))

        # Đổi trạng thái trong database.
        with get_connection() as conn:
            document_code = document["document_code"]

            if not document_code:
                document_code = generate_document_code(
                    conn,
                    ensured["project_label"],
                    ensured["category_label"],
                )

            conn.execute(
                """
                UPDATE documents
                SET document_code = ?,
                    project = ?,
                    category = ?,
                    status = 'APPROVED',
                    file_path = ?,
                    reviewed_at = ?,
                    reviewed_by = ?,
                    rejection_reason = NULL
                WHERE id = ?
                """,
                (
                    document_code,
                    ensured["project_label"],
                    ensured["category_label"],
                    str(destination),
                    reviewed_at,
                    current_user["full_name"],
                    document_id,
                ),
            )

        write_audit_log(
                user=current_user,
                action="APPROVE",
                document_id=document_id,
                details=(
                    f"Đã duyệt hồ sơ '{document['original_name']}' "
                    f"và chuyển vào kho chính thức."
                ),
            )

        return RedirectResponse(
            "/manager/pending?message=Đã+duyệt+hồ+sơ,+project/file+con+thành+công.",
            status_code=303,
        )

    except Exception as error:
        # Nếu đã chuyển file nhưng database lỗi,
        # thử chuyển file về lại pending.
        if destination.exists() and not source.exists():
            shutil.move(str(destination), str(source))

        return RedirectResponse(
            f"/manager/pending?error=Lỗi+khi+duyệt+hồ+sơ:+{str(error)}",
            status_code=303,
        )
@app.post("/manager/documents/{document_id}/reject")
def reject_document_on_web(
    request: Request,
    document_id: int,
    rejection_reason: str = Form("")
):
    """
    Quản lý từ chối hồ sơ:

    pending/
    → rejected/

    PENDING
    → REJECTED
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_MANAGER:
        return RedirectResponse("/employee", status_code=303)

    with get_connection() as conn:
        document = conn.execute(
            """
            SELECT *
            FROM documents
            WHERE id = ?
              AND status = 'PENDING'
            """,
            (document_id,),
        ).fetchone()

    if not document:
        return RedirectResponse(
            "/manager/pending?error=Không+tìm+thấy+hồ+sơ+chờ+duyệt.",
            status_code=303,
        )

    source = Path(document["file_path"])

    if not source.exists():
        return RedirectResponse(
            "/manager/pending?error=Không+tìm+thấy+file+thật+trong+pending.",
            status_code=303,
        )

    reason = rejection_reason.strip()

    if not reason:
        reason = "Quản lý từ chối nhưng chưa ghi lý do."

    destination = REJECTED_DIR / document["stored_name"]

    reviewed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        # Chuyển file thật từ pending sang rejected.
        shutil.move(str(source), str(destination))

        # Cập nhật database.
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE documents
                SET status = 'REJECTED',
                    file_path = ?,
                    reviewed_at = ?,
                    reviewed_by = ?,
                    rejection_reason = ?
                WHERE id = ?
                """,
                (
                    str(destination),
                    reviewed_at,
                    current_user["full_name"],
                    reason,
                    document_id,
                ),
            )

        write_audit_log(
                user=current_user,
                action="REJECT",
                document_id=document_id,
                details=(
                    f"Từ chối hồ sơ '{document['original_name']}'. "
                    f"Lý do: {reason}"
                ),
            )

        return RedirectResponse(
            "/manager/pending?message=Đã+từ+chối+hồ+sơ.",
            status_code=303,
        )

    except Exception as error:
        # Nếu database lỗi, cố gắng đưa file về pending.
        if destination.exists() and not source.exists():
            shutil.move(str(destination), str(source))

        return RedirectResponse(
            f"/manager/pending?error=Lỗi+khi+từ+chối+hồ+sơ:+{str(error)}",
            status_code=303,
        )
# ============================================================
# KHO HỒ SƠ ĐÃ ĐƯỢC DUYỆT
# ============================================================

@app.get("/documents/approved", response_class=HTMLResponse)
def approved_documents_page(
    request: Request,
    keyword: str = "",
    project: str = "",
    category: str = "",
    submitted_by: str = "",
    date_from: str = "",
    date_to: str = "",
    error: str | None = None
):
    """
    Hiển thị và tìm kiếm hồ sơ đã được duyệt.

    Tất cả tài khoản đang đăng nhập đều xem được toàn bộ hồ sơ APPROVED.
    Có thể lọc theo:
    - tên file
    - project
    - loại hồ sơ
    - người gửi
    - ngày gửi
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    keyword = keyword.strip()
    project = project.strip()
    category = category.strip()
    submitted_by = submitted_by.strip()
    date_from = date_from.strip()
    date_to = date_to.strip()

    query = """
        SELECT *
        FROM documents
        WHERE status = 'APPROVED'
    """

    params = []

    # Lọc theo từ khóa: mã hồ sơ, tên file, mô tả, vị trí, project, loại hồ sơ, người gửi
    if keyword:
        query += """
            AND (
                lower(COALESCE(document_code, '')) LIKE lower(?)
                OR lower(COALESCE(original_name, '')) LIKE lower(?)
                OR lower(COALESCE(description, '')) LIKE lower(?)
                OR lower(COALESCE(location, '')) LIKE lower(?)
                OR lower(COALESCE(project, '')) LIKE lower(?)
                OR lower(COALESCE(category, '')) LIKE lower(?)
                OR lower(COALESCE(submitted_by, '')) LIKE lower(?)
            )
        """
        keyword_like = f"%{keyword}%"
        params.extend([
            keyword_like,
            keyword_like,
            keyword_like,
            keyword_like,
            keyword_like,
            keyword_like,
            keyword_like,
        ])

    # Lọc theo project
    approved_projects = get_approved_projects()

    valid_projects = {
        item["label"]
        for item in approved_projects.values()
    }

    if project in valid_projects:
        query += """
            AND project = ?
        """
        params.append(project)

    # Lọc theo file con / loại hồ sơ
    if category:
        query += """
            AND category = ?
        """
        params.append(category)

    # Lọc theo người gửi
    if submitted_by:
        query += """
            AND lower(submitted_by) LIKE lower(?)
        """
        params.append(f"%{submitted_by}%")

    # Lọc từ ngày gửi
    if date_from:
        query += """
            AND submitted_at >= ?
        """
        params.append(f"{date_from} 00:00:00")

    # Lọc đến ngày gửi
    if date_to:
        query += """
            AND submitted_at <= ?
        """
        params.append(f"{date_to} 23:59:59")

    query += """
        ORDER BY id DESC
    """

    with get_connection() as conn:
        documents = conn.execute(
            query,
            params,
        ).fetchall()

        stats = conn.execute(
            """
            SELECT
                COUNT(*) AS total_uploaded_documents,
                SUM(
                    CASE
                        WHEN status = 'APPROVED' THEN 1
                        ELSE 0
                    END
                ) AS total_approved_documents
            FROM documents
            """
        ).fetchone()

    with get_connection() as conn:
        total_project_count = conn.execute(
            """
            SELECT COUNT(*) AS total_project_count
            FROM projects
            WHERE status = 'APPROVED'
            """
        ).fetchone()["total_project_count"]
    total_uploaded_documents = stats["total_uploaded_documents"] or 0
    total_approved_documents = stats["total_approved_documents"] or 0

    return templates.TemplateResponse(
        request=request,
        name="approved_documents.html",
        context={
            "user": current_user,
            "documents": documents,
            "projects": get_approved_projects(),
            "categories": get_all_active_categories(),
            "filters": {
                "keyword": keyword,
                "project": project,
                "category": category,
                "submitted_by": submitted_by,
                "date_from": date_from,
                "date_to": date_to,
            },
            "error": error,

            "total_project_count": total_project_count,
            "total_uploaded_documents": total_uploaded_documents,
            "total_approved_documents": total_approved_documents,
        }
    )
@app.get("/documents/approved/live-search")
def approved_documents_live_search(
    request: Request,
    keyword: str = "",
    project: str = "",
    category: str = "",
    submitted_by: str = "",
    date_from: str = "",
    date_to: str = "",
):
    current_user = get_current_user(request)

    if not current_user:
        return JSONResponse(
            {
                "success": False,
                "message": "Not logged in",
                "documents": [],
            },
            status_code=401,
        )

    query = """
        SELECT
            id,
            document_code,
            original_name,
            description,
            location,
            project,
            category,
            submitted_by,
            submitted_at,
            reviewed_at,
            reviewed_by,
            status
        FROM documents
        WHERE status = 'APPROVED'
    """

    params = []

    keyword = keyword.strip()
    project = project.strip()
    category = category.strip()
    submitted_by = submitted_by.strip()
    date_from = date_from.strip()
    date_to = date_to.strip()

    if keyword:
        query += """
            AND (
                LOWER(COALESCE(document_code, '')) LIKE ?
                OR LOWER(COALESCE(original_name, '')) LIKE ?
                OR LOWER(COALESCE(description, '')) LIKE ?
                OR LOWER(COALESCE(location, '')) LIKE ?
                OR LOWER(COALESCE(project, '')) LIKE ?
                OR LOWER(COALESCE(category, '')) LIKE ?
                OR LOWER(COALESCE(submitted_by, '')) LIKE ?
            )
        """
        keyword_like = f"%{keyword.lower()}%"
        params.extend([
            keyword_like,
            keyword_like,
            keyword_like,
            keyword_like,
            keyword_like,
            keyword_like,
            keyword_like,
        ])

    if project:
        query += " AND project = ?"
        params.append(project)

    if category:
        query += " AND category = ?"
        params.append(category)

    if submitted_by:
        query += " AND LOWER(COALESCE(submitted_by, '')) LIKE ?"
        params.append(f"%{submitted_by.lower()}%")

    if date_from:
        query += " AND submitted_at >= ?"
        params.append(f"{date_from} 00:00:00")

    if date_to:
        query += " AND submitted_at <= ?"
        params.append(f"{date_to} 23:59:59")

    query += " ORDER BY id DESC"

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()

    documents = []

    for row in rows:
        documents.append({
            "id": row["id"],
            "document_code": row["document_code"] or str(row["id"]),
            "original_name": row["original_name"] or "",
            "description": row["description"] or "",
            "location": row["location"] or "",
            "project": row["project"] or "",
            "category": row["category"] or "",
            "submitted_by": row["submitted_by"] or "",
            "submitted_at": row["submitted_at"] or "",
            "reviewed_at": row["reviewed_at"] or "",
            "reviewed_by": row["reviewed_by"] or "",
        })

    return {
        "success": True,
        "is_manager": current_user["role"] == ROLE_MANAGER,
        "documents": documents,
    }

@app.get("/documents/{document_id}/download")
def download_approved_document_web(
    request: Request,
    document_id: int
):
    """
    Download một hồ sơ đã được duyệt.

    Chỉ người dùng đã đăng nhập mới có thể tải.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    with get_connection() as conn:
        document = conn.execute(
            """
            SELECT *
            FROM documents
            WHERE id = ?
              AND status = 'APPROVED'
            """,
            (document_id,),
        ).fetchone()

    if not document:
        return RedirectResponse(
            "/documents/approved?error=Không+tìm+thấy+hồ+sơ+đã+được+duyệt.",
            status_code=303,
        )

    source = Path(document["file_path"])

    if not source.exists():
        return RedirectResponse(
            "/documents/approved?error=File+thật+không+còn+trong+kho+lưu+trữ.",
            status_code=303,
        )

    write_audit_log(
        user=current_user,
        action="DOWNLOAD",
        document_id=document_id,
        details=(
            f"Download hồ sơ '{document['original_name']}'."
        ),
    )

    media_type, _ = mimetypes.guess_type(str(source))

    if not media_type:
        media_type = "application/octet-stream"

    safe_filename = quote(document["original_name"])

    return FileResponse(
        path=source,
        media_type=media_type,
        headers={
            "Content-Disposition": (
                f"inline; filename*=UTF-8''{safe_filename}"
            )
        },
    )
@app.post("/documents/{document_id}/delete")
def delete_approved_document_web(
    request: Request,
    document_id: int
):
    """
    Quản lý xóa hẳn hồ sơ khỏi hệ thống.

    Khi xóa:
    - Xóa file thật khỏi các thư mục lưu trữ.
    - Xóa dòng dữ liệu khỏi bảng documents.
    - Hồ sơ sẽ không còn hiển thị trong kho.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_MANAGER:
        return RedirectResponse("/documents/approved", status_code=303)

    with get_connection() as conn:
        document = conn.execute(
            """
            SELECT *
            FROM documents
            WHERE id = ?
            """,
            (document_id,),
        ).fetchone()

    if not document:
        return RedirectResponse(
            "/documents/approved?error=Không+tìm+thấy+hồ+sơ+cần+xóa.",
            status_code=303,
        )

    deleted_file_count = 0
    file_paths_to_delete = set()

    # File path đang lưu trong database
    if document["file_path"]:
        file_paths_to_delete.add(Path(document["file_path"]))

    # Tìm thêm file trùng stored_name trong các thư mục hệ thống
    folders_to_check = [
        PENDING_DIR,
        STORAGE_DIR,
        REJECTED_DIR,
        DOWNLOADS_DIR,
    ]

    for folder in folders_to_check:
        if folder.exists():
            for matched_file in folder.rglob(document["stored_name"]):
                file_paths_to_delete.add(matched_file)

    try:
        # Xóa file thật khỏi ổ đĩa.
        # Dùng force_delete_file để tránh lỗi OneDrive/Windows khóa file tạm thời.
        for file_path in file_paths_to_delete:
            if force_delete_file(file_path):
                deleted_file_count += 1

        # Xóa hồ sơ khỏi database
        with get_connection() as conn:
            conn.execute(
                """
                DELETE FROM documents
                WHERE id = ?
                """,
                (document_id,),
            )

        write_audit_log(
            user=current_user,
            action="DELETE_DOCUMENT",
            document_id=document_id,
            details=(
                f"Xóa hẳn hồ sơ '{document['original_name']}'. "
                f"Số file thật đã xóa: {deleted_file_count}."
            ),
        )

        return RedirectResponse(
            "/documents/approved",
            status_code=303,
        )

    except Exception as error:
        return RedirectResponse(
            f"/documents/approved?error=Không+thể+xóa+hồ+sơ:+{quote(str(error))}",
            status_code=303,
        )
@app.post("/projects/{project_id}/delete")
def delete_project_web(
    request: Request,
    project_id: int
):
    """
    Quản lý xóa project khỏi hệ thống.

    Khi xóa project:
    - Xóa toàn bộ hồ sơ thuộc project khỏi bảng documents.
    - Xóa file thật nếu OneDrive/Windows cho phép.
    - Cố gắng xóa thư mục storage của project.
    - Xóa hẳn dòng project khỏi bảng projects để project_code được giải phóng.
    - Xóa luôn danh sách file con trong project_categories.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_MANAGER:
        return RedirectResponse("/documents/approved", status_code=303)

    with get_connection() as conn:
        project = conn.execute(
            """
            SELECT *
            FROM projects
            WHERE id = ?
              AND status = 'APPROVED'
            """,
            (project_id,),
        ).fetchone()

    if not project:
        return RedirectResponse(
            "/documents/approved?error=Không+tìm+thấy+project+cần+xóa.",
            status_code=303,
        )

    project_label = project["label"]
    project_folder_name = project["folder"]
    project_code = project["project_code"] or ""

    with get_connection() as conn:
        documents = conn.execute(
            """
            SELECT *
            FROM documents
            WHERE project = ?
            """,
            (project_label,),
        ).fetchall()

    file_paths_to_delete = set()

    for document in documents:
        if document["file_path"]:
            file_paths_to_delete.add(Path(document["file_path"]))

        if document["stored_name"]:
            folders_to_check = [
                PENDING_DIR,
                STORAGE_DIR,
                REJECTED_DIR,
                DOWNLOADS_DIR,
            ]

            for folder in folders_to_check:
                if folder.exists():
                    for matched_file in folder.rglob(document["stored_name"]):
                        file_paths_to_delete.add(matched_file)

    deleted_file_count = 0

    try:
        # Xóa từng file thật liên quan đến project.
        # Nếu OneDrive đang khóa file, force_delete_file sẽ retry và bỏ qua nếu vẫn bị khóa.
        for file_path in file_paths_to_delete:
            if force_delete_file(file_path):
                deleted_file_count += 1

        # Cố gắng xóa toàn bộ thư mục storage của project.
        # Nếu OneDrive đang khóa folder thì không làm lỗi web.
        project_storage_folder = STORAGE_DIR / project_folder_name
        folder_deleted = try_delete_folder(project_storage_folder)

        # Xóa dữ liệu project khỏi database.
        # Dùng DELETE thật để mã project được xóa/giải phóng hoàn toàn.
        with get_connection() as conn:
            hard_delete_project_metadata(
                conn=conn,
                project_id=project_id,
                project_label=project_label,
            )

        folder_delete_text = (
            "Đã xóa folder project."
            if folder_deleted
            else "Không xóa được folder project do OneDrive/Windows đang khóa."
        )

        write_audit_log(
            user=current_user,
            action="DELETE_PROJECT",
            details=(
                f"Xóa project '{project_label}'. "
                f"Mã project đã xóa/giải phóng: {project_code}. "
                f"Số hồ sơ đã xóa: {len(documents)}. "
                f"Số file thật đã xóa: {deleted_file_count}. "
                f"{folder_delete_text}"
            ),
        )

        return RedirectResponse(
            "/documents/approved",
            status_code=303,
        )

    except Exception as error:
        return RedirectResponse(
            f"/documents/approved?error=Không+thể+xóa+project:+{quote(str(error))}",
            status_code=303,
        )
# ============================================================
# NHẬT KÝ HOẠT ĐỘNG
# ============================================================

@app.get("/manager/audit-logs", response_class=HTMLResponse)
def manager_audit_logs(
    request: Request,
    action: str = ""
):
    """
    Trang chỉ dành cho quản lý.

    Hiển thị các hoạt động:
    - UPLOAD
    - APPROVE
    - REJECT
    - DOWNLOAD
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if not is_admin_user(current_user):
        return RedirectResponse("/manager", status_code=303)

    action = action.strip().upper()

    query = """
        SELECT *
        FROM audit_logs
        WHERE 1 = 1
    """

    params = []

    # Nếu quản lý chọn lọc theo hành động
    if action in (
            "UPLOAD",
            "MANAGER_UPLOAD",
            "APPROVE",
            "REJECT",
            "DOWNLOAD",
    ):
        query += """
            AND action = ?
        """
        params.append(action)

    query += """
        ORDER BY id DESC
        LIMIT 200
    """

    with get_connection() as conn:
        logs = conn.execute(
            query,
            params
        ).fetchall()

    return templates.TemplateResponse(
        request=request,
        name="audit_logs.html",
        context={
            "user": current_user,
            "logs": logs,
            "selected_action": action,
        }
    )
# ============================================================
# DASHBOARD ADMIN
# ============================================================

@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request):
    """
    Trang dành riêng cho admin.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if not is_admin_user(current_user):
        return RedirectResponse("/", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="admin_dashboard.html",
        context={
            "user": current_user,
        }
    )
@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request):
    """
    Admin xem danh sách user để nâng quyền nhân viên thành quản lý.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if not is_admin_user(current_user):
        return RedirectResponse("/", status_code=303)

    with get_connection() as conn:
        users = conn.execute(
            """
            SELECT
                id,
                username,
                full_name,
                role,
                created_at,
                COALESCE(is_active, 1) AS is_active,
                COALESCE(approval_status, 'APPROVED') AS approval_status,
                COALESCE(is_admin, 0) AS is_admin
            FROM users
            ORDER BY
                CASE
                    WHEN is_admin = 1 THEN 0
                    WHEN role = 'MANAGER' THEN 1
                    ELSE 2
                END,
                id DESC
            """
        ).fetchall()

    return templates.TemplateResponse(
        request=request,
        name="admin_users.html",
        context={
            "user": current_user,
            "users": users,
        }
    )
@app.post("/admin/users/{user_id}/promote-manager")
def promote_user_to_manager(
    request: Request,
    user_id: int
):
    """
    Admin chuyển tài khoản nhân viên thành quản lý.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if not is_admin_user(current_user):
        return RedirectResponse("/", status_code=303)

    if user_id == current_user["id"]:
        return RedirectResponse("/admin/users", status_code=303)

    with get_connection() as conn:
        target_user = conn.execute(
            """
            SELECT
                id,
                username,
                full_name,
                role,
                COALESCE(is_admin, 0) AS is_admin
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()

        if not target_user:
            return RedirectResponse("/admin/users", status_code=303)

        if target_user["is_admin"]:
            return RedirectResponse("/admin/users", status_code=303)

        conn.execute(
            """
            UPDATE users
            SET role = 'MANAGER',
                is_active = 1,
                approval_status = 'APPROVED'
            WHERE id = ?
            """,
            (user_id,),
        )

    write_audit_log(
        user=current_user,
        action="PROMOTE_MANAGER",
        details=(
            f"Admin chuyển tài khoản '{target_user['username']}' "
            f"({target_user['full_name']}) thành quản lý."
        ),
    )

    return RedirectResponse("/admin/users", status_code=303)

@app.get("/admin/create-manager", response_class=HTMLResponse)
def admin_create_manager_page(request: Request):
    """
    Form tạo tài khoản quản lý.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if not is_admin_user(current_user):
        return RedirectResponse("/", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="admin_create_manager.html",
        context={
            "user": current_user,
            "error": None,
            "success": None,
        }
    )


@app.post("/admin/create-manager", response_class=HTMLResponse)
def admin_create_manager(
    request: Request,
    username: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...)
):
    """
    Admin tạo tài khoản quản lý mới.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if not is_admin_user(current_user):
        return RedirectResponse("/", status_code=303)

    def show_page(error=None, success=None):
        return templates.TemplateResponse(
            request=request,
            name="admin_create_manager.html",
            context={
                "user": current_user,
                "error": error,
                "success": success,
            }
        )

    username = username.strip().lower()
    full_name = full_name.strip()

    if not username:
        return show_page(error="Username không được để trống.")

    if not full_name:
        return show_page(error="Họ tên không được để trống.")

    if len(password) < 8:
        return show_page(error="Mật khẩu cần ít nhất 8 ký tự.")

    if password != confirm_password:
        return show_page(error="Hai mật khẩu không khớp.")

    password_salt, password_hash = hash_password(password)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO users (
                    username,
                    full_name,
                    role,
                    password_salt,
                    password_hash,
                    created_at,
                    is_active,
                    approval_status,
                    is_admin
                )
                VALUES (?, ?, 'MANAGER', ?, ?, ?, 1, 'APPROVED', 0)
                """,
                (
                    username,
                    full_name,
                    password_salt,
                    password_hash,
                    created_at,
                ),
            )

    except sqlite3.IntegrityError:
        return show_page(
            error="Username này đã tồn tại."
        )

    write_audit_log(
        user=current_user,
        action="CREATE_MANAGER",
        details=(
            f"Admin tạo tài khoản quản lý '{username}' "
            f"({full_name})."
        ),
    )

    return show_page(
        success=(
            f"Đã tạo tài khoản quản lý '{username}' thành công."
        )
    )
# ============================================================
# ĐĂNG XUẤT
# ============================================================

@app.get("/logout")
def logout(request: Request):
    """
    Đăng xuất khỏi máy/trình duyệt hiện tại.
    Chỉ xóa session của máy này, không ảnh hưởng máy khác.
    """

    session_token = request.session.get("session_token")

    if session_token:
        with get_connection() as conn:
            conn.execute("""
                DELETE FROM user_sessions
                WHERE session_token = ?
            """, (session_token,))

    request.session.clear()

    return RedirectResponse("/", status_code=303)