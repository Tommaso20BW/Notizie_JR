import os
import re
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
DROPBOX_FOLDER = "/NotizieJR"
TXT_FILENAME = "link.txt"  # Nome fisso del file da cui leggere i link

# Inizializzazione del client ufficiale Google GenAI
client = genai.Client(api_key=GEMINI_API_KEY)

# ---------------------------------------------------------------------------
# MAPPATURA FONTI (giornalista che parla -> emoji personalizzata + nome)
#
# La chiave deve combaciare con il tag che Gemini scrive (vedi PROMPT).
# Per aggiungere un giornalista: aggiungi la voce qui E il relativo tag nel PROMPT.
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

# Emoji usata quando il giornalista NON e' tra quelli mappati sopra
DEFAULT_EMOJI = "📲"

# Prompt: stesse regole di formattazione del bot giornali, adattato ai video
# YouTube. Gemini prima identifica CHI PARLA, poi estrae le notizie sulla Juve.
PROMPT = """Sei un estrattore di notizie calcistiche estremamente preciso. Analizza il video YouTube allegato (puo' essere un video normale oppure una diretta live ormai terminata) e riporta SOLO le notizie riguardanti la Juventus.

PRIMA DI TUTTO, identifica QUALE giornalista sta parlando nel video. Basati sui nomi mostrati a schermo, su come si presenta, sul nome del canale o sul contesto del discorso. Scrivi come PRIMISSIMA RIGA della risposta UNO solo di questi tag, da solo su una riga:
[FONTE_AGRESTI] se parla Romeo Agresti
[FONTE_MORETTO] se parla Matteo Moretto
[FONTE_ROMANO] se parla Fabrizio Romano
[FONTE_SCHIRA] se parla Nicolò Schira
[FONTE_PEDULLA] se parla Alfredo Pedullà
[FONTE_ALTRO] se non e' nessuno di loro

REGOLA TASSATIVA ED IMPERATIVA:
- NON USARE MAI GLI ASTERISCHI (**) per il grassetto.
- Usa SOLO ed esclusivamente i tag HTML <b> e </b> per applicare il grassetto.

Formattazione richiesta:
1. Applica il grassetto HTML usando <b> e </b> sui nomi di battesimo e cognomi dei giocatori, allenatori, dirigenti (es: <b>Damien Comolli</b>) e squadre di calcio.
2. Struttura: [NOTIZIA][Emoji] Testo continuo senza titoli...
3. Sii sintetico (max 280 caratteri a notizia).
4. Per le cifre in milioni di euro usa SEMPRE il formato compatto: 1M€, 50M€, 100M€. Mai scrivere "milioni di euro" o "mln" o "M di euro".
5. Separa ogni notizia con una riga vuota.
6. Se nel video non si parla della Juventus, scrivi solo il tag della fonte e nessuna notizia.
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
    # Estrae ogni URL presente nel file (uno per riga o anche con testo intorno)
    found = re.findall(r'https?://[^\s<>"\']+', content)
    found = [u.rstrip(').,;') for u in found]

    # Rimuove eventuali duplicati mantenendo l'ordine
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
    """True se l'URL e' un video YouTube."""
    u = url.lower()
    return "youtube.com" in u or "youtu.be" in u


def normalize_youtube_url(url):
    """
    Estrae l'ID del video e restituisce un URL YouTube CANONICO
    (https://www.youtube.com/watch?v=ID), senza parametri di tracciamento.
    Necessario perche' Gemini accetta solo il formato canonico.
    """
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
    return url  # se non riconosciuto, restituisce l'originale


def get_youtube_author(url):
    """Recupera il nome del canale YouTube tramite oEmbed (solo come ripiego)."""
    try:
        r = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=10,
        )
        if r.ok:
            data = r.json()
            return data.get("author_name", "")
        print(f"oEmbed non ok ({r.status_code}) per {url}")
    except Exception as e:
        print(f"Errore oEmbed per {url}: {e}")
    return ""


def generate_news_from_url(url):
    """
    Invia l'URL a Gemini.
    - Video YouTube: Gemini "guarda" il video (anche le dirette terminate).
    - Altre pagine web: Gemini le legge con lo strumento URL context.
    """
    if is_youtube(url):
        clean_url = normalize_youtube_url(url)
        print(f"Invio video YouTube a Gemini: {clean_url}")
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=types.Content(
                parts=[
                    types.Part(file_data=types.FileData(file_uri=clean_url)),
                    types.Part(text=PROMPT),
                ]
            ),
        )
        return response.text

    print(f"Invio pagina web a Gemini (URL context): {url}")
    response = client.models.generate_content(
        model="gemini-3.5-flash",
        contents=f"{PROMPT}\n\nContenuto da analizzare a questo indirizzo: {url}",
        config=types.GenerateContentConfig(tools=[{"url_context": {}}]),
    )
    return response.text


def determina_fonte(raw, url):
    """
    Determina chi parla nel video dal tag inserito da Gemini e ripulisce il
    testo dai tag. Se Gemini non riconosce nessuno (FONTE_ALTRO o niente),
    ripiega sul nome del canale YouTube via oEmbed.
    Restituisce: (emoji, nome_fonte, testo_pulito)
    """
    found_key = None
    for tag, key in TAG_FONTE.items():
        if tag in raw:
            if found_key is None:
                found_key = key
            raw = raw.replace(tag, "")
    raw = raw.replace("[FONTE_ALTRO]", "").strip()

    if found_key:
        emoji, nome = CANALI[found_key]
        return emoji, f"{nome} - YouTube", raw

    # Ripiego: nome del canale (uploader) via oEmbed
    author_name = ""
    if is_youtube(url):
        author_name = get_youtube_author(normalize_youtube_url(url))
    if author_name.strip():
        return DEFAULT_EMOJI, f"{author_name.strip()} - YouTube", raw
    return DEFAULT_EMOJI, "YouTube", raw


def split_notizie(raw):
    """
    Divide il testo di Gemini in singole notizie.
    Prova prima con [NOTIZIA], poi con doppio newline (paragrafi).
    """
    if "[NOTIZIA]" in raw:
        lista = [n.strip() for n in raw.split("[NOTIZIA]") if n.strip()]
    else:
        lista = [n.strip() for n in raw.split("\n\n") if n.strip()]
    return lista


def send_to_telegram(news_list, emoji_fonte, nome_fonte):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    tg_reborn = '<tg-emoji emoji-id="5985659276327132147">👉</tg-emoji>'

    for news in news_list:
        clean = news.strip()
        if not clean:
            continue

        # Rimuove eventuali asterischi e tag fonte residui
        clean = clean.replace("**", "")
        clean = re.sub(r'\[FONTE[^\]]*\]', '', clean).strip()
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

        # Piccola pausa tra un messaggio e l'altro per evitare rate limit
        time.sleep(1)


if __name__ == "__main__":
    urls, dropbox_paths = get_urls_from_dropbox()

    if not urls:
        print("Nessun URL nuovo. Chiusura.")
    else:
        print(f"Trovati {len(urls)} URL da elaborare.")
        for i, link in enumerate(urls):
            print(f"\nElaborazione URL: {link}")

            try:
                # 1) Gemini guarda il video, identifica chi parla ed estrae le notizie
                raw = generate_news_from_url(link)

                if raw and raw.strip():
                    # 2) Determina la fonte (chi parla) dal tag e pulisce il testo
                    emoji_fonte, nome_fonte, raw = determina_fonte(raw, link)
                    print(f"Fonte rilevata: {nome_fonte}")

                    # 3) Spezza e invia
                    lista = split_notizie(raw)
                    print(f"Notizie trovate: {len(lista)}")
                    send_to_telegram(lista, emoji_fonte, nome_fonte)
                else:
                    print("Nessuna notizia estratta da questo URL.")
            except Exception as e:
                print(f"Errore Gemini: {e}")

            if i < len(urls) - 1:
                print("In attesa di 20 secondi prima del prossimo URL...")
                time.sleep(20)

        print("Cancellazione file txt da Dropbox...")
        delete_files_from_dropbox(dropbox_paths)

        print("Operazione completata.")
