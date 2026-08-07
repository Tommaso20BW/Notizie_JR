"""
Juventus Press News Bot

Controlla le notizie Juventus pubblicate OGGI su:
- Tuttosport
- Corriere dello Sport
- La Gazzetta dello Sport
- Sky Sport Calciomercato ("Juve"/"Juventus", esclusi i titoli "video")
- Sky Sport: pagina notizie Juventus
- Juventus.com
- Gianluca Di Marzio (titolo o testo con "Juventus") e Alfredo Pedullà
- Borsa Italiana (notizie sull'azione Juventus)
- YouTube: Juventus, Fabrizio Romano in Italiano e Romeo Agresti
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
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from article_journal import ArticleJournal
from preview_image import PreviewImageResolver, normalize_image_url
from telegram_notifier import (
    TELEGRAM_MAX_CAPTION_LENGTH,
    TELEGRAM_MAX_MESSAGE_LENGTH,
    TelegramClient,
    format_article_message,
)
from video_media import VideoPreparationError, prepare_telegram_video


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
PENDING_FILE = SCRIPT_DIR / ".pending_juve_press_news.json"
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
SKY_JUVENTUS_NEWS_URL = (
    "https://sport.sky.it/calcio/squadre/juventus/news"
)
JUVENTUS_NEWS_URL = "https://www.juventus.com/it/news/"
JUVENTUS_FEED_TEMPLATE = (
    "https://www.juventus.com/it/news/_libraries/"
    "{date_value}/{date_value}/{page}/_news-list"
)
GIANLUCA_DI_MARZIO_URL = "https://www.gianlucadimarzio.com/"
ALFREDO_PEDULLA_JUVENTUS_URLS = (
    "https://www.alfredopedulla.com/search/juve/",
)
BORSA_ITALIANA_JUVENTUS_URL = (
    "https://www.borsaitaliana.it/borsa/azioni/"
    "elenco-completo-notizie.html?isin=IT0005572778&lang=it"
)
YOUTUBE_CHANNELS = (
    {
        "source": "YouTube - Juventus",
        "channel_id": "UCLzKhsxrExAC6yAdtZ-BOWw",
        "channel_url": "https://www.youtube.com/@Juventus",
    },
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
YOUTUBE_SHORTS_URL_TEMPLATE = "https://www.youtube.com/shorts/{video_id}"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
YOUTUBE_NS = "{http://www.youtube.com/xml/schemas/2015}"
X_ACCOUNTS = (
    {"handle": "juventusfc", "filter_juventus": False, "include_reposts": False},
    {"handle": "Glongari", "filter_juventus": True, "include_reposts": False},
    {"handle": "romeoagresti", "filter_juventus": False, "include_reposts": False},
    {"handle": "NicoSchira", "filter_juventus": True, "include_reposts": False},
    {"handle": "AlfredoPedulla", "filter_juventus": True, "include_reposts": False},
    {"handle": "MatteMoretto", "filter_juventus": True, "include_reposts": False},
    {"handle": "FabrizioRomano", "filter_juventus": True, "include_reposts": False},
    {"handle": "DiMarzio", "filter_juventus": True, "include_reposts": False},
    {"handle": "_Morik92_", "filter_juventus": False, "include_reposts": False},
    {"handle": "ilbianconerocom", "filter_juventus": False, "include_reposts": False},
    {"handle": "BaridonMarco", "filter_juventus": False, "include_reposts": False},
)
X_RSS_MIRROR_TEMPLATES = (
    # Istanze Nitter dirette
    "https://nitter.net/{handle}/rss",
    "https://xcancel.com/{handle}/rss",
    "https://nitter.poast.org/{handle}/rss",
    "https://nitter.privacydev.net/{handle}/rss",
    "https://nitter.kylrth.com/{handle}/rss",
    "https://nitter.fdn.fr/{handle}/rss",
    # Aggregatori/load balancer (scelgono automaticamente un'istanza attiva)
    "https://twiiit.com/{handle}/rss",
    "https://farside.link/nitter/{handle}/rss",
)
X_RSS_TIMEOUT_SECONDS = 12
X_MEDIA_API_TEMPLATES = (
    "https://api.fxtwitter.com/status/{tweet_id}",
    "https://api.vxtwitter.com/status/{tweet_id}",
)
X_MEDIA_API_TIMEOUT_SECONDS = 12
# Telegram può scaricare da un URL remoto file non-foto fino a 20 MB.
# Teniamo un margine per l'audio, che non è incluso nel bitrate video.
TELEGRAM_REMOTE_VIDEO_TARGET_BYTES = 18_000_000
X_STATUS_PATH_RE = re.compile(r"^/([A-Za-z0-9_]+)/status/(\d+)$")
X_HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)
X_MARKER_TRANSLATION = str.maketrans("", "", "#@")

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
GAZZETTA_ENGLISH_PATH_RE = re.compile(r"^/en(?:/|$)", re.IGNORECASE)
X_JUVENTUS_MENTION_RE = re.compile(r"(?<!\w)@juventusfc\b", re.IGNORECASE)
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
    image_url: str = ""
    image_urls: tuple[str, ...] = ()
    video_url: str = ""
    video_thumbnail_url: str = ""

    @property
    def notification_key(self) -> str:
        """Chiave usata per non inviare due volte la stessa notizia."""
        return self.state_key or self.url

    @property
    def all_image_urls(self) -> tuple[str, ...]:
        """Tutte le immagini note dell'articolo (image_urls, con image_url come fallback)."""
        if self.image_urls:
            return self.image_urls
        if self.image_url:
            return (self.image_url,)
        return ()


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


