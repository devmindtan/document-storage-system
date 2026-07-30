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


@router.get("/statistics", response_class=HTMLResponse)
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


@router.get("/manager/audit-logs", response_class=HTMLResponse)
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


