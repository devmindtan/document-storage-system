import hashlib
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"

PASSWORD_ITERATIONS = 600_000


def hash_password(password: str):
    salt = secrets.token_bytes(16)

    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    ).hex()

    return salt.hex(), password_hash


def ensure_columns():
    with sqlite3.connect(DB_PATH) as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(users)").fetchall()
        }

        if "is_active" not in columns:
            conn.execute(
                """
                ALTER TABLE users
                ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1
                """
            )

        if "approval_status" not in columns:
            conn.execute(
                """
                ALTER TABLE users
                ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'APPROVED'
                """
            )

        if "is_admin" not in columns:
            conn.execute(
                """
                ALTER TABLE users
                ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0
                """
            )


def create_or_update_admin():
    print("=== TẠO / CẬP NHẬT TÀI KHOẢN ADMIN ===")
    print("Database đang dùng:")
    print(DB_PATH)
    print()

    username = input("Username admin: ").strip().lower()
    full_name = input("Họ tên admin: ").strip()

    password = input("Mật khẩu admin: ")
    confirm_password = input("Nhập lại mật khẩu: ")

    if not username:
        print("Username không được để trống.")
        return

    if not full_name:
        print("Họ tên không được để trống.")
        return

    if len(password) < 8:
        print("Mật khẩu cần ít nhất 8 ký tự.")
        return

    if password != confirm_password:
        print("Hai mật khẩu không khớp.")
        return

    password_salt, password_hash = hash_password(password)
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        existing = conn.execute(
            """
            SELECT id
            FROM users
            WHERE lower(username) = lower(?)
            """,
            (username,),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE users
                SET full_name = ?,
                    role = 'MANAGER',
                    password_salt = ?,
                    password_hash = ?,
                    is_active = 1,
                    approval_status = 'APPROVED',
                    is_admin = 1
                WHERE id = ?
                """,
                (
                    full_name,
                    password_salt,
                    password_hash,
                    existing["id"],
                ),
            )

            print("Đã cập nhật tài khoản hiện có thành ADMIN.")

        else:
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
                VALUES (?, ?, 'MANAGER', ?, ?, ?, 1, 'APPROVED', 1)
                """,
                (
                    username,
                    full_name,
                    password_salt,
                    password_hash,
                    created_at,
                ),
            )

            print("Đã tạo tài khoản ADMIN mới.")


if __name__ == "__main__":
    ensure_columns()
    create_or_update_admin()