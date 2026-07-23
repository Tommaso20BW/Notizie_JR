"""
Juventus Press News Bot

Controlla le notizie Juventus pubblicate OGGI su:
- Tuttosport
- Corriere dello Sport
- La Gazzetta dello Sport
- Sky Sport Calciomercato ("Juve"/"Juventus", esclusi i titoli "video")
- Juventus.com
- Gianluca Di Marzio (solo titoli con "Juventus") e Alfredo Pedullà
- Borsa Italiana (notizie sull'azione Juventus)
- YouTube: Fabrizio Romano in Italiano e Romeo Agresti
- X: profili configurati (filtri e repost definiti per account)

Ogni notizia viene inviata su Telegram una sola volta. Lo stato è salvato nel file
.seen_juve_press_news.json accanto allo script.
"""

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import escape
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


def configure_console_encoding() -> None:
    """Evita che caratteri tipografici delle fonti blocchino il bot su Windows."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


configure_console_encoding()

ROME = ZoneInfo("Europe/Rome")
SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = SCRIPT_DIR / ".seen_juve_press_news.json"
MAX_SEEN = 2000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}

TUTTOSPORT_URL = "https://www.tuttosport.com/squadra/calcio/juventus/t128"
CORRIERE_URL = (
    "https://www.corrieredellosport.it/squadra/calcio/juventus/t128"
)
GAZZETTA_PAGE_URL = (
    "https://www.gazzetta.it/calcio/squadre/juventus/notizie/"
)
GAZZETTA_API_URL = (
    "https://appservice.gazzetta.it/gaz/app/api/mygazzetta/search"
)
SKY_URL_TEMPLATE = (
    "https://sport.sky.it/calciomercato/{year}/{month:02d}/{day:02d}/"
    "calciomercato-news-trattative-oggi-{day}-{month_name}"
)
JUVENTUS_NEWS_URL = "https://www.juventus.com/it/news/"
JUVENTUS_FEED_TEMPLATE = (
    "https://www.juventus.com/it/news/_libraries/"
    "{date_value}/{date_value}/{page}/_news-list"
)
GIANLUCA_DI_MARZIO_URL = "https://www.gianlucadimarzio.com/"
ALFREDO_PEDULLA_JUVENTUS_URLS = (
    "https://www.alfredopedulla.com/squadre/juventus/",
    "https://www.alfredopedulla.com/tag/juventus/",
)
BORSA_ITALIANA_JUVENTUS_URL = (
    "https://www.borsaitaliana.it/borsa/azioni/"
    "elenco-completo-notizie.html?isin=IT0005572778&lang=it"
)
YOUTUBE_CHANNELS = (
    {
        "source": "YouTube - Fabrizio Romano in Italiano",
        "channel_id": "UC7pT9g1-oKwVgbpipZODvBA",
        "channel_url": "https://www.youtube.com/@FabrizioRomanoItaliano",
    },
    {
        "source": "YouTube - Romeo Agresti",
        "channel_id": "UCmlXlTE2oTArVL8DafyRsXA",
        "channel_url": "https://www.youtube.com/@RomeoAgresti",
    },
)
YOUTUBE_FEED_TEMPLATE = (
    "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
)
ATOM_NS = "{http://www.w3.org/2005/Atom}"
YOUTUBE_NS = "{http://www.youtube.com/xml/schemas/2015}"
X_ACCOUNTS = (
    {"handle": "juventusfc", "filter_juventus": False, "include_reposts": True},
    {"handle": "Glongari", "filter_juventus": True, "include_reposts": False},
    {"handle": "romeoagresti", "filter_juventus": False, "include_reposts": True},
    {"handle": "NicoSchira", "filter_juventus": True, "include_reposts": False},
    {"handle": "AlfredoPedulla", "filter_juventus": True, "include_reposts": False},
    {"handle": "MatteMoretto", "filter_juventus": True, "include_reposts": False},
    {"handle": "FabrizioRomano", "filter_juventus": True, "include_reposts": False},
    {"handle": "DiMarzio", "filter_juventus": True, "include_reposts": False},
    {"handle": "_Morik92_", "filter_juventus": False, "include_reposts": True},
    {"handle": "ilbianconerocom", "filter_juventus": False, "include_reposts": True},
)
X_RSS_MIRROR_TEMPLATES = (
    "https://nitter.net/{handle}/rss",
    "https://xcancel.com/{handle}/rss",
)
X_RSS_TIMEOUT_SECONDS = 12
X_STATUS_PATH_RE = re.compile(r"^/([A-Za-z0-9_]+)/status/(\d+)$")

TELEGRAM_SOURCE_EMOJIS = (
    ("Sky Sport - Calciomercato", "6033058586945392520", "📰"),
    ("La Gazzetta dello Sport", "6032862491623559282", "📰"),
    ("Corriere dello Sport", "6030691308346019878", "📰"),
    ("Tuttosport", "6032834612990841221", "📰"),
    ("X - @", "5796663209016431644", "📲"),
    ("YouTube - ", "6032683730789732131", "🖥"),
    ("Gianluca Di Marzio", "5785253271912324677", "📲"),
    ("Alfredo Pedullà", "5785322627044220734", "📲"),
    ("Borsa Italiana", "5373001317042101552", "📈"),
    ("Juventus.com", "6028591382870888482", "⚪️"),
)

SKY_MONTH_NAMES = {
    1: "gennaio",
    2: "febbraio",
    3: "marzo",
    4: "aprile",
    5: "maggio",
    6: "giugno",
    7: "luglio",
    8: "agosto",
    9: "settembre",
    10: "ottobre",
    11: "novembre",
    12: "dicembre",
}

URL_DATE_RE = re.compile(r"/(\d{4})/(\d{2})/(\d{2})(?:-|/)")
JUVE_KEYWORD_RE = re.compile(r"\b(?:juventus|juve)\b", re.IGNORECASE)
JUVENTUS_KEYWORD_RE = re.compile(r"\bjuventus\b", re.IGNORECASE)
SKY_RECAP_TITLE_RE = re.compile(
    r"^calciomercato,.*\bnews\b.*\boggi\b",
    re.IGNORECASE,
)
SKY_VIDEO_TITLE_RE = re.compile(r"\bvideo\b", re.IGNORECASE)
SKY_EXCLUDED_TITLE_RE = re.compile(r"\bjuve\s+stabia\b", re.IGNORECASE)
BORSA_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+"
    r"(gen|feb|mar|apr|mag|giu|lug|ago|set|ott|nov|dic)\s+"
    r"(\d{1,2}):(\d{2})\b",
    re.IGNORECASE,
)

BORSA_MONTHS = {
    "gen": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "mag": 5,
    "giu": 6,
    "lug": 7,
    "ago": 8,
    "set": 9,
    "ott": 10,
    "nov": 11,
    "dic": 12,
}


@dataclass(frozen=True)
class Article:
    source: str
    title: str
    url: str
    published: datetime
    summary: str = ""
    state_key: str = ""

    @property
    def notification_key(self) -> str:
        """Chiave usata per non inviare due volte la stessa notizia."""
        return self.state_key or self.url


def normalize_url(url: str) -> str:
    """Rimuove query e frammento, mantenendo intatto il percorso."""
    parts = urlsplit(url.strip())
    path = re.sub(r"/{2,}", "/", parts.path)
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit(
        (
            parts.scheme.lower() or "https",
            parts.netloc.lower(),
            path,
            "",
            "",
        )
    )


def parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ROME)
    return parsed.astimezone(ROME)


def date_from_article_url(url: str) -> datetime | None:
    match = URL_DATE_RE.search(url)
    if not match:
        return None
    try:
        return datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            tzinfo=ROME,
        )
    except ValueError:
        return None


def is_requested_date(
    published: datetime,
    requested_dates: set[date],
) -> bool:
    return published.astimezone(ROME).date() in requested_dates


def is_today(published: datetime, today: date) -> bool:
    """Compatibilità per le fonti che vengono richieste una data alla volta."""
    return is_requested_date(published, {today})


def is_juventus_title(title: str) -> bool:
    """Esclude omonimie, come la squadra Juve Stabia."""
    return bool(
        JUVE_KEYWORD_RE.search(title)
        and not SKY_EXCLUDED_TITLE_RE.search(title)
    )


def article_summary(card) -> str:
    for element in card.find_all(["div", "p"], class_=True):
        classes = element.get("class", [])
        if any(str(name).startswith("Summary_") for name in classes):
            return element.get_text(" ", strip=True)
    return ""


def scrape_html_source(
    session: requests.Session,
    source: str,
    page_url: str,
    expected_host: str,
    requested_dates: set[date],
) -> list[Article]:
    response = session.get(page_url, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    articles: list[Article] = []
    urls_done: set[str] = set()

    for card in soup.find_all("article"):
        heading = card.find("h2")
        link = heading.find("a", href=True) if heading else None
        if not link:
            continue

        url = normalize_url(urljoin(page_url, link["href"]))
        if urlsplit(url).netloc.lower() != expected_host:
            continue

        published = None
        time_tag = card.find("time")
        if time_tag:
            raw_datetime = time_tag.get("datetime")
            if raw_datetime:
                try:
                    published = parse_iso_datetime(raw_datetime)
                except ValueError:
                    published = None

        if published is None:
            published = date_from_article_url(url)

        if (
            published is None
            or not is_requested_date(published, requested_dates)
        ):
            continue
        if url in urls_done:
            continue

        title = link.get_text(" ", strip=True)
        if not title:
            continue

        urls_done.add(url)
        articles.append(
            Article(
                source=source,
                title=title,
                url=url,
                published=published,
                summary=article_summary(card),
            )
        )

    return articles


def scrape_tuttosport(
    session: requests.Session,
    requested_dates: set[date],
) -> list[Article]:
    return scrape_html_source(
        session=session,
        source="Tuttosport",
        page_url=TUTTOSPORT_URL,
        expected_host="www.tuttosport.com",
        requested_dates=requested_dates,
    )


def scrape_corriere(
    session: requests.Session,
    requested_dates: set[date],
) -> list[Article]:
    return scrape_html_source(
        session=session,
        source="Corriere dello Sport",
        page_url=CORRIERE_URL,
        expected_host="www.corrieredellosport.it",
        requested_dates=requested_dates,
    )


def scrape_gazzetta(
    session: requests.Session,
    requested_dates: set[date],
) -> list[Article]:
    # La pagina Gazzetta carica le notizie da questo feed JSON ufficiale.
    response = session.get(
        GAZZETTA_API_URL,
        params={
            "section": '["Calcio/Serie A/Juventus"]',
            "page": 1,
            "limit": 100,
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()

    articles: list[Article] = []
    urls_done: set[str] = set()
    for item in payload.get("data", []):
        raw_date = item.get("firstPublicationDate")
        raw_url = item.get("url")
        title = item.get("headline")
        if not raw_date or not raw_url or not title:
            continue

        try:
            published = parse_iso_datetime(raw_date)
        except ValueError:
            continue
        if not is_requested_date(published, requested_dates):
            continue

        url = normalize_url(raw_url)
        host = urlsplit(url).netloc.lower()
        if not (
            host == "www.gazzetta.it"
            or host == "video.gazzetta.it"
            or host.endswith(".gazzetta.it")
        ):
            continue
        if url in urls_done:
            continue

        urls_done.add(url)
        articles.append(
            Article(
                source="La Gazzetta dello Sport",
                title=str(title).strip(),
                url=url,
                published=published,
                summary=str(item.get("standFirst") or "").strip(),
            )
        )

    return articles


def sky_url_for_date(today: date) -> str:
    return SKY_URL_TEMPLATE.format(
        year=today.year,
        month=today.month,
        day=today.day,
        month_name=SKY_MONTH_NAMES[today.month],
    )


def _scrape_sky_calciomercato_for_date(
    session: requests.Session,
    today: date,
) -> list[Article]:
    page_url = sky_url_for_date(today)
    response = session.get(page_url, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    articles: list[Article] = []
    keys_done: set[str] = set()
    for post in soup.select("div.lvbg-post"):
        title_tag = post.select_one("h2.lvbg-post__title-v2")
        time_tag = post.select_one(
            "time.lvbg-post__timestamp-time[datetime]"
        )
        if not title_tag or not time_tag:
            continue

        title = title_tag.get_text(" ", strip=True)
        if (
            SKY_RECAP_TITLE_RE.search(title)
            or SKY_VIDEO_TITLE_RE.search(title)
        ):
            continue
        # Il testo della diretta può citare qualunque squadra in modo
        # incidentale: per Sky notifichiamo soltanto aggiornamenti che citano
        # Juve/Juventus direttamente nel titolo.
        if not is_juventus_title(title):
            continue

        summary_tag = post.select_one(".lvbg-post__body")
        # Considera solo i paragrafi del singolo aggiornamento. Usare tutto
        # il contenitore includeva anche i TAG globali della pagina, dove
        # "juventus" e "juve" compaiono sempre, generando falsi positivi.
        paragraphs = (
            summary_tag.select("p")
            if summary_tag
            else []
        )
        summary = " ".join(
            paragraph.get_text(" ", strip=True)
            for paragraph in paragraphs
        )
        # Sky inserisce talvolta i TAG nello stesso <p> del testo: non sono
        # parte della notizia e possono contenere artificialmente "Juventus".
        summary = re.split(
            r"\s*\bTAG:\s*",
            summary,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()

        try:
            published = parse_iso_datetime(time_tag["datetime"])
        except (KeyError, ValueError):
            continue
        if not is_today(published, today):
            continue

        # Tutti gli aggiornamenti Sky condividono lo stesso URL. La chiave
        # separata impedisce che il primo blocco faccia scartare tutti gli altri.
        state_key = (
            f"sky-live:{published.isoformat()}:{title.casefold()}"
        )
        if state_key in keys_done:
            continue

        keys_done.add(state_key)
        articles.append(
            Article(
                source="Sky Sport - Calciomercato",
                title=title,
                url=normalize_url(page_url),
                published=published,
                summary=summary,
                state_key=state_key,
            )
        )

    return articles


def scrape_sky_calciomercato(
    session: requests.Session,
    requested_dates: set[date],
) -> list[Article]:
    articles_by_key: dict[str, Article] = {}
    for requested_date in sorted(requested_dates):
        source_articles: list[Article] = []
        for sky_date in (requested_date, requested_date - timedelta(days=1)):
            try:
                source_articles = _scrape_sky_calciomercato_for_date(
                    session,
                    sky_date,
                )
            except requests.HTTPError as error:
                response = error.response
                if response is None or response.status_code != 404:
                    raise
                continue
            break

        for article in source_articles:
            articles_by_key.setdefault(article.notification_key, article)
    return list(articles_by_key.values())


def juventus_feed_url(today: date, page: int = 1) -> str:
    return JUVENTUS_FEED_TEMPLATE.format(
        date_value=today.isoformat(),
        page=page,
    )


def _scrape_juventus_official_for_date(
    session: requests.Session,
    today: date,
) -> list[Article]:
    articles: list[Article] = []
    urls_done: set[str] = set()
    pages_done: set[str] = set()
    page_url: str | None = juventus_feed_url(today)

    # Il feed ufficiale è già filtrato per la data richiesta. Seguiamo
    # comunque l'eventuale paginazione, così non perdiamo giornate molto ricche.
    for _ in range(10):
        if not page_url or page_url in pages_done:
            break
        pages_done.add(page_url)

        response = session.get(page_url, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        for content in soup.select(
            ".grid-item-content[data-dateutc]"
        ):
            link = content.find_parent("a", href=True)
            title_tag = content.select_one(".item-title")
            raw_date = content.get("data-dateutc")
            if not link or not title_tag or not raw_date:
                continue

            try:
                published = parse_iso_datetime(raw_date)
            except ValueError:
                continue
            if not is_today(published, today):
                continue

            url = normalize_url(urljoin(JUVENTUS_NEWS_URL, link["href"]))
            if urlsplit(url).netloc.lower() != "www.juventus.com":
                continue
            if url in urls_done:
                continue

            title = title_tag.get_text(" ", strip=True)
            if not title:
                continue

            urls_done.add(url)
            articles.append(
                Article(
                    source="Juventus.com",
                    title=title,
                    url=url,
                    published=published,
                )
            )

        next_link = soup.select_one("[data-page-url]")
        next_path = (
            next_link.get("data-page-url")
            if next_link
            else None
        )
        next_url = (
            normalize_url(urljoin(JUVENTUS_NEWS_URL, next_path))
            if next_path
            else None
        )
        if next_url and urlsplit(next_url).netloc.lower() != (
            "www.juventus.com"
        ):
            next_url = None
        page_url = next_url

    return articles


def scrape_juventus_official(
    session: requests.Session,
    requested_dates: set[date],
) -> list[Article]:
    articles_by_key: dict[str, Article] = {}
    for requested_date in sorted(requested_dates):
        for article in _scrape_juventus_official_for_date(
            session,
            requested_date,
        ):
            articles_by_key.setdefault(article.notification_key, article)
    return list(articles_by_key.values())


def scrape_gianluca_di_marzio(
    session: requests.Session,
    requested_dates: set[date],
) -> list[Article]:
    """Recupera dalla home solo le notizie con "Juventus" nel titolo."""
    response = session.get(GIANLUCA_DI_MARZIO_URL, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    articles: list[Article] = []
    urls_done: set[str] = set()
    for link in soup.select("#tcc-index a[href]"):
        title_tag = link.select_one(".title")
        if not title_tag:
            continue

        title = title_tag.get_text(" ", strip=True)
        if not title or not JUVENTUS_KEYWORD_RE.search(title):
            continue

        raw_url = str(link.get("href") or "").strip()
        if not raw_url:
            continue
        url = normalize_url(urljoin(GIANLUCA_DI_MARZIO_URL, raw_url))
        if urlsplit(url).netloc.lower() != "www.gianlucadimarzio.com":
            continue
        if url in urls_done:
            continue
        urls_done.add(url)

        try:
            article_response = session.get(url, timeout=30)
            article_response.raise_for_status()
        except requests.RequestException:
            continue

        article_soup = BeautifulSoup(article_response.text, "html.parser")
        article_data = None
        for script in article_soup.find_all(
            "script",
            attrs={"type": "application/ld+json"},
        ):
            try:
                structured_data = json.loads(script.string or "")
            except json.JSONDecodeError:
                continue

            graph = (
                structured_data.get("@graph", [])
                if isinstance(structured_data, dict)
                else []
            )
            for item in graph:
                item_types = (
                    item.get("@type", [])
                    if isinstance(item, dict)
                    else []
                )
                if isinstance(item_types, str):
                    item_types = [item_types]
                if "NewsArticle" in item_types:
                    article_data = item
                    break
            if article_data:
                break

        if not article_data:
            continue
        try:
            published = parse_iso_datetime(str(article_data["datePublished"]))
        except (KeyError, ValueError):
            continue
        if not is_requested_date(published, requested_dates):
            continue

        # Il titolo della home è quello su cui va applicato il filtro.
        # Usiamo quello completo dei metadati solo dopo averlo accettato.
        article_title = str(article_data.get("headline") or title).strip()
        summary = BeautifulSoup(
            str(article_data.get("abstract") or ""),
            "html.parser",
        ).get_text(" ", strip=True)

        articles.append(
            Article(
                source="Gianluca Di Marzio",
                title=article_title,
                url=url,
                published=published,
                summary=summary,
            )
        )

    return articles


def scrape_alfredo_pedulla(
    session: requests.Session,
    requested_dates: set[date],
) -> list[Article]:
    articles: list[Article] = []
    urls_done: set[str] = set()
    for page_url in ALFREDO_PEDULLA_JUVENTUS_URLS:
        response = session.get(page_url, timeout=30)
        response.raise_for_status()
        # Il sito dichiara una codifica non coerente con i contenuti UTF-8.
        # Senza questa assegnazione, Telegram riceve sequenze come "Ã¨".
        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")

        for item in soup.select("li.article-block-item"):
            link = item.select_one("a.block-title[href]")
            date_tag = item.select_one(".block-date")
            if not link or not date_tag:
                continue

            raw_date = date_tag.get_text(" ", strip=True)
            try:
                published = datetime.strptime(
                    raw_date,
                    "%d/%m/%Y | %H:%M",
                ).replace(tzinfo=ROME)
            except ValueError:
                continue
            if not is_requested_date(published, requested_dates):
                continue

            title = link.get_text(" ", strip=True)
            if not title or not is_juventus_title(title):
                continue

            url = normalize_url(urljoin(page_url, link["href"]))
            if urlsplit(url).netloc.lower() != "www.alfredopedulla.com":
                continue
            if url in urls_done:
                continue

            urls_done.add(url)
            articles.append(
                Article(
                    source="Alfredo Pedullà",
                    title=title,
                    url=url,
                    published=published,
                )
            )

    return articles


def scrape_borsa_italiana(
    session: requests.Session,
    requested_dates: set[date],
) -> list[Article]:
    response = session.get(BORSA_ITALIANA_JUVENTUS_URL, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    articles: list[Article] = []
    urls_done: set[str] = set()
    for link in soup.select("a.news[href]"):
        item = link.find_parent("li")
        date_tag = item.select_one(".m-feed__date") if item else None
        if not date_tag:
            continue

        match = BORSA_DATE_RE.search(date_tag.get_text(" ", strip=True))
        if not match:
            continue
        try:
            published = datetime(
                max(requested_dates).year,
                BORSA_MONTHS[match.group(2).lower()],
                int(match.group(1)),
                int(match.group(3)),
                int(match.group(4)),
                tzinfo=ROME,
            )
        except ValueError:
            continue
        if not is_requested_date(published, requested_dates):
            continue

        title = link.get_text(" ", strip=True)
        if not title or not is_juventus_title(title):
            continue

        url = normalize_url(
            urljoin(BORSA_ITALIANA_JUVENTUS_URL, link["href"])
        )
        if urlsplit(url).netloc.lower() != "www.borsaitaliana.it":
            continue
        if url in urls_done:
            continue

        author = item.select_one(".m-feed__author") if item else None
        summary = (
            f"Fonte: {author.get_text(' ', strip=True)}"
            if author
            else ""
        )
        urls_done.add(url)
        articles.append(
            Article(
                source="Borsa Italiana",
                title=title,
                url=url,
                published=published,
                summary=summary,
            )
        )

    return articles


def scrape_youtube_channels(
    session: requests.Session,
    requested_dates: set[date],
) -> list[Article]:
    """Recupera tutti i video pubblicati oggi dai canali configurati."""
    articles: list[Article] = []
    keys_done: set[str] = set()

    for channel in YOUTUBE_CHANNELS:
        feed_url = YOUTUBE_FEED_TEMPLATE.format(
            channel_id=channel["channel_id"],
        )
        response = session.get(feed_url, timeout=30)
        response.raise_for_status()
        root = ET.fromstring(response.content)

        for entry in root.findall(f"{ATOM_NS}entry"):
            title = entry.findtext(f"{ATOM_NS}title", default="").strip()
            raw_published = entry.findtext(
                f"{ATOM_NS}published",
                default="",
            )
            video_id = entry.findtext(
                f"{YOUTUBE_NS}videoId",
                default="",
            )
            if not title or not raw_published or not video_id:
                continue

            try:
                published = parse_iso_datetime(raw_published)
            except ValueError:
                continue
            if not is_requested_date(published, requested_dates):
                continue

            state_key = f"youtube:{channel['channel_id']}:{video_id}"
            if state_key in keys_done:
                continue

            keys_done.add(state_key)
            articles.append(
                Article(
                    source=channel["source"],
                    title=title,
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    published=published,
                    state_key=state_key,
                )
            )

    return articles


def _download_x_feed(
    feed_url: str,
    headers: dict[str, str],
) -> bytes | None:
    """Scarica un mirror RSS senza interrompere il controllo degli altri."""
    try:
        response = requests.get(
            feed_url,
            headers=headers,
            timeout=X_RSS_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException:
        return None
    return response.content


def scrape_x_profiles(
    session: requests.Session,
    requested_dates: set[date],
) -> list[Article]:
    """Recupera i post X odierni da più mirror RSS indipendenti."""
    articles: list[Article] = []
    keys_done: set[str] = set()
    feed_sources = [
        (
            account,
            mirror_template.format(handle=account["handle"]),
        )
        for account in X_ACCOUNTS
        for mirror_template in X_RSS_MIRROR_TEMPLATES
    ]
    headers = dict(session.headers)

    with ThreadPoolExecutor(
        max_workers=min(6, len(feed_sources)),
    ) as executor:
        future_sources = {
            executor.submit(_download_x_feed, feed_url, headers): account
            for account, feed_url in feed_sources
        }
        for future in as_completed(future_sources):
            account = future_sources[future]
            content = future.result()
            if content is None:
                continue

            try:
                root = ET.fromstring(content)
            except ET.ParseError:
                continue
            channel = root.find("channel")
            if channel is None:
                continue

            handle = account["handle"]
            for item in channel.findall("item"):
                title = item.findtext("title", default="").strip()
                raw_published = item.findtext("pubDate", default="")
                tweet_id = item.findtext("guid", default="").strip()
                raw_link = item.findtext("link", default="").strip()
                if not title or not raw_published or not tweet_id:
                    continue

                # Per gli account indicati dall'utente si mantengono anche i repost.
                if (
                    not account["include_reposts"]
                    and title.startswith("RT by @")
                ):
                    continue
                if (
                    account["filter_juventus"]
                    and not is_juventus_title(title)
                ):
                    continue

                try:
                    published = parsedate_to_datetime(raw_published)
                except (TypeError, ValueError):
                    continue
                if published.tzinfo is None:
                    published = published.replace(tzinfo=ROME)
                published = published.astimezone(ROME)
                if not is_requested_date(published, requested_dates):
                    continue

                link_match = X_STATUS_PATH_RE.match(urlsplit(raw_link).path)
                if link_match:
                    tweet_url = (
                        f"https://x.com/{link_match.group(1)}/status/"
                        f"{link_match.group(2)}"
                    )
                else:
                    tweet_url = f"https://x.com/{handle}/status/{tweet_id}"

                # Lo stesso tweet può arrivare da più mirror: una sola notifica.
                state_key = f"x:{handle}:{tweet_id}"
                if state_key in keys_done:
                    continue

                keys_done.add(state_key)
                articles.append(
                    Article(
                        source=f"X - @{handle}",
                        title=title,
                        url=tweet_url,
                        published=published,
                        state_key=state_key,
                    )
                )

    return articles


def load_seen() -> list[str]:
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"Stato non leggibile ({STATE_FILE.name}); "
            "interrompo per evitare notifiche duplicate."
        ) from error
    if not isinstance(data, list) or not all(
        isinstance(item, str) for item in data
    ):
        raise RuntimeError(
            f"Formato non valido in {STATE_FILE.name}; "
            "interrompo per evitare notifiche duplicate."
        )
    return list(dict.fromkeys(data))


def save_seen(seen: Iterable[str]) -> None:
    values = list(dict.fromkeys(seen))[-MAX_SEEN:]
    temporary = STATE_FILE.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(values, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, STATE_FILE)


def telegram_source_emoji(source: str) -> str:
    """Restituisce l'emoji premium associata alla fonte Telegram."""
    for source_prefix, emoji_id, fallback_emoji in TELEGRAM_SOURCE_EMOJIS:
        if source.startswith(source_prefix):
            return (
                f'<tg-emoji emoji-id="{emoji_id}">'
                f"{fallback_emoji}</tg-emoji>"
            )
    return "📰"


