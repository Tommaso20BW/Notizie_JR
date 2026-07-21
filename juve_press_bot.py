"""
Juventus Press News Bot

Controlla le notizie Juventus pubblicate OGGI su:
- Tuttosport
- Corriere dello Sport
- La Gazzetta dello Sport
- Sky Sport Calciomercato (solo aggiornamenti con "Juve" o "Juventus")
- Juventus.com

Ogni notizia viene inviata su Telegram una sola volta. Lo stato è salvato nel file
.seen_juve_press_news.json accanto allo script.
"""

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from html import escape
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


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
SKY_RECAP_TITLE_RE = re.compile(
    r"^calciomercato,.*\bnews\b.*\boggi\b",
    re.IGNORECASE,
)


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


def is_today(published: datetime, today: date) -> bool:
    return published.astimezone(ROME).date() == today


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
    today: date,
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

        if published is None or not is_today(published, today):
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
    today: date,
) -> list[Article]:
    return scrape_html_source(
        session=session,
        source="Tuttosport",
        page_url=TUTTOSPORT_URL,
        expected_host="www.tuttosport.com",
        today=today,
    )


def scrape_corriere(
    session: requests.Session,
    today: date,
) -> list[Article]:
    return scrape_html_source(
        session=session,
        source="Corriere dello Sport",
        page_url=CORRIERE_URL,
        expected_host="www.corrieredellosport.it",
        today=today,
    )


def scrape_gazzetta(
    session: requests.Session,
    today: date,
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
        if not is_today(published, today):
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


def scrape_sky_calciomercato(
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
        if SKY_RECAP_TITLE_RE.search(title):
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
        if not JUVE_KEYWORD_RE.search(f"{title} {summary}"):
            continue

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


def juventus_feed_url(today: date, page: int = 1) -> str:
    return JUVENTUS_FEED_TEMPLATE.format(
        date_value=today.isoformat(),
        page=page,
    )


def scrape_juventus_official(
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


class TelegramClient:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id

    def send_article(self, article: Article) -> None:
        summary = ""
        if article.summary:
            summary = f"\n\n{escape(article.summary)}"

        text = (
            f"📰 <b>{escape(article.source)}</b>\n\n"
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


def collect_today_articles(
    session: requests.Session,
    today: date,
) -> tuple[list[Article], list[str]]:
    scrapers = (
        ("Tuttosport", scrape_tuttosport),
        ("Corriere dello Sport", scrape_corriere),
        ("La Gazzetta dello Sport", scrape_gazzetta),
        ("Sky Sport - Calciomercato", scrape_sky_calciomercato),
        ("Juventus.com", scrape_juventus_official),
    )
    articles_by_key: dict[str, Article] = {}
    errors: list[str] = []

    for source, scraper in scrapers:
        try:
            source_articles = scraper(session, today)
        except (requests.RequestException, ValueError, KeyError) as error:
            errors.append(f"{source}: {error}")
            print(f"[{source}] errore durante il recupero: {error}")
            continue

        print(f"[{source}] notizie di oggi trovate: {len(source_articles)}")
        for article in source_articles:
            articles_by_key.setdefault(article.notification_key, article)

    if len(errors) == len(scrapers):
        raise RuntimeError("Nessuna fonte è stata recuperata correttamente.")

    return list(articles_by_key.values()), errors


def run(dry_run: bool = False) -> None:
    today = datetime.now(ROME).date()
    session = requests.Session()
    session.headers.update(HEADERS)

    articles, _ = collect_today_articles(session, today)

    # I siti mostrano prima le notizie più recenti. Telegram le riceve invece
    # dalla più vecchia alla più nuova, per mantenere l'ordine cronologico.
    articles.sort(key=lambda item: (item.published, item.source, item.title))

    if dry_run:
        print(f"[TEST] Totale notizie del {today.isoformat()}: {len(articles)}")
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
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Errore: {error}", file=sys.stderr)
        sys.exit(1)
