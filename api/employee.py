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


@router.get("/employee", response_class=HTMLResponse)
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


@router.get("/employee/my-documents", response_class=HTMLResponse)
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


@router.get("/employee/upload", response_class=HTMLResponse)
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


@router.post("/employee/upload", response_class=HTMLResponse)
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


@router.post("/employee/projects/request", response_class=HTMLResponse)
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


