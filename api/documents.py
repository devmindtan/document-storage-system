from pathlib import Path
from urllib.parse import quote
from datetime import datetime
import json
import mimetypes
import os
import re
import shutil
import stat
import time
import uuid

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse

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
)
from core.templates import templates
from database.connection import get_connection
from core.security import hash_password, verify_password
from core.dependencies import (
    create_login_session,
    create_employee_user,
    get_current_user,
    is_admin_user,
)
from services.projects import (
    make_project_code_from_project_name,
    check_project_code_is_available,
    make_folder_name_from_project_name,
    get_approved_projects,
    get_approved_project_by_key,
    get_default_project_key,
    get_default_category_key,
    make_category_folder_from_label,
    make_category_code_from_label,
    make_unique_category_folder,
    seed_project_default_categories,
    get_active_categories_for_project_key,
    get_all_active_categories,
    get_project_category_by_key,
    get_project_category_json_list,
    get_category_management_rows,
    create_project_category_for_manager,
    delete_project_category_for_manager,
    get_default_categories_for_new_project,
    build_upload_context,
    build_manager_upload_context,
    get_next_project_number,
    make_unique_project_folder,
    create_project_storage_folders,
    request_new_project_from_employee,
    create_project_immediately_by_manager,
    normalize_project_label_for_upload,
    get_default_category_from_special_key,
    validate_new_category_input,
    get_or_create_approved_project_by_label,
    get_or_create_active_category_by_label,
    ensure_approved_project_and_category_for_document,
    resolve_upload_selection,
)
from services.documents import (
    write_audit_log,
    remove_readonly_and_retry,
    force_delete_file,
    try_delete_folder,
    hard_delete_project_metadata,
    cleanup_deleted_project_records,
    storage_folder_from_project_and_category,
    project_code_from_label,
    category_code_from_label,
    generate_document_code,
    save_upload_file_to_path,
)

router = APIRouter()


@router.get("/documents/approved", response_class=HTMLResponse)
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


@router.get("/documents/approved/live-search")
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


@router.get("/documents/{document_id}/download")
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


@router.post("/documents/{document_id}/delete")
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

    previous_status = document["status"]

    if previous_status not in ("APPROVED", "REJECTED"):
        return RedirectResponse(
            "/documents/approved?error=Hồ+sơ+này+không+ở+trạng+thái+có+thể+xóa+"
            "(có+thể+đang+được+xử+lý).",
            status_code=303,
        )

    deleting_status = f"DELETING_FROM_{previous_status}"

    # Claim hồ sơ trước khi đụng vào file thật, tránh 2 request cùng xóa
    # 1 hồ sơ song song.
    with get_connection() as conn:
        claim = conn.execute(
            """
            UPDATE documents
            SET status = ?
            WHERE id = ? AND status = ?
            """,
            (deleting_status, document_id, previous_status),
        )

    if claim.rowcount == 0:
        return RedirectResponse(
            "/documents/approved?error=Hồ+sơ+đang+được+xử+lý+bởi+người+khác,+"
            "vui+lòng+tải+lại+trang.",
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

        still_present = [p for p in file_paths_to_delete if p.exists()]

        if still_present:
            with get_connection() as conn:
                conn.execute(
                    """
                    UPDATE documents
                    SET status = ?
                    WHERE id = ?
                    """,
                    (previous_status, document_id),
                )

            write_audit_log(
                user=current_user,
                action="DELETE_DOCUMENT_FAILED",
                document_id=document_id,
                details=(
                    f"Xóa file thất bại cho hồ sơ '{document['original_name']}' "
                    f"({len(still_present)} file còn sót lại). "
                    f"Đã khôi phục trạng thái về {previous_status}."
                ),
            )

            return RedirectResponse(
                "/documents/approved?error=Xóa+file+thất+bại,+hồ+sơ+đã+được+"
                "khôi+phục.+Vui+lòng+thử+lại.",
                status_code=303,
            )

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
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE documents
                SET status = ?
                WHERE id = ?
                """,
                (previous_status, document_id),
            )

        return RedirectResponse(
            f"/documents/approved?error=Không+thể+xóa+hồ+sơ:+{quote(str(error))}",
            status_code=303,
        )


@router.post("/projects/{project_id}/delete")
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
        return JSONResponse({"success": False, "message": "Không tìm thấy project cần xóa."})

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

        return JSONResponse({
            "success": True,
            "message": f"Đã xóa project '{project_label}'.",
        })

    except Exception as error:
        return JSONResponse({
            "success": False,
            "message": f"Không thể xóa project: {error}",
        })
# ============================================================
# NHẬT KÝ HOẠT ĐỘNG
# ============================================================


