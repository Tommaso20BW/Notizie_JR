import json
import os
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import instagram_source
from article_journal import ArticleJournal
from telegram_notifier import TelegramClient


class FakeResponse:
    def __init__(self, result):
        self.status_code = 200
        self.ok = True
        self.text = ""
        self._result = result

    def json(self):
        return {"ok": True, "result": self._result}


class FakeSession:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.files = []

    def post(self, endpoint, json=None, timeout=None, data=None, files=None):
        payload = json if json is not None else data
        self.calls.append((endpoint, payload, timeout))
        self.files.append(files)
        return FakeResponse(self.results.pop(0))


@dataclass
class Article:
    source: str = "Instagram - Juventus"
    title: str = "Nuovo post della Juventus"
    url: str = "https://www.instagram.com/p/ABC/"
    summary: str = "Caption del post"


@dataclass
class JournalSample:
    source: str
    title: str
    url: str
    published: datetime
    summary: str
    state_key: str
    image_url: str = ""
    image_urls: tuple[str, ...] = ()
    video_url: str = ""
    video_thumbnail_url: str = ""
    media_items: tuple[tuple[str, str, str], ...] = ()

    @property
    def notification_key(self):
        return self.state_key or self.url


class InstagramPatchTests(unittest.TestCase):
    def test_instagram_loader_is_anonymous_and_needs_no_secrets(self):
        created = object()
        fake_instaloader = SimpleNamespace(Instaloader=lambda **kwargs: created)

        with patch.object(instagram_source, "instaloader", fake_instaloader):
            loader = instagram_source._instagram_loader()

        self.assertIs(loader, created)

    def test_today_sidecar_preserves_photo_video_order(self):
        now_rome = datetime.now(instagram_source.ROME).replace(microsecond=0)
        today_utc = now_rome.astimezone(timezone.utc)
        yesterday_utc = (now_rome - timedelta(days=1)).astimezone(timezone.utc)

        photo_node = SimpleNamespace(
            is_video=False,
            display_url="https://cdn.example/photo.jpg",
            video_url=None,
        )
        video_node = SimpleNamespace(
            is_video=True,
            display_url="https://cdn.example/cover.jpg",
            video_url="https://cdn.example/video.mp4",
        )
        today_post = SimpleNamespace(
            shortcode="TODAY1",
            product_type="carousel_container",
            date_utc=today_utc,
            typename="GraphSidecar",
            is_video=False,
            caption="  Una   caption\nJuventus  ",
            is_pinned=False,
            get_sidecar_nodes=lambda: iter([photo_node, video_node]),
        )
        old_post = SimpleNamespace(
            shortcode="OLD1",
            product_type="feed",
            date_utc=yesterday_utc,
            typename="GraphImage",
            is_video=False,
            caption="vecchio",
            is_pinned=False,
            url="https://cdn.example/old.jpg",
        )

        fake_profile = SimpleNamespace(get_posts=lambda: iter([today_post, old_post]))
        fake_instaloader = SimpleNamespace(
            Profile=SimpleNamespace(from_username=lambda context, name: fake_profile)
        )
        fake_loader = SimpleNamespace(context=object())

        with patch.object(instagram_source, "instaloader", fake_instaloader), patch.object(
            instagram_source, "_instagram_loader", return_value=fake_loader
        ):
            posts = instagram_source.fetch_instagram_posts({now_rome.date()})

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].shortcode, "TODAY1")
        self.assertEqual(posts[0].caption, "Una caption Juventus")
        self.assertEqual(
            posts[0].media_items,
            (
                ("photo", "https://cdn.example/photo.jpg", ""),
                (
                    "video",
                    "https://cdn.example/video.mp4",
                    "https://cdn.example/cover.jpg",
                ),
            ),
        )

    def test_reel_is_included_and_deduplicated_against_grid(self):
        now_rome = datetime.now(instagram_source.ROME).replace(microsecond=0)
        published_utc = now_rome.astimezone(timezone.utc)

        def make_post(shortcode):
            return SimpleNamespace(
                shortcode=shortcode,
                product_type="clips",
                date_utc=published_utc,
                typename="GraphVideo",
                is_video=True,
                caption="Reel Juventus",
                is_pinned=False,
                video_url="https://cdn.example/reel.mp4",
                url="https://cdn.example/reel-cover.jpg",
            )

        fake_profile = SimpleNamespace(
            get_posts=lambda: iter([make_post("REEL1")]),
            get_reels=lambda: iter([make_post("REEL1")]),
        )
        fake_instaloader = SimpleNamespace(
            Profile=SimpleNamespace(from_username=lambda context, name: fake_profile)
        )

        with patch.object(instagram_source, "instaloader", fake_instaloader), patch.object(
            instagram_source,
            "_instagram_loader",
            return_value=SimpleNamespace(context=object()),
        ):
            posts = instagram_source.fetch_instagram_posts({now_rome.date()})

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].url, "https://www.instagram.com/reel/REEL1/")
        self.assertEqual(posts[0].media_items[0][0], "video")

    def test_mixed_instagram_post_is_one_multipart_album(self):
        session = FakeSession(
            [[{"message_id": 101}, {"message_id": 102}, {"message_id": 103}]]
        )
        client = TelegramClient("token", "chat", session=session, sleep=lambda _: None)

        with TemporaryDirectory() as directory:
            video = Path(directory) / "video.mp4"
            video.write_bytes(b"video")
            receipt = client.send_article(
                Article(),
                prepared_media_items=(
                    ("photo", "https://cdn.example/one.jpg", False),
                    ("video", str(video), True),
                    ("photo", "https://cdn.example/two.jpg", False),
                ),
            )

        endpoint, payload, timeout = session.calls[0]
        self.assertTrue(endpoint.endswith("/sendMediaGroup"))
        self.assertEqual(timeout, 60)
        media = json.loads(payload["media"])
        self.assertEqual([item["type"] for item in media], ["photo", "video", "photo"])
        self.assertEqual(media[1]["media"], "attach://media_1")
        self.assertIn("caption", media[0])
        self.assertNotIn("caption", media[1])
        self.assertIn("media_1", session.files[0])
        self.assertEqual(receipt.mode, "album")
        self.assertEqual(receipt.message_id, 101)

    def test_eleven_media_are_split_without_mixing_posts(self):
        session = FakeSession(
            [
                [{"message_id": index} for index in range(1, 11)],
                {"message_id": 11},
            ]
        )
        client = TelegramClient("token", "chat", session=session, sleep=lambda _: None)
        media = tuple(
            ("photo", f"https://cdn.example/{index}.jpg", False)
            for index in range(11)
        )

        receipt = client.send_article(Article(), prepared_media_items=media)

        self.assertEqual(len(session.calls), 2)
        self.assertTrue(session.calls[0][0].endswith("/sendMediaGroup"))
        self.assertEqual(len(session.calls[0][1]["media"]), 10)
        self.assertTrue(session.calls[1][0].endswith("/sendPhoto"))
        self.assertEqual(session.calls[1][1]["caption"], "")
        self.assertEqual(receipt.message_id, 1)
        self.assertEqual(receipt.mode, "album")

    def test_journal_refreshes_expiring_instagram_urls(self):
        published = datetime.now(timezone.utc)
        first = JournalSample(
            source="Instagram - Juventus",
            title="Nuovo post della Juventus",
            url="https://www.instagram.com/p/ABC/",
            published=published,
            summary="caption",
            state_key="instagram:juventus:ABC",
            media_items=(("photo", "https://cdn.example/old.jpg", ""),),
        )
        refreshed = JournalSample(
            source=first.source,
            title=first.title,
            url=first.url,
            published=published,
            summary=first.summary,
            state_key=first.state_key,
            media_items=(("photo", "https://cdn.example/new.jpg", ""),),
        )

        with TemporaryDirectory() as directory:
            path = Path(directory) / "pending.json"
            journal = ArticleJournal(path)
            self.assertTrue(journal.add(first))
            self.assertFalse(journal.add(refreshed))
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(saved[0]["media_items"][0][1], "https://cdn.example/new.jpg")


if __name__ == "__main__":
    unittest.main()
