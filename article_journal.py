"""Journal JSON incrementale delle notizie scoperte ma non ancora inviate."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Protocol


class JournalArticle(Protocol):
    source: str
    title: str
    url: str
    summary: str
    state_key: str
    image_url: str
    image_urls: tuple[str, ...]
    video_url: str
    video_thumbnail_url: str
    media_items: tuple[tuple[str, str, str], ...]
    published: object

    @property
    def notification_key(self) -> str: ...


class ArticleJournal:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries = self._load()

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Journal non leggibile ({self.path.name}).") from error
        if not isinstance(data, list):
            raise RuntimeError(f"Formato non valido in {self.path.name}.")

        entries: dict[str, dict] = {}
        for entry in data:
            if not isinstance(entry, dict):
                raise RuntimeError(f"Formato non valido in {self.path.name}.")
            key = entry.get("notification_key")
            if not isinstance(key, str) or not key:
                raise RuntimeError(f"Chiave non valida in {self.path.name}.")
            entries[key] = entry
        return entries

    def _save(self) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(list(self._entries.values()), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    @property
    def entries(self) -> list[dict]:
        return list(self._entries.values())

    def add(self, article: JournalArticle) -> bool:
        key = article.notification_key
        entry = {
            "notification_key": key,
            "source": article.source,
            "title": article.title,
            "url": article.url,
            "published": article.published.isoformat(),
            "summary": article.summary,
            "state_key": article.state_key,
            "image_url": article.image_url,
            "image_urls": list(article.image_urls),
            "video_url": article.video_url,
            "video_thumbnail_url": article.video_thumbnail_url,
            "media_items": [
                list(item) for item in getattr(article, "media_items", ())
            ],
        }
        is_new = key not in self._entries
        if self._entries.get(key) == entry:
            return False
        # Gli URL CDN di Instagram sono firmati e possono cambiare: anche una
        # voce già pendente viene aggiornata con gli URL freschi del nuovo run.
        self._entries[key] = entry
        self._save()
        return is_new

    def remove(self, notification_key: str) -> bool:
        if self._entries.pop(notification_key, None) is None:
            return False
        self._save()
        return True

    def discard_all(self, notification_keys: set[str]) -> int:
        removed = 0
        for key in notification_keys:
            if self._entries.pop(key, None) is not None:
                removed += 1
        if removed:
            self._save()
        return removed

    def clear(self) -> None:
        self._entries.clear()
        self._save()
