import logging
import secrets
from io import BytesIO
from datetime import date, datetime, time, timedelta, timezone
from hmac import compare_digest
from pathlib import Path
from time import time as unix_time
from urllib.parse import unquote, urlencode, urlsplit
from uuid import uuid4
import warnings

import httpx
import pillow_jxl  # noqa: F401
from pillow_heif import register_heif_opener
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.i18n import format_time, get_language, translate
from app.models import (
    Booking,
    BookingPhoto,
    BookingStatus,
    Event,
    Location,
    LocationPhoto,
    Participant,
    ParticipantPhoto,
    ParticipantType,
    TimeSlot,
    event_participants,
    utc_now,
)
from app.settings import settings
from app.templating import templates

register_heif_opener()

router = APIRouter()
logger = logging.getLogger(__name__)
MAX_IMAGE_SIZE = 20 * 1024 * 1024
MAX_PARTICIPANT_PHOTOS = 10
IMAGE_FORMATS = {
    "JPEG": "jpg",
    "PNG": "png",
    "WEBP": "webp",
    "GIF": "gif",
    "JXL": "jxl",
    "HEIF": "heic",
}
MAX_IMAGE_DIMENSION = 1024
JPEG_QUALITY = 70
DISCORD_ROLE_CACHE_SECONDS = 60


def booking_queue_sort_key(booking: Booking) -> tuple[datetime, int]:
    created_at = booking.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    else:
        created_at = created_at.astimezone(timezone.utc)
    return created_at, booking.id


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


async def check_csrf(request: Request) -> None:
    form = await request.form()
    submitted = str(form.get("csrf_token", ""))
    expected = str(request.session.get("csrf_token", ""))
    if not expected or not compare_digest(submitted, expected):
        raise HTTPException(status_code=403, detail="Invalid or missing form token.")


def require_admin(request: Request) -> None:
    if request.session.get("is_admin") is not True:
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})


def clear_participant_session(request: Request) -> None:
    request.session.pop("participant_id", None)
    request.session.pop("participant_name", None)
    request.session.pop("discord_authorized_role_id", None)
    request.session.pop("discord_roles_checked_at", None)


def discord_auth_redirect(status: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"/participant?{urlencode({'discord_auth': status})}",
        status_code=303,
    )


def participant_email_in_use(
    db: Session,
    email: str | None,
    exclude_id: int | None = None,
) -> bool:
    if not email:
        return False
    statement = select(Participant.id).where(func.lower(Participant.email) == email.lower())
    if exclude_id is not None:
        statement = statement.where(Participant.id != exclude_id)
    return db.scalar(statement.limit(1)) is not None


def discord_oauth_configured() -> bool:
    return bool(
        settings.discord_client_id
        and settings.discord_client_secret
        and settings.discord_bot_token
        and settings.discord_guild_id.isdigit()
    )


def referenced_upload_filenames(db: Session) -> set[str]:
    references = (
        db.scalars(select(Participant.avatar_url)).all()
        + db.scalars(select(ParticipantPhoto.photo_url)).all()
        + db.scalars(select(Location.floor_plan_url)).all()
        + db.scalars(select(LocationPhoto.photo_url)).all()
        + db.scalars(select(BookingPhoto.photo_url)).all()
    )
    filenames = set()
    for reference in references:
        if not reference:
            continue
        path = unquote(urlsplit(reference).path)
        if not path.startswith("/uploads/"):
            continue
        filename = path.removeprefix("/uploads/")
        if filename and Path(filename).name == filename:
            filenames.add(filename)
    return filenames


def find_orphaned_uploads(db: Session) -> list[Path]:
    upload_dir = Path(settings.upload_dir).resolve()
    referenced_filenames = referenced_upload_filenames(db)
    extensions = {f".{extension}" for extension in IMAGE_FORMATS.values()}
    if not upload_dir.exists():
        return []
    return sorted(
        (
            path
            for path in upload_dir.iterdir()
            if path.suffix.lower() in extensions
            and path.name not in referenced_filenames
            and not path.is_symlink()
            and path.is_file()
        ),
        key=lambda path: path.name,
    )


async def fetch_discord_member_roles(discord_id: str) -> tuple[list[str] | None, str | None]:
    if not settings.discord_bot_token or not settings.discord_guild_id.isdigit():
        return None, "not_configured"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"https://discord.com/api/v10/guilds/{settings.discord_guild_id}/members/{discord_id}",
                headers={"Authorization": f"Bot {settings.discord_bot_token}"},
            )
        if response.status_code == 404:
            return None, "not_member"
        response.raise_for_status()
        member = response.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("Discord member lookup returned HTTP %s.", exc.response.status_code)
        return None, "failed"
    except httpx.RequestError:
        logger.exception("Discord member lookup failed.")
        return None, "failed"
    except ValueError:
        logger.exception("Discord member lookup returned invalid JSON.")
        return None, "failed"

    roles = member.get("roles") if isinstance(member, dict) else None
    if not isinstance(roles, list) or not all(
        isinstance(role_id, str) and role_id.isdigit() for role_id in roles
    ):
        logger.warning("Discord member response did not contain valid role IDs.")
        return None, "failed"
    return roles, None


async def send_discord_dm(discord_id: str | None, message: str) -> None:
    if not settings.discord_bot_token:
        logger.warning("Cannot send Discord booking notification: bot token is not configured.")
        return
    if not discord_id or not discord_id.isdigit():
        logger.warning("Cannot send Discord booking notification: recipient has no linked Discord account.")
        return

    headers = {"Authorization": f"Bot {settings.discord_bot_token}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            channel_response = await client.post(
                "https://discord.com/api/v10/users/@me/channels",
                headers=headers,
                json={"recipient_id": discord_id},
            )
            channel_response.raise_for_status()
            channel = channel_response.json()
            channel_id = channel.get("id") if isinstance(channel, dict) else None
            if not isinstance(channel_id, str) or not channel_id.isdigit():
                logger.warning("Discord did not return a valid DM channel for a booking notification.")
                return

            message_response = await client.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                headers=headers,
                json={"content": message},
            )
            message_response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Discord booking notification failed with HTTP %s.",
            exc.response.status_code,
        )
    except httpx.RequestError:
        logger.exception("Discord booking notification request failed.")
    except ValueError:
        logger.exception("Discord returned invalid JSON while opening a DM channel.")


def get_public_base_url() -> str:
    public_base_url = settings.public_base_url.strip().rstrip("/")
    if not public_base_url:
        redirect_url = urlsplit(settings.discord_redirect_uri)
        if redirect_url.scheme not in {"http", "https"} or not redirect_url.netloc:
            raise RuntimeError(
                "Set PUBLIC_BASE_URL or configure a valid DISCORD_REDIRECT_URI "
                "to include links in booking notifications."
            )
        public_base_url = f"{redirect_url.scheme}://{redirect_url.netloc}"
    base_url = urlsplit(public_base_url)
    if (
        base_url.scheme not in {"http", "https"}
        or not base_url.netloc
        or base_url.path
        or base_url.query
        or base_url.fragment
        or base_url.username
        or base_url.password
    ):
        raise RuntimeError("PUBLIC_BASE_URL must be an HTTP or HTTPS origin without a path.")
    return public_base_url


def build_participant_area_url(
    participant_id: int,
    event_id: int,
    selected_date: date | None = None,
) -> str:
    query = {
        "participant_id": str(participant_id),
        "event_id": str(event_id),
    }
    if selected_date is not None:
        query["selected_date"] = selected_date.isoformat()
    return f"{get_public_base_url()}/participant?{urlencode(query)}"


async def notify_intervener_of_booking(
    slot: TimeSlot,
    participant: Participant,
    location: Location,
) -> None:
    participant_area_url = (
        build_participant_area_url(slot.intervener_id, slot.event_id)
        + "&schedule_scope=mine"
    )
    notification = translate(
        get_language(settings.default_language),
        "discord_booking_requested",
    ).format(
        event=slot.event.title,
        participant=participant.name,
        date=slot.date.strftime("%d/%m/%Y"),
        start=format_time(slot.start_time, get_language(settings.default_language)),
        end=format_time(slot.end_time, get_language(settings.default_language)),
        location=location.name,
        participant_area_url=participant_area_url,
    )
    await send_discord_dm(slot.intervener.discord_id, notification)


async def get_authorized_participant(
    request: Request,
    db: Session,
) -> tuple[Participant | None, str | None]:
    debug_participant_id = request.session.get("debug_participant_id")
    if (
        request.session.get("is_admin") is True
        and isinstance(debug_participant_id, int)
        and not isinstance(debug_participant_id, bool)
    ):
        participant = db.scalar(
            select(Participant)
            .where(Participant.id == debug_participant_id)
            .options(joinedload(Participant.participant_type))
        )
        if participant is None:
            request.session.pop("debug_participant_id", None)
            request.session.pop("debug_participant_name", None)
            return None, "login_required"
        request.session["debug_participant_name"] = participant.name
        return participant, None

    participant_id = request.session.get("participant_id")
    if not isinstance(participant_id, int) or isinstance(participant_id, bool):
        return None, None
    participant = db.scalar(
        select(Participant)
        .where(Participant.id == participant_id)
        .options(joinedload(Participant.participant_type))
    )
    if participant is None or not participant.discord_id:
        clear_participant_session(request)
        return None, "login_required"
    request.session["participant_name"] = participant.name

    required_role_id = participant.participant_type.discord_role_id
    if not required_role_id or not required_role_id.isdigit():
        clear_participant_session(request)
        return None, "role_not_configured"

    authorized_role_id = request.session.get("discord_authorized_role_id")
    checked_at = request.session.get("discord_roles_checked_at")
    if (
        authorized_role_id != required_role_id
        or not isinstance(checked_at, (int, float))
        or unix_time() - checked_at >= DISCORD_ROLE_CACHE_SECONDS
    ):
        role_ids, error = await fetch_discord_member_roles(participant.discord_id)
        if error == "not_configured":
            return None, "not_configured"
        if error == "not_member":
            clear_participant_session(request)
            return None, "not_member"
        if error or role_ids is None:
            return None, "discord_unavailable"
        if required_role_id not in role_ids:
            clear_participant_session(request)
            return None, "role_required"
        request.session["discord_authorized_role_id"] = required_role_id
        request.session["discord_roles_checked_at"] = unix_time()

    return participant, None


async def require_participant_access(
    request: Request,
    db: Session,
    *,
    participant_id: int | None = None,
    intervener: bool | None = None,
    allow_admin: bool = False,
) -> Participant:
    if allow_admin and request.session.get("is_admin") is True:
        participant = db.scalar(
            select(Participant)
            .where(Participant.id == participant_id)
            .options(joinedload(Participant.participant_type))
        )
        if participant is None:
            raise HTTPException(status_code=404, detail="Participant not found.")
        if intervener is not None and participant.participant_type.is_intervener != intervener:
            raise HTTPException(status_code=403, detail="This action is not permitted for this participant type.")
        return participant
    participant, error = await get_authorized_participant(request, db)
    if error == "discord_unavailable":
        raise HTTPException(status_code=503, detail="Discord role verification is temporarily unavailable.")
    if error or participant is None:
        raise HTTPException(
            status_code=303,
            headers={"Location": f"/participant?discord_auth={error or 'login_required'}"},
        )
    if participant_id is not None and participant.id != participant_id:
        raise HTTPException(status_code=403, detail="This profile does not belong to the signed-in Discord account.")
    if intervener is not None and participant.participant_type.is_intervener != intervener:
        raise HTTPException(status_code=403, detail="This action is not permitted for this participant type.")
    return participant


@router.get("/auth/discord/link", name="discord_link")
def discord_link(request: Request):
    clear_participant_session(request)
    if not discord_oauth_configured():
        return discord_auth_redirect("not_configured")
    state = secrets.token_urlsafe(32)
    request.session["discord_oauth_state"] = state
    authorization_url = "https://discord.com/oauth2/authorize?" + urlencode(
        {
            "client_id": settings.discord_client_id,
            "redirect_uri": settings.discord_redirect_uri,
            "response_type": "code",
            "scope": "identify email",
            "state": state,
        }
    )
    return RedirectResponse(url=authorization_url, status_code=303)


