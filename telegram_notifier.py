"""Formattazione e consegna affidabile delle notizie a Telegram."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Protocol

import requests

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
TELEGRAM_MAX_CAPTION_LENGTH = 1024
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

SOURCE_EMOJIS = (
    ("Sky Sport", "6033058586945392520", "📰"),
    ("La Gazzetta dello Sport", "6032862491623559282", "📰"),
    ("Corriere dello Sport", "6030691308346019878", "📰"),
    ("Tuttosport", "6032834612990841221", "📰"),
    ("X - ", "5796663209016431644", "📲"),
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


@dataclass(frozen=True)
class DeliveryReceipt:
    message_id: int | None
    mode: str
    photo_fallback: bool = False
    video_fallback: bool = False


def source_emoji(source: str) -> str:
    for source_prefix, emoji_id, fallback_emoji in SOURCE_EMOJIS:
        if source.startswith(source_prefix):
            if emoji_id:
                return f'<tg-emoji emoji-id="{emoji_id}">{fallback_emoji}</tg-emoji>'
            return fallback_emoji
    return "📰"


def _clip(value: str, limit: int) -> str:
    value = " ".join((value or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def format_article_message(
    article: ArticleLike,
    *,
    max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH,
) -> str:
    """Crea l'unico formato Telegram usato dal bot."""
    source = escape(_clip(article.source, 160))
    is_caption = max_length <= TELEGRAM_MAX_CAPTION_LENGTH
    title = escape(_clip(article.title, 350 if is_caption else 900))
    url = escape(article.url.strip(), quote=True)
    summary = _clip(article.summary, 350 if is_caption else 2400)

    parts = [
        f"{source_emoji(article.source)} <b>{source}</b>",
        f"<b>{title}</b>",
    ]
    if summary:
        parts.append(escape(summary))
    parts.append(f'🔗 <a href="{url}">Apri contenuto</a>')

    message = "\n\n".join(parts)
    if len(message) > max_length and summary:
        parts.remove(escape(summary))
        message = "\n\n".join(parts)
    if len(message) > max_length:
        raise ValueError(f"Testo Telegram oltre il limite di {max_length} caratteri.")
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
        self.api_root = f"https://api.telegram.org/bot{token}"
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

    def _deliver_result(self, method: str, payload: dict):
        last_error = "errore sconosciuto"

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.post(
                    f"{self.api_root}/{method}",
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
                return data.get("result")

            description = data.get("description") or response.text
            last_error = f"HTTP {response.status_code} - {description}"
            if (
                response.status_code not in RETRYABLE_STATUS_CODES
                or attempt == self.max_attempts
            ):
                break
            self.sleep(self._retry_delay(attempt, data))

        raise TelegramDeliveryError(
            f"Telegram {method} fallito dopo {self.max_attempts} "
            f"tentativi: {last_error}"
        )

    def _deliver_files_result(
        self,
        method: str,
        payload: dict,
        file_specs: Sequence[tuple[str, str, str, str]],
    ):
        """Invia uno o più file multipart riaprendoli a ogni tentativo."""
        last_error = "errore sconosciuto"

        for attempt in range(1, self.max_attempts + 1):
            try:
                with ExitStack() as stack:
                    files = {}
                    for field_name, file_path, filename, mime_type in file_specs:
                        handle = stack.enter_context(Path(file_path).open("rb"))
                        files[field_name] = (filename, handle, mime_type)
                    response = self.session.post(
                        f"{self.api_root}/{method}",
                        data=payload,
                        files=files,
                        timeout=60,
                    )
            except (OSError, requests.RequestException) as error:
                last_error = f"errore di rete o file: {error}"
                if attempt == self.max_attempts:
                    break
                self.sleep(self._retry_delay(attempt))
                continue

            data = self._response_data(response)
            if response.ok and data.get("ok") is True:
                return data.get("result")

            description = data.get("description") or response.text
            last_error = f"HTTP {response.status_code} - {description}"
            if (
                response.status_code not in RETRYABLE_STATUS_CODES
                or attempt == self.max_attempts
            ):
                break
            self.sleep(self._retry_delay(attempt, data))

        raise TelegramDeliveryError(
            f"Telegram {method} fallito dopo {self.max_attempts} "
            f"tentativi: {last_error}"
        )

    def _deliver_file_result(
        self,
        method: str,
        payload: dict,
        video_file_path: str,
    ):
        """Compatibilità: invia un singolo MP4 multipart."""
        return self._deliver_files_result(
            method,
            payload,
            (("video", video_file_path, "video.mp4", "video/mp4"),),
        )

    def send_message(self, text: str) -> int | None:
        result = self._deliver_result(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        message_id = (result or {}).get("message_id")
        return int(message_id) if message_id is not None else None

    def send_photo(self, photo_url: str, caption: str) -> int | None:
        result = self._deliver_result(
            "sendPhoto",
            {
                "chat_id": self.chat_id,
                "photo": photo_url,
                "caption": caption,
                "parse_mode": "HTML",
            },
        )
        message_id = (result or {}).get("message_id")
        return int(message_id) if message_id is not None else None

    def send_video(self, video_url: str, caption: str) -> int | None:
        result = self._deliver_result(
            "sendVideo",
            {
                "chat_id": self.chat_id,
                "video": video_url,
                "caption": caption,
                "parse_mode": "HTML",
                "supports_streaming": True,
            },
        )
        message_id = (result or {}).get("message_id")
        return int(message_id) if message_id is not None else None

    def send_document(self, document_url: str, caption: str) -> int | None:
        """Invia un PDF/documento pubblico direttamente come documento Telegram."""
        result = self._deliver_result(
            "sendDocument",
            {
                "chat_id": self.chat_id,
                "document": document_url,
                "caption": caption,
                "parse_mode": "HTML",
            },
        )
        message_id = (result or {}).get("message_id")
        return int(message_id) if message_id is not None else None

    def send_video_file(self, video_file_path: str, caption: str) -> int | None:
        result = self._deliver_file_result(
            "sendVideo",
            {
                "chat_id": self.chat_id,
                "caption": caption,
                "parse_mode": "HTML",
                "supports_streaming": "true",
            },
            video_file_path,
        )
        message_id = (result or {}).get("message_id")
        return int(message_id) if message_id is not None else None

    def send_media_group(
        self,
        photo_urls: Sequence[str],
        caption: str,
    ) -> list[int]:
        """Invia più foto come un unico album Telegram (max 10 elementi).
        La didascalia va solo sul primo elemento, altrimenti Telegram la
        ripeterebbe sotto ogni immagine."""
        media = []
        for index, url in enumerate(photo_urls):
            item: dict = {"type": "photo", "media": url}
            if index == 0:
                item["caption"] = caption
                item["parse_mode"] = "HTML"
            media.append(item)

        result = self._deliver_result(
            "sendMediaGroup",
            {"chat_id": self.chat_id, "media": media},
        )
        message_ids = []
        for entry in result or []:
            message_id = entry.get("message_id")
            if message_id is not None:
                message_ids.append(int(message_id))
        return message_ids

    def send_mixed_media_group(
        self,
        video_url: str,
        photo_urls: Sequence[str],
        caption: str,
    ) -> list[int]:
        """Invia un video e le eventuali foto dello stesso post in un album."""
        media = [
            {
                "type": "video",
                "media": video_url,
                "caption": caption,
                "parse_mode": "HTML",
                "supports_streaming": True,
            }
        ]
        media.extend(
            {"type": "photo", "media": url}
            for url in photo_urls[:9]
        )
        result = self._deliver_result(
            "sendMediaGroup",
            {"chat_id": self.chat_id, "media": media},
        )
        return [
            int(entry["message_id"])
            for entry in (result or [])
            if entry.get("message_id") is not None
        ]

    def send_mixed_media_group_file(
        self,
        video_file_path: str,
        photo_urls: Sequence[str],
        caption: str,
    ) -> list[int]:
        media = [
            {
                "type": "video",
                "media": "attach://video",
                "caption": caption,
                "parse_mode": "HTML",
                "supports_streaming": True,
            }
        ]
        media.extend(
            {"type": "photo", "media": url}
            for url in photo_urls[:9]
        )
        result = self._deliver_file_result(
            "sendMediaGroup",
            {
                "chat_id": self.chat_id,
                "media": json.dumps(media, ensure_ascii=False),
            },
            video_file_path,
        )
        return [
            int(entry["message_id"])
            for entry in (result or [])
            if entry.get("message_id") is not None
        ]

    def send_article(
        self,
        article: ArticleLike,
        *,
        document_url: str = "",
        video_url: str = "",
        video_file_path: str = "",
        video_thumbnail_url: str = "",
        photo_url: str = "",
        photo_urls: Sequence[str] = (),
    ) -> DeliveryReceipt:
        if document_url:
            caption = format_article_message(
                article,
                max_length=TELEGRAM_MAX_CAPTION_LENGTH,
            )
            message_id = self.send_document(document_url, caption)
            return DeliveryReceipt(message_id, "documento")

        # Telegram consente al massimo 10 elementi per album.
        urls = list(photo_urls)[:10] if photo_urls else (
            [photo_url] if photo_url else []
        )

        mixed_album_failed = False
        if (video_file_path or video_url) and urls:
            try:
                caption = format_article_message(
                    article,
                    max_length=TELEGRAM_MAX_CAPTION_LENGTH,
                )
                if video_file_path:
                    message_ids = self.send_mixed_media_group_file(
                        video_file_path,
                        urls,
                        caption,
                    )
                else:
                    message_ids = self.send_mixed_media_group(
                        video_url,
                        urls,
                        caption,
                    )
                first_id = message_ids[0] if message_ids else None
                return DeliveryReceipt(first_id, "album")
            except (TelegramDeliveryError, ValueError):
                mixed_album_failed = True

        video_fallback = False
        if video_file_path or video_url:
            try:
                caption = format_article_message(
                    article,
                    max_length=TELEGRAM_MAX_CAPTION_LENGTH,
                )
                if video_file_path:
                    message_id = self.send_video_file(
                        video_file_path,
                        caption,
                    )
                else:
                    message_id = self.send_video(video_url, caption)
                return DeliveryReceipt(
                    message_id,
                    "video",
                    photo_fallback=mixed_album_failed,
                )
            except (TelegramDeliveryError, ValueError):
                video_fallback = True

        if video_fallback and not urls and video_thumbnail_url:
            urls = [video_thumbnail_url]

        if len(urls) > 1:
            try:
                message_ids = self.send_media_group(
                    urls,
                    format_article_message(
                        article,
                        max_length=TELEGRAM_MAX_CAPTION_LENGTH,
                    ),
                )
                first_id = message_ids[0] if message_ids else None
                return DeliveryReceipt(
                    first_id,
                    "album",
                    video_fallback=video_fallback,
                )
            except (TelegramDeliveryError, ValueError):
                message_id = self.send_message(format_article_message(article))
                return DeliveryReceipt(
                    message_id,
                    "testo",
                    photo_fallback=True,
                    video_fallback=video_fallback,
                )

        if urls:
            try:
                message_id = self.send_photo(
                    urls[0],
                    format_article_message(
                        article,
                        max_length=TELEGRAM_MAX_CAPTION_LENGTH,
                    ),
                )
                return DeliveryReceipt(
                    message_id,
                    "foto",
                    video_fallback=video_fallback,
                )
            except (TelegramDeliveryError, ValueError):
                message_id = self.send_message(format_article_message(article))
                return DeliveryReceipt(
                    message_id,
                    "testo",
                    photo_fallback=True,
                    video_fallback=video_fallback,
                )

        message_id = self.send_message(format_article_message(article))
        return DeliveryReceipt(
            message_id,
            "testo",
            video_fallback=video_fallback,
        )
