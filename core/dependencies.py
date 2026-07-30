import secrets
import sqlite3
from datetime import datetime

from fastapi import Request

from core.config import ROLE_EMPLOYEE
from core.security import hash_password
from database.connection import get_connection


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


def is_admin_user(user) -> bool:
    """
    Kiểm tra tài khoản hiện tại có phải admin không.
    """

    return bool(user and user["is_admin"])


