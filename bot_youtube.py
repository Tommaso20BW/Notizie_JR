import os
import re
import time
import json
import requests
import dropbox
import yt_dlp
from google import genai
from google.genai import types

# Configurazione variabili d'ambiente da GitHub Secrets
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
DROPBOX_FOLDER = "/NotizieJR"
TXT_FILENAME = "link.txt"

MODEL = "gemini-3.5-flash"

client = genai.Client(api_key=GEMINI_API_KEY)

# ---------------------------------------------------------------------------
# MAPPATURA FONTI
# ---------------------------------------------------------------------------
CANALI = {
    "agresti": ('<tg-emoji emoji-id="5784902446098685755">📲</tg-emoji>', "Romeo Agresti"),
    "moretto": ('<tg-emoji emoji-id="5785259727248170398">📲</tg-emoji>', "Matteo Moretto"),
    "romano":  ('<tg-emoji emoji-id="5785366354106261925">📲</tg-emoji>', "Fabrizio Romano"),
    "schira":  ('<tg-emoji emoji-id="5785305056333012850">📲</tg-emoji>', "Nicolò Schira"),
    "pedull":  ('<tg-emoji emoji-id="5785322627044220734">📲</tg-emoji>', "Alfredo Pedullà"),
}

TAG_FONTE = {
    "[FONTE_AGRESTI]": "agresti",
    "[FONTE_MORETTO]": "moretto",
    "[FONTE_ROMANO]":  "romano",
    "[FONTE_SCHIRA]":  "schira",
    "[FONTE_PEDULLA]": "pedull",
}

DEFAULT_EMOJI = "📲"

PROMPT = """Hai ricevuto la trascrizione completa di un video YouTube sulla Juventus.
Il tuo compito e' estrarre SOLO le notizie sulla Juventus dette ESPLICITAMENTE nella trascrizione.

REGOLE DI VERIDICITA':
- Riporta SOLO cio' che e' scritto nella trascrizione. NON aggiungere nulla.
- Non inventare, non dedurre, non completare con conoscenze pregresse.
- Se nella trascrizione non si parla della Juventus, non scrivere nessuna notizia.

FONTE: scegli UNO tra: [FONTE_AGRESTI] Romeo Agresti, [FONTE_MORETTO] Matteo Moretto,
[FONTE_ROMANO] Fabrizio Romano, [FONTE_SCHIRA] Nicolò Schira, [FONTE_PEDULLA] Alfredo Pedullà,
oppure [FONTE_ALTRO] se non e' nessuno di loro.
Scrivi il tag fonte sulla prima riga.

POI le notizie sulla Juventus, una per riga in questo formato:
[NOTIZIA][Emoji] Testo continuo della notizia

REGOLE DI FORMATTAZIONE:
- NON USARE MAI GLI ASTERISCHI (**). Usa SOLO i tag HTML <b> e </b> per il grassetto.
- Metti in grassetto <b>...</b> nomi e cognomi di giocatori, allenatori, dirigenti e nomi delle squadre.
- Massimo 280 caratteri a notizia, sii sintetico.
- Cifre in milioni sempre in formato compatto: 1M€, 50M€, 100M€. Mai "milioni di euro" ne' "mln".
- Separa ogni notizia con una riga vuota.
"""


def crea_dropbox_client():
    return dropbox.Dropbox(
        app_key=DROPBOX_APP_KEY,
        app_secret=DROPBOX_APP_SECRET,
        oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
    )


def get_urls_from_dropbox():
    dbx = crea_dropbox_client()
    file_path = f"{DROPBOX_FOLDER}/{TXT_FILENAME}"
    try:
        print(f"Lettura {file_path}...")
        metadata, response = dbx.files_download(file_path)
    except dropbox.exceptions.ApiError as e:
        print(f"Nessun file {TXT_FILENAME} trovato su Dropbox: {e}")
        return [], []

    content = response.content.decode("utf-8", errors="ignore")
    found = re.findall(r'https?://[^\s<>"\']+', content)
    found = [u.rstrip(').,;') for u in found]

    seen = set()
    urls = []
    for u in found:
        if u not in seen:
            seen.add(u)
            urls.append(u)

    print(f"Trovati {len(urls)} URL in {TXT_FILENAME}.")
    return urls, [metadata.path_lower]


