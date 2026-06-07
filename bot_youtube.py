import os
import re
import glob
import subprocess
import tempfile
import time
import requests
import dropbox
from google import genai
from google.genai import types

# Configurazione variabili d'ambiente da GitHub Secrets
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
YOUTUBE_COOKIES = os.getenv("YOUTUBE_COOKIES")   # contenuto del file cookies.txt in formato Netscape
DROPBOX_FOLDER = "/NotizieJR"
TXT_FILENAME = "link.txt"

# Modello Gemini. Se le allucinazioni continuano, prova un modello Pro
# (cambia solo questa riga, il resto del codice non si tocca).
MODEL = "gemini-3.5-flash"

# Inizializzazione del client ufficiale Google GenAI
client = genai.Client(api_key=GEMINI_API_KEY)

# ---------------------------------------------------------------------------
# MAPPATURA FONTI (giornalista che parla -> emoji personalizzata + nome)
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
SENTINEL_NO_VIDEO = "VIDEO_NON_LEGGIBILE"

PROMPT = f"""Stai analizzando il trascritto di un video YouTube. Il tuo compito e' estrarre SOLO le notizie sulla Juventus dette ESPLICITAMENTE in questo trascritto.

REGOLE DI VERIDICITA' (LE PIU' IMPORTANTI):
- Riporta SOLO ed esclusivamente cio' che e' scritto nel trascritto.
- NON usare MAI tue conoscenze pregresse, voci di mercato o notizie che conosci da altre fonti.
- Non inventare, non dedurre, non "completare" con cio' che ti sembra plausibile.

Prima di tutto scrivi:
[FONTE_X] chi parla, scegliendo UNO tra: [FONTE_AGRESTI] Romeo Agresti, [FONTE_MORETTO] Matteo Moretto, [FONTE_ROMANO] Fabrizio Romano, [FONTE_SCHIRA] Nicolò Schira, [FONTE_PEDULLA] Alfredo Pedullà, oppure [FONTE_ALTRO] se non e' nessuno di loro

POI le notizie sulla Juventus, una per riga in questo formato:
[NOTIZIA][Emoji] (mm:ss) Testo continuo della notizia
dove (mm:ss) e' il timestamp del trascritto in cui se ne parla.

REGOLE DI FORMATTAZIONE:
- NON USARE MAI GLI ASTERISCHI (**). Usa SOLO i tag HTML <b> e </b> per il grassetto.
- Metti in grassetto <b>...</b> nomi e cognomi di giocatori, allenatori, dirigenti e i nomi delle squadre.
- Massimo 280 caratteri a notizia, sii sintetico.
- Cifre in milioni sempre in formato compatto: 1M€, 50M€, 100M€. Mai "milioni di euro" ne' "mln".
- Separa ogni notizia con una riga vuota.
- Se nel trascritto non si parla della Juventus, scrivi solo [FONTE_X] e nessuna notizia.
"""


# ---------------------------------------------------------------------------
# DROPBOX
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# YOUTUBE UTILS
# ---------------------------------------------------------------------------

def is_youtube(url):
    u = url.lower()
    return "youtube.com" in u or "youtu.be" in u


def normalize_youtube_url(url):
    """Restituisce un URL YouTube canonico (https://www.youtube.com/watch?v=ID)."""
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
    """Recupera (titolo, nome_canale) del video via oEmbed."""
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


# ---------------------------------------------------------------------------
# YT-DLP: scarica sottotitoli e li converte in testo con timestamp
# ---------------------------------------------------------------------------

