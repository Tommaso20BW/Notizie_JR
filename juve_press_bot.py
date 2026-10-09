"""
Juventus Press News Bot

Controlla le notizie Juventus pubblicate OGGI su:
- Tuttosport
- Corriere dello Sport
- La Gazzetta dello Sport
- Sky Sport Calciomercato ("Juve"/"Juventus", esclusi i titoli "video")
- Sky Sport: feed RSS Serie A (solo articoli pertinenti alla Juventus)
- Juventus.com
- Comunicati stampa PDF Juventus.com
- Gianluca Di Marzio (filtro di rilevanza Juventus) e Alfredo Pedullà
- Borsa Italiana (notizie sull'azione Juventus)
- YouTube: Juventus, Fabrizio Romano e Romeo Agresti
- X: profili configurati (filtri e repost definiti per account)

Ogni notizia viene inviata su Telegram una sola volta. Lo stato è salvato nel file
.seen_juve_press_news.json accanto allo script.
"""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from pypdf.errors import PdfReadError

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
YESTERDAY_COLLECTION_START_MINUTE = 23 * 60 + 30
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
SKY_JUVENTUS_RSS_URLS = (
    "https://sport.sky.it/rss/sport_calcio.xml",
    "https://sport.sky.it/rss/sport_calcio_serie-a.xml",
)
# Se un feed risponde e l'altro è lento, dopo questo tempo non si attende più
# il ritardatario: le sue notizie saranno riprese al ciclo successivo.
SKY_JUVENTUS_FEED_GRACE_SECONDS = 5.0
SKY_JUVENTUS_DETAIL_MAX_WORKERS = 6
SKY_JUVENTUS_SEEN_KEYS: set[str] | None = None
# Articoli Sky già valutati in modo definitivo durante questo worker: il feed
# contiene tutta la Serie A, quindi senza questa cache le notizie non Juve
# verrebbero riaperte a ogni ciclo. Come per Di Marzio vive solo in memoria.
SKY_JUVENTUS_CHECKED_URLS: set[str] = set()
JUVENTUS_NEWS_URL = "https://www.juventus.com/it/news/"
JUVENTUS_FEED_TEMPLATE = (
    "https://www.juventus.com/it/news/_libraries/"
    "{date_value}/{date_value}/{page}/_news-list"
)
JUVENTUS_PRESS_RELEASE_LIBRARY_TEMPLATE = (
    "https://www.juventus.com/it/club/investitori/_libraries/"
    "season-{season}/{page}/_price-sensitive-press-releases"
)
JUVENTUS_PRESS_RELEASE_MAX_PAGES = 4
JUVENTUS_PRESS_RELEASE_DATE_CACHE: dict[str, datetime | None] = {}
JUVENTUS_PDF_SIZE_RE = re.compile(
    r"\s+\d+(?:[.,]\d+)?\s*(?:KB|MB|GB)\s*$",
    re.IGNORECASE,
)
JUVENTUS_PDF_NUMERIC_DATE_RE = re.compile(
    r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b"
)
GIANLUCA_DI_MARZIO_URL = "https://www.gianlucadimarzio.com/"
GIANLUCA_DI_MARZIO_LISTING_URLS = (
    GIANLUCA_DI_MARZIO_URL,
    "https://www.gianlucadimarzio.com/calciomercato/",
)
GIANLUCA_DI_MARZIO_ARTICLE_PATH_RE = re.compile(r"-\d{5,}$")
GIANLUCA_DI_MARZIO_CHECKED_URLS: set[str] = set()
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
    {"handle": "David_Ornstein", "filter_juventus": True, "include_reposts": False},
    {"handle": "Plettigoal", "filter_juventus": True, "include_reposts": False},
    {"handle": "SkySportsNews", "filter_juventus": True, "include_reposts": False},
    {"handle": "SkySportDE", "filter_juventus": True, "include_reposts": False},
    {"handle": "Tanziloic", "filter_juventus": True, "include_reposts": False},
    {"handle": "JacobsBen", "filter_juventus": True, "include_reposts": False},
    {"handle": "sachatavolieri", "filter_juventus": True, "include_reposts": False},
)
X_RSS_MIRROR_TEMPLATES = (
    "https://fxtwitter.com/{handle}/feed.xml",
    "https://fixupx.com/{handle}/feed.xml",
)
X_RSS_HEADERS = {
    "User-Agent": "Notizie_JR/1.0 (RSS reader; GitHub Actions)",
    "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8",
}
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
X_JUVENTUS_MENTION_RE = re.compile(r"(?<!\w)@?juventusfc\b", re.IGNORECASE)
DI_MARZIO_JUVENTUS_CONTEXT_RE = re.compile(
    r"\b(?:"
    r"mercato|calciomercato|trattativa|trattative|contatti|contatto|"
    r"offerta|offerte|accordo|accordi|firma|firmare|rinnovo|rinnovato|rinnovi|"
    r"acquisto|acquistare|cessione|cedere|prestito|scambio|"
    r"interesse|interessa|interessato|interessati|obiettivo|obiettivi|"
    r"segue|seguito|seguire|monitora|monitorato|monitorare|"
    r"cerca|cercano|cercando|vuole|vogliono|punta|puntano|"
    r"trattando|tratterà|trattare|proposta|proposte|operazione|operazioni|"
    r"visite mediche|ufficiale|ufficialità|esordio|formazione|"
    r"infortunio|infortuni|squalifica|convocato|convocazione|"
    r"allenatore|allenatori|dirigenza|dirigente|dirigenti|proprietà|"
    r"contratto|contratti|tesserato|tesseramento|rescissione|"
    r"partita|partite|gol|rete|reti|campionato|sfida|sfidare|affronta|"
    r"affrontare|derby|coppa|champions|europa league|serie a|"
    r"al lavoro|in trattativa|pronta|pronto|interessata|interessato"
    r")\b",
    re.IGNORECASE,
)
DI_MARZIO_JUVENTUS_DIRECT_RE = re.compile(
    r"(?:"
    r"\b(?:la\s+)?(?:juventus|juve)\b[^.!?]{0,120}"
    r"\b(?:ha|hanno|sta|stanno|vuole|vogliono|cerca|cercano|"
    r"punta|puntano|tratta|trattano|segue|seguono|monitora|monitorano|"
    r"lavora|lavorano|lavorare|valuta|valutano|offre|offrono|"
    r"propone|propongono|contatta|contattano|incontra|incontrano|"
    r"chiama|chiamano|annuncia|annunciano|acquista|acquistano|"
    r"cede|cedono|rinnova|rinnovano|firma|firmano|chiude|chiudono|"
    r"convoca|convocano|schiera|schierano|affronta|affrontano|"
    r"sfida|sfidano|batte|pareggia|pareggiano|sconfigge|sconfiggono|"
    r"perde|vince|gioca|giocano|giocherà|esordisce|torna|arriva|"
    r"approda|sbarca|si\s+trasferisce|intende|pens[aao]|prepara|"
    r"preparano|provando|provano|può|puo|potrebbe|potrà|potra)\b"
    r"|"
    r"\b(?:piace|interessa|intriga|conviene|serve|manca|"
    r"si\s+avvicina|si\s+allontana|è\s+vicino|e\s+vicino|"
    r"resta\s+vicino|approda|arriva|torna)\b[^.!?]{0,100}"
    r"\b(?:alla\s+)?(?:juventus|juve)\b"
    r"|"
    r"\b(?:nel\s+mirino|obiettivo|obiettivo\s+di|obiettivi\s+di|"
    r"destinazione|direzione|accordo\s+con|contatti\s+con|"
    r"interesse\s+per|interesse\s+della|trattativa\s+con|"
    r"trattativa\s+della|offerta\s+della|proposta\s+della)\b"
    r"[^.!?]{0,100}\b(?:juventus|juve)\b"
    r")",
    re.IGNORECASE,
)
SKY_RECAP_TITLE_RE = re.compile(
    r"^calciomercato,.*\bnews\b.*\boggi\b",
    re.IGNORECASE,
)
SKY_VIDEO_TITLE_RE = re.compile(r"\bvideo\b", re.IGNORECASE)
JUVE_STABIA_RE = re.compile(r"\bjuve(?:\s+|[-_/]+)stabia\b", re.IGNORECASE)
# Squadre Juventus diverse dalla prima squadra maschile (Sky Juventus le esclude).
SKY_JUVENTUS_OTHER_TEAM_RE = re.compile(
    r"\b(?:juventus|juve)\s+"
    r"(?:women|femminile|next\s*gen(?:eration)?|primavera|under\s*\d{2}|u\s?\d{2})\b",
    re.IGNORECASE,
)
# Perifrasi con cui Sky indica la Juventus senza scrivere "Juve/Juventus".
# Vengono considerate solo se l'articolo ha già un'ancora esplicita sulla Juve
# (tag/parole chiave o citazione nel corpo): "bianconeri" vale anche l'Udinese.
SKY_JUVENTUS_ALIAS_RE = re.compile(
    r"\b(?:bianconer[oiae]|vecchia\s+signora)\b",
    re.IGNORECASE,
)
# Un solo accenno nel corpo non basta: servono più citazioni oppure un tag Juve.
SKY_JUVENTUS_BODY_MIN_MENTIONS = 3
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
    """Accetta oggi e, per ieri, soltanto la fascia dalle 23:30 in poi."""
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
    """Ignora 'Juve Stabia' come nome composto e cerca la vera Juve/Juventus."""
    text_without_juve_stabia = JUVE_STABIA_RE.sub(" ", title)
    return bool(JUVE_KEYWORD_RE.search(text_without_juve_stabia))


