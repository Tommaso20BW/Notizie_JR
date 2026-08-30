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

    def test_x_photo_download_is_retried_before_text_fallback(self):
        session = FakeSession(
            [
                FakeResponse(
                    400,
                    {
                        "ok": False,
                        "description": "failed to get HTTP URL content",
                    },
                ),
                FakeResponse(
                    400,
                    {
                        "ok": False,
                        "description": "failed to get HTTP URL content",
                    },
                ),
                FakeResponse(
                    200,
                    {"ok": True, "result": {"message_id": 791}},
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

        receipt = client.send_article(
            SampleArticle(
                source="X - Reporter",
                url="https://x.com/Reporter/status/1234567890",
            ),
            photo_url="https://pbs.twimg.com/media/photo.jpg",
        )

        self.assertEqual(len(session.calls), 3)
        self.assertTrue(
            all(call[0].endswith("/sendPhoto") for call in session.calls)
        )
        self.assertEqual(sleeps, [1, 2])
        self.assertEqual(receipt.message_id, 791)
        self.assertEqual(receipt.mode, "foto")
        self.assertFalse(receipt.photo_fallback)

    def test_x_photo_falls_back_to_text_after_three_failed_attempts(self):
        failed_download = FakeResponse(
            400,
            {
                "ok": False,
                "description": "failed to get HTTP URL content",
            },
        )
        session = FakeSession(
            [
                failed_download,
                failed_download,
                failed_download,
                FakeResponse(
                    200,
                    {"ok": True, "result": {"message_id": 792}},
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

        receipt = client.send_article(
            SampleArticle(
                source="X - Reporter",
                url="https://x.com/Reporter/status/1234567890",
            ),
            photo_url="https://pbs.twimg.com/media/broken.jpg",
        )

        self.assertEqual(len(session.calls), 4)
        self.assertTrue(
            all(
                call[0].endswith("/sendPhoto")
                for call in session.calls[:3]
            )
        )
        self.assertTrue(session.calls[3][0].endswith("/sendMessage"))
        self.assertEqual(sleeps, [1, 2])
        self.assertEqual(receipt.message_id, 792)
        self.assertEqual(receipt.mode, "testo")
        self.assertTrue(receipt.photo_fallback)

    def test_article_with_video_is_sent_as_streaming_video(self):
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {"ok": True, "result": {"message_id": 791}},
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
            video_url="https://video.twimg.com/clip.mp4",
        )

        endpoint, payload, _ = session.calls[0]
        self.assertTrue(endpoint.endswith("/sendVideo"))
        self.assertEqual(payload["video"], "https://video.twimg.com/clip.mp4")
        self.assertTrue(payload["supports_streaming"])
        self.assertLessEqual(len(payload["caption"]), 1024)
        self.assertEqual(receipt.message_id, 791)
        self.assertEqual(receipt.mode, "video")
        self.assertFalse(receipt.video_fallback)

    def test_prepared_video_is_uploaded_as_multipart_video(self):
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {"ok": True, "result": {"message_id": 795}},
                )
            ]
        )
        client = TelegramClient(
            "token",
            "chat",
            session=session,
            sleep=lambda _: None,
        )

        with TemporaryDirectory() as directory:
            video_file = Path(directory) / "prepared.mp4"
            video_file.write_bytes(b"prepared-video")
            receipt = client.send_article(
                SampleArticle(),
                video_file_path=str(video_file),
            )

        endpoint, payload, timeout = session.calls[0]
        self.assertTrue(endpoint.endswith("/sendVideo"))
        self.assertEqual(payload["supports_streaming"], "true")
        self.assertEqual(timeout, 60)
        self.assertIn("video", session.files[0])
        self.assertEqual(receipt.message_id, 795)
        self.assertEqual(receipt.mode, "video")

    def test_video_and_photos_are_sent_as_one_mixed_album(self):
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "ok": True,
                        "result": [
                            {"message_id": 792},
                            {"message_id": 793},
                            {"message_id": 794},
                        ],
                    },
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
            video_url="https://video.twimg.com/clip.mp4",
            photo_urls=(
                "https://pbs.twimg.com/photo-1.jpg",
                "https://pbs.twimg.com/photo-2.jpg",
            ),
        )

        endpoint, payload, _ = session.calls[0]
        self.assertTrue(endpoint.endswith("/sendMediaGroup"))
        self.assertEqual(
            [item["type"] for item in payload["media"]],
            ["video", "photo", "photo"],
        )
        self.assertIn("caption", payload["media"][0])
        self.assertNotIn("caption", payload["media"][1])
        self.assertEqual(receipt.message_id, 792)
        self.assertEqual(receipt.mode, "album")

    def test_prepared_video_and_photos_use_multipart_mixed_album(self):
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "ok": True,
                        "result": [
                            {"message_id": 796},
                            {"message_id": 797},
                        ],
                    },
                )
            ]
        )
        client = TelegramClient(
            "token",
            "chat",
            session=session,
            sleep=lambda _: None,
        )

        with TemporaryDirectory() as directory:
            video_file = Path(directory) / "prepared.mp4"
            video_file.write_bytes(b"prepared-video")
            receipt = client.send_article(
                SampleArticle(),
                video_file_path=str(video_file),
                photo_urls=("https://pbs.twimg.com/photo.jpg",),
            )

        endpoint, payload, _ = session.calls[0]
        self.assertTrue(endpoint.endswith("/sendMediaGroup"))
        self.assertIn('"media": "attach://video"', payload["media"])
        self.assertIn("video", session.files[0])
        self.assertEqual(receipt.message_id, 796)
        self.assertEqual(receipt.mode, "album")

    def test_rejected_video_falls_back_to_poster(self):
        session = FakeSession(
            [
                FakeResponse(
                    400,
                    {"ok": False, "description": "bad mixed media"},
                ),
                FakeResponse(
                    400,
                    {"ok": False, "description": "failed to get HTTP URL"},
                ),
                FakeResponse(
                    200,
                    {"ok": True, "result": {"message_id": 792}},
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
            video_url="https://video.twimg.com/broken.mp4",
            photo_url="https://example.com/poster.jpg",
        )

        self.assertTrue(session.calls[0][0].endswith("/sendMediaGroup"))
        self.assertTrue(session.calls[1][0].endswith("/sendVideo"))
        self.assertTrue(session.calls[2][0].endswith("/sendPhoto"))
        self.assertEqual(receipt.message_id, 792)
        self.assertEqual(receipt.mode, "foto")
        self.assertTrue(receipt.video_fallback)
        self.assertFalse(receipt.photo_fallback)


if __name__ == "__main__":
    unittest.main()
