import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data.db"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

LOCAL_STORAGE_DIR = BASE_DIR

PENDING_DIR = LOCAL_STORAGE_DIR / "pending"
STORAGE_DIR = LOCAL_STORAGE_DIR / "storage"
REJECTED_DIR = LOCAL_STORAGE_DIR / "rejected"
DOWNLOADS_DIR = LOCAL_STORAGE_DIR / "downloads"

MAX_FILE_SIZE_MB = 100

ALLOWED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".png", ".jpg", ".jpeg",
    ".txt", ".zip",
}

CATEGORY_MAP = {
    "BC": {
        "label": "Business Case",
        "folder": "business_case",
        "code": "BC",
    },
    "CAPEX": {
        "label": "CAPEX",
        "folder": "capex",
        "code": "CAPEX",
    },
    "PTW": {
        "label": "PTW",
        "folder": "ptw",
        "code": "PTW",
    },
    "JSA": {
        "label": "JSA",
        "folder": "jsa",
        "code": "JSA",
    },
    "RAHS": {
        "label": "Risk Assessment / Hazard Study",
        "folder": "risk_assessment_hazard_study",
        "code": "RAHS",
    },
    "DWG": {
        "label": "Drawings",
        "folder": "drawings",
        "code": "DWG",
    },
    "FSH": {
        "label": "FAT / SAT / Handover",
        "folder": "fat_sat_handover",
        "code": "FSH",
    },
    "PHOTO": {
        "label": "Photos",
        "folder": "photos",
        "code": "PHOTO",
    },
    "TSOP": {
        "label": "Training / SOP",
        "folder": "training_sop",
        "code": "TSOP",
    },
    "LEG": {
        "label": "Legal",
        "folder": "legal",
        "code": "LEG",
    },
    "VM": {
        "label": "Vendor Manual",
        "folder": "vendor_manual",
        "code": "VM",
    },
    "OTH": {
        "label": "Others",
        "folder": "others",
        "code": "OTH",
    },
}
PROJECT_MAP = {
    "1": {
        "label": "Project 1",
        "folder": "project_1",
    },
    "2": {
        "label": "Project 2",
        "folder": "project_2",
    },
    "3": {
        "label": "Project 3",
        "folder": "project_3",
    },
}

# Phải giống số PASSWORD_ITERATIONS của project Terminal cũ
PASSWORD_ITERATIONS = 600_000

ROLE_EMPLOYEE = "EMPLOYEE"
ROLE_MANAGER = "MANAGER"

# Giá trị đặc biệt dùng trong dropdown upload.
# Khi user chọn các giá trị này, hệ thống sẽ hiện ô nhập mới.
NEW_PROJECT_KEY = "__new_project__"
NEW_CATEGORY_KEY = "__new_category__"
DEFAULT_CATEGORY_PREFIX = "__default_category__:"

# Khóa session: mặc định là chuỗi demo nội bộ cũ để không phá vỡ session
# đang chạy khi chưa cấu hình biến môi trường. Đặt SESSION_SECRET_KEY
# trong môi trường (hoặc .env) khi triển khai thật, không dùng giá trị demo.
SESSION_SECRET_KEY = os.environ.get(
    "SESSION_SECRET_KEY",
    "day-la-khoa-demo-cho-he-thong-luu-tru-ho-so-2026",
)
