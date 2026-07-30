# -*- coding: utf-8 -*-
"""
HỆ THỐNG LƯU TRỮ HỒ SƠ CÓ QUY TRÌNH PHÊ DUYỆT
------------------------------------------------
Luồng hoạt động:
1. Nhân viên gửi hồ sơ.
2. File được copy vào thư mục pending/ (chờ duyệt), CHƯA vào kho chính thức.
3. Quản lý đăng nhập bằng mã quản lý để duyệt hoặc từ chối.
4. Nếu duyệt: file được chuyển vào storage/file_loai_1, 2 hoặc 3.
5. Chỉ hồ sơ có trạng thái APPROVED mới tìm kiếm và download được.

Đây là bản học tập/chạy nội bộ đơn giản. Mã quản lý trong code không an toàn
cho hệ thống thật; khi triển khai thật cần có tài khoản, mật khẩu đã mã hóa,
phân quyền và nhật ký hoạt động.
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import shutil
import sqlite3
import secrets
import uuid
from datetime import datetime
from pathlib import Path

# ============================================================
# 1. CẤU HÌNH HỆ THỐNG
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"

# File nhân viên gửi sẽ nằm ở đây khi đang chờ quản lý phê duyệt.
PENDING_DIR = BASE_DIR / "pending"

# File được duyệt mới được chuyển vào kho chính thức.
STORAGE_DIR = BASE_DIR / "storage"

# File bị từ chối được đưa vào đây để giữ lại bằng chứng/audit.
REJECTED_DIR = BASE_DIR / "rejected"

# File download mô phỏng được copy ra đây.
DOWNLOADS_DIR = BASE_DIR / "downloads"

ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".png", ".jpg", ".jpeg", ".txt", ".zip",
}

MAX_FILE_SIZE_MB = 100

# Số lần xử lý password hash.
# Mức này phù hợp cho bản học tập; khi deploy thật cần đo hiệu năng server.
PASSWORD_ITERATIONS = 600_000

ROLE_EMPLOYEE = "EMPLOYEE"
ROLE_MANAGER = "MANAGER"

CATEGORY_MAP = {
    "1": {"label": "File loại 1", "folder": "file_loai_1"},
    "2": {"label": "File loại 2", "folder": "file_loai_2"},
    "3": {"label": "File loại 3", "folder": "file_loai_3"},
}

STATUS_LABELS = {
    "PENDING": "Chờ quản lý duyệt",
    "APPROVED": "Đã được duyệt",
    "REJECTED": "Đã bị từ chối",
}


# ============================================================
# 2. DATABASE VÀ KHỞI TẠO THƯ MỤC
# ============================================================
def get_connection() -> sqlite3.Connection:
    """Mở kết nối SQLite và cho phép gọi cột theo tên."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_system() -> None:
    """Tạo các thư mục và bảng documents nếu chưa tồn tại."""
    PENDING_DIR.mkdir(exist_ok=True)
    STORAGE_DIR.mkdir(exist_ok=True)
    REJECTED_DIR.mkdir(exist_ok=True)
    DOWNLOADS_DIR.mkdir(exist_ok=True)

    for category in CATEGORY_MAP.values():
        (STORAGE_DIR / category["folder"]).mkdir(
            parents=True,
            exist_ok=True
        )

    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(
                    status IN ('PENDING', 'APPROVED', 'REJECTED')
                ),
                submitted_at TEXT NOT NULL,
                submitted_by TEXT NOT NULL,
                reviewed_at TEXT,
                reviewed_by TEXT,
                rejection_reason TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                full_name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(
                    role IN ('EMPLOYEE', 'MANAGER')
                ),
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_documents_status
            ON documents(status)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_documents_name
            ON documents(original_name)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_documents_submitter
            ON documents(submitted_by)
            """
        )

        # Thêm cột lưu ID tài khoản đã gửi hồ sơ.
        # Chỉ chạy một lần nếu database cũ chưa có cột này.
        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(documents)"
            ).fetchall()
        }

        if "submitted_by_user_id" not in columns:
            conn.execute(
                """
                ALTER TABLE documents
                ADD COLUMN submitted_by_user_id INTEGER
                """
            )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_documents_submitted_by_user_id
            ON documents(submitted_by_user_id)
            """
        )

