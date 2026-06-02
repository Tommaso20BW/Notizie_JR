import os
import json
import requests
import time
from pypdf import PdfReader
from google import genai
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2.service_account import Credentials
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import io

# Configurazione variabili d'ambiente da GitHub Secrets
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID")
GDRIVE_CREDENTIALS = os.getenv("GDRIVE_CREDENTIALS")

# Inizializzazione del client ufficiale Google GenAI
client = genai.Client(api_key=GEMINI_API_KEY)


def crea_sessione_robusta():
    """Crea una sessione HTTP con tentativi di ripescaggio automatico"""
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


def crea_drive_service():
    """Crea il client Google Drive tramite Service Account"""
    creds_dict = json.loads(GDRIVE_CREDENTIALS)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)


def get_pdf_from_drive():
    """Scarica tutti i PDF presenti nella cartella Google Drive"""
    service = crea_drive_service()

    # Cerca tutti i PDF nella cartella
    query = f"'{GDRIVE_FOLDER_ID}' in parents and mimeType='application/pdf' and trashed=false"
    results = service.files().list(
        q=query,
        fields="files(id, name)",
        pageSize=10
    ).execute()

    files = results.get("files", [])
    if not files:
        print("Nessun PDF trovato su Google Drive.")
        return [], []

    print(f"Trovati {len(files)} PDF su Google Drive.")
    pdf_paths = []
    file_ids = []

    for idx, file in enumerate(files):
        file_id = file["id"]
        file_name = file["name"]
        local_filename = f"giornale_{idx}.pdf"

        try:
            request = service.files().get_media(fileId=file_id)
            with io.FileIO(local_filename, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request, chunksize=1024*1024)
                done = False
                while not done:
                    status, done = downloader.next_chunk()
                    print(f"Download {file_name}: {int(status.progress() * 100)}%")

            pdf_paths.append(local_filename)
            file_ids.append(file_id)
            print(f"Scaricato: {file_name}")

        except Exception as e:
            print(f"Errore download {file_name}: {e}")

    return pdf_paths, file_ids


def delete_files_from_drive(file_ids):
    """Sposta i PDF nel cestino di Google Drive dopo l'elaborazione"""
    service = crea_drive_service()
    for file_id in file_ids:
        try:
            service.files().update(fileId=file_id, body={"trashed": True}).execute()
            print(f"File {file_id} spostato nel cestino.")
        except Exception as e:
            print(f"Errore cancellazione file {file_id}: {e}")


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
    """
    response = client.models.generate_content(
        model='gemini-3.5-flash',
        contents=f"{prompt}\n\nTesto del quotidiano:\n{text}",
    )
    return response.text


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

        # Rimuove forzatamente gli asterischi se presenti
        clean = clean.replace("**", "")

        tag = next((t for t in emoji_mapping if t in clean), "[FONTE_TUTTO]")
        clean = clean.replace(tag, "").strip()

        emoji_fonte, nome_fonte = emoji_mapping[tag]

        testo = f"{clean}\n\n{emoji_fonte} <i>{nome_fonte}</i>\n\n{tg_reborn} @Juventus_Reborn"

        requests.post(url, json={"chat_id": CHAT_ID, "text": testo, "parse_mode": "HTML"})


if __name__ == "__main__":
    pdfs, drive_file_ids = get_pdf_from_drive()

    if len(pdfs) == 0:
        print("Nessun PDF nuovo. Chiusura.")
    else:
        for i, path in enumerate(pdfs):
            print(f"Elaborazione {path}...")
            testo = extract_text_from_single_pdf(path)
            if testo.strip():
                try:
                    raw = generate_news_with_gemini(testo)
                    lista = [n.strip() for n in raw.split("[NOTIZIA]") if n.strip()]
                    send_to_telegram(lista)
                except Exception as e:
                    print(f"Errore Gemini: {e}")

            # Rimuove il file locale
            if os.path.exists(path):
                os.remove(path)

            if i < len(pdfs) - 1:
                print("In attesa di 20 secondi prima del prossimo giornale...")
                time.sleep(20)

        # Cancella tutti i PDF da Google Drive
        print("Cancellazione PDF da Google Drive...")
        delete_files_from_drive(drive_file_ids)

        print("Operazione completata.")
