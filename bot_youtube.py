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

# Inizializzazione del client ufficiale Google GenAI
client = genai.Client(api_key=GEMINI_API_KEY)

# ---------------------------------------------------------------------------
# MAPPATURA FONTI (canali YouTube -> emoji personalizzata + nome mostrato)
#
# Come aggiungere/modificare un canale:
#   "parola_chiave": ('<tg-emoji emoji-id="ID">📲</tg-emoji>', "Nome Mostrato")
#
# La "parola_chiave" (in minuscolo) viene cercata dentro il nome e l'handle
# del canale restituiti da YouTube. Es: il canale "Romeo Agresti" contiene
# "agresti", quindi basta usare "agresti" come chiave.
# ---------------------------------------------------------------------------
CANALI = {
    "agresti": ('<tg-emoji emoji-id="5784902446098685755">📲</tg-emoji>', "Romeo Agresti"),
    "moretto": ('<tg-emoji emoji-id="5785259727248170398">📲</tg-emoji>', "Matteo Moretto"),
    "romano":  ('<tg-emoji emoji-id="5785366354106261925">📲</tg-emoji>', "Fabrizio Romano"),
    "schira":  ('<tg-emoji emoji-id="5785305056333012850">📲</tg-emoji>', "Nicolò Schira"),
    "pedull":  ('<tg-emoji emoji-id="5785322627044220734">📲</tg-emoji>', "Alfredo Pedullà"),
}

# Emoji usata quando il canale NON è tra quelli mappati sopra
DEFAULT_EMOJI = "📲"

# Prompt: stesse regole di formattazione del bot giornali, ma adattato ai
# video YouTube e SENZA i tag fonte (la fonte la determiniamo noi via oEmbed).
PROMPT = """Sei un estrattore di notizie calcistiche estremamente preciso. Analizza il video YouTube allegato (può essere un video normale oppure una diretta live ormai terminata) e riporta SOLO le notizie riguardanti la Juventus.

REGOLA TASSATIVA ED IMPERATIVA:
- NON USARE MAI GLI ASTERISCHI (**) per il grassetto.
- Usa SOLO ed esclusivamente i tag HTML <b> e </b> per applicare il grassetto.

Formattazione richiesta:
1. Applica il grassetto HTML usando <b> e </b> sui nomi di battesimo e cognomi dei giocatori, allenatori, dirigenti (es: <b>Damien Comolli</b>) e squadre di calcio.
2. Struttura: [NOTIZIA][Emoji] Testo continuo senza titoli...
3. Sii sintetico (max 280 caratteri a notizia).
4. Per le cifre in milioni di euro usa SEMPRE il formato compatto: 1M€, 50M€, 100M€. Mai scrivere "milioni di euro" o "mln" o "M di euro".
5. Separa ogni notizia con una riga vuota.
6. Se nel video non si parla della Juventus, non scrivere nulla.
"""


def crea_dropbox_client():
    """Crea il client Dropbox con refresh token (non scade mai)"""
    return dropbox.Dropbox(
        app_key=DROPBOX_APP_KEY,
        app_secret=DROPBOX_APP_SECRET,
        oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
    )


def get_urls_from_dropbox():
    """Legge tutti i file .txt nella cartella Dropbox ed estrae gli URL contenuti."""
    dbx = crea_dropbox_client()

    try:
        result = dbx.files_list_folder(DROPBOX_FOLDER)
    except dropbox.exceptions.ApiError as e:
        print(f"Errore accesso cartella Dropbox: {e}")
        return [], []

    txt_files = [
        f for f in result.entries
        if isinstance(f, dropbox.files.FileMetadata) and f.name.lower().endswith(".txt")
    ]

    if not txt_files:
        print("Nessun file .txt trovato su Dropbox.")
        return [], []

    print(f"Trovati {len(txt_files)} file .txt su Dropbox.")
    urls = []
    dropbox_paths = []

    for file in txt_files:
        try:
            print(f"Lettura {file.name}...")
            metadata, response = dbx.files_download(file.path_lower)
            content = response.content.decode("utf-8", errors="ignore")
            # Estrae ogni URL presente nel file (uno per riga o anche con testo intorno)
            found = re.findall(r'https?://[^\s<>"\']+', content)
            found = [u.rstrip(').,;') for u in found]
            urls.extend(found)
            dropbox_paths.append(file.path_lower)
            print(f"Trovati {len(found)} URL in {file.name}.")
        except Exception as e:
            print(f"Errore lettura {file.name}: {e}")

    # Rimuove eventuali duplicati mantenendo l'ordine
    seen = set()
    unique_urls = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique_urls.append(u)

    return unique_urls, dropbox_paths


def delete_files_from_dropbox(dropbox_paths):
    """Cancella i file txt da Dropbox dopo l'elaborazione."""
    dbx = crea_dropbox_client()
    for path in dropbox_paths:
        try:
            dbx.files_delete_v2(path)
            print(f"File {path} cancellato da Dropbox.")
        except Exception as e:
            print(f"Errore cancellazione {path}: {e}")


def is_youtube(url):
    """True se l'URL è un video YouTube."""
    u = url.lower()
    return "youtube.com" in u or "youtu.be" in u


def get_youtube_author(url):
    """Recupera nome e handle del canale YouTube tramite oEmbed (nessuna API key)."""
    try:
        r = requests.get(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=10,
        )
        if r.ok:
            data = r.json()
            return data.get("author_name", ""), data.get("author_url", "")
        print(f"oEmbed non ok ({r.status_code}) per {url}")
    except Exception as e:
        print(f"Errore oEmbed per {url}: {e}")
    return "", ""


def get_fonte(author_name, author_url):
    """Sceglie emoji + nome fonte in base al canale YouTube rilevato."""
    hay = f"{author_name} {author_url}".lower()
    for key, (emoji, nome) in CANALI.items():
        if key in hay:
            return emoji, f"{nome} - YouTube"
    # Canale non mappato: usa comunque il nome reale del canale
    if author_name.strip():
        return DEFAULT_EMOJI, f"{author_name.strip()} - YouTube"
    return DEFAULT_EMOJI, "YouTube"


def generate_news_from_url(url):
    """
    Invia l'URL a Gemini.
    - Video YouTube: Gemini "guarda" il video (anche le dirette terminate).
    - Altre pagine web: Gemini le legge con lo strumento URL context.
    """
    if is_youtube(url):
        print(f"Invio video YouTube a Gemini: {url}")
        response = client.models.generate_content(
            model="gemini-3.5-flash",
            contents=types.Content(
                parts=[
                    types.Part(file_data=types.FileData(file_uri=url)),
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

            # 1) Determina la fonte (canale YouTube) via oEmbed
            if is_youtube(link):
                author_name, author_url = get_youtube_author(link)
            else:
                author_name, author_url = "", ""
            emoji_fonte, nome_fonte = get_fonte(author_name, author_url)
            print(f"Fonte rilevata: {nome_fonte}")

            # 2) Estrae le notizie con Gemini
            try:
                raw = generate_news_from_url(link)
                if raw and raw.strip():
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
