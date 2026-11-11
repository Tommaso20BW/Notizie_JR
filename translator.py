"""Traduzione in italiano dei post X scritti in un'altra lingua.

- Con il secret DEEPL_API_KEY usa DeepL (qualità migliore; il piano gratuito basta per
  i post del bot). Le chiavi del piano gratuito finiscono con ":fx".
- Senza chiave, o se DeepL non risponde, usa Google Traduttore (due indirizzi diversi, ognuno
  con un secondo tentativo: da GitHub Google a volte risponde con errori temporanei).
- La lingua la riconosce il servizio stesso: se il post è già in italiano non cambia nulla.
- Qualsiasi errore lascia il post in lingua originale: la notizia parte comunque.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from functools import lru_cache

import requests

TIMEOUT_SECONDS = 10
MIN_LETTERS = 12          # testi brevissimi (solo emoji, "FT", un nome) restano come sono
GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
GOOGLE_ALT_URL = "https://clients5.google.com/translate_a/t"
ATTEMPTS = 2              # tentativi per ogni servizio
RETRY_SECONDS = 2
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


def _google_alt(text: str) -> Translation | None:
    """Secondo indirizzo di Google: risponde [[testo, lingua]]."""
    response = requests.get(
        GOOGLE_ALT_URL,
        params={"client": "dict-chrome-ex", "sl": "auto", "tl": "it", "q": text},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    item = response.json()[0]
    translated, language = (item if isinstance(item, list) else [item, ""])[:2]
    translated, language = str(translated or "").strip(), str(language or "").lower()
    return Translation(translated, language) if translated and language else None


def _describe(error: Exception) -> str:
    status = getattr(getattr(error, "response", None), "status_code", None)
    return f"{type(error).__name__}{f' {status}' if status else ''}"


@lru_cache(maxsize=256)
def translate_to_italian(text: str) -> Translation | None:
    """Traduzione in italiano, oppure None se il testo è già italiano o qualcosa va storto."""
    text = (text or "").strip()
    if _letters(text) < MIN_LETTERS:
        return None
    key = os.environ.get("DEEPL_API_KEY", "").strip()
    providers = ([("DeepL", lambda t: _deepl(t, key))] if key else []) + [
        ("Google", _google), ("Google 2", _google_alt)]
    for name, provider in providers:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                result = provider(text)
                break
            except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as error:
                print(f"[TRADUZIONE] {name} non disponibile (tentativo {attempt}): {_describe(error)}", flush=True)
                result = None
                if attempt < ATTEMPTS:
                    time.sleep(RETRY_SECONDS)
        if result is None:
            continue
        language = result.language.split("-")[0]
        if language == "it" or result.text == text:
            return None
        print(f"[TRADUZIONE] tradotto dall'{language} con {name}", flush=True)
        return Translation(result.text, language)
    print("[TRADUZIONE] nessun servizio ha tradotto: post in lingua originale", flush=True)
    return None
