import json
import re
import sqlite3
import unicodedata
import uuid
from datetime import datetime

from core.config import (
    CATEGORY_MAP,
    NEW_PROJECT_KEY,
    NEW_CATEGORY_KEY,
    DEFAULT_CATEGORY_PREFIX,
    STORAGE_DIR,
)
from database.connection import get_connection
from services.documents import (
    category_code_from_label,
    cleanup_deleted_project_records,
    write_audit_log,
)


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


def get_canonical_category_project_id(conn) -> int:
    """
    Trả về project_id "ảo" dùng làm nơi lưu danh sách "Loại hồ sơ" dùng
    chung cho toàn bộ hệ thống. Cố định = 0 — project thật luôn có id >= 1
    (AUTOINCREMENT) nên 0 không bao giờ trùng, và vì vậy không bao giờ bị
    xóa kèm khi một project thật bị xóa.

    Trước đây hàm này trả về id của project nhỏ nhất với giả định "project
    không có tính năng xóa" — giả định đó SAI: /projects/{id}/delete xóa
    hẳn project + toàn bộ project_categories của id đó
    (services/documents.hard_delete_project_metadata), nên xóa đúng
    project đang là canonical đã xóa sạch danh sách Loại hồ sơ dùng chung
    của TOÀN hệ thống. `conn` không còn cần thiết nhưng giữ lại tham số để
    không phải sửa lại các nơi đang gọi hàm này.
    """
    return 0


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


def get_user_allowed_categories(user_id: int) -> set:
    """
    Danh sách tên loại hồ sơ (category_label) mà user được phép upload,
    theo bảng user_category_permissions. Chỉ có ý nghĩa khi
    users.category_permissions_enabled = 1.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT category_label
            FROM user_category_permissions
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()

    return {row["category_label"] for row in rows}


def set_user_category_permissions(
    user_id: int,
    enabled: bool,
    category_labels,
):
    """
    Ghi đè toàn bộ danh sách loại hồ sơ được phép của một user, và cập
    nhật cờ bật/tắt giới hạn phân quyền.
    """
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE users
            SET category_permissions_enabled = ?
            WHERE id = ?
            """,
            (1 if enabled else 0, user_id),
        )

        conn.execute(
            "DELETE FROM user_category_permissions WHERE user_id = ?",
            (user_id,),
        )

        for label in category_labels:
            conn.execute(
                """
                INSERT OR IGNORE INTO user_category_permissions
                    (user_id, category_label)
                VALUES (?, ?)
                """,
                (user_id, label),
            )


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


