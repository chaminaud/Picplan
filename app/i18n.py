import json
from datetime import time
from functools import lru_cache
from pathlib import Path

from app.settings import settings

TRANSLATIONS_DIR = Path(__file__).parent / "translations"
SUPPORTED_LANGUAGES = ("fr", "en")


@lru_cache(maxsize=len(SUPPORTED_LANGUAGES))
def load_translations(language: str) -> dict[str, str]:
    path = TRANSLATIONS_DIR / f"{language}.json"
    with path.open(encoding="utf-8") as translation_file:
        return json.load(translation_file)


def get_language(language: str | None) -> str:
    candidate = (language or settings.default_language).lower().split("-")[0]
    return candidate if candidate in SUPPORTED_LANGUAGES else settings.default_language


def translate(language: str, key: str) -> str:
    translations = load_translations(language)
    return translations.get(key, key)


def format_time(value: time, language: str) -> str:
    if language == "fr":
        return f"{value.hour:02d}h{value.minute:02d}"
    return value.strftime("%H:%M")
