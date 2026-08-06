"""Recupero dei post Instagram pubblicati dal profilo ufficiale Juventus."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from itertools import islice
from zoneinfo import ZoneInfo

try:
    import instaloader
except ImportError:  # pragma: no cover - gestito con un errore esplicito a runtime.
    instaloader = None


ROME = ZoneInfo("Europe/Rome")
INSTAGRAM_PROFILE = "juventus"
INSTAGRAM_MAX_POSTS_TO_CHECK = 30
INSTAGRAM_OLD_POSTS_BEFORE_STOP = 4


class InstagramSourceError(RuntimeError):
    """Instagram non può essere letto in modo affidabile in questo run."""


@dataclass(frozen=True)
class InstagramPost:
    shortcode: str
    url: str
    published: datetime
    caption: str
    media_items: tuple[tuple[str, str, str], ...]


def _normalise_text(value: str) -> str:
    return " ".join((value or "").split())


def _instagram_loader():
    """Crea un client Instaloader anonimo per il profilo pubblico Juventus."""
    if instaloader is None:
        raise InstagramSourceError(
            "Dipendenza instaloader assente: installa requirements-juve-press.txt."
        )

    try:
        return instaloader.Instaloader(
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            quiet=True,
        )
    except Exception as error:
        raise InstagramSourceError(
            f"Client Instagram anonimo non inizializzabile: {error}"
        ) from error


def _post_permalink(post, *, is_reel: bool = False) -> str:
    product_type = str(getattr(post, "product_type", "") or "").lower()
    path = "reel" if is_reel or product_type == "clips" else "p"
    return f"https://www.instagram.com/{path}/{post.shortcode}/"


def _post_datetime(post) -> datetime:
    published = post.date_utc
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return published.astimezone(ROME)


def _post_media(post) -> tuple[tuple[str, str, str], ...]:
    """Restituisce (tipo, URL, copertina) nell'ordine originale del post."""
    media: list[tuple[str, str, str]] = []

    if post.typename == "GraphSidecar":
        for node in post.get_sidecar_nodes():
            display_url = str(node.display_url or "").strip()
            if node.is_video:
                video_url = str(node.video_url or "").strip()
                if video_url:
                    media.append(("video", video_url, display_url))
                elif display_url:
                    media.append(("photo", display_url, ""))
            elif display_url:
                media.append(("photo", display_url, ""))
    elif post.is_video:
        video_url = str(post.video_url or "").strip()
        thumbnail_url = str(post.url or "").strip()
        if video_url:
            media.append(("video", video_url, thumbnail_url))
        elif thumbnail_url:
            media.append(("photo", thumbnail_url, ""))
    else:
        image_url = str(post.url or "").strip()
        if image_url:
            media.append(("photo", image_url, ""))

    # Un post Instagram può contenere più elementi. Telegram li dividerà in
    # album da massimo 10 senza separare media appartenenti a post diversi.
    return tuple(media[:20])


def _posts_for_dates(
    iterator,
    requested_dates: set[date],
    *,
    is_reel: bool,
) -> list[InstagramPost]:
    oldest_requested = min(requested_dates)
    old_non_pinned = 0
    results: list[InstagramPost] = []

    for post in islice(iterator, INSTAGRAM_MAX_POSTS_TO_CHECK):
        published = _post_datetime(post)
        post_date = published.date()
        is_pinned = bool(getattr(post, "is_pinned", False))

        if post_date in requested_dates:
            media_items = _post_media(post)
            if not media_items:
                continue
            results.append(
                InstagramPost(
                    shortcode=str(post.shortcode),
                    url=_post_permalink(post, is_reel=is_reel),
                    published=published,
                    caption=_normalise_text(post.caption or ""),
                    media_items=media_items,
                )
            )
            old_non_pinned = 0
            continue

        if post_date < oldest_requested and not is_pinned:
            old_non_pinned += 1
            if old_non_pinned >= INSTAGRAM_OLD_POSTS_BEFORE_STOP:
                break

    return results


def fetch_instagram_posts(requested_dates: set[date]) -> list[InstagramPost]:
    if not requested_dates:
        return []

    loader = _instagram_loader()
    try:
        profile = instaloader.Profile.from_username(
            loader.context,
            INSTAGRAM_PROFILE,
        )
    except Exception as error:
        raise InstagramSourceError(
            f"Profilo Instagram @{INSTAGRAM_PROFILE} non leggibile: {error}"
        ) from error

    regular_posts: list[InstagramPost] = []
    reels: list[InstagramPost] = []
    source_errors: list[str] = []

    try:
        regular_posts = _posts_for_dates(
            profile.get_posts(),
            requested_dates,
            is_reel=False,
        )
    except Exception as error:
        source_errors.append(f"post: {error}")

    get_reels = getattr(profile, "get_reels", None)
    if callable(get_reels):
        try:
            reels = _posts_for_dates(
                get_reels(),
                requested_dates,
                is_reel=True,
            )
        except Exception as error:
            source_errors.append(f"Reel: {error}")

    if source_errors and not regular_posts and not reels:
        raise InstagramSourceError(
            f"Profilo Instagram @{INSTAGRAM_PROFILE} non leggibile: "
            + "; ".join(source_errors)
        )
    for error in source_errors:
        print(f"[Instagram] recupero parziale ({error}).")

    # Un Reel può comparire anche nella griglia dei post. In quel caso la
    # versione proveniente dall'iteratore Reel sostituisce quella regolare,
    # così il collegamento punta a /reel/ e il contenuto viene inviato una volta.
    unique: dict[str, InstagramPost] = {}
    for post in regular_posts:
        unique[post.shortcode] = post
    for post in reels:
        unique[post.shortcode] = post
    return sorted(
        unique.values(),
        key=lambda post: post.published,
        reverse=True,
    )