def create_global_category(category_label: str, category_code: str, current_user):
    """
    Thêm 1 "Loại hồ sơ" mới vào danh sách dùng chung cho toàn hệ thống.

    Không dùng create_project_category_for_manager vì hàm đó bắt buộc
    project_id trỏ tới 1 project thật đang APPROVED (JOIN bảng projects) —
    "Loại hồ sơ" dùng chung lại cố tình KHÔNG gắn với project thật nào
    (project_id ảo, xem get_canonical_category_project_id), nên cần hàm
    riêng không phụ thuộc bảng projects.
    """
    category_label = category_label.strip()
    category_code = category_code.strip().upper()

    if not category_label:
        return False, "Tên loại hồ sơ không được để trống.", None

    if len(category_label) > 50:
        return False, "Tên loại hồ sơ không được vượt quá 50 ký tự.", None

    if category_code:
        category_code = re.sub(r"[^A-Z0-9]+", "", category_code)

    if not category_code:
        category_code = make_category_code_from_label(category_label)

    if len(category_code) > 10:
        return False, "Mã loại hồ sơ không được vượt quá 10 ký tự.", None

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as conn:
        project_id = get_canonical_category_project_id(conn)

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
                return False, "Loại hồ sơ này đã tồn tại.", None

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
                action="CREATE_PROJECT_CATEGORY",
                details=f"Bật lại loại hồ sơ '{category_label}'.",
            )

            return True, "Đã bật lại loại hồ sơ thành công.", {
                "id": existing_label["id"],
                "label": category_label,
                "code": category_code,
            }

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
            return False, "Mã loại hồ sơ này đã tồn tại.", None

        folder_name = make_unique_category_folder(
            conn=conn,
            project_id=project_id,
            category_label=category_label,
        )

        category_key = f"CUSTOM_{uuid.uuid4().hex[:12]}"

        cursor = conn.execute(
            """
            INSERT INTO project_categories (
                project_id, category_key, label, folder, code, is_active, created_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (project_id, category_key, category_label, folder_name, category_code, now),
        )

        new_category_id = cursor.lastrowid

    write_audit_log(
        user=current_user,
        action="CREATE_PROJECT_CATEGORY",
        details=f"Thêm loại hồ sơ '{category_label}' mã '{category_code}'.",
    )

    return True, f"Đã thêm loại hồ sơ '{category_label}' thành công.", {
        "id": new_category_id,
        "label": category_label,
        "code": category_code,
    }


def rename_global_category(category_id: int, new_label: str, new_code: str, current_user):
    """
    Đổi tên/mã 1 "Loại hồ sơ" dùng chung. Hồ sơ đã nộp trước đó giữ nguyên
    giá trị category cũ (chỉ là chữ tự do, không phải khóa ngoại) — coi như
    ảnh chụp tại thời điểm nộp, giống cách delete_global_category chỉ ẩn
    chứ không xóa các bản ghi cũ.
    """
    new_label = new_label.strip()
    new_code = new_code.strip().upper()

    if not new_label:
        return False, "Tên loại hồ sơ không được để trống.", None

    if len(new_label) > 50:
        return False, "Tên loại hồ sơ không được vượt quá 50 ký tự.", None

    if new_code:
        new_code = re.sub(r"[^A-Z0-9]+", "", new_code)

    if not new_code:
        new_code = make_category_code_from_label(new_label)

    if len(new_code) > 10:
        return False, "Mã loại hồ sơ không được vượt quá 10 ký tự.", None

    with get_connection() as conn:
        project_id = get_canonical_category_project_id(conn)

        category = conn.execute(
            """
            SELECT id, label
            FROM project_categories
            WHERE id = ? AND project_id = ? AND is_active = 1
            """,
            (category_id, project_id),
        ).fetchone()

        if not category:
            return False, "Loại hồ sơ không tồn tại hoặc đã bị xóa.", None

        old_label = category["label"]

        duplicate_label = conn.execute(
            """
            SELECT id FROM project_categories
            WHERE project_id = ? AND lower(label) = lower(?) AND id != ? AND is_active = 1
            """,
            (project_id, new_label, category_id),
        ).fetchone()

        if duplicate_label:
            return False, "Loại hồ sơ này đã tồn tại.", None

        duplicate_code = conn.execute(
            """
            SELECT id FROM project_categories
            WHERE project_id = ? AND code = ? AND id != ? AND is_active = 1
            """,
            (project_id, new_code, category_id),
        ).fetchone()

        if duplicate_code:
            return False, "Mã loại hồ sơ này đã tồn tại.", None

        conn.execute(
            "UPDATE project_categories SET label = ?, code = ? WHERE id = ?",
            (new_label, new_code, category_id),
        )

    write_audit_log(
        user=current_user,
        action="RENAME_PROJECT_CATEGORY",
        details=f"Đổi tên loại hồ sơ '{old_label}' thành '{new_label}'.",
    )

    return True, f"Đã đổi tên loại hồ sơ thành '{new_label}' thành công.", {
        "id": category_id,
        "label": new_label,
        "code": new_code,
    }


def delete_global_category(category_id: int, current_user):
    """
    Ẩn (soft-delete) 1 "Loại hồ sơ" khỏi danh sách dùng chung. Xem
    create_global_category để biết vì sao không dùng
    delete_project_category_for_manager (hàm đó cũng bắt buộc JOIN với 1
    project thật đang APPROVED).
    """
    with get_connection() as conn:
        project_id = get_canonical_category_project_id(conn)

        category = conn.execute(
            """
            SELECT id, label, is_active
            FROM project_categories
            WHERE id = ?
              AND project_id = ?
            """,
            (category_id, project_id),
        ).fetchone()

        if not category:
            return False, "Loại hồ sơ không tồn tại hoặc đã bị xóa."

        active_count = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM project_categories
            WHERE project_id = ?
              AND is_active = 1
            """,
            (project_id,),
        ).fetchone()["total"]

        if active_count <= 1:
            return False, "Phải còn ít nhất 1 loại hồ sơ."

        conn.execute(
            """
            UPDATE project_categories
            SET is_active = 0
            WHERE id = ?
            """,
            (category_id,),
        )

    write_audit_log(
        user=current_user,
        action="DELETE_PROJECT_CATEGORY",
        details=f"Ẩn loại hồ sơ '{category['label']}'.",
    )

    return True, "Đã xóa loại hồ sơ thành công."


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

    if current_user["category_permissions_enabled"]:
        allowed_categories_json = json.dumps(
            sorted(get_user_allowed_categories(current_user["id"]))
        )
    else:
        allowed_categories_json = "null"

    return {
        "user": current_user,
        "projects": projects,
        "categories": categories,
        "allowed_categories_json": allowed_categories_json,
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


def rename_project(project_id: int, new_name: str, current_user):
    """
    Đổi tên 1 project đang APPROVED.

    project_code/folder giữ nguyên (không phụ thuộc tên hiển thị) nên không
    cần đổi mã hay di chuyển file. Nhưng documents.project là cột chữ tự do
    khớp CHÍNH XÁC theo tên project (không phải khóa ngoại theo id) — mọi
    nơi lọc/xóa hồ sơ theo project (vd. xóa project ở api/documents.py,
    "Hồ sơ của tôi" lọc theo project) đều so khớp theo tên này, nên phải
    cập nhật luôn các hồ sơ đã có để không bị "mất" khỏi project sau khi đổi tên.
    """
    new_name = new_name.strip().upper()

    if not new_name:
        return False, "Tên project không được để trống."

    if len(new_name) > 50:
        return False, "Tên project không được vượt quá 50 ký tự."

    with get_connection() as conn:
        project = conn.execute(
            "SELECT id, label FROM projects WHERE id = ? AND status = 'APPROVED'",
            (project_id,),
        ).fetchone()

        if not project:
            return False, "Không tìm thấy project cần sửa."

        old_label = project["label"]

        if new_name.lower() == old_label.lower():
            return True, f"Đã đổi tên project thành '{new_name}' thành công."

        duplicate = conn.execute(
            "SELECT id FROM projects WHERE lower(label) = lower(?) AND id != ?",
            (new_name, project_id),
        ).fetchone()

        if duplicate:
            return False, "Tên project này đã tồn tại."

        conn.execute("UPDATE projects SET label = ? WHERE id = ?", (new_name, project_id))
        conn.execute("UPDATE documents SET project = ? WHERE project = ?", (new_name, old_label))

    write_audit_log(
        user=current_user,
        action="RENAME_PROJECT",
        details=f"Đổi tên project '{old_label}' thành '{new_name}'.",
    )

    return True, f"Đã đổi tên project thành '{new_name}' thành công."


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


