from pathlib import Path
from typing import List
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


@router.get("/manager", response_class=HTMLResponse)
def manager_dashboard(request: Request):
    """
    Trang dành cho quản lý.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_MANAGER:
        return RedirectResponse("/employee", status_code=303)

    with get_connection() as conn:
        pending_count = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE status = 'PENDING'"
        ).fetchone()[0]

    return templates.TemplateResponse(
        request=request,
        name="manager_dashboard.html",
        context={
            "user": current_user,
            "pending_count": pending_count,
        }
    )


@router.get("/manager/my-documents", response_class=HTMLResponse)
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


@router.get("/manager/upload", response_class=HTMLResponse)
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


@router.post("/manager/upload", response_class=HTMLResponse)
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


@router.post("/manager/projects/create", response_class=HTMLResponse)
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


@router.get("/project-categories/{project_id}")
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


@router.post("/manager/project-categories/add")
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


@router.post("/manager/project-categories/{category_id}/delete")
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


@router.get("/manager/pending", response_class=HTMLResponse)
def manager_pending_documents(
    request: Request,
    keyword: str = "",
    message: str | None = None,
    error: str | None = None
):
    """
    Hiển thị toàn bộ hồ sơ đang chờ quản lý duyệt.

    Có thể lọc theo 1 từ khóa chung (tên file, project, loại hồ sơ, người gửi).
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if current_user["role"] != ROLE_MANAGER:
        return RedirectResponse("/employee", status_code=303)

    keyword = keyword.strip()

    query = """
        SELECT *
        FROM documents
        WHERE status = 'PENDING'
    """
    params = []

    if keyword:
        query += """
            AND (
                lower(COALESCE(original_name, '')) LIKE lower(?)
                OR lower(COALESCE(project, '')) LIKE lower(?)
                OR lower(COALESCE(category, '')) LIKE lower(?)
                OR lower(COALESCE(submitted_by, '')) LIKE lower(?)
            )
        """
        keyword_like = f"%{keyword}%"
        params.extend([keyword_like, keyword_like, keyword_like, keyword_like])

    query += " ORDER BY id ASC"

    with get_connection() as conn:
        documents = conn.execute(query, params).fetchall()

    return templates.TemplateResponse(
        request=request,
        name="manager_pending.html",
        context={
            "user": current_user,
            "documents": documents,
            "message": message,
            "error": error,
            "keyword": keyword,
        }
    )


@router.get("/manager/project-requests", response_class=HTMLResponse)
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


@router.post("/manager/projects/{project_id}/approve")
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


@router.post("/manager/projects/{project_id}/reject")
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


@router.get("/manager/users", response_class=HTMLResponse)
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


@router.post("/manager/users/{user_id}/toggle-status")
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


@router.post("/manager/users/{user_id}/approve")
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


@router.post("/manager/users/{user_id}/reject")
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


@router.post("/manager/documents/{document_id}/approve")
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


@router.post("/manager/documents/{document_id}/reject")
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


