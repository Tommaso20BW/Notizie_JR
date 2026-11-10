import json
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

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

    def post(
        self,
        endpoint,
        json=None,
        timeout=None,
        data=None,
        files=None,
    ):
        payload = json if json is not None else data
        self.calls.append((endpoint, payload, timeout))
        self.files = getattr(self, "files", [])
        self.files.append(files)
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
        self.assertIn(
            '<tg-emoji emoji-id="5271604874419647061">🔗</tg-emoji>',
            message,
        )

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


    # Il bot invia sempre un Rich Message (sendRichMessage): niente più sendPhoto,
    # sendVideo o album. I test controllano gli stessi casi nel formato attuale.

    def _client(self, results, sleeps=None):
        session = FakeSession(results)
        client = TelegramClient(
            "token",
            "chat",
            session=session,
            sleep=(sleeps.append if sleeps is not None else (lambda _: None)),
        )
        return client, session

    @staticmethod
    def _html(call):
        payload = call[1]
        rich = payload["rich_message"]
        if isinstance(rich, str):
            rich = json.loads(rich)
        return rich["html"]

    def test_rate_limit_uses_retry_after_and_returns_message_id(self):
        sleeps = []
        client, session = self._client(
            [
                FakeResponse(429, {"ok": False, "description": "Too Many Requests",
                                   "parameters": {"retry_after": 4}}),
                FakeResponse(200, {"ok": True, "result": {"message_id": 123}}),
            ],
            sleeps,
        )
        receipt = client.send_article(SampleArticle())
        self.assertEqual(receipt.message_id, 123)
        self.assertEqual(receipt.mode, "rich")
        self.assertEqual(sleeps, [4])
        self.assertEqual(len(session.calls), 2)

    def test_article_with_image_is_sent_as_photo(self):
        client, session = self._client(
            [FakeResponse(200, {"ok": True, "result": {"message_id": 789}})]
        )
        receipt = client.send_article(SampleArticle(), photo_url="https://example.com/photo.jpg")
        self.assertTrue(session.calls[0][0].endswith("/sendRichMessage"))
        self.assertIn('<img src="https://example.com/photo.jpg"/>', self._html(session.calls[0]))
        self.assertEqual(receipt.mode, "rich")
        self.assertFalse(receipt.photo_fallback)

    def test_rejected_photo_falls_back_to_text(self):
        rejected = FakeResponse(400, {"ok": False, "description": "wrong file identifier"})
        client, session = self._client(
            [rejected] * 5 + [FakeResponse(200, {"ok": True, "result": {"message_id": 790}})]
        )
        receipt = client.send_article(SampleArticle(), photo_url="https://example.com/broken.jpg")
        # Una sola foto di un sito: 5 tentativi, poi subito il solo testo.
        self.assertEqual(len(session.calls), 6)
        self.assertNotIn("<img", self._html(session.calls[-1]))
        self.assertEqual(receipt.message_id, 790)
        self.assertEqual(receipt.mode, "rich_text")
        self.assertTrue(receipt.photo_fallback)

    def test_x_photo_download_is_retried_before_text_fallback(self):
        failed = FakeResponse(400, {"ok": False, "description": "failed to get HTTP URL content"})
        sleeps = []
        client, session = self._client(
            [failed, failed, FakeResponse(200, {"ok": True, "result": {"message_id": 791}})],
            sleeps,
        )
        receipt = client.send_article(
            SampleArticle(source="X - Reporter", url="https://x.com/Reporter/status/1234567890"),
            photo_url="https://pbs.twimg.com/media/photo.jpg",
        )
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(sleeps, [1, 2])
        self.assertEqual(receipt.message_id, 791)
        self.assertEqual(receipt.mode, "rich")
        self.assertFalse(receipt.photo_fallback)

    def test_x_photo_falls_back_to_text_after_three_failed_attempts(self):
        failed = FakeResponse(400, {"ok": False, "description": "failed to get HTTP URL content"})
        client, session = self._client(
            [failed] * 10 + [FakeResponse(200, {"ok": True, "result": {"message_id": 792}})]
        )
        receipt = client.send_article(
            SampleArticle(source="X - Reporter", url="https://x.com/Reporter/status/1234567890"),
            photo_url="https://pbs.twimg.com/media/broken.jpg",
        )
        # 5 tentativi con la foto, 5 con la versione più leggera, poi il solo testo.
        self.assertEqual(len(session.calls), 11)
        self.assertIn("name=medium", self._html(session.calls[5]))
        self.assertNotIn("<img", self._html(session.calls[-1]))
        self.assertEqual(receipt.message_id, 792)
        self.assertEqual(receipt.mode, "rich_text")
        self.assertTrue(receipt.photo_fallback)

    def test_article_with_video_is_sent_as_streaming_video(self):
        client, session = self._client(
            [FakeResponse(200, {"ok": True, "result": {"message_id": 791}})]
        )
        receipt = client.send_article(SampleArticle(), video_url="https://video.twimg.com/clip.mp4")
        self.assertIn('<video src="https://video.twimg.com/clip.mp4"></video>', self._html(session.calls[0]))
        self.assertEqual(receipt.message_id, 791)
        self.assertEqual(receipt.mode, "rich")
        self.assertFalse(receipt.video_fallback)

    def test_prepared_video_is_uploaded_as_multipart_video(self):
        client, session = self._client(
            [FakeResponse(200, {"ok": True, "result": {"message_id": 795}})]
        )
        with TemporaryDirectory() as directory:
            video_file = Path(directory) / "prepared.mp4"
            video_file.write_bytes(b"prepared-video")
            receipt = client.send_article(SampleArticle(), video_file_path=str(video_file))
        self.assertTrue(session.calls[0][0].endswith("/sendRichMessage"))
        self.assertIn("rich_video", session.files[0])
        self.assertIn("tg://video?id=video_0", self._html(session.calls[0]))
        self.assertEqual(receipt.message_id, 795)
        self.assertEqual(receipt.mode, "rich")

    def test_video_and_photos_are_sent_as_one_mixed_album(self):
        client, session = self._client(
            [FakeResponse(200, {"ok": True, "result": {"message_id": 792}})]
        )
        receipt = client.send_article(
            SampleArticle(),
            video_url="https://video.twimg.com/clip.mp4",
            photo_urls=("https://pbs.twimg.com/photo-1.jpg", "https://pbs.twimg.com/photo-2.jpg"),
        )
        html = self._html(session.calls[0])
        self.assertIn("<tg-collage><video", html)
        self.assertEqual(html.count("<img "), 2)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(receipt.message_id, 792)

    def test_prepared_video_and_photos_use_multipart_mixed_album(self):
        client, session = self._client(
            [FakeResponse(200, {"ok": True, "result": {"message_id": 796}})]
        )
        with TemporaryDirectory() as directory:
            video_file = Path(directory) / "prepared.mp4"
            video_file.write_bytes(b"prepared-video")
            receipt = client.send_article(
                SampleArticle(),
                video_file_path=str(video_file),
                photo_urls=("https://pbs.twimg.com/photo.jpg",),
            )
        html = self._html(session.calls[0])
        self.assertIn("rich_video", session.files[0])
        self.assertIn('<tg-collage><video src="tg://video?id=video_0"></video><img', html)
        self.assertEqual(receipt.message_id, 796)

    def test_rejected_video_falls_back_to_poster(self):
        failed = FakeResponse(400, {"ok": False, "description": "failed to get HTTP URL"})
        client, session = self._client(
            [failed] * 5 + [FakeResponse(200, {"ok": True, "result": {"message_id": 792}})]
        )
        receipt = client.send_article(
            SampleArticle(),
            video_url="https://video.twimg.com/broken.mp4",
            photo_url="https://example.com/poster.jpg",
        )
        # Video rifiutato: la notizia parte con la sola foto, senza video.
        last = self._html(session.calls[-1])
        self.assertIn('<img src="https://example.com/poster.jpg"/>', last)
        self.assertNotIn("<video", last)
        self.assertEqual(receipt.message_id, 792)
        self.assertEqual(receipt.mode, "rich_one_photo")
        self.assertTrue(receipt.video_fallback)
        self.assertFalse(receipt.photo_fallback)


if __name__ == "__main__":
    unittest.main()
