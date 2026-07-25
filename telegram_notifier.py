"""Formattazione e consegna affidabile delle notizie a Telegram."""

from __future__ import annotations

import time
from collections.abc import Callable
from html import escape
from typing import Protocol

import requests

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

SOURCE_EMOJIS = (
    ("Sky Sport - Calciomercato", "6033058586945392520", "📰"),
    ("La Gazzetta dello Sport", "6032862491623559282", "📰"),
    ("Corriere dello Sport", "6030691308346019878", "📰"),
    ("Tuttosport", "6032834612990841221", "📰"),
    ("X - @", "5796663209016431644", "📲"),
    ("YouTube - ", "6032683730789732131", "🖥"),
    ("Gianluca Di Marzio", "5785253271912324677", "📲"),
    ("Alfredo Pedullà", "5785322627044220734", "📲"),
    ("Borsa Italiana", "5373001317042101552", "📈"),
    ("Juventus.com", "6028591382870888482", "⚪️"),
)


class ArticleLike(Protocol):
    source: str
    title: str
    url: str
    summary: str


class TelegramDeliveryError(RuntimeError):
    """Errore definitivo dopo i tentativi previsti."""


def source_emoji(source: str) -> str:
    for source_prefix, emoji_id, fallback_emoji in SOURCE_EMOJIS:
        if source.startswith(source_prefix):
            return f'<tg-emoji emoji-id="{emoji_id}">{fallback_emoji}</tg-emoji>'
    return "📰"


def _clip(value: str, limit: int) -> str:
    value = " ".join((value or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def format_article_message(article: ArticleLike) -> str:
    """Crea l'unico formato Telegram usato dal bot."""
    source = escape(_clip(article.source, 160))
    title = escape(_clip(article.title, 900))
    url = escape(article.url.strip(), quote=True)
    summary = _clip(article.summary, 2400)

    parts = [
        f"{source_emoji(article.source)} <b>{source}</b>",
        f"<b>{title}</b>",
    ]
    if summary:
        parts.append(escape(summary))
    parts.append(f'🔗 <a href="{url}">Apri contenuto</a>')

    message = "\n\n".join(parts)
    if len(message) > TELEGRAM_MAX_MESSAGE_LENGTH:
        raise ValueError("Messaggio Telegram oltre il limite di 4096 caratteri.")
    return message


class TelegramClient:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        session: requests.Session | None = None,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not token or not chat_id:
            raise ValueError("Token e chat_id Telegram sono obbligatori.")
        self.chat_id = chat_id
        self.endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
        self.session = session or requests.Session()
        self.max_attempts = max(max_attempts, 1)
        self.sleep = sleep

    @staticmethod
    def _response_data(response: requests.Response) -> dict:
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _retry_delay(
        self,
        attempt: int,
        response_data: dict | None = None,
    ) -> int:
        if response_data:
            retry_after = response_data.get("parameters", {}).get("retry_after")
            try:
                return max(int(retry_after), 1)
            except (TypeError, ValueError):
                pass
        return min(2 ** (attempt - 1), 10)

    def send_message(self, text: str) -> int | None:
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        last_error = "errore sconosciuto"

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.post(
                    self.endpoint,
                    json=payload,
                    timeout=30,
                )
            except requests.RequestException as error:
                last_error = f"errore di rete: {error}"
                if attempt == self.max_attempts:
                    break
                self.sleep(self._retry_delay(attempt))
                continue

            data = self._response_data(response)
            if response.ok and data.get("ok") is True:
                message_id = data.get("result", {}).get("message_id")
                return int(message_id) if message_id is not None else None

            description = data.get("description") or response.text
            last_error = f"HTTP {response.status_code} - {description}"
            if (
                response.status_code not in RETRYABLE_STATUS_CODES
                or attempt == self.max_attempts
            ):
                break
            self.sleep(self._retry_delay(attempt, data))

        raise TelegramDeliveryError(
            f"Telegram sendMessage fallito dopo {self.max_attempts} "
            f"tentativi: {last_error}"
        )

    def send_article(self, article: ArticleLike) -> int | None:
        return self.send_message(format_article_message(article))