def is_juventus_x_post(text: str) -> bool:
    """Accetta Juve/Juventus e la menzione dell'account ufficiale."""
    return is_juventus_title(text) or bool(
        X_JUVENTUS_MENTION_RE.search(text)
    )


def x_source_requires_juventus_filter(source: str) -> bool:
    """Riconosce gli account X per i quali va applicato il filtro Juventus."""
    prefix = "X - "
    if not source.startswith(prefix):
        return False
    handle = source[len(prefix):].casefold()
    return any(
        str(account["handle"]).casefold() == handle
        and bool(account["filter_juventus"])
        for account in X_ACCOUNTS
    )


def is_article_allowed(article: Article) -> bool:
    """Ultima barriera prima dell'invio, inclusi gli elementi già nel pending."""
    if x_source_requires_juventus_filter(article.source):
        return is_juventus_x_post(article.title)
    return True


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


def _clean_gazzetta_text(value: object) -> str:
    """Rimuove HTML Gazzetta preservando i tag inline dentro le parole."""
    soup = BeautifulSoup(str(value or ""), "html.parser")

    # Alcuni campi Gazzetta contengono tag inline nel mezzo di una parola,
    # per esempio: "line<span>a</span>". Usare get_text(" ") produrrebbe
    # erroneamente "line a". Gli elementi a blocco mantengono invece uno spazio.
    for tag in soup.find_all(["br", "p", "div", "li"]):
        if tag.name == "br":
            tag.replace_with(" ")
            continue
        tag.insert_before(" ")
        tag.insert_after(" ")

    return " ".join(soup.get_text("", strip=False).split())


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
                title=_clean_gazzetta_text(title),
                url=url,
                published=published,
                summary=_clean_gazzetta_text(item.get("standFirst") or ""),
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
        # Priorità alla data di prima pubblicazione: "updated" (ultima modifica)
        # viene usata solo se il feed non espone nessun'altra data.
        raw_published = (
            _feed_item_text(item, "pubDate")
            or _feed_item_text(item, "published")
            or _feed_item_text(item, "date")
            or _feed_item_text(item, "updated")
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
                url=url,                published=published,
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


def _json_ld_text(value: object, _depth: int = 0) -> str:
    """Normalizza in testo semplice i campi JSON-LD (str, lista, dict, altro).

    keywords, about e articleBody non hanno un formato fisso: possono essere
    stringhe, liste di stringhe, oggetti con "name" o liste di oggetti.
    """
    if value is None or _depth > 4:
        return ""
    if isinstance(value, str):
        return _clean_feed_text(value)
    if isinstance(value, dict):
        parts = (
            _json_ld_text(value.get(key), _depth + 1)
            for key in ("name", "alternateName", "headline", "description")
        )
        return " ".join(part for part in parts if part)
    if isinstance(value, (list, tuple, set)):
        parts = (_json_ld_text(item, _depth + 1) for item in value)
        return " ".join(part for part in parts if part)
    return ""


def _sky_article_body(
    soup: BeautifulSoup,
    article_data: dict,
    max_length: int = 8000,
) -> str:
    """Corpo dell'articolo da JSON-LD, con ripiego sui paragrafi della pagina."""
    body = _json_ld_text(article_data.get("articleBody"))
    if len(body) < 200:
        container = soup.find("article") or soup.find("main")
        if container is not None:
            paragraphs = (
                paragraph.get_text(" ", strip=True)
                for paragraph in container.find_all("p")
            )
            fallback = " ".join(text for text in paragraphs if text)
            if len(fallback) > len(body):
                body = fallback
    return body[:max_length]


def _sky_article_keywords(soup: BeautifulSoup, article_data: dict) -> str:
    """Parole chiave e tag da JSON-LD (keywords, about) e meta tag della pagina."""
    meta_tags = (
        str(tag.get("content") or "").strip()
        for selector in (
            'meta[property="article:tag"]',
            'meta[name="keywords"]',
            'meta[name="news_keywords"]',
        )
        for tag in soup.select(selector)
    )
    parts = (
        _json_ld_text(article_data.get("keywords")),
        _json_ld_text(article_data.get("about")),
        *meta_tags,
    )
    return " ".join(part for part in parts if part)


def _is_relevant_sky_juventus_article(
    title: str,
    summary: str,
    meta_description: str,
    article_body: str,
    keywords: str = "",
) -> bool:
    """Filtro di pertinenza Juventus per il feed Sky Serie A.

    Riutilizza i criteri di ``_is_relevant_di_marzio_juventus_article`` per
    titolo, sommario e meta description, senza modificarli. In più valuta il
    contesto del corpo e dei tag, perché il feed contiene tutta la Serie A:
    una citazione marginale della Juventus non rende l'articolo una notizia Juve.
    """
    title, summary, meta_description, article_body, keywords = (
        SKY_JUVENTUS_OTHER_TEAM_RE.sub(" ", str(text or ""))
        for text in (title, summary, meta_description, article_body, keywords)
    )

    tags_mention_juve = is_juventus_title(keywords)
    body_mentions = len(
        JUVE_KEYWORD_RE.findall(JUVE_STABIA_RE.sub(" ", article_body))
    )
    # "Bianconeri" vale come Juventus solo con un'ancora esplicita sulla Juve.
    if tags_mention_juve or body_mentions >= SKY_JUVENTUS_BODY_MIN_MENTIONS:
        title, summary, meta_description, article_body = (
            SKY_JUVENTUS_ALIAS_RE.sub("Juventus", text)
            for text in (title, summary, meta_description, article_body)
        )
        body_mentions = len(
            JUVE_KEYWORD_RE.findall(JUVE_STABIA_RE.sub(" ", article_body))
        )

    # Titolo (segnale forte), sommario e meta description: stessi criteri Di Marzio.
    if _is_relevant_di_marzio_juventus_article(
        title,
        summary,
        title,
        summary,
        "",
        meta_description,
    ):
        return True

    # Titolo e sommario non bastano: il corpo deve parlare concretamente della
    # Juventus e la citazione deve essere ripetuta oppure confermata dai tag.
    if not _di_marzio_juventus_is_contextually_relevant(article_body):
        return False
    return tags_mention_juve or body_mentions >= SKY_JUVENTUS_BODY_MIN_MENTIONS


def _is_sky_editorial_url(url: str) -> bool:
    """Accetta solo articoli di sport.sky.it, non pagine di categoria o ricerca."""
    parts = urlsplit(url)
    if parts.netloc.lower() != "sport.sky.it":
        return False
    path = parts.path.casefold()
    return not any(
        segment in path
        for segment in ("/argomenti/", "/squadre/", "/tag/", "/search", "/ricerca")
    )


def _sky_juventus_article_from_url(
    session: requests.Session,
    url: str,
    requested_dates: set[date],
) -> Article | None:
    """Apre un singolo articolo Sky e verifica che riguardi davvero la Juve."""
    if not _is_sky_editorial_url(url):
        return None

    try:
        article_response = session.get(url, timeout=15)
        article_response.raise_for_status()
    except requests.RequestException:
        # Errore transitorio: l'URL non entra nella cache e sarà ritentato.
        return None

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
        return None

    # La diretta mercato è già monitorata blocco per blocco dallo scraper
    # dedicato: qui evitiamo di inviare anche l'articolo contenitore.
    if (
        SKY_RECAP_TITLE_RE.search(title)
        or "calciomercato-news-trattative-oggi" in url
        or "calciomercato-news-" in url
    ):
        SKY_JUVENTUS_CHECKED_URLS.add(url)
        return None

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
        return None

    summary = _json_ld_text(
        article_data.get("description") or article_data.get("abstract")
    )
    meta_description = _first_meta_content(
        article_soup,
        (
            'meta[name="description"]',
            'meta[property="og:description"]',
        ),
    )
    if not summary:
        summary = meta_description
    summary = _clean_feed_text(summary)
    meta_description = _clean_feed_text(meta_description)

    article_body = _sky_article_body(article_soup, article_data)
    keywords = _sky_article_keywords(article_soup, article_data)

    if not _is_relevant_sky_juventus_article(
        title,
        summary,
        meta_description,
        article_body,
        keywords,
    ):
        # Scarto definitivo: dipende solo dal contenuto della pagina. Gli
        # articoli accettati non entrano nella cache, così restano recuperabili
        # finché lo stato persistente non li registra come inviati.
        SKY_JUVENTUS_CHECKED_URLS.add(url)
        return None

    image_url = _schema_image_url(article_data.get("image"), url)
    return Article(
        source="Sky Sport - Juventus",
        title=title,
        url=url,
        published=published,
        summary=summary,
        image_url=image_url,
    )


def _sky_today() -> date:
    """Data odierna italiana, ricalcolata a ogni chiamata (cambio giorno incluso)."""
    return datetime.now(ROME).date()


def _sky_url_key(url: str) -> str:
    """Chiave per riconoscere lo stesso articolo anche con maiuscole o slash finale."""
    parts = urlsplit(url)
    return f"{parts.netloc.lower()}{parts.path.rstrip('/').casefold()}"


def _sky_feed_urls(
    session: requests.Session,
    feed_url: str,
    requested_dates: set[date],
) -> list[str]:
    """Scarica un feed RSS Sky e restituisce gli URL pubblicati nelle date richieste."""
    response = session.get(feed_url, timeout=20)
    response.raise_for_status()
    feed_articles = _feed_articles_from_xml(
        response.content,
        source="Sky Sport - Juventus",
        base_url="https://sport.sky.it/",
        allowed_hosts={"sport.sky.it"},
        requested_dates=requested_dates,
        juventus_only=False,
    )
    return [feed_article.url for feed_article in feed_articles]


def scrape_sky_juventus_news(
    session: requests.Session,
    requested_dates: set[date],
) -> list[Article]:
    """Legge in parallelo i feed RSS Sky e tiene le notizie di oggi sulla Juventus.

    Ogni feed passa i propri URL ai worker di dettaglio appena risponde, senza
    attendere l'altro: vale la prima versione valida intercettata e le copie
    successive (stesso URL nell'altro feed) vengono scartate. Un feed in errore
    non blocca quello funzionante; solo se falliscono tutti l'errore risale a
    ``collect_articles``, che lo registra come per le altre fonti.
    """
    # Solo notizie di oggi (Europe/Rome), anche se il feed contiene i giorni scorsi.
    sky_dates = {_sky_today()} & set(requested_dates)
    if not sky_dates:
        return []

    claimed: set[str] = set()
    articles: list[Article] = []
    feed_errors: list[Exception] = []
    feeds_ok = 0

    feed_executor = ThreadPoolExecutor(max_workers=len(SKY_JUVENTUS_RSS_URLS))
    detail_executor = ThreadPoolExecutor(
        max_workers=SKY_JUVENTUS_DETAIL_MAX_WORKERS,
    )
    try:
        feed_futures: dict[Future, str] = {
            feed_executor.submit(
                _sky_feed_urls,
                session,
                feed_url,
                sky_dates,
            ): feed_url
            for feed_url in SKY_JUVENTUS_RSS_URLS
        }
        detail_futures: dict[Future, str] = {}
        pending: set[Future] = set(feed_futures)
        grace_deadline: float | None = None

        while pending:
            timeout = (
                None
                if grace_deadline is None
                else max(0.0, grace_deadline - time.monotonic())
            )
            done, pending = wait(
                pending,
                timeout=timeout,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                # Un feed ha già risposto e l'altro è lento: non lo si attende
                # oltre, riproveremo al prossimo ciclo.
                for future in [f for f in pending if f in feed_futures]:
                    future.cancel()
                    pending.discard(future)
                    print(
                        "[RSS] Sky Sport - Juventus: feed troppo lento, "
                        f"riprovo al prossimo ciclo ({feed_futures[future]})"
                    )
                grace_deadline = None
                continue

            for future in done:
                if future in feed_futures:
                    feed_url = feed_futures[future]
                    try:
                        feed_urls = future.result()
                    except (
                        requests.RequestException,
                        ET.ParseError,
                        ValueError,
                    ) as error:
                        feed_errors.append(error)
                        print(
                            f"[RSS] Sky Sport - Juventus: feed {feed_url} "
                            f"errore ({compact_log_text(error, 70)})"
                        )
                        continue

                    feeds_ok += 1
                    for url in feed_urls:
                        # Stesso articolo già preso in carico dall'altro feed.
                        key = _sky_url_key(url)
                        if key in claimed:
                            continue
                        claimed.add(key)

                        # Nel worker reale lo stato persistente contiene gli URL
                        # già inviati. In --dry-run il valore è None e si
                        # analizzano tutti i candidati, come prima.
                        if (
                            SKY_JUVENTUS_SEEN_KEYS is not None
                            and url in SKY_JUVENTUS_SEEN_KEYS
                        ):
                            continue
                        # Già valutato e scartato in questo worker.
                        if url in SKY_JUVENTUS_CHECKED_URLS:
                            continue

                        detail_future = detail_executor.submit(
                            _sky_juventus_article_from_url,
                            session,
                            url,
                            sky_dates,
                        )
                        detail_futures[detail_future] = url
                        pending.add(detail_future)
                    continue

                try:
                    article = future.result()
                except (requests.RequestException, ValueError, KeyError) as error:
                    # Una pagina difettosa non deve far perdere le altre notizie.
                    print(
                        f"[SKY] {detail_futures[future]}: errore "
                        f"({compact_log_text(error, 70)})"
                    )
                    continue
                if article is not None:
                    articles.append(article)

            if (
                grace_deadline is None
                and feeds_ok
                and any(f in feed_futures for f in pending)
            ):
                grace_deadline = (
                    time.monotonic() + SKY_JUVENTUS_FEED_GRACE_SECONDS
                )
    finally:
        # Un feed bloccato non deve trattenere il ciclo oltre il suo timeout.
        feed_executor.shutdown(wait=False, cancel_futures=True)
        detail_executor.shutdown(wait=True)

    if not feeds_ok and feed_errors:
        raise feed_errors[0]
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




def juventus_press_release_season(today: date) -> str:
    """Slug della stagione usato dalla libreria Investitori (es. 2026-27)."""
    start_year = today.year if today.month >= 7 else today.year - 1
    return f"{start_year}-{str(start_year + 1)[-2:]}"


def juventus_press_release_library_url(today: date, page: int = 1) -> str:
    return JUVENTUS_PRESS_RELEASE_LIBRARY_TEMPLATE.format(
        season=juventus_press_release_season(today),
        page=page,
    )


def _juventus_press_release_title(link) -> str:
    title = " ".join(link.get_text(" ", strip=True).split())
    title = JUVENTUS_PDF_SIZE_RE.sub("", title).strip(" -–|:")
    return title or "Comunicato ufficiale Juventus"


def _juventus_press_release_date_from_text(text: str) -> datetime | None:
    """Riconosce sia '21 agosto 2026' sia '21/08/2026'."""
    published = _parse_italian_calendar_date(text)
    if published is not None:
        return published
    match = JUVENTUS_PDF_NUMERIC_DATE_RE.search(text)
    if not match:
        return None
    try:
        return datetime(
            int(match.group(3)),
            int(match.group(2)),
            int(match.group(1)),
            tzinfo=ROME,
        )
    except ValueError:
        return None


def _juventus_press_release_date_from_pdf(
    session: requests.Session,
    pdf_url: str,
) -> datetime | None:
    """Legge la data del comunicato dal PDF e la memorizza per il worker."""
    if pdf_url in JUVENTUS_PRESS_RELEASE_DATE_CACHE:
        return JUVENTUS_PRESS_RELEASE_DATE_CACHE[pdf_url]

    response = session.get(pdf_url, timeout=30)
    response.raise_for_status()
    content_type = str(response.headers.get("Content-Type") or "").casefold()
    if not response.content.startswith(b"%PDF") and "pdf" not in content_type:
        raise ValueError("Il collegamento Juventus non restituisce un PDF.")

    try:
        reader = PdfReader(io.BytesIO(response.content))
    except PdfReadError:
        raise

    text_parts: list[str] = []
    for page in reader.pages[:2]:
        try:
            text_parts.append(page.extract_text() or "")
        except Exception:
            continue

    published = _juventus_press_release_date_from_text("\n".join(text_parts))
    JUVENTUS_PRESS_RELEASE_DATE_CACHE[pdf_url] = published
    return published


def scrape_juventus_press_releases(
    session: requests.Session,
    requested_dates: set[date],
) -> list[Article]:
    """Recupera esclusivamente i comunicati PDF datati oggi in Europe/Rome."""
    if not requested_dates:
        return []

    today = max(requested_dates)
    articles: list[Article] = []
    urls_done: set[str] = set()

    for page_number in range(1, JUVENTUS_PRESS_RELEASE_MAX_PAGES + 1):
        page_url = juventus_press_release_library_url(today, page_number)
        response = session.get(page_url, timeout=30)
        if response.status_code == 404:
            break
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        page_candidates: list[tuple[object, str]] = []
        for link in soup.select("a[href]"):
            raw_href = str(link.get("href") or "").strip()
            if not raw_href:
                continue

            pdf_url = normalize_url(urljoin(page_url, raw_href))
            parts = urlsplit(pdf_url)
            if parts.netloc.lower() not in {"www.juventus.com", "juventus.com"}:
                continue
            path_lower = parts.path.casefold()
            if not (
                path_lower.endswith(".pdf")
                or "/images/image/private/fl_attachment/" in path_lower
                or "/images/image/upload/fl_attachment/" in path_lower
            ):
                continue
            if pdf_url in urls_done:
                continue

            urls_done.add(pdf_url)
            page_candidates.append((link, pdf_url))

        if not page_candidates:
            break

        page_dates: list[date] = []
        for link, pdf_url in page_candidates:
            try:
                published = _juventus_press_release_date_from_pdf(
                    session,
                    pdf_url,
                )
            except (requests.RequestException, PdfReadError, ValueError) as error:
                print(
                    f"[PDF JUVE] non leggibile | "
                    f"{compact_log_text(pdf_url, 55)} | "
                    f"{compact_log_text(error, 55)}"
                )
                continue

            if published is None:
                continue

            local_date = published.astimezone(ROME).date()
            page_dates.append(local_date)

            # Vincolo specifico richiesto: mai inviare PDF di ieri o più vecchi.
            if local_date != today:
                continue

            articles.append(
                Article(
                    source="Juventus.com - Comunicati PDF",
                    title=_juventus_press_release_title(link),
                    url=pdf_url,
                    published=published,
                    state_key=f"juventus-pdf:{pdf_url}",
                )
            )

        # La libreria è ordinata dal più recente al più vecchio.
        if page_dates and max(page_dates) < today:
            break

    return articles


def _gianluca_di_marzio_listing_candidates(
    session: requests.Session,
    page_url: str,
) -> dict[str, tuple[str, str]]:
    """Estrae gli articoli reali da una pagina elenco di Di Marzio."""
    response = session.get(page_url, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    candidates: dict[str, tuple[str, str]] = {}
    for link in soup.select("a[href]"):
        raw_url = str(link.get("href") or "").strip()
        if not raw_url:
            continue

        url = normalize_url(urljoin(page_url, raw_url))
        parts = urlsplit(url)
        if parts.netloc.lower() != "www.gianlucadimarzio.com":
            continue
        if not GIANLUCA_DI_MARZIO_ARTICLE_PATH_RE.search(parts.path):
            continue

        preview_text = link.get_text(" ", strip=True)
        title_tag = link.select_one(".title")
        title = (
            title_tag.get_text(" ", strip=True)
            if title_tag
            else preview_text
        )
        candidates.setdefault(url, (title, preview_text))

    return candidates


def _di_marzio_juventus_is_contextually_relevant(text: str) -> bool:
    """Accetta Juve/Juventus solo quando la citazione è realmente contestuale."""
    cleaned = JUVE_STABIA_RE.sub(" ", str(text or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned or not JUVE_KEYWORD_RE.search(cleaned):
        return False

    # Le citazioni parentetiche (es. "Alajbegovic (Juventus)") sono tipicamente
    # appartenenza di squadra dentro una lista e non rendono l'articolo una news Juve.
    cleaned = re.sub(
        r"\([^)]*\b(?:juventus|juve)\b[^)]*\)",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    # Anche riferimenti puramente biografici non sono sufficienti.
    cleaned = re.sub(
        r"\b(?:ex|l'\s*ex)\s+(?:giocatore|calciatore|allenatore)?\s*(?:della|di|con)?\s*(?:juventus|juve)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(?:ha|hanno)\s+giocat[oa]\s+(?:per|con|nella|nella squadra della|alla|in)\s+(?:la\s+)?(?:juventus|juve)\b",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )

    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+|\n+", cleaned)
        if part.strip()
    ]
    for sentence in sentences:
        if not JUVE_KEYWORD_RE.search(sentence):
            continue
        if DI_MARZIO_JUVENTUS_DIRECT_RE.search(sentence):
            return True
        if DI_MARZIO_JUVENTUS_CONTEXT_RE.search(sentence):
            return True
    return False


def _is_relevant_di_marzio_juventus_article(
    listing_title: str,
    preview_text: str,
    article_title: str,
    summary: str,
    article_body: str,
    meta_description: str,
) -> bool:
    """Filtro specifico Di Marzio per evitare citazioni Juventus incidentali."""
    # Titolo della card o titolo dell'articolo: segnale più forte.
    if is_juventus_title(listing_title) or is_juventus_title(article_title):
        return True

    # Preview, summary e description vengono considerati solo se la citazione
    # è accompagnata da una relazione concreta con la Juventus.
    for text in (preview_text, summary, meta_description):
        if _di_marzio_juventus_is_contextually_relevant(text):
            return True

    # Nel corpo non basta più trovare "Juventus" una sola volta.
    return _di_marzio_juventus_is_contextually_relevant(article_body)


def scrape_gianluca_di_marzio(
    session: requests.Session,
    requested_dates: set[date],
) -> list[Article]:
    """Recupera da home e Calciomercato le notizie che citano "Juventus"."""
    candidates: dict[str, tuple[str, str]] = {}
    listing_errors: list[requests.RequestException] = []

    # La home non contiene sempre ogni articolo appena pubblicato.
    # Leggiamo anche la sezione Calciomercato e deduplichiamo gli URL.
    for page_url in GIANLUCA_DI_MARZIO_LISTING_URLS:
        try:
            page_candidates = _gianluca_di_marzio_listing_candidates(
                session,
                page_url,
            )
        except requests.RequestException as error:
            listing_errors.append(error)
            continue
        for url, card_data in page_candidates.items():
            candidates.setdefault(url, card_data)

    if (
        not candidates
        and len(listing_errors) == len(GIANLUCA_DI_MARZIO_LISTING_URLS)
    ):
        raise listing_errors[0]

    articles: list[Article] = []
    for url, (listing_title, preview_text) in candidates.items():
        # Durante il worker lo stesso articolo non va riaperto ogni 15 secondi.
        if url in GIANLUCA_DI_MARZIO_CHECKED_URLS:
            continue

        try:
            article_response = session.get(url, timeout=30)
            article_response.raise_for_status()
        except requests.RequestException:
            # Un errore transitorio non entra nella cache: sarà ritentato.
            continue

        article_soup = BeautifulSoup(article_response.text, "html.parser")
        article_title, published, summary, image_url = _generic_article_metadata(
            article_soup,
            url,
        )

        # Se i metadati non sono ancora completi, ritentiamo nei cicli successivi.
        if published is None or not article_title:
            continue

        GIANLUCA_DI_MARZIO_CHECKED_URLS.add(url)
        if not is_requested_date(published, requested_dates):
            continue

        article_data = _sky_structured_article(article_soup)
        article_body = _clean_feed_text(
            str(article_data.get("articleBody") or "")
        )
        meta_description = _first_meta_content(
            article_soup,
            ('meta[name="description"]', 'meta[property="og:description"]'),
        )

        # Una citazione isolata di "Juventus" nel corpo non è sufficiente.
        # Titolo/card sono il segnale forte; gli altri campi devono mostrare
        # un rapporto concreto con la Juventus.
        if not _is_relevant_di_marzio_juventus_article(
            listing_title,
            preview_text,
            article_title,
            summary,
            article_body,
            meta_description,
        ):
            continue

        articles.append(
            Article(
                source="Gianluca Di Marzio",
                title=article_title,
                url=url,
                published=published,
                summary=summary,
                image_url=image_url,
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


def _x_tweet_id(*candidates: str) -> str:
    """Estrae l'ID numerico sia dai GUID Nitter sia dai permalink RSS."""
    for candidate in candidates:
        candidate = candidate.strip()
        if candidate.isdigit():
            return candidate
        link_match = X_STATUS_PATH_RE.match(urlsplit(candidate).path)
        if link_match:
            return link_match.group(2)
    return ""


def _x_rss_has_status_item(root: ET.Element) -> bool:
    """Scarta HTML, feed vuoti e avvisi RSS mascherati da normali feed."""
    channel = root.find("channel")
    if channel is None:
        return False
    return any(
        _x_tweet_id(
            item.findtext("guid", default=""),
            item.findtext("link", default=""),
        )
        for item in channel.findall("item")
    )


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
        if _x_rss_has_status_item(root):
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
    """Riconosce i video nativi nei feed FxTwitter e Nitter."""
    if any(
        child.tag.rsplit("}", 1)[-1].lower() == "enclosure"
        and child.attrib.get("type", "").lower().startswith("video/")
        for child in item.iter()
    ):
        return True
    description = item.findtext("description", default="")
    return bool(
        re.search(
            r"<brYs*/?>Ys*Video\s*<br\s*/?>",
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
        # animazioni  in loop: vengono escluse esplicitamente.
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
    headers = {**session.headers, **X_RSS_HEADERS}

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
                raw_guid = item.findtext("guid", default="").strip()
                raw_link = item.findtext("link", default="").strip()
                tweet_id = _x_tweet_id(raw_guid, raw_link)
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
    """Pubblica subito lo stato quando il bot gira dentro GitHub Actions."""
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
        ('Juventus.com - Comunicati PDF', scrape_juventus_press_releases),
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

    if article.source == "Juventus.com - Comunicati PDF":
        receipt = telegram.send_article(article, document_url=article.url)
        touch_worker_heartbeat()
        return receipt

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
    global SKY_JUVENTUS_SEEN_KEYS

    today = datetime.now(ROME).date()
    if dry_run:
        SKY_JUVENTUS_SEEN_KEYS = None
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
                if article.source == "Juventus.com - Comunicati PDF":
                    print(f'[PDF] {article.url}')
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
                    max_length=(
                        TELEGRAM_MAX_CAPTION_LENGTH
                        if (
                            article.source == "Juventus.com - Comunicati PDF"
                            or image_urls
                            or article.video_url
                        )
                        else TELEGRAM_MAX_MESSAGE_LENGTH
                    ),
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
    SKY_JUVENTUS_SEEN_KEYS = seen
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
        if not is_article_allowed(article):
            journal.remove(key)
            print(
                f'[FILTRO] scartato | {article.source} | '
                f'{compact_log_text(article.title, 65)}'
            )
            return
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
