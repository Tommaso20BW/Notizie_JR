import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import juve_press_bot as bot
from telegram_notifier import DeliveryReceipt, TelegramDeliveryError


class FakeTelegramClient:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.sent = []

    def send_article(
        self,
        article,
        *,
        video_url="",
        video_file_path="",
        video_thumbnail_url="",
        photo_url="",
        photo_urls=(),
    ):
        self.sent.append(article.notification_key)
        return DeliveryReceipt(message_id=100, mode="testo")


class FailingTelegramClient(FakeTelegramClient):
    def send_article(self, article, **kwargs):
        raise TelegramDeliveryError("Telegram non disponibile")


class FakePreviewResolver:
    def __init__(self, session):
        pass

    def resolve(self, page_url, direct_image_url=""):
        return direct_image_url

    def resolve_all(self, page_url, direct_image_urls=()):
        return list(direct_image_urls)


def sample_article():
    return bot.Article(
        source="Tuttosport",
        title="Notizia Juventus",
        url="https://example.com/news",
        published=datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc),
        state_key="article:1",
    )


class RunJournalTests(unittest.TestCase):
    def _state_paths(self, directory):
        seen = Path(directory) / "seen.json"
        pending = Path(directory) / "pending.json"
        seen.write_text("[]", encoding="utf-8")
        pending.write_text("[]", encoding="utf-8")
        return seen, pending

    def test_discovered_article_is_delivered_before_collection_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            seen, pending = self._state_paths(directory)
            telegram = FakeTelegramClient("token", "chat")

            def interrupted_collection(session, requested_dates, on_article=None):
                on_article(sample_article())
                self.assertEqual(telegram.sent, ["article:1"])
                raise RuntimeError("fonte successiva non disponibile")

            with (
                patch.object(bot, "STATE_FILE", seen),
                patch.object(bot, "PENDING_FILE", pending),
                patch.object(bot, "collect_articles", interrupted_collection),
                patch.object(bot, "TelegramClient", return_value=telegram),
                patch.object(bot, "PreviewImageResolver", FakePreviewResolver),
                patch.object(bot.time, "sleep", lambda _: None),
                patch.dict(
                    os.environ,
                    {"TELEGRAM_TOKEN": "token", "CHAT_ID": "chat"},
                    clear=False,
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "fonte successiva"):
                    bot.run()

            self.assertEqual(json.loads(pending.read_text(encoding="utf-8")), [])
            self.assertEqual(
                json.loads(seen.read_text(encoding="utf-8"))["items"],
                ["article:1"],
            )

    def test_failed_delivery_remains_in_journal_for_the_next_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            seen, pending = self._state_paths(directory)

            def successful_collection(session, requested_dates, on_article=None):
                article = sample_article()
                on_article(article)
                return [article], []

            with (
                patch.object(bot, "STATE_FILE", seen),
                patch.object(bot, "PENDING_FILE", pending),
                patch.object(bot, "collect_articles", successful_collection),
                patch.object(bot, "TelegramClient", FailingTelegramClient),
                patch.object(bot, "PreviewImageResolver", FakePreviewResolver),
                patch.dict(
                    os.environ,
                    {"TELEGRAM_TOKEN": "token", "CHAT_ID": "chat"},
                    clear=False,
                ),
            ):
                bot.run()

            stored = json.loads(pending.read_text(encoding="utf-8"))
            self.assertEqual(stored[0]["notification_key"], "article:1")
            self.assertEqual(
                json.loads(seen.read_text(encoding="utf-8"))["items"],
                [],
            )

    def test_successful_delivery_moves_article_from_pending_to_seen(self):
        with tempfile.TemporaryDirectory() as directory:
            seen, pending = self._state_paths(directory)

            def successful_collection(session, requested_dates, on_article=None):
                article = sample_article()
                on_article(article)
                return [article], []

            with (
                patch.object(bot, "STATE_FILE", seen),
                patch.object(bot, "PENDING_FILE", pending),
                patch.object(bot, "collect_articles", successful_collection),
                patch.object(bot, "TelegramClient", FakeTelegramClient),
                patch.object(bot, "PreviewImageResolver", FakePreviewResolver),
                patch.object(bot.time, "sleep", lambda _: None),
                patch.dict(
                    os.environ,
                    {"TELEGRAM_TOKEN": "token", "CHAT_ID": "chat"},
                    clear=False,
                ),
            ):
                bot.run()

            self.assertEqual(
                json.loads(seen.read_text(encoding="utf-8"))["items"],
                ["article:1"],
            )
            self.assertEqual(json.loads(pending.read_text(encoding="utf-8")), [])

    def test_successor_run_does_not_resend_persisted_article(self):
        with tempfile.TemporaryDirectory() as directory:
            seen, pending = self._state_paths(directory)
            telegram = FakeTelegramClient("token", "chat")

            def same_collection(session, requested_dates, on_article=None):
                article = sample_article()
                on_article(article)
                return [article], []

            with (
                patch.object(bot, "STATE_FILE", seen),
                patch.object(bot, "PENDING_FILE", pending),
                patch.object(bot, "collect_articles", same_collection),
                patch.object(bot, "TelegramClient", return_value=telegram),
                patch.object(bot, "PreviewImageResolver", FakePreviewResolver),
                patch.object(bot.time, "sleep", lambda _: None),
                patch.dict(
                    os.environ,
                    {"TELEGRAM_TOKEN": "token", "CHAT_ID": "chat"},
                    clear=False,
                ),
            ):
                bot.run()
                bot.run()

            self.assertEqual(telegram.sent, ["article:1"])
            self.assertEqual(
                json.loads(seen.read_text(encoding="utf-8"))["items"],
                ["article:1"],
            )


if __name__ == "__main__":
    unittest.main()
