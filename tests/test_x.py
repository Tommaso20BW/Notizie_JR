import unittest
from datetime import date
from unittest.mock import patch

import juve_press_bot as bot


class FeedResponse:
    content = b"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<rss version=\"2.0\">
  <channel>
    <item>
      <title>#Juventus tratta con @Reporter: forza #Juve</title>
      <pubDate>Fri, 31 Jul 2026 10:00:00 GMT</pubDate>
      <guid>2083000000000000000</guid>
      <link>https://nitter.example/Reporter/status/2083000000000000000</link>
    </item>
  </channel>
</rss>
"""

    def raise_for_status(self):
        return None


class JsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    headers = {}


class XTests(unittest.TestCase):
    def test_clean_x_text_removes_only_hash_and_at_symbols(self):
        self.assertEqual(
            bot.clean_x_text("#Juventus con @Reporter: 2-1!"),
            "Juventus con Reporter: 2-1!",
        )

    def test_scraper_filters_raw_text_then_cleans_x_notification(self):
        accounts = (
            {
                "handle": "Reporter",
                "filter_juventus": True,
                "include_reposts": False,
            },
        )
        mirrors = ("https://nitter.example/{handle}/rss",)

        with (
            patch.object(bot, "X_ACCOUNTS", accounts),
            patch.object(bot, "X_RSS_MIRROR_TEMPLATES", mirrors),
            patch.object(bot.requests, "get", return_value=FeedResponse()),
        ):
            articles = bot.scrape_x_profiles(
                FakeSession(),
                {date(2026, 7, 31)},
            )

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].source, "X - Reporter")
        self.assertEqual(
            articles[0].title,
            "Juventus tratta con Reporter: forza Juve",
        )
        self.assertNotIn("#", articles[0].title)
        self.assertNotIn("@", articles[0].title)

    def test_native_x_video_is_resolved_to_telegram_sized_mp4(self):
        video_feed = b"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<rss version=\"2.0\">
  <channel>
    <item>
      <title>Video Juventus</title>
      <pubDate>Fri, 31 Jul 2026 10:00:00 GMT</pubDate>
      <guid>2083000000000000001</guid>
      <link>https://nitter.example/Reporter/status/2083000000000000001</link>
      <description><![CDATA[
        <p>Video Juventus</p>
        <a href=\"https://nitter.example/Reporter/status/2083000000000000001#m\">
          <br>Video<br>
          <img src=\"https://nitter.example/pic/poster.jpg\" />
        </a>
      ]]></description>
    </item>
  </channel>
</rss>
"""
        feed_response = FeedResponse()
        feed_response.content = video_feed
        media_response = JsonResponse(
            {
                "tweet": {
                    "media": {
                        "videos": [
                            {
                                "duration": 120,
                                "thumbnail_url": (
                                    "https://pbs.twimg.com/poster.jpg"
                                ),
                                "formats": [
                                    {
                                        "url": "https://video.twimg.com/low.mp4",
                                        "container": "mp4",
                                        "bitrate": 288_000,
                                    },
                                    {
                                        "url": "https://video.twimg.com/high.mp4",
                                        "container": "mp4",
                                        "bitrate": 2_176_000,
                                    },
                                ],
                            }
                        ]
                    }
                }
            }
        )
        accounts = (
            {
                "handle": "Reporter",
                "filter_juventus": True,
                "include_reposts": False,
            },
        )
        mirrors = ("https://nitter.example/{handle}/rss",)

        with (
            patch.object(bot, "X_ACCOUNTS", accounts),
            patch.object(bot, "X_RSS_MIRROR_TEMPLATES", mirrors),
            patch.object(
                bot.requests,
                "get",
                side_effect=[feed_response, media_response],
            ),
        ):
            articles = bot.scrape_x_profiles(
                FakeSession(),
                {date(2026, 7, 31)},
            )

        self.assertEqual(len(articles), 1)
        self.assertEqual(
            articles[0].video_url,
            "https://video.twimg.com/low.mp4",
        )
        self.assertEqual(articles[0].image_url, "")
        self.assertEqual(
            articles[0].video_thumbnail_url,
            "https://pbs.twimg.com/poster.jpg",
        )

    def test_x_post_with_video_and_photos_keeps_both_media_types(self):
        payload = {
            "tweet": {
                "media": {
                    "videos": [
                        {
                            "url": "https://video.twimg.com/clip.mp4",
                            "thumbnail_url": "https://pbs.twimg.com/poster.jpg",
                        }
                    ],
                    "photos": [
                        {"url": "https://pbs.twimg.com/photo-1.jpg"},
                        {"url": "https://pbs.twimg.com/photo-2.jpg"},
                    ],
                }
            }
        }

        media = bot._x_media_from_payload(payload)

        self.assertEqual(media.video_url, "https://video.twimg.com/clip.mp4")
        self.assertEqual(
            media.image_urls,
            (
                "https://pbs.twimg.com/photo-1.jpg",
                "https://pbs.twimg.com/photo-2.jpg",
            ),
        )
        self.assertEqual(
            media.video_thumbnail_url,
            "https://pbs.twimg.com/poster.jpg",
        )

    def test_embedded_mp4_is_used_without_media_api(self):
        item = bot.ET.fromstring(
            """<item><description><![CDATA[
            <video poster=\"poster.jpg\"><source src=\"clip.mp4\" type=\"video/mp4\"></video>
            ]]></description></item>"""
        )

        self.assertEqual(
            bot._rss_item_video_url(item, "https://nitter.example/user/status/1"),
            "https://nitter.example/user/status/clip.mp4",
        )

    def test_video_enclosure_is_not_mistaken_for_a_photo(self):
        item = bot.ET.fromstring(
            """<item><enclosure url=\"https://cdn.example/clip.mp4\"
            type=\"video/mp4\" /></item>"""
        )

        self.assertEqual(bot._rss_item_images(item), [])
        self.assertEqual(
            bot._rss_item_video_url(item),
            "https://cdn.example/clip.mp4",
        )


if __name__ == "__main__":
    unittest.main()