def is_juventus_x_post(text: str) -> bool:
    """Accetta Juve/Juventus e la menzione dell'account ufficiale."""
    return is_juventus_title(text) or bool(
        X_JUVENTUS_MENTION_RE.search(text)
    )


def split_x_hashtag(hashtag: str) -> str:
    """Separa in parole un hashtag CamelCase, preservando gli acronimi."""
    words = []
    for segment in hashtag.split("_"):
        current_word = []
        for index, character in enumerate(segment):
            previous = segment[index - 1] if index else ""
            following = segment[index + 1] if index + 1 < len(segment) else ""
            starts_word = (
                bool(current_word)
                and character.isupper()
                and (
                    previous.islower()
                    or previous.isdigit()
                    or (previous.isupper() and following.islower())
                )
            )
            if starts_word:
                words.append("".join(current_word))
                current_word = []
            current_word.append(character)
        if current_word:
            words.append("".join(current_word))
    return " ".join(words)


def clean_x_text(text: str) -> str:
    """Pulisce hashtag e menzioni nei testi provenienti da X."""
    text = X_HASHTAG_RE.sub(
        lambda match: split_x_hashtag(match.group(1)),
        text,
    )
    return text.translate(X_MARKER_TRANSLATION)


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
        url_parts = urlsplit(url)
        host = url_parts.netloc.lower()
        if not (
            host == "www.gazzetta.it"
            or host == "video.gazzetta.it"
            or host.endswith(".gazzetta.it")
        ):
            continue
        # Il feed Juventus include anche le traduzioni inglesi pubblicate
        # sotto /en/: notifichiamo soltanto gli articoli in italiano.
        if GAZZETTA_ENGLISH_PATH_RE.match(url_parts.path):
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
        try:
            source_articles = _scrape_sky_calciomercato_for_date(
                session,
                requested_date,
            )
        except requests.HTTPError as error:
            response = error.response
            if response is not None and response.status_code == 404:
                continue
            raise

        for article in source_articles:
            articles_by_key.setdefault(article.notification_key, article)
    return list(articles_by_key.values())


