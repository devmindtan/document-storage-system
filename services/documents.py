import os
import re
import shutil
import stat
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import UploadFile

from core.config import (
    CATEGORY_MAP,
    DOWNLOADS_DIR,
    MAX_FILE_SIZE_MB,
    PENDING_DIR,
    PROJECT_MAP,
    REJECTED_DIR,
    STORAGE_DIR,
)
from database.connection import get_connection


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

    from services.projects import make_category_code_from_label

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


