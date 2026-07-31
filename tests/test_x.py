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


if __name__ == "__main__":
    unittest.main()
