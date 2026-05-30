"""Tiny i18n stub. Wrap user-facing strings in t() so we can wire translations later."""
from .settings import get_settings


_TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {},
    "ru": {},
}


def t(key: str, lang: str | None = None) -> str:
    lang = lang or get_settings().language
    return _TRANSLATIONS.get(lang, {}).get(key, key)


def plural(count: int, singular: str, plural: str | None = None) -> str:
    """English count + noun with regular -s plural (or an explicit override
    for irregulars). Use instead of `(s)` suffixes — readers scan the
    correct form faster and exports look less like compiler output.
    """
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"
