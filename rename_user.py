from pathlib import Path
import sqlite3

# Lấy đúng thư mục đang chứa file này
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"

# Username tài khoản cần sửa
username_can_sua = "athang"

# Tên mới muốn hiển thị trên website
ten_moi = "Thắng Đặng"

with sqlite3.connect(DB_PATH) as conn:
    cursor = conn.execute(
        """
        UPDATE users
        SET full_name = ?
        WHERE username = ?
        """,
        (ten_moi, username_can_sua),
    )

    if cursor.rowcount == 0:
        print("Không tìm thấy username này.")
    else:
        print("Đã đổi tên hiển thị thành công.")