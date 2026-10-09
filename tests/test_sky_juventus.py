import json
import threading
import time
import unittest
from datetime import date, datetime
from unittest import mock
from xml.etree import ElementTree as ET

import requests

import juve_press_bot as bot

TODAY = date(2026, 10, 9)
TODAY_PUB = "Fri, 09 Oct 2026 10:00:00 +0200"
YESTERDAY_PUB = "Thu, 08 Oct 2026 21:00:00 +0200"
BASE = "https://sport.sky.it/calcio/serie-a/2026/10/09/"
CALCIO, SERIE_A = bot.SKY_JUVENTUS_RSS_URLS


class FakeResponse:
    def __init__(self, *, content=b"", text="", status=200):
        self.content = content
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(str(self.status_code))
            error.response = self
            raise error


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, timeout):
        self.calls.append(url)
        route = self.routes.get(url)
        if callable(route):
            return route()
        return route or FakeResponse(status=404)


def rss(*links, pub=TODAY_PUB):
    items = "".join(
        f"<item><title>Titolo {index}</title><link>{link}</link>"
        f"<pubDate>{pub}</pubDate><description>Sommario {index}</description>"
        "</item>"
        for index, link in enumerate(links)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>Sky</title>{items}</channel></rss>"
    ).encode()


def rss_item(link, date_tags):
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<item><title>Titolo</title><link>{link}</link>{date_tags}</item>"
        "</channel></rss>"
    ).encode()


def page(title, description, body="", keywords=None, about=None):
    data = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": title,
        "description": description,
        "datePublished": "2026-10-09T10:00:00+02:00",
        "articleBody": body,
    }
    if keywords is not None:
        data["keywords"] = keywords
    if about is not None:
        data["about"] = about
    return FakeResponse(
        text=(
            "<html><head><script type='application/ld+json'>"
            f"{json.dumps(data)}</script></head><body></body></html>"
        )
    )


def juve_page():
    return page(
        "Juventus, accordo per il nuovo attaccante",
        "La Juventus ha chiuso la trattativa.",
        "La Juventus punta sul rinforzo. " * 2,
    )


def make_session(pages=None, *, calcio=None, serie_a=None):
    """Sessione simulata: i due feed rispondono con i contenuti indicati."""
    routes = {
        CALCIO: FakeResponse(content=calcio if calcio is not None else rss()),
        SERIE_A: FakeResponse(content=serie_a if serie_a is not None else rss()),
    }
    routes.update(pages or {})
    return FakeSession(routes)


