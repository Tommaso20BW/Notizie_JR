import unittest
from dataclasses import dataclass

import requests

from telegram_notifier import (
    TelegramClient,
    TelegramDeliveryError,
    format_article_message,
)


@dataclass
class SampleArticle:
    source: str = "Tuttosport"
    title: str = "Juve & mercato <estate>"
    url: str = "https://example.com/news?a=1&b=2"
    summary: str = "Contatto tra <club> & giocatore."


class FakeResponse:
    def __init__(self, status_code, data=None, text=""):
        self.status_code = status_code
        self._data = data or {}
        self.text = text
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def post(self, endpoint, json, timeout):
        self.calls.append((endpoint, json, timeout))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class TelegramNotifierTests(unittest.TestCase):
    def test_message_has_one_consistent_escaped_format(self):
        message = format_article_message(SampleArticle())

        self.assertIn("<b>Tuttosport</b>", message)
        self.assertIn("Juve &amp; mercato &lt;estate&gt;", message)
        self.assertIn("Contatto tra &lt;club&gt; &amp; giocatore.", message)
        self.assertIn("https://example.com/news?a=1&amp;b=2", message)
        self.assertIn("🔗", message)

    def test_rate_limit_uses_retry_after_and_returns_message_id(self):
        session = FakeSession(
            [
                FakeResponse(
                    429,
                    {
                        "ok": False,
                        "description": "Too Many Requests",
                        "parameters": {"retry_after": 4},
                    },
                ),
                FakeResponse(
                    200,
                    {"ok": True, "result": {"message_id": 123}},
                ),
            ]
        )
        sleeps = []
        client = TelegramClient(
            "token",
            "chat",
            session=session,
            sleep=sleeps.append,
        )

        receipt = client.send_article(SampleArticle())

        self.assertEqual(receipt.message_id, 123)
        self.assertEqual(receipt.mode, "testo")
        self.assertEqual(sleeps, [4])
        self.assertEqual(len(session.calls), 2)

    def test_network_error_is_retried(self):
        session = FakeSession(
            [
                requests.ConnectionError("offline"),
                FakeResponse(
                    200,
                    {"ok": True, "result": {"message_id": 456}},
                ),
            ]
        )
        client = TelegramClient(
            "token",
            "chat",
            session=session,
            sleep=lambda _: None,
        )

        receipt = client.send_article(SampleArticle())
        self.assertEqual(receipt.message_id, 456)
        self.assertEqual(len(session.calls), 2)

    def test_permanent_error_is_not_retried(self):
        session = FakeSession(
            [FakeResponse(400, {"ok": False, "description": "Bad Request"})]
        )
        client = TelegramClient(
            "token",
            "chat",
            session=session,
            sleep=lambda _: None,
        )

        with self.assertRaisesRegex(TelegramDeliveryError, "Bad Request"):
            client.send_article(SampleArticle())
        self.assertEqual(len(session.calls), 1)

    def test_article_with_image_is_sent_as_photo(self):
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {"ok": True, "result": {"message_id": 789}},
                )
            ]
        )
        client = TelegramClient(
            "token",
            "chat",
            session=session,
            sleep=lambda _: None,
        )

        receipt = client.send_article(
            SampleArticle(),
            photo_url="https://example.com/photo.jpg",
        )

        endpoint, payload, _ = session.calls[0]
        self.assertTrue(endpoint.endswith("/sendPhoto"))
        self.assertEqual(payload["photo"], "https://example.com/photo.jpg")
        self.assertLessEqual(len(payload["caption"]), 1024)
        self.assertEqual(receipt.mode, "foto")
        self.assertFalse(receipt.photo_fallback)

    def test_rejected_photo_falls_back_to_text(self):
        session = FakeSession(
            [
                FakeResponse(
                    400,
                    {"ok": False, "description": "wrong file identifier"},
                ),
                FakeResponse(
                    200,
                    {"ok": True, "result": {"message_id": 790}},
                ),
            ]
        )
        client = TelegramClient(
            "token",
            "chat",
            session=session,
            sleep=lambda _: None,
        )

        receipt = client.send_article(
            SampleArticle(),
            photo_url="https://example.com/broken.jpg",
        )

        self.assertTrue(session.calls[0][0].endswith("/sendPhoto"))
        self.assertTrue(session.calls[1][0].endswith("/sendMessage"))
        self.assertEqual(receipt.message_id, 790)
        self.assertEqual(receipt.mode, "testo")
        self.assertTrue(receipt.photo_fallback)


if __name__ == "__main__":
    unittest.main()
