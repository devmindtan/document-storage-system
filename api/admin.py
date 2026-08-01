from pathlib import Path
from urllib.parse import quote
from datetime import datetime
import json
import mimetypes
import os
import re
import shutil
import sqlite3
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
    get_user_allowed_categories,
    set_user_category_permissions,
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


@router.get("/admin", response_class=HTMLResponse)
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


@router.get("/admin/users", response_class=HTMLResponse)
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


@router.post("/admin/users/{user_id}/promote-manager")
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


@router.post("/admin/users/{user_id}/demote-employee")
def demote_user_to_employee(
    request: Request,
    user_id: int
):
    """
    Admin chuyển tài khoản quản lý (không phải admin) trở về nhân viên.
    Đảo ngược của promote-manager.
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

        if target_user["is_admin"] or target_user["role"] != ROLE_MANAGER:
            return RedirectResponse("/admin/users", status_code=303)

        conn.execute(
            """
            UPDATE users
            SET role = 'EMPLOYEE'
            WHERE id = ?
            """,
            (user_id,),
        )

    write_audit_log(
        user=current_user,
        action="DEMOTE_EMPLOYEE",
        details=(
            f"Admin chuyển tài khoản '{target_user['username']}' "
            f"({target_user['full_name']}) về nhân viên."
        ),
    )

    return RedirectResponse("/admin/users", status_code=303)


@router.get("/admin/create-manager", response_class=HTMLResponse)
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


@router.post("/admin/create-manager", response_class=HTMLResponse)
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


@router.get("/admin/create-employee", response_class=HTMLResponse)
def admin_create_employee_page(request: Request):
    """
    Form tạo tài khoản nhân viên.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if not is_admin_user(current_user):
        return RedirectResponse("/", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="admin_create_employee.html",
        context={
            "user": current_user,
            "error": None,
            "success": None,
        }
    )


@router.post("/admin/create-employee", response_class=HTMLResponse)
def admin_create_employee(
    request: Request,
    username: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...)
):
    """
    Admin tạo tài khoản nhân viên mới, duyệt sẵn ngay (bỏ qua bước
    chờ duyệt vì admin đã trực tiếp tạo tài khoản này).
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if not is_admin_user(current_user):
        return RedirectResponse("/", status_code=303)

    def show_page(error=None, success=None):
        return templates.TemplateResponse(
            request=request,
            name="admin_create_employee.html",
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
                VALUES (?, ?, 'EMPLOYEE', ?, ?, ?, 1, 'APPROVED', 0)
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
        action="CREATE_EMPLOYEE",
        details=(
            f"Admin tạo tài khoản nhân viên '{username}' "
            f"({full_name})."
        ),
    )

    return show_page(
        success=(
            f"Đã tạo tài khoản nhân viên '{username}' thành công."
        )
    )


@router.get("/manager/users/{user_id}/permissions", response_class=HTMLResponse)
def user_category_permissions_page(
    request: Request,
    user_id: int,
    message: str = "",
    error: str = "",
):
    """
    Trang phân quyền loại hồ sơ cho một nhân viên cụ thể.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if not is_admin_user(current_user):
        return RedirectResponse("/", status_code=303)

    with get_connection() as conn:
        target_user = conn.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

    if not target_user or target_user["role"] != ROLE_EMPLOYEE:
        return RedirectResponse("/manager/users", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="user_permissions.html",
        context={
            "user": current_user,
            "target_user": target_user,
            "categories": CATEGORY_MAP,
            "selected_categories": get_user_allowed_categories(user_id),
            "permissions_enabled": bool(target_user["category_permissions_enabled"]),
            "message": message or None,
            "error": error or None,
        }
    )


@router.post("/manager/users/{user_id}/permissions", response_class=HTMLResponse)
def user_category_permissions_save(
    request: Request,
    user_id: int,
    enabled: str = Form(""),
    categories: list = Form([]),
):
    """
    Lưu phân quyền loại hồ sơ cho một nhân viên cụ thể.
    """

    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse("/", status_code=303)

    if not is_admin_user(current_user):
        return RedirectResponse("/", status_code=303)

    with get_connection() as conn:
        target_user = conn.execute(
            "SELECT id, role FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

    if not target_user or target_user["role"] != ROLE_EMPLOYEE:
        return RedirectResponse("/manager/users", status_code=303)

    set_user_category_permissions(
        user_id=user_id,
        enabled=bool(enabled),
        category_labels=categories,
    )

    return RedirectResponse(
        f"/manager/users/{user_id}/permissions?message="
        + quote("Đã lưu phân quyền."),
        status_code=303,
    )
# ============================================================
# ĐĂNG XUẤT
# ============================================================


