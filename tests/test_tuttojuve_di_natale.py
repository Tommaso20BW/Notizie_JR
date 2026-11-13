import unittest
from datetime import date, datetime
from unittest import mock

import juve_press_bot as bot
from telegram_notifier import format_article_message, source_emoji, source_link_label

LISTING_HTML = """
<div class="tcc-border solid upper">notizie di Mirko Di Natale</div>
<div class="tcc-border upper date">Venerdì 13 novembre</div>
<div class="tcc-list-news"><div class="pre-0000FF">
<span class="hh serif">21:10</span><a href="/primo-piano/oggi-uno-800500"><span><span class="tcc-badge tcc-label">Esclusiva TJ</span></span>Titolo di oggi troncato...</a>
</div><div class="pre-0000FF">
<span class="hh serif">09:30</span><a href="/primo-piano/oggi-live-800490"><span><span class="tcc-badge tcc-label">Live</span></span>Juventus-Roma 1-0 - Finita</a>
</div></div>
<div class="tcc-border upper date">Giovedì 12 novembre</div>
<div class="tcc-list-news"><div class="pre-0000FF">
<span class="hh serif">19:00</span><a href="/primo-piano/ieri-800357"><span><span class="tcc-badge tcc-label">Esclusiva TJ</span></span>Notizia di ieri</a>
</div></div>
<a href="https://www.tuttojuve.com/firme-bianconere/riquadro-laterale-800379">Riquadro laterale</a>
<ol><li><a href="https://www.tuttojuve.com/calciomercato/piu-letta-800373">1 Più letta</a></li></ol>
"""

DETAIL_TEMPLATE = """
<html><head>
<meta property="og:title" content="{title}">
<meta property="og:description" content="Sommario l&#039;articolo">
<meta property="og:image" content="https://example.com/{slug}.jpg">
</head><body></body></html>
"""

ARTICLE_TITLES = {
    "https://www.tuttojuve.com/primo-piano/oggi-uno-800500": "Titolo di oggi completo",
    "https://www.tuttojuve.com/primo-piano/oggi-live-800490": "Juventus-Roma 1-0 - Finita",
}


class _Response:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self):
        self.calls = []

    def get(self, url, timeout=0):
        self.calls.append(url)
        if url == bot.TUTTOJUVE_DI_NATALE_URL:
            return _Response(LISTING_HTML)
        return _Response(
            DETAIL_TEMPLATE.format(title=ARTICLE_TITLES[url], slug=url[-6:])
        )


def _fake_now(year, month, day, hour=21, minute=44):
    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(year, month, day, hour, minute, tzinfo=tz)

    return mock.patch.object(bot, "datetime", FakeDateTime)


class TuttoJuveDiNataleTests(unittest.TestCase):
    def setUp(self):
        bot.TUTTOJUVE_DETAIL_CACHE.clear()

    def test_takes_only_todays_articles_with_full_title(self):
        session = _Session()
        with _fake_now(2026, 11, 13):
            articles = bot.scrape_tuttojuve_di_natale(
                session, {date(2026, 11, 13), date(2026, 11, 12)}
            )

        self.assertEqual(
            [article.title for article in articles],
            ["Titolo di oggi completo", "Juventus-Roma 1-0 - Finita"],
        )
        self.assertEqual(
            [article.published.strftime("%d/%m %H:%M") for article in articles],
            ["13/11 21:10", "13/11 09:30"],
        )
        self.assertTrue(
            all(article.source == "Mirko Di Natale - TuttoJuve" for article in articles)
        )
        self.assertEqual(articles[0].image_url, "https://example.com/800500.jpg")
        self.assertEqual(articles[0].summary, "Sommario l'articolo")
        # Ieri e riquadri laterali non vengono nemmeno aperti.
        self.assertEqual(len(session.calls), 3)

    def test_nothing_when_no_article_today(self):
        with _fake_now(2026, 11, 14):
            articles = bot.scrape_tuttojuve_di_natale(
                _Session(), {date(2026, 11, 14), date(2026, 11, 13)}
            )
        self.assertEqual(articles, [])

    def test_skipped_when_today_not_requested(self):
        session = _Session()
        with _fake_now(2026, 11, 13):
            articles = bot.scrape_tuttojuve_di_natale(session, {date(2026, 11, 12)})
        self.assertEqual(articles, [])
        self.assertEqual(session.calls, [])

    def test_registered_among_scrapers(self):
        names = [name for name, _ in bot._article_scrapers()]
        self.assertIn("Mirko Di Natale - TuttoJuve", names)


class TuttoJuveDiNataleMessageTests(unittest.TestCase):
    def test_custom_emoji_and_label(self):
        source = "Mirko Di Natale - TuttoJuve"
        self.assertEqual(
            source_emoji(source),
            '<tg-emoji emoji-id="5810081494302992771">📲</tg-emoji>',
        )
        self.assertEqual(source_link_label(source), "Apri notizia")

    def test_message_header(self):
        article = mock.Mock(
            source="Mirko Di Natale - TuttoJuve",
            title="Titolo",
            url="https://www.tuttojuve.com/primo-piano/x-800500",
            summary="",
        )
        message = format_article_message(article)
        self.assertTrue(
            message.startswith(
                '<tg-emoji emoji-id="5810081494302992771">📲</tg-emoji> '
                "<b>Mirko Di Natale - TuttoJuve</b>"
            )
        )


if __name__ == "__main__":
    unittest.main()
