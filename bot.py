import os
import re
import requests
from pypdf import PdfReader
from openai import OpenAI

# Configurazione variabili d'ambiente
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)

def get_pdf_from_telegram():
    """Recupera gli ultimi 3 PDF inviati nella chat"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    response = requests.get(url).json()
    
    file_ids = []
    # Analizza i messaggi al contrario per prendere gli ultimi file
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
    full_text = ""
    for path in pdf_paths:
        reader = PdfReader(path)
        for page in reader.pages:
            full_text += page.extract_text() + "\n"
    return full_text

def generate_news_with_gpt(text):
    prompt = """
    Analizza il testo di questi giornali ed estrai TUTTE le notizie rilevanti sulla Juventus.
    Separa ogni notizia con il marcatore [NOTIZIA].
    Ogni notizia deve seguire RIGIDAMENTE questo stile e non superare MAI i 280 caratteri totali:

    [EMOJI INIZIALI] Testo della notizia breve e d'impatto.
    
    📰 [Nome Quotidiano Fonte, es: TuttoSport, Gazzetta dello Sport, Corriere dello Sport]
    
    👉 @Juventus_Reborn

    Nota: Sii molto sintetico per non sforare i 280 caratteri incluso il tag finale.
    """
    
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": text}
        ]
    )
    return response.choices[0].message.content

def send_to_telegram(news_list):
    for news in news_list:
        if news.strip():
            # Pulisce il marcatore
            clean_news = news.replace("[NOTIZIA]", "").strip()
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {
                "chat_id": CHAT_ID,
                "text": clean_news,
                "parse_mode": "Markdown"
            }
            requests.post(url, json=payload)

if __name__ == "__main__":
    print("Scaricamento PDF...")
    pdfs = get_pdf_from_telegram()
    if len(pdfs) < 3:
        print(f"Trovati solo {len(pdfs)} PDF. Ce ne vogliono 3.")
    else:
        print("Estrazione testo...")
        testo_giornali = extract_text_from_pdfs(pdfs)
        print("Generazione notizie con IA...")
        notizie_raw = generate_news_with_gpt(testo_giornali)
        
        # Divide le notizie generate basandosi sul marcatore
        lista_notizie = notizie_raw.split("[NOTIZIA]")
        print(f"Invio di {len(lista_notizie)-1} notizie su Telegram...")
        send_to_telegram(lista_notizie)
        print("Fatto!")