# ============================================================
# 3. HÀM HỖ TRỢ
# ============================================================
def print_line() -> None:
    print("-" * 94)


def format_size(size_bytes: int) -> str:
    """Đổi byte thành định dạng dễ đọc: KB, MB, GB..."""
    size = float(size_bytes)

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}"

        size /= 1024

    return f"{size_bytes} B"


def clean_path_input(value: str) -> Path:
    """Bỏ dấu nháy/space khi người dùng dán đường dẫn Windows."""
    return Path(
        value.strip().strip('"').strip("'")
    ).expanduser()


def choose_category(allow_all: bool = False) -> str | None:
    """Cho người dùng chọn File loại 1/2/3, hoặc tất cả khi tìm kiếm."""
    print("\nChọn loại hồ sơ:")

    for key, item in CATEGORY_MAP.items():
        print(f"{key}. {item['label']}")

    if allow_all:
        print("0. Tất cả loại")

    while True:
        choice = input("Nhập lựa chọn: ").strip()

        if allow_all and choice == "0":
            return None

        if choice in CATEGORY_MAP:
            return CATEGORY_MAP[choice]["label"]

        print("Lựa chọn không hợp lệ. Vui lòng nhập lại.")


def category_folder_from_label(category_label: str) -> Path:
    """Tìm thư mục kho chính thức theo tên loại hồ sơ."""
    for item in CATEGORY_MAP.values():
        if item["label"] == category_label:
            return STORAGE_DIR / item["folder"]

    raise ValueError("Không tìm thấy thư mục của loại hồ sơ.")


def get_unique_path(folder: Path, original_name: str) -> Path:
    """Tạo đường dẫn không ghi đè file cũ ở folder đích."""
    destination = folder / original_name

    if not destination.exists():
        return destination

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    return folder / (
        f"{Path(original_name).stem}_{timestamp}{Path(original_name).suffix}"
    )


def authenticate_manager():
    """
    Đăng nhập bằng tài khoản quản lý trong bảng users.

    Chỉ tài khoản có role = MANAGER mới được duyệt hồ sơ.
    Hàm trả về họ tên quản lý nếu đăng nhập thành công.
    """

    print("\n=== ĐĂNG NHẬP QUẢN LÝ ===")

    username = input("Nhập username quản lý: ").strip().lower()

    if not username:
        print("Username không được để trống.")
        return None

    password = input("Nhập mật khẩu: ")
    with get_connection() as conn:
        manager = conn.execute(
            """
            SELECT
                username,
                full_name,
                role,
                password_salt,
                password_hash
            FROM users
            WHERE lower(username) = lower(?)
            """,
            (username,),
        ).fetchone()

    # Không tìm thấy tài khoản hoặc tài khoản không phải quản lý
    if not manager or manager["role"] != ROLE_MANAGER:
        print("[TỪ CHỐI] Không tìm thấy tài khoản quản lý hợp lệ.")
        return None

    # Kiểm tra mật khẩu bằng password hash
    is_correct_password = verify_password(
        password,
        manager["password_salt"],
        manager["password_hash"],
    )

    if not is_correct_password:
        print("[TỪ CHỐI] Mật khẩu không đúng.")
        return None

    print(f"[THÀNH CÔNG] Xin chào quản lý: {manager['full_name']}")

    return manager["full_name"]


def validate_submission(source: Path, category: str) -> tuple[bool, str]:
    """Kiểm tra file trước khi đưa vào hàng chờ duyệt."""

    if not source.exists():
        return False, "Không tìm thấy file. Hãy kiểm tra lại đường dẫn."

    if not source.is_file():
        return False, "Đường dẫn này không phải là file."

    extension = source.suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))

        return (
            False,
            f"Định dạng {extension or '(không có đuôi)'} "
            f"chưa được phép. Cho phép: {allowed}"
        )

    if source.stat().st_size > MAX_FILE_SIZE_MB * 1024 * 1024:
        return (
            False,
            f"File vượt quá dung lượng tối đa {MAX_FILE_SIZE_MB} MB."
        )

    # Chặn trùng tên nếu đã có hồ sơ chờ hoặc đã duyệt trong cùng loại.
    with get_connection() as conn:
        existing = conn.execute(
            """
            SELECT id, status
            FROM documents
            WHERE lower(original_name) = lower(?)
              AND category = ?
              AND status IN ('PENDING', 'APPROVED')
            """,
            (source.name, category),
        ).fetchone()

    if existing:
        status_text = STATUS_LABELS[existing["status"]]

        return (
            False,
            f"Đã có hồ sơ cùng tên trong {category} "
            f"(mã: {existing['id']}, trạng thái: {status_text})."
        )

    return True, "File hợp lệ."