def parse_srt(srt_text):
    """Converte SRT in righe '(mm:ss) testo', rimuovendo tag HTML."""
    lines = []
    for block in re.split(r'\n{2,}', srt_text.strip()):
        parts = block.strip().splitlines()
        if len(parts) < 2:
            continue
        # Cerca la riga con il timestamp (es. 00:01:23,456 --> 00:01:25,789)
        time_line = next((p for p in parts if re.match(r'\d{2}:\d{2}:\d{2}', p)), None)
        if not time_line:
            continue
        m = re.match(r'(\d{2}):(\d{2}):(\d{2})', time_line)
        hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3))
        total_mm = hh * 60 + mm
        # Testo: tutto ciò che non è numero di sequenza o riga timestamp
        text_parts = [
            p for p in parts
            if not re.match(r'^\d+$', p.strip())
            and not re.match(r'\d{2}:\d{2}:\d{2}', p)
        ]
        text = re.sub(r'<[^>]+>', '', ' '.join(text_parts)).strip()
        if text:
            lines.append(f"({total_mm:02d}:{ss:02d}) {text}")
    return '\n'.join(lines) if lines else None


def get_transcript_ytdlp(video_url):
    """
    Scarica i sottotitoli del video con yt-dlp usando i cookie da Secret.
    Prova prima italiano, poi inglese.
    Restituisce testo '(mm:ss) riga...' o None se non disponibile.
    """
    if not YOUTUBE_COOKIES:
        print("YOUTUBE_COOKIES non configurato, salto yt-dlp.")
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        cookies_path = os.path.join(tmpdir, "cookies.txt")
        with open(cookies_path, "w", encoding="utf-8") as f:
            f.write(YOUTUBE_COOKIES)

        out_template = os.path.join(tmpdir, "video")
        cmd = [
            "yt-dlp",
            "--cookies", cookies_path,
            "--write-auto-subs",
            "--sub-langs", "it.*,en.*",
            "--skip-download",
            "--convert-subs", "srt",
            "-o", out_template,
            video_url,
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            if result.returncode != 0:
                print(f"yt-dlp errore (code {result.returncode}): {result.stderr[:300]}")
                return None
        except subprocess.TimeoutExpired:
            print("yt-dlp timeout dopo 90s.")
            return None
        except FileNotFoundError:
            print("yt-dlp non trovato. Aggiungilo a requirements.txt.")
            return None

        srt_files = sorted(glob.glob(os.path.join(tmpdir, "*.srt")))
        if not srt_files:
            print("yt-dlp: nessun file .srt scaricato.")
            return None

        # Preferisci italiano se disponibile
        chosen = next((f for f in srt_files if ".it." in f), srt_files[0])
        print(f"Sottotitolo scelto: {os.path.basename(chosen)}")

        with open(chosen, "r", encoding="utf-8") as f:
            srt_text = f.read()

        return parse_srt(srt_text)


# ---------------------------------------------------------------------------
# GEMINI
# ---------------------------------------------------------------------------

def generate_news_from_url(url):
    """
    Invia il contenuto del video/pagina a Gemini.
    Ritorna (testo_risposta, video_verificato).
    video_verificato=True significa che il trascritto e' stato ottenuto
    direttamente via yt-dlp: la verifica titolo viene saltata.
    """
    if is_youtube(url):
        clean_url = normalize_youtube_url(url)

        # --- APPROCCIO 1: trascritto via yt-dlp (affidabile al 100%) ---
        transcript = get_transcript_ytdlp(clean_url)
        if transcript:
            print(f"Trascritto disponibile ({len(transcript)} car.). Invio a Gemini.")
            contents = f"Cosa dice nel video:\n\n{transcript}\n\n{PROMPT}"
            response = client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=types.GenerateContentConfig(temperature=0.1, seed=42),
            )
            return response.text, True  # verificato: il trascritto e' del video giusto

        # --- APPROCCIO 2: URL nel testo come fallback ---
        print(f"Nessun trascritto. Invio URL diretto a Gemini: {clean_url}")
        contents = f"Cosa dice nel video: {clean_url}\n\n{PROMPT}"
        response = client.models.generate_content(
            model=MODEL,
            contents=contents,
            config=types.GenerateContentConfig(temperature=0.1, seed=42),
        )
        return response.text, False  # non verificato: titolo check attivo

    # --- Pagina web non-YouTube ---
    print(f"Invio pagina web a Gemini (URL context): {url}")
    response = client.models.generate_content(
        model=MODEL,
        contents=f"{PROMPT}\n\nContenuto da analizzare a questo indirizzo: {url}",
        config=types.GenerateContentConfig(temperature=0.1, seed=42, tools=[{"url_context": {}}]),
    )
    return response.text, False