class TelegramClient:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id

    def send_article(self, article: Article) -> None:
        summary = ""
        if article.summary:
            summary = f"\n\n{escape(article.summary)}"

        source_emoji = telegram_source_emoji(article.source)
        text = (
            f"{source_emoji} <b>{escape(article.source)}</b>\n\n"
            f"<b>{escape(article.title)}</b>"
            f"{summary}\n\n"
            f'<a href="{escape(article.url, quote=True)}">'
            "Leggi l’articolo</a>"
        )
        endpoint = (
            f"https://api.telegram.org/bot{self.token}/sendMessage"
        )
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }

        for attempt in range(3):
            response = requests.post(endpoint, json=payload, timeout=30)
            if response.ok:
                return

            try:
                telegram_error = response.json()
            except ValueError:
                telegram_error = {}

            if response.status_code == 429 and attempt < 2:
                retry_after = (
                    telegram_error.get("parameters", {}).get(
                        "retry_after",
                        2,
                    )
                )
                time.sleep(max(int(retry_after), 1))
                continue

            description = telegram_error.get(
                "description",
                response.text,
            )
            raise RuntimeError(
                f"Telegram sendMessage: HTTP {response.status_code} - "
                f"{description}"
            )


def collect_articles(
    session: requests.Session,
    requested_dates: set[date],
) -> tuple[list[Article], list[str]]:
    scrapers = (
        ("Tuttosport", scrape_tuttosport),
        ("Corriere dello Sport", scrape_corriere),
        ("La Gazzetta dello Sport", scrape_gazzetta),
        ("Sky Sport - Calciomercato", scrape_sky_calciomercato),
        ("Juventus.com", scrape_juventus_official),
        ("Gianluca Di Marzio", scrape_gianluca_di_marzio),
        ("Alfredo Pedullà", scrape_alfredo_pedulla),
        ("Borsa Italiana", scrape_borsa_italiana),
        ("YouTube", scrape_youtube_channels),
        ("X", scrape_x_profiles),
    )
    articles_by_key: dict[str, Article] = {}
    errors: list[str] = []

    for source, scraper in scrapers:
        try:
            source_articles = scraper(session, requested_dates)
        except (
            requests.RequestException,
            ValueError,
            KeyError,
            ET.ParseError,
        ) as error:
            errors.append(f"{source}: {error}")
            print(f"[{source}] errore durante il recupero: {error}")
            continue

        print(f"[{source}] notizie di oggi trovate: {len(source_articles)}")
        for article in source_articles:
            articles_by_key.setdefault(article.notification_key, article)

    if len(errors) == len(scrapers):
        raise RuntimeError("Nessuna fonte è stata recuperata correttamente.")

    return list(articles_by_key.values()), errors