def display_records(
    records: list[sqlite3.Row],
    include_reviewer: bool = True
) -> None:
    """Hiển thị danh sách hồ sơ ngắn gọn, rõ ràng."""

    if not records:
        print("\nKhông có hồ sơ nào.")
        return

    print_line()

    print(
        f"{'ID':<5} {'Tên file':<30} {'Loại':<15} "
        f"{'Trạng thái':<22} {'Người gửi':<16} {'Ngày gửi'}"
    )

    print_line()

    for row in records:
        name = row["original_name"]

        if len(name) <= 29:
            display_name = name
        else:
            display_name = name[:26] + "..."

        print(
            f"{row['id']:<5} "
            f"{display_name:<30} "
            f"{row['category']:<15} "
            f"{STATUS_LABELS[row['status']]:<22} "
            f"{row['submitted_by'][:15]:<16} "
            f"{row['submitted_at']}"
        )

    print_line()

    if include_reviewer:
        for row in records:
            if row["status"] == "REJECTED" and row["rejection_reason"]:
                print(
                    f"Hồ sơ #{row['id']} bị từ chối: "
                    f"{row['rejection_reason']}"
                )


def get_document(document_id: int) -> sqlite3.Row | None:
    """Lấy một hồ sơ theo ID."""
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM documents WHERE id = ?",
            (document_id,)
        ).fetchone()
def hash_password(password, salt_hex=None):
    """
    Biến mật khẩu thành password hash.

    Không lưu password gốc vào database.
    Hàm trả về:
    - salt dạng text
    - password hash dạng text
    """

    if salt_hex is None:
        # Tạo salt ngẫu nhiên mới cho tài khoản mới.
        salt = secrets.token_bytes(16)
    else:
        # Khi đăng nhập, dùng lại salt đã lưu trong database.
        salt = bytes.fromhex(salt_hex)

    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )

    return salt.hex(), password_hash.hex()


def verify_password(password, saved_salt, saved_hash):
    """
    Kiểm tra mật khẩu người dùng nhập có đúng không.
    """

    _, entered_hash = hash_password(password, saved_salt)

    return hmac.compare_digest(entered_hash, saved_hash)

def authenticate_user():
    """
    Đăng nhập tài khoản bất kỳ.

    Hàm trả về thông tin người dùng nếu username và mật khẩu đúng.
    Nếu sai, hàm trả về None.
    """

    print("\n=== ĐĂNG NHẬP HỆ THỐNG ===")

    username = input("Nhập username: ").strip().lower()

    if not username:
        print("[LỖI] Username không được để trống.")
        return None

    # Dùng input() để dễ học trong PyCharm.
    # Khi làm hệ thống thật, nên dùng giao diện web có ô password được che.
    password = input("Nhập mật khẩu: ")

    with get_connection() as conn:
        user = conn.execute(
            """
            SELECT
                id,
                username,
                full_name,
                role,
                password_salt,
                password_hash
            FROM users
            WHERE lower(username) = lower(?)
            """,
            (username,),
        ).fetchone()

    if not user:
        print("[TỪ CHỐI] Không tìm thấy tài khoản này.")
        return None

    is_correct_password = verify_password(
        password,
        user["password_salt"],
        user["password_hash"],
    )

    if not is_correct_password:
        print("[TỪ CHỐI] Mật khẩu không đúng.")
        return None

    print(f"\n[THÀNH CÔNG] Xin chào: {user['full_name']}")

    if user["role"] == ROLE_MANAGER:
        print("Quyền truy cập: QUẢN LÝ")
    else:
        print("Quyền truy cập: NHÂN VIÊN")

    return user