# ---------------------------------------------------------------------------
# PARSING RISPOSTA GEMINI
# ---------------------------------------------------------------------------

def estrai_meta(raw):
    """Estrae fonte e restituisce il testo notizie ripulito dai tag."""
    titolo = ""
    m = re.search(r'\[TITOLO\]\s*(.+)', raw)
    if m:
        titolo = m.group(1).strip()

    found_key = None
    for tag, key in TAG_FONTE.items():
        if tag in raw and found_key is None:
            found_key = key

    raw = re.sub(r'\[TITOLO\].*', '', raw)
    raw = re.sub(r'\[DURATA\].*', '', raw)
    raw = re.sub(r'\[CANALE\].*', '', raw)
    for tag in list(TAG_FONTE.keys()) + ["[FONTE_ALTRO]"]:
        raw = raw.replace(tag, "")

    return titolo, found_key, raw.strip()


def titoli_combaciano(titolo_gemini, titolo_reale):
    """Confronto tollerante: True se i due titoli condividono abbastanza parole."""
    def parole(s):
        return set(re.sub(r'[^a-z0-9]+', ' ', s.lower()).split())

    a = parole(titolo_gemini)
    b = parole(titolo_reale)
    if not a or not b:
        return False

    b_sig = {w for w in b if len(w) >= 3} or b
    comuni = a & b_sig
    return (len(comuni) / len(b_sig)) >= 0.4


def split_notizie(raw):
    """Divide il testo di Gemini in singole notizie."""
    if "[NOTIZIA]" in raw:
        lista = [n.strip() for n in raw.split("[NOTIZIA]") if n.strip()]
    else:
        lista = [n.strip() for n in raw.split("\n\n") if n.strip()]
    return lista


def pulisci_notizia(testo):
    """Toglie timestamp (mm:ss), asterischi, tag residui e spazi doppi."""
    testo = testo.replace("**", "")
    testo = re.sub(r'\[FONTE[^\]]*\]', '', testo)
    testo = re.sub(r'\(\d{1,2}:\d{2}(?::\d{2})?\)', '', testo)
    testo = re.sub(r'\s{2,}', ' ', testo)
    return testo.strip()


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# ELABORAZIONE PRINCIPALE
# ---------------------------------------------------------------------------

def elabora_url(link):
    """Elabora un singolo URL con tutte le verifiche anti-allucinazione."""
    clean_link = normalize_youtube_url(link) if is_youtube(link) else link

    titolo_reale, autore_reale = ("", "")
    if is_youtube(link):
        titolo_reale, autore_reale = get_youtube_meta(clean_link)

    try:
        raw, video_verificato = generate_news_from_url(link)
    except Exception as e:
        print(f"Errore Gemini: {e}")
        return

    if not raw or not raw.strip():
        print("Risposta vuota da Gemini. Non invio nulla.")
        return

    if SENTINEL_NO_VIDEO in raw.upper():
        print(f"Gemini non e' riuscito a leggere il video. NON invio nulla.")
        return

    titolo_g, found_key, testo = estrai_meta(raw)

    # Verifica anti-allucinazione: attiva solo se Gemini ha caricato il video
    # direttamente (fallback URL). Se abbiamo usato yt-dlp, il trascritto e'
    # gia' verificato da noi e il check e' inutile.
    if not video_verificato:
        if titolo_reale:
            if not titoli_combaciano(titolo_g, titolo_reale):
                print("ATTENZIONE: probabile allucinazione, il video non risulta letto davvero.")
                print(f"  Titolo riportato da Gemini: {titolo_g!r}")
                print(f"  Titolo reale del video:     {titolo_reale!r}")
                print("  NON invio nulla.")
                return
        else:
            print("Impossibile verificare il titolo via oEmbed: procedo con cautela.")
    else:
        print(f"Video verificato via yt-dlp. Titolo reale: {titolo_reale!r}")

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


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

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
