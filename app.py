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
from core.dependencies import (
    create_login_session,
    create_employee_user,
    get_current_user,
    is_admin_user,
)
from database.schema import (
    initialize_projects,
    initialize_project_categories,
    initialize_audit_logs,
    initialize_user_status,
    initialize_user_sessions,
    initialize_admin_status,
    initialize_user_approval_status,
    initialize_storage_and_documents,
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