import os
import requests
import time
import dropbox
from google import genai

# Variabili d'ambiente da GitHub Secrets
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")

DROPBOX_URLS_FILE = "/NotizieJR/youtube_urls.txt"

client = genai.Client(api_key=GEMINI_API_KEY)


def crea_dropbox_client():
    return dropbox.Dropbox(
        app_key=DROPBOX_APP_KEY,
        app_secret=DROPBOX_APP_SECRET,
        oauth2_refresh_token=DROPBOX_REFRESH_TOKEN
    )


def get_urls_from_dropbox():
    dbx = crea_dropbox_client()
    try:
        metadata, response = dbx.files_download(DROPBOX_URLS_FILE)
        content = response.content.decode("utf-8")
        urls = [line.strip() for line in content.splitlines() if line.strip()]
        print(f"Trovati {len(urls)} URL da elaborare.")
        return urls
    except dropbox.exceptions.ApiError as e:
        print(f"Nessun file URL trovato su Dropbox (o errore): {e}")
        return []


def delete_urls_file_from_dropbox():
    dbx = crea_dropbox_client()
    try:
        dbx.files_delete_v2(DROPBOX_URLS_FILE)
        print(f"File {DROPBOX_URLS_FILE} cancellato da Dropbox.")
    except Exception as e:
        print(f"Errore cancellazione file: {e}")


def generate_news_from_youtube(url):
    prompt = (
        "Guarda questo video e dimmi cosa viene detto: " + url + "\n\n"
        "Sei un estrattore di notizie calcistiche estremamente preciso. "
        "Riporta SOLO le notizie riguardanti la Juventus presenti nel video.\n\n"
        "REGOLE TASSATIVE:\n"
        "- NON USARE MAI GLI ASTERISCHI (**) per il grassetto.\n"
        "- Usa SOLO i tag HTML <b> e </b> per il grassetto.\n"
        "- Se nel video non ci sono notizie sulla Juventus, rispondi SOLO con: NESSUNA_NOTIZIA\n\n"
        "Formattazione:\n"
        "1. Metti in grassetto con <b></b> i nomi di giocatori, allenatori, dirigenti e squadre.\n"
        "2. Alla fine di OGNI notizia metti il tag del giornalista che parla nel video, scegliendo tra:\n"
        "   [FONTE_ROMEO_AGRESTI], [FONTE_MATTEO_MORETTO], [FONTE_FABRIZIO_ROMANO],\n"
        "   [FONTE_NICOLO_SCHIRA], [FONTE_ALFREDO_PEDULLA], [FONTE_ALTRO]\n"
        "3. Struttura obbligatoria: [NOTIZIA][Emoji] Testo... [FONTE_X]\n"
        "4. Max 280 caratteri a notizia.\n"
        "5. Cifre in milioni: 1M euro, 50M euro, 100M euro.\n"
        "6. Ogni notizia inizia con [NOTIZIA] + emoji pertinente."
    )

    for tentativo in range(3):
        try:
            response = client.models.generate_content(
                model="gemini-3.5-flash",
                contents=prompt
            )
            return response.text
        except Exception as e:
            print(f"Errore Gemini tentativo {tentativo+1}/3 per {url}: {e}")
            if tentativo < 2:
                print("Riprovo tra 30 secondi...")
                time.sleep(30)
    return None


# Mapping fonte -> (custom_emoji_id, nome visualizzato)
FONTE_MAPPING = {
    "[FONTE_ROMEO_AGRESTI]":   ("5784902446098685755", "Romeo Agresti - YouTube"),
    "[FONTE_MATTEO_MORETTO]":  ("5785259727248170398", "Matteo Moretto - YouTube"),
    "[FONTE_FABRIZIO_ROMANO]": ("5785366354106261925", "Fabrizio Romano - YouTube"),
    "[FONTE_NICOLO_SCHIRA]":   ("5785305056333012850", "Nicolo Schira - YouTube"),
    "[FONTE_ALFREDO_PEDULLA]": ("5785322627044220734", "Alfredo Pedulla - YouTube"),
    "[FONTE_ALTRO]":           ("5784902446098685755", "YouTube"),
}


def send_to_telegram(news_list):
    url_api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    tg_reborn = '<tg-emoji emoji-id="5985659276327132147">👉</tg-emoji>'

    for news in news_list:
        clean = news.strip()
        if not clean:
            continue

        clean = clean.replace("**", "")

        tag_trovato = "[FONTE_ALTRO]"
        for tag in FONTE_MAPPING:
            if tag in clean:
                tag_trovato = tag
                break

        emoji_id, nome_fonte = FONTE_MAPPING[tag_trovato]
        emoji_fonte = f'<tg-emoji emoji-id="{emoji_id}">📲</tg-emoji>'

        clean = clean.replace(tag_trovato, "").strip()

        testo = f"{clean}\n\n{emoji_fonte} <i>{nome_fonte}</i>\n\n{tg_reborn} @Juventus_Reborn"

        try:
            requests.post(
                url_api,
                json={"chat_id": CHAT_ID, "text": testo, "parse_mode": "HTML"},
                timeout=10
            )
            time.sleep(1)
        except Exception as e:
            print(f"Errore invio Telegram: {e}")


if __name__ == "__main__":
    urls = get_urls_from_dropbox()

    if not urls:
        print("Nessun URL da elaborare. Chiusura.")
    else:
        for i, url in enumerate(urls):
            print(f"\nElaborazione URL {i+1}/{len(urls)}: {url}")

            raw = generate_news_from_youtube(url)

            if not raw:
                print("Nessuna risposta da Gemini, salto.")
                continue

            if "NESSUNA_NOTIZIA" in raw:
                print("Nessuna notizia Juventus trovata nel video.")
                continue

            lista = [n.strip() for n in raw.split("[NOTIZIA]") if n.strip()]
            print(f"Notizie trovate: {len(lista)}")
            send_to_telegram(lista)

            if i < len(urls) - 1:
                print("Attesa 15 secondi prima del prossimo video...")
                time.sleep(15)

        print("\nCancellazione file URL da Dropbox...")
        delete_urls_file_from_dropbox()

        print("Operazione completata.")
