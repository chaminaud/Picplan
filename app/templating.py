# SPDX-License-Identifier: LGPL-3.0-or-later
# This file is part of PicPlan. See COPYING.LESSER and COPYING for license details.

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.i18n import format_time

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["format_time"] = format_time
