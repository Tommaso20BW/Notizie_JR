import os
import re
import requests
import time
import dropbox
from google import genai

# Configurazione variabili d'ambiente da GitHub Secrets
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
DROPBOX_FOLDER = "/NotizieJR"

# Inizializzazione del client ufficiale Google GenAI
client = genai.Client(api_key=GEMINI_API_KEY)


def crea_dropbox_client():
    """Crea il client Dropbox con refresh token (non scade mai)"""
    return dropbox.Dropbox(
        app_key=DROPBOX_APP_KEY,
        app_secret=DROPBOX_APP_SECRET,
        oauth2_refresh_token=DROPBOX_REFRESH_TOKEN
    )


def get_pdf_from_dropbox():
    """Scarica tutti i PDF presenti nella cartella Dropbox"""
    dbx = crea_dropbox_client()

    try:
        result = dbx.files_list_folder(DROPBOX_FOLDER)
    except dropbox.exceptions.ApiError as e:
        print(f"Errore accesso cartella Dropbox: {e}")
        return [], []

    pdf_files = [
        f for f in result.entries
        if isinstance(f, dropbox.files.FileMetadata) and f.name.lower().endswith(".pdf")
    ]

    if not pdf_files:
        print("Nessun PDF trovato su Dropbox.")
        return [], []

    print(f"Trovati {len(pdf_files)} PDF su Dropbox.")
    pdf_paths = []
    dropbox_paths = []

    for idx, file in enumerate(pdf_files):
        local_filename = f"giornale_{idx}.pdf"
        try:
            print(f"Download {file.name}...")
            metadata, response = dbx.files_download(file.path_lower)
            with open(local_filename, "wb") as f:
                f.write(response.content)
            pdf_paths.append(local_filename)
            dropbox_paths.append(file.path_lower)
            print(f"Scaricato: {file.name}")
        except Exception as e:
            print(f"Errore download {file.name}: {e}")

    return pdf_paths, dropbox_paths


def delete_files_from_dropbox(dropbox_paths):
    """Cancella i PDF da Dropbox dopo l'elaborazione"""
    dbx = crea_dropbox_client()
    for path in dropbox_paths:
        try:
            dbx.files_delete_v2(path)
            print(f"File {path} cancellato da Dropbox.")
        except Exception as e:
            print(f"Errore cancellazione {path}: {e}")


def generate_news_from_pdf(path):
    """
    Invia il PDF DIRETTAMENTE a Gemini, che lo legge da solo.
    Funziona anche se il PDF e' una scansione (foto delle pagine),
    perche' Gemini "guarda" anche le immagini e non solo il testo.
    """
    prompt = """Sei un estrattore di notizie calcistiche estremamente preciso. Analizza il PDF del quotidiano allegato e riporta SOLO le notizie riguardanti la Juventus.

    REGOLA TASSATIVA ED IMPERATIVA:
    - NON USARE MAI GLI ASTERISCHI (**).
    - Per le formattazioni usa SOLO ed esclusivamente i tag indicati qui sotto.

    Tag da usare:
    1. PERSONE (giocatori, allenatori, dirigenti): racchiudi nome e cognome tra <b> e </b>. Esempio: <b>Damien Comolli</b>, <b>Dusan Vlahovic</b>.
    2. SQUADRE di calcio: racchiudile tra <t> e </t> (NON usare <b>). Esempio: <t>Juventus</t>, <t>Real Madrid</t>, <t>Atletico Madrid</t>.
    3. CAMPIONATI e COMPETIZIONI: racchiudili tra <c> e </c> (NON usare <b>). Esempio: <c>Serie A</c>, <c>Champions League</c>, <c>Europa League</c>, <c>Coppa Italia</c>.

    Altre regole di formattazione:
    4. Inserisci tassativamente uno di questi tre tag alla fine di ogni notizia per indicare la fonte: [FONTE_TUTTO], [FONTE_GAZZETTA] o [FONTE_CORRIERE].
    5. Struttura: [NOTIZIA][Emoji] Testo continuo senza titoli... [TAG_FONTE]
    6. Sii sintetico (max 280 caratteri a notizia).
    7. Per le cifre in milioni di euro usa SEMPRE il formato compatto: 1M€, 50M€, 100M€. Mai scrivere "milioni di euro" o "mln" o "M di euro".
    8. Separa ogni notizia con una riga vuota.
    """

    # 1) Carica il PDF su Gemini (gestisce anche file grandi e scansioni)
    print(f"Caricamento di {path} su Gemini...")
    uploaded = client.files.upload(file=path)

    try:
        # 2) Chiede a Gemini di leggere il PDF ed estrarre le notizie
        response = client.models.generate_content(
            model='gemini-3.5-flash',
            contents=[uploaded, prompt],
        )
        return response.text
    finally:
        # 3) Pulisce il file temporaneo caricato su Gemini
        try:
            client.files.delete(name=uploaded.name)
        except Exception as e:
            print(f"Impossibile cancellare il file Gemini: {e}")


