import os
import requests
from pypdf import PdfReader
from google import genai
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# Configurazione variabili d'ambiente da GitHub Secrets
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Inizializzazione del client ufficiale Google GenAI per la serie 3.5
client = genai.Client(api_key=GEMINI_API_KEY)

def crea_sessione_robusta():
    """Crea una sessione HTTP con tentativi di ripescaggio automatico in caso di instabilità"""
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
    """Intercetta il file_id del PDF sia nei messaggi normali che negli inoltri diretti"""
    if "document" in message:
        doc = message["document"]
        if doc.get("mime_type") == "application/pdf" or doc.get("file_name", "").lower().endswith(".pdf"):
            return doc.get("file_id")
    return None

def get_pdf_from_telegram():
    """Recupera e scarica fino a 3 PDF recenti presenti nella chat di Telegram"""
    session = crea_sessione_robusta()
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?limit=100"
    
    try:
        response = session.get(url, timeout=30).json()
    except Exception as e:
        print(f"Errore di connessione a Telegram durante getUpdates: {e}")
        return []
    
    file_ids = []
    updates = response.get("result", [])
    
    # Analizza la cronologia al contrario partendo dall'ultimo messaggio inviato
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
    print(f"Trovati {len(file_ids)} file PDF negli aggiornamenti. Avvio del download...")
    
    for idx, file_id in enumerate(file_ids):
        try:
            file_info = session.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}", timeout=30).json()
            if "result" in file_info:
                file_path = file_info["result"]["file_path"]
                download_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
                
                local_filename = f"giornale_{idx}.pdf"
                print(f"Scaricamento file {idx+1}...")
                
                with session.get(download_url, stream=True, timeout=120) as r:
                    if r.status_code == 200:
                        with open(local_filename, "wb") as f:
                            for chunk in r.iter_content(chunk_size=65536):
                                if chunk:
                                    f.write(chunk)
                        
                        if os.path.exists(local_filename) and os.path.getsize(local_filename) > 0:
                            print(f"-> File {idx+1} salvato ({round(os.path.getsize(local_filename)/(1024*1024), 2)} MB).")
                            pdf_paths.append(local_filename)
                    else:
                        print(f"-> Telegram ha negato il download del file {idx+1} (Status: {r.status_code}). Supera i 20MB.")
            else:
                print(f"-> File {idx+1} ignorato dal bot: {file_info.get('description', 'Limite API o errore')}")
        except Exception as e:
            print(f"-> Errore sul file {idx+1}: {e}")
        
    return pdf_paths

def extract_text_from_pdfs(pdf_paths):
    """Estrae tutto il contenuto testuale dai PDF scaricati"""
    full_text = ""
    for path in pdf_paths:
        try:
            reader = PdfReader(path)
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    full_text += text + "\n"
        except Exception as e:
            print(f"Errore lettura testo nel file {path}: {e}")
    return full_text

def generate_news_with_gemini(text):
    """Invia il testo a Gemini 3.5 Flash imponendo la formattazione e i tag HTML richiesti"""
    prompt = """
    Analizza il testo di questi giornali ed estrai TUTTE le notizie rilevanti sulla Juventus.
    Separa nettamente ogni singola notizia inserendo la parola esatta [NOTIZIA] all'inizio di ognuna.
    
    Regole RIGIDE di formattazione del testo (Applica tassativamente ed esclusivamente questi tag HTML):
    1. Applica il GRASSETTO usando i tag <b> e </b> sui nomi di battesimo e cognomi dei giocatori (es: <b>Bernardo Silva</b>, <b>Brahim Diaz</b>), sui nomi di allenatori (es: <b>Thiago Motta</b>), sui dirigenti (es: <b>Damien Comolli</b>) e sui nomi di tutte le squadre di calcio citate (es: <b>Juventus</b>, <b>Atletico Madrid</b>). Il nome deve includere anche il nome di battesimo se presente.
    
    2. Rileva quale quotidiano riporta la notizia (TuttoSport, Gazzetta dello Sport o Corriere dello Sport) e inserisci la parola chiave della fonte corrispondente alla fine del testo della notizia usando uno di questi tre tag precisi: [FONTE_TUTTO], [FONTE_GAZZETTA] o [FONTE_CORRIERE]. Se non è chiara la fonte, usa [FONTE_DEFAULT].
    
    Struttura finale della risposta per ogni notizia:
    [NOTIZIA][EMOJI INIZIALI ADATTE] Testo breve, lineare e d'impatto senza titoli della notizia con i tag <b> applicati. [TAG_FONTE_RILEVATA]
    
    Nota fondamentale: Sii estremamente sintetico nel testo della notizia. Non inserire MAI titoli o intestazioni, l'output deve essere un flusso lineare di testo che inizia direttamente con l'emoji. Non usare asterischi (*) o trattini bassi (_).
    """
    
    response = client.models.generate_content(
        model='gemini-3.5-flash',
        contents=f"{prompt}\n\nTesto dei quotidiani:\n{text}",
    )
    return response.text

