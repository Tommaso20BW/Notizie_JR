import unittest
from datetime import date, datetime
from unittest import mock

import requests

import juve_press_bot as bot
from telegram_notifier import format_article_message, source_emoji, source_link_label

BASE = "https://www.ilbianconero.com"
TODAY_URL = f"{BASE}/liste/juventus-thuram-infortunio-recupero/blt2b3bcaaa14472602"
LATE_URL = f"{BASE}/liste/juventus-omonia-dove-vederla-tv-streaming/blt1f344f00326042c5"
OLD_URL = f"{BASE}/liste/varela-gol-confronto-attaccanti/bltea692f843f635fef"
NO_STAMP_URL = f"{BASE}/liste/juventus-notizia-senza-orario/blt0000000000000001"
FAILING_URL = f"{BASE}/liste/juventus-pagina-non-raggiungibile/blt0000000000000002"

LISTING_HTML = f"""
<html><body>
<nav>
  <a href="/notizie">Tutte le notizie</a>
  <a href="/squadra/juventus/7w2y7s7q5r1h1flz9y4sj6xka">Women</a>
  <a href="/mercato/1/k94w8e1yy9ch14mllpf4srnks">Mercato</a>
</nav>
<main>
  <article>
    <a href="{TODAY_URL}"><img alt="GOAL GFX HD" src="/x.jpg"></a>
    <div>
      <span>Juventus</span>
      <a href="{TODAY_URL}"><h3>Titolo di oggi troncato...</h3></a>
      <p>18:2425/11/2026</p>
    </div>
  </article>
  <article>
    <div>
      <span>Altre notizie</span>
      <a href="{LATE_URL}?utm_source=home#top"><h3>cm grafica omonia</h3></a>
      <p><span>23:48</span> <span>24/11/2026</span></p>
    </div>
  </article>
  <article>
    <div>
      <a href="{OLD_URL}"><h3>Notizia di due giorni fa</h3></a>
      <p>21:4923/11/2026</p>
    </div>
  </article>
  <article>
    <div>
      <a href="{NO_STAMP_URL}"><h3>Senza orario nella card</h3></a>
    </div>
  </article>
  <article>
    <div>
      <a href="{FAILING_URL}"><h3>Pagina che non risponde</h3></a>
      <p>10:0925/11/2026</p>
    </div>
  </article>
</main>
</body></html>
"""

DETAIL_TEMPLATE = """
<html><head>
<title>{title} | Il BiancoNero</title>
<meta property="og:title" content="{title} | Il BiancoNero">
<meta property="og:description" content="Sommario l&#039;articolo">
<meta property="og:image" content="https://assets.ilbianconero.com/images/v3/{slug}/Foto(1).jpg">
</head><body><article><h1>{title}</h1>
<p>{visible_date}</p></article></body></html>
"""

DETAILS = {
    TODAY_URL: ("Thuram, infortunio e recupero: quando torna", "slug-today", ""),
    LATE_URL: ("Juventus-Omonia: dove vederla", "slug-late", ""),
    NO_STAMP_URL: ("Notizia senza orario", "slug-nostamp", "25 nov 2026, 09:05"),
}


class _Response:
    def __init__(self, text):
        self.text = text
        self.encoding = None

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self):
        self.calls = []

    def get(self, url, timeout=0):
        self.calls.append(url)
        if url == bot.ILBIANCONERO_LISTING_URL:
            return _Response(LISTING_HTML)
        if url == FAILING_URL:
            raise requests.ConnectionError("giù")
        title, slug, visible_date = DETAILS[url]
        return _Response(
            DETAIL_TEMPLATE.format(
                title=title, slug=slug, visible_date=visible_date
            )
        )


REQUESTED = {date(2026, 11, 25), date(2026, 11, 24)}


