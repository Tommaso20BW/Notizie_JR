import os
import requests
from pypdf import PdfReader
from google import genai
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# Configurazione variabili d'ambiente
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)

def crea_sessione_robusta():
    """Crea una sessione HTTP che riprova automaticamente in caso di rallentamenti"""
    session = requests.Session()
    retry = Retry(
        total=5,  # Riprova fino a 5 volte
        backoff_factor=1,  # Aspetta un po' di più tra un tentativo e l'altro
        status_forcelist=[500, 502, 503, 504]
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

def extract_pdf_info(message):
    if "document" in message:
        doc = message["document"]
        if doc.get("mime_type") == "application/pdf" or doc.get("file_name", "").lower().endswith(".pdf"):
            return doc.get("file_id")
    return None

def get_pdf_from_telegram():
    session = crea_sessione_robusta()
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?limit=100"
    
    try:
        response = session.get(url, timeout=30).json()
    except Exception as e:
        print(f"Errore di connessione a Telegram durante getUpdates: {e}")
        return []
    
    file_ids = []
    updates = response.get("result", [])
    
    for update in reversed(updates):
        message = update.get("message") or update.get("channel_post")
        if not message:
            continue
            
        file_id = extract_pdf_info(message)
        if not file_id and "reply_to_message" in message:
            file_id = extract_pdf_info(message["reply_to_message"])
            
        if file_id and file_id not in file_ids:
            file_ids.append(file_id)
            if len(file_ids) == 3:
                break
                
    if not file_ids:
        return []
        
    pdf_paths = []
    print(f"Trovati {len(file_ids)} file PDF validi negli aggiornamenti. Inizio il download pesante...")
    
    for idx, file_id in enumerate(file_ids):
        try:
            # Chiamata per ottenere il percorso del file su Telegram
            file_info = session.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}", timeout=30).json()
            if "result" in file_info:
                file_path = file_info["result"]["file_path"]
                download_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                
                local_filename = f"giornale_{idx}.pdf"
                print(f"Scaricamento file {idx+1}/3...")
                
                # Download a blocchi con timeout esteso a 120 secondi per i file grandi
                with session.get(download_url, stream=True, timeout=120) as r:
                    r.raise_for_status()
                    with open(local_filename, "wb") as f:
                        for chunk in r.iter_content(chunk_size=65536): # Blocco più grande per velocizzare
                            if chunk:
                                f.write(chunk)
                                
                # Verifica che il file sia stato effettivamente salvato e non sia vuoto
                if os.path.exists(local_filename) and os.path.getsize(local_filename) > 0:
                    print(f"File {idx+1}/3 completato con successo ({round(os.path.getsize(local_filename)/(1024*1024), 2)} MB).")
                    pdf_paths.append(local_filename)
                else:
                    print(f"File {idx+1}/3 scaricato ma risulta corrotto o vuoto.")
        except Exception as e:
            print(f"Errore critico durante il download del file {idx+1}: {e}")
        
    return pdf_paths

def extract_text_from_pdfs(pdf_paths):
    full_text = ""
    for path in pdf_paths:
        try:
            reader = PdfReader(path)
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    full_text += text + "\n"
        except Exception as e:
            print(f"Errore nella lettura del file {path}: {e}")
    return full_text

def generate_news_with_gemini(text):
    prompt = """
    Analizza il testo di questi giornali ed estrai TUTTE le notizie rilevanti sulla Juventus.
    Separa nettamente ogni singola notizia inserendo la parola esatta [NOTIZIA] prima di ognuna.
    Ogni notizia deve seguire RIGIDAMENTE questo stile e non superare MAI i 280 caratteri totali:

    [EMOJI INIZIALI] Testo della notizia breve e d'impatto.
    
    📰 [Nome Quotidiano Fonte, es: TuttoSport, Gazzetta dello Sport, Corriere dello Sport]
    
    👉 @Juventus_Reborn

    Nota fondamentale: Sii estremamente sintetico nel testo della notizia per non sforare MAI i 280 caratteri complessivi (inclusi i tag e la fonte). Non inventare notizie non presenti nel testo.
    """
    
    response = client.models.generate_content(
        model='gemini-3.5-flash',
        contents=f"{prompt}\n\nTesto dei giornali:\n{text}",
    )
    return response.text

def send_to_telegram(news_list):
    for news in news_list:
        clean_news = news.strip()
        if clean_news:
            clean_news = clean_news.replace("[NOTIZIA]", "").strip()
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {
                "chat_id": CHAT_ID,
                "text": clean_news,
                "parse_mode": "Markdown"
            }
            requests.post(url, json=payload)

if __name__ == "__main__":
    print("Scaricamento PDF da Telegram...")
    pdfs = get_pdf_from_telegram()
    if len(pdfs) < 3:
        print(f"Errore: Trovati solo {len(pdfs)} PDF validi su 3 richiesti.")
        print("I server di Telegram o la rete di GitHub hanno rallentato il download dei file pesanti.")
        print("👉 SOLUZIONE: Rilancia il workflow su GitHub adesso; la nuova sessione riproverà in automatico.")
    else:
        print("Estrazione testo dai PDF...")
        testo_giornali = extract_text_from_pdfs(pdfs)
        
        if not testo_giornali.strip():
            print("Errore: Impossibile estrarre testo dai PDF.")
        else:
            print("Generazione notizie con Gemini 3.5 Flash...")
            notizie_raw = generate_news_with_gemini(testo_giornali)
            lista_notizie = notizie_raw.split("[NOTIZIA]")
            print(f"Invio dei post su Telegram...")
            send_to_telegram(lista_notizie)
            print("Procedura completata con successo!")
