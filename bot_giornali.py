import html
import json
import os
import re
import time
import unicodedata

import dropbox
import requests
from google import genai


# Configurazione variabili d'ambiente da GitHub Secrets
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
DROPBOX_FOLDER = "/NotizieJR"

# Impostazioni regolabili senza modificare il codice
MAX_CARATTERI_NOTIZIA = int(os.getenv("MAX_CARATTERI_NOTIZIA", "280"))
USA_DOPPIA_VERIFICA = os.getenv("USA_DOPPIA_VERIFICA", "true").lower() not in {
    "0",
    "false",
    "no",
}

# Inizializzazione del client ufficiale Google GenAI
client = genai.Client(api_key=GEMINI_API_KEY)

# Modelli in ordine di priorità. Il secondo viene usato solo se il primo
# è temporaneamente sovraccarico o non disponibile.
MODELLI = ["gemini-3.5-flash", "gemini-2.5-flash"]

FONTI_VALIDE = ("TUTTO", "GAZZETTA", "CORRIERE")

# L'output strutturato impedisce che le notizie vengano divise in base
# a righe vuote, titoli o paragrafi generati liberamente dal modello.
SCHEMA_NOTIZIE = {
    "type": "object",
    "properties": {
        "notizie": {
            "type": "array",
            "description": "Notizie autonome riguardanti la Juventus.",
            "items": {
                "type": "object",
                "properties": {
                    "testo": {
                        "type": "string",
                        "description": (
                            "Testo fedele e autosufficiente della notizia. "
                            "Usa <b>persona</b>, <t>squadra</t> e "
                            "<c>competizione</c>. Nessun titolo o tag fonte."
                        ),
                    },
                    "fonte": {
                        "type": "string",
                        "enum": list(FONTI_VALIDE),
                        "description": "Quotidiano da cui proviene la notizia.",
                    },
                    "pagina": {
                        "type": "string",
                        "description": (
                            "Numero stampato della pagina; se non è leggibile, "
                            "numero progressivo della pagina nel PDF."
                        ),
                    },
                    "riscontro": {
                        "type": "string",
                        "description": (
                            "Breve passaggio copiato fedelmente dal PDF che "
                            "sostiene nomi, cifre, attribuzioni e modalità "
                            "presenti nel testo."
                        ),
                    },
                },
                "required": ["testo", "fonte", "pagina", "riscontro"],
            },
        }
    },
    "required": ["notizie"],
}

# Schema usato soltanto per accorciare le notizie già verificate. Fonte,
# pagina e riscontro non vengono rigenerati: il codice conserva gli originali.
SCHEMA_RIASSUNTI = {
    "type": "object",
    "properties": {
        "notizie": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "description": "Identificatore numerico del candidato.",
                    },
                    "testo": {
                        "type": "string",
                        "description": (
                            "Riassunto fedele, completo e autosufficiente "
                            "entro il limite richiesto."
                        ),
                    },
                },
                "required": ["id", "testo"],
            },
        }
    },
    "required": ["notizie"],
}


def crea_dropbox_client():
    """Crea il client Dropbox con refresh token."""
    return dropbox.Dropbox(
        app_key=DROPBOX_APP_KEY,
        app_secret=DROPBOX_APP_SECRET,
        oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
    )


def get_pdf_from_dropbox():
    """
    Scarica i PDF e conserva il nome originale.

    Il nome originale aiuta a determinare la fonte senza obbligare il modello
    a indovinarla dal contenuto del giornale.
    """
    dbx = crea_dropbox_client()

    try:
        result = dbx.files_list_folder(DROPBOX_FOLDER)
        entries = list(result.entries)
        while result.has_more:
            result = dbx.files_list_folder_continue(result.cursor)
            entries.extend(result.entries)
    except dropbox.exceptions.ApiError as e:
        print(f"Errore accesso cartella Dropbox: {e}")
        return []

    pdf_files = [
        f
        for f in entries
        if isinstance(f, dropbox.files.FileMetadata)
        and f.name.lower().endswith(".pdf")
    ]

    if not pdf_files:
        print("Nessun PDF trovato su Dropbox.")
        return []

    print(f"Trovati {len(pdf_files)} PDF su Dropbox.")
    documenti = []

    for idx, file in enumerate(pdf_files):
        local_filename = f"giornale_{idx}.pdf"
        try:
            print(f"Download {file.name}...")
            _, response = dbx.files_download(file.path_lower)
            with open(local_filename, "wb") as f:
                f.write(response.content)
            documenti.append(
                {
                    "local_path": local_filename,
                    "dropbox_path": file.path_lower,
                    "original_name": file.name,
                }
            )
            print(f"Scaricato: {file.name}")
        except Exception as e:
            print(f"Errore download {file.name}: {e}")

    return documenti