class SkyJuventusRssTests(unittest.TestCase):
    def setUp(self):
        bot.SKY_JUVENTUS_CHECKED_URLS.clear()
        self.addCleanup(bot.SKY_JUVENTUS_CHECKED_URLS.clear)
        for patcher in (
            mock.patch.object(bot, "SKY_JUVENTUS_SEEN_KEYS", None),
            mock.patch.object(bot, "_sky_today", return_value=TODAY),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def scrape(self, session, requested=None):
        return bot.scrape_sky_juventus_news(session, requested or {TODAY})

    # --- feed e data -------------------------------------------------------

    def test_both_feeds_are_queried(self):
        session = make_session()
        self.assertEqual(self.scrape(session), [])
        self.assertCountEqual(session.calls, [CALCIO, SERIE_A])

    def test_articles_from_each_feed_are_collected(self):
        only_calcio = BASE + "solo-calcio"
        only_serie_a = BASE + "solo-serie-a"
        session = make_session(
            {only_calcio: juve_page(), only_serie_a: juve_page()},
            calcio=rss(only_calcio),
            serie_a=rss(only_serie_a),
        )
        self.assertCountEqual(
            [a.url for a in self.scrape(session)], [only_calcio, only_serie_a]
        )

    def test_yesterday_and_older_articles_are_excluded_in_both_feeds(self):
        old = BASE + "vecchio"
        session = make_session(
            {old: juve_page()},
            calcio=rss(old, pub=YESTERDAY_PUB),
            serie_a=rss(old, pub="Mon, 05 Oct 2026 10:00:00 +0200"),
        )
        self.assertEqual(self.scrape(session), [])
        self.assertNotIn(old, session.calls)

    def test_requested_yesterday_does_not_reopen_the_door(self):
        old = BASE + "ieri-sera"
        session = make_session(
            {old: juve_page()}, serie_a=rss(old, pub=YESTERDAY_PUB)
        )
        self.assertEqual(
            self.scrape(session, {date(2026, 10, 8), TODAY}), []
        )
        self.assertNotIn(old, session.calls)

    def test_missing_or_unparsable_date_is_excluded(self):
        no_date = BASE + "senza-data"
        bad_date = BASE + "data-rotta"
        session = make_session(
            {no_date: juve_page(), bad_date: juve_page()},
            calcio=rss_item(no_date, ""),
            serie_a=rss_item(bad_date, "<pubDate>ieri, circa</pubDate>"),
        )
        self.assertEqual(self.scrape(session), [])
        self.assertNotIn(no_date, session.calls)
        self.assertNotIn(bad_date, session.calls)

    def test_first_publication_date_wins_over_updated_in_any_order(self):
        first = BASE + "pubdate-poi-updated"
        second = BASE + "updated-poi-pubdate"
        session = make_session(
            {first: juve_page(), second: juve_page()},
            calcio=rss_item(
                first,
                f"<pubDate>{YESTERDAY_PUB}</pubDate>"
                "<updated>2026-10-09T09:00:00+02:00</updated>",
            ),
            serie_a=rss_item(
                second,
                "<updated>2026-10-09T09:00:00+02:00</updated>"
                f"<pubDate>{YESTERDAY_PUB}</pubDate>",
            ),
        )
        # Pubblicati ieri, aggiornati oggi: devono restare esclusi.
        self.assertEqual(self.scrape(session), [])

        today_first = BASE + "oggi-aggiornato-oggi"
        session = make_session(
            {today_first: juve_page()},
            serie_a=rss_item(
                today_first,
                "<updated>2026-10-07T09:00:00+02:00</updated>"
                f"<pubDate>{TODAY_PUB}</pubDate>",
            ),
        )
        self.assertEqual([a.url for a in self.scrape(session)], [today_first])

    def test_date_is_evaluated_in_rome_timezone(self):
        # 23:30 UTC dell'8 ottobre = 01:30 del 9 ottobre a Roma.
        url = BASE + "dopo-mezzanotte"
        session = make_session(
            {url: juve_page()},
            serie_a=rss(url, pub="Thu, 08 Oct 2026 23:30:00 +0000"),
        )
        self.assertEqual([a.url for a in self.scrape(session)], [url])

    def test_date_change_during_execution_uses_the_current_day(self):
        url = BASE + "ancora-di-ieri"
        session = make_session({url: juve_page()}, serie_a=rss(url))
        both_days = {TODAY, date(2026, 10, 10)}
        with mock.patch.object(bot, "_sky_today", return_value=date(2026, 10, 10)):
            self.assertEqual(self.scrape(session, both_days), [])
            # Il giorno dopo non è tra le date richieste: nessuna richiesta.
            fresh = make_session()
            self.assertEqual(self.scrape(fresh, {TODAY}), [])
            self.assertEqual(fresh.calls, [])

    # --- filtro Juventus ---------------------------------------------------

    def test_relevant_juventus_article_is_accepted(self):
        url = BASE + "juve-mercato"
        session = make_session({url: juve_page()}, serie_a=rss(url))
        articles = self.scrape(session)

        self.assertEqual([a.url for a in articles], [url])
        self.assertEqual(articles[0].source, "Sky Sport - Juventus")
        self.assertIsInstance(articles[0].published, datetime)

    def test_other_team_article_is_discarded_from_both_feeds(self):
        url = BASE + "napoli-news"
        other = BASE + "milan-news"
        session = make_session(
            {
                url: page(
                    "Napoli, Conte recupera Anguissa",
                    "Il Napoli prepara la sfida di domenica.",
                    "Il Napoli lavora a Castel Volturno.",
                ),
                other: page("Milan, Allegri carica", "Il Milan si allena."),
            },
            calcio=rss(other),
            serie_a=rss(url),
        )
        self.assertEqual(self.scrape(session), [])

    def test_marginal_juventus_mention_is_discarded(self):
        url = BASE + "roma-news"
        session = make_session(
            {
                url: page(
                    "Roma, Gasperini prepara il turno",
                    "La Roma si allena a Trigoria.",
                    "Domenica la Roma affronta la Juventus. Il resto del "
                    "servizio riguarda la formazione giallorossa.",
                )
            },
            calcio=rss(url),
        )
        self.assertEqual(self.scrape(session), [])

    def test_title_without_juventus_is_judged_on_summary_and_body(self):
        title = "Tudor in conferenza: «Vlahovic sta bene, domani si gioca»"
        self.assertFalse(bot.is_juventus_title(title))
        url = BASE + "conferenza"
        session = make_session(
            {
                url: page(
                    title,
                    "Il tecnico dei bianconeri presenta la partita.",
                    "La Juventus gioca domani. La Juventus ritrova il "
                    "centravanti. Per la Juventus è una sfida importante.",
                    keywords=["Juventus", "Serie A"],
                )
            },
            calcio=rss(url),
        )
        self.assertEqual([a.url for a in self.scrape(session)], [url])

    def test_keywords_about_and_body_accept_non_string_formats(self):
        url = BASE + "formati"
        session = make_session(
            {
                url: page(
                    "Infortunio in casa bianconera, i tempi di recupero",
                    "Esami per il difensore.",
                    ["La Juventus attende gli esiti. ", "La Juventus decide."],
                    keywords=["Calcio", {"name": "Juventus"}],
                    about=[{"@type": "Thing", "name": "Juventus"}, 3, None],
                )
            },
            serie_a=rss(url),
        )
        self.assertEqual([a.url for a in self.scrape(session)], [url])

    def test_juventus_women_and_next_gen_are_excluded(self):
        for index, title in enumerate(
            ("Juventus Women, vittoria in campionato", "Juve Next Gen ko")
        ):
            with self.subTest(title=title):
                url = BASE + f"altre-squadre-{index}"
                session = make_session(
                    {url: page(title, title, "Le bianchenere e la squadra.")},
                    serie_a=rss(url),
                )
                self.assertEqual(self.scrape(session), [])

    # --- doppioni e stato --------------------------------------------------

    def test_same_article_in_both_feeds_is_handled_once(self):
        url = BASE + "doppione"
        session = make_session({url: juve_page()}, calcio=rss(url), serie_a=rss(url))
        articles = self.scrape(session)

        self.assertEqual([a.url for a in articles], [url])
        self.assertEqual(session.calls.count(url), 1)

    def test_url_variants_of_the_same_article_are_recognised(self):
        url = BASE + "variante"
        session = make_session(
            {url: juve_page()},
            calcio=rss(url + "?ref=feed", url + "#commenti", url),
            serie_a=rss(url + "/", url.replace("https://", "https://")),
        )
        self.assertEqual(len(self.scrape(session)), 1)
        self.assertEqual(session.calls.count(url), 1)

    def test_seen_and_checked_urls_are_not_reopened(self):
        seen_url = BASE + "gia-inviato"
        checked_url = BASE + "gia-valutato"
        session = make_session(
            calcio=rss(seen_url), serie_a=rss(checked_url, seen_url)
        )
        bot.SKY_JUVENTUS_CHECKED_URLS.add(checked_url)

        with mock.patch.object(bot, "SKY_JUVENTUS_SEEN_KEYS", {seen_url}):
            self.assertEqual(self.scrape(session), [])
        self.assertCountEqual(session.calls, [CALCIO, SERIE_A])

    def test_published_article_is_not_republished_on_next_scan(self):
        url = BASE + "pubblicato"
        session = make_session({url: juve_page()}, calcio=rss(url), serie_a=rss(url))
        first = self.scrape(session)
        self.assertEqual(len(first), 1)

        # Dopo l'invio lo stato persistente contiene l'URL.
        with mock.patch.object(bot, "SKY_JUVENTUS_SEEN_KEYS", {first[0].url}):
            self.assertEqual(self.scrape(session), [])
        self.assertEqual(session.calls.count(url), 1)

    def test_rejected_article_is_not_reopened_on_next_cycle(self):
        url = BASE + "napoli-news"
        session = make_session(
            {url: page("Napoli, news", "Il Napoli lavora.")}, serie_a=rss(url)
        )
        self.scrape(session)
        self.scrape(session)

        self.assertEqual(session.calls.count(url), 1)

    def test_accepted_article_stays_recoverable_until_marked_as_seen(self):
        url = BASE + "accettato"
        session = make_session({url: juve_page()}, serie_a=rss(url))
        self.assertEqual(len(self.scrape(session)), 1)
        self.assertEqual(len(self.scrape(session)), 1)

    def test_transient_page_error_is_retried_next_cycle(self):
        url = BASE + "errore-temporaneo"
        session = make_session(serie_a=rss(url))
        self.assertEqual(self.scrape(session), [])
        self.assertEqual(self.scrape(session), [])

        self.assertEqual(session.calls.count(url), 2)

    def test_other_hosts_and_category_pages_are_ignored(self):
        session = make_session(
            calcio=rss("https://example.com/2026/10/09/juve"),
            serie_a=rss(
                "https://sport.sky.it/argomenti/juve",
                "https://sport.sky.it/calcio/squadre/juventus/news",
            ),
        )
        self.assertEqual(self.scrape(session), [])
        self.assertCountEqual(session.calls, [CALCIO, SERIE_A])

    # --- errori ------------------------------------------------------------

    def test_a_failing_feed_does_not_block_the_working_one(self):
        url = BASE + "dal-feed-buono"
        failures = (
            FakeResponse(status=503),
            FakeResponse(content=b"<rss><item>"),
            FakeResponse(content=b"non xml"),
        )
        for failure in failures:
            for broken_feed, good_feed in ((CALCIO, SERIE_A), (SERIE_A, CALCIO)):
                with self.subTest(status=failure.status_code, broken=broken_feed):
                    bot.SKY_JUVENTUS_CHECKED_URLS.clear()
                    session = make_session({url: juve_page()})
                    session.routes[broken_feed] = failure
                    session.routes[good_feed] = FakeResponse(content=rss(url))
                    self.assertEqual(
                        [a.url for a in self.scrape(session)], [url]
                    )

    def test_a_feed_timeout_does_not_block_the_working_one(self):
        url = BASE + "dal-feed-buono"

        def timeout():
            raise requests.Timeout("timeout")

        session = make_session({url: juve_page()}, serie_a=rss(url))
        session.routes[CALCIO] = timeout
        self.assertEqual([a.url for a in self.scrape(session)], [url])

    def test_all_feeds_failing_propagates_to_the_source_loop(self):
        for failure in (
            FakeResponse(status=503),
            FakeResponse(content=b"<rss><item>"),
        ):
            with self.subTest(status=failure.status_code):
                session = make_session()
                session.routes[CALCIO] = failure
                session.routes[SERIE_A] = failure
                with self.assertRaises((requests.RequestException, ET.ParseError)):
                    self.scrape(session)

    def test_collection_cycle_continues_when_sky_fails(self):
        other = bot.Article(
            source="Altra fonte",
            title="Juventus, notizia",
            url="https://example.com/notizia",
            published=datetime(2026, 10, 9, 10, 0, tzinfo=bot.ROME),
        )
        for failure in (FakeResponse(status=500), FakeResponse(content=b"non xml")):
            sky_session = make_session()
            sky_session.routes[CALCIO] = failure
            sky_session.routes[SERIE_A] = failure
            scrapers = (
                (
                    "Sky Sport - Juventus",
                    lambda _s, dates, s=sky_session: bot.scrape_sky_juventus_news(
                        s, dates
                    ),
                ),
                ("Altra fonte", lambda _s, _d: [other]),
            )
            with self.subTest(content=failure.content):
                with mock.patch.object(bot, "_article_scrapers", return_value=scrapers):
                    articles, errors = bot.collect_articles(
                        requests.Session(), {TODAY}
                    )
                self.assertEqual([a.source for a in articles], ["Altra fonte"])
                self.assertEqual(len(errors), 1)
                self.assertTrue(errors[0].startswith("Sky Sport - Juventus"))

    def test_page_with_unusable_metadata_does_not_drop_other_articles(self):
        good = BASE + "buono"
        bad = BASE + "difettoso"
        session = make_session(
            {
                good: page("Juventus, ufficiale", "La Juventus ha firmato."),
                bad: FakeResponse(text="<html></html>"),
            },
            calcio=rss(bad),
            serie_a=rss(good),
        )
        self.assertEqual([a.url for a in self.scrape(session)], [good])

    # --- priorità alla prima versione valida -------------------------------

    def test_slow_feed_does_not_delay_the_valid_article_of_the_fast_one(self):
        url = BASE + "veloce"
        release = threading.Event()
        self.addCleanup(release.set)

        def slow_feed():
            release.wait(5)
            return FakeResponse(content=rss(BASE + "in-ritardo"))

        session = make_session({url: juve_page()}, serie_a=rss(url))
        session.routes[CALCIO] = slow_feed

        with mock.patch.object(bot, "SKY_JUVENTUS_FEED_GRACE_SECONDS", 0.05):
            started = time.monotonic()
            articles = self.scrape(session)
            elapsed = time.monotonic() - started

        self.assertEqual([a.url for a in articles], [url])
        self.assertLess(elapsed, 2.0)

    def test_first_intercepted_version_is_the_one_kept(self):
        url = BASE + "stesso-articolo"
        release = threading.Event()
        self.addCleanup(release.set)

        def delayed_calcio():
            release.wait(0.3)
            return FakeResponse(content=rss(url))

        session = make_session({url: juve_page()}, serie_a=rss(url))
        session.routes[CALCIO] = delayed_calcio

        articles = self.scrape(session)

        self.assertEqual([a.url for a in articles], [url])
        self.assertEqual(session.calls.count(url), 1)


class SkyJuventusFilterTests(unittest.TestCase):
    def test_di_marzio_filter_behaviour_is_unchanged(self):
        # Alias e regole Sky non devono influenzare il filtro Di Marzio.
        self.assertFalse(
            bot._is_relevant_di_marzio_juventus_article(
                "Mercato Udinese",
                "",
                "Mercato Udinese",
                "I bianconeri cercano un difensore.",
                "",
                "",
            )
        )

    def test_json_ld_text_handles_any_shape(self):
        self.assertEqual(bot._json_ld_text(None), "")
        self.assertEqual(bot._json_ld_text(12), "")
        self.assertEqual(bot._json_ld_text("<b>Juve</b>"), "Juve")
        self.assertEqual(bot._json_ld_text(["a", {"name": "b"}, None]), "a b")
        self.assertEqual(bot._json_ld_text({"name": "Juventus"}), "Juventus")


class SkyRealFeedFormatTests(unittest.TestCase):
    """Il feed Sky vero usa giorni/mesi italiani e scrive "GMT" su ora locale."""

    def setUp(self):
        bot.SKY_JUVENTUS_CHECKED_URLS.clear()
        self.addCleanup(bot.SKY_JUVENTUS_CHECKED_URLS.clear)
        for patcher in (
            mock.patch.object(bot, "SKY_JUVENTUS_SEEN_KEYS", None),
            mock.patch.object(bot, "_sky_today", return_value=TODAY),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_feed_urls_are_the_canonical_ones(self):
        self.assertEqual(
            bot.SKY_JUVENTUS_RSS_URLS,
            (
                "https://sport.sky.it/rss/sport.calcio.xml",
                "https://sport.sky.it/rss/sport.calcio.serie-a.xml",
            ),
        )

    def test_italian_month_and_weekday_are_parsed(self):
        for raw, expected in (
            ("ven, 09 ott 2026 17:25:00 GMT", (2026, 10, 9)),
            ("sab, 10 ott 2026 18:30:00 GMT", (2026, 10, 10)),
            ("mar, 06 ott 2026 12:56:43 GMT", (2026, 10, 6)),
            ("dom, 24 mag 2026 20:45:00 GMT", (2026, 5, 24)),
            ("gio, 31 dic 2026 09:00:00 GMT", (2026, 12, 31)),
        ):
            with self.subTest(raw=raw):
                parsed = bot._parse_feed_published(raw)
                self.assertIsNotNone(parsed)
                self.assertEqual(
                    (parsed.year, parsed.month, parsed.day), expected
                )

    def test_english_dates_still_parse(self):
        parsed = bot._parse_feed_published("Fri, 09 Oct 2026 10:00:00 +0200")
        self.assertEqual(parsed.date(), TODAY)

    def test_gmt_label_is_read_as_rome_clock_when_requested(self):
        raw = "ven, 09 ott 2026 23:30:00 GMT"
        as_utc = bot._parse_feed_published(raw)
        as_local = bot._parse_feed_published(raw, zero_offset_is_local=True)
        self.assertEqual(as_utc.date(), date(2026, 10, 10))
        self.assertEqual(as_local.date(), TODAY)
        self.assertEqual((as_local.hour, as_local.minute), (23, 30))

    def test_explicit_offset_is_respected_even_when_local_is_requested(self):
        parsed = bot._parse_feed_published(
            "Fri, 09 Oct 2026 10:00:00 +0200", zero_offset_is_local=True
        )
        self.assertEqual(parsed.hour, 10)

    def test_video_urls_are_discarded_without_being_opened(self):
        video = "https://sport.sky.it/calcio/serie-a/video/2026/10/09/juve-1130337"
        article = BASE + "articolo-juve"
        session = make_session(
            {video: juve_page(), article: juve_page()},
            calcio=rss(video, article),
        )
        urls = [a.url for a in bot.scrape_sky_juventus_news(session, {TODAY})]
        self.assertEqual(urls, [article])
        self.assertNotIn(video, session.calls)

    def test_real_style_feed_produces_articles(self):
        late = BASE + "notte"
        early = BASE + "mattina"
        feed = (
            '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            f"<item><title>Notte</title><link>{late}</link>"
            "<pubDate>ven, 09 ott 2026 23:40:00 GMT</pubDate></item>"
            f"<item><title>Mattina</title><link>{early}</link>"
            "<pubDate>ven, 09 ott 2026 08:05:00 GMT</pubDate></item>"
            "</channel></rss>"
        ).encode()
        session = make_session(
            {late: juve_page(), early: juve_page()}, calcio=feed
        )
        urls = [
            a.url for a in bot.scrape_sky_juventus_news(session, {TODAY})
        ]
        self.assertCountEqual(urls, [late, early])


if __name__ == "__main__":
    unittest.main()
