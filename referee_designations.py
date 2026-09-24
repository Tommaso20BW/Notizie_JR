#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import unicodedata
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo
    ROME = ZoneInfo("Europe/Rome")
except Exception:
    ROME = None

STATE_FILE = Path(".seen_juve_referee_designations.json")
AIA_URL = "https://www.aia-figc.it/news/?c=9"
UEFA_COMPETITIONS = {
    "Champions League": "https://it.uefa.com/uefachampionsleague/clubs/50139/matches/",
    "Europa League": "https://it.uefa.com/uefaeuropaleague/clubs/50139--juventus/matches/",
    "Conference League": "https://it.uefa.com/uefaeuropaconferenceleague/clubs/50139--juventus/matches/",
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}
FLAGS = {
    "ALB":"🇦🇱","AUT":"🇦🇹","BEL":"🇧🇪","BIH":"🇧🇦","BLR":"🇧🇾","BUL":"🇧🇬",
    "CRO":"🇭🇷","CYP":"🇨🇾","CZE":"🇨🇿","DEN":"🇩🇰","ENG":"🏴","ESP":"🇪🇸",
    "FIN":"🇫🇮","FRA":"🇫🇷","GEO":"🇬🇪","GER":"🇩🇪","GRE":"🇬🇷","HUN":"🇭🇺",
    "IRL":"🇮🇪","ISL":"🇮🇸","ISR":"🇮🇱","ITA":"🇮🇹","KAZ":"🇰🇿","KOS":"🇽🇰",
    "LTU":"🇱🇹","LUX":"🇱🇺","LVA":"🇱🇻","MDA":"🇲🇩","MKD":"🇲🇰","MNE":"🇲🇪",
    "NED":"🇳🇱","NOR":"🇳🇴","POL":"🇵🇱","POR":"🇵🇹","ROU":"🇷🇴","RUS":"🇷🇺",
    "SCO":"🏴","SRB":"🇷🇸","SVK":"🇸🇰","SVN":"🇸🇮","SWE":"🇸🇪","SUI":"🇨🇭",
    "TUR":"🇹🇷","UKR":"🇺🇦","WAL":"🏴","BRA":"🇧🇷","ARG":"🇦🇷","COL":"🇨🇴",
    "URU":"🇺🇾","CAN":"🇨🇦","USA":"🇺🇸",
}


def now():
    return datetime.now(ROME) if ROME else datetime.now()


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def surname(s: str) -> str:
    parts = clean(s).split()
    return parts[-1] if parts else ""


def hashtag(opponent: str) -> str:
    replacements = {
        "N.E.C.": "NEC", "N.E.C": "NEC", "H. Verona": "Verona",
        "Hellas Verona": "Verona", "H. Beer-Sheva": "BeerSheva",
        "AZ Alkmaar": "AZAlkmaar", "Maccabi Tel Aviv": "MaccabiTelAviv",
        "Red Bull Salzburg": "Salzburg",
    }
    opponent = replacements.get(clean(opponent), clean(opponent))
    opponent = unicodedata.normalize("NFKD", opponent)
    opponent = "".join(c for c in opponent if not unicodedata.combining(c))
    words = re.findall(r"[A-Za-z0-9]+", opponent)
    return "#Juve" + "".join(w[:1].upper() + w[1:] for w in words)