def delete_files_from_dropbox(dropbox_paths):
    dbx = crea_dropbox_client()
    for path in dropbox_paths:
        try:
            dbx.files_delete_v2(path)
            print(f"File {path} cancellato da Dropbox.")
        except Exception as e:
            print(f"Errore cancellazione {path}: {e}")


def is_youtube(url):
    u = url.lower()
    return "youtube.com" in u or "youtu.be" in u


def normalize_youtube_url(url):
    video_id = None
    m = re.search(r'youtu\.be/([A-Za-z0-9_-]{11})', url)
    if m:
        video_id = m.group(1)
    if not video_id:
        m = re.search(r'[?&]v=([A-Za-z0-9_-]{11})', url)
        if m:
            video_id = m.group(1)
    if not video_id:
        m = re.search(r'youtube\.com/(?:live|shorts|embed)/([A-Za-z0-9_-]{11})', url)
        if m:
            video_id = m.group(1)
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return url


def get_youtube_meta(url):
    try:
        r = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=10,
        )
        if r.ok:
            data = r.json()
            return data.get("title", ""), data.get("author_name", "")
        print(f"oEmbed non ok ({r.status_code}) per {url}")
    except Exception as e:
        print(f"Errore oEmbed per {url}: {e}")
    return "", ""


def get_transcript(video_url):
    """Scarica i sottotitoli automatici con yt-dlp. Prova it -> en -> qualsiasi."""
    ydl_opts = {
        "writeautomaticsub": True,
        "writesubtitles": True,
        "subtitleslangs": ["it", "en"],
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)

        # Cerca prima sottotitoli automatici, poi manuali
        for subs_key in ("automatic_captions", "subtitles"):
            subs = info.get(subs_key, {})
            for lang in ["it", "en"] + list(subs.keys()):
                if lang not in subs:
                    continue
                # Prende il formato json3 o srv3 o ttml o vtt
                formati = subs[lang]
                url_sub = None
                for fmt in formati:
                    if fmt.get("ext") in ("json3", "srv3", "ttml", "vtt"):
                        url_sub = fmt["url"]
                        break
                if not url_sub and formati:
                    url_sub = formati[0]["url"]
                if not url_sub:
                    continue

                r = requests.get(url_sub, timeout=15)
                if not r.ok:
                    continue

                testo = estrai_testo_sottotitoli(r.text, formati[0].get("ext", ""))
                if testo:
                    print(f"Trascrizione scaricata (lingua: {lang}, {len(testo)} caratteri).")
                    return testo

        print("Nessun sottotitolo trovato.")
        return None

    except Exception as e:
        print(f"Errore scaricamento trascrizione: {e}")
        return None


def estrai_testo_sottotitoli(contenuto, ext):
    """Estrae testo puro da json3, vtt o altri formati."""
    try:
        if ext == "json3":
            data = json.loads(contenuto)
            testi = []
            for evento in data.get("events", []):
                for seg in evento.get("segs", []):
                    t = seg.get("utf8", "").strip()
                    if t and t != "\n":
                        testi.append(t)
            return " ".join(testi)
    except Exception:
        pass

    # Fallback: rimuove tag e righe di timing (funziona per vtt, ttml, srv)
    testo = re.sub(r'<[^>]+>', ' ', contenuto)
    testo = re.sub(r'\d{2}:\d{2}[^\n]*', '', testo)
    testo = re.sub(r'WEBVTT.*', '', testo, flags=re.DOTALL | re.IGNORECASE)
    testo = re.sub(r'[\r\n]+', ' ', testo)
    testo = re.sub(r'\s{2,}', ' ', testo)
    return testo.strip()


def generate_news_from_transcript(titolo, autore, trascrizione):
    contenuto = (
        f"{PROMPT}\n\n"
        f"TITOLO VIDEO: {titolo}\n"
        f"CANALE: {autore}\n\n"
        f"TRASCRIZIONE:\n{trascrizione}"
    )
    response = client.models.generate_content(
        model=MODEL,
        contents=contenuto,
        config=types.GenerateContentConfig(
            temperature=0.1,
            seed=42,
        ),
    )
    return response.text


def generate_news_from_url(url):
    """Fallback per URL non YouTube."""
    print(f"Invio pagina web a Gemini (URL context): {url}")
    response = client.models.generate_content(
        model=MODEL,
        contents=f"{PROMPT}\n\nContenuto da analizzare a questo indirizzo: {url}",
        config=types.GenerateContentConfig(
            temperature=0.1,
            seed=42,
            tools=[{"url_context": {}}],
        ),
    )
    return response.text


