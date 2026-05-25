"""Tiny i18n stub. Wrap user-facing strings in t() so we can wire translations later."""
from .settings import get_settings


_TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {},
    "ru": {},
}


def t(key: str, lang: str | None = None) -> str:
    lang = lang or get_settings().language
    return _TRANSLATIONS.get(lang, {}).get(key, key)