def split_notizie(raw):
    """
    Divide il testo di Gemini in singole notizie.
    Prova prima con [NOTIZIA], poi con doppio newline (paragrafi).
    """
    # Caso 1: Gemini ha usato il tag [NOTIZIA]
    if "[NOTIZIA]" in raw:
        lista = [n.strip() for n in raw.split("[NOTIZIA]") if n.strip()]
    else:
        # Caso 2: Gemini ha separato le notizie con righe vuote
        lista = [n.strip() for n in raw.split("\n\n") if n.strip()]

    return lista


def _hashtag_persona(testo):
    """
    Persone: hashtag SOLO sull'ultima parola (il cognome).
    - "Damien Comolli" -> "Damien #Comolli"
    - "Vlahovic"       -> "#Vlahovic"
    """
    testo = " ".join(testo.split())  # normalizza spazi/newline
    if not testo:
        return ""
    parole = testo.split(" ")
    if len(parole) == 1:
        return "#" + parole[0]
    return " ".join(parole[:-1]) + " #" + parole[-1]


def _hashtag_squadra(testo):
    """
    Squadre: hashtag unico, parole unite.
    - "Real Madrid"     -> "#RealMadrid"
    - "Juventus"        -> "#Juventus"
    - "Atletico Madrid" -> "#Atleti" (alias speciale)
    """
    norm = " ".join(testo.split()).lower()
    if "atletico" in norm or "atlético" in norm:
        return "#Atleti"
    return "#" + "".join(testo.split())


def _hashtag_competizione(testo):
    """
    Campionati/competizioni: hashtag unico.
    Competizioni UEFA con sigle dedicate:
    - Champions League -> #UCL
    - Europa League    -> #UEL
    - Conference       -> #UECL
    Le altre: parole unite (es. "Serie A" -> "#SerieA").
    """
    norm = " ".join(testo.split()).lower()
    if "conference" in norm:
        return "#UECL"
    if "europa league" in norm or norm == "europa":
        return "#UEL"
    if "champions" in norm:
        return "#UCL"
    return "#" + "".join(testo.split())


def render_v1(testo):
    """
    Versione ATTUALE (identica a prima):
    - persone <b> e squadre <t> -> grassetto
    - campionati <c> -> testo normale (niente grassetto)
    """
    # squadre -> grassetto, come adesso
    testo = re.sub(r"<t>(.*?)</t>", lambda m: "<b>" + m.group(1) + "</b>",
                   testo, flags=re.DOTALL | re.IGNORECASE)
    # campionati -> testo normale
    testo = re.sub(r"</?c>", "", testo, flags=re.IGNORECASE)
    testo = testo.replace("**", "")
    return testo.strip()


def render_v2(testo):
    """
    Versione HASHTAG (niente grassetto):
    - persone (<b>): hashtag sul cognome
    - squadre (<t>): hashtag unito (con alias #Atleti)
    - campionati (<c>): hashtag/sigla
    """
    testo = re.sub(r"<b>(.*?)</b>", lambda m: _hashtag_persona(m.group(1)),
                   testo, flags=re.DOTALL | re.IGNORECASE)
    testo = re.sub(r"<t>(.*?)</t>", lambda m: _hashtag_squadra(m.group(1)),
                   testo, flags=re.DOTALL | re.IGNORECASE)
    testo = re.sub(r"<c>(.*?)</c>", lambda m: _hashtag_competizione(m.group(1)),
                   testo, flags=re.DOTALL | re.IGNORECASE)
    # pulizia di eventuali tag residui (no grassetto in questa versione)
    testo = re.sub(r"</?(b|t|c)>", "", testo, flags=re.IGNORECASE)
    testo = testo.replace("**", "")
    return testo.strip()


