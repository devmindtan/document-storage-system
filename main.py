from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from core.config import STATIC_DIR, SESSION_SECRET_KEY
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
from api import auth, employee, manager, documents, admin, statistics

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

app.include_router(auth.router)
app.include_router(employee.router)
app.include_router(manager.router)
app.include_router(documents.router)
app.include_router(admin.router)
app.include_router(statistics.router)


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
