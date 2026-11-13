from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.i18n import format_time

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["format_time"] = format_time