def estrai_meta(raw):
    found_key = None
    for tag, key in TAG_FONTE.items():
        if tag in raw and found_key is None:
            found_key = key
    for tag in list(TAG_FONTE.keys()) + ["[FONTE_ALTRO]"]:
        raw = raw.replace(tag, "")
    return found_key, raw.strip()


def split_notizie(raw):
    if "[NOTIZIA]" in raw:
        lista = [n.strip() for n in raw.split("[NOTIZIA]") if n.strip()]
    else:
        lista = [n.strip() for n in raw.split("\n\n") if n.strip()]
    return lista


def pulisci_notizia(testo):
    testo = testo.replace("**", "")
    testo = re.sub(r'\[FONTE[^\]]*\]', '', testo)
    testo = re.sub(r'\(\d{1,2}:\d{2}(?::\d{2})?\)', '', testo)
    testo = re.sub(r'\s{2,}', ' ', testo)
    return testo.strip()


def send_to_telegram(news_list, emoji_fonte, nome_fonte):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    tg_reborn = '<tg-emoji emoji-id="5985659276327132147">👉</tg-emoji>'

    for news in news_list:
        clean = news.strip()
        if not clean:
            continue
        testo = f"{clean}\n\n{emoji_fonte} <i>{nome_fonte}</i>\n\n{tg_reborn} @Juventus_Reborn"
        try:
            resp = requests.post(
                url,
                json={"chat_id": CHAT_ID, "text": testo, "parse_mode": "HTML"},
                timeout=10,
            )
            if not resp.ok:
                print(f"Errore Telegram: {resp.status_code} - {resp.text}")
        except Exception as e:
            print(f"Errore invio Telegram: {e}")
        time.sleep(1)


def elabora_url(link):
    clean_link = normalize_youtube_url(link) if is_youtube(link) else link

    if not is_youtube(link):
        try:
            raw = generate_news_from_url(link)
        except Exception as e:
            print(f"Errore Gemini: {e}")
            return
        found_key, testo = estrai_meta(raw)
        emoji_fonte, nome_fonte = DEFAULT_EMOJI, "Web"
        lista = [pulisci_notizia(n) for n in split_notizie(testo)]
        lista = [n for n in lista if n]
        if lista:
            send_to_telegram(lista, emoji_fonte, nome_fonte)
        return

    # YouTube
    titolo_reale, autore_reale = get_youtube_meta(clean_link)
    print(f"Titolo video: {titolo_reale!r}")
    print(f"Canale: {autore_reale!r}")

    trascrizione = get_transcript(clean_link)
    if not trascrizione:
        print("Nessuna trascrizione disponibile. Salto.")
        return

    try:
        raw = generate_news_from_transcript(titolo_reale, autore_reale, trascrizione)
    except Exception as e:
        print(f"Errore Gemini: {e}")
        return

    if not raw or not raw.strip():
        print("Risposta vuota da Gemini. Non invio nulla.")
        return

    found_key, testo = estrai_meta(raw)

    if found_key:
        emoji_fonte, nome = CANALI[found_key]
        nome_fonte = f"{nome} - YouTube"
    elif autore_reale.strip():
        emoji_fonte, nome_fonte = DEFAULT_EMOJI, f"{autore_reale.strip()} - YouTube"
    else:
        emoji_fonte, nome_fonte = DEFAULT_EMOJI, "YouTube"
    print(f"Fonte rilevata: {nome_fonte}")

    lista = [pulisci_notizia(n) for n in split_notizie(testo)]
    lista = [n for n in lista if n]
    print(f"Notizie trovate: {len(lista)}")
    if lista:
        send_to_telegram(lista, emoji_fonte, nome_fonte)
    else:
        print("Nessuna notizia sulla Juventus in questo video.")


if __name__ == "__main__":
    urls, dropbox_paths = get_urls_from_dropbox()

    if not urls:
        print("Nessun URL nuovo. Chiusura.")
    else:
        print(f"Trovati {len(urls)} URL da elaborare.")
        for i, link in enumerate(urls):
            print(f"\nElaborazione URL: {link}")
            elabora_url(link)

            if i < len(urls) - 1:
                print("In attesa di 20 secondi prima del prossimo URL...")
                time.sleep(20)

        print("Cancellazione file txt da Dropbox...")
        delete_files_from_dropbox(dropbox_paths)

        print("Operazione completata.")