def delete_files_from_dropbox(dropbox_paths):
    """Cancella da Dropbox solo i PDF elaborati con successo."""
    if not dropbox_paths:
        return

    dbx = crea_dropbox_client()
    for path in dropbox_paths:
        try:
            dbx.files_delete_v2(path)
            print(f"File {path} cancellato da Dropbox.")
        except Exception as e:
            print(f"Errore cancellazione {path}: {e}")


def _senza_accenti(testo):
    return "".join(
        carattere
        for carattere in unicodedata.normalize("NFKD", testo)
        if not unicodedata.combining(carattere)
    )


def _fonte_da_nome_file(nome):
    """Ricava la fonte dal nome del PDF quando è indicata chiaramente."""
    norm = _senza_accenti(nome).lower()
    if "tuttosport" in norm or re.search(r"\btutto\b", norm):
        return "TUTTO"
    if "gazzetta" in norm:
        return "GAZZETTA"
    if "corriere" in norm or "corsport" in norm:
        return "CORRIERE"
    return None


def _normalizza_fonte(fonte):
    norm = _senza_accenti(str(fonte)).upper().strip()
    if "TUTTO" in norm:
        return "TUTTO"
    if "GAZZETTA" in norm:
        return "GAZZETTA"
    if "CORRIERE" in norm or "CORSPORT" in norm:
        return "CORRIERE"
    return None


def _genera_json(uploaded, prompt, schema=SCHEMA_NOTIZIE):
    """Esegue una richiesta strutturata con fallback per i soli errori 503."""
    ultimo_errore = None

    for modello in MODELLI:
        try:
            print(f"Tentativo con il modello {modello}...")
            response = client.models.generate_content(
                model=modello,
                contents=[uploaded, prompt],
                config={
                    "response_mime_type": "application/json",
                    "response_schema": schema,
                    "temperature": 0,
                    "max_output_tokens": 16384,
                },
            )

            candidates = getattr(response, "candidates", None) or []
            if candidates:
                finish_reason = str(
                    getattr(candidates[0], "finish_reason", "")
                ).upper()
                if "MAX_TOKENS" in finish_reason:
                    raise RuntimeError(
                        "Risposta Gemini incompleta: limite di output "
                        "raggiunto. Il PDF non verrà cancellato."
                    )

            parsed = getattr(response, "parsed", None)
            if hasattr(parsed, "model_dump"):
                parsed = parsed.model_dump()
            if not isinstance(parsed, dict):
                parsed = json.loads(response.text)

            notizie = parsed.get("notizie")
            if not isinstance(notizie, list):
                raise ValueError("Gemini non ha restituito una lista di notizie.")
            return notizie
        except Exception as e:
            ultimo_errore = e
            msg = str(e)
            if (
                "503" in msg
                or "UNAVAILABLE" in msg
                or "overloaded" in msg.lower()
            ):
                print(
                    f"Modello {modello} non disponibile (503). "
                    "Passo al modello successivo..."
                )
                continue
            raise

    raise ultimo_errore


