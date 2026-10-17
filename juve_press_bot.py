
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
# URL canonici: sono quelli dichiarati dal feed stesso (atom:link rel="self").
SKY_JUVENTUS_RSS_URLS = (
    "https://sport.sky.it/rss/sport.calcio.xml",
    "https://sport.sky.it/rss/sport.calcio.serie-a.xml",
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