def load_state():
    if not STATE_FILE.exists():
        return {"aia": {}, "uefa": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        data.setdefault("aia", {})
        data.setdefault("uefa", {})
        return data
    except Exception:
        return {"aia": {}, "uefa": {}}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def get(session, url):
    try:
        r = session.get(url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        print(f"[ARBITRI] richiesta fallita: {url} -> {e}")
        return None


def send(text):
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat = os.getenv("CHAT_ID", "").strip()
    if not token or not chat:
        print("[ARBITRI] TELEGRAM_TOKEN/CHAT_ID mancanti")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": text, "disable_web_page_preview": "true"},
            timeout=25,
        )
        r.raise_for_status()
        return bool(r.json().get("ok"))
    except requests.RequestException as e:
        print(f"[ARBITRI] Telegram: {e}")
        return False


def sig(roles, flags=None):
    raw = json.dumps({"roles": roles, "flags": flags or {}}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def format_message(prefix, tag, roles, flags=None):
    flags = flags or {}
    def v(k):
        return roles[k] + (f" {flags[k]}" if flags.get(k) else "")
    return (
        f"{prefix}ℹ️ Designazione arbitrale di {tag}:\n\n"
        f"ARBITRO: {v('ARBITRO')}\n"
        f"ASSISTENTI: {v('ASSISTENTI')}\n"
        f"IV: {v('IV')}\n"
        f"VAR: {v('VAR')}\n"
        f"AVAR: {v('AVAR')}"
    )


def article_date(soup):
    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").lower()
        value = meta.get("content", "")
        if key in {"article:published_time", "date", "pubdate", "publishdate"}:
            m = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", value)
            if m:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    text = soup.get_text(" ", strip=True)
    for pattern in [r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b", r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b"]:
        m = re.search(pattern, text)
        if m:
            try:
                if len(m.group(1)) == 4:
                    return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                pass
    return None


def aia_parse(soup):
    lines = [clean(x) for x in soup.get_text("\n", strip=True).splitlines() if clean(x)]
    idx = next((i for i, x in enumerate(lines) if re.search(r"\bJUVENTUS\b", x, re.I)), None)
    if idx is None:
        return None
    block = lines[max(0, idx-5):idx+18]
    match_line = next((x for x in block if "JUVENTUS" in x.upper() and re.search(r"[-–—]", x)), "")
    if not match_line:
        return None
    parts = re.split(r"\s*[-–—]\s*", match_line, maxsplit=1)
    if len(parts) != 2 or "JUVENTUS" not in match_line.upper():
        return None
    home, away = clean(parts[0]), clean(parts[1])
    opponent = away if "JUVENTUS" in home.upper() else home
    roles = {}
    for line in block:
        u = line.upper()
        for label in ("ARBITRO", "ASSISTENTI", "IV", "VAR", "AVAR"):
            if u.startswith(label):
                roles[label] = clean(re.sub(rf"^{label}\s*:?\s*", "", line, flags=re.I))
    if not all(roles.get(x) for x in ("ARBITRO", "ASSISTENTI", "IV", "VAR", "AVAR")):
        return None
    return opponent, roles


def aia_scan(session, state):
    html = get(session, AIA_URL)
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        url = urljoin(AIA_URL, a["href"])
        if "aia-figc.it/news/" in url and url not in seen:
            seen.add(url)
            links.append(url)
    changed = False
    today = now().date()
    for url in links[:80]:
        if url in state["aia"]:
            continue
        html = get(session, url)
        if not html:
            continue
        article = BeautifulSoup(html, "html.parser")
        if article_date(article) != today:
            continue
        parsed = aia_parse(article)
        if not parsed:
            continue
        opponent, roles = parsed
        if send(format_message("🇮🇹", hashtag(opponent), roles)):
            state["aia"][url] = {"sent_at": now().isoformat(), "signature": sig(roles)}
            changed = True
            print(f"[ARBITRI AIA] inviata Juve-{opponent}")
    return changed


def matchinfo(url):
    path = urlparse(url).path.rstrip("/")
    if path.endswith("/matchinfo"):
        return url
    return url.rstrip("/") + "/matchinfo/"


def uefa_fixture_links(session, schedule):
    html = get(session, schedule)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        url = urljoin(schedule, a["href"])
        if "/match/" not in urlparse(url).path.lower() or url in seen:
            continue
        context = clean(a.parent.get_text(" ", strip=True) if a.parent else a.get_text(" ", strip=True))
        if "juventus" in context.lower() or "juve" in context.lower():
            seen.add(url)
            m = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](20\d{2})\b", context)
            d = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else ""
            out.append((url, d))
    return out


def uefa_parse(session, url):
    info = matchinfo(url)
    html = get(session, info)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = [clean(x) for x in text.splitlines() if clean(x)]

    title = clean(soup.title.get_text(" ", strip=True)) if soup.title else ""
    home = away = ""
    m = re.search(r"(.+?)\s+(?:v|vs|–|-|—)\s+(.+?)(?:\s+\||$)", title, re.I)
    if m:
        home, away = clean(m.group(1)), clean(m.group(2))
    if "juventus" not in f"{home} {away}".lower():
        for line in lines:
            if "juventus" in line.lower() and re.search(r"\b(v|vs)\b|[-–—]", line, re.I):
                p = re.split(r"\s+(?:v|vs)\s+|\s*[-–—]\s*", line, maxsplit=1, flags=re.I)
                if len(p) == 2:
                    home, away = p[0], p[1]
                    break
    if "juventus" not in f"{home} {away}".lower():
        return None
    opponent = away if "juventus" in home.lower() else home

    # UEFA renders officials together with three-letter nationality codes.
    # Find the relevant label and consume the names/codes until the next role.
    role_labels = {
        "ARBITRO": ["referee", "arbitro", "schiedsrichter"],
        "ASSISTENTI": ["assistant referees", "assistenti", "schiedsrichterassistenten"],
        "IV": ["fourth official", "quarto ufficiale", "vierter offizieller"],
        "VAR": ["video assistant referee", "videoassistent"],
        "AVAR": ["first assistant of the video assistant", "erster assistent des videoassistenten"],
    }
    role_lines = {}
    for role, labels in role_labels.items():
        for line in lines:
            if any(label in line.lower() for label in labels):
                role_lines[role] = line
                break

    def people(line):
        if not line:
            return []
        tokens = line.split()
        result = []
        for i, token in enumerate(tokens):
            code = token.strip("(),").upper()
            if not re.fullmatch(r"[A-Z]{3}", code) or code not in FLAGS:
                continue
            names = []
            j = i - 1
            while j >= 0 and len(names) < 4:
                t = tokens[j].strip(",")
                if t.upper() in {"REFEREE", "ARBITRO", "SCHIEDSRICHTER", "ASSISTENTS", "ASSISTENTI", "OFFICIAL", "OFFIZIELLER", "VAR", "AVAR"}:
                    break
                if re.fullmatch(r"[A-Z]{3}", t.upper()):
                    break
                names.append(t)
                j -= 1
            if names:
                result.append((surname(" ".join(reversed(names))), FLAGS[code]))
        return result

    parsed = {role: people(role_lines.get(role, "")) for role in role_labels}
    if not all(parsed[x] for x in role_labels):
        return {"designated": False, "info_url": info, "home": home, "away": away, "opponent": opponent}

    roles = {
        "ARBITRO": parsed["ARBITRO"][0][0],
        "ASSISTENTI": " – ".join(x[0] for x in parsed["ASSISTENTI"]),
        "IV": parsed["IV"][0][0],
        "VAR": parsed["VAR"][0][0],
        "AVAR": parsed["AVAR"][0][0],
    }
    flags = {
        "ARBITRO": parsed["ARBITRO"][0][1],
        "ASSISTENTI": " – ".join(x[1] for x in parsed["ASSISTENTI"]),
        "IV": parsed["IV"][0][1],
        "VAR": parsed["VAR"][0][1],
        "AVAR": parsed["AVAR"][0][1],
    }
    return {"designated": True, "info_url": info, "home": home, "away": away, "opponent": opponent, "roles": roles, "flags": flags}


def uefa_scan(session, state):
    changed = False
    today = now().date()
    for competition, schedule in UEFA_COMPETITIONS.items():
        fixtures = uefa_fixture_links(session, schedule)
        print(f"[ARBITRI UEFA] {competition}: {len(fixtures)} partite trovate")
        for url, date_text in fixtures:
            if date_text:
                try:
                    if date.fromisoformat(date_text) < today:
                        continue
                except ValueError:
                    pass
            result = uefa_parse(session, url)
            if not result or not result.get("designated"):
                continue
            key = result["info_url"]
            signature = sig(result["roles"], result["flags"])
            old = state["uefa"].get(key, {})
            old_sig = old if isinstance(old, str) else old.get("signature")
            if old_sig == signature:
                continue
            message = format_message("🇪🇺", hashtag(result["opponent"]), result["roles"], result["flags"])
            if send(message):
                state["uefa"][key] = {
                    "signature": signature,
                    "sent_at": now().isoformat(),
                    "competition": competition,
                    "match": f"{result['home']} - {result['away']}",
                    "match_date": date_text,
                }
                changed = True
                print(f"[ARBITRI UEFA] {'aggiornata' if old_sig else 'inviata'} {result['home']} - {result['away']}")
    return changed


def run(duration, interval):
    session = requests.Session()
    state = load_state()
    started = time.monotonic()
    cycle = 0
    while time.monotonic() - started < duration:
        cycle += 1
        print(f"[ARBITRI] ciclo {cycle}")
        try:
            aia_scan(session, state)
            uefa_scan(session, state)
            save_state(state)
        except Exception as e:
            print(f"[ARBITRI] errore ciclo: {e}")
            save_state(state)
        remaining = duration - (time.monotonic() - started)
        if remaining <= 0:
            break
        time.sleep(min(interval, max(1, int(remaining))))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--duration-seconds", type=int, default=3300)
    p.add_argument("--interval-seconds", type=int, default=60)
    a = p.parse_args()
    run(a.duration_seconds, a.interval_seconds)


if __name__ == "__main__":
    main()
