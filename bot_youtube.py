import os
import re
import time
import requests
import dropbox
from google import genai
from google.genai import types
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import NoTranscriptFound, TranscriptsDisabled, YouTubeTranscriptApiException

# Configurazione variabili d'ambiente da GitHub Secrets
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
DROPBOX_FOLDER = "/NotizieJR"
TXT_FILENAME = "link.txt"  # Nome fisso del file da cui leggere i link

# Modello Gemini ottimizzato per compiti testuali veloci ed economici
MODEL = "gemini-2.5-flash"

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

# Tag che Gemini puo' scrivere -> chiave in CANALI
TAG_FONTE = {
    "[FONTE_AGRESTI]": "agresti",
    "[FONTE_MORETTO]": "moretto",
    "[FONTE_ROMANO]":  "romano",
    "[FONTE_SCHIRA]":  "schira",
    "[FONTE_PEDULLA]": "pedull",
}

# Emoji usata quando il giornalista NON e' tra quelli mappati
DEFAULT_EMOJI = "📲"

# Parola che Gemini deve usare se non riesce a leggere il video
SENTINEL_NO_VIDEO = "VIDEO_NON_LEGGIBILE"

# Prompt ad alta veridicita': Gemini si baserà sul testo estratto dai sottotitoli.
PROMPT = f"""Stai analizzando il testo estratto dai SOTTOTITOLI di uno specifico video di YouTube. Il tuo compito e' estrarre SOLO le notizie sulla Juventus che vengono dette ESPLICITAMENTE nel testo.

REGOLE DI VERIDICITA' (LE PIU' IMPORTANTI):
- Riporta SOLO ed esclusivamente cio' che viene scritto nel testo dei sottotitoli fornito.
- NON usare MAI tue conoscenze pregresse, voci di mercato o notizie che conosci da altre fonti. Se una cosa non è presente nel testo, NON scriverla.
- Non inventare, non dedurre, non "completare" con cio' che ti sembra plausibile.
- Se il testo fornito indica un errore o è vuoto, rispondi ESATTAMENTE con questa sola parola e nient'altro: {SENTINEL_NO_VIDEO}

PROVA DI VISIONE (obbligatoria, da scrivere PRIMA di tutto):
[TITOLO] Inventa o estrai un titolo coerente basandoti sul contesto del testo (es. il focus principale del discorso)
[DURATA] Scrivi "ND" (Non Disponibile dai sottotitoli)
[FONTE_X] chi parla, scegliendo UNO tra: [FONTE_AGRESTI] Romeo Agresti, [FONTE_MORETTO] Matteo Moretto, [FONTE_ROMANO] Fabrizio Romano, [FONTE_SCHIRA] Nicolò Schira, [FONTE_PEDULLA] Alfredo Pedullà, oppure [FONTE_ALTRO] se non e' nessuno di loro

POI le notizie sulla Juventus, una per riga in questo formato:
[NOTIZIA][Emoji] (mm:ss) Testo continuo della notizia
Nota: se i sottotitoli non contengono i minutaggi esatti, ometti il timestamp (mm:ss) o usa dei riferimenti se presenti.

REGOLE DI FORMATTAZIONE:
- NON USARE MAI GLI ASTERISCHI (**). Usa SOLO i tag HTML <b> e </b> per il grassetto.
- Metti in grassetto <b>...</b> nomi e cognomi di giocatori, allenatori, dirigenti e i nomi delle squadre.
- Massimo 280 caratteri a notizia, sii sintetico.
- Cifre in milioni sempre in formato compatto: 1M€, 50M€, 100M€. Mai "milioni di euro" ne' "mln".
- Separa ogni notizia con una riga vuota.
- Se nel testo non si parla della Juventus, scrivi solo le righe [TITOLO]/[DURATA]/[FONTE_X] e nessuna notizia.
"""


def crea_dropbox_client():
    """Crea il client Dropbox con refresh token (non scade mai)"""
    return dropbox.Dropbox(
        app_key=DROPBOX_APP_KEY,
        app_secret=DROPBOX_APP_SECRET,
        oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
    )


def get_urls_from_dropbox():
    """Legge il file link.txt nella cartella Dropbox ed estrae gli URL contenuti."""
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
    """Cancella i file da Dropbox dopo l'elaborazione."""
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


def extract_youtube_id(url):
    """Estrae l'ID univoco del video di YouTube."""
    m = re.search(r'youtu\.be/([A-Za-z0-9_-]{11})', url)
    if m:
        return m.group(1)
    m = re.search(r'[?&]v=([A-Za-z0-9_-]{11})', url)
    if m:
        return m.group(1)
    m = re.search(r'youtube\.com/(?:live|shorts|embed)/([A-Za-z0-9_-]{11})', url)
    if m:
        return m.group(1)
    return None


def normalize_youtube_url(url):
    video_id = extract_youtube_id(url)
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return url


def get_youtube_meta(url):
    """Recupera (titolo, nome_canale) del video via oEmbed. ('', '') se non disponibile."""
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