class IlBianconeroScraperTests(unittest.TestCase):
    def setUp(self):
        bot.ILBIANCONERO_DETAIL_CACHE.clear()

    def _scrape(self, session=None, requested=REQUESTED):
        session = session or _Session()
        return session, bot.scrape_ilbianconero(session, requested)

    def test_takes_requested_days_with_full_title_summary_and_image(self):
        session, articles = self._scrape()

        by_url = {article.url: article for article in articles}
        self.assertEqual(
            set(by_url), {TODAY_URL, LATE_URL, NO_STAMP_URL}
        )
        today = by_url[TODAY_URL]
        self.assertEqual(today.source, "Il Bianconero")
        # Titolo completo dalla pagina, senza il suffisso del sito.
        self.assertEqual(today.title, "Thuram, infortunio e recupero: quando torna")
        self.assertEqual(today.summary, "Sommario l'articolo")
        self.assertEqual(
            today.image_url,
            "https://assets.ilbianconero.com/images/v3/slug-today/Foto(1).jpg",
        )
        self.assertEqual(today.published.strftime("%d/%m %H:%M"), "25/11 18:24")
        self.assertEqual(today.published.tzinfo, bot.ROME)

    def test_listing_stamp_formats_and_query_string_removed(self):
        _, articles = self._scrape()
        by_url = {article.url: article for article in articles}
        # Orario e data separati in due tag, link con query e frammento.
        self.assertEqual(
            by_url[LATE_URL].published.strftime("%d/%m %H:%M"), "24/11 23:48"
        )

    def test_date_outside_requested_days_is_never_opened(self):
        session, articles = self._scrape()
        self.assertNotIn(OLD_URL, session.calls)
        self.assertNotIn(OLD_URL, {article.url for article in articles})

    def test_non_article_links_ignored(self):
        session, _ = self._scrape()
        opened = [url for url in session.calls if url != bot.ILBIANCONERO_LISTING_URL]
        self.assertTrue(all("/blt" in url for url in opened))

    def test_card_without_stamp_uses_visible_date_of_article(self):
        _, articles = self._scrape()
        article = next(a for a in articles if a.url == NO_STAMP_URL)
        self.assertEqual(article.published.strftime("%d/%m %H:%M"), "25/11 09:05")

    def test_card_without_stamp_outside_requested_days_is_dropped(self):
        _, articles = self._scrape(requested={date(2026, 11, 24)})
        self.assertEqual({a.url for a in articles}, {LATE_URL})

    def test_transient_error_skips_article_without_failing_source(self):
        session, articles = self._scrape()
        self.assertIn(FAILING_URL, session.calls)
        self.assertNotIn(FAILING_URL, {article.url for article in articles})

    def test_detail_pages_opened_once_across_cycles(self):
        session = _Session()
        bot.scrape_ilbianconero(session, REQUESTED)
        first_round = [u for u in session.calls if u in DETAILS]
        bot.scrape_ilbianconero(session, REQUESTED)
        second_round = [u for u in session.calls if u in DETAILS]
        self.assertEqual(len(first_round), 3)
        self.assertEqual(second_round, first_round)

    def test_listing_error_propagates(self):
        class _Down:
            def get(self, url, timeout=0):
                raise requests.ConnectionError("giù")

        with self.assertRaises(requests.ConnectionError):
            bot.scrape_ilbianconero(_Down(), REQUESTED)

    def test_registered_among_scrapers(self):
        names = [name for name, _ in bot._article_scrapers()]
        self.assertIn("Il Bianconero", names)

    def test_article_path_pattern(self):
        self.assertTrue(bot._ilbianconero_article_url(TODAY_URL))
        self.assertEqual(
            bot._ilbianconero_article_url("/liste/x/blt10e965db312d14c4?a=1"),
            f"{BASE}/liste/x/blt10e965db312d14c4",
        )
        for href in (
            "/notizie",
            "/notizie/2",
            "/squadra/juventus/7w2y7s7q5r1h1flz9y4sj6xka",
            "https://altrosito.com/liste/x/blt10e965db312d14c4",
        ):
            self.assertEqual(bot._ilbianconero_article_url(href), "")


class IlBianconeroMessageTests(unittest.TestCase):
    def test_custom_emoji_and_label(self):
        self.assertEqual(source_link_label("Il Bianconero"), "Apri notizia")
        self.assertEqual(
            source_emoji("Il Bianconero"),
            '<tg-emoji emoji-id="5972294171325636771">📰</tg-emoji>',
        )

    def test_message_header(self):
        article = mock.Mock(
            source="Il Bianconero",
            title="Titolo",
            url=TODAY_URL,
            summary="",
        )
        message = format_article_message(article)
        self.assertTrue(
            message.startswith(
                '<tg-emoji emoji-id="5972294171325636771">📰</tg-emoji> '
                "<b>Il Bianconero</b>"
            )
        )
        self.assertIn("Apri notizia", message)


if __name__ == "__main__":
    unittest.main()