def _walk_json_objects(value):
    """Visita ricorsivamente gli oggetti presenti nei blocchi JSON-LD."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_objects(child)


def _sky_structured_article(soup: BeautifulSoup) -> dict:
    """Restituisce il primo Article/NewsArticle trovato nei JSON-LD Sky."""
    article_types = {
        "Article",
        "NewsArticle",
        "ReportageNewsArticle",
        "VideoObject",
    }
    for script in soup.find_all(
        "script",
        attrs={"type": "application/ld+json"},
    ):
        try:
            payload = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue

        for item in _walk_json_objects(payload):
            raw_types = item.get("@type", ())
            item_types = (
                {raw_types}
                if isinstance(raw_types, str)
                else set(raw_types or ())
            )
            if item_types & article_types:
                return item
    return {}


def _first_meta_content(
    soup: BeautifulSoup,
    selectors: tuple[str, ...],
) -> str:
    for selector in selectors:
        tag = soup.select_one(selector)
        if not tag:
            continue
        content = str(tag.get("content") or "").strip()
        if content:
            return content
    return ""


def _schema_image_url(value, page_url: str) -> str:
    """Estrae un URL immagine dai formati JSON-LD più comuni."""
    candidates = value if isinstance(value, list) else [value]
    for candidate in candidates:
        if isinstance(candidate, dict):
            raw_url = candidate.get("url") or candidate.get("contentUrl")
        else:
            raw_url = candidate
        image_url = normalize_image_url(str(raw_url or ""), page_url)
        if image_url:
            return image_url
    return ""


def scrape_sky_juventus_news(
    session: requests.Session,
    requested_dates: set[date],
) -> list[Article]:
    """Monitora gli articoli pubblicati nella pagina Sky Sport Juventus."""
    response = session.get(SKY_JUVENTUS_NEWS_URL, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    candidate_urls: list[str] = []
    urls_done: set[str] = set()
    for link in soup.select("a[href]"):
        url = normalize_url(
            urljoin(SKY_JUVENTUS_NEWS_URL, str(link.get("href") or ""))
        )
        if urlsplit(url).netloc.lower() != "sport.sky.it":
            continue

        url_date = date_from_article_url(url)
        if (
            url_date is None
            or not is_requested_date(url_date, requested_dates)
            or url in urls_done
        ):
            continue

        urls_done.add(url)
        candidate_urls.append(url)

    articles: list[Article] = []
    for url in candidate_urls:
        try:
            article_response = session.get(url, timeout=30)
            article_response.raise_for_status()
        except requests.RequestException:
            # Un singolo articolo non deve bloccare tutta la fonte Sky.
            continue

        article_soup = BeautifulSoup(article_response.text, "html.parser")
        article_data = _sky_structured_article(article_soup)

        title = str(article_data.get("headline") or "").strip()
        if not title:
            title = _first_meta_content(
                article_soup,
                ('meta[property="og:title"]', 'meta[name="twitter:title"]'),
            )
        if not title:
            heading = article_soup.find("h1")
            title = heading.get_text(" ", strip=True) if heading else ""
        if not title:
            continue

        # La diretta mercato è già monitorata blocco per blocco dallo scraper
        # dedicato: qui evitiamo di inviare anche l'articolo contenitore.
        if (
            SKY_RECAP_TITLE_RE.search(title)
            or "calciomercato-news-trattative-oggi" in url
            or "calciomercato-news-" in url
        ):
            continue

        published = None
        raw_dates = (
            article_data.get("datePublished"),
            _first_meta_content(
                article_soup,
                (
                    'meta[property="article:published_time"]',
                    'meta[name="date"]',
                    'meta[name="pub_date"]',
                ),
            ),
        )
        for raw_date in raw_dates:
            if not raw_date:
                continue
            try:
                published = parse_iso_datetime(str(raw_date))
            except ValueError:
                continue
            break

        if published is None:
            time_tag = article_soup.select_one("time[datetime]")
            if time_tag:
                try:
                    published = parse_iso_datetime(
                        str(time_tag.get("datetime") or "")
                    )
                except ValueError:
                    published = None
        if published is None:
            published = date_from_article_url(url)
        if (
            published is None
            or not is_requested_date(published, requested_dates)
        ):
            continue

        summary = str(
            article_data.get("description")
            or article_data.get("abstract")
            or ""
        ).strip()
        if not summary:
            summary = _first_meta_content(
                article_soup,
                (
                    'meta[name="description"]',
                    'meta[property="og:description"]',
                ),
            )
        summary = BeautifulSoup(summary, "html.parser").get_text(
            " ", strip=True
        )

        tags = " ".join(
            str(tag.get("content") or "").strip()
            for tag in article_soup.select('meta[property="article:tag"]')
            if str(tag.get("content") or "").strip()
        )
        searchable_text = " ".join(
            part
            for part in (
                title,
                summary,
                str(article_data.get("keywords") or ""),
                str(article_data.get("about") or ""),
                tags,
            )
            if part
        )
        if (
            not JUVE_KEYWORD_RE.search(searchable_text)
            or SKY_EXCLUDED_TITLE_RE.search(searchable_text)
        ):
            continue

        image_url = _schema_image_url(article_data.get("image"), url)
        articles.append(
            Article(
                source="Sky Sport - Juventus",
                title=title,
                url=url,
                published=published,
                summary=summary,
                image_url=image_url,
            )
        )

    return articles


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
    """Recupera le notizie che citano "Juventus" nel titolo o nel testo."""
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
        if not title:
            continue

        # La card della home contiene spesso anche l'anteprima dell'articolo.
        # Serve per intercettare notizie il cui titolo non nomina la Juventus.
        preview_text = link.get_text(" ", strip=True)

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

        article_title = str(article_data.get("headline") or title).strip()
        summary = BeautifulSoup(
            str(article_data.get("abstract") or ""),
            "html.parser",
        ).get_text(" ", strip=True)
        article_body = BeautifulSoup(
            str(article_data.get("articleBody") or ""),
            "html.parser",
        ).get_text(" ", strip=True)

        description_tag = article_soup.select_one(
            'meta[name="description"], meta[property="og:description"]'
        )
        meta_description = (
            str(description_tag.get("content") or "").strip()
            if description_tag
            else ""
        )

        # Il controllo viene eseguito soltanto sul contenuto della singola
        # notizia, evitando menu, articoli correlati e sezioni globali del sito.
        searchable_text = " ".join(
            part
            for part in (
                title,
                preview_text,
                article_title,
                summary,
                article_body,
                meta_description,
            )
            if part
        )
        if not JUVENTUS_KEYWORD_RE.search(searchable_text):
            continue

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


def is_youtube_short(session: requests.Session, video_id: str) -> bool:
    """Restituisce True se il video ID appartiene a uno YouTube Short."""
    shorts_url = YOUTUBE_SHORTS_URL_TEMPLATE.format(video_id=video_id)
    try:
        response = session.get(shorts_url, timeout=15, allow_redirects=True)
        response.raise_for_status()
    except requests.RequestException:
        # Se YouTube non consente la verifica, non blocchiamo un video normale.
        # Nessun log dedicato agli Shorts.
        return False

    expected_path = f"/shorts/{video_id}"
    final_path = urlsplit(response.url).path.rstrip("/")
    if final_path == expected_path:
        return True

    # Fallback: in alcuni casi YouTube mantiene/riscrive l'URL lato pagina.
    # Il canonical permette comunque di riconoscere lo stesso Short.
    soup = BeautifulSoup(response.text, "html.parser")
    canonical = soup.find("link", rel="canonical", href=True)
    if canonical:
        canonical_path = urlsplit(str(canonical.get("href") or "")).path.rstrip("/")
        if canonical_path == expected_path:
            return True

    return False


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
            if is_youtube_short(session, video_id):
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
                    image_url=(
                        f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
                    ),
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


def _rss_item_images(item: ET.Element, page_url: str = "") -> list[str]:
    """Estrae tutte le foto da media:content/media:thumbnail, enclosure ed
    eventuale HTML del feed RSS, mantenendo l'ordine e senza duplicati."""
    images: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        image_url = normalize_image_url(candidate, page_url)
        if image_url and image_url not in seen:
            seen.add(image_url)
            images.append(image_url)

    for child in item.iter():
        local_name = child.tag.rsplit("}", 1)[-1].lower()
        media_type = child.attrib.get("type", "").lower()
        if local_name == "thumbnail" or (
            local_name == "content"
            and (not media_type or media_type.startswith("image/"))
        ):
            add(child.attrib.get("url", ""))
        elif local_name == "enclosure" and media_type.startswith("image/"):
            add(child.attrib.get("url", ""))

    description = item.findtext("description", default="")
    if description:
        for image in BeautifulSoup(description, "html.parser").find_all(
            "img", src=True
        ):
            add(image.get("src", ""))

    return images


