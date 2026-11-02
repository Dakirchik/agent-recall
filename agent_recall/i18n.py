"""Interface language: English by default, Russian built in.

Choose with `language = "ru"` / `"en"` in config.toml or the AGENT_RECALL_LANG
environment variable; otherwise the OS UI language decides. Strings in the code are
English and wrapped in `_()`; translations live in `locales.py` keyed by the English text.
A test checks that every `_()` string has a translation.
"""
from __future__ import annotations

import locale
import os
import sys

SUPPORTED = ("en", "ru")
_lang: str | None = None


def _system_lang() -> str:
    if sys.platform == "win32":
        try:
            import ctypes
            langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "ru" if (langid & 0x3FF) == 0x19 else "en"  # 0x19 = LANG_RUSSIAN
        except Exception:
            pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(var, "")
        if v:
            return "ru" if v.lower().startswith("ru") else "en"
    try:
        loc = locale.getlocale()[0] or ""
    except Exception:
        loc = ""
    return "ru" if loc.lower().startswith("ru") else "en"


def _norm(value: str | None) -> str | None:
    if not value:
        return None
    v = str(value).strip().lower()[:2]
    return v if v in SUPPORTED else None


def set_lang(value: str | None = None, config_value: str | None = None) -> str:
    """Priority: explicit value > AGENT_RECALL_LANG > config > OS language."""
    global _lang
    _lang = (_norm(value) or _norm(os.environ.get("AGENT_RECALL_LANG")) or _norm(config_value)
             or _system_lang())
    return _lang


def lang() -> str:
    return _lang or set_lang()


def _(text: str, **kw) -> str:
    if lang() == "ru":
        from .locales import RU
        text = RU.get(text, text)
    return text.format(**kw) if kw else text


def T(en: str, ru: str) -> str:
    """Inline bilingual text (used by the full-screen UI, where strings are short and many)."""
    return ru if lang() == "ru" else en