def send_to_telegram(news_list):
    """Costruisce il post inserendo le Emoji Premium e la spaziatura corretta, poi lo pubblica su Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    # Mappatura delle Custom Emoji Premium estratte dal tuo JSON strutturale
    emoji_mapping = {
        "[FONTE_TUTTO]": ('<tg-emoji id="6032834612990841221">📰</tg-emoji>', "TuttoSport"),
        "[FONTE_GAZZETTA]": ('<tg-emoji id="6032862491623559282">📰</tg-emoji>', "Gazzetta dello Sport"),
        "[FONTE_CORRIERE]": ('<tg-emoji id="6030691308346019878">📰</tg-emoji>', "Corriere dello Sport"),
        "[FONTE_DEFAULT]": ('📰', "Quotidiano")
    }
    tg_reborn_emoji = '<tg-emoji id="5985659276327132147">👉</tg-emoji>'

    for idx, news in enumerate(news_list):
        clean_news = news.strip()
        if not clean_news:
            continue
            
        # Determina la fonte inserita da Gemini e pulisce il tag di controllo
        tag_fonte = "[FONTE_DEFAULT]"
        for tag in emoji_mapping.keys():
            if tag in clean_news:
                tag_fonte = tag
                clean_news = clean_news.replace(tag, "").strip()
                break
                
        emoji_fonte, nome_fonte = emoji_mapping[tag_fonte]
        
        # Costruzione del post finale con doppi a capo per generare la spaziatura richiesta
        testo_finale = (
            f"{clean_news}\n\n"
            f"{emoji_fonte} <i>{nome_fonte}</i>\n\n"
            f"{tg_reborn_emoji} @Juventus_Reborn"
        )
        
        payload = {
            "chat_id": CHAT_ID,
            "text": testo_finale,
            "parse_mode": "HTML"  # Forziamo l'HTML per far visualizzare le Custom Emoji e i tag b/i
        }
        
        res = requests.post(url, json=payload)
        res_json = res.json()
        
        if res_json.get("ok"):
            print(f"-> Notizia {idx+1} pubblicata sul canale con stile Premium!")
        else:
            print(f"-> Errore di pubblicazione sulla notizia {idx+1}: {res_json.get('description')}")

if __name__ == "__main__":
    print("Scaricamento PDF da Telegram...")
    pdfs = get_pdf_from_telegram()
    
    if len(pdfs) == 0:
        print("Errore critico: Nessun PDF è stato scaricato (controlla le dimensioni dei file).")
    else:
        print(f"Procedo con l'estrazione da {len(pdfs)} giornali recuperati con successo.")
        print("Estrazione testo dai PDF...")
        testo_giornali = extract_text_from_pdfs(pdfs)
        
        if not testo_giornali.strip():
            print("Errore: Testo assente o non estraibile dai PDF.")
        else:
            print("Generazione notizie con Gemini 3.5 Flash...")
            notizie_raw = generate_news_with_gemini(testo_giornali)
            
            # Divisione della stringa in singole notizie basata sul marcatore impostato
            lista_notizie = notizie_raw.split("[NOTIZIA]")
            lista_notizie = [n.strip() for n in lista_notizie if n.strip()]
            
            print(f"Trovate {len(lista_notizie)} notizie elaborate. Avvio pubblicazione...")
            send_to_telegram(lista_notizie)
            print("Procedura completata con successo!")
