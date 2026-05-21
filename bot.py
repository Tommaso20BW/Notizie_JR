import os
import requests
import time
from pypdf import PdfReader
from google import genai
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# Configurazione variabili d'ambiente da GitHub Secrets
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

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

def extract_pdf_info(message):
    """Intercetta il file_id del PDF"""
    if "document" in message:
        doc = message["document"]
        if doc.get("mime_type") == "application/pdf" or doc.get("file_name", "").lower().endswith(".pdf"):
            return doc.get("file_id")
    return None

def get_pdf_from_telegram():
    """Recupera PDF e svuota la coda di Telegram tramite offset"""
    session = crea_sessione_robusta()
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?limit=100"
    
    try:
        response = session.get(url, timeout=30).json()
    except Exception as e:
        print(f"Errore di connessione a Telegram: {e}")
        return []
    
    updates = response.get("result", [])
    if not updates:
        return []
        
    file_ids = []
    highest_update_id = 0
    
    for update in reversed(updates):
        u_id = update.get("update_id")
        if u_id > highest_update_id:
            highest_update_id = u_id
            
        message = update.get("message") or update.get("channel_post")
        if not message: continue
            
        file_id = extract_pdf_info(message)
        if not file_id and "reply_to_message" in message:
            file_id = extract_pdf_info(message["reply_to_message"])
            
        if file_id and file_id not in file_ids:
            file_ids.append(file_id)
            if len(file_ids) == 3: break
                
    pdf_paths = []
    if file_ids:
        print(f"Trovati {len(file_ids)} PDF. Avvio download...")
        for idx, file_id in enumerate(file_ids):
            try:
                file_info = session.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}", timeout=30).json()
                if "result" in file_info:
                    file_path = file_info["result"]["file_path"]
                    download_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                    local_filename = f"giornale_{idx}.pdf"
                    
                    with session.get(download_url, stream=True, timeout=120) as r:
                        if r.status_code == 200:
                            with open(local_filename, "wb") as f:
                                for chunk in r.iter_content(chunk_size=65536):
                                    if chunk: f.write(chunk)
                            pdf_paths.append(local_filename)
            except Exception as e:
                print(f"Errore file {idx+1}: {e}")
                
    # Svuotamento coda
    session.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={highest_update_id + 1}&limit=1", timeout=10)
    return pdf_paths

def extract_text_from_single_pdf(path):
    text = ""
    try:
        reader = PdfReader(path)
        for page in reader.pages:
            p = page.extract_text()
            if p: text += p + "\n"
    except Exception as e:
        print(f"Errore lettura {path}: {e}")
    return text

def generate_news_with_gemini(text):
    prompt = """Sei un estrattore di notizie calcistiche. Estrai TUTTE le notizie sulla <b>Juventus</b> dal testo fornito.
    - Non inventare nulla.
    - Formato: Grassetti su giocatori, allenatori, dirigenti e squadre.
    - Fonte obbligatoria alla fine con tag: [FONTE_TUTTO], [FONTE_GAZZETTA] o [FONTE_CORRIERE].
    - Struttura: [NOTIZIA] [Emoji] Testo... [TAG_FONTE]
    - Sii sintetico (max 280 caratteri).
    """
    response = client.models.generate_content(
        model='gemini-3.5-flash',
        contents=f"{prompt}\n\nTesto:\n{text}",
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
        if not clean: continue
        tag = next((t for t in emoji_mapping if t in clean), "[FONTE_TUTTO]")
        clean = clean.replace(tag, "").strip()
        emoji_fonte, nome_fonte = emoji_mapping[tag]
        
        testo = f"{clean}\n\n{emoji_fonte} <i>{nome_fonte}</i>\n\n{tg_reborn} @Juventus_Reborn"
        requests.post(url, json={"chat_id": CHAT_ID, "text": testo, "parse_mode": "HTML"})

if __name__ == "__main__":
    pdfs = get_pdf_from_telegram()
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
            if os.path.exists(path): os.remove(path)
            if i < len(pdfs) - 1:
                time.sleep(20) # Pausa ottimizzata
        print("Operazione completata.")