def _prompt_estrazione(nome_originale, fonte_attesa):
    fonte = (
        f"La fonte è certamente {fonte_attesa}: usa sempre questo valore."
        if fonte_attesa
        else (
            "Determina la fonte esclusivamente dalla testata visibile nel PDF. "
            "Se non è identificabile con certezza, non estrarre notizie."
        )
    )

    return f"""
Agisci come estrattore documentale, non come giornalista. Leggi il PDF
"{nome_originale}" e individua esclusivamente le notizie che riguardano la
Juventus. {fonte}

Regole di contenuto:
- Ogni elemento deve corrispondere a una sola notizia o a un solo nucleo
  informativo coerente presente nello stesso articolo.
- Non unire articoli, box, didascalie o argomenti diversi, anche se citano la
  stessa persona. Non dividere invece titolo, sommario e corpo dello stesso
  articolo in notizie duplicate.
- Riporta solo fatti, nomi, cifre, attribuzioni e giudizi esplicitamente
  presenti nel PDF. Non usare conoscenze esterne e non completare dettagli.
- Conserva esattamente il grado di certezza e l'attribuzione: "potrebbe",
  "valuta", "secondo il giornale" e una dichiarazione non sono fatti certi.
- Se una scansione è ambigua o il testo non è leggibile, ometti il dettaglio.
- Il testo finale deve essere autosufficiente, senza titolo, e lungo al
  massimo {MAX_CARATTERI_NOTIZIA} caratteri visibili. Se non ci sta, elimina
  dettagli secondari senza cambiare il significato; non troncare frasi.
- Non convertire o normalizzare le cifre se ciò può cambiarne il significato.
  Puoi abbreviare soltanto "milioni di euro" in "M€".
- Per la formattazione usa soltanto: <b>nome persona</b>,
  <t>nome squadra</t>, <c>competizione</c>. Non usare asterischi.
- Il campo "riscontro" deve contenere un breve passaggio realmente leggibile
  nel PDF e sufficiente a controllare i dettagli più delicati della notizia.
- Non includere una notizia se non riesci a fornire pagina e riscontro.
""".strip()


def _prompt_verifica(nome_originale, fonte_attesa, candidati):
    fonte = (
        f"Il file è della fonte {fonte_attesa}; imponi questo valore."
        if fonte_attesa
        else (
            "Accetta una fonte soltanto se la testata è chiaramente visibile "
            "nel PDF."
        )
    )

    candidati_json = json.dumps(
        {"notizie": candidati},
        ensure_ascii=False,
        separators=(",", ":"),
    )

    return f"""
Sei il verificatore finale di un'estrazione documentale dal PDF
"{nome_originale}". Confronta uno per uno i candidati qui sotto con il PDF.
{fonte}

Per ciascun candidato:
- elimina ogni nome, cifra, nesso causale o dettaglio non sostenuto dal PDF;
- preserva fonte dell'affermazione, condizionali, dubbi e grado di certezza;
- elimina il candidato se il riscontro non è leggibile o non basta;
- separa candidati che fondono notizie o articoli diversi;
- unisci soltanto duplicati che derivano da titolo/sommario/corpo del medesimo
  articolo;
- mantieni una sola notizia per elemento e massimo
  {MAX_CARATTERI_NOTIZIA} caratteri visibili, senza troncare;
- usa esclusivamente i tag <b>, <t> e <c> previsti e nessun tag fonte nel
  testo;
- restituisci un riscontro breve e fedele e la pagina corretta.

Non aggiungere alcuna informazione per rendere il testo più scorrevole.
In caso di dubbio, ometti.

CANDIDATI DA VERIFICARE:
{candidati_json}
""".strip()


