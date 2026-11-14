# SPDX-License-Identifier: LGPL-3.0-or-later
# This file is part of PicPlan. See COPYING.LESSER and COPYING for license details.

from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.i18n import get_language, translate
from app.models import Event, event_participants
from app.routers.pages import csrf_token, router as pages_router
from app.settings import settings
from app.templating import templates

app = FastAPI(title="PicPlan", version="0.9.2")
app.add_middleware(SessionMiddleware, secret_key=settings.app_secret, same_site="lax", https_only=settings.app_env == "production")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
upload_dir = Path(settings.upload_dir)
upload_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=upload_dir), name="uploads")
app.include_router(pages_router)


@app.get("/", name="home")
def home(
    request: Request,
    language: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    selected_language = get_language(language or request.cookies.get("language"))
    events_query = select(Event).where(
        Event.end_date >= date.today(),
        Event.is_closed.is_(False),
    )
    participant_id = request.session.get("participant_id")
    is_admin = request.session.get("is_admin") is True
    debug_participant_id = request.session.get("debug_participant_id")
    is_debug_profile = (
        is_admin
        and isinstance(debug_participant_id, int)
        and not isinstance(debug_participant_id, bool)
    )
    if is_debug_profile:
        participant_id = debug_participant_id
    is_participant = (
        isinstance(participant_id, int) and not isinstance(participant_id, bool)
    )
    if (
        (not is_admin or is_debug_profile)
        and is_participant
    ):
        events_query = events_query.join(
            event_participants,
            event_participants.c.event_id == Event.id,
        ).where(event_participants.c.participant_id == participant_id)
    events = db.scalars(events_query.order_by(Event.start_date, Event.title)).unique().all()
    discord_auth_messages = {
        "success": "discord_auth_success",
        "cancelled": "discord_link_cancelled",
        "email_mismatch": "discord_link_email_mismatch",
        "already_linked": "discord_link_already_linked",
        "not_configured": "discord_link_not_configured",
        "failed": "discord_link_failed",
        "not_member": "discord_member_required",
        "role_not_configured": "discord_role_not_configured",
        "role_required": "discord_role_required",
        "discord_unavailable": "discord_role_check_failed",
        "login_required": "discord_login_required",
    }
    discord_auth_message = discord_auth_messages.get(
        request.query_params.get("discord_auth", "")
    )
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context={
            "events": events,
            "language": selected_language,
            "t": lambda key: translate(selected_language, key),
            "csrf_token": csrf_token(request),
            "is_admin": is_admin,
            "is_participant": is_participant,
            "is_debug_profile": is_debug_profile,
            "debug_profile_name": request.session.get("debug_participant_name"),
            "participant_name": (
                request.session.get("debug_participant_name")
                if is_debug_profile
                else request.session.get("participant_name")
            ),
            "discord_auth_message": discord_auth_message,
            "app_version": request.app.version,
            "discord_oauth_enabled": bool(
                settings.discord_client_id
                and settings.discord_client_secret
                and settings.discord_bot_token
                and settings.discord_guild_id.isdigit()
            ),
        },
    )


@app.get("/language", name="set_language")
def set_language(language: str = Query(default="fr")):
    selected_language = get_language(language)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie("language", selected_language, max_age=31536000, samesite="lax")
    return response


@app.get("/health", name="health")
def health():
    return {"status": "ok", "app": "PicPlan"}