def create_user(username, full_name, role, password):
    """
    Tạo một tài khoản mới trong bảng users.

    role chỉ có thể là:
    - EMPLOYEE
    - MANAGER
    """

    username = username.strip().lower()
    full_name = full_name.strip()

    if not username:
        return False, "Username không được để trống."

    if not full_name:
        return False, "Họ tên không được để trống."

    if role not in (ROLE_EMPLOYEE, ROLE_MANAGER):
        return False, "Role không hợp lệ."

    if len(password) < 8:
        return False, "Mật khẩu cần có ít nhất 8 ký tự."

    salt, password_hash = hash_password(password)

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
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    username,
                    full_name,
                    role,
                    salt,
                    password_hash,
                    created_at,
                ),
            )

        return True, "Tạo tài khoản thành công."

    except sqlite3.IntegrityError:
        return False, "Username này đã tồn tại."

def register_employee():
    """
    Cho phép tạo tài khoản nhân viên.

    Tài khoản tạo từ menu này chỉ là EMPLOYEE.
    Không cho người dùng tự tạo quyền MANAGER.
    """

    print("\n=== ĐĂNG KÝ TÀI KHOẢN NHÂN VIÊN ===")

    username = input("Nhập username: ").strip()
    full_name = input("Nhập họ và tên: ").strip()

    password = input("Nhập mật khẩu: ")
    confirm_password = input("Nhập lại mật khẩu: ")

    if password != confirm_password:
        print("[LỖI] Hai mật khẩu không khớp.")
        return

    success, message = create_user(
        username=username,
        full_name=full_name,
        role=ROLE_EMPLOYEE,
        password=password,
    )

    if success:
        print(f"[THÀNH CÔNG] {message}")
    else:
        print(f"[LỖI] {message}")

def create_first_manager():
    """
    Tạo tài khoản quản lý đầu tiên.

    Hàm chỉ chạy khi database chưa có quản lý nào.
    """

    with get_connection() as conn:
        manager = conn.execute(
            """
            SELECT id
            FROM users
            WHERE role = 'MANAGER'
            LIMIT 1
            """
        ).fetchone()

    if manager:
        return

    print("\n=== KHỞI TẠO QUẢN LÝ ĐẦU TIÊN ===")
    print("Hệ thống chưa có tài khoản quản lý.")

    username = input("Tạo username quản lý: ").strip()
    full_name = input("Nhập họ tên quản lý: ").strip()

    password = input("Tạo mật khẩu quản lý: ")
    confirm_password = input("Nhập lại mật khẩu: ")

    if password != confirm_password:
        print("[LỖI] Hai mật khẩu không khớp.")
        return

    success, message = create_user(
        username=username,
        full_name=full_name,
        role=ROLE_MANAGER,
        password=password,
    )

    print(message)