def _prompt_riassunto(nome_originale, candidati):
    candidati_json = json.dumps(
        {"notizie": candidati},
        ensure_ascii=False,
        separators=(",", ":"),
    )

    return f"""
Sei l'ultimo redattore di controllo del PDF "{nome_originale}". I candidati
qui sotto sono già stati verificati sul documento, ma superano il limite di
{MAX_CARATTERI_NOTIZIA} caratteri visibili.

Riscrivi ciascun candidato rispettando tassativamente queste priorità:
1. conserva il fatto centrale: soggetto, azione o situazione e oggetto;
2. conserva attribuzione e grado di certezza ("potrebbe", "valuta",
   "secondo...", dichiarazioni e indiscrezioni);
3. conserva cifre, condizioni e scadenze quando sono essenziali al fatto;
4. elimina soltanto contesto secondario, ripetizioni e aggettivi;
5. non aggiungere sinonimi che rendano il fatto più certo o più forte;
6. non introdurre informazioni assenti dal candidato e dal suo riscontro;
7. produci una frase completa e autosufficiente: non troncare mai;
8. resta entro {MAX_CARATTERI_NOTIZIA} caratteri visibili, esclusi i tag;
9. conserva soltanto i tag <b>, <t> e <c> già previsti.

Restituisci esattamente un risultato per ciascun id ricevuto. Non eliminare,
unire o dividere candidati. Restituisci soltanto id e nuovo testo: fonte,
pagina e riscontro saranno conservati dal programma.

CANDIDATI DA RIASSUMERE:
{candidati_json}
""".strip()


def _tag_bilanciati(testo):
    for tag in ("b", "t", "c"):
        aperture = len(re.findall(fr"<{tag}>", testo, flags=re.IGNORECASE))
        chiusure = len(re.findall(fr"</{tag}>", testo, flags=re.IGNORECASE))
        if aperture != chiusure:
            return False
    return True


def _sanitizza_markup(testo):
    """
    Conserva soltanto i tag interni previsti ed esegue l'escape di tutto il
    resto, così il parse_mode HTML di Telegram non può rompersi.
    """
    testo = html.unescape(str(testo))
    testo = testo.replace("**", "")
    testo = re.sub(r"\[NOTIZIA\]", "", testo, flags=re.IGNORECASE)
    testo = re.sub(
        r"\[FONTE_(?:TUTTO|GAZZETTA|CORRIERE)\]",
        "",
        testo,
        flags=re.IGNORECASE,
    )
    testo = " ".join(testo.split()).strip()

    if not _tag_bilanciati(testo):
        testo = re.sub(r"</?(?:b|t|c)>", "", testo, flags=re.IGNORECASE)

    segnaposto = {}

    def salva_tag(match):
        chiusura = "/" if match.group(1) else ""
        tag = match.group(2).lower()
        token = f"__TAG_CONSENTITO_{len(segnaposto)}__"
        segnaposto[token] = f"<{chiusura}{tag}>"
        return token

    testo = re.sub(
        r"<(/?)(b|t|c)>",
        salva_tag,
        testo,
        flags=re.IGNORECASE,
    )
    testo = html.escape(testo, quote=False)

    for token, tag in segnaposto.items():
        testo = testo.replace(token, tag)

    return testo.strip()


def _lunghezza_visibile(testo):
    senza_tag = re.sub(r"</?(?:b|t|c)>", "", testo, flags=re.IGNORECASE)
    return len(html.unescape(senza_tag))


def _trova_notizie_lunghe(notizie):
    lunghe = []
    for indice, notizia in enumerate(notizie):
        if not isinstance(notizia, dict):
            continue
        testo = _sanitizza_markup(notizia.get("testo", ""))
        if _lunghezza_visibile(testo) > MAX_CARATTERI_NOTIZIA:
            lunghe.append(
                {
                    "id": indice,
                    "testo": testo,
                    "fonte": notizia.get("fonte", ""),
                    "pagina": notizia.get("pagina", ""),
                    "riscontro": notizia.get("riscontro", ""),
                }
            )
    return lunghe


def _riassumi_notizie_lunghe(uploaded, nome_originale, notizie):
    """
    Accorcia soltanto le notizie oltre soglia, conservando invariati fonte,
    pagina e riscontro. Non usa mai un taglio meccanico del testo.
    """
    notizie = [dict(notizia) for notizia in notizie]

    for tentativo in range(1, 4):
        lunghe = _trova_notizie_lunghe(notizie)
        if not lunghe:
            return notizie

        etichetta = "notizia" if len(lunghe) == 1 else "notizie"
        print(
            f"Riassunto mirato di {len(lunghe)} {etichetta} oltre "
            f"{MAX_CARATTERI_NOTIZIA} caratteri "
            f"(tentativo {tentativo}/3)..."
        )
        risultati = _genera_json(
            uploaded,
            _prompt_riassunto(nome_originale, lunghe),
            schema=SCHEMA_RIASSUNTI,
        )

        attesi = {elemento["id"] for elemento in lunghe}
        ricevuti = set()

        for risultato in risultati:
            if not isinstance(risultato, dict):
                continue
            try:
                indice = int(risultato.get("id"))
            except (TypeError, ValueError):
                continue
            if indice not in attesi or indice in ricevuti:
                continue

            nuovo_testo = _sanitizza_markup(risultato.get("testo", ""))
            if not nuovo_testo:
                continue

            # Cambia esclusivamente il testo: la provenienza documentale
            # verificata rimane quella del candidato originale.
            notizie[indice]["testo"] = nuovo_testo
            ricevuti.add(indice)

        mancanti = attesi - ricevuti
        if mancanti:
            raise RuntimeError(
                "Gemini non ha restituito tutti i riassunti richiesti. "
                "Il PDF non verrà cancellato."
            )

    ancora_lunghe = _trova_notizie_lunghe(notizie)
    if ancora_lunghe:
        raise RuntimeError(
            "Impossibile riassumere fedelmente tutte le notizie entro "
            f"{MAX_CARATTERI_NOTIZIA} caratteri dopo 3 tentativi. "
            "Nessun testo verrà troncato e il PDF non verrà cancellato."
        )

    return notizie


def _valida_notizie(notizie, fonte_attesa):
    """
    Applica controlli deterministici.

    Non tronca e non scarta per lunghezza: ogni testo oltre soglia deve essere
    già passato dalla fase di riassunto mirato.
    """
    valide = []
    gia_viste = set()

    for indice, notizia in enumerate(notizie, start=1):
        if not isinstance(notizia, dict):
            print(f"Notizia {indice} scartata: struttura non valida.")
            continue

        testo = _sanitizza_markup(notizia.get("testo", ""))
        pagina = " ".join(str(notizia.get("pagina", "")).split()).strip()
        riscontro = " ".join(str(notizia.get("riscontro", "")).split()).strip()
        fonte_modello = _normalizza_fonte(notizia.get("fonte", ""))
        fonte = fonte_attesa or fonte_modello

        if fonte_attesa and fonte_modello and fonte_modello != fonte_attesa:
            print(
                f"Notizia {indice}: fonte del modello corretta da "
                f"{fonte_modello} a {fonte_attesa} in base al nome del PDF."
            )

        if not testo or not pagina or len(riscontro) < 8 or not fonte:
            print(
                f"Notizia {indice} scartata: mancano testo, fonte, pagina "
                "o riscontro verificabile."
            )
            continue

        lunghezza = _lunghezza_visibile(testo)
        if lunghezza > MAX_CARATTERI_NOTIZIA:
            raise RuntimeError(
                f"Notizia {indice} ancora troppo lunga: {lunghezza} "
                f"caratteri. Nessun testo verrà troncato o scartato."
            )

        chiave = re.sub(
            r"\W+",
            "",
            re.sub(r"</?(?:b|t|c)>", "", testo, flags=re.IGNORECASE).lower(),
        )
        if not chiave or chiave in gia_viste:
            print(f"Notizia {indice} scartata: duplicata o vuota.")
            continue

        gia_viste.add(chiave)
        valide.append(
            {
                "testo": testo,
                "fonte": fonte,
                "pagina": pagina,
                "riscontro": riscontro,
            }
        )

    return valide


def generate_news_from_pdf(path, nome_originale):
    """
    Estrae le notizie in JSON e, normalmente, esegue una seconda verifica
    indipendente sullo stesso PDF prima dell'invio.
    """
    fonte_attesa = _fonte_da_nome_file(nome_originale)
    if fonte_attesa:
        print(f"Fonte ricavata dal nome del file: {fonte_attesa}.")
    else:
        print(
            "Fonte non ricavabile dal nome del file: verrà accettata solo "
            "se riconoscibile con certezza nel PDF."
        )

    print(f"Caricamento di {path} su Gemini...")
    uploaded = client.files.upload(file=path)

    try:
        print("Prima lettura: estrazione delle notizie...")
        candidati = _genera_json(
            uploaded,
            _prompt_estrazione(nome_originale, fonte_attesa),
        )

        if USA_DOPPIA_VERIFICA and candidati:
            print(
                f"Seconda lettura: verifica documentale di "
                f"{len(candidati)} candidati..."
            )
            candidati = _genera_json(
                uploaded,
                _prompt_verifica(
                    nome_originale,
                    fonte_attesa,
                    candidati,
                ),
            )

        candidati = _riassumi_notizie_lunghe(
            uploaded,
            nome_originale,
            candidati,
        )
        notizie = _valida_notizie(candidati, fonte_attesa)
        print(
            f"Notizie approvate: {len(notizie)} su "
            f"{len(candidati)} dopo i controlli finali."
        )
        for indice, notizia in enumerate(notizie, start=1):
            estratto = notizia["riscontro"][:180]
            print(
                f"  [{indice}] {notizia['fonte']} - pagina "
                f"{notizia['pagina']} - riscontro: {estratto}"
            )
        return notizie
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception as e:
            print(f"Impossibile cancellare il file Gemini: {e}")


def _hashtag_persona(testo):
    """
    Persone: nome di battesimo staccato, cognome anche composto in un hashtag.
    """
    testo = " ".join(testo.split())
    if not testo:
        return ""
    parole = testo.split(" ")
    if len(parole) == 1:
        return "#" + parole[0]
    return parole[0] + " #" + "".join(parole[1:])


def _hashtag_squadra(testo):
    """Squadre: hashtag unico, con alias #Atleti."""
    norm = " ".join(testo.split()).lower()
    if "atletico" in norm or "atlético" in norm:
        return "#Atleti"
    return "#" + "".join(testo.split())


