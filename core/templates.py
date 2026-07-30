from fastapi.templating import Jinja2Templates

from core.config import TEMPLATES_DIR

templates = Jinja2Templates(directory=TEMPLATES_DIR)