# ============================================================
# 4. NHÂN VIÊN GỬI HỒ SƠ CHỜ DUYỆT
# ============================================================
def submit_for_approval(current_user) -> None:
    """
    Nhân viên gửi file:
    - File được copy vào pending/.
    - Database lưu trạng thái PENDING.
    - File chưa được đưa vào kho chính thức storage/.
    """

    print("\n=== GỬI HỒ SƠ CHỜ QUẢN LÝ DUYỆT ===")

    # Chỉ nhân viên mới được gửi hồ sơ.
    if current_user["role"] != ROLE_EMPLOYEE:
        print("[TỪ CHỐI] Chỉ nhân viên mới được gửi hồ sơ.")
        return

    # Không cho người dùng tự nhập tên.
    # Hệ thống tự lấy tên từ tài khoản đã đăng nhập.
    submitter = current_user["full_name"]

    source_text = input("Nhập đường dẫn file cần gửi: ").strip()

    if not source_text:
        print("Bạn chưa nhập đường dẫn file.")
        return

    source = clean_path_input(source_text)

    category = choose_category()

    is_valid, message = validate_submission(source, category)

    if not is_valid:
        print(f"\n[THÔNG BÁO LỖI] {message}")
        return

    # Tạo tên riêng để tránh trùng file trong thư mục pending.
    stored_name = f"{uuid.uuid4().hex}{source.suffix.lower()}"

    # File mới gửi nằm ở pending, chưa vào storage.
    pending_path = PENDING_DIR / stored_name

    submitted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        # Copy file vào khu vực chờ duyệt.
        shutil.copy2(source, pending_path)

        # Lưu thông tin vào database với trạng thái PENDING.
        with get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO documents (
    original_name,
    stored_name,
    category,
    file_path,
    file_size,
    status,
    submitted_at,
    submitted_by,
    submitted_by_user_id
)
VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
                """,
                (
                    source.name,
                    stored_name,
                    category,
                    str(pending_path),
                    source.stat().st_size,
                    submitted_at,
                    submitter,
                    current_user["id"],
                ),
            )

            document_id = cursor.lastrowid

        print("\n[ĐÃ GỬI] Hồ sơ đã được gửi cho quản lý duyệt.")
        print(f"Mã hồ sơ: {document_id}")
        print("Trạng thái: Chờ quản lý duyệt")
        print(
            "Lưu ý: Hồ sơ chưa thể tìm kiếm hoặc download "
            "cho đến khi được duyệt."
        )

    except Exception as error:
        # Nếu lưu database lỗi sau khi copy file, xóa file trong pending.
        if pending_path.exists():
            pending_path.unlink()

        print(f"\n[THÔNG BÁO LỖI] Không thể gửi hồ sơ: {error}")


# ============================================================
# 5. QUẢN LÝ DUYỆT / TỪ CHỐI HỒ SƠ
# ============================================================
def list_pending_documents() -> list[sqlite3.Row]:
    """Hiển thị tất cả hồ sơ đang chờ duyệt."""

    with get_connection() as conn:
        records = conn.execute(
            """
            SELECT *
            FROM documents
            WHERE status = 'PENDING'
            ORDER BY id ASC
            """
        ).fetchall()

    print("\n=== DANH SÁCH HỒ SƠ CHỜ DUYỆT ===")

    display_records(records, include_reviewer=False)

    return records


def approve_document(
    document: sqlite3.Row,
    manager_name: str
) -> None:
    """
    Duyệt hồ sơ:
    - Chuyển file từ pending/ vào storage/file_loai_x/.
    - Đổi trạng thái trong database thành APPROVED.
    """

    source = Path(document["file_path"])

    if not source.exists():
        print(
            "[LỖI] Không tìm thấy file trong thư mục pending/. "
            "Không thể duyệt."
        )
        return

    destination_folder = category_folder_from_label(document["category"])

    destination = destination_folder / document["stored_name"]

    reviewed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        # Di chuyển file thật từ pending sang storage.
        shutil.move(str(source), str(destination))

        # Cập nhật thông tin trong database.
        with get_connection() as conn:
            conn.execute(
                """
                UPDATE documents
                SET status = 'APPROVED',
                    file_path = ?,
                    reviewed_at = ?,
                    reviewed_by = ?,
                    rejection_reason = NULL
                WHERE id = ?
                """,
                (
                    str(destination),
                    reviewed_at,
                    manager_name,
                    document["id"],
                ),
            )

        print(
            f"[ĐÃ DUYỆT] Hồ sơ #{document['id']} "
            "đã vào kho chính thức."
        )

        print(f"Vị trí lưu: {destination}")

    except Exception as error:
        # Nếu di chuyển file thành công nhưng database lỗi,
        # thử chuyển file ngược lại pending.
        if destination.exists() and not source.exists():
            shutil.move(str(destination), str(source))

        print(f"[LỖI] Không thể duyệt hồ sơ: {error}")


def reject_document(
    document: sqlite3.Row,
    manager_name: str
) -> None:
    """
    Từ chối hồ sơ:
    - Chuyển file từ pending/ sang rejected/.
    - Đổi trạng thái thành REJECTED.
    - Lưu lý do từ chối.
    """

    source = Path(document["file_path"])

    if not source.exists():
        print(
            "[LỖI] Không tìm thấy file trong thư mục pending/. "
            "Không thể từ chối."
        )
        return

    reason = input("Nhập lý do từ chối: ").strip()

    if not reason:
        reason = "Quản lý từ chối nhưng chưa ghi lý do."

    destination = REJECTED_DIR / document["stored_name"]

    reviewed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        # Chuyển file vào rejected.
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
                    manager_name,
                    reason,
                    document["id"],
                ),
            )

        print(
            f"[ĐÃ TỪ CHỐI] Hồ sơ #{document['id']} "
            "đã được chuyển sang rejected/."
        )

    except Exception as error:
        # Nếu database lỗi, thử chuyển file ngược về pending.
        if destination.exists() and not source.exists():
            shutil.move(str(destination), str(source))

        print(f"[LỖI] Không thể từ chối hồ sơ: {error}")


def manager_review_flow(current_user):
    """
    Quản lý xem danh sách PENDING và chọn duyệt hoặc từ chối.

    Không yêu cầu nhập mã quản lý lần nữa vì người này
    đã đăng nhập từ đầu chương trình.
    """

    # Chặn nhân viên gọi nhầm chức năng này.
    if current_user["role"] != ROLE_MANAGER:
        print("[TỪ CHỐI] Chỉ quản lý mới được duyệt hồ sơ.")
        return

    manager_name = current_user["full_name"]

    pending_records = list_pending_documents()

    if not pending_records:
        return

    document_id_text = input(
        "Nhập ID hồ sơ cần xử lý "
        "(hoặc Enter để quay lại): "
    ).strip()

    if not document_id_text:
        return

    if not document_id_text.isdigit():
        print("ID phải là số.")
        return

    document = get_document(int(document_id_text))

    if not document or document["status"] != "PENDING":
        print("Không tìm thấy hồ sơ chờ duyệt với ID này.")
        return

    print(f"\nHồ sơ #{document['id']}: {document['original_name']}")
    print(f"Người gửi: {document['submitted_by']}")
    print(f"Loại: {document['category']}")
    print(f"Dung lượng: {format_size(document['file_size'])}")

    print("1. Duyệt hồ sơ")
    print("2. Từ chối hồ sơ")
    print("3. Hủy")

    action = input("Chọn thao tác: ").strip()

    if action == "1":
        approve_document(document, manager_name)

    elif action == "2":
        reject_document(document, manager_name)

    elif action == "3":
        print("Đã hủy xử lý.")

    else:
        print("Lựa chọn không hợp lệ.")


# ============================================================
# 6. TÌM KIẾM VÀ DOWNLOAD HỒ SƠ ĐÃ DUYỆT
# ============================================================
def search_approved_documents(
    keyword: str = "",
    category: str | None = None
) -> list[sqlite3.Row]:
    """Chỉ tìm được các hồ sơ APPROVED trong kho chính thức."""

    query = """
        SELECT *
        FROM documents
        WHERE status = 'APPROVED'
    """

    params: list[str] = []

    if keyword:
        query += " AND lower(original_name) LIKE lower(?)"
        params.append(f"%{keyword}%")

    if category:
        query += " AND category = ?"
        params.append(category)

    query += " ORDER BY id DESC"

    with get_connection() as conn:
        return conn.execute(query, params).fetchall()


def search_approved_flow() -> list[sqlite3.Row]:
    """Giao diện tìm kiếm hồ sơ đã được duyệt."""

    print("\n=== TÌM KIẾM HỒ SƠ ĐÃ ĐƯỢC DUYỆT ===")

    keyword = input(
        "Nhập tên file hoặc mã hồ sơ "
        "(có thể để trống): "
    ).strip()

    category = choose_category(allow_all=True)

    # Nếu nhập toàn số thì hiểu là tìm theo ID.
    if keyword.isdigit():
        with get_connection() as conn:
            records = conn.execute(
                """
                SELECT *
                FROM documents
                WHERE id = ?
                  AND status = 'APPROVED'
                """,
                (int(keyword),),
            ).fetchall()

    else:
        records = search_approved_documents(keyword, category)

    if not records:
        print("\n[THÔNG BÁO] Không tìm thấy hồ sơ đã được duyệt phù hợp.")
        return []

    display_records(records)

    return records


def list_approved_documents() -> None:
    """Xem toàn bộ hồ sơ đã được duyệt."""

    print("\n=== DANH SÁCH HỒ SƠ ĐÃ ĐƯỢC DUYỆT ===")

    display_records(search_approved_documents())


def download_approved_document() -> None:
    """
    Download chỉ được phép với hồ sơ đã APPROVED.
    File sẽ được copy từ storage sang downloads.
    """

    print("\n=== DOWNLOAD HỒ SƠ ĐÃ ĐƯỢC DUYỆT ===")

    records = search_approved_flow()

    if not records:
        return

    document_id_text = input(
        "Nhập ID hồ sơ cần tải "
        "(hoặc Enter để hủy): "
    ).strip()

    if not document_id_text:
        print("Đã hủy download.")
        return

    if not document_id_text.isdigit():
        print("ID phải là số.")
        return

    with get_connection() as conn:
        document = conn.execute(
            """
            SELECT *
            FROM documents
            WHERE id = ?
              AND status = 'APPROVED'
            """,
            (int(document_id_text),),
        ).fetchone()

    if not document:
        print("Không tìm thấy hồ sơ đã được duyệt với ID này.")
        return

    source = Path(document["file_path"])

    if not source.exists():
        print(
            "[LỖI] Có dữ liệu trong database "
            "nhưng file thật không còn trong kho."
        )
        return

    destination = get_unique_path(
        DOWNLOADS_DIR,
        document["original_name"]
    )

    try:
        shutil.copy2(source, destination)

        print("[THÀNH CÔNG] Download file thành công.")
        print(f"File đã được copy vào: {destination}")

    except Exception as error:
        print(f"[LỖI] Không thể download file: {error}")


# ============================================================
# 7. THEO DÕI TRẠNG THÁI HỒ SƠ CỦA NGƯỜI GỬI
# ============================================================
def view_submitter_status() -> None:
    """
    Cho nhân viên xem hồ sơ của chính mình:
    PENDING / APPROVED / REJECTED.
    """

    print("\n=== TRA CỨU TRẠNG THÁI HỒ SƠ ĐÃ GỬI ===")

    submitter = input("Nhập tên người gửi: ").strip()

    if not submitter:
        print("Tên người gửi không được để trống.")
        return

    with get_connection() as conn:
        records = conn.execute(
            """
            SELECT *
            FROM documents
            WHERE lower(submitted_by) = lower(?)
            ORDER BY id DESC
            """,
            (submitter,),
        ).fetchall()

    display_records(records)

def view_my_submission_status(current_user):
    """
    Nhân viên chỉ được xem hồ sơ mang tên của tài khoản
    hiện đang đăng nhập.
    """

    print("\n=== HỒ SƠ CỦA TÔI ===")

    with get_connection() as conn:
        records = conn.execute(
            """
            SELECT *
            FROM documents
            WHERE submitted_by_user_id = ?
            ORDER BY id DESC
            """,
            (current_user["id"],),
        ).fetchall()

    display_records(records)


def list_all_approved_documents():
    """
    Lấy tất cả hồ sơ đã được quản lý duyệt.

    Không giới hạn theo người gửi.
    Nhân viên đã đăng nhập có thể xem các hồ sơ APPROVED
    trên toàn hệ thống.
    """

    with get_connection() as conn:
        records = conn.execute(
            """
            SELECT *
            FROM documents
            WHERE status = 'APPROVED'
            ORDER BY id DESC
            """
        ).fetchall()

    return records


def download_approved_document_from_system(current_user):
    """
    Nhân viên có thể download mọi hồ sơ đã được duyệt
    trên hệ thống.

    Không thể tải hồ sơ PENDING hoặc REJECTED.
    """

    print("\n=== DOWNLOAD HỒ SƠ ĐÃ ĐƯỢC DUYỆT TRÊN HỆ THỐNG ===")

    records = list_all_approved_documents()

    if not records:
        print("\n[THÔNG BÁO] Hiện chưa có hồ sơ nào được duyệt để download.")
        return

    print("\nDanh sách toàn bộ hồ sơ đã được duyệt:")
    display_records(records)

    document_id_text = input(
        "Nhập ID hồ sơ bạn muốn tải "
        "(hoặc Enter để quay lại): "
    ).strip()

    if not document_id_text:
        print("Đã hủy download.")
        return

    if not document_id_text.isdigit():
        print("[LỖI] ID hồ sơ phải là số.")
        return

    # Chỉ cần kiểm tra hồ sơ đã được duyệt.
    # Không kiểm tra người gửi, vì nhân viên được phép tải
    # mọi hồ sơ APPROVED trên hệ thống.
    with get_connection() as conn:
        document = conn.execute(
            """
            SELECT *
            FROM documents
            WHERE id = ?
              AND status = 'APPROVED'
            """,
            (int(document_id_text),),
        ).fetchone()

    if not document:
        print(
            "[TỪ CHỐI] Không tìm thấy hồ sơ đã được duyệt "
            "với mã này."
        )
        return

    source = Path(document["file_path"])

    if not source.exists():
        print(
            "[LỖI] Database có thông tin hồ sơ "
            "nhưng file thật không còn trong kho lưu trữ."
        )
        return

    destination = get_unique_path(
        DOWNLOADS_DIR,
        document["original_name"],
    )

    try:
        shutil.copy2(source, destination)

        print("\n[THÀNH CÔNG] Download hồ sơ thành công.")
        print(f"Tài liệu: {document['original_name']}")
        print(f"Người gửi: {document['submitted_by']}")
        print(f"File đã được copy vào: {destination}")

    except Exception as error:
        print(f"\n[LỖI] Không thể download hồ sơ: {error}")
# ============================================================
# 8. MENU CHÍNH
# ============================================================
def employee_menu(current_user):
    """
    Menu dành riêng cho nhân viên đã đăng nhập.
    """

    while True:
        print("\n" + "=" * 70)
        print(f"NHÂN VIÊN: {current_user['full_name']}")
        print("=" * 70)

        print("1. Gửi hồ sơ chờ duyệt")
        print("2. Xem trạng thái hồ sơ của tôi")
        print("3. Download hồ sơ đã được duyệt trên hệ thống")
        print("4. Đăng xuất")

        choice = input("\nChọn chức năng (1-4): ").strip()

        if choice == "1":
            submit_for_approval(current_user)

        elif choice == "2":
            view_my_submission_status(current_user)

        elif choice == "3":
            download_approved_document_from_system(current_user)

        elif choice == "4":
            print("Đã đăng xuất tài khoản nhân viên.")
            break

        else:
            print("Lựa chọn không hợp lệ. Vui lòng nhập số từ 1 đến 4.")

def manager_menu(current_user):
    """
    Menu dành riêng cho quản lý đã đăng nhập.
    """

    while True:
        print("\n" + "=" * 70)
        print(f"QUẢN LÝ: {current_user['full_name']}")
        print("=" * 70)

        print("1. Duyệt / từ chối hồ sơ")
        print("2. Xem danh sách hồ sơ đã được duyệt")
        print("3. Tìm kiếm hồ sơ đã được duyệt trên hệ thống")
        print("4. Download hồ sơ đã được duyệt trên hệ thống")
        print("5. Đăng xuất")

        choice = input("\nChọn chức năng (1-5): ").strip()

        if choice == "1":
            manager_review_flow(current_user)

        elif choice == "2":
            list_approved_documents()

        elif choice == "3":
            search_approved_flow()

        elif choice == "4":
            download_approved_document()

        elif choice == "5":
            print("Đã đăng xuất tài khoản quản lý.")
            break

        else:
            print("Lựa chọn không hợp lệ.")

def main():
    """
    Menu đầu tiên của hệ thống.

    Người dùng phải đăng nhập trước.
    Sau khi đăng nhập thành công, hệ thống chuyển tới
    menu nhân viên hoặc menu quản lý tùy theo role.
    """

    while True:
        print("\n" + "=" * 78)
        print("HỆ THỐNG LƯU TRỮ HỒ SƠ CÓ QUY TRÌNH PHÊ DUYỆT")
        print("=" * 78)

        print("1. Đăng nhập")
        print("2. Đăng ký tài khoản nhân viên")
        print("3. Thoát")

        choice = input("\nChọn chức năng (1-3): ").strip()

        if choice == "1":
            current_user = authenticate_user()

            # Sai username hoặc mật khẩu thì quay lại menu đầu.
            if not current_user:
                continue

            # Dựa vào role để đưa tới menu riêng.
            if current_user["role"] == ROLE_MANAGER:
                manager_menu(current_user)

            elif current_user["role"] == ROLE_EMPLOYEE:
                employee_menu(current_user)

            else:
                print("[LỖI] Tài khoản có quyền không hợp lệ.")

        elif choice == "2":
            register_employee()

        elif choice == "3":
            print("\nCảm ơn bạn đã sử dụng hệ thống. Tạm biệt!")
            break

        else:
            print("Lựa chọn không hợp lệ. Vui lòng nhập số từ 1 đến 3.")


if __name__ == "__main__":
    initialize_system()
    create_first_manager()
    main()

    #user_test = authenticate_user()
    #print(user_test)