def _hashtag_competizione(testo):
    """Campionati e competizioni: hashtag unico o sigla UEFA."""
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
    Versione con persone e squadre in grassetto; competizioni senza grassetto.
    """
    testo = re.sub(
        r"<t>(.*?)</t>",
        lambda m: "<b>" + m.group(1) + "</b>",
        testo,
        flags=re.DOTALL | re.IGNORECASE,
    )
    testo = re.sub(r"</?c>", "", testo, flags=re.IGNORECASE)
    testo = testo.replace("**", "")
    return testo.strip()


def render_v2(testo):
    """Versione hashtag senza grassetto."""
    testo = re.sub(
        r"<b>(.*?)</b>",
        lambda m: _hashtag_persona(m.group(1)),
        testo,
        flags=re.DOTALL | re.IGNORECASE,
    )
    testo = re.sub(
        r"<t>(.*?)</t>",
        lambda m: _hashtag_squadra(m.group(1)),
        testo,
        flags=re.DOTALL | re.IGNORECASE,
    )
    testo = re.sub(
        r"<c>(.*?)</c>",
        lambda m: _hashtag_competizione(m.group(1)),
        testo,
        flags=re.DOTALL | re.IGNORECASE,
    )
    testo = re.sub(r"</?(b|t|c)>", "", testo, flags=re.IGNORECASE)
    testo = testo.replace("**", "")
    return testo.strip()


# Stato per il separatore: viene inserito tra due notizie.
_prima_notizia_inviata = False


def send_to_telegram(news_list):
    """
    Invia le due versioni e restituisce True soltanto se ogni invio riesce.
    """
    global _prima_notizia_inviata
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    separatore = "———————————————"

    emoji_mapping = {
        "TUTTO": (
            '<tg-emoji emoji-id="6032834612990841221">📰</tg-emoji>',
            "Tuttosport",
            "@tuttosport",
        ),
        "GAZZETTA": (
            '<tg-emoji emoji-id="6032862491623559282">📰</tg-emoji>',
            "Gazzetta dello Sport",
            "@Gazzetta_it",
        ),
        "CORRIERE": (
            '<tg-emoji emoji-id="6030691308346019878">📰</tg-emoji>',
            "Corriere dello Sport",
            "@CorSport",
        ),
    }
    tg_reborn = (
        '<tg-emoji emoji-id="5985659276327132147">👉</tg-emoji>'
    )

    def _post(testo):
        for attempt in range(5):
            try:
                resp = requests.post(
                    url,
                    json={
                        "chat_id": CHAT_ID,
                        "text": testo,
                        "parse_mode": "HTML",
                    },
                    timeout=10,
                )
                if resp.ok:
                    return True
                if resp.status_code == 429:
                    retry_after = (
                        resp.json()
                        .get("parameters", {})
                        .get("retry_after", 30)
                    )
                    print(
                        f"Rate limit Telegram, attendo {retry_after + 1}s "
                        f"(tentativo {attempt + 1}/5)..."
                    )
                    time.sleep(retry_after + 1)
                    continue

                print(f"Errore Telegram: {resp.status_code} - {resp.text}")
                return False
            except Exception as e:
                print(f"Errore invio Telegram: {e}")
                return False

        print("Telegram: tentativi esauriti, messaggio saltato.")
        return False

    tutto_inviato = True

    for news in news_list:
        clean = news["testo"].strip()
        fonte = _normalizza_fonte(news["fonte"])
        if not clean or fonte not in emoji_mapping:
            print("Notizia saltata: testo vuoto o fonte non valida.")
            tutto_inviato = False
            continue

        emoji_fonte, nome_fonte, handle_fonte = emoji_mapping[fonte]
        corpo_v1 = render_v1(clean)
        corpo_v2 = render_v2(clean)

        testo_v1 = (
            f"{corpo_v1}\n\n{emoji_fonte} <i>{nome_fonte}</i>"
            f"\n\n{tg_reborn} @Juventus_Reborn"
        )

        if _prima_notizia_inviata:
            tutto_inviato = _post(separatore) and tutto_inviato
            time.sleep(1)

        esito_v1 = _post(testo_v1)
        tutto_inviato = esito_v1 and tutto_inviato
        if esito_v1:
            _prima_notizia_inviata = True
        time.sleep(1)

        testo_v2 = f"{corpo_v2}\n\n📲 {handle_fonte}"
        tutto_inviato = _post(testo_v2) and tutto_inviato
        time.sleep(1)

    return tutto_inviato


if __name__ == "__main__":
    documenti = get_pdf_from_dropbox()

    if not documenti:
        print("Nessun PDF nuovo. Chiusura.")
    else:
        dropbox_da_cancellare = []

        for i, documento in enumerate(documenti):
            path = documento["local_path"]
            nome_originale = documento["original_name"]
            elaborazione_riuscita = False

            print(f"Elaborazione {nome_originale}...")
            try:
                lista = generate_news_from_pdf(path, nome_originale)
                if lista:
                    print(f"Notizie pronte per l'invio: {len(lista)}")
                    elaborazione_riuscita = send_to_telegram(lista)
                    if not elaborazione_riuscita:
                        print(
                            "Invio incompleto: il PDF resterà su Dropbox "
                            "per evitare di perdere le notizie."
                        )
                else:
                    print("Nessuna notizia Juventus verificata nel PDF.")
                    elaborazione_riuscita = True
            except Exception as e:
                print(f"Errore durante l'elaborazione: {e}")
            finally:
                if os.path.exists(path):
                    os.remove(path)

            if elaborazione_riuscita:
                dropbox_da_cancellare.append(documento["dropbox_path"])

            if i < len(documenti) - 1:
                print(
                    "In attesa di 20 secondi prima del prossimo giornale..."
                )
                time.sleep(20)

        print("Cancellazione da Dropbox dei soli PDF elaborati...")
        delete_files_from_dropbox(dropbox_da_cancellare)

        non_elaborati = len(documenti) - len(dropbox_da_cancellare)
        if non_elaborati:
            print(
                f"{non_elaborati} PDF non cancellati perché l'elaborazione "
                "o l'invio non sono terminati correttamente."
            )
        print("Operazione completata.")
