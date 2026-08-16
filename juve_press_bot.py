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
- YouTube: Juventus, Fabrizio Romano e Romeo Agresti
- X: profili configurati (filtri e repost definiti per account)

Ogni notizia viene inviata su Telegram una sola volta. Lo stato è salvato nel file
.seen_juve_press_news.json accanto allo script.
"""

import argparse
import json
import os
import re
import subprocess
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
    DeliveryReceipt,
    TELEGRAM_MAX_CAPTION_LENGTH,
    TELEGRAM_MAX_MESSAGE_LENGTH,
    TelegramClient,
    TelegramDeliveryError,
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
YESTERDAY_COLLECTION_START_MINUTE = 23 * 60 + 50
SOURCE_MAX_WORKERS = 6
DEFAULT_WORKER_DURATION_SECONDS = 55 * 60
DEFAULT_POLL_INTERVAL_SECONDS = 15
STATE_CHECKPOINT_ENV = "CHECKPOINT_STATE_TO_GIT"
HEARTBEAT_FILE_ENV = "WORKER_HEARTBEAT_FILE"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}


def compact_log_text(value: object, limit: int = 90) -> str:
    """Rende leggibili i log senza stampare titoli o errori interminabili."""
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


TUTTOSPORT_URL = "https://www.tuttosport.com/squadra/calcio/juventus/t128"
TUTTOSPORT_RSS_URL = "https://www.tuttosport.com/rss/calcio/serie-a/juventus"
CORRIERE_URL = (
    "https://www.corrieredellosport.it/squadra/calcio/juventus/t128"
)
CORRIERE_RSS_URL = "https://www.corrieredellosport.it/rss/calcio/serie-a/juve"
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
        "source": "YouTube - Fabrizio Romano",
        "channel_id": "UC7pT9g1-oKwVgbpipZODvBA",
        "channel_url": "https://www.youtube.com/@FabrizioRomanoItaliano",
    },
    {
        "source": "YouTube - Romeo Agresti",
        "channel_id": "UCmlXlTE2oTArVL8DafyRsXA",
        "channel_url": "https://www.youtube.com/@RomeoAgresti",
    },
)
YOUTUBE_API_URL = "https://www.googleapis.com/youtube/v3"
YOUTUBE_API_KEY_ENV = "YOUTUBE_API_KEY"
YOUTUBE_CHANNELS_PER_CYCLE_ENV = "YOUTUBE_CHANNELS_PER_CYCLE"
YOUTUBE_SHORTS_URL_TEMPLATE = "https://www.youtube.com/shorts/{video_id}"

# Cache in memoria per tutta la durata del worker.
# La playlist uploads di ogni canale non cambia durante il processo, quindi
# basta recuperarla una sola volta tramite channels.list.
YOUTUBE_UPLOAD_PLAYLISTS: dict[str, str] = {}
YOUTUBE_SHORT_CACHE: dict[str, bool] = {}
YOUTUBE_CHANNEL_CURSOR = 0
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
    {"handle": "GiovaAlbanese", "filter_juventus": False, "include_reposts": False},
    {"handle": "@David_Ornstein", "filter_juventus": True, "include_reposts": False},
    {"handle": "@Plettigoal", "filter_juventus": True, "include_reposts": False},
    {"handle": "@SkySportsNews", "filter_juventus": True, "include_reposts": False},
    {"handle": "@SkySportDE", "filter_juventus": True, "include_reposts": False},
    {"handle": "@Tanziloic", "filter_juventus": True, "include_reposts": False},
    {"handle": "@JacobsBen", "filter_juventus": True, "include_reposts": False},
    {"handle": "@sachatavolieri", "filter_juventus": True, "include_reposts": False},
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
X_REPOST_RE = re.compile(r"^RT(?:\s+by)?\s+@", re.IGNORECASE)
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
    r"(gen|feb|mar|apr|mag|giu|lug|ago|set|ott|nov|dic)\s+"    r"(\d{1,2}):(\d{2})\b",
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
ITALIAN_MONTHS = {
    "gennaio": 1,
    "febbraio": 2,
    "marzo": 3,
    "aprile": 4,
    "maggio": 5,
    "giugno": 6,
    "luglio": 7,
    "agosto": 8,
    "settembre": 9,
    "ottobre": 10,
    "novembre": 11,
    "dicembre": 12,
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


class CollectionError(RuntimeError):
    """Errore transitorio quando nessuna fonte risponde durante un ciclo."""


class StateCheckpointError(RuntimeError):
    """Errore durante il salvataggio immediato dello stato su GitHub."""


def touch_worker_heartbeat() -> None:
    """Aggiorna il battito usato dal watchdog del workflow GitHub Actions."""
    raw_path = os.environ.get(HEARTBEAT_FILE_ENV, "").strip()
    if not raw_path:
        return
    try:
        Path(raw_path).touch()
    except OSError as error:
        print(f"[WORKER] heartbeat non aggiornabile: {error}")


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


def collection_dates(
    today: date,
    coverage_start: date | None = None,
) -> set[date]:
    """Include ieri appena lo stato contiene una deduplica completa per quel giorno."""
    requested_dates = {today}
    yesterday = today - timedelta(days=1)
    if coverage_start is None or coverage_start <= yesterday:
        requested_dates.add(yesterday)
    return requested_dates


def is_collection_candidate(
    published: datetime,
    requested_dates: set[date],
) -> bool:
    """Accetta oggi e, per ieri, soltanto la fascia dalle 23:50 in poi."""
    if not requested_dates:
        return False

    local_published = published.astimezone(ROME)
    published_date = local_published.date()
    collection_day = max(requested_dates)
    if published_date not in requested_dates:
        return False
    if published_date == collection_day:
        return True
    if published_date != collection_day - timedelta(days=1):
        return False

    published_minute = local_published.hour * 60 + local_published.minute
    return published_minute >= YESTERDAY_COLLECTION_START_MINUTE


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
        heading = card.find(["h2", "h3"])
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


def scrape_rss_source(
    session: requests.Session,
    *,
    source: str,
    feed_url: str,
    base_url: str,
    allowed_hosts: set[str],
    requested_dates: set[date],
) -> list[Article]:
    response = session.get(feed_url, timeout=30)
    response.raise_for_status()
    return _feed_articles_from_xml(
        response.content,
        source=source,
        base_url=base_url,
        allowed_hosts=allowed_hosts,
        requested_dates=requested_dates,
    )


def scrape_tuttosport(
    session: requests.Session,
    requested_dates: set[date],
) -> list[Article]:
    try:
        return scrape_rss_source(
            session,
            source="Tuttosport",
            feed_url=TUTTOSPORT_RSS_URL,
            base_url=TUTTOSPORT_URL,
            allowed_hosts={"www.tuttosport.com", "tuttosport.com"},
            requested_dates=requested_dates,
        )
    except (requests.RequestException, ET.ParseError, ValueError) as error:
        print(f"[RSS] Tuttosport: fallback HTML ({compact_log_text(error, 70)})")
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
    try:
        return scrape_rss_source(
            session,
            source="Corriere dello Sport",
            feed_url=CORRIERE_RSS_URL,
            base_url=CORRIERE_URL,
            allowed_hosts={"www.corrieredellosport.it", "corrieredellosport.it"},
            requested_dates=requested_dates,
        )
    except (requests.RequestException, ET.ParseError, ValueError) as error:
        print(f"[RSS] Corriere dello Sport: fallback HTML ({compact_log_text(error, 70)})")
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
        # I TAG SEO di Sky possono finire attaccati al titolo del blocco.
        # Vanno rimossi prima del filtro Juve/Juventus, altrimenti una news
        # su un'altra squadra può diventare un falso positivo.
        title = re.split(
            r"\s*\bTAG:\s*",
            title,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()
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
        articles.append(            Article(
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


def _clean_feed_text(value: str) -> str:
    """Converte HTML/XML di titolo o sommario in testo semplice."""
    return BeautifulSoup(str(value or ""), "html.parser").get_text(
        " ", strip=True
    )


def _feed_item_text(item: ET.Element, *names: str) -> str:
    """Legge un campo RSS anche quando usa namespace (es. dc:date)."""
    wanted = {name.casefold() for name in names}
    for child in item.iter():
        local_name = child.tag.rsplit("}", 1)[-1].casefold()
        if local_name not in wanted:
            continue
        value = (child.text or "").strip()
        if value:
            return value
    return ""


def _feed_item_link(item: ET.Element) -> str:
    """Restituisce il link di un item RSS o di una entry Atom."""
    for child in item.iter():
        if child.tag.rsplit("}", 1)[-1].casefold() != "link":
            continue
        href = str(child.get("href") or "").strip()
        value = href or (child.text or "").strip()
        if value:
            return value
    return ""


def _parse_feed_published(raw_value: str) -> datetime | None:
    """Supporta sia RFC 2822 dei feed RSS sia date ISO/Atom."""
    raw_value = str(raw_value or "").strip()
    if not raw_value:
        return None
    try:
        parsed = parsedate_to_datetime(raw_value)
    except (TypeError, ValueError, OverflowError):
        try:
            return parse_iso_datetime(raw_value)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ROME)
    return parsed.astimezone(ROME)


def _feed_articles_from_xml(
    content: bytes | str,
    *,
    source: str,
    base_url: str,
    allowed_hosts: set[str],
    requested_dates: set[date],
    juventus_only: bool = False,
) -> list[Article]:
    """Converte un feed RSS/Atom in Article applicando il filtro data."""
    root = ET.fromstring(content)
    nodes = list(root.findall(".//item"))
    if not nodes:
        nodes = [
            node
            for node in root.iter()
            if node.tag.rsplit("}", 1)[-1].casefold() == "entry"
        ]

    articles: list[Article] = []
    urls_done: set[str] = set()
    for item in nodes:
        title = _clean_feed_text(_feed_item_text(item, "title"))
        raw_link = _feed_item_link(item) or _feed_item_text(item, "guid")
        raw_published = _feed_item_text(
            item,
            "pubDate",
            "published",
            "updated",
            "date",
        )
        if not title or not raw_link or not raw_published:
            continue

        published = _parse_feed_published(raw_published)
        if (
            published is None
            or not is_requested_date(published, requested_dates)
        ):
            continue

        url = normalize_url(urljoin(base_url, raw_link))
        if urlsplit(url).netloc.lower() not in allowed_hosts:
            continue
        if url in urls_done:
            continue

        summary = _clean_feed_text(
            _feed_item_text(item, "description", "summary", "content")
        )
        if juventus_only:
            searchable_text = " ".join(part for part in (title, summary) if part)
            if not is_juventus_title(searchable_text):
                continue

        image_url = ""
        for child in item.iter():
            local_name = child.tag.rsplit("}", 1)[-1].casefold()
            if local_name not in {"enclosure", "content", "thumbnail"}:
                continue
            raw_image = str(child.get("url") or "").strip()
            media_type = str(child.get("type") or "").casefold()
            if not raw_image:
                continue
            if media_type and not media_type.startswith("image/"):
                continue
            image_url = normalize_image_url(raw_image, url)
            if image_url:
                break

        urls_done.add(url)
        articles.append(
            Article(
                source=source,
                title=title,
                url=url,
                published=published,
                summary=summary,
                image_url=image_url,
            )
        )
    return articles


def _generic_article_metadata(
    soup: BeautifulSoup,
    page_url: str,
) -> tuple[str, datetime | None, str, str]:
    """Estrae titolo, data, sommario e immagine da una pagina articolo."""
    article_data = _sky_structured_article(soup)

    title = str(article_data.get("headline") or "").strip()
    if not title:
        title = _first_meta_content(
            soup,
            ('meta[property="og:title"]', 'meta[name="twitter:title"]'),
        )
    if not title:
        heading = soup.find("h1")
        title = heading.get_text(" ", strip=True) if heading else ""

    published = None
    raw_dates = (
        article_data.get("datePublished"),
        _first_meta_content(
            soup,
            (
                'meta[property="article:published_time"]',
                'meta[name="date"]',
                'meta[name="pub_date"]',
                'meta[itemprop="datePublished"]',
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
        time_tag = soup.select_one("time[datetime]")
        if time_tag:
            try:
                published = parse_iso_datetime(
                    str(time_tag.get("datetime") or "")
                )
            except ValueError:
                published = None

    summary = str(
        article_data.get("description")
        or article_data.get("abstract")
        or ""
    ).strip()
    if not summary:
        summary = _first_meta_content(
            soup,
            ('meta[name="description"]', 'meta[property="og:description"]'),
        )
    summary = _clean_feed_text(summary)

    image_url = _schema_image_url(article_data.get("image"), page_url)
    if not image_url:
        image_url = normalize_image_url(
            _first_meta_content(
                soup,
                ('meta[property="og:image"]', 'meta[name="twitter:image"]'),
            ),
            page_url,
        )

    return title, published, summary, image_url


def _parse_italian_calendar_date(text: str) -> datetime | None:
    """Fallback per date testuali come '10 Agosto 2026'."""
    match = re.search(
        r"\b(\d{1,2})\s+"
        r"(gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|"
        r"settembre|ottobre|novembre|dicembre)\s+"
        r"(\d{4})\b",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    month = ITALIAN_MONTHS.get(match.group(2).casefold())
    if not month:
        return None
    try:
        return datetime(
            int(match.group(3)),
            month,
            int(match.group(1)),
            tzinfo=ROME,
        )
    except ValueError:
        return None




def _scrape_article_detail_candidates(
    session: requests.Session,
    *,
    source: str,
    candidate_urls: list[str],
    requested_dates: set[date],
    juventus_only: bool,
    visible_date_parser: Callable[[BeautifulSoup], datetime | None] | None = None,
) -> list[Article]:
    """Fallback: apre solo i candidati e verifica la data sull'articolo."""
    articles: list[Article] = []
    for url in candidate_urls:
        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
        except requests.RequestException:
            continue
        soup = BeautifulSoup(response.text, "html.parser")
        title, published, summary, image_url = _generic_article_metadata(
            soup,
            url,
        )
        if published is None and visible_date_parser is not None:
            published = visible_date_parser(soup)
        if (
            published is None
            or not is_requested_date(published, requested_dates)
            or not title
        ):
            continue
        if juventus_only:
            article_data = _sky_structured_article(soup)
            body = _clean_feed_text(str(article_data.get("articleBody") or ""))
            searchable_text = " ".join(
                part for part in (title, summary, body) if part
            )
            if not is_juventus_title(searchable_text):
                continue
        articles.append(
            Article(
                source=source,
                title=title,
                url=url,
                published=published,
                summary=summary,
                image_url=image_url,
            )
        )
    return articles






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
            Article(                source="Borsa Italiana",
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


def _youtube_api_get(
    session: requests.Session,
    endpoint: str,
    params: dict[str, object],
) -> dict:
    """Chiama YouTube Data API v3 usando la chiave configurata nei Secrets."""
    api_key = os.environ.get(YOUTUBE_API_KEY_ENV, "").strip()
    if not api_key:
        raise ValueError(
            f"Secret {YOUTUBE_API_KEY_ENV} mancante: "
            "YouTube Data API non configurata."
        )

    request_params = dict(params)
    request_params["key"] = api_key
    response = session.get(
        f"{YOUTUBE_API_URL}/{endpoint}",
        params=request_params,
        timeout=30,
    )

    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        detail = ""
        try:
            payload = response.json()
            api_error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(api_error, dict):
                message = str(api_error.get("message") or "").strip()
                reasons = api_error.get("errors") or []
                reason = ""
                if reasons and isinstance(reasons[0], dict):
                    reason = str(reasons[0].get("reason") or "").strip()
                detail = " | ".join(
                    part for part in (reason, message) if part
                )
        except ValueError:
            pass

        if detail:
            raise requests.HTTPError(
                f"YouTube Data API HTTP {response.status_code}: {detail}",
                response=response,
            ) from error
        raise

    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Risposta YouTube Data API non valida.")
    return payload


def _youtube_upload_playlists(
    session: requests.Session,
) -> dict[str, str]:
    """Recupera e memorizza la playlist uploads dei canali configurati."""
    if YOUTUBE_UPLOAD_PLAYLISTS:
        return YOUTUBE_UPLOAD_PLAYLISTS

    channel_ids = ",".join(
        str(channel["channel_id"])
        for channel in YOUTUBE_CHANNELS
    )
    payload = _youtube_api_get(
        session,
        "channels",
        {
            "part": "contentDetails",
            "id": channel_ids,
            "maxResults": len(YOUTUBE_CHANNELS),
        },
    )

    for item in payload.get("items", []):
        if not isinstance(item, dict):
            continue
        channel_id = str(item.get("id") or "").strip()
        content_details = item.get("contentDetails") or {}
        related = (
            content_details.get("relatedPlaylists") or {}
            if isinstance(content_details, dict)
            else {}
        )
        uploads_id = (
            str(related.get("uploads") or "").strip()
            if isinstance(related, dict)
            else ""
        )
        if channel_id and uploads_id:
            YOUTUBE_UPLOAD_PLAYLISTS[channel_id] = uploads_id

    missing = [
        str(channel["source"])
        for channel in YOUTUBE_CHANNELS
        if str(channel["channel_id"]) not in YOUTUBE_UPLOAD_PLAYLISTS
    ]
    if missing:
        print(
            "[YOUTUBE API] playlist uploads non trovata per: "
            + ", ".join(missing)
        )

    return YOUTUBE_UPLOAD_PLAYLISTS


def _youtube_channels_for_cycle() -> tuple[dict, ...]:
    """Ruota i canali per limitare il consumo della quota API giornaliera."""
    global YOUTUBE_CHANNEL_CURSOR

    channels = tuple(YOUTUBE_CHANNELS)
    if not channels:
        return ()

    raw_count = os.environ.get(
        YOUTUBE_CHANNELS_PER_CYCLE_ENV,
        str(len(channels)),
    ).strip()
    try:
        per_cycle = int(raw_count)
    except ValueError:
        per_cycle = len(channels)
    per_cycle = max(1, min(per_cycle, len(channels)))

    if per_cycle >= len(channels):
        return channels

    selected = tuple(
        channels[(YOUTUBE_CHANNEL_CURSOR + offset) % len(channels)]
        for offset in range(per_cycle)
    )
    YOUTUBE_CHANNEL_CURSOR = (
        YOUTUBE_CHANNEL_CURSOR + per_cycle
    ) % len(channels)
    return selected


def _is_youtube_short_cached(
    session: requests.Session,
    video_id: str,
) -> bool:
    """Evita di verificare continuamente lo stesso video sulla pagina Shorts."""
    if video_id not in YOUTUBE_SHORT_CACHE:
        YOUTUBE_SHORT_CACHE[video_id] = is_youtube_short(session, video_id)
    return YOUTUBE_SHORT_CACHE[video_id]


def scrape_youtube_channels(
    session: requests.Session,
    requested_dates: set[date],
) -> list[Article]:
    """Recupera i nuovi video tramite YouTube Data API v3, senza feed RSS."""
    upload_playlists = _youtube_upload_playlists(session)
    articles: list[Article] = []
    keys_done: set[str] = set()
    channel_errors: list[Exception] = []
    selected_channels = _youtube_channels_for_cycle()

    for channel in selected_channels:
        channel_id = str(channel["channel_id"])
        playlist_id = upload_playlists.get(channel_id)
        if not playlist_id:
            continue

        try:
            payload = _youtube_api_get(
                session,
                "playlistItems",
                {
                    "part": "snippet,contentDetails",
                    "playlistId": playlist_id,
                    "maxResults": 10,
                },
            )
        except requests.RequestException as error:
            channel_errors.append(error)
            print(
                f"[YOUTUBE API] {channel['source']}: "
                f"{compact_log_text(error, 70)}"
            )
            continue
        for item in payload.get("items", []):
            if not isinstance(item, dict):
                continue
            snippet = item.get("snippet") or {}
            content_details = item.get("contentDetails") or {}
            if not isinstance(snippet, dict) or not isinstance(
                content_details,
                dict,
            ):
                continue

            resource_id = snippet.get("resourceId") or {}
            video_id = str(
                content_details.get("videoId")
                or (
                    resource_id.get("videoId")
                    if isinstance(resource_id, dict)
                    else ""
                )
                or ""
            ).strip()
            title = str(snippet.get("title") or "").strip()
            raw_published = str(
                content_details.get("videoPublishedAt")
                or snippet.get("publishedAt")
                or ""
            ).strip()

            if not video_id or not title or not raw_published:
                continue
            if title.casefold() in {"private video", "deleted video"}:
                continue

            try:
                published = parse_iso_datetime(raw_published)
            except ValueError:
                continue
            if not is_requested_date(published, requested_dates):
                continue
            if _is_youtube_short_cached(session, video_id):
                continue

            state_key = f"youtube:{channel_id}:{video_id}"
            if state_key in keys_done:
                continue

            keys_done.add(state_key)
            articles.append(
                Article(
                    source=str(channel["source"]),
                    title=title,
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    published=published,
                    state_key=state_key,
                    image_url=(
                        f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
                    ),
                )
            )

    # Se l'unico canale controllato nel ciclo è fallito, segnaliamo l'errore
    # alla gestione standard delle fonti; gli altri scraper continuano.
    if channel_errors and not articles and len(channel_errors) == len(
        selected_channels
    ):
        raise channel_errors[0]

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


def _download_first_x_feed(
    handle: str,
    headers: dict[str, str],
) -> ET.Element | None:
    """Usa i mirror come fallback e si ferma al primo feed RSS valido."""
    for mirror_template in X_RSS_MIRROR_TEMPLATES:
        content = _download_x_feed(
            mirror_template.format(handle=handle),
            headers,
        )
        if content is None:
            continue
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            continue
        if root.find("channel") is not None:
            return root
    return None


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
    """Recupera i post X usando, per account, il primo mirror RSS valido."""
    if not X_ACCOUNTS:
        return []
    articles: list[Article] = []
    keys_done: set[str] = set()
    headers = dict(session.headers)

    with ThreadPoolExecutor(
        max_workers=min(SOURCE_MAX_WORKERS, len(X_ACCOUNTS)),
    ) as executor:
        future_sources = {
            executor.submit(
                _download_first_x_feed,
                account["handle"],
                headers,
            ): account
            for account in X_ACCOUNTS
        }
        for future in as_completed(future_sources):
            account = future_sources[future]
            root = future.result()
            if root is None:
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
                    and X_REPOST_RE.match(title)
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


def _invalid_seen_state() -> RuntimeError:
    return RuntimeError(
        f"Formato non valido in {STATE_FILE.name}; "
        "interrompo per evitare notifiche duplicate."
    )


def _decode_seen_state(
    data: object,
    state_date: date,
) -> tuple[dict[date, list[str]], date]:
    """Legge il formato corrente e i due formati storici dello stato."""
    if isinstance(data, list):
        if not all(isinstance(item, str) for item in data):
            raise _invalid_seen_state()
        return {state_date: list(dict.fromkeys(data))}, state_date

    if not isinstance(data, dict):
        raise _invalid_seen_state()

    # Formato precedente: {"date": "YYYY-MM-DD", "items": [...]}.
    if "date" in data or "items" in data:
        stored_date = data.get("date")
        items = data.get("items")
        if (
            not isinstance(stored_date, str)
            or not isinstance(items, list)
            or not all(isinstance(item, str) for item in items)
        ):
            raise _invalid_seen_state()
        try:
            parsed_date = date.fromisoformat(stored_date)
        except ValueError as error:
            raise _invalid_seen_state() from error
        return {parsed_date: list(dict.fromkeys(items))}, parsed_date

    raw_buckets = data.get("dates")
    raw_coverage_start = data.get("coverage_start")
    if not isinstance(raw_buckets, dict) or not isinstance(
        raw_coverage_start,
        str,
    ):
        raise _invalid_seen_state()
    try:
        coverage_start = date.fromisoformat(raw_coverage_start)
    except ValueError as error:
        raise _invalid_seen_state() from error

    buckets: dict[date, list[str]] = {}
    for raw_date, items in raw_buckets.items():
        if (
            not isinstance(raw_date, str)
            or not isinstance(items, list)
            or not all(isinstance(item, str) for item in items)
        ):
            raise _invalid_seen_state()
        try:
            parsed_date = date.fromisoformat(raw_date)
        except ValueError as error:
            raise _invalid_seen_state() from error
        buckets[parsed_date] = list(dict.fromkeys(items))
    return buckets, coverage_start


def _retained_seen_buckets(
    buckets: dict[date, list[str]],
    state_date: date,
) -> dict[date, list[str]]:
    retained_dates = collection_dates(state_date)
    retained = {
        bucket_date: list(items)
        for bucket_date, items in buckets.items()
        if bucket_date in retained_dates
    }
    retained.setdefault(state_date, [])
    normalized: dict[date, list[str]] = {}
    known: set[str] = set()
    for bucket_date in sorted(retained):
        unique_items: list[str] = []
        for item in retained[bucket_date]:
            if item not in known:
                known.add(item)
                unique_items.append(item)
        normalized[bucket_date] = unique_items[-MAX_SEEN:]
    return normalized


def _normalized_coverage_start(
    coverage_start: date,
    buckets: dict[date, list[str]],
    state_date: date,
) -> date:
    if coverage_start > state_date:
        raise _invalid_seen_state()
    yesterday = state_date - timedelta(days=1)
    if coverage_start <= yesterday and yesterday not in buckets:
        return state_date
    return coverage_start


def _write_seen_buckets(
    buckets: dict[date, list[str]],
    coverage_start: date,
) -> None:
    temporary = STATE_FILE.with_suffix('.json.tmp')
    temporary.write_text(
        json.dumps(
            {
                'coverage_start': coverage_start.isoformat(),
                'dates': {
                    bucket_date.isoformat(): items
                    for bucket_date, items in sorted(buckets.items())
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    os.replace(temporary, STATE_FILE)


def _read_seen_buckets(
    state_date: date,
) -> tuple[dict[date, list[str]], bool, date]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f'Stato non leggibile ({STATE_FILE.name}); '
            'interrompo per evitare notifiche duplicate.'
        ) from error
    is_current_format = (
        isinstance(data, dict)
        and 'coverage_start' in data
        and 'dates' in data
    )
    buckets, coverage_start = _decode_seen_state(data, state_date)
    return buckets, is_current_format, coverage_start


def load_seen_state(state_date: date) -> tuple[list[str], date]:
    if not STATE_FILE.exists():
        return [], state_date
    buckets, is_current_format, coverage_start = _read_seen_buckets(state_date)
    retained = _retained_seen_buckets(buckets, state_date)
    normalized_coverage_start = _normalized_coverage_start(
        coverage_start,
        retained,
        state_date,
    )
    if (
        retained != buckets
        or not is_current_format
        or normalized_coverage_start != coverage_start
    ):
        _write_seen_buckets(retained, normalized_coverage_start)
        print(
            f'[STATO] finestra aggiornata al {state_date.isoformat()}: '
            'deduplica di oggi e ieri conservata.'
        )
    seen = [
        item
        for bucket_date in sorted(retained)
        for item in retained[bucket_date]
    ]
    return seen, normalized_coverage_start


def load_seen(state_date: date) -> list[str]:
    return load_seen_state(state_date)[0]


def save_seen(seen: Iterable[str], state_date: date) -> None:
    if STATE_FILE.exists():
        stored_buckets, _, coverage_start = _read_seen_buckets(state_date)
        buckets = _retained_seen_buckets(stored_buckets, state_date)
        coverage_start = _normalized_coverage_start(
            coverage_start,
            buckets,
            state_date,
        )
    else:
        buckets = {state_date: []}
        coverage_start = state_date
    known = {item for items in buckets.values() for item in items}
    current_items = buckets.setdefault(state_date, [])
    for item in dict.fromkeys(seen):
        if item not in known:
            known.add(item)
            current_items.append(item)
    _write_seen_buckets(
        _retained_seen_buckets(buckets, state_date),
        coverage_start,
    )


def checkpoint_state_to_git() -> bool:
    '''Pubblica subito lo stato quando il bot gira dentro GitHub Actions.'''
    enabled = os.environ.get(STATE_CHECKPOINT_ENV, '').lower() in {'1', 'true', 'yes'}
    if not enabled:
        return False
    target_ref = os.environ.get('GITHUB_REF_NAME', '').strip()
    if not target_ref:
        raise StateCheckpointError('GITHUB_REF_NAME mancante: impossibile salvare lo stato.')
    state_paths = (STATE_FILE.name, PENDING_FILE.name)
    def git(*arguments: str, allowed_codes: tuple[int, ...] = (0,)):
        touch_worker_heartbeat()
        result = subprocess.run(
            ('git', *arguments),
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
        touch_worker_heartbeat()
        if result.returncode not in allowed_codes:
            detail = (result.stderr or result.stdout or 'errore sconosciuto').strip()
            raise StateCheckpointError(f"git {' '.join(arguments)} fallito: {detail}")
        return result

    git('add', '--', *state_paths)
    diff = git('diff', '--cached', '--quiet', '--', *state_paths, allowed_codes=(0, 1))
    if diff.returncode == 0:
        return False
    git('commit', '-m', 'chore: checkpoint stato notizie')
    git('pull', '--rebase', 'origin', target_ref)
    git('push', 'origin', f'HEAD:{target_ref}')
    return True


def article_from_journal(entry: dict) -> Article:
    try:
        return Article(
            source=str(entry['source']),
            title=str(entry['title']),
            url=str(entry['url']),
            published=parse_iso_datetime(str(entry['published'])),
            summary=str(entry.get('summary', '')),
            state_key=str(entry.get('state_key', '')),
            image_url=str(entry.get('image_url', '')),
            image_urls=tuple(entry.get('image_urls') or ()),
            video_url=str(entry.get('video_url', '')),
            video_thumbnail_url=str(entry.get('video_thumbnail_url', '')),
        )
    except (KeyError, ValueError, TypeError) as error:
        raise RuntimeError(f'Notizia non valida in {PENDING_FILE.name}.') from error


def _article_scrapers() -> tuple[tuple[str, Callable], ...]:
    return (
        ('Tuttosport', scrape_tuttosport),
        ('Corriere dello Sport', scrape_corriere),
        ('La Gazzetta dello Sport', scrape_gazzetta),
        ('Sky Sport - Calciomercato', scrape_sky_calciomercato),
        ('Sky Sport - Juventus', scrape_sky_juventus_news),
        ('Juventus.com', scrape_juventus_official),
        ('Gianluca Di Marzio', scrape_gianluca_di_marzio),
        ('Alfredo Pedullà', scrape_alfredo_pedulla),
        ('Borsa Italiana', scrape_borsa_italiana),
        ('YouTube', scrape_youtube_channels),
        ('X', scrape_x_profiles),
    )


def _run_source_scraper(scraper: Callable, headers: dict[str, str], requested_dates: set[date]) -> list[Article]:
    with requests.Session() as source_session:
        source_session.headers.update(headers)
        return scraper(source_session, requested_dates)


def collect_articles(
    session: requests.Session,
    requested_dates: set[date],
    on_article: Callable[[Article], None] | None = None,
) -> tuple[list[Article], list[str]]:
    scrapers = _article_scrapers()
    articles_by_key: dict[str, Article] = {}
    errors: list[str] = []
    headers = dict(session.headers)
    with ThreadPoolExecutor(max_workers=min(SOURCE_MAX_WORKERS, len(scrapers))) as executor:
        future_sources = {
            executor.submit(_run_source_scraper, scraper, headers, requested_dates): source
            for source, scraper in scrapers
        }
        for future in as_completed(future_sources):
            touch_worker_heartbeat()
            source = future_sources[future]
            try:
                source_articles = future.result()
            except (requests.RequestException, ValueError, KeyError, ET.ParseError) as error:
                errors.append(f'{source}: {error}')
                print(f'[FONTE] {source}: errore ({compact_log_text(error, 70)})')
                continue
            for article in sorted(source_articles, key=lambda item: (item.published, item.source, item.title)):
                if not is_collection_candidate(article.published, requested_dates):
                    continue
                if article.notification_key in articles_by_key:
                    continue
                articles_by_key[article.notification_key] = article
                if on_article is not None:
                    on_article(article)
    if len(errors) == len(scrapers):
        raise CollectionError('Nessuna fonte è stata recuperata correttamente.')
    return list(articles_by_key.values()), errors


def deliver_article(
    article: Article,
    session: requests.Session,
    telegram: TelegramClient,
    preview_resolver: PreviewImageResolver,
) -> DeliveryReceipt:
    touch_worker_heartbeat()
    image_urls = preview_resolver.resolve_all(article.url, article.all_image_urls)
    if article.video_url:
        try:
            with prepare_telegram_video(session, article.video_url) as video_file:
                receipt = telegram.send_article(
                    article,
                    video_file_path=str(video_file),
                    video_thumbnail_url=article.video_thumbnail_url,
                    photo_urls=image_urls,
                )
        except VideoPreparationError as error:
            fallback_images = image_urls or ([article.video_thumbnail_url] if article.video_thumbnail_url else [])
            print(f'[MEDIA] video non pronto; uso fallback ({compact_log_text(error, 55)})')
            receipt = telegram.send_article(article, photo_urls=fallback_images)
    else:
        receipt = telegram.send_article(article, photo_urls=image_urls)
    touch_worker_heartbeat()
    if receipt.photo_fallback:
        print(f'[MEDIA] foto/album in fallback: {receipt.mode}')
    if receipt.video_fallback:
        print(f'[MEDIA] video in fallback: {receipt.mode}')
    return receipt


def run(dry_run: bool = False, include_yesterday: bool = False, preview_messages: bool = False) -> int:
    with requests.Session() as session:
        session.headers.update(HEADERS)
        return _run_cycle(
            session,
            dry_run=dry_run,
            include_yesterday=include_yesterday,
            preview_messages=preview_messages,
        )


def _run_cycle(
    session: requests.Session,
    *,
    dry_run: bool = False,
    include_yesterday: bool = False,
    preview_messages: bool = False,
) -> int:
    today = datetime.now(ROME).date()
    if dry_run:
        requested_dates = collection_dates(today)
        articles, _ = collect_articles(session, requested_dates)
        articles.sort(key=lambda item: (item.published, item.source, item.title))
        preview_resolver = PreviewImageResolver(session)
        selected_days = ', '.join(requested_date.isoformat() for requested_date in sorted(requested_dates))
        print(f'[TEST] Totale notizie del {selected_days}: {len(articles)}')
        for article in articles:
            print(f"[TEST] {article.source} | {article.published.strftime('%H:%M')} | {article.title}")
            if preview_messages:
                image_urls = preview_resolver.resolve_all(article.url, article.all_image_urls)
                print('\n--- ANTEPRIMA TELEGRAM ---')
                if article.video_url:
                    print(f'[VIDEO] {article.video_url}')
                if article.video_thumbnail_url:
                    print(f'[COPERTINA VIDEO] {article.video_thumbnail_url}')
                if image_urls:
                    print(f"[FOTO] {len(image_urls)}: {', '.join(image_urls)}")
                else:
                    print('[FOTO] nessuna')
                print(format_article_message(
                    article,
                    max_length=(TELEGRAM_MAX_CAPTION_LENGTH if image_urls or article.video_url else TELEGRAM_MAX_MESSAGE_LENGTH),
                ))
                print('--- FINE ANTEPRIMA ---\n')
        return 0

    token = os.environ.get('TELEGRAM_TOKEN')
    chat_id = os.environ.get('CHAT_ID')
    if not token or not chat_id:
        raise RuntimeError('Secret mancanti: configura TELEGRAM_TOKEN e CHAT_ID.')

    state_was_missing = not STATE_FILE.exists()
    seen_list, coverage_start = load_seen_state(today)
    requested_dates = collection_dates(today, None if include_yesterday else coverage_start)
    seen = set(seen_list)
    journal = ArticleJournal(PENDING_FILE)
    journal.discard_all(seen)

    baseline_if_missing = os.environ.get('BASELINE_IF_NO_STATE', '').lower() in {'1', 'true', 'yes'}
    if baseline_if_missing and state_was_missing:
        def save_baseline_article(article: Article) -> None:
            if article.notification_key not in seen:
                journal.add(article)
        collect_articles(session, requested_dates, on_article=save_baseline_article)
        seen_list = [str(entry['notification_key']) for entry in journal.entries]
        save_seen(seen_list, today)
        journal.clear()
        checkpoint_state_to_git()
        print(f'[STATO] inizializzato senza reinvii: {len(seen_list)} notizie')
        return 0

    telegram = TelegramClient(token, chat_id)
    preview_resolver = PreviewImageResolver(session)
    attempted: set[str] = set()
    sent_count = 0

    def try_delivery(article: Article) -> None:
        nonlocal sent_count
        key = article.notification_key
        if key in seen or key in attempted:
            return
        attempted.add(key)
        try:
            receipt = deliver_article(article, session, telegram, preview_resolver)
        except (TelegramDeliveryError, requests.RequestException, OSError, ValueError) as error:
            print(
                f'[INVIO] rimandato | {article.source} | '
                f'{compact_log_text(article.title, 55)} | '
                f'{compact_log_text(error, 55)}'
            )
            return
        seen.add(key)
        seen_list.append(key)
        save_seen(seen_list, today)
        journal.remove(key)
        checkpoint_state_to_git()
        sent_count += 1
        print(
            f'[PUB] {article.source} | {compact_log_text(article.title, 65)} | '
            f'{receipt.mode} #{receipt.message_id or "?"} | stato salvato'
        )
        time.sleep(0.8)

    pending = [article_from_journal(entry) for entry in journal.entries]
    pending.sort(key=lambda item: (item.published, item.source, item.title))
    for article in pending:
        if not is_collection_candidate(article.published, requested_dates):
            journal.remove(article.notification_key)
            continue
        try_delivery(article)

    def publish_discovered(article: Article) -> None:
        if article.notification_key in seen:
            return
        journal.add(article)
        try_delivery(article)

    collect_articles(session, requested_dates, on_article=publish_discovered)
    checkpoint_state_to_git()
    return sent_count


def run_worker(
    duration_seconds: float = DEFAULT_WORKER_DURATION_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    *,
    dry_run: bool = False,
    include_yesterday: bool = False,
    preview_messages: bool = False,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    if duration_seconds <= 0:
        raise ValueError('La durata del worker deve essere maggiore di zero.')
    if poll_interval_seconds <= 0:
        raise ValueError("L'intervallo del worker deve essere maggiore di zero.")
    monotonic = clock or time.monotonic
    sleeper = sleep or time.sleep
    deadline = monotonic() + duration_seconds
    cycle = 0
    touch_worker_heartbeat()
    print(f'[WORKER] attivo {duration_seconds / 60:.0f} min | pausa {poll_interval_seconds:.0f}s dopo ogni ciclo')
    while monotonic() < deadline:
        cycle += 1
        touch_worker_heartbeat()
        cycle_started = monotonic()
        sent_count = 0
        outcome = 'ok'
        try:
            sent_count = run(
                dry_run=dry_run,
                include_yesterday=include_yesterday,
                preview_messages=preview_messages,
            )
        except CollectionError as error:
            outcome = f'errore: {compact_log_text(error, 70)}'
            checkpoint_state_to_git()
        touch_worker_heartbeat()
        elapsed = monotonic() - cycle_started
        remaining = deadline - monotonic()
        if remaining <= 0:
            print(f'[CICLO {cycle}] nuove={sent_count} | {outcome} | {elapsed:.1f}s')
            break
        wait_seconds = min(poll_interval_seconds, remaining)
        print(f'[CICLO {cycle}] nuove={sent_count} | {outcome} | {elapsed:.1f}s | pausa={wait_seconds:.0f}s')
        sleeper(wait_seconds)
        touch_worker_heartbeat()
    print(f'\n[WORKER] fine | cicli={cycle} | arresto pulito')


def main() -> None:
    parser = argparse.ArgumentParser(description='Invia su Telegram le notizie Juventus pubblicate oggi.')
    parser.add_argument('--dry-run', action='store_true', help='Recupera e mostra le notizie senza usare Telegram.')
    parser.add_argument('--include-yesterday', action='store_true', help='Compatibilità: le notizie di ieri sono ora controllate automaticamente.')
    parser.add_argument('--preview-messages', action='store_true', help='Con --dry-run mostra il testo HTML esatto che verrebbe inviato a Telegram.')
    parser.add_argument('--worker', action='store_true', help='Ripete i controlli fino alla durata configurata.')
    parser.add_argument('--duration-seconds', type=float, default=DEFAULT_WORKER_DURATION_SECONDS, help='Durata totale del worker (predefinita: 3300 secondi).')
    parser.add_argument('--interval-seconds', type=float, default=DEFAULT_POLL_INTERVAL_SECONDS, help='Pausa dopo ogni ciclo (predefinita: 15 secondi).')
    args = parser.parse_args()
    if args.preview_messages and not args.dry_run:
        parser.error('--preview-messages richiede --dry-run')
    if args.worker:
        run_worker(
            duration_seconds=args.duration_seconds,
            poll_interval_seconds=args.interval_seconds,
            dry_run=args.dry_run,
            include_yesterday=args.include_yesterday,
            preview_messages=args.preview_messages,
        )
    else:
        run(
            dry_run=args.dry_run,
            include_yesterday=args.include_yesterday,
            preview_messages=args.preview_messages,
        )


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'Errore: {error}', file=sys.stderr)
        sys.exit(1)