def _rss_item_video_thumbnail(item: ET.Element, page_url: str = "") -> str:
    description = item.findtext("description", default="")
    if not description:
        return ""
    video = BeautifulSoup(description, "html.parser").select_one("video[poster]")
    if not video:
        return ""
    return normalize_image_url(str(video.get("poster") or ""), page_url)


def _rss_item_has_native_video(item: ET.Element) -> bool:
    """Riconosce il marcatore usato da Nitter per i video nativi di X."""
    description = item.findtext("description", default="")
    return bool(
        re.search(
            r"<br\s*/?>\s*Video\s*<br\s*/?>",
            description,
            flags=re.IGNORECASE,
        )
    )


def _best_x_mp4(media: dict) -> str:
    """Sceglie la variante MP4 migliore che Telegram può leggere da URL."""
    raw_variants = media.get("formats") or media.get("variants") or ()
    variants: list[tuple[int, str]] = []
    for variant in raw_variants:
        if not isinstance(variant, dict):
            continue
        container = str(
            variant.get("container") or variant.get("content_type") or ""
        ).lower()
        candidate = normalize_image_url(str(variant.get("url") or ""))
        if not candidate or "mp4" not in container:
            continue
        try:
            bitrate = max(int(variant.get("bitrate") or 0), 0)
        except (TypeError, ValueError):
            bitrate = 0
        variants.append((bitrate, candidate))

    if variants:
        variants.sort(key=lambda item: item[0])
        try:
            duration = float(media.get("duration") or 0)
            if not duration and media.get("duration_millis"):
                duration = float(media["duration_millis"]) / 1000
        except (TypeError, ValueError):
            duration = 0

        if duration > 0:
            fitting = [
                variant
                for variant in variants
                if duration * (variant[0] + 160_000) / 8
                <= TELEGRAM_REMOTE_VIDEO_TARGET_BYTES
            ]
            if fitting:
                return fitting[-1][1]
            return variants[0][1]
        return variants[-1][1]

    return normalize_image_url(str(media.get("url") or ""))


