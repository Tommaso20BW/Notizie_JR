"""Traduzione in italiano dei post X scritti in un'altra lingua.

- Con il secret DEEPL_API_KEY usa DeepL (qualità migliore; il piano gratuito basta per
  i post del bot). Le chiavi del piano gratuito finiscono con ":fx".
- Senza chiave, o se DeepL non risponde, usa Google Traduttore.
- La lingua la riconosce il servizio stesso: se il post è già in italiano non cambia nulla.
- Qualsiasi errore lascia il post in lingua originale: la notizia parte comunque.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

import requests

TIMEOUT_SECONDS = 10
MIN_LETTERS = 12          # testi brevissimi (solo emoji, "FT", un nome) restano come sono
GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
LANGUAGE_NAMES = {
    "en": "inglese", "de": "tedesco", "fr": "francese", "es": "spagnolo",
    "pt": "portoghese", "nl": "olandese", "tr": "turco", "ar": "arabo",
    "pl": "polacco", "sr": "serbo", "hr": "croato", "da": "danese",
    "sv": "svedese", "no": "norvegese", "nb": "norvegese",
}


@dataclass(frozen=True)
class Translation:
    text: str
    language: str          # codice della lingua originale (es. "en")

    @property
    def language_name(self) -> str:
        return LANGUAGE_NAMES.get(self.language, "lingua originale")


def _letters(text: str) -> int:
    return sum(ch.isalpha() for ch in text)


def _deepl(text: str, key: str) -> Translation | None:
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
    response = requests.post(
        f"https://{host}/v2/translate",
        headers={"Authorization": f"DeepL-Auth-Key {key}"},
        data={"text": text, "target_lang": "IT", "preserve_formatting": "1"},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    item = (response.json().get("translations") or [{}])[0]
    language = str(item.get("detected_source_language") or "").lower()
    translated = str(item.get("text") or "").strip()
    return Translation(translated, language) if translated and language else None


def _google(text: str) -> Translation | None:
    response = requests.get(
        GOOGLE_URL,
        params={"client": "gtx", "sl": "auto", "tl": "it", "dt": "t", "q": text},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()
    translated = "".join(part[0] for part in (data[0] or ()) if part and part[0]).strip()
    language = str(data[2] or "").lower() if len(data) > 2 else ""
    return Translation(translated, language) if translated and language else None


@lru_cache(maxsize=256)
def translate_to_italian(text: str) -> Translation | None:
    """Traduzione in italiano, oppure None se il testo è già italiano o qualcosa va storto."""
    text = (text or "").strip()
    if _letters(text) < MIN_LETTERS:
        return None
    key = os.environ.get("DEEPL_API_KEY", "").strip()
    providers = ([lambda t: _deepl(t, key)] if key else []) + [_google]
    for provider in providers:
        try:
            result = provider(text)
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as error:
            print(f"[TRADUZIONE] servizio non disponibile: {type(error).__name__}")
            continue
        if result is None:
            continue
        if result.language.split("-")[0] == "it" or result.text == text:
            return None
        return Translation(result.text, result.language.split("-")[0])
    return None