@router.get("/auth/discord/callback", name="discord_link_callback")
async def discord_link_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    expected_state = request.session.pop("discord_oauth_state", None)
    if (
        not isinstance(expected_state, str)
        or not isinstance(state, str)
        or not compare_digest(expected_state, state)
    ):
        clear_participant_session(request)
        return discord_auth_redirect("failed")
    if error:
        clear_participant_session(request)
        return discord_auth_redirect("cancelled")
    if not code:
        return discord_auth_redirect("failed")
    if not discord_oauth_configured():
        return discord_auth_redirect("not_configured")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            token_response = await client.post(
                "https://discord.com/api/v10/oauth2/token",
                data={
                    "client_id": settings.discord_client_id,
                    "client_secret": settings.discord_client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.discord_redirect_uri,
                },
            )
            token_response.raise_for_status()
            token_payload = token_response.json()
            if not isinstance(token_payload, dict):
                logger.warning("Discord OAuth token response was not an object.")
                return discord_auth_redirect("failed")
            access_token = token_payload.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                logger.warning("Discord OAuth token response did not contain an access token.")
                return discord_auth_redirect("failed")

            user_response = await client.get(
                "https://discord.com/api/v10/users/@me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            user_response.raise_for_status()
            discord_user = user_response.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("Discord OAuth API returned HTTP %s.", exc.response.status_code)
        return discord_auth_redirect("failed")
    except httpx.RequestError:
        logger.exception("Discord OAuth request failed.")
        return discord_auth_redirect("failed")
    except ValueError:
        logger.exception("Discord OAuth returned invalid JSON.")
        return discord_auth_redirect("failed")

    if not isinstance(discord_user, dict):
        logger.warning("Discord OAuth user response was not an object.")
        return discord_auth_redirect("failed")
    discord_id = discord_user.get("id")
    if not isinstance(discord_id, str) or not discord_id.isdigit():
        return discord_auth_redirect("failed")

    participant = db.scalar(
        select(Participant)
        .where(Participant.discord_id == discord_id)
        .options(joinedload(Participant.participant_type))
    )
    if participant is None:
        email = discord_user.get("email")
        if not isinstance(email, str) or discord_user.get("verified") is not True:
            return discord_auth_redirect("email_mismatch")
        matching_participants = db.scalars(
            select(Participant)
            .where(func.lower(Participant.email) == email.strip().lower())
            .limit(2)
            .options(joinedload(Participant.participant_type))
        ).all()
        if len(matching_participants) != 1:
            return discord_auth_redirect("email_mismatch")
        participant = matching_participants[0]

    role_ids, role_error = await fetch_discord_member_roles(discord_id)
    if role_error == "not_configured":
        return discord_auth_redirect("not_configured")
    if role_error == "not_member":
        return discord_auth_redirect("not_member")
    if role_error or role_ids is None:
        return discord_auth_redirect("discord_unavailable")
    required_role_id = participant.participant_type.discord_role_id
    if not required_role_id or not required_role_id.isdigit():
        return discord_auth_redirect("role_not_configured")
    if required_role_id not in role_ids:
        return discord_auth_redirect("role_required")

    participant.discord_id = discord_id
    discord_handle = discord_user.get("global_name") or discord_user.get("username")
    if isinstance(discord_handle, str) and discord_handle.strip():
        participant.discord_handle = discord_handle.strip()
    avatar = discord_user.get("avatar")
    participant.discord_avatar = avatar if isinstance(avatar, str) else None
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.info("Discord account link conflicted with an existing account link.")
        return discord_auth_redirect("already_linked")

    clear_participant_session(request)
    request.session["participant_id"] = participant.id
    request.session["participant_name"] = participant.name
    request.session["discord_authorized_role_id"] = required_role_id
    request.session["discord_roles_checked_at"] = unix_time()
    return discord_auth_redirect("success")


@router.post("/admin/uploads/images", name="upload_image")
async def upload_image(
    request: Request,
    image: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    if request.session.get("is_admin") is not True:
        await require_participant_access(request, db)
    language = get_language(request.cookies.get("language"))
    content = await image.read(MAX_IMAGE_SIZE + 1)
    if not content:
        raise HTTPException(status_code=400, detail=translate(language, "choose_image_error"))
    if len(content) > MAX_IMAGE_SIZE:
        raise HTTPException(status_code=413, detail=translate(language, "image_too_large"))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as uploaded_image:
                image_format = uploaded_image.format
                if image_format not in IMAGE_FORMATS:
                    raise HTTPException(status_code=415, detail=translate(language, "unsupported_image"))
                uploaded_image.verify()
            with Image.open(BytesIO(content)) as uploaded_image:
                normalized_image = ImageOps.exif_transpose(uploaded_image)
                normalized_image.thumbnail(
                    (MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION),
                    Image.Resampling.LANCZOS,
                )
                if "A" in normalized_image.getbands() or "transparency" in normalized_image.info:
                    rgba_image = normalized_image.convert("RGBA")
                    jpeg_image = Image.new("RGB", rgba_image.size, (255, 255, 255))
                    jpeg_image.paste(rgba_image, mask=rgba_image.getchannel("A"))
                else:
                    jpeg_image = normalized_image.convert("RGB")
                output = BytesIO()
                jpeg_image.save(
                    output,
                    format="JPEG",
                    quality=JPEG_QUALITY,
                    optimize=True,
                )
                content = output.getvalue()
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(status_code=415, detail=translate(language, "invalid_image")) from None

    filename = f"{uuid4().hex}.jpg"
    destination = Path(settings.upload_dir) / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return JSONResponse({"url": f"/uploads/{filename}", "filename": filename})


def render(
    request: Request,
    template: str,
    context: dict | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    language = get_language(request.query_params.get("language") or request.cookies.get("language"))
    values = context or {}
    values.update(
        {
            "language": language,
            "t": lambda key: translate(language, key),
            "csrf_token": csrf_token(request),
            "is_admin": request.session.get("is_admin") is True,
            "is_participant": (
                isinstance(request.session.get("participant_id"), int)
                or (
                    request.session.get("is_admin") is True
                    and isinstance(request.session.get("debug_participant_id"), int)
                    and not isinstance(request.session.get("debug_participant_id"), bool)
                )
            ),
            "participant_name": (
                request.session.get("debug_participant_name")
                if request.session.get("is_admin") is True
                else request.session.get("participant_name")
            ),
            "is_debug_profile": (
                request.session.get("is_admin") is True
                and isinstance(request.session.get("debug_participant_id"), int)
                and not isinstance(request.session.get("debug_participant_id"), bool)
            ),
        }
    )
    return templates.TemplateResponse(
        request=request,
        name=template,
        context=values,
        status_code=status_code,
    )


def form_date(value: object, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid {field}.") from None


def form_time(value: object, field: str) -> time:
    try:
        return time.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid {field}.") from None


def participant_photo_urls(value: object, language: str) -> list[str]:
    if not isinstance(value, str):
        return []
    urls = list(dict.fromkeys(url.strip() for url in value.splitlines() if url.strip()))
    if len(urls) > MAX_PARTICIPANT_PHOTOS:
        raise HTTPException(
            status_code=400,
            detail=translate(language, "participant_photo_limit_error"),
        )
    if any(len(url) > 2048 for url in urls):
        raise HTTPException(
            status_code=400,
            detail=translate(language, "participant_photo_url_too_long"),
        )
    return urls


def form_int(value: object, field: str, minimum: int = 1) -> int:
    try:
        result = int(str(value))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid {field}.") from None
    if result < minimum:
        raise HTTPException(status_code=400, detail=f"{field} must be at least {minimum}.")
    return result


SLOT_GENERATION_ERRORS = {
    "Invalid event.": "invalid_event",
    "Invalid intervener.": "invalid_intervener",
    "Invalid booking duration.": "invalid_booking_duration",
    "Invalid time between bookings.": "invalid_booking_gap",
    "booking duration must be at least 1.": "invalid_booking_duration",
    "time between bookings must be at least 0.": "invalid_booking_gap",
    "Duration and time between bookings cannot exceed 1440 minutes.": "slot_durations_too_long",
    "The schedule would create more than 10000 slots.": "too_many_generated_slots",
    "Event not found.": "event_not_found",
    "Choose a valid intervener.": "choose_valid_intervener",
    "Invalid start time.": "invalid_working_hours",
    "Invalid end time.": "invalid_working_hours",
    "Invalid lunch start.": "invalid_lunch_hours",
    "Invalid lunch end.": "invalid_lunch_hours",
    "End time must be after start time.": "invalid_working_hours",
    "Lunch end must be after lunch start.": "invalid_lunch_hours",
    "The lunch break must be inside the working hours or completely outside them.": "lunch_must_fit_working_hours",
    "These settings do not produce any booking slots.": "no_slots_from_settings",
}


def get_event_or_404(db: Session, event_id: int) -> Event:
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found.")
    return event


def get_open_event_or_404(db: Session, event_id: int) -> Event:
    event = db.get(Event, event_id)
    if event is None or event.is_closed:
        raise HTTPException(status_code=404, detail="Event not found.")
    return event


def participant_has_event(db: Session, participant_id: int, event_id: int) -> bool:
    return (
        db.scalar(
            select(event_participants.c.event_id).where(
                event_participants.c.event_id == event_id,
                event_participants.c.participant_id == participant_id,
            )
        )
        is not None
    )


def require_event_membership(
    request: Request,
    db: Session,
    participant: Participant,
    event_id: int,
) -> None:
    get_open_event_or_404(db, event_id)
    if not participant_has_event(db, participant.id, event_id):
        language = get_language(
            request.query_params.get("language") or request.cookies.get("language")
        )
        raise HTTPException(
            status_code=404,
            detail=translate(language, "event_member_required"),
        )


def get_slot_or_404(db: Session, slot_id: int) -> TimeSlot:
    slot = db.get(TimeSlot, slot_id)
    if slot is None:
        raise HTTPException(status_code=404, detail="Time slot not found.")
    return slot


def booking_action_error_redirect(
    participant_id: int,
    event_id: int,
    slot_date: date,
    form,
    error_code: str,
) -> RedirectResponse:
    if str(form.get("return_to_participant", "")) == "1":
        query = {
            "participant_id": str(participant_id),
            "event_id": str(event_id),
            "selected_date": str(form.get("selected_date") or slot_date.isoformat()),
            "schedule_scope": str(form.get("schedule_scope") or "all"),
            "booking_error": error_code,
        }
        return RedirectResponse(url=f"/participant?{urlencode(query)}", status_code=303)
    query = {"event_id": str(event_id), "booking_error": error_code}
    dashboard_event_id = str(form.get("dashboard_event_id", "")).strip()
    if dashboard_event_id.isdigit():
        query["event_id"] = dashboard_event_id
    return RedirectResponse(
        url=f"/interveners/{participant_id}?{urlencode(query)}",
        status_code=303,
    )


@router.get("/events/{event_id}", name="event_schedule")
async def event_schedule(
    event_id: int,
    request: Request,
    selected_date: date | None = None,
    participant_id: int | None = None,
    db: Session = Depends(get_db),
):
    is_admin = request.session.get("is_admin") is True
    is_debug_profile = is_admin and isinstance(
        request.session.get("debug_participant_id"), int
    )
    participant = None
    if not is_admin or is_debug_profile:
        participant, auth_error = await get_authorized_participant(request, db)
        if participant is None:
            return RedirectResponse(
                url=f"/?{urlencode({'discord_auth': auth_error or 'login_required'})}",
                status_code=303,
            )
        if participant_id and participant.id != participant_id:
            raise HTTPException(
                status_code=403,
                detail="This profile does not belong to the signed-in Discord account.",
            )
        if participant_id and participant.participant_type.is_intervener:
            raise HTTPException(
                status_code=403,
                detail="This action is not permitted for this participant type.",
            )
    elif participant_id:
        participant = await require_participant_access(
            request,
            db,
            participant_id=participant_id,
            intervener=False,
        )

    event = get_open_event_or_404(db, event_id)
    if participant is not None:
        require_event_membership(request, db, participant, event.id)
    if selected_date is None:
        selected_date = min(max(date.today(), event.start_date), event.end_date)
    if selected_date < event.start_date or selected_date > event.end_date:
        raise HTTPException(status_code=404, detail="Date is outside this event.")
    if participant and participant.participant_type.is_intervener:
        participant = None
    context = build_schedule_matrix(
        request=request,
        db=db,
        event=event,
        selected_date=selected_date,
        participant=participant,
        admin_view=not is_debug_profile,
    )
    return render(request, "event_schedule.html", context)


def build_schedule_matrix(
    request: Request,
    db: Session,
    event: Event,
    selected_date: date,
    participants: list[Participant] | None = None,
    participant: Participant | None = None,
    matrix_id: str = "schedule-container",
    include_all_interveners: bool = False,
    filter_intervener_id: int | None = None,
    admin_view: bool = True,
) -> dict:
    slot_query = (
        select(TimeSlot)
        .where(TimeSlot.event_id == event.id, TimeSlot.date == selected_date)
        .where(
            TimeSlot.intervener_id.in_(
                select(event_participants.c.participant_id).where(
                    event_participants.c.event_id == event.id
                )
            )
        )
        .options(
            joinedload(TimeSlot.intervener).joinedload(Participant.participant_type),
            joinedload(TimeSlot.bookings).joinedload(Booking.participant),
            joinedload(TimeSlot.bookings).joinedload(Booking.participant).joinedload(Participant.participant_type),
            joinedload(TimeSlot.bookings).joinedload(Booking.participant).joinedload(Participant.photos),
            joinedload(TimeSlot.bookings).joinedload(Booking.location),
            joinedload(TimeSlot.bookings).joinedload(Booking.photos),
        )
        .order_by(TimeSlot.start_time, TimeSlot.end_time)
    )
    if filter_intervener_id is not None:
        slot_query = slot_query.where(TimeSlot.intervener_id == filter_intervener_id)
    slots = db.scalars(slot_query).unique().all()
    intervener_query = (
        select(Participant)
        .join(event_participants, event_participants.c.participant_id == Participant.id)
        .join(ParticipantType)
        .where(
            event_participants.c.event_id == event.id,
            ParticipantType.is_intervener.is_(True),
        )
    )
    if filter_intervener_id is not None:
        intervener_query = intervener_query.where(
            Participant.id == filter_intervener_id
        )
    interveners = db.scalars(
        intervener_query
        .options(
            joinedload(Participant.participant_type),
            joinedload(Participant.photos),
        )
        .distinct()
        .order_by(Participant.name)
    ).unique().all()
    intervener_profiles = [
        {
            "id": intervener.id,
            "name": intervener.name,
            "emoji": intervener.participant_type.emoji or "🎨",
            "participant_type": intervener.participant_type.name,
            "avatar_url": intervener.avatar_url,
            "bio": intervener.bio or "",
            "social_url": intervener.social_url,
            "photos": [photo.photo_url for photo in intervener.photos],
        }
        for intervener in interveners
    ]
    locations = db.scalars(
        select(Location).where(Location.event_id == event.id).order_by(Location.name)
    ).all()
    days = [
        event.start_date + timedelta(days=offset)
        for offset in range((event.end_date - event.start_date).days + 1)
    ]

    active_statuses = (BookingStatus.PENDING, BookingStatus.APPROVED)
    daily_booking_count = 0
    booked_interveners: set[int] = set()
    if participant:
        daily_booking_count = (
            db.scalar(
                select(func.count(Booking.id))
                .join(TimeSlot)
                .where(
                    Booking.participant_id == participant.id,
                    TimeSlot.event_id == event.id,
                    TimeSlot.date == selected_date,
                    Booking.status.in_(active_statuses),
                )
            )
            or 0
        )
        booked_interveners = set(
            db.scalars(
                select(TimeSlot.intervener_id)
                .join(Booking)
                .where(
                    Booking.participant_id == participant.id,
                    TimeSlot.event_id == event.id,
                    TimeSlot.date == selected_date,
                    Booking.status.in_(active_statuses),
                )
            ).all()
        )

    if slots:
        first_minute = min(
            slot.start_time.hour * 60 + slot.start_time.minute for slot in slots
        )
        last_minute = max(
            slot.end_time.hour * 60 + slot.end_time.minute for slot in slots
        )
        grid_start = first_minute // 5 * 5
        grid_end = (last_minute + 4) // 5 * 5
    else:
        grid_start = grid_end = 0

    row_count = (grid_end - grid_start) // 5
    language = get_language(
        request.query_params.get("language") or request.cookies.get("language")
    )
    intervener_columns = {
        intervener.id: index for index, intervener in enumerate(interveners)
    }
    slot_intervals_by_intervener: dict[int, list[tuple[int, int]]] = {
        intervener.id: [] for intervener in interveners
    }
    for slot in slots:
        if slot.intervener_id in slot_intervals_by_intervener:
            slot_intervals_by_intervener[slot.intervener_id].append(
                (
                    slot.start_time.hour * 60 + slot.start_time.minute,
                    slot.end_time.hour * 60 + slot.end_time.minute,
                )
            )

    rows = []
    hour_labels = []
    for minute in range(grid_start, grid_end, 5):
        cells = [
            {
                "has_slot": any(
                    slot_start < minute + 5 and slot_end > minute
                    for slot_start, slot_end in slot_intervals_by_intervener[
                        intervener.id
                    ]
                )
            }
            for intervener in interveners
        ]
        rows.append(
            {
                "time": f"{minute // 60:02d}:00",
                "is_full_hour": minute % 60 == 0,
                "is_half_hour_line": (minute + 5) % 30 == 0,
                "has_slot": any(cell["has_slot"] for cell in cells),
                "cells": cells,
            }
        )
        if minute % 60 == 0:
            row_index = len(rows) - 1
            hour_labels.append(
                {
                    "time": format_time(
                        time(hour=minute // 60),
                        language,
                    ),
                    "row_start": row_index + 2,
                    "row_span": min(12, row_count - row_index),
                }
            )
    can_manage_schedule = admin_view and request.session.get("is_admin") is True
    can_manage_schedule = admin_view and request.session.get("is_admin") is True
    grid_slots = []
    for slot in slots:
        slot_start = slot.start_time.hour * 60 + slot.start_time.minute
        slot_end = slot.end_time.hour * 60 + slot.end_time.minute
        active_bookings = [
            booking for booking in slot.bookings if booking.status in active_statuses
        ]
        pending_count = sum(
            booking.status == BookingStatus.PENDING for booking in active_bookings
        )
        has_approved_booking = any(
            booking.status == BookingStatus.APPROVED for booking in active_bookings
        )
        own_booking = next(
            (
                booking
                for booking in active_bookings
                if participant and booking.participant_id == participant.id
            ),
            None,
        )
        can_review = bool(
            participant
            and participant.participant_type.is_intervener
            and slot.intervener_id == participant.id
        )
        can_view_participant_profile = bool(
            participant
        )
        can_book = bool(
            participant
            and not participant.participant_type.is_intervener
            and not slot.is_lunch_break
            and selected_date >= date.today()
            and own_booking is None
            and not has_approved_booking
            and pending_count < event.max_pending_bookings_per_slot
            and daily_booking_count < event.max_slots_per_participant_per_day
            and (
                event.allow_booking_same_intervener_per_day
                or slot.intervener_id not in booked_interveners
            )
        )
        if can_manage_schedule:
            display_bookings = [
                booking
                for booking in slot.bookings
                if booking.status != BookingStatus.REJECTED
            ]
        elif participant:
            if participant.participant_type.is_intervener:
                display_bookings = [
                    booking
                    for booking in slot.bookings
                    if booking.status in active_statuses
                ]
            else:
                display_bookings = [
                    booking
                    for booking in slot.bookings
                    if booking.status == BookingStatus.APPROVED
                    or booking.status == BookingStatus.PENDING
                ]
        else:
            display_bookings = [
                booking
                for booking in slot.bookings
                if booking.status == BookingStatus.APPROVED
            ]

        queue_positions = {
            booking.id: position
            for position, booking in enumerate(
                sorted(
                    (
                        booking
                        for booking in active_bookings
                        if booking.status == BookingStatus.PENDING
                    ),
                    key=booking_queue_sort_key,
                )
            )
        }
        rejected_count = sum(
            booking.status == BookingStatus.REJECTED
            for booking in slot.bookings
        )
        booking_details = [
            {
                "name": booking.participant.name,
                "emoji": booking.participant.participant_type.emoji or "👤",
                "status": translate(language, booking.status.value),
                "status_value": booking.status.value,
                "location": booking.location.name,
                "notes": (
                    booking.notes or ""
                    if booking.status != BookingStatus.PENDING
                    or can_review
                    or (participant and booking.participant_id == participant.id)
                    else ""
                ),
                "booking_id": booking.id,
                "queue_position": queue_positions.get(booking.id),
                "can_process": (
                    booking.status == BookingStatus.PENDING
                    and queue_positions.get(booking.id) == 0
                ),
                "can_reject": (
                    booking.status == BookingStatus.PENDING
                    and queue_positions.get(booking.id) == 0
                    and rejected_count
                    < max(event.max_pending_bookings_per_slot - 1, 0)
                ),
                "avatar_url": booking.participant.avatar_url
                or (
                    f"https://cdn.discordapp.com/avatars/{booking.participant.discord_id}/"
                    f"{booking.participant.discord_avatar}."
                    f"{'gif' if booking.participant.discord_avatar.startswith('a_') else 'png'}"
                    if can_view_participant_profile
                    and booking.participant.discord_id
                    and booking.participant.discord_avatar
                    else None
                ),
                "social_url": booking.participant.social_url,
                "bio": (booking.participant.bio or "") if can_view_participant_profile else "",
                "email": (booking.participant.email or "") if can_review else "",
                "discord_handle": (booking.participant.discord_handle or "") if can_review else "",
                "participant_type": booking.participant.participant_type.name if can_view_participant_profile else "",
                "profile_photos": (
                    [photo.photo_url for photo in booking.participant.photos]
                    if can_view_participant_profile
                    else []
                ),
                "photos": [photo.photo_url for photo in booking.photos] if can_review else [],
            }
            for booking in display_bookings
        ]
        column_index = intervener_columns.get(slot.intervener_id)
        if column_index is None:
            continue
        column = column_index + 2
        row_start = (slot_start - grid_start) // 5 + 2
        row_span = max(1, (slot_end - grid_start + 4) // 5 - (row_start - 2))
        grid_slots.append(
            {
                "id": slot.id,
                "date": slot.date.isoformat(),
                "column": column,
                "row_start": row_start,
                "row_span": row_span,
                "start": slot.start_time.strftime("%H:%M"),
                "end": slot.end_time.strftime("%H:%M"),
                "display_start": format_time(slot.start_time, language),
                "display_end": format_time(slot.end_time, language),
                "capacity": slot.capacity,
                "is_lunch_break": slot.is_lunch_break,
                "can_delete": not slot.bookings,
                "has_approved_booking": any(
                    booking.status == BookingStatus.APPROVED
                    for booking in active_bookings
                ),
                "intervener": slot.intervener.name,
                "intervener_emoji": slot.intervener.participant_type.emoji or "🎨",
                "can_book": can_book,
                "can_review": can_review,
                "can_edit": can_review,
                "intervener_id": participant.id if can_review else None,
                "csrf_token": csrf_token(request) if can_review else "",
                "own_booking_status": own_booking.status.value if own_booking else None,
                "queue_count": pending_count,
                "queue_limit": event.max_pending_bookings_per_slot,
                "remaining_refusals": max(
                    event.max_pending_bookings_per_slot - 1 - rejected_count,
                    0,
                ),
                "bookings": booking_details,
                "review_bookings": can_review,
                "display_names": ", ".join(item["name"] for item in booking_details),
                "display_locations": ", ".join(
                    dict.fromkeys(item["location"] for item in booking_details)
                ),
            }
        )

    return {
        "event": event,
        "selected_date": selected_date,
        "days": days,
        "participants": participants or [],
        "participant": participant,
        "participant_booked_count": daily_booking_count,
        "max_slots": event.max_slots_per_participant_per_day,
        "interveners": interveners,
        "selected_intervener": next(
            (
                intervener
                for intervener in interveners
                if intervener.id == filter_intervener_id
            ),
            None,
        ),
        "intervener_profiles": intervener_profiles,
        "locations": locations,
        "rows": rows,
        "hour_labels": hour_labels,
        "grid_slots": grid_slots,
        "grid_row_count": row_count,
        "grid_column_count": max(1, len(interveners)),
        "matrix_id": matrix_id,
        "csrf_token": csrf_token(request),
        "can_manage_schedule": can_manage_schedule,
        "language": language,
        "t": lambda key: translate(
            get_language(request.query_params.get("language") or request.cookies.get("language")),
            key,
        ),
    }


@router.post("/events/{event_id}/bookings/toggle", name="toggle_schedule_booking")
async def toggle_schedule_booking(
    event_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    form = await request.form()
    event = get_open_event_or_404(db, event_id)
    participant = await require_participant_access(request, db, intervener=False)
    require_event_membership(request, db, participant, event.id)
    participant_id = form_int(form.get("participant_id"), "participant")
    if participant_id != participant.id:
        raise HTTPException(status_code=403, detail="This profile does not belong to the signed-in Discord account.")
    selected_date = form_date(form.get("selected_date"), "date")
    selected_intervener_id = (
        form_int(form.get("intervener_id"), "intervener")
        if str(form.get("intervener_id", "")).strip()
        else None
    )
    active_statuses = (BookingStatus.PENDING, BookingStatus.APPROVED)
    language = get_language(request.cookies.get("language"))
    error_message = None
    requested_booking = False
    try:
        slot = get_slot_or_404(db, form_int(form.get("slot_id"), "time slot"))
        if slot.event_id != event.id or slot.date != selected_date:
            raise HTTPException(status_code=400, detail="Time slot does not match this event day.")
        if slot.is_lunch_break or slot.date < date.today():
            raise HTTPException(status_code=400, detail="This time slot cannot be booked.")
        existing_booking = db.scalar(
            select(Booking).where(
                Booking.time_slot_id == slot.id,
                Booking.participant_id == participant.id,
            )
        )
        if existing_booking and existing_booking.status in active_statuses:
            existing_booking.status = BookingStatus.CANCELLED
            db.commit()
        else:
            if db.scalar(
                select(Booking.id).where(
                    Booking.time_slot_id == slot.id,
                    Booking.status == BookingStatus.APPROVED,
                )
            ) is not None:
                raise HTTPException(status_code=409, detail="This time slot has already been approved.")
            location_id = form_int(form.get("location_id"), "location")
            location = db.get(Location, location_id)
            if location is None or location.event_id != event.id:
                raise HTTPException(status_code=400, detail="Choose a location from this event.")
            if slot.date >= date.today():
                pending_count = db.scalar(
                    select(func.count(Booking.id)).where(
                        Booking.time_slot_id == slot.id,
                        Booking.status == BookingStatus.PENDING,
                    )
                ) or 0
                if pending_count >= event.max_pending_bookings_per_slot:
                    raise HTTPException(status_code=409, detail="The booking queue for this time slot is full.")
            daily_count = db.scalar(
                select(func.count(Booking.id))
                .join(TimeSlot)
                .where(
                    Booking.participant_id == participant.id,
                    TimeSlot.event_id == event.id,
                    TimeSlot.date == slot.date,
                    Booking.status.in_(active_statuses),
                )
            ) or 0
            if daily_count >= event.max_slots_per_participant_per_day:
                raise HTTPException(status_code=409, detail="You have reached the daily booking limit for this event.")
            if not event.allow_booking_same_intervener_per_day:
                same_intervener = db.scalar(
                    select(Booking.id)
                    .join(TimeSlot)
                    .where(
                        Booking.participant_id == participant.id,
                        TimeSlot.event_id == event.id,
                        TimeSlot.date == slot.date,
                        TimeSlot.intervener_id == slot.intervener_id,
                        Booking.status.in_(active_statuses),
                    )
                )
                if same_intervener is not None:
                    raise HTTPException(status_code=409, detail="You already booked this intervener for that day.")
            booking = existing_booking or Booking(
                time_slot_id=slot.id,
                participant_id=participant.id,
                location_id=location.id,
            )
            booking.location_id = location.id
            booking.notes = str(form.get("notes", "")).strip() or None
            booking.status = BookingStatus.PENDING
            booking.created_at = utc_now()
            if existing_booking is None:
                db.add(booking)
            db.commit()
            requested_booking = True
    except HTTPException as error:
        if error.status_code == 404:
            raise
        booking_errors = {
            "This time slot cannot be booked.": "matrix_slot_unavailable",
            "Time slot does not match this event day.": "matrix_slot_unavailable",
            "Choose a location from this event.": "matrix_choose_location",
            "The booking queue for this time slot is full.": "matrix_slot_full",
            "This time slot has already been approved.": "matrix_slot_approved",
            "You have reached the daily booking limit for this event.": "matrix_daily_limit",
            "You already booked this intervener for that day.": "matrix_same_intervener",
        }
        error_message = translate(language, booking_errors.get(str(error.detail), "matrix_booking_error"))
    except IntegrityError:
        db.rollback()
        logger.warning(
            "Concurrent booking request conflicted for participant %s and time slot %s.",
            participant.id,
            form.get("slot_id"),
        )
        error_message = translate(language, "matrix_booking_conflict")

    if requested_booking:
        await notify_intervener_of_booking(slot, participant, location)

    context = build_schedule_matrix(
        request=request,
        db=db,
        event=event,
        selected_date=selected_date,
        participant=participant,
        filter_intervener_id=selected_intervener_id,
        matrix_id=(
            "participant-schedule-container"
            if form.get("return_to_participant") == "1"
            else "schedule-container"
        ),
        admin_view=form.get("return_to_participant") != "1",
    )
    context["matrix_error"] = error_message
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request=request,
            name="partials/schedule_matrix.html",
            context=context,
        )
    if form.get("return_to_participant") == "1":
        return RedirectResponse(
            url=f"/participant?participant_id={participant.id}&event_id={event.id}"
            f"&selected_date={selected_date.isoformat()}"
            f"{'&intervener_id=' + str(selected_intervener_id) if selected_intervener_id else ''}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/events/{event.id}?selected_date={selected_date.isoformat()}&participant_id={participant.id}",
        status_code=303,
    )


@router.get("/participant", name="participant_portal")
async def participant_portal(
    request: Request,
    participant_id: int | None = None,
    event_id: int | None = None,
    selected_date: date | None = None,
    intervener_id: int | None = None,
    schedule_scope: str = "mine",
    db: Session = Depends(get_db),
):
    selected, auth_error = await get_authorized_participant(request, db)
    if participant_id and selected and participant_id != selected.id:
        raise HTTPException(status_code=403, detail="This profile does not belong to the signed-in Discord account.")
    events_query = select(Event).where(
        Event.end_date >= date.today(),
        Event.is_closed.is_(False),
    )
    if selected is not None:
        events_query = events_query.join(
            event_participants,
            event_participants.c.event_id == Event.id,
        ).where(event_participants.c.participant_id == selected.id)
    events = db.scalars(events_query.order_by(Event.start_date, Event.title)).unique().all()
    selected_event = None
    selected_intervener = None
    event_interveners = []
    matrix = None
    bookings = []
    selected_scope = "mine" if selected and selected.participant_type.is_intervener else "all"
    if schedule_scope in {"mine", "all"} and selected and selected.participant_type.is_intervener:
        selected_scope = schedule_scope
    if selected:
        selected_event = next((event for event in events if event.id == event_id), None)
        if event_id and selected_event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        if selected_event is None and events:
            selected_event = events[0]
        if selected_event:
            if not selected.participant_type.is_intervener:
                event_interveners = db.scalars(
                    select(Participant)
                    .join(
                        event_participants,
                        event_participants.c.participant_id == Participant.id,
                    )
                    .join(ParticipantType)
                    .where(
                        event_participants.c.event_id == selected_event.id,
                        ParticipantType.is_intervener.is_(True),
                    )
                    .options(joinedload(Participant.participant_type))
                    .order_by(func.lower(Participant.name), Participant.id)
                ).unique().all()
                if intervener_id is not None:
                    selected_intervener = next(
                        (
                            intervener
                            for intervener in event_interveners
                            if intervener.id == intervener_id
                        ),
                        None,
                    )
                    if selected_intervener is None:
                        raise HTTPException(status_code=404, detail="Intervener not found for this event.")
            if selected_date is None:
                selected_date = min(max(date.today(), selected_event.start_date), selected_event.end_date)
            if selected_date < selected_event.start_date or selected_date > selected_event.end_date:
                raise HTTPException(status_code=404, detail="Date is outside this event.")
            matrix = build_schedule_matrix(
                request=request,
                db=db,
                event=selected_event,
                selected_date=selected_date,
                participant=selected,
                matrix_id="participant-schedule-container",
                include_all_interveners=selected_scope == "all",
                filter_intervener_id=(
                    selected.id
                    if selected.participant_type.is_intervener and selected_scope == "mine"
                    else selected_intervener.id if selected_intervener else None
                ),
                admin_view=False,
            )
        if not selected.participant_type.is_intervener:
            bookings = db.scalars(
                select(Booking)
                .join(TimeSlot)
                .join(Event, Event.id == TimeSlot.event_id)
                .join(
                    event_participants,
                    event_participants.c.event_id == TimeSlot.event_id,
                )
                .where(
                    Booking.participant_id == selected.id,
                    Event.is_closed.is_(False),
                    event_participants.c.participant_id == selected.id,
                )
                .options(
                    joinedload(Booking.time_slot).joinedload(TimeSlot.event),
                    joinedload(Booking.time_slot).joinedload(TimeSlot.intervener),
                    joinedload(Booking.location),
                )
                .order_by(Booking.created_at.desc())
            ).unique().all()
    discord_auth_message = {
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
    }.get(auth_error or request.query_params.get("discord_auth", ""))
    context = {
        "participant": selected,
        "events": events,
        "selected_event": selected_event,
        "selected_date": selected_date,
        "event_interveners": event_interveners,
        "selected_intervener": selected_intervener,
        "matrix": matrix,
        "bookings": bookings,
        "schedule_scope": selected_scope,
        "generated_slots": (
            request.query_params["generated"]
            if request.query_params.get("generated", "").isdigit()
            else None
        ),
        "discord_oauth_enabled": discord_oauth_configured(),
        "discord_auth_message": discord_auth_message,
        "booking_action_message": {
            "already_processed": "booking_action_already_processed",
            "rejection_limit_reached": "booking_action_rejection_limit_reached",
            "approval_conflict": "booking_action_approval_conflict",
            "not_first_in_queue": "booking_action_not_first_in_queue",
        }.get(request.query_params.get("booking_error", "")),
    }
    if matrix:
        context.update(matrix)
    return render(request, "participant_portal.html", context)


@router.post("/auth/discord/logout", name="discord_logout")
async def discord_logout(request: Request):
    await check_csrf(request)
    clear_participant_session(request)
    return RedirectResponse(url="/participant", status_code=303)


@router.get("/participant/profile", name="participant_profile")
async def participant_profile(
    request: Request,
    db: Session = Depends(get_db),
):
    participant = await require_participant_access(request, db)
    message = (
        "participant_profile_updated"
        if request.query_params.get("updated") == "1"
        else None
    )
    return render(
        request,
        "participant_profile.html",
        {
            "participant": participant,
            "profile_message": message,
        },
    )


@router.post("/participant/profile", name="update_own_participant_profile")
async def update_own_participant_profile(
    request: Request,
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    participant = await require_participant_access(request, db)
    form = await request.form()
    photo_urls = participant_photo_urls(
        form.get("photo_urls"),
        get_language(request.cookies.get("language")),
    )
    name = str(form.get("name", "")).strip()
    if not name:
        raise HTTPException(
            status_code=400,
            detail=translate(
                get_language(request.cookies.get("language")),
                "participant_name_required",
            ),
        )
    participant.name = name
    participant.bio = str(form.get("bio", "")).strip() or None
    participant.discord_handle = str(form.get("discord_handle", "")).strip() or None
    participant.social_url = str(form.get("social_url", "")).strip() or None
    participant.avatar_url = str(form.get("avatar_url", "")).strip() or None
    participant.photos = [ParticipantPhoto(photo_url=url) for url in photo_urls]
    db.commit()
    request.session["participant_name"] = participant.name
    return RedirectResponse(url="/participant/profile?updated=1", status_code=303)


@router.post("/participant/bookings", name="create_booking")
async def create_booking(request: Request, db: Session = Depends(get_db)):
    await check_csrf(request)
    form = await request.form()
    participant = await require_participant_access(request, db, intervener=False)
    submitted_participant_id = form_int(form.get("participant_id"), "participant")
    if submitted_participant_id != participant.id:
        raise HTTPException(status_code=403, detail="This profile does not belong to the signed-in Discord account.")
    slot = db.get(TimeSlot, form_int(form.get("time_slot_id"), "time slot"))
    location = db.get(Location, form_int(form.get("location_id"), "location"))
    if participant is None or slot is None or location is None:
        raise HTTPException(status_code=404, detail="Participant, time slot, or location not found.")
    if slot.is_lunch_break or slot.date < date.today():
        raise HTTPException(status_code=400, detail="This time slot cannot be booked.")
    if location.event_id != slot.event_id:
        raise HTTPException(status_code=400, detail="Choose a location from this event.")
    existing = db.scalar(
        select(Booking).where(
            Booking.time_slot_id == slot.id,
            Booking.participant_id == participant.id,
        )
    )
    if existing and existing.status in (BookingStatus.PENDING, BookingStatus.APPROVED):
        raise HTTPException(status_code=409, detail="You already have an active booking for this time slot.")
    event = get_open_event_or_404(db, slot.event_id)
    require_event_membership(request, db, participant, event.id)
    if db.scalar(
        select(Booking.id).where(
            Booking.time_slot_id == slot.id,
            Booking.status == BookingStatus.APPROVED,
        )
    ) is not None:
        raise HTTPException(status_code=409, detail="This time slot has already been approved.")
    pending_count = db.scalar(
        select(func.count(Booking.id)).where(
            Booking.time_slot_id == slot.id,
            Booking.status == BookingStatus.PENDING,
        )
    ) or 0
    if pending_count >= event.max_pending_bookings_per_slot:
        raise HTTPException(status_code=409, detail="The booking queue for this time slot is full.")
    daily_count = db.scalar(
        select(func.count(Booking.id))
        .join(TimeSlot)
        .where(
            Booking.participant_id == participant.id,
            TimeSlot.event_id == event.id,
            TimeSlot.date == slot.date,
            Booking.status.in_([BookingStatus.PENDING, BookingStatus.APPROVED]),
        )
    ) or 0
    if daily_count >= event.max_slots_per_participant_per_day:
        raise HTTPException(status_code=409, detail="You have reached the daily booking limit for this event.")
    if not event.allow_booking_same_intervener_per_day:
        same_intervener = db.scalar(
            select(Booking.id)
            .join(TimeSlot)
            .where(
                Booking.participant_id == participant.id,
                TimeSlot.event_id == event.id,
                TimeSlot.date == slot.date,
                TimeSlot.intervener_id == slot.intervener_id,
                Booking.status.in_([BookingStatus.PENDING, BookingStatus.APPROVED]),
            )
        )
        if same_intervener is not None:
            raise HTTPException(status_code=409, detail="You already booked this intervener for that day.")
    booking = existing or Booking(
        time_slot_id=slot.id,
        participant_id=participant.id,
        location_id=location.id,
    )
    booking.location_id = location.id
    booking.notes = str(form.get("notes", "")).strip() or None
    booking.status = BookingStatus.PENDING
    booking.created_at = utc_now()
    if existing is None:
        db.add(booking)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        logger.warning(
            "Concurrent booking request conflicted for participant %s and time slot %s.",
            participant.id,
            slot.id,
        )
        raise HTTPException(
            status_code=409,
            detail="A booking for this time slot already exists. Refresh and try again.",
        ) from error
    await notify_intervener_of_booking(slot, participant, location)
    return RedirectResponse(url=f"/participant?participant_id={participant.id}", status_code=303)


@router.post("/participant/bookings/{booking_id}/cancel", name="cancel_booking")
async def cancel_booking(booking_id: int, request: Request, db: Session = Depends(get_db)):
    await check_csrf(request)
    form = await request.form()
    participant = await require_participant_access(request, db)
    participant_id = form_int(form.get("participant_id"), "participant")
    if participant_id != participant.id:
        raise HTTPException(status_code=403, detail="This profile does not belong to the signed-in Discord account.")
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found.")
    if booking.participant_id != participant_id:
        raise HTTPException(status_code=403, detail="This booking does not belong to this participant.")
    require_event_membership(request, db, participant, booking.time_slot.event_id)
    if booking.status not in (BookingStatus.PENDING, BookingStatus.APPROVED):
        raise HTTPException(status_code=409, detail="This booking can no longer be cancelled.")
    booking.status = BookingStatus.CANCELLED
    db.commit()
    return RedirectResponse(url=f"/participant?participant_id={participant_id}", status_code=303)


@router.get("/interveners/{participant_id}", name="intervener_dashboard")
async def intervener_dashboard(
    participant_id: int,
    request: Request,
    event_id: str | None = None,
    db: Session = Depends(get_db),
):
    participant = await require_participant_access(
        request,
        db,
        participant_id=participant_id,
        intervener=True,
        allow_admin=True,
    )
    events = db.scalars(
        select(Event)
        .join(event_participants, event_participants.c.event_id == Event.id)
        .where(
            event_participants.c.participant_id == participant.id,
            Event.is_closed.is_(False),
        )
        .order_by(Event.start_date, Event.title)
    ).unique().all()
    if event_id and not event_id.isdigit():
        raise HTTPException(status_code=404, detail="Event not found for this intervener.")
    selected_event_id = int(event_id) if event_id else None
    if selected_event_id is not None and all(
        event.id != selected_event_id for event in events
    ):
        raise HTTPException(status_code=404, detail="Event not found for this intervener.")
    slot_query = (
        select(TimeSlot)
        .where(
            TimeSlot.intervener_id == participant_id,
            TimeSlot.date >= date.today(),
            TimeSlot.event_id.in_(
                select(Event.id).where(Event.is_closed.is_(False))
            ),
            TimeSlot.event_id.in_(
                select(event_participants.c.event_id).where(
                    event_participants.c.participant_id == participant_id
                )
            ),
        )
        .options(
            joinedload(TimeSlot.event),
            joinedload(TimeSlot.bookings).joinedload(Booking.participant),
            joinedload(TimeSlot.bookings).joinedload(Booking.participant).joinedload(Participant.photos),
            joinedload(TimeSlot.bookings).joinedload(Booking.location),
        )
        .order_by(TimeSlot.date, TimeSlot.start_time)
    )
    if selected_event_id is not None:
        slot_query = slot_query.where(TimeSlot.event_id == selected_event_id)
    slots = db.scalars(slot_query).unique().all()
    language = get_language(
        request.query_params.get("language") or request.cookies.get("language")
    )
    message = None
    slot_error = request.query_params.get("slot_error")
    if slot_error:
        message = slot_generation_error_message(
            HTTPException(status_code=400, detail=slot_error),
            language,
        )
    elif request.query_params.get("generated", "").isdigit():
        message = translate(language, "intervener_slots_generated").format(
            count=request.query_params["generated"]
        )
    elif (
        request.query_params.get("deleted", "").isdigit()
        and request.query_params.get("preserved", "").isdigit()
    ):
        message = translate(language, "intervener_slots_deleted").format(
            deleted=request.query_params["deleted"],
            preserved=request.query_params["preserved"],
        )
    elif request.query_params.get("booking_error") in {
        "already_processed",
        "rejection_limit_reached",
        "approval_conflict",
        "not_first_in_queue",
    }:
        message = translate(
            language,
            f"booking_action_{request.query_params['booking_error']}",
        )
    return render(
        request,
        "intervener_dashboard.html",
        {
            "participant": participant,
            "slots": slots,
            "events": events,
            "selected_event_id": str(selected_event_id) if selected_event_id is not None else "",
            "slot_message": message,
        },
    )


@router.post(
    "/interveners/{participant_id}/slots/delete",
    name="intervener_delete_event_slots",
)
async def intervener_delete_event_slots(
    participant_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    participant = await require_participant_access(
        request,
        db,
        participant_id=participant_id,
        intervener=True,
        allow_admin=True,
    )
    form = await request.form()
    event_id = form_int(form.get("event_id"), "event")
    get_open_event_or_404(db, event_id)
    if request.session.get("is_admin") is not True:
        require_event_membership(request, db, participant, event_id)
    slots = db.scalars(
        select(TimeSlot)
        .where(
            TimeSlot.event_id == event_id,
            TimeSlot.intervener_id == participant.id,
        )
        .options(joinedload(TimeSlot.bookings))
    ).unique().all()
    deletable_slots = [slot for slot in slots if not slot.bookings]
    preserved_slots = len(slots) - len(deletable_slots)
    for slot in deletable_slots:
        db.delete(slot)
    db.commit()
    return RedirectResponse(
        url=(
            f"/interveners/{participant.id}?"
            + urlencode(
                {
                    "event_id": str(event_id),
                    "deleted": str(len(deletable_slots)),
                    "preserved": str(preserved_slots),
                }
            )
        ),
        status_code=303,
    )


@router.post(
    "/interveners/{participant_id}/slots/create",
    name="intervener_create_slots",
)
async def intervener_create_slots(
    participant_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    participant = await require_participant_access(
        request,
        db,
        participant_id=participant_id,
        intervener=True,
        allow_admin=True,
    )
    form = await request.form()
    event_id = form_int(form.get("event_id"), "event")
    event = get_open_event_or_404(db, event_id)
    if request.session.get("is_admin") is not True:
        require_event_membership(request, db, participant, event.id)
    try:
        generated_slots = generate_time_slots_for_event(
            db,
            event,
            participant,
            duration_minutes=form_int(
                form.get("duration_minutes"),
                "booking duration",
            ),
            break_minutes=form_int(
                form.get("break_minutes"),
                "time between bookings",
                minimum=0,
            ),
            day_start=form_time(form.get("start_time"), "start time"),
            day_end=form_time(form.get("end_time"), "end time"),
            lunch_start=form_time(form.get("lunch_start"), "lunch start"),
            lunch_end=form_time(form.get("lunch_end"), "lunch end"),
        )
    except HTTPException as error:
        return RedirectResponse(
            url=(
                f"/interveners/{participant.id}?"
                + urlencode(
                    {
                        "event_id": str(event_id),
                        "slot_error": str(error.detail),
                    }
                )
            ),
            status_code=303,
        )
    db.add_all(generated_slots)
    db.commit()
    booking_slot_count = sum(not slot.is_lunch_break for slot in generated_slots)
    return RedirectResponse(
        url=(
            f"/participant?"
            + urlencode(
                {
                    "participant_id": str(participant.id),
                    "event_id": str(event.id),
                    "schedule_scope": "all",
                    "generated": str(booking_slot_count),
                }
            )
        ),
        status_code=303,
    )


@router.post("/interveners/bookings/{booking_id}/{action}", name="update_booking_status")
async def update_booking_status(
    booking_id: int,
    action: str,
    request: Request,
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    form = await request.form()
    participant_id = form_int(form.get("participant_id"), "intervener")
    participant = await require_participant_access(
        request,
        db,
        participant_id=participant_id,
        intervener=True,
        allow_admin=True,
    )
    booking = db.scalar(
        select(Booking)
        .join(TimeSlot)
        .where(Booking.id == booking_id, TimeSlot.intervener_id == participant.id)
        .options(
            joinedload(Booking.participant),
            joinedload(Booking.time_slot).joinedload(TimeSlot.event),
        )
    )
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found for this intervener.")
    if request.session.get("is_admin") is not True:
        require_event_membership(request, db, participant, booking.time_slot.event_id)
    if action not in {"approve", "reject"}:
        raise HTTPException(status_code=409, detail="This booking is no longer pending.")
    if booking.status != BookingStatus.PENDING:
        return booking_action_error_redirect(
            participant.id,
            booking.time_slot.event_id,
            booking.time_slot.date,
            form,
            "already_processed",
        )
    pending_bookings = db.scalars(
        select(Booking)
        .where(
            Booking.time_slot_id == booking.time_slot_id,
            Booking.status == BookingStatus.PENDING,
        )
        .options(joinedload(Booking.participant))
        .order_by(Booking.created_at, Booking.id)
    ).unique().all()
    if not pending_bookings or pending_bookings[0].id != booking.id:
        return booking_action_error_redirect(
            participant.id,
            booking.time_slot.event_id,
            booking.time_slot.date,
            form,
            "not_first_in_queue",
        )
    rejection_reason = ""
    if action == "approve":
        approved_count = db.scalar(
            select(func.count(Booking.id)).where(
                Booking.time_slot_id == booking.time_slot_id,
                Booking.status == BookingStatus.APPROVED,
            )
        ) or 0
        if approved_count >= 1:
            return booking_action_error_redirect(
                participant.id,
                booking.time_slot.event_id,
                booking.time_slot.date,
                form,
                "approval_conflict",
            )
        approval_update = db.execute(
            update(Booking)
            .where(
                Booking.id == booking.id,
                Booking.status == BookingStatus.PENDING,
                ~select(Booking.id)
                .where(
                    Booking.time_slot_id == booking.time_slot_id,
                    Booking.status == BookingStatus.APPROVED,
                )
                .exists(),
            )
            .values(status=BookingStatus.APPROVED)
            .execution_options(synchronize_session="fetch")
        )
        if approval_update.rowcount != 1:
            db.refresh(booking)
            error_code = (
                "already_processed"
                if booking.status != BookingStatus.PENDING
                else "approval_conflict"
            )
            return booking_action_error_redirect(
                participant.id,
                booking.time_slot.event_id,
                booking.time_slot.date,
                form,
                error_code,
            )
        remaining_ids = [item.id for item in pending_bookings if item.id != booking.id]
        if remaining_ids:
            db.execute(
                update(Booking)
                .where(
                    Booking.id.in_(remaining_ids),
                    Booking.status == BookingStatus.PENDING,
                )
                .values(status=BookingStatus.REJECTED)
                .execution_options(synchronize_session="fetch")
            )
    else:
        rejected_count = db.scalar(
            select(func.count(Booking.id)).where(
                Booking.time_slot_id == booking.time_slot_id,
                Booking.status == BookingStatus.REJECTED,
            )
        ) or 0
        if rejected_count >= max(
            booking.time_slot.event.max_pending_bookings_per_slot - 1,
            0,
        ):
            return booking_action_error_redirect(
                participant.id,
                booking.time_slot.event_id,
                booking.time_slot.date,
                form,
                "rejection_limit_reached",
            )
        rejection_reason = str(form.get("reason", "")).strip()
        if not rejection_reason or len(rejection_reason) > 1000:
            raise HTTPException(
                status_code=400,
                detail="A rejection reason of at most 1000 characters is required.",
            )
        rejection_update = db.execute(
            update(Booking)
            .where(
                Booking.id == booking.id,
                Booking.status == BookingStatus.PENDING,
            )
            .values(status=BookingStatus.REJECTED)
            .execution_options(synchronize_session="fetch")
        )
        if rejection_update.rowcount != 1:
            db.refresh(booking)
            return booking_action_error_redirect(
                participant.id,
                booking.time_slot.event_id,
                booking.time_slot.date,
                form,
                "already_processed",
            )
    db.commit()
    notification_language = get_language(settings.default_language)
    notification_fields = dict(
        event=booking.time_slot.event.title,
        date=booking.time_slot.date.strftime("%d/%m/%Y"),
        start=format_time(booking.time_slot.start_time, notification_language),
        end=format_time(booking.time_slot.end_time, notification_language),
        reason=rejection_reason,
    )
    if action == "approve":
        notification_fields["participant_area_url"] = build_participant_area_url(
            booking.participant_id,
            booking.time_slot.event_id,
            booking.time_slot.date,
        )
    notification = translate(
        notification_language,
        (
            "discord_booking_approved"
            if action == "approve"
            else "discord_booking_rejected"
        ),
    ).format(**notification_fields)
    await send_discord_dm(booking.participant.discord_id, notification)
    if action == "approve":
        queue_rejection_reason = translate(
            notification_language,
            "discord_booking_rejected_after_queue_approval_reason",
        ).format(participant=booking.participant.name)
        for queued_booking in pending_bookings:
            if queued_booking.id == booking.id:
                continue
            queued_notification = translate(
                notification_language,
                "discord_booking_rejected",
            ).format(
                event=booking.time_slot.event.title,
                date=booking.time_slot.date.strftime("%d/%m/%Y"),
                start=format_time(
                    booking.time_slot.start_time,
                    notification_language,
                ),
                end=format_time(
                    booking.time_slot.end_time,
                    notification_language,
                ),
                reason=queue_rejection_reason,
            )
            await send_discord_dm(
                queued_booking.participant.discord_id,
                queued_notification,
            )
    if request.headers.get("HX-Request") == "true":
        slot = db.scalar(
            select(TimeSlot)
            .where(TimeSlot.id == booking.time_slot_id)
            .options(
                joinedload(TimeSlot.event),
                joinedload(TimeSlot.bookings).joinedload(Booking.participant),
                joinedload(TimeSlot.bookings).joinedload(Booking.participant).joinedload(Participant.photos),
                joinedload(TimeSlot.bookings).joinedload(Booking.location),
            )
        )
        return render(
            request,
            "partials/intervener_slot.html",
            {
                "slot": slot,
                "intervener_id": participant_id,
                "return_to_participant": form.get("return_to_participant") == "1",
                "selected_event_id": str(form.get("dashboard_event_id", "")).strip(),
            },
        )
    if form.get("return_to_participant") == "1":
        query = {"participant_id": str(participant_id)}
        for field in ("event_id", "selected_date", "schedule_scope"):
            value = str(form.get(field, "")).strip()
            if value:
                query[field] = value
        return RedirectResponse(url=f"/participant?{urlencode(query)}", status_code=303)
    dashboard_event_id = str(form.get("dashboard_event_id", "")).strip()
    query = {"event_id": dashboard_event_id} if dashboard_event_id.isdigit() else {}
    return RedirectResponse(
        url=f"/interveners/{participant_id}?{urlencode(query)}" if query else f"/interveners/{participant_id}",
        status_code=303,
    )


@router.get("/admin/login", name="admin_login")
def admin_login(request: Request):
    if request.session.get("is_admin") is True:
        return RedirectResponse(url="/admin", status_code=303)
    return render(request, "admin_login.html", {"admin_configured": bool(settings.admin_password)})


@router.post("/admin/login", name="admin_login_submit")
async def admin_login_submit(request: Request):
    await check_csrf(request)
    form = await request.form()
    submitted_password = str(form.get("password", ""))
    if not settings.admin_password:
        return render(request, "admin_login.html", {"admin_configured": False, "login_error": True}, 503)
    if not compare_digest(submitted_password, settings.admin_password):
        return render(request, "admin_login.html", {"admin_configured": True, "login_error": True}, 401)
    request.session.clear()
    request.session["is_admin"] = True
    csrf_token(request)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/logout", name="admin_logout")
async def admin_logout(request: Request, _admin: None = Depends(require_admin)):
    await check_csrf(request)
    request.session.pop("is_admin", None)
    request.session.pop("debug_participant_id", None)
    request.session.pop("debug_participant_name", None)
    return RedirectResponse(url="/", status_code=303)


@router.get("/admin/debug/profile", name="admin_debug_profile")
def admin_debug_profile(
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    participants = db.scalars(
        select(Participant)
        .options(joinedload(Participant.participant_type))
        .order_by(Participant.name)
    ).unique().all()
    return render(
        request,
        "admin_debug_profile.html",
        {
            "participants": participants,
            "selected_participant_id": request.session.get("debug_participant_id"),
        },
    )


@router.post("/admin/debug/profile", name="start_admin_debug_profile")
async def start_admin_debug_profile(
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    form = await request.form()
    participant_id = form_int(form.get("participant_id"), "participant")
    participant = db.scalar(
        select(Participant)
        .where(Participant.id == participant_id)
        .options(joinedload(Participant.participant_type))
    )
    if participant is None:
        raise HTTPException(status_code=404, detail="Participant not found.")
    request.session["debug_participant_id"] = participant.id
    request.session["debug_participant_name"] = participant.name
    logger.info(
        "Administrator started temporary debug profile participant_id=%s.",
        participant.id,
    )
    return RedirectResponse(url="/participant", status_code=303)


@router.post("/admin/debug/profile/stop", name="stop_admin_debug_profile")
async def stop_admin_debug_profile(
    request: Request,
    _admin: None = Depends(require_admin),
):
    await check_csrf(request)
    participant_id = request.session.pop("debug_participant_id", None)
    request.session.pop("debug_participant_name", None)
    if isinstance(participant_id, int) and not isinstance(participant_id, bool):
        logger.info(
            "Administrator stopped temporary debug profile participant_id=%s.",
            participant_id,
        )
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/admin", name="admin_dashboard")
def admin_dashboard(request: Request, _admin: None = Depends(require_admin), db: Session = Depends(get_db)):
    counts = {
        "events": db.scalar(
            select(func.count(Event.id)).where(Event.is_closed.is_(False))
        ) or 0,
        "locations": db.scalar(
            select(func.count(Location.id))
            .join(Event)
            .where(Event.is_closed.is_(False))
        ) or 0,
        "participants": db.scalar(select(func.count(Participant.id))) or 0,
        "pending": db.scalar(
            select(func.count(Booking.id))
            .join(TimeSlot)
            .join(Event)
            .where(
                Booking.status == BookingStatus.PENDING,
                Event.is_closed.is_(False),
            )
        ) or 0,
    }
    events = db.scalars(
        select(Event)
        .where(Event.is_closed.is_(False))
        .order_by(Event.start_date.desc())
    ).all()
    return render(request, "admin_dashboard.html", {"counts": counts, "events": events})


@router.get("/admin/maintenance", name="admin_maintenance")
def admin_maintenance(
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    orphan_files = [
        {
            "name": path.name,
            "size": path.stat().st_size,
        }
        for path in find_orphaned_uploads(db)
    ]
    result = request.query_params.get("result")
    message_key = {
        "deleted": "orphan_photo_deleted",
        "referenced": "orphan_photo_became_referenced",
        "missing": "orphan_photo_missing",
    }.get(result)
    return render(
        request,
        "admin_maintenance.html",
        {"orphan_files": orphan_files, "result_message": message_key},
    )


@router.post("/admin/maintenance/delete", name="delete_orphan_photo")
async def delete_orphan_photo(
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    form = await request.form()
    filename = str(form.get("filename", ""))
    upload_dir = Path(settings.upload_dir).resolve()
    photo_path = upload_dir / filename
    valid_filename = (
        bool(filename)
        and "\x00" not in filename
        and Path(filename).name == filename
        and Path(filename).suffix.lower()
        in {f".{extension}" for extension in IMAGE_FORMATS.values()}
    )
    if not valid_filename or photo_path.parent != upload_dir:
        raise HTTPException(status_code=400, detail="Invalid uploaded image filename.")
    if photo_path.is_symlink() or not photo_path.is_file():
        return RedirectResponse(
            url="/admin/maintenance?result=missing",
            status_code=303,
        )
    if filename in referenced_upload_filenames(db):
        return RedirectResponse(
            url="/admin/maintenance?result=referenced",
            status_code=303,
        )
    photo_path.unlink()
    logger.info("Administrator deleted orphaned uploaded image filename=%s.", filename)
    return RedirectResponse(
        url="/admin/maintenance?result=deleted",
        status_code=303,
    )


def slot_generation_context(db: Session, selected_event_id: int | None = None) -> dict:
    events = db.scalars(
        select(Event)
        .where(Event.is_closed.is_(False))
        .order_by(Event.start_date, Event.title)
    ).all()
    interveners_query = (
        select(Participant)
        .join(ParticipantType)
        .where(ParticipantType.is_intervener.is_(True))
    )
    if selected_event_id is not None:
        interveners_query = interveners_query.join(
            event_participants,
            event_participants.c.participant_id == Participant.id,
        ).where(event_participants.c.event_id == selected_event_id)
    interveners = db.scalars(interveners_query.order_by(Participant.name)).unique().all()
    selected_event = (
        db.scalar(
            select(Event).where(
                Event.id == selected_event_id,
                Event.is_closed.is_(False),
            )
        )
        if selected_event_id
        else None
    )
    return {
        "events": events,
        "interveners": interveners,
        "selected_event": selected_event,
    }


def generate_time_slots_for_event(
    db: Session,
    event: Event,
    intervener: Participant,
    *,
    duration_minutes: int,
    break_minutes: int,
    day_start: time,
    day_end: time,
    lunch_start: time,
    lunch_end: time,
) -> list[TimeSlot]:
    if duration_minutes > 1440 or break_minutes > 1440:
        raise HTTPException(
            status_code=400,
            detail="Duration and time between bookings cannot exceed 1440 minutes.",
        )
    if day_end <= day_start:
        raise HTTPException(status_code=400, detail="End time must be after start time.")
    if lunch_end <= lunch_start:
        raise HTTPException(status_code=400, detail="Lunch end must be after lunch start.")
    lunch_intersects = lunch_start < day_end and lunch_end > day_start
    lunch_is_inside_day = day_start <= lunch_start and lunch_end <= day_end
    if lunch_intersects and not lunch_is_inside_day:
        raise HTTPException(
            status_code=400,
            detail="The lunch break must be inside the working hours or completely outside them.",
        )

    work_periods = (
        [(day_start, lunch_start), (lunch_end, day_end)]
        if lunch_is_inside_day
        else [(day_start, day_end)]
    )
    generated_slots: list[TimeSlot] = []
    current_day = event.start_date
    while current_day <= event.end_date:
        for period_start, period_end in work_periods:
            cursor = datetime.combine(current_day, period_start)
            period_limit = datetime.combine(current_day, period_end)
            while cursor + timedelta(minutes=duration_minutes) <= period_limit:
                slot_end = cursor + timedelta(minutes=duration_minutes)
                generated_slots.append(
                    TimeSlot(
                        event_id=event.id,
                        intervener_id=intervener.id,
                        date=current_day,
                        start_time=cursor.time(),
                        end_time=slot_end.time(),
                        capacity=1,
                    )
                )
                cursor = slot_end + timedelta(minutes=break_minutes)
        if lunch_is_inside_day:
            generated_slots.append(
                TimeSlot(
                    event_id=event.id,
                    intervener_id=intervener.id,
                    date=current_day,
                    start_time=lunch_start,
                    end_time=lunch_end,
                    is_lunch_break=True,
                    capacity=1,
                )
            )
        current_day += timedelta(days=1)

    if not generated_slots:
        raise HTTPException(
            status_code=400,
            detail="These settings do not produce any booking slots.",
        )
    if len(generated_slots) > 10000:
        raise HTTPException(
            status_code=400,
            detail="The schedule would create more than 10000 slots.",
        )

    existing_slots = db.scalars(
        select(TimeSlot).where(
            TimeSlot.event_id == event.id,
            TimeSlot.intervener_id == intervener.id,
            TimeSlot.date >= event.start_date,
            TimeSlot.date <= event.end_date,
        )
    ).all()
    for new_slot in generated_slots:
        overlap = next(
            (
                existing
                for existing in existing_slots
                if existing.date == new_slot.date
                and existing.start_time < new_slot.end_time
                and existing.end_time > new_slot.start_time
            ),
            None,
        )
        if overlap is not None:
            raise HTTPException(
                status_code=409,
                detail=f"slot_overlap|{new_slot.date.isoformat()}|"
                f"{overlap.start_time.strftime('%H:%M')}|"
                f"{overlap.end_time.strftime('%H:%M')}",
            )
    return generated_slots


def slot_generation_error_message(error: HTTPException, language: str) -> str:
    detail = str(error.detail)
    if detail.startswith("slot_overlap|"):
        overlap_date, overlap_start, overlap_end = detail.removeprefix(
            "slot_overlap|"
        ).split("|")
        return translate(language, "slot_overlap").format(
            date=overlap_date,
            start=format_time(time.fromisoformat(overlap_start), language),
            end=format_time(time.fromisoformat(overlap_end), language),
        )
    translation_key = SLOT_GENERATION_ERRORS.get(detail)
    return translate(language, translation_key) if translation_key else detail


@router.get("/admin/slots/create", name="create_slots_page")
def create_slots_page(
    request: Request,
    event_id: int | None = None,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    context = slot_generation_context(db, event_id)
    if event_id and context["selected_event"] is None:
        raise HTTPException(status_code=404, detail="Event not found.")
    return render(request, "admin_slot_generator.html", context)


@router.post("/admin/slots/create", name="create_slots")
async def create_slots(
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    form = await request.form()
    context = slot_generation_context(db)
    try:
        event_id = form_int(form.get("event_id"), "event")
        intervener_id = form_int(form.get("intervener_id"), "intervener")
        duration_minutes = form_int(form.get("duration_minutes"), "booking duration")
        break_minutes = form_int(form.get("break_minutes"), "time between bookings", minimum=0)
        if duration_minutes > 1440 or break_minutes > 1440:
            raise HTTPException(status_code=400, detail="Duration and time between bookings cannot exceed 1440 minutes.")
        event = get_open_event_or_404(db, event_id)
        context = slot_generation_context(db, event.id)
        intervener = db.get(Participant, intervener_id)
        if intervener is None or not intervener.participant_type.is_intervener:
            raise HTTPException(status_code=400, detail="Choose a valid intervener.")
        if not participant_has_event(db, intervener.id, event.id):
            raise HTTPException(status_code=400, detail="Choose an intervener assigned to this event.")
        generated_slots = generate_time_slots_for_event(
            db,
            event,
            intervener,
            duration_minutes=duration_minutes,
            break_minutes=break_minutes,
            day_start=form_time(form.get("start_time"), "start time"),
            day_end=form_time(form.get("end_time"), "end time"),
            lunch_start=form_time(form.get("lunch_start"), "lunch start"),
            lunch_end=form_time(form.get("lunch_end"), "lunch end"),
        )
    except HTTPException as error:
        language = get_language(request.cookies.get("language"))
        error_message = slot_generation_error_message(error, language)
        context.update(
            {
                "form_values": dict(form),
                "error": error_message,
            }
        )
        return render(request, "admin_slot_generator.html", context, status_code=error.status_code)

    db.add_all(generated_slots)
    db.commit()
    booking_slot_count = sum(not slot.is_lunch_break for slot in generated_slots)
    return RedirectResponse(
        url=f"/admin/events/{event.id}/schedule?generated={booking_slot_count}",
        status_code=303,
    )


@router.get("/admin/events", name="admin_events")
def admin_events(request: Request, _admin: None = Depends(require_admin), db: Session = Depends(get_db)):
    events = db.scalars(
        select(Event).order_by(Event.is_closed, Event.start_date.desc())
    ).all()
    return render(request, "admin_events.html", {"events": events})


@router.post("/admin/events", name="create_event")
async def create_event(request: Request, _admin: None = Depends(require_admin), db: Session = Depends(get_db)):
    await check_csrf(request)
    form = await request.form()
    event = Event(
        title=str(form.get("title", "")).strip(),
        description=str(form.get("description", "")).strip() or None,
        start_date=form_date(form.get("start_date"), "start date"),
        end_date=form_date(form.get("end_date"), "end date"),
        max_slots_per_participant_per_day=form_int(form.get("daily_limit"), "daily limit"),
        max_pending_bookings_per_slot=form_int(form.get("max_pending_bookings_per_slot"), "pending booking limit"),
        allow_booking_same_intervener_per_day=form.get("allow_same_intervener") == "on",
    )
    if not event.title or event.end_date < event.start_date:
        raise HTTPException(status_code=400, detail="Provide a title and a valid date range.")
    db.add(event)
    db.commit()
    return RedirectResponse(url="/admin/events", status_code=303)


@router.get("/admin/events/{event_id}/edit", name="edit_event")
def edit_event(event_id: int, request: Request, _admin: None = Depends(require_admin), db: Session = Depends(get_db)):
    event = get_event_or_404(db, event_id)
    return render(request, "admin_event_edit.html", {"event": event})


@router.post("/admin/events/{event_id}/edit", name="update_event")
async def update_event(
    event_id: int,
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    form = await request.form()
    event = get_event_or_404(db, event_id)
    event.title = str(form.get("title", "")).strip()
    event.description = str(form.get("description", "")).strip() or None
    event.start_date = form_date(form.get("start_date"), "start date")
    event.end_date = form_date(form.get("end_date"), "end date")
    event.max_slots_per_participant_per_day = form_int(form.get("daily_limit"), "daily limit")
    event.max_pending_bookings_per_slot = form_int(form.get("max_pending_bookings_per_slot"), "pending booking limit")
    event.allow_booking_same_intervener_per_day = form.get("allow_same_intervener") == "on"
    if not event.title or event.end_date < event.start_date:
        raise HTTPException(status_code=400, detail="Provide a title and a valid date range.")
    db.commit()
    return RedirectResponse(url="/admin/events", status_code=303)


@router.post("/admin/events/{event_id}/delete", name="delete_event")
async def delete_event(
    event_id: int,
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    event = get_event_or_404(db, event_id)
    db.delete(event)
    db.commit()
    return RedirectResponse(url="/admin/events", status_code=303)


@router.post("/admin/events/{event_id}/toggle-closed", name="toggle_event_closed")
async def toggle_event_closed(
    event_id: int,
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    event = get_event_or_404(db, event_id)
    event.is_closed = not event.is_closed
    db.commit()
    return RedirectResponse(url="/admin/events", status_code=303)


@router.get(
    "/admin/events/{event_id}/participants",
    name="admin_event_participants",
)
def admin_event_participants(
    event_id: int,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    event = get_open_event_or_404(db, event_id)
    return RedirectResponse(
        url=f"/admin/events/{event.id}/schedule?manage_participants=1",
        status_code=303,
    )


@router.post(
    "/admin/events/{event_id}/participants",
    name="update_admin_event_participants",
)
async def update_admin_event_participants(
    event_id: int,
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    event = get_open_event_or_404(db, event_id)
    form = await request.form()
    language = get_language(request.cookies.get("language"))
    schedule_return_params = {"manage_participants": "1"}
    selected_date_value = str(form.get("selected_date", "")).strip()
    if selected_date_value:
        schedule_return_params["selected_date"] = selected_date_value
    intervener_value = str(form.get("intervener_id", "")).strip()
    if intervener_value.isdigit():
        schedule_return_params["intervener_id"] = intervener_value

    def schedule_return_url(**params: str) -> str:
        return (
            f"/admin/events/{event.id}/schedule?"
            f"{urlencode(schedule_return_params | params)}"
        )

    raw_ids = form.getlist("participant_ids")
    try:
        participant_ids = [int(value) for value in raw_ids]
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=translate(language, "invalid_participant_selection"),
        ) from None
    if any(participant_id < 1 for participant_id in participant_ids):
        raise HTTPException(
            status_code=400,
            detail=translate(language, "invalid_participant_selection"),
        )
    if len(participant_ids) != len(set(participant_ids)):
        raise HTTPException(
            status_code=400,
            detail=translate(language, "duplicate_participant_selection"),
        )

    participants = (
        db.scalars(
            select(Participant)
            .where(Participant.id.in_(participant_ids))
            .options(joinedload(Participant.participant_type))
        ).unique().all()
        if participant_ids
        else []
    )
    if len(participants) != len(participant_ids):
        raise HTTPException(
            status_code=400,
            detail=translate(language, "selected_participant_not_found"),
        )

    current_ids = set(
        db.scalars(
            select(event_participants.c.participant_id).where(
                event_participants.c.event_id == event.id
            )
        ).all()
    )
    removed_ids = current_ids - set(participant_ids)
    if removed_ids:
        assigned_participants = db.scalars(
            select(Participant)
            .where(Participant.id.in_(removed_ids))
            .options(joinedload(Participant.participant_type))
        ).unique().all()
        for participant in assigned_participants:
            if participant.participant_type.is_intervener:
                has_slots = db.scalar(
                    select(TimeSlot.id)
                    .where(
                        TimeSlot.event_id == event.id,
                        TimeSlot.intervener_id == participant.id,
                    )
                    .limit(1)
                )
                if has_slots is not None:
                    return RedirectResponse(
                        url=schedule_return_url(
                            assignment_error="intervener_slots"
                        ),
                        status_code=303,
                    )
            has_bookings = db.scalar(
                select(Booking.id)
                .join(TimeSlot)
                .where(
                    TimeSlot.event_id == event.id,
                    Booking.participant_id == participant.id,
                )
                .limit(1)
            )
            if has_bookings is not None:
                return RedirectResponse(
                    url=schedule_return_url(
                        assignment_error="participant_bookings"
                    ),
                    status_code=303,
                )

    event.participants = participants
    db.commit()
    if intervener_value.isdigit() and int(intervener_value) not in set(participant_ids):
        schedule_return_params.pop("intervener_id", None)
    return RedirectResponse(
        url=schedule_return_url(updated="1"),
        status_code=303,
    )


@router.get("/admin/locations", name="admin_locations")
def admin_locations(
    request: Request,
    event_id: int | None = None,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    location_query = (
        select(Location)
        .join(Event)
        .where(Event.is_closed.is_(False))
        .options(joinedload(Location.event))
    )
    if event_id is not None:
        location_query = location_query.where(Location.event_id == event_id)
    locations = db.scalars(location_query.order_by(Location.name)).unique().all()
    events = db.scalars(
        select(Event)
        .where(Event.is_closed.is_(False))
        .order_by(Event.title)
    ).all()
    return render(
        request,
        "admin_locations.html",
        {
            "locations": locations,
            "events": events,
            "selected_event_id": event_id,
        },
    )


def admin_locations_url(event_id: str) -> str:
    event_id = event_id.strip()
    if not event_id.isdigit():
        return "/admin/locations"
    return f"/admin/locations?{urlencode({'event_id': event_id})}"


@router.post("/admin/locations", name="create_location")
async def create_location(request: Request, _admin: None = Depends(require_admin), db: Session = Depends(get_db)):
    await check_csrf(request)
    form = await request.form()
    event_id = form_int(form.get("event_id"), "event")
    get_open_event_or_404(db, event_id)
    location = Location(
        name=str(form.get("name", "")).strip(),
        event_id=event_id,
        floor_plan_url=str(form.get("floor_plan_url", "")).strip() or None,
    )
    if not location.name:
        raise HTTPException(status_code=400, detail="Location name is required.")
    db.add(location)
    db.flush()
    location.photos = [
        LocationPhoto(photo_url=url.strip())
        for url in str(form.get("photo_urls", "")).splitlines()
        if url.strip()
    ]
    db.commit()
    return RedirectResponse(
        url=admin_locations_url(str(form.get("return_event_id", ""))),
        status_code=303,
    )


@router.post("/admin/locations/{location_id}/edit", name="update_location")
async def update_location(
    location_id: int,
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    form = await request.form()
    location = db.get(Location, location_id)
    if location is None:
        raise HTTPException(status_code=404, detail="Location not found.")
    get_open_event_or_404(db, location.event_id)
    event_id = form_int(form.get("event_id"), "event")
    get_open_event_or_404(db, event_id)
    name = str(form.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="Location name is required.")
    location.name = name
    location.event_id = event_id
    location.floor_plan_url = str(form.get("floor_plan_url", "")).strip() or None
    location.photos = [
        LocationPhoto(photo_url=url.strip())
        for url in str(form.get("photo_urls", "")).splitlines()
        if url.strip()
    ]
    db.commit()
    return RedirectResponse(
        url=admin_locations_url(str(form.get("return_event_id", ""))),
        status_code=303,
    )


@router.post("/admin/locations/{location_id}/delete", name="delete_location")
async def delete_location(
    location_id: int,
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    location = db.get(Location, location_id)
    if location is None:
        raise HTTPException(status_code=404, detail="Location not found.")
    get_open_event_or_404(db, location.event_id)
    if location.bookings:
        raise HTTPException(status_code=409, detail="A location with bookings cannot be deleted.")
    db.delete(location)
    db.commit()
    return RedirectResponse(
        url=admin_locations_url(str(form.get("return_event_id", ""))),
        status_code=303,
    )


@router.get("/admin/participant-types", name="admin_participant_types")
def admin_participant_types(
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    types = db.scalars(select(ParticipantType).order_by(ParticipantType.name)).all()
    return render(request, "admin_participant_types.html", {"types": types})


@router.post("/admin/participant-types", name="create_participant_type")
async def create_participant_type(
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="Participant type name is required.")
    db.add(
        ParticipantType(
            name=name,
            is_intervener=form.get("is_intervener") == "on",
            emoji=str(form.get("emoji", "")).strip() or None,
            discord_role_id=str(form.get("discord_role_id", "")).strip() or None,
        )
    )
    db.commit()
    return RedirectResponse(url="/admin/participant-types", status_code=303)


@router.post("/admin/participant-types/{type_id}/edit", name="update_participant_type")
async def update_participant_type(
    type_id: int,
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    form = await request.form()
    participant_type = db.get(ParticipantType, type_id)
    if participant_type is None:
        raise HTTPException(status_code=404, detail="Participant type not found.")
    name = str(form.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="Participant type name is required.")
    participant_type.name = name
    participant_type.emoji = str(form.get("emoji", "")).strip() or None
    participant_type.is_intervener = form.get("is_intervener") == "on"
    participant_type.discord_role_id = str(form.get("discord_role_id", "")).strip() or None
    db.commit()
    return RedirectResponse(url="/admin/participant-types", status_code=303)


@router.post("/admin/participant-types/{type_id}/delete", name="delete_participant_type")
async def delete_participant_type(
    type_id: int,
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    participant_type = db.get(ParticipantType, type_id)
    if participant_type is None:
        raise HTTPException(status_code=404, detail="Participant type not found.")
    if participant_type.participants:
        raise HTTPException(status_code=409, detail="A participant type in use cannot be deleted.")
    db.delete(participant_type)
    db.commit()
    return RedirectResponse(url="/admin/participant-types", status_code=303)


@router.get("/admin/participants", name="admin_participants")
def admin_participants(request: Request, _admin: None = Depends(require_admin), db: Session = Depends(get_db)):
    participants = db.scalars(
        select(Participant)
        .options(joinedload(Participant.participant_type))
        .order_by(func.lower(Participant.name), Participant.name, Participant.id)
    ).unique().all()
    types = db.scalars(select(ParticipantType).order_by(ParticipantType.name)).all()
    events = db.scalars(
        select(Event)
        .where(Event.is_closed.is_(False))
        .order_by(Event.start_date, Event.title, Event.id)
    ).all()
    participant_form_message = {
        "email_in_use": "participant_email_already_in_use",
    }.get(request.query_params.get("participant_error", ""))
    return render(
        request,
        "admin_participants.html",
        {
            "participants": participants,
            "types": types,
            "events": events,
            "participant_form_message": participant_form_message,
        },
    )


@router.post("/admin/participants", name="create_participant")
async def create_participant(
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    form = await request.form()
    type_id = form_int(form.get("type_id"), "participant type")
    if db.get(ParticipantType, type_id) is None:
        raise HTTPException(status_code=404, detail="Participant type not found.")
    event_value = str(form.get("event_id", "")).strip()
    event = (
        get_open_event_or_404(db, form_int(event_value, "event"))
        if event_value
        else None
    )
    name = str(form.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=400, detail="Participant name is required.")
    email = str(form.get("email", "")).strip() or None
    if participant_email_in_use(db, email):
        return RedirectResponse(
            url="/admin/participants?participant_error=email_in_use",
            status_code=303,
        )
    participant = Participant(
        name=name,
        type_id=type_id,
        bio=str(form.get("bio", "")).strip() or None,
        email=email,
        discord_handle=str(form.get("discord_handle", "")).strip() or None,
        social_url=str(form.get("social_url", "")).strip() or None,
        avatar_url=str(form.get("avatar_url", "")).strip() or None,
    )
    if event:
        participant.events.append(event)
    participant.photos = [
        ParticipantPhoto(photo_url=url)
        for url in participant_photo_urls(
            form.get("photo_urls"),
            get_language(request.cookies.get("language")),
        )
    ]
    db.add(participant)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if participant_email_in_use(db, email):
            return RedirectResponse(
                url="/admin/participants?participant_error=email_in_use",
                status_code=303,
            )
        raise
    return RedirectResponse(url="/admin/participants", status_code=303)


@router.get("/admin/participants/{participant_id}/edit", name="edit_participant")
def edit_participant(
    participant_id: int,
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    participant = db.get(Participant, participant_id)
    if participant is None:
        raise HTTPException(status_code=404, detail="Participant not found.")
    types = db.scalars(select(ParticipantType).order_by(ParticipantType.name)).all()
    participant_form_message = {
        "email_in_use": "participant_email_already_in_use",
    }.get(request.query_params.get("participant_error", ""))
    return render(
        request,
        "admin_participant_edit.html",
        {
            "participant": participant,
            "types": types,
            "participant_form_message": participant_form_message,
        },
    )


@router.post("/admin/participants/{participant_id}/edit", name="update_participant")
async def update_participant(
    participant_id: int,
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    form = await request.form()
    participant = db.get(Participant, participant_id)
    if participant is None:
        raise HTTPException(status_code=404, detail="Participant not found.")
    type_id = form_int(form.get("type_id"), "participant type")
    if db.get(ParticipantType, type_id) is None:
        raise HTTPException(status_code=404, detail="Participant type not found.")
    email = str(form.get("email", "")).strip() or None
    if participant_email_in_use(db, email, exclude_id=participant.id):
        return RedirectResponse(
            url=f"/admin/participants/{participant.id}/edit?participant_error=email_in_use",
            status_code=303,
        )
    photos = participant_photo_urls(
        form.get("photo_urls"),
        get_language(request.cookies.get("language")),
    )
    participant.name = str(form.get("name", "")).strip()
    participant.type_id = type_id
    participant.bio = str(form.get("bio", "")).strip() or None
    participant.email = email
    participant.discord_handle = str(form.get("discord_handle", "")).strip() or None
    participant.social_url = str(form.get("social_url", "")).strip() or None
    participant.avatar_url = str(form.get("avatar_url", "")).strip() or None
    participant.photos = [ParticipantPhoto(photo_url=url) for url in photos]
    if not participant.name:
        raise HTTPException(status_code=400, detail="Participant name is required.")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if participant_email_in_use(db, email, exclude_id=participant_id):
            return RedirectResponse(
                url=f"/admin/participants/{participant_id}/edit?participant_error=email_in_use",
                status_code=303,
            )
        raise
    return RedirectResponse(url="/admin/participants", status_code=303)


@router.post("/admin/participants/{participant_id}/delete", name="delete_participant")
async def delete_participant(
    participant_id: int,
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    participant = db.get(Participant, participant_id)
    if participant is None:
        raise HTTPException(status_code=404, detail="Participant not found.")
    if participant.bookings or participant.time_slots:
        raise HTTPException(status_code=409, detail="A participant with bookings or time slots cannot be deleted.")
    db.delete(participant)
    db.commit()
    return RedirectResponse(url="/admin/participants", status_code=303)


@router.get("/admin/events/{event_id}/schedule", name="admin_schedule")
def admin_schedule(
    event_id: int,
    request: Request,
    selected_date: date | None = None,
    intervener_id: str | None = None,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    event = get_open_event_or_404(db, event_id)
    if selected_date is None:
        selected_date = min(max(date.today(), event.start_date), event.end_date)
    if selected_date < event.start_date or selected_date > event.end_date:
        raise HTTPException(status_code=404, detail="Date is outside this event.")
    selected_intervener_id = None
    intervener_value = (intervener_id or "").strip()
    if intervener_value:
        try:
            selected_intervener_id = int(intervener_value)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Intervener ID must be an integer.") from exc
    if selected_intervener_id is not None:
        intervener = db.get(Participant, selected_intervener_id)
        if (
            intervener is None
            or not intervener.participant_type.is_intervener
            or not participant_has_event(db, intervener.id, event.id)
        ):
            raise HTTPException(status_code=404, detail="Intervener not found.")
    interveners = db.scalars(
        select(Participant)
        .join(event_participants, event_participants.c.participant_id == Participant.id)
        .join(ParticipantType)
        .where(
            event_participants.c.event_id == event.id,
            ParticipantType.is_intervener.is_(True),
        )
        .order_by(Participant.name)
    ).unique().all()
    participants = db.scalars(
        select(Participant)
        .join(ParticipantType)
        .options(joinedload(Participant.participant_type))
        .order_by(ParticipantType.name, Participant.name)
    ).unique().all()
    assigned_ids = set(
        db.scalars(
            select(event_participants.c.participant_id).where(
                event_participants.c.event_id == event.id
            )
        ).all()
    )
    assignment_error_key = {
        "intervener_slots": "remove_intervener_slots_first",
        "participant_bookings": "cannot_unlink_booked_participant",
    }.get(request.query_params.get("assignment_error", ""))
    matrix = build_schedule_matrix(
        request=request,
        db=db,
        event=event,
        selected_date=selected_date,
        matrix_id="admin-schedule-container",
        include_all_interveners=True,
        filter_intervener_id=selected_intervener_id,
    )
    context = matrix | {
        "event": event,
        "interveners": interveners,
        "selected_date": selected_date,
        "selected_intervener_id": selected_intervener_id,
        "matrix": matrix,
        "generated": request.query_params.get("generated"),
        "participants": participants,
        "assigned_ids": assigned_ids,
        "manage_participants": request.query_params.get("manage_participants") == "1",
        "participants_updated": request.query_params.get("updated") == "1",
        "assignment_error": (
            translate(
                get_language(request.cookies.get("language")),
                assignment_error_key,
            )
            if assignment_error_key
            else None
        ),
    }
    return render(request, "admin_schedule.html", context)


@router.post("/admin/events/{event_id}/schedule", name="create_time_slot")
async def create_time_slot(
    event_id: int,
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    form = await request.form()
    event = get_open_event_or_404(db, event_id)
    intervener_id = form_int(form.get("intervener_id"), "intervener")
    intervener = db.get(Participant, intervener_id)
    if intervener is None or not intervener.participant_type.is_intervener:
        raise HTTPException(status_code=400, detail="Choose a valid intervener.")
    if not participant_has_event(db, intervener.id, event.id):
        raise HTTPException(status_code=400, detail="Choose an intervener assigned to this event.")
    slot_date = form_date(form.get("date"), "date")
    start_time = form_time(form.get("start_time"), "start time")
    end_time = form_time(form.get("end_time"), "end time")
    if slot_date < event.start_date or slot_date > event.end_date or end_time <= start_time:
        raise HTTPException(status_code=400, detail="The time slot must fit within the event dates and have a valid duration.")
    db.add(
        TimeSlot(
            event_id=event.id,
            intervener_id=intervener.id,
            date=slot_date,
            start_time=start_time,
            end_time=end_time,
            capacity=form_int(form.get("capacity", 1), "capacity"),
            is_lunch_break=form.get("is_lunch_break") == "on",
        )
    )
    db.commit()
    selected_intervener_id = str(form.get("selected_intervener_id", "")).strip()
    filter_query = (
        f"&intervener_id={intervener.id}"
        if selected_intervener_id == str(intervener.id)
        else ""
    )
    return RedirectResponse(
        url=f"/admin/events/{event_id}/schedule?selected_date={slot_date.isoformat()}{filter_query}",
        status_code=303,
    )


@router.post("/admin/time-slots/{slot_id}", name="update_time_slot")
async def update_time_slot(
    slot_id: int,
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    form = await request.form()
    slot = get_slot_or_404(db, slot_id)
    slot_date = form_date(form.get("date"), "date")
    start_time = form_time(form.get("start_time"), "start time")
    end_time = form_time(form.get("end_time"), "end time")
    if slot_date < slot.event.start_date or slot_date > slot.event.end_date or end_time <= start_time:
        raise HTTPException(status_code=400, detail="The time slot must fit within the event dates and have a valid duration.")
    selected_intervener_value = str(form.get("selected_intervener_id", "")).strip()
    selected_intervener_id = (
        form_int(selected_intervener_value, "intervener")
        if selected_intervener_value
        else None
    )
    if selected_intervener_id is not None:
        selected_intervener = db.get(Participant, selected_intervener_id)
        if selected_intervener is None or not selected_intervener.participant_type.is_intervener:
            raise HTTPException(status_code=400, detail="Choose a valid intervener filter.")
    slot.date = slot_date
    slot.start_time = start_time
    slot.end_time = end_time
    slot.capacity = form_int(form.get("capacity"), "capacity")
    slot.is_lunch_break = form.get("is_lunch_break") == "on"
    db.commit()
    if request.headers.get("HX-Request") == "true":
        refreshed = db.scalars(
            select(TimeSlot)
            .where(TimeSlot.id == slot_id)
            .options(joinedload(TimeSlot.intervener), joinedload(TimeSlot.bookings).joinedload(Booking.participant))
        ).unique().one()
        response = render(
            request,
            "partials/admin_slot_row.html",
            {"slot": refreshed},
        )
        response.headers["HX-Refresh"] = "true"
        return response
    redirect_url = (
        f"/admin/events/{slot.event_id}/schedule?selected_date={slot_date.isoformat()}"
    )
    if selected_intervener_id is not None:
        redirect_url += f"&intervener_id={selected_intervener_id}"
    return RedirectResponse(url=redirect_url, status_code=303)


@router.post("/interveners/time-slots/{slot_id}", name="intervener_update_time_slot")
async def intervener_update_time_slot(
    slot_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    form = await request.form()
    participant = await require_participant_access(request, db, intervener=True)
    slot = get_slot_or_404(db, slot_id)
    if slot.intervener_id != participant.id:
        raise HTTPException(status_code=403, detail="This time slot does not belong to this intervener.")
    require_event_membership(request, db, participant, slot.event_id)

    slot_date = form_date(form.get("date"), "date")
    start_time = form_time(form.get("start_time"), "start time")
    end_time = form_time(form.get("end_time"), "end time")
    if (
        slot_date < slot.event.start_date
        or slot_date > slot.event.end_date
        or end_time <= start_time
    ):
        raise HTTPException(
            status_code=400,
            detail="The time slot must fit within the event dates and have a valid duration.",
        )

    slot.date = slot_date
    slot.start_time = start_time
    slot.end_time = end_time
    slot.capacity = form_int(form.get("capacity"), "capacity")
    slot.is_lunch_break = form.get("is_lunch_break") == "on"
    db.commit()
    schedule_scope = str(form.get("schedule_scope", "mine"))
    if schedule_scope not in {"mine", "all"}:
        schedule_scope = "mine"
    return RedirectResponse(
        url="/participant?"
        + urlencode(
            {
                "participant_id": str(participant.id),
                "event_id": str(slot.event_id),
                "selected_date": slot_date.isoformat(),
                "schedule_scope": schedule_scope,
            }
        ),
        status_code=303,
    )


@router.post("/admin/time-slots/{slot_id}/delete", name="delete_time_slot")
async def delete_time_slot(
    slot_id: int,
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    form = await request.form()
    slot = get_slot_or_404(db, slot_id)
    event_id = slot.event_id
    event = slot.event
    slot_date_value = str(form.get("selected_date", "")).strip()
    slot_date = form_date(slot_date_value, "date") if slot_date_value else slot.date
    if slot_date < event.start_date or slot_date > event.end_date:
        raise HTTPException(status_code=400, detail="Date is outside this event.")
    selected_intervener_value = str(form.get("selected_intervener_id", "")).strip()
    if selected_intervener_value:
        selected_intervener_id = form_int(selected_intervener_value, "intervener")
        selected_intervener = db.get(Participant, selected_intervener_id)
        if selected_intervener is None or not selected_intervener.participant_type.is_intervener:
            raise HTTPException(status_code=400, detail="Choose a valid intervener filter.")
        filter_query = f"&intervener_id={selected_intervener_id}"
    else:
        filter_query = ""
    if slot.bookings:
        raise HTTPException(status_code=409, detail="A time slot with bookings cannot be deleted.")
    db.delete(slot)
    db.commit()
    return RedirectResponse(
        url=f"/admin/events/{event_id}/schedule?selected_date={slot_date.isoformat()}{filter_query}",
        status_code=303,
    )


@router.get("/admin/events/{event_id}/print", name="print_schedule")
def print_schedule(
    event_id: int,
    request: Request,
    sort_by: str = "intervener",
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if sort_by not in {"intervener", "participant"}:
        raise HTTPException(status_code=422, detail="Invalid print schedule sort order.")
    event = get_open_event_or_404(db, event_id)
    slots = db.scalars(
        select(TimeSlot)
        .where(TimeSlot.event_id == event_id)
        .options(
            joinedload(TimeSlot.intervener),
            joinedload(TimeSlot.bookings).joinedload(Booking.participant),
            joinedload(TimeSlot.bookings).joinedload(Booking.location),
        )
        .order_by(TimeSlot.date, TimeSlot.start_time)
    ).unique().all()
    days = []
    current_day = event.start_date
    while current_day <= event.end_date:
        day_slots = [slot for slot in slots if slot.date == current_day]
        rows = [
            (slot, booking)
            for slot in day_slots
            if not slot.is_lunch_break
            for booking in (
                [booking for booking in slot.bookings if booking.status == BookingStatus.APPROVED]
                or [None]
            )
        ]
        rows.extend(
            (slot, None)
            for slot in day_slots
            if slot.is_lunch_break
        )
        if sort_by == "participant":
            rows.sort(
                key=lambda row: (
                    row[1] is None,
                    row[1].participant.name.casefold() if row[1] else "",
                    row[0].start_time,
                    row[0].intervener.name.casefold(),
                )
            )
        else:
            rows.sort(
                key=lambda row: (
                    row[0].intervener.name.casefold(),
                    row[0].start_time,
                    row[1].participant.name.casefold() if row[1] else "",
                )
            )
        days.append((current_day, rows))
        current_day = date.fromordinal(current_day.toordinal() + 1)
    return render(
        request,
        "print_schedule.html",
        {"event": event, "days": days, "sort_by": sort_by},
    )


@router.get("/admin/bookings", name="admin_bookings")
def admin_bookings(request: Request, _admin: None = Depends(require_admin), db: Session = Depends(get_db)):
    bookings = db.scalars(
        select(Booking)
        .join(TimeSlot)
        .join(Event)
        .where(Event.is_closed.is_(False))
        .options(
            joinedload(Booking.participant),
            joinedload(Booking.participant).joinedload(Participant.photos),
            joinedload(Booking.location),
            joinedload(Booking.time_slot).joinedload(TimeSlot.event),
            joinedload(Booking.time_slot).joinedload(TimeSlot.intervener),
        )
        .order_by(Booking.created_at.desc())
    ).unique().all()
    return render(request, "admin_bookings.html", {"bookings": bookings})


@router.post("/admin/bookings/{booking_id}/status", name="admin_update_booking")
async def admin_update_booking(
    booking_id: int,
    request: Request,
    _admin: None = Depends(require_admin),
    db: Session = Depends(get_db),
):
    await check_csrf(request)
    form = await request.form()
    booking = db.get(Booking, booking_id)
    if booking is None:
        raise HTTPException(status_code=404, detail="Booking not found.")
    get_open_event_or_404(db, booking.time_slot.event_id)
    try:
        status = BookingStatus(str(form.get("status", "")))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid booking status.") from None
    if status == BookingStatus.APPROVED:
        approved_count = db.scalar(
            select(func.count(Booking.id)).where(
                Booking.time_slot_id == booking.time_slot_id,
                Booking.status == BookingStatus.APPROVED,
                Booking.id != booking.id,
            )
        ) or 0
        if approved_count >= 1:
            raise HTTPException(status_code=409, detail="This time slot already has an approved booking.")
    booking.status = status
    db.commit()
    return RedirectResponse(url="/admin/bookings", status_code=303)
