import json
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from article_journal import ArticleJournal


@dataclass
class SampleArticle:
    source: str = "Sky Sport"
    title: str = "Notizia Juventus"
    url: str = "https://example.com/news"
    summary: str = "Sommario"
    state_key: str = "sky:123"
    image_url: str = "https://example.com/photo.jpg"
    image_urls: tuple = ()
    video_url: str = "https://video.twimg.com/clip.mp4"
    video_thumbnail_url: str = "https://example.com/poster.jpg"
    published: datetime = datetime(2026, 7, 26, tzinfo=timezone.utc)

    @property
    def notification_key(self):
        return self.state_key or self.url


class ArticleJournalTests(unittest.TestCase):
    def test_article_is_written_immediately_and_survives_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pending.json"
            journal = ArticleJournal(path)

            self.assertTrue(journal.add(SampleArticle()))

            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored[0]["notification_key"], "sky:123")
            self.assertEqual(stored[0]["image_url"], "https://example.com/photo.jpg")
            self.assertEqual(
                stored[0]["video_url"],
                "https://video.twimg.com/clip.mp4",
            )
            self.assertEqual(
                stored[0]["video_thumbnail_url"],
                "https://example.com/poster.jpg",
            )
            self.assertEqual(len(ArticleJournal(path).entries), 1)

    def test_duplicate_is_not_written_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pending.json"
            journal = ArticleJournal(path)

            self.assertTrue(journal.add(SampleArticle()))
            self.assertFalse(journal.add(SampleArticle()))
            self.assertEqual(len(journal.entries), 1)

    def test_remove_is_persisted_after_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pending.json"
            journal = ArticleJournal(path)
            journal.add(SampleArticle())

            self.assertTrue(journal.remove("sky:123"))

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [])


if __name__ == "__main__":
    unittest.main()