@dataclass(frozen=True)
class XMedia:
    video_url: str = ""
    video_thumbnail_url: str = ""
    image_urls: tuple[str, ...] = ()


def _x_media_from_payload(payload: dict) -> XMedia:
    """Legge video e foto sia da FxTwitter sia da VxTwitter."""
    tweet = payload.get("tweet")
    if isinstance(tweet, dict):
        media = tweet.get("media")
        if isinstance(media, dict):
            image_urls = tuple(
                image_url
                for photo in (media.get("photos") or ())
                if isinstance(photo, dict)
                if (image_url := normalize_image_url(str(photo.get("url") or "")))
            )
            videos = media.get("videos") or ()
            for video in videos:
                if isinstance(video, dict):
                    video_url = _best_x_mp4(video)
                    if video_url:
                        return XMedia(
                            video_url=video_url,
                            video_thumbnail_url=normalize_image_url(
                                str(video.get("thumbnail_url") or "")
                            ),
                            image_urls=image_urls,
                        )

    extended_media = payload.get("media_extended") or ()
    image_urls = tuple(
        image_url
        for media in extended_media
        if isinstance(media, dict)
        if str(media.get("type") or "").lower() in {"image", "photo"}
        if (image_url := normalize_image_url(str(media.get("url") or "")))
    )
    for media in extended_media:
        if not isinstance(media, dict):
            continue
        # Le GIF di X sono MP4 senza audio, ma Telegram le mostra come
        # animazioni in loop: vengono escluse esplicitamente.
        if str(media.get("type") or "").lower() != "video":
            continue
        video_url = _best_x_mp4(media)
        if video_url:
            return XMedia(
                video_url=video_url,
                video_thumbnail_url=normalize_image_url(
                    str(media.get("thumbnail_url") or "")
                ),
                image_urls=image_urls,
            )
    return XMedia(image_urls=image_urls)


