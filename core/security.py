import hashlib
import hmac
import secrets

from core.config import PASSWORD_ITERATIONS


def hash_password(password: str):
    """
    Tạo salt và password hash để lưu vào database.

    Không bao giờ lưu mật khẩu gốc.
    """

    salt = secrets.token_bytes(16)

    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    ).hex()

    return salt.hex(), password_hash


def verify_password(password, saved_salt, saved_hash):
    """
    Kiểm tra mật khẩu người dùng nhập
    với password hash đang lưu trong database.
    """

    salt = bytes.fromhex(saved_salt)

    entered_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    ).hex()

    return hmac.compare_digest(entered_hash, saved_hash)
