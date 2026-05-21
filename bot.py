import os
import requests
from pypdf import PdfReader
from google import genai

# Configurazione variabili d'ambiente
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)

def extract_pdf_info(message):
    """Estrae il file_id se il messaggio contiene un PDF (anche se inoltrato o con didascalia)"""
    if "document" in message:
        doc = message["document"]
        if doc.get("mime_type") == "application/pdf" or doc.get("file_name", "").lower().endswith(".pdf"):
            return doc.get("file_id")
    return None

def get_pdf_from_telegram():
    """Recupera gli ultimi 3 PDF inviati o inoltrati nella chat"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?limit=100"
    try:
        response = requests.get(url).json()
    except Exception as e:
        print(f"Errore di connessione a Telegram: {e}")
        return []
    
    file_ids = []
    updates = response.get("result", [])
    
    # Analizziamo gli aggiornamenti al contrario (dai più recenti)
    for update in reversed(updates):
        message = update.get("message") or update.get("channel_post")
        if not message:
            continue
            
        # Controllo se il file è presente nel messaggio (gestisce anche gli inoltrati)
        file_id = extract_pdf_info(message)
        
        # Fallback nel caso in cui le API strutturino l'inoltro in un sotto-blocco
        if not file_id and "reply_to_message" in message:
            file_id = extract_pdf_info(message["reply_to_message"])
            
        if file_id and file_id not in file_ids:
            file_ids.append(file_id)
            if len(file_ids) == 3:
                break
                
    if not file_ids:
        return []
        
    pdf_paths = []
    print(f"Trovati {len(file_ids)} file PDF validi negli aggiornamenti. Inizio il download...")
    
    for idx, file_id in enumerate(file_ids):
        try:
            file_info = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}", timeout=30).json()
            if "result" in file_info:
                file_path = file_info["result"]["file_path"]
                download_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                
                local_filename = f"giornale_{idx}.pdf"
                print(f"Scaricamento file {idx+1}/3 (ID: {file_id[:10]}...)...")
                with requests.get(download_url, stream=True, timeout=60) as r:
                    with open(local_filename, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                pdf_paths.append(local_filename)
        except Exception as e:
            print(f"Errore durante il download del file {idx+1}: {e}")
        
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

    Nota fondamentale: Sii estremamente sintetico nel testo della notizia per non accedere MAI ai 280 caratteri complessivi (inclusi i tag e la fonte). Non inventare notizie non presenti nel testo.
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
            # Rimuove eventuali residui del marcatore
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
        print(f"Errore: Trovati solo {len(pdfs)} PDF validi.")
        print("👉 Assicurati che il bot sia Amministratore del gruppo e abbia il permesso di leggere i messaggi.")
        print("👉 Prova a fare un nuovo inoltro dei 3 quotidiani e lancia subito l'azione su GitHub.")
    else:
        print("Estrazione testo dai PDF...")
        testo_giornali = extract_text_from_pdfs(pdfs)
        
        if not testo_giornali.strip():
            print("Errore: Impossibile estrarre testo dai PDF (forse sono solo immagini scansionate senza OCR).")
        else:
            print("Generazione notizie con Gemini 3.5 Flash...")
            notizie_raw = generate_news_with_gemini(testo_giornali)
            lista_notizie = notizie_raw.split("[NOTIZIA]")
            print(f"Invio dei post su Telegram...")
            send_to_telegram(lista_notizie)
            print("Procedura completata con successo!")