def _resolve_x_media(tweet_id: str) -> XMedia:
    """Recupera i media da API pubbliche, senza bloccare le altre fonti."""
    for template in X_MEDIA_API_TEMPLATES:
        try:
            response = requests.get(
                template.format(tweet_id=tweet_id),
                headers=HEADERS,
                timeout=X_MEDIA_API_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        media = _x_media_from_payload(payload)
        if media.video_url:
            return media
    return XMedia()


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
                    and not is_juventus_x_post(title)
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
                image_urls = tuple(_rss_item_images(item, raw_link))
                rss_image_urls = image_urls
                video_url = ""
                video_thumbnail_url = _rss_item_video_thumbnail(item, raw_link)
                if _rss_item_has_native_video(item):
                    x_media = _resolve_x_media(tweet_id)
                    video_url = x_media.video_url
                    if video_url:
                        # Il tag <img> di Nitter è la copertina del video,
                        # non una foto separata da includere nell'album.
                        image_urls = x_media.image_urls
                        video_thumbnail_url = x_media.video_thumbnail_url or (
                            rss_image_urls[0] if rss_image_urls else ""
                        )
                elif video_thumbnail_url:
                    # Il tag <video> nei feed Nitter rappresenta una GIF di X.
                    # Non inviamo l'MP4 animato: usiamo soltanto il poster
                    # statico quando non ci sono vere foto nel post.
                    if not image_urls:
                        image_urls = (video_thumbnail_url,)
                    video_thumbnail_url = ""
                articles.append(
                    Article(
                        source=f"X - {handle}",
                        title=clean_x_text(title),
                        url=tweet_url,
                        published=published,
                        state_key=state_key,
                        image_url=image_urls[0] if image_urls else "",
                        image_urls=image_urls,
                        video_url=video_url,
                        video_thumbnail_url=video_thumbnail_url,
                    )
                )

    return articles


def load_seen(state_date: date) -> list[str]:
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"Stato non leggibile ({STATE_FILE.name}); "
            "interrompo per evitare notifiche duplicate."
        ) from error
    # Migrazione trasparente dal vecchio formato, che era una semplice lista.
    if isinstance(data, list) and all(isinstance(item, str) for item in data):
        values = list(dict.fromkeys(data))
        save_seen(values, state_date)
        return values

    if not isinstance(data, dict):
        raise RuntimeError(
            f"Formato non valido in {STATE_FILE.name}; "
            "interrompo per evitare notifiche duplicate."
        )
    stored_date = data.get("date")
    items = data.get("items")
    if not isinstance(stored_date, str) or not isinstance(items, list) or not all(
        isinstance(item, str) for item in items
    ):
        raise RuntimeError(
            f"Formato non valido in {STATE_FILE.name}; "
            "interrompo per evitare notifiche duplicate."
        )

    if stored_date != state_date.isoformat():
        save_seen([], state_date)
        print(
            f"[STATO] nuovo giorno ({state_date.isoformat()}): "
            "cache delle notizie inviate azzerata."
        )
        return []
    return list(dict.fromkeys(items))