# Stato per il separatore: messo prima di ogni notizia tranne la primissima
# (vale anche tra giornali diversi, dato che send_to_telegram viene chiamata piu' volte)
_prima_notizia_inviata = False


def send_to_telegram(news_list):
    global _prima_notizia_inviata
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    # Messaggio separatore tra una notizia (le sue 2 versioni) e la successiva
    separatore = "———————————————"

    emoji_mapping = {
        "[FONTE_TUTTO]": ('<tg-emoji emoji-id="6032834612990841221">📰</tg-emoji>', "TuttoSport", "@tuttosport"),
        "[FONTE_GAZZETTA]": ('<tg-emoji emoji-id="6032862491623559282">📰</tg-emoji>', "Gazzetta dello Sport", "@Gazzetta_it"),
        "[FONTE_CORRIERE]": ('<tg-emoji emoji-id="6030691308346019878">📰</tg-emoji>', "Corriere dello Sport", "@CorSport")
    }
    tg_reborn = '<tg-emoji emoji-id="5985659276327132147">👉</tg-emoji>'

    def _post(testo):
        try:
            resp = requests.post(
                url,
                json={"chat_id": CHAT_ID, "text": testo, "parse_mode": "HTML"},
                timeout=10
            )
            if not resp.ok:
                print(f"Errore Telegram: {resp.status_code} - {resp.text}")
        except Exception as e:
            print(f"Errore invio Telegram: {e}")

    for news in news_list:
        clean = news.strip()
        if not clean:
            continue

        # Rimuove eventuali asterischi residui
        clean = clean.replace("**", "")

        # Individua il tag fonte, default TuttoSport
        tag = next((t for t in emoji_mapping if t in clean), "[FONTE_TUTTO]")
        clean = clean.replace(tag, "").strip()

        emoji_fonte, nome_fonte, handle_fonte = emoji_mapping[tag]

        # Costruisce i due corpi a partire dallo stesso testo (con i tag <b>/<t>/<c>)
        corpo_v1 = render_v1(clean)
        corpo_v2 = render_v2(clean)

        # VERSIONE 1: come adesso (con la riga @Juventus_Reborn)
        testo_v1 = f"{corpo_v1}\n\n{emoji_fonte} <i>{nome_fonte}</i>\n\n{tg_reborn} @Juventus_Reborn"

        # Separatore prima di ogni notizia, tranne la primissima inviata
        if _prima_notizia_inviata:
            _post(separatore)
            time.sleep(1)
        _prima_notizia_inviata = True

        _post(testo_v1)
        # Piccola pausa tra un messaggio e l'altro per evitare rate limit
        time.sleep(1)

        # VERSIONE 2: con gli hashtag, SENZA la riga @Juventus_Reborn
        testo_v2 = f"{corpo_v2}\n\n📲 {handle_fonte}"
        _post(testo_v2)
        time.sleep(1)


if __name__ == "__main__":
    pdfs, dropbox_paths = get_pdf_from_dropbox()

    if len(pdfs) == 0:
        print("Nessun PDF nuovo. Chiusura.")
    else:
        for i, path in enumerate(pdfs):
            print(f"Elaborazione {path}...")
            try:
                raw = generate_news_from_pdf(path)
                if raw and raw.strip():
                    lista = split_notizie(raw)
                    print(f"Notizie trovate: {len(lista)}")
                    send_to_telegram(lista)
                else:
                    print(f"Nessuna notizia estratta da {path}.")
            except Exception as e:
                print(f"Errore Gemini: {e}")

            if os.path.exists(path):
                os.remove(path)

            if i < len(pdfs) - 1:
                print("In attesa di 20 secondi prima del prossimo giornale...")
                time.sleep(20)

        print("Cancellazione PDF da Dropbox...")
        delete_files_from_dropbox(dropbox_paths)

        print("Operazione completata.")
