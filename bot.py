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
    Sei un estrattore di notizie calcistiche estremamente preciso e letterale. Il tuo compito è analizzare il testo dei quotidiani forniti ed estrarre le notizie riguardanti la Juventus.

    REGOLA TASSATIVA DI FEDELTÀ: 
    - Non inventare nulla. 
    - Non fare supposizioni, non aggiungere dettagli di mercato basati sulla tua conoscenza pregressa e non ricamare sulle trattative.
    - Riporta SOLO ed esclusivamente i fatti, le cifre, i nomi e le dichiarazioni esplicitamente scritti nel testo fornito. Se il testo non contiene notizie sulla Juventus, non generare nulla.

    Regole RIGIDE di formattazione del testo (Applica tassativamente ed esclusivamente questi tag HTML):
    1. Applica il GRASSETTO usando i tag <b> e </b> sui nomi di battesimo e cognomi dei giocatori (es: <b>Bernardo Silva</b>, <b>Brahim Diaz</b>), sui nomi di allenatori (es: <b>Thiago Motta</b>), sui dirigenti (es: <b>Damien Comolli</b>) e sui nomi di tutte le squadre di calcio citate (es: <b>Juventus</b>, <b>Atletico Madrid</b>). Il nome deve includere anche il nome di battesimo se presente nel testo.
    
    2. IDENTIFICAZIONE DELLA FONTE OBBLIGATORIA: Assegna TASSATIVAMENTE ogni notizia a uno specifico quotidiano. Devi inserire la parola chiave della fonte corrispondente alla fine del testo della notizia usando uno di questi tre tag precisi: [FONTE_TUTTO], [FONTE_GAZZETTA] o [FONTE_CORRIERE]. Non lasciare mai notizie senza uno di questi tre tag specifici. Se una notizia è riportata su più giornali, assegnala a quello che fornisce più dettagli o al primo che incontra.
    
    Struttura finale della risposta per ogni notizia:
    [NOTIZIA][EMOJI INIZIALI ADATTE] Testo breve, lineare, fedele e d'impatto senza alcun titolo o intestazione. Il testo deve iniziare direttamente con l'emoji e contenere i tag <b> applicati. [TAG_FONTE_RILEVATA]
    
    Nota fondamentale: Sii estremamente sintetico nel testo della notizia per rimanere comodamente nei 280 caratteri. L'output deve essere un flusso continuo diviso solo dal marcatore [NOTIZIA]. Non usare asterischi (*) o trattini bassi (_).
    """
    
    response = client.models.generate_content(
        model='gemini-3.5-flash',
        contents=f"{prompt}\n\nTesto dei quotidiani:\n{text}",
    )
    return response.text

def send_to_telegram(news_list):
    """Costruisce il post inserendo le emoji Premium con attributo emoji-id e la spaziatura corretta"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    # Mappatura corretta con l'attributo esatto emoji-id richiesto dalle API di Telegram
    emoji_mapping = {
        "[FONTE_TUTTO]": ('<tg-emoji emoji-id="6032834612990841221">📰</tg-emoji>', "TuttoSport"),
        "[FONTE_GAZZETTA]": ('<tg-emoji emoji-id="6032862491623559282">📰</tg-emoji>', "Gazzetta dello Sport"),
        "[FONTE_CORRIERE]": ('<tg-emoji emoji-id="6030691308346019878">📰</tg-emoji>', "Corriere dello Sport")
    }
    tg_reborn_emoji = '<tg-emoji emoji-id="5985659276327132147">👉</tg-emoji>'

    for idx, news in enumerate(news_list):
        clean_news = news.strip()
        if not clean_news:
            continue
            
        # Determina la fonte inserita da Gemini e pulisce il tag di controllo
        tag_fonte = None
        for tag in emoji_mapping.keys():
            if tag in clean_news:
                tag_fonte = tag
                clean_news = clean_news.replace(tag, "").strip()
                break
                
        # Se per un'anomalia Gemini non ha inserito il tag, fa un fallback sicuro su TuttoSport
        if not tag_fonte:
            tag_fonte = "[FONTE_TUTTO]"
                
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
            "parse_mode": "HTML"
        }
        
        res = requests.post(url, json=payload)
        res_json = res.json()
        
        if res_json.get("ok"):
            print(f"-> Notizia {idx+1} pubblicata con stile Premium!")
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
