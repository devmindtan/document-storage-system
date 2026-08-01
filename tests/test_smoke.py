"""
Characterization smoke test — chốt lại HÀNH VI HIỆN TẠI của app.py làm mốc
trước khi tách kiến trúc, KHÔNG phải test đúng/sai nghiệp vụ.

Phạm vi có chủ đích:
- Mọi route GET không cần {id} trong path: kiểm tra status code theo từng
  vai trò (anonymous/employee/manager/admin).
- Mọi route có {id} trong path: chỉ kiểm tra auth guard (chưa đăng nhập /
  sai vai trò) bằng id giả (999999), KHÔNG seed dữ liệu thật, vì mọi route
  đều check quyền trước khi query DB (đã xác nhận qua code).
- Không test luồng upload file thật (tránh ghi vào thư mục storage/pending
  thật của dự án).

Chạy: .venv/bin/pytest tests/test_smoke.py -q
"""

import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import main as app_module  # noqa: E402
import database.connection as connection_module  # noqa: E402
from core.security import hash_password  # noqa: E402


def _seed_user(db_path, username, full_name, role, is_admin, is_active=1, approval_status="APPROVED"):
    import sqlite3

    salt, pw_hash = hash_password("Test@12345")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                full_name TEXT NOT NULL,
                role TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                approval_status TEXT NOT NULL DEFAULT 'APPROVED',
                is_admin INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            INSERT INTO users (
                username, full_name, role, password_salt, password_hash,
                created_at, is_active, approval_status, is_admin
            )
            VALUES (?, ?, ?, ?, ?, datetime('now'), ?, ?, ?)
            """,
            (username, full_name, role, salt, pw_hash, is_active, approval_status, is_admin),
        )


@pytest.fixture(scope="module")
def clients(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("db") / "test.db"
    # get_connection() đọc DB_PATH từ namespace của module nơi nó được định
    # nghĩa (database.connection), không phải từ app_module — phải patch
    # đúng chỗ đó thì override mới có tác dụng.
    connection_module.DB_PATH = db_path

    _seed_user(db_path, "employee1", "Nhân viên Test", "EMPLOYEE", is_admin=0)
    _seed_user(db_path, "manager1", "Quản lý Test", "MANAGER", is_admin=0)
    _seed_user(db_path, "admin1", "Admin Test", "MANAGER", is_admin=1)

    with TestClient(app_module.app) as base_client:
        anon = base_client

        employee = TestClient(app_module.app)
        r = employee.post("/login", data={"username": "employee1", "password": "Test@12345"}, follow_redirects=False)
        assert r.status_code == 303, r.text

        manager = TestClient(app_module.app)
        r = manager.post("/login", data={"username": "manager1", "password": "Test@12345"}, follow_redirects=False)
        assert r.status_code == 303, r.text

        admin = TestClient(app_module.app)
        r = admin.post("/login", data={"username": "admin1", "password": "Test@12345"}, follow_redirects=False)
        assert r.status_code == 303, r.text

        yield {"anon": anon, "employee": employee, "manager": manager, "admin": admin}


def test_login_page_anonymous(clients):
    r = clients["anon"].get("/", follow_redirects=False)
    assert r.status_code == 200


def test_login_page_redirects_logged_in_user(clients):
    r = clients["employee"].get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/employee"

    r = clients["manager"].get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/manager"


def test_register_page(clients):
    assert clients["anon"].get("/register").status_code == 200


GET_ROUTES_NO_PARAMS = [
    "/employee",
    "/employee/my-documents",
    "/employee/upload",
    "/statistics",
    "/manager",
    "/manager/my-documents",
    "/manager/upload",
    "/manager/pending",
    "/manager/project-requests",
    "/manager/users",
    "/documents/approved",
    "/manager/audit-logs",
    "/admin",
    "/admin/create-manager",
]


@pytest.mark.parametrize("path", GET_ROUTES_NO_PARAMS)
def test_get_route_anonymous_redirects_to_login(clients, path):
    r = clients["anon"].get(path, follow_redirects=False)
    assert r.status_code == 303, f"{path} -> {r.status_code}"
    assert r.headers["location"] == "/"


EMPLOYEE_ONLY_GET = [
    "/employee",
    "/employee/my-documents",
    "/employee/upload",
]

MANAGER_ONLY_GET = [
    "/manager",
    "/manager/my-documents",
    "/manager/upload",
    "/manager/pending",
    "/manager/project-requests",
]

# Dù path có tiền tố /manager/, 2 route này thực ra chỉ cho phép is_admin_user()
# (app.py:4374, app.py:4440) — không phải mọi manager thường đều vào được.
ADMIN_ONLY_GET = [
    "/admin",
    "/admin/create-manager",
    "/manager/users",
    "/manager/audit-logs",
]

SHARED_GET = [
    "/statistics",
    "/documents/approved",
    "/documents/approved/live-search",
]


@pytest.mark.parametrize("path", EMPLOYEE_ONLY_GET)
def test_employee_routes_ok_for_employee(clients, path):
    r = clients["employee"].get(path, follow_redirects=False)
    assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:300]}"


@pytest.mark.parametrize("path", EMPLOYEE_ONLY_GET)
def test_employee_routes_blocked_for_manager(clients, path):
    r = clients["manager"].get(path, follow_redirects=False)
    assert r.status_code == 303, f"{path} -> {r.status_code}"


@pytest.mark.parametrize("path", MANAGER_ONLY_GET)
def test_manager_routes_ok_for_manager(clients, path):
    r = clients["manager"].get(path, follow_redirects=False)
    assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:300]}"


@pytest.mark.parametrize("path", MANAGER_ONLY_GET)
def test_manager_routes_blocked_for_employee(clients, path):
    r = clients["employee"].get(path, follow_redirects=False)
    assert r.status_code == 303, f"{path} -> {r.status_code}"


@pytest.mark.parametrize("path", ADMIN_ONLY_GET)
def test_admin_routes_ok_for_admin(clients, path):
    r = clients["admin"].get(path, follow_redirects=False)
    assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text[:300]}"


@pytest.mark.parametrize("path", ADMIN_ONLY_GET)
def test_admin_routes_blocked_for_plain_manager(clients, path):
    r = clients["manager"].get(path, follow_redirects=False)
    assert r.status_code == 303, f"{path} -> {r.status_code}"


@pytest.mark.parametrize("path", SHARED_GET)
def test_shared_routes_ok_for_employee_and_manager(clients, path):
    assert clients["employee"].get(path, follow_redirects=False).status_code == 200, path
    assert clients["manager"].get(path, follow_redirects=False).status_code == 200, path


DUMMY_ID = 999999

MUTATING_ROUTES_MANAGER_ONLY = [
    ("post", "/manager/projects/{id}/approve"),
    ("post", "/manager/projects/{id}/reject"),
    ("post", "/manager/users/{id}/toggle-status"),
    ("post", "/manager/users/{id}/approve"),
    ("post", "/manager/users/{id}/reject"),
    ("post", "/manager/documents/{id}/approve"),
    ("post", "/manager/documents/{id}/reject"),
    ("post", "/manager/project-categories/{id}/delete"),
    ("post", "/projects/{id}/delete"),
]

MUTATING_ROUTES_ADMIN_ONLY = [
    ("post", "/manager/users/{id}/promote-manager"),
    ("post", "/manager/users/{id}/demote-employee"),
]

DOWNLOAD_DELETE_SHARED = [
    ("get", "/documents/{id}/download"),
    ("post", "/documents/{id}/delete"),
]


def test_live_search_anonymous_returns_401_json(clients):
    r = clients["anon"].get("/documents/approved/live-search", follow_redirects=False)
    assert r.status_code == 401
    assert r.json()["success"] is False


def test_project_categories_by_project_anonymous_returns_401_json(clients):
    r = clients["anon"].get(f"/project-categories/{DUMMY_ID}", follow_redirects=False)
    assert r.status_code == 401
    assert r.json()["success"] is False


@pytest.mark.parametrize("method,path_tpl", MUTATING_ROUTES_MANAGER_ONLY)
def test_manager_mutating_routes_blocked_for_anonymous(clients, method, path_tpl):
    path = path_tpl.format(id=DUMMY_ID)
    r = getattr(clients["anon"], method)(path, follow_redirects=False)
    assert r.status_code == 303, f"{path} -> {r.status_code}"
    assert r.headers["location"] == "/"


@pytest.mark.parametrize("method,path_tpl", MUTATING_ROUTES_MANAGER_ONLY)
def test_manager_mutating_routes_blocked_for_employee(clients, method, path_tpl):
    path = path_tpl.format(id=DUMMY_ID)
    r = getattr(clients["employee"], method)(path, follow_redirects=False)
    assert r.status_code == 303, f"{path} -> {r.status_code}"


@pytest.mark.parametrize("method,path_tpl", MUTATING_ROUTES_ADMIN_ONLY)
def test_admin_mutating_routes_blocked_for_plain_manager(clients, method, path_tpl):
    path = path_tpl.format(id=DUMMY_ID)
    r = getattr(clients["manager"], method)(path, follow_redirects=False)
    assert r.status_code == 303, f"{path} -> {r.status_code}"


@pytest.mark.parametrize("method,path_tpl", DOWNLOAD_DELETE_SHARED)
def test_shared_id_routes_blocked_for_anonymous(clients, method, path_tpl):
    path = path_tpl.format(id=DUMMY_ID)
    r = getattr(clients["anon"], method)(path, follow_redirects=False)
    assert r.status_code == 303, f"{path} -> {r.status_code}"


def test_logout(clients):
    fresh = TestClient(app_module.app)
    r = fresh.post("/login", data={"username": "employee1", "password": "Test@12345"}, follow_redirects=False)
    assert r.status_code == 303
    r = fresh.get("/logout", follow_redirects=False)
    assert r.status_code == 303
    r = fresh.get("/employee", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_employee_upload_real_file(clients):
    """
    Bug thật từng lọt lưới: 71 smoke test ở trên chỉ kiểm tra auth guard, không
    ai thực sự POST 1 file qua /employee/upload -- nên thiếu `from typing
    import List` (api/employee.py) và MAX_FILE_SIZE_MB (services/documents.py)
    không bị phát hiện cho tới khi người dùng thật bấm nút upload. Test này
    đi hết đường: chọn project/category có sẵn, gửi 1 file thật, xác nhận
    trả về thành công và tạo đúng 1 dòng trong bảng documents.

    Ghi chú: mỗi router tự giữ bản copy PENDING_DIR/STORAGE_DIR riêng (import
    trực tiếp từ core.config vào namespace của từng file api/*.py), nên
    monkeypatch 1 module không chặn được nơi file thật sẽ ghi xuống -- test
    này chấp nhận ghi 1 file thật rồi tự xóa lại trong finally.
    """
    import io
    from pathlib import Path

    from services.projects import get_active_categories_for_project_key

    cat_key = next(iter(get_active_categories_for_project_key("1").keys()))

    files = [("uploaded_files", ("smoke_test_upload.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf"))]
    data = {
        "project_key": "1",
        "category_key": cat_key,
        "new_project_name": "",
        "new_category_name": "",
        "new_category_code": "",
        "description": "Smoke test upload.",
        "location": "Kho test",
    }

    written_path = None

    try:
        r = clients["employee"].post("/employee/upload", data=data, files=files, follow_redirects=False)
        assert r.status_code == 200, r.text[:500]
        assert "success-message" in r.text, r.text[:500]

        with sqlite3.connect(connection_module.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM documents WHERE original_name = 'smoke_test_upload.pdf'"
            ).fetchone()

        assert row is not None
        assert row["status"] == "PENDING"
        written_path = row["file_path"]
    finally:
        if written_path:
            Path(written_path).unlink(missing_ok=True)
