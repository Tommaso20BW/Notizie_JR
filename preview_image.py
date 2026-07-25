"""Ricerca dell'immagine di anteprima associata a un contenuto web."""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

PREVIEW_META_SELECTORS = (
    'meta[property="og:image:secure_url"]',
    'meta[property="og:image"]',
    'meta[name="twitter:image"]',
    'meta[property="twitter:image"]',
    'link[rel="image_src"]',
)


def normalize_image_url(image_url: str, page_url: str = "") -> str:
    image_url = (image_url or "").strip()
    if not image_url:
        return ""
    resolved = urljoin(page_url, image_url)
    parts = urlsplit(resolved)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    return resolved


class PreviewImageResolver:
    def __init__(self, session: requests.Session) -> None:
        self.session = session
        self.cache: dict[str, str] = {}

    def resolve(self, page_url: str, direct_image_url: str = "") -> str:
        direct = normalize_image_url(direct_image_url, page_url)
        if direct:
            return direct
        if page_url in self.cache:
            return self.cache[page_url]

        host = urlsplit(page_url).netloc.lower()
        if host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            self.cache[page_url] = ""
            return ""

        image_url = ""
        try:
            response = self.session.get(page_url, timeout=20)
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type.startswith("image/"):
                image_url = normalize_image_url(response.url)
            else:
                soup = BeautifulSoup(response.text, "html.parser")
                for selector in PREVIEW_META_SELECTORS:
                    tag = soup.select_one(selector)
                    if not tag:
                        continue
                    candidate = tag.get("content") or tag.get("href") or ""
                    image_url = normalize_image_url(candidate, response.url)
                    if image_url:
                        break
        except requests.RequestException:
            image_url = ""

        self.cache[page_url] = image_url
        return image_url
