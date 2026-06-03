import os
import requests
import time
import dropbox
from dropbox.oauth import DropboxOAuth2FlowNoRedirect
from pypdf import PdfReader
from google import genai
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

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


def crea_dropbox_client():
    """Crea il client Dropbox con refresh token (non scade mai)"""
    return dropbox.Dropbox(
        app_key=DROPBOX_APP_KEY,
        app_secret=DROPBOX_APP_SECRET,
        oauth2_refresh_token=DROPBOX_REFRESH_TOKEN
    )


def crea_sessione_robusta():
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[500, 502, 503, 504]
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def get_pdf_from_dropbox():
    """Scarica tutti i PDF presenti nella cartella Dropbox"""
    dbx = crea_dropbox_client()

    try:
        result = dbx.files_list_folder(DROPBOX_FOLDER)
    except dropbox.exceptions.ApiError as e:
        print(f"Errore accesso cartella Dropbox: {e}")
        return [], []

    pdf_files = [
        f for f in result.entries
        if isinstance(f, dropbox.files.FileMetadata) and f.name.lower().endswith(".pdf")
    ]

    if not pdf_files:
        print("Nessun PDF trovato su Dropbox.")
        return [], []

    print(f"Trovati {len(pdf_files)} PDF su Dropbox.")
    pdf_paths = []
    dropbox_paths = []

    for idx, file in enumerate(pdf_files):
        local_filename = f"giornale_{idx}.pdf"
        try:
            print(f"Download {file.name}...")
            metadata, response = dbx.files_download(file.path_lower)
            with open(local_filename, "wb") as f:
                f.write(response.content)
            pdf_paths.append(local_filename)
            dropbox_paths.append(file.path_lower)
            print(f"Scaricato: {file.name}")
        except Exception as e:
            print(f"Errore download {file.name}: {e}")

    return pdf_paths, dropbox_paths


def delete_files_from_dropbox(dropbox_paths):
    """Cancella i PDF da Dropbox dopo l'elaborazione"""
    dbx = crea_dropbox_client()
    for path in dropbox_paths:
        try:
            dbx.files_delete_v2(path)
            print(f"File {path} cancellato da Dropbox.")
        except Exception as e:
            print(f"Errore cancellazione {path}: {e}")


def extract_text_from_single_pdf(path):
    text = ""
    try:
        reader = PdfReader(path)
        for page in reader.pages:
            p = page.extract_text()
            if p:
                text += p + "\n"
    except Exception as e:
        print(f"Errore lettura {path}: {e}")
    return text


def generate_news_with_gemini(text):
    prompt = """Sei un estrattore di notizie calcistiche estremamente preciso. Il tuo compito è analizzare il testo e riportare SOLO le notizie riguardanti la Juventus.
    
    REGOLA TASSATIVA ED IMPERATIVI: 
    - NON USARE MAI GLI ASTERISCHI (**) per il grassetto.
    - Usa SOLO ed esclusivamente i tag HTML <b> e </b> per applicare il grassetto.
    
    Formattazione richiesta:
    1. Applica il grassetto HTML usando <b> e </b> sui nomi di battesimo e cognomi dei giocatori, allenatori, dirigenti (es: <b>Damien Comolli</b>) e squadre di calcio.
    2. Inserisci tassativamente uno di questi tre tag alla fine di ogni notizia per indicare la fonte: [FONTE_TUTTO], [FONTE_GAZZETTA] o [FONTE_CORRIERE].
    3. Struttura: [NOTIZIA][Emoji] Testo continuo senza titoli... [TAG_FONTE]
    4. Sii sintetico (max 280 caratteri a notizia).
    5. Per le cifre in milioni di euro usa SEMPRE il formato compatto: 1M€, 50M€, 100M€. Mai scrivere "milioni di euro" o "mln" o "M di euro".
    6. Separa ogni notizia con una riga vuota.
    """
    response = client.models.generate_content(
        model='gemini-3.5-flash',
        contents=f"{prompt}\n\nTesto del quotidiano:\n{text}",
    )
    return response.text


def split_notizie(raw):
    """
    Divide il testo di Gemini in singole notizie.
    Prova prima con [NOTIZIA], poi con doppio newline (paragrafi).
    """
    # Caso 1: Gemini ha usato il tag [NOTIZIA]
    if "[NOTIZIA]" in raw:
        lista = [n.strip() for n in raw.split("[NOTIZIA]") if n.strip()]
    else:
        # Caso 2: Gemini ha separato le notizie con righe vuote
        lista = [n.strip() for n in raw.split("\n\n") if n.strip()]

    return lista


def send_to_telegram(news_list):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    emoji_mapping = {
        "[FONTE_TUTTO]": ('<tg-emoji emoji-id="6032834612990841221">📰</tg-emoji>', "TuttoSport"),
        "[FONTE_GAZZETTA]": ('<tg-emoji emoji-id="6032862491623559282">📰</tg-emoji>', "Gazzetta dello Sport"),
        "[FONTE_CORRIERE]": ('<tg-emoji emoji-id="6030691308346019878">📰</tg-emoji>', "Corriere dello Sport")
    }
    tg_reborn = '<tg-emoji emoji-id="5985659276327132147">👉</tg-emoji>'

    for news in news_list:
        clean = news.strip()
        if not clean:
            continue

        # Rimuove eventuali asterischi residui
        clean = clean.replace("**", "")

        # Individua il tag fonte, default TuttoSport
        tag = next((t for t in emoji_mapping if t in clean), "[FONTE_TUTTO]")
        clean = clean.replace(tag, "").strip()

        emoji_fonte, nome_fonte = emoji_mapping[tag]

        testo = f"{clean}\n\n{emoji_fonte} <i>{nome_fonte}</i>\n\n{tg_reborn} @Juventus_Reborn"

        try:
            resp = requests.post(
                url,
                json={"chat_id": CHAT_ID, "text": testo, "parse_mode": "HTML"},
                timeout=10
            )
            if not resp.ok:
                print(f"Errore Telegram: {resp.status_code} - {resp.text}")
        except Exception as e:
            print(f"Errore invio Telegram: {e}")

        # Piccola pausa tra un messaggio e l'altro per evitare rate limit
        time.sleep(1)


if __name__ == "__main__":
    pdfs, dropbox_paths = get_pdf_from_dropbox()

    if len(pdfs) == 0:
        print("Nessun PDF nuovo. Chiusura.")
    else:
        for i, path in enumerate(pdfs):
            print(f"Elaborazione {path}...")
            testo = extract_text_from_single_pdf(path)
            if testo.strip():
                try:
                    raw = generate_news_with_gemini(testo)
                    lista = split_notizie(raw)
                    print(f"Notizie trovate: {len(lista)}")
                    send_to_telegram(lista)
                except Exception as e:
                    print(f"Errore Gemini: {e}")

            if os.path.exists(path):
                os.remove(path)

            if i < len(pdfs) - 1:
                print("In attesa di 20 secondi prima del prossimo giornale...")
                time.sleep(20)

        print("Cancellazione PDF da Dropbox...")
        delete_files_from_dropbox(dropbox_paths)

        print("Operazione completata.")