def generate_news_from_url(url):
    """Estrae i sottotitoli e li invia a Gemini, evitando il download multimediale."""
    if is_youtube(url):
        video_id = extract_youtube_id(url)
        if not video_id:
            print(f"Impossibile estrarre ID da {url}")
            return SENTINEL_NO_VIDEO

        print(f"Estrazione sottotitoli per l'ID YouTube: {video_id}")
        try:
            ytt = YouTubeTranscriptApi()
            transcript_list = ytt.fetch(video_id, languages=['it', 'en'])

            # Genera la stringa temporizzata inserendo i minuti iniziali del blocco
            sottotitoli_str = ""
            for seg in transcript_list:
                minutes = int(seg['start'] // 60)
                seconds = int(seg['start'] % 60)
                sottotitoli_str += f"({minutes:02d}:{seconds:02d}) {seg['text']}\n"

        except (NoTranscriptFound, TranscriptsDisabled) as e:
            print(f"Sottotitoli non disponibili o disabilitati su YT: {e}")
            return SENTINEL_NO_VIDEO
        except YouTubeTranscriptApiException as e:
            print(f"Errore youtube-transcript-api: {e}")
            return SENTINEL_NO_VIDEO
        except Exception as e:
            print(f"Errore imprevisto nell'estrazione sottotitoli: {e}")
            return SENTINEL_NO_VIDEO

        print("Inoltro dei sottotitoli a Gemini...")
        testo_input = f"{PROMPT}\n\nSOTTOTITOLI DEL VIDEO DA ANALIZZARE:\n{sottotitoli_str}"

        response = client.models.generate_content(
            model=MODEL,
            contents=testo_input,
            config=types.GenerateContentConfig(
                temperature=0.1,
                seed=42,
            ),
        )
        return response.text

    # Gestione pagine web standard (lasciata invariata come ripiego)
    print(f"Invio pagina web a Gemini (URL context): {url}")
    response = client.models.generate_content(
        model=MODEL,
        contents=f"{PROMPT}\n\nContenuto da analizzare a questo indirizzo: {url}",
        config=types.GenerateContentConfig(temperature=0.1, seed=42, tools=[{"url_context": {}}]),
    )
    return response.text


def estrai_meta(raw):
    """
    Estrae titolo riportato e fonte dalle righe di intestazione,
    e restituisce il testo notizie ripulito dai tag.
    """
    titolo = ""
    m = re.search(r'\[TITOLO\]\s*(.+)', raw)
    if m:
        titolo = m.group(1).strip()

    found_key = None
    for tag, key in TAG_FONTE.items():
        if tag in raw and found_key is None:
            found_key = key

    # Rimuove le righe di intestazione e tutti i tag fonte
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

    # Considera solo parole "significative" (3+ lettere) del titolo reale
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
    """Toglie timestamp residui non formattati, asterischi e spazi doppi."""
    testo = testo.replace("**", "")
    testo = re.sub(r'\[FONTE[^\]]*\]', '', testo)
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
    """Elabora un singolo URL con tutte le verifiche anti-allucinazione."""
    clean_link = normalize_youtube_url(link) if is_youtube(link) else link

    # Titolo + canale reali da YouTube (servono per la verifica e per il ripiego fonte)
    titolo_reale, autore_reale = ("", "")
    if is_youtube(link):
        titolo_reale, autore_reale = get_youtube_meta(clean_link)

    try:
        raw = generate_news_from_url(link)
    except Exception as e:
        print(f"Errore generazione: {e}")
        return

    if not raw or not raw.strip():
        print("Risposta vuota da Gemini. Non invio nulla.")
        return

    # 1) Gemini dichiara di non aver letto il video/sottotitoli
    if SENTINEL_NO_VIDEO in raw.upper():
        print(f"Impossibile elaborare il video ({SENTINEL_NO_VIDEO}). Sottotitoli assenti. NON invio nulla.")
        return

    titolo_g, found_key, testo = estrai_meta(raw)

    # 2) Verifica anti-allucinazione tollerante basata sul titolo reale
    if titolo_reale and titolo_g:
        if not titoli_combaciano(titolo_g, titolo_reale):
            print("ATTENZIONE: Il titolo generato differisce molto da quello oEmbed reale.")
            print(f"  Titolo ipotizzato da Gemini: {titolo_g!r}")
            print(f"  Titolo reale del video:       {titolo_reale!r}")
            print("  Procedo comunque basandomi sulla fedeltà dei sottotitoli estrapolati.")

    # 3) Fonte (chi parla)
    if found_key:
        emoji_fonte, nome = CANALI[found_key]
        nome_fonte = f"{nome} - YouTube"
    elif autore_reale.strip():
        emoji_fonte, nome_fonte = DEFAULT_EMOJI, f"{autore_reale.strip()} - YouTube"
    else:
        emoji_fonte, nome_fonte = DEFAULT_EMOJI, "YouTube"
    print(f"Fonte rilevata: {nome_fonte}")

    # 4) Pulizia e invio
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