def save_seen(seen: Iterable[str], state_date: date) -> None:
    values = list(dict.fromkeys(seen))[-MAX_SEEN:]
    temporary = STATE_FILE.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            {"date": state_date.isoformat(), "items": values},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(temporary, STATE_FILE)


def article_from_journal(entry: dict) -> Article:
    try:
        return Article(
            source=str(entry["source"]),
            title=str(entry["title"]),
            url=str(entry["url"]),
            published=parse_iso_datetime(str(entry["published"])),
            summary=str(entry.get("summary", "")),
            state_key=str(entry.get("state_key", "")),
            image_url=str(entry.get("image_url", "")),
            image_urls=tuple(entry.get("image_urls") or ()),
            video_url=str(entry.get("video_url", "")),
            video_thumbnail_url=str(entry.get("video_thumbnail_url", "")),
        )
    except (KeyError, ValueError, TypeError) as error:
        raise RuntimeError(
            f"Notizia non valida in {PENDING_FILE.name}."
        ) from error


def collect_articles(
    session: requests.Session,
    requested_dates: set[date],
    on_article: Callable[[Article], None] | None = None,
) -> tuple[list[Article], list[str]]:
    scrapers = (
        ("Tuttosport", scrape_tuttosport),
        ("Corriere dello Sport", scrape_corriere),
        ("La Gazzetta dello Sport", scrape_gazzetta),
        ("Sky Sport - Calciomercato", scrape_sky_calciomercato),
        ("Sky Sport - Juventus", scrape_sky_juventus_news),
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
            if article.notification_key in articles_by_key:
                continue
            articles_by_key[article.notification_key] = article
            if on_article is not None:
                on_article(article)

    if len(errors) == len(scrapers):
        raise RuntimeError("Nessuna fonte è stata recuperata correttamente.")

    return list(articles_by_key.values()), errors


def run(
    dry_run: bool = False,
    include_yesterday: bool = False,
    preview_messages: bool = False,
) -> None:
    today = datetime.now(ROME).date()
    requested_dates = {today}
    if include_yesterday:
        requested_dates.add(today - timedelta(days=1))
    session = requests.Session()
    session.headers.update(HEADERS)

    if dry_run:
        articles, _ = collect_articles(session, requested_dates)
    else:
        token = os.environ.get("TELEGRAM_TOKEN")
        chat_id = os.environ.get("CHAT_ID")
        if not token or not chat_id:
            raise RuntimeError(
                "Secret mancanti: configura TELEGRAM_TOKEN e CHAT_ID."
            )

        seen_list = load_seen(today)
        seen = set(seen_list)
        journal = ArticleJournal(PENDING_FILE)
        cleaned = journal.discard_all(seen)
        if cleaned:
            print(f"[STATO] rimosse {cleaned} notizie già inviate dal journal.")
        print(f"[STATO] articoli già notificati: {len(seen)}")
        print(f"[STATO] articoli in attesa: {len(journal.entries)}")

        def save_discovered(article: Article) -> None:
            if article.notification_key in seen:
                return
            if journal.add(article):
                print(
                    f"[STATO] salvata subito nel journal: "
                    f"{article.source} | {article.title}"
                )

        articles, _ = collect_articles(
            session,
            requested_dates,
            on_article=save_discovered,
        )


    # I siti mostrano prima le notizie più recenti. Telegram le riceve invece
    # dalla più vecchia alla più nuova, per mantenere l'ordine cronologico.
    articles.sort(key=lambda item: (item.published, item.source, item.title))

    if dry_run:
        preview_resolver = PreviewImageResolver(session)
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
            if preview_messages:
                image_urls = preview_resolver.resolve_all(
                    article.url,
                    article.all_image_urls,
                )
                print("\n--- ANTEPRIMA TELEGRAM ---")
                if article.video_url:
                    print(f"[VIDEO] {article.video_url}")
                if article.video_thumbnail_url:
                    print(f"[COPERTINA VIDEO] {article.video_thumbnail_url}")
                if image_urls:
                    print(f"[FOTO] {len(image_urls)}: {', '.join(image_urls)}")
                else:
                    print("[FOTO] nessuna")
                print(
                    format_article_message(
                        article,
                        max_length=(
                            TELEGRAM_MAX_CAPTION_LENGTH
                            if image_urls or article.video_url
                            else TELEGRAM_MAX_MESSAGE_LENGTH
                        ),
                    )
                )
                print("--- FINE ANTEPRIMA ---\n")
        return

    baseline_if_missing = os.environ.get(
        "BASELINE_IF_NO_STATE",
        "",
    ).lower() in {"1", "true", "yes"}
    if baseline_if_missing and not STATE_FILE.exists():
        seen_list = [
            str(entry["notification_key"])
            for entry in journal.entries
        ]
        save_seen(seen_list, today)
        journal.clear()
        print(
            "[STATO] cache iniziale assente: "
            f"registrate {len(seen_list)} notizie correnti senza reinviarle."
        )
        return

    pending = [article_from_journal(entry) for entry in journal.entries]
    pending.sort(key=lambda item: (item.published, item.source, item.title))
    if not pending:
        print("[NEWS] nessuna nuova notizia di oggi.")
        return

    telegram = TelegramClient(token, chat_id)
    preview_resolver = PreviewImageResolver(session)
    sent_count = 0
    for article in pending:
        image_urls = preview_resolver.resolve_all(article.url, article.all_image_urls)
        if article.video_url:
            try:
                with prepare_telegram_video(
                    session,
                    article.video_url,
                ) as video_file:
                    receipt = telegram.send_article(
                        article,
                        video_file_path=str(video_file),
                        video_thumbnail_url=article.video_thumbnail_url,
                        photo_urls=image_urls,
                    )
            except VideoPreparationError as error:
                fallback_images = image_urls or (
                    [article.video_thumbnail_url]
                    if article.video_thumbnail_url
                    else []
                )
                print(
                    f"[NEWS] video non preparabile ({error}): "
                    "uso il fallback statico."
                )
                receipt = telegram.send_article(
                    article,
                    photo_urls=fallback_images,
                )
        else:
            receipt = telegram.send_article(
                article,
                photo_urls=image_urls,
            )
        print(
            f"[NEWS] notificato da {article.source}: {article.title} "
            f"(modalità={receipt.mode}, "
            f"message_id={receipt.message_id or 'non disponibile'})"
        )
        if receipt.photo_fallback:
            print(
                "[NEWS] foto/album non accettati da Telegram: "
                f"inviato in modalità {receipt.mode}."
            )
        if receipt.video_fallback:
            print(
                "[NEWS] video non accettato da Telegram: "
                f"inviato in modalità {receipt.mode}."
            )
        seen.add(article.notification_key)
        seen_list.append(article.notification_key)
        save_seen(seen_list, today)
        journal.remove(article.notification_key)
        sent_count += 1
        time.sleep(0.8)

    print(f"[NEWS] notifiche inviate: {sent_count}")


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
    parser.add_argument(
        "--preview-messages",
        action="store_true",
        help=(
            "Con --dry-run mostra il testo HTML esatto che verrebbe inviato "
            "a Telegram."
        ),
    )
    args = parser.parse_args()
    if args.preview_messages and not args.dry_run:
        parser.error("--preview-messages richiede --dry-run")
    run(
        dry_run=args.dry_run,
        include_yesterday=args.include_yesterday,
        preview_messages=args.preview_messages,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Errore: {error}", file=sys.stderr)
        sys.exit(1)
