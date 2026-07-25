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
        if key in self._entries:
            return False
        self._entries[key] = {
            "notification_key": key,
            "source": article.source,
            "title": article.title,
            "url": article.url,
            "published": article.published.isoformat(),
            "summary": article.summary,
            "state_key": article.state_key,
            "image_url": article.image_url,
        }
        self._save()
        return True

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
