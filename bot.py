import os
import requests
from pypdf import PdfReader
from google import genai

# Configurazione variabili d'ambiente
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Inizializza il client con la libreria ufficiale Google GenAI
client = genai.Client(api_key=GEMINI_API_KEY)

def get_pdf_from_telegram():
    """Recupera gli ultimi 3 PDF inviati nella chat"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    response = requests.get(url).json()
    
    file_ids = []
    for update in reversed(response.get("result", [])):
        message = update.get("message", {})
        if "document" in message and message["document"]["mime_type"] == "application/pdf":
            file_ids.append(message["document"]["file_id"])
            if len(file_ids) == 3:
                break
                
    pdf_paths = []
    for idx, file_id in enumerate(file_ids):
        file_info = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}").json()
        file_path = file_info["result"]["file_path"]
        download_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        
        local_filename = f"giornale_{idx}.pdf"
        with requests.get(download_url, stream=True) as r:
            with open(local_filename, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        pdf_paths.append(local_filename)
        
    return pdf_paths

def extract_text_from_pdfs(pdf_paths):
    """Estrae il testo dai PDF scaricati"""
    full_text = ""
    for path in pdf_paths:
        reader = PdfReader(path)
        for page in reader.pages:
            full_text += page.extract_text() + "\n"
    return full_text

def generate_news_with_gemini(text):
    """Invia il testo a Gemini 3.5 Flash e riceve le notizie formattate"""
    prompt = """
    Analizza il testo di questi giornali ed estrai TUTTE le notizie rilevanti sulla Juventus.
    Separa nettamente ogni singola notizia inserendo la parola esatta [NOTIZIA] prima di ognuna.
    Ogni notizia deve seguire RIGIDAMENTE questo stile e non superare MAI i 280 caratteri totali:

    [EMOJI INIZIALI] Testo della notizia breve e d'impatto.
    
    📰 [Nome Quotidiano Fonte, es: TuttoSport, Gazzetta dello Sport, Corriere dello Sport]
    
    👉 @Juventus_Reborn

    Nota fondamentale: Sii estremamente sintetico nel testo della notizia per non sforare MAI i 280 caratteri complessivi (inclusi i tag e la fonte). Non inventare notizie non presenti nel testo.
    """
    
    # Utilizzo mirato del modello gemini-3.5-flash
    response = client.models.generate_content(
        model='gemini-3.5-flash',
        contents=f"{prompt}\n\nTesto dei giornali:\n{text}",
    )
    return response.text

def send_to_telegram(news_list):
    """Invia ogni singola notizia sul canale Telegram"""
    for news in news_list:
        clean_news = news.strip()
        if clean_news:
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
        print(f"Trovati solo {len(pdfs)} PDF. Ce ne vogliono 3 per procedere.")
    else:
        print("Estrazione testo dai PDF...")
        testo_giornali = extract_text_from_pdfs(pdfs)
        
        print("Generazione notizie con Gemini 3.5 Flash...")
        notizie_raw = generate_news_with_gemini(testo_giornali)
        
        lista_notizie = notizie_raw.split("[NOTIZIA]")
        
        print(f"Invio dei post su Telegram...")
        send_to_telegram(lista_notizie)
        print("Procedura completata con successo!")
