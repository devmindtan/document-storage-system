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


@router.get("/", response_class=HTMLResponse)
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


@router.post("/login", response_class=HTMLResponse)
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


@router.get("/register", response_class=HTMLResponse)
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


@router.post("/register", response_class=HTMLResponse)
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


@router.get("/logout")
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