def run(
    dry_run: bool = False,
    include_yesterday: bool = False,
) -> None:
    today = datetime.now(ROME).date()
    requested_dates = {today}
    if include_yesterday:
        requested_dates.add(today - timedelta(days=1))
    session = requests.Session()
    session.headers.update(HEADERS)

    articles, _ = collect_articles(session, requested_dates)

    # I siti mostrano prima le notizie più recenti. Telegram le riceve invece
    # dalla più vecchia alla più nuova, per mantenere l'ordine cronologico.
    articles.sort(key=lambda item: (item.published, item.source, item.title))

    if dry_run:
        selected_days = ", ".join(
            requested_date.isoformat()
            for requested_date in sorted(requested_dates)
        )
        print(f"[TEST] Totale notizie del {selected_days}: {len(articles)}")
        for article in articles:
            print(
                f"[TEST] {article.source} | "
                f"{article.published.strftime('%H:%M')} | {article.title}"
            )
        return

    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError(
            "Secret mancanti: configura TELEGRAM_TOKEN e CHAT_ID."
        )

    seen_list = load_seen()
    seen = set(seen_list)
    print(f"[STATO] articoli già notificati: {len(seen)}")

    baseline_if_missing = os.environ.get(
        "BASELINE_IF_NO_STATE",
        "",
    ).lower() in {"1", "true", "yes"}
    if baseline_if_missing and not STATE_FILE.exists():
        seen_list = [article.notification_key for article in articles]
        save_seen(seen_list)
        print(
            "[STATO] cache iniziale assente: "
            f"registrate {len(seen_list)} notizie correnti senza reinviarle."
        )
        return

    pending = [
        article
        for article in articles
        if article.notification_key not in seen
    ]
    if not pending:
        print("[NEWS] nessuna nuova notizia di oggi.")
        return

    telegram = TelegramClient(token, chat_id)
    for article in pending:
        telegram.send_article(article)
        print(f"[NEWS] notificato da {article.source}: {article.title}")
        seen.add(article.notification_key)
        seen_list.append(article.notification_key)
        save_seen(seen_list)
        time.sleep(0.8)

    print(f"[NEWS] notifiche inviate: {len(pending)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Invia su Telegram le notizie Juventus pubblicate oggi."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Recupera e mostra le notizie senza usare Telegram.",
    )
    parser.add_argument(
        "--include-yesterday",
        action="store_true",
        help="TEST: aggiunge alle notizie di oggi anche quelle di ieri.",
    )
    args = parser.parse_args()
    run(
        dry_run=args.dry_run,
        include_yesterday=args.include_yesterday,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Errore: {error}", file=sys.stderr)
        sys.exit(1)
