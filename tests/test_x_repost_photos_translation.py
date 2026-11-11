import os
import unittest
from datetime import date
from unittest.mock import patch

import juve_press_bot as bot
import telegram_notifier as tn
import translator

FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>Juventus, accordo vicino per il nuovo attaccante</title>
    <pubDate>Fri, 31 Jul 2026 10:00:00 GMT</pubDate>
    <guid>2083000000000000001</guid>
    <link>https://fxtwitter.com/Reporter/status/2083000000000000001</link>
  </item>
  <item>
    <title>Juventus, un altro giornalista ne parla</title>
    <pubDate>Fri, 31 Jul 2026 10:05:00 GMT</pubDate>
    <guid>2083000000000000002</guid>
    <link>https://fxtwitter.com/AltroGiornalista/status/2083000000000000002</link>
  </item>
</channel></rss>
"""


class Response:
    def __init__(self, payload=None, content=b""):
        self.payload, self.content = payload, content

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Session:
    headers = {}


class RepostTests(unittest.TestCase):
    def test_repost_is_recognised_from_the_original_author_in_the_link(self):
        accounts = ({"handle": "Reporter", "filter_juventus": False},)
        with patch.object(bot, "X_ACCOUNTS", accounts), \
                patch.object(bot, "X_RSS_MIRROR_TEMPLATES", ("https://feed.example/{handle}",)), \
                patch.object(bot.requests, "get", return_value=Response(content=FEED)), \
                patch.object(bot, "_download_x_feed", return_value=FEED):
            articles = bot.scrape_x_profiles(Session(), {date(2026, 7, 31)})
        self.assertEqual([a.state_key for a in articles], ["x:Reporter:2083000000000000001"])


class PhotoTests(unittest.TestCase):
    def test_x_photos_are_requested_as_large_jpg(self):
        self.assertEqual(
            bot.x_photo_url("https://pbs.twimg.com/media/HWrNU5rXEAErWCt.png?name=orig"),
            "https://pbs.twimg.com/media/HWrNU5rXEAErWCt?format=jpg&name=large",
        )
        other = "https://pbs.twimg.com/amplify_video_thumb/1/img/a.jpg"
        self.assertEqual(bot.x_photo_url(other), other)

    def test_media_api_returns_photos_when_there_is_no_video(self):
        payload = {"tweet": {"media": {"photos": [{"url": "https://pbs.twimg.com/media/ABC.jpg"}]}}}
        with patch.object(bot.requests, "get", return_value=Response(payload)):
            media = bot._resolve_x_media("123")
        self.assertEqual(media.image_urls, ("https://pbs.twimg.com/media/ABC?format=jpg&name=large",))
        self.assertEqual(media.video_url, "")


class TranslationTests(unittest.TestCase):
    def test_google_result_is_used_and_italian_is_left_alone(self):
        translator.translate_to_italian.cache_clear()
        english = [[["Accordo raggiunto con la Juventus.", "Agreement reached with Juventus.", None]], None, "en"]
        with patch.object(translator.requests, "get", return_value=Response(english)), \
                patch.dict(os.environ, {"DEEPL_API_KEY": ""}):
            result = translator.translate_to_italian("Agreement reached with Juventus.")
        self.assertEqual((result.text, result.language_name), ("Accordo raggiunto con la Juventus.", "inglese"))

        translator.translate_to_italian.cache_clear()
        italian = [[["Accordo raggiunto.", "Accordo raggiunto.", None]], None, "it"]
        with patch.object(translator.requests, "get", return_value=Response(italian)), \
                patch.dict(os.environ, {"DEEPL_API_KEY": ""}):
            self.assertIsNone(translator.translate_to_italian("Accordo raggiunto con la Juventus."))

    def test_second_google_address_is_used_when_the_first_fails(self):
        translator.translate_to_italian.cache_clear()
        alt = [["Accordo raggiunto con la Juventus.", "en"]]

        def fake_get(url, **kwargs):
            if url == translator.GOOGLE_URL:
                raise translator.requests.ConnectionError("bloccato")
            return Response(alt)

        with patch.object(translator.requests, "get", side_effect=fake_get),                 patch.object(translator, "RETRY_SECONDS", 0),                 patch.dict(os.environ, {"DEEPL_API_KEY": ""}):
            result = translator.translate_to_italian("Agreement reached with Juventus.")
        self.assertEqual((result.text, result.language), ("Accordo raggiunto con la Juventus.", "en"))

    def test_translated_x_post_shows_italian_text_and_original_language(self):
        article = tn._MessageArticle(source="X - FabrizioRomano", title="Here we go!",
                                     url="https://x.com/FabrizioRomano/status/1", summary="")
        translation = translator.Translation("Ci siamo!", "en")
        with patch.dict(os.environ, {"TRANSLATE_X_POSTS": "1"}), \
                patch.object(tn, "translate_to_italian", return_value=translation):
            translated = tn._translate_x(article)
        self.assertEqual(translated.title, "Ci siamo!")
        html = tn.format_article_rich_html(translated)
        self.assertIn("Ci siamo!", html)
        self.assertIn("X - FabrizioRomano 🌐</h2>", html)          # globo accanto alla fonte
        self.assertNotIn("Tradotto", html)                               # nessuna riga sotto

    def test_translation_is_off_without_the_switch(self):
        article = tn._MessageArticle(source="X - FabrizioRomano", title="Here we go!",
                                     url="https://x.com/FabrizioRomano/status/1", summary="")
        with patch.dict(os.environ, {"TRANSLATE_X_POSTS": ""}):
            self.assertIs(tn._translate_x(article), article)


class FallbackAndRotationTests(unittest.TestCase):
    def test_profile_api_is_turned_into_feed_items(self):
        results = [
            {"type": "status", "id": "1", "url": "https://x.com/Reporter/status/1", "text": "Juve, ci siamo",
             "created_at": "Mon Nov 09 16:00:02 +0000 2026",
             "media": {"all": [{"type": "photo", "url": "https://pbs.twimg.com/media/A.jpg"}]}},
            {"type": "status", "id": "2", "url": "https://x.com/Altro/status/2", "text": "Repost",
             "created_at": "Mon Nov 09 16:01:02 +0000 2026", "reposted_by": {"screen_name": "Reporter"}},
            {"type": "status", "id": "3", "url": "https://x.com/Reporter/status/3", "text": "Risposta",
             "created_at": "Mon Nov 09 16:02:02 +0000 2026", "replying_to": {"screen_name": "x"}},
        ]
        items = bot._x_statuses_to_rss(results).find("channel").findall("item")
        self.assertEqual([i.findtext("guid") for i in items], ["1", "2"])
        self.assertEqual(bot._rss_item_images(items[0]), ["https://pbs.twimg.com/media/A.jpg"])
        self.assertTrue(bot.X_REPOST_RE.match(items[1].findtext("title")))

    def test_broken_feed_falls_back_to_profile_api(self):
        api = Response({"results": [{"type": "status", "id": "9", "url": "https://x.com/reporter/status/9",
                                     "text": "Notizia", "created_at": "Mon Nov 09 16:00:02 +0000 2026"}]})
        api.status_code = 200
        with patch.object(bot, "_download_x_feed", return_value=None),                 patch.object(bot.requests, "get", return_value=api):
            root = bot._download_first_x_feed("Reporter", {})
        self.assertEqual(root.find("channel").find("item").findtext("guid"), "9")

    def test_rotation_spreads_accounts_and_waits_longer_after_failures(self):
        accounts = ({"handle": "A", "filter_juventus": False}, {"handle": "B", "filter_juventus": False})
        with patch.object(bot, "X_ACCOUNTS", accounts), patch.dict(bot.X_ROTATION, {"enabled": True}),                 patch.object(bot, "_X_SCHEDULE", {}):
            self.assertEqual(len(bot._x_accounts_due(now=0)), 2)       # primo giro: tutti
            bot._x_schedule_next("A", ok=True, now=0)
            bot._x_schedule_next("B", ok=False, now=0)
            self.assertEqual(bot._x_accounts_due(now=59), [])
            self.assertEqual([a["handle"] for a in bot._x_accounts_due(now=61)], ["A"])
            bot._x_schedule_next("B", ok=False, now=0)                  # seconda volta vuoto: 4 minuti
            self.assertEqual(bot._X_SCHEDULE["B"], (240, 2))


class SinglePhotoFallbackTests(unittest.TestCase):
    def test_one_lighter_photo_is_tried_before_text_only(self):
        client = tn.TelegramClient("t", "c")
        sent = []

        def fake(article, **kwargs):
            sent.append(kwargs.get("photo_urls", []))
            if len(sent) == 1:
                raise tn.TelegramDeliveryError("foto rifiutata")
            return 7

        article = tn._MessageArticle(source="Sito", title="Titolo", url="https://a.it", summary="")
        with patch.object(client, "_send_rich_article", side_effect=fake):
            receipt = client.send_article(article, photo_urls=[
                "https://pbs.twimg.com/media/A?format=jpg&name=large", "https://pbs.twimg.com/media/B.jpg"])
        self.assertEqual(sent[1], ["https://pbs.twimg.com/media/A?format=jpg&name=medium"])
        self.assertEqual(receipt.mode, "rich_one_photo")


if __name__ == "__main__":
    unittest.main()
