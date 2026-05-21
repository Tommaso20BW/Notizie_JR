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
    session = requests.Session()
    retry = Retry(total=5, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
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
                
    pdf_paths = []
    print(f"Trovati {len(file_ids)} file PDF negli aggiornamenti. Provo a scaricarli...")
    
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
                        
                        if os.path.exists(local_filename) and os.path.getsize(local_filename) > 0:
                            pdf_paths.append(local_filename)
        except Exception as e:
            print(f"-> Errore download file {idx+1}: {e}")
        
    return pdf_paths

def extract_text_from_pdfs(pdf_paths):
    full_text = ""
    for path in pdf_paths:
        try:
            reader = PdfReader(path)
            for page in reader.pages:
                text = page.extract_text()
                if text: full_text += text + "\n"
        except Exception as e:
            print(f"Errore lettura file {path}: {e}")
    return full_text

def generate_news_with_gemini(text):
    prompt = """
    Analizza il testo di questi giornali ed estrai TUTTE le notizie rilevanti sulla Juventus.
    Scrivi le notizie una di seguito all'altra.
    IMPORTANTE: Inizia OGNI singola notizia tassativamente ed esattamente con la parola chiave [NOTIZIA].
    
    Ogni notizia deve seguire RIGIDAMENTE questo stile e non superare MAI i 280 caratteri totali:

    [NOTIZIA] [EMOJI INIZIALI] Testo della notizia breve e d'impatto.
    📰 [Nome Quotidiano Fonte, es: TuttoSport, Gazzetta dello Sport, Corriere dello Sport]
    👉 @Juventus_Reborn

    Nota fondamentale: Sii estremamente sintetico nel testo per non sforare i 280 caratteri. Non usare grassetti o corsivi strani (niente asterischi o trattini bassi).
    """
    
    response = client.models.generate_content(
        model='gemini-3.5-flash',
        contents=f"{prompt}\n\nTesto dei giornali:\n{text}",
    )
    return response.text

def send_to_telegram(news_list):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    inviati = 0
    
    for idx, news in enumerate(news_list):
        clean_news = news.strip()
        if not clean_news:
            continue
            
        print(f"Provando a inviare la notizia {idx}...")
        payload = {
            "chat_id": CHAT_ID,
            "text": clean_news
        }
        
        # Invio standard senza parse_mode per evitare blocchi dovuti a caratteri speciali
        res = requests.post(url, json=payload)
        res_json = res.json()
        
        if res_json.get("ok"):
            print(f"-> Notizia {idx} inviata con successo!")
            inviati += 1
        else:
            print(f"-> Errore di invio sulla notizia {idx}: {res_json.get('description')}")
            
    print(f"Totale messaggi recapitati sul canale: {inviati}")

if __name__ == "__main__":
    print("Scaricamento PDF da Telegram...")
    pdfs = get_pdf_from_telegram()
    
    if len(pdfs) == 0:
        print("Errore critico: Nessun PDF scaricato.")
    else:
        print(f"Procedo con {len(pdfs)} giornali.")
        testo_giornali = extract_text_from_pdfs(pdfs)
        
        if not testo_giornali.strip():
            print("Errore: Testo assente nei PDF.")
        else:
            print("Generazione notizie con Gemini 3.5 Flash...")
            notizie_raw = generate_news_with_gemini(testo_giornali)
            
            # Splittiamo sulla parola chiave passata da Gemini
            lista_notizie = notizie_raw.split("[NOTIZIA]")
            
            # Rimuoviamo elementi vuoti dalla lista
            lista_notizie = [n.strip() for n in lista_notizie if n.strip()]
            
            print(f"Trovate {len(lista_notizie)} notizie elaborate. Avvio l'invio...")
            send_to_telegram(lista_notizie)
            print("Procedura completata!")
