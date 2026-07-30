import sqlite3
import shutil
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"

PENDING_DIR = BASE_DIR / "pending"
STORAGE_DIR = BASE_DIR / "storage"
REJECTED_DIR = BASE_DIR / "rejected"
DOWNLOADS_DIR = BASE_DIR / "downloads"


PROJECT_MAP = {
    "1": {"label": "Project 1", "folder": "project_1"},
    "2": {"label": "Project 2", "folder": "project_2"},
    "3": {"label": "Project 3", "folder": "project_3"},
}

CATEGORY_MAP = {
    "1": {"label": "File loại 1", "folder": "file_loai_1"},
    "2": {"label": "File loại 2", "folder": "file_loai_2"},
    "3": {"label": "File loại 3", "folder": "file_loai_3"},
}


def confirm_reset():
    print("CẢNH BÁO:")
    print("File này sẽ xóa toàn bộ dữ liệu đã upload.")
    print("Bao gồm:")
    print("- toàn bộ hồ sơ")
    print("- toàn bộ file trong storage / pending / rejected / downloads")
    print("- toàn bộ tài khoản nhân viên")
    print("- toàn bộ tài khoản quản lý")
    print("- toàn bộ tài khoản admin")
    print("- toàn bộ nhật ký hoạt động")
    print()
    print("Sau khi chạy xong, bạn cần tạo lại tài khoản admin.")
    print()

    answer = input("Gõ YES để xác nhận xóa toàn bộ dữ liệu: ")

    if answer != "YES":
        print("Đã hủy. Không xóa dữ liệu.")
        raise SystemExit


def table_exists(conn, table_name):
    result = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        AND name = ?
        """,
        (table_name,),
    ).fetchone()

    return result is not None


def clear_database():
    if not DB_PATH.exists():
        print("Không tìm thấy data.db, bỏ qua phần xóa database.")
        return

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")

        tables_to_clear = [
            "documents",
            "audit_logs",
            "user_category_permissions",
            "users",
        ]

        for table_name in tables_to_clear:
            if table_exists(conn, table_name):
                conn.execute(f"DELETE FROM {table_name}")
                print(f"Đã xóa dữ liệu bảng: {table_name}")

        if table_exists(conn, "sqlite_sequence"):
            for table_name in tables_to_clear:
                conn.execute(
                    "DELETE FROM sqlite_sequence WHERE name = ?",
                    (table_name,),
                )

        conn.commit()

    print("Đã xóa dữ liệu trong database.")


def clear_folder(folder_path):
    folder_path.mkdir(parents=True, exist_ok=True)

    for item in folder_path.iterdir():
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    print(f"Đã xóa nội dung thư mục: {folder_path.name}")


def recreate_folders():
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    REJECTED_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

    for project in PROJECT_MAP.values():
        project_folder = STORAGE_DIR / project["folder"]
        project_folder.mkdir(parents=True, exist_ok=True)

        for category in CATEGORY_MAP.values():
            category_folder = project_folder / category["folder"]
            category_folder.mkdir(parents=True, exist_ok=True)

    print("Đã tạo lại cấu trúc thư mục storage.")


def clear_files():
    clear_folder(PENDING_DIR)
    clear_folder(STORAGE_DIR)
    clear_folder(REJECTED_DIR)
    clear_folder(DOWNLOADS_DIR)

    recreate_folders()


def main():
    confirm_reset()
    clear_database()
    clear_files()

    print()
    print("HOÀN TẤT RESET.")
    print("Toàn bộ hồ sơ, file upload, tài khoản và nhật ký đã bị xóa.")
    print()
    print("Bước tiếp theo:")
    print("1. Chạy lại file create_admin_account.py để tạo admin mới.")
    print("2. Sau đó chạy lại server uvicorn app:app --reload.")


if __name__ == "__main__":
    main()