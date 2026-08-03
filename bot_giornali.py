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
# Telegram accetta messaggi fino a 4096 caratteri: si lascia margine per
# l'intestazione con la fonte e per i tag HTML. Le notizie oltre soglia
# vengono divise localmente, senza consumare altre richieste Gemini.
MAX_CARATTERI_NOTIZIA = int(os.getenv("MAX_CARATTERI_NOTIZIA", "3800"))
USA_DOPPIA_VERIFICA = os.getenv("USA_DOPPIA_VERIFICA", "false").lower() not in {
    "0",
    "false",
    "no",
}
MAX_CICLI_GEMINI = max(1, int(os.getenv("MAX_CICLI_GEMINI", "3")))
ATTESA_503_GEMINI = max(1, int(os.getenv("ATTESA_503_GEMINI", "20")))

# Inizializzazione del client ufficiale Google GenAI
client = genai.Client(api_key=GEMINI_API_KEY)

# Flash-Lite è ottimizzato per parsing documentale ad alto volume. Gli altri
# modelli usano quote separate e fungono da riserva su 429/503.
MODELLI = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
]

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
            print(
                f"Il download di {file.name} è fallito: "
                "il PDF resterà su Dropbox per il prossimo tentativo."
            )

    return documenti


def delete_files_from_dropbox(dropbox_paths):
    """Cancella da Dropbox i PDF indicati."""
    if not dropbox_paths:
        return

    try:
        dbx = crea_dropbox_client()
    except Exception as e:
        print(f"Impossibile creare il client per la cancellazione Dropbox: {e}")
        return

    for path in dropbox_paths:
        for tentativo in range(1, 4):
            try:
                dbx.files_delete_v2(path)
                print(f"File {path} cancellato da Dropbox.")
                break
            except Exception as e:
                if tentativo == 3:
                    print(
                        f"Errore cancellazione {path} dopo 3 tentativi: {e}"
                    )
                else:
                    print(
                        f"Errore cancellazione {path}: {e}. "
                        f"Nuovo tentativo ({tentativo + 1}/3)..."
                    )
                    time.sleep(2)


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


def _secondi_attesa_gemini(messaggio):
    """Ricava il retry delay suggerito dall'errore Gemini, con limite prudente."""
    for pattern in (
        r"Please retry in\s+([0-9.]+)s",
        r"['\"]retryDelay['\"]\s*:\s*['\"]([0-9.]+)s",
    ):
        match = re.search(pattern, messaggio, flags=re.IGNORECASE)
        if match:
            return min(max(int(float(match.group(1))) + 2, 2), 60)
    return 30


def _genera_json(uploaded, prompt, schema=SCHEMA_NOTIZIE):
    """Prova tutti i modelli e ripete il giro con backoff su 429/503."""
    ultimo_errore = None
    modelli_con_quota_giornaliera_esaurita = set()

    for ciclo in range(1, MAX_CICLI_GEMINI + 1):
        attesa_ciclo = None
        modelli_tentati = 0

        for modello in MODELLI:
            if modello in modelli_con_quota_giornaliera_esaurita:
                continue

            modelli_tentati += 1
            try:
                print(
                    f"Tentativo con il modello {modello} "
                    f"(ciclo {ciclo}/{MAX_CICLI_GEMINI})..."
                )
                response = client.models.generate_content(
                    model=modello,
                    contents=[uploaded, prompt],
                    config={
                        "response_mime_type": "application/json",
                        "response_schema": schema,
                        "temperature": 0,
                        "max_output_tokens": 65536,
                        # Si riserva poco budget al pensiero interno del modello,
                        # così non sottrae spazio al JSON delle notizie.
                        "thinking_config": {"thinking_budget": 2048},
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
                            "raggiunto. Il PDF resterà su Dropbox."
                        )

                parsed = getattr(response, "parsed", None)
                if hasattr(parsed, "model_dump"):
                    parsed = parsed.model_dump()
                if not isinstance(parsed, dict):
                    parsed = json.loads(response.text)

                notizie = parsed.get("notizie")
                if not isinstance(notizie, list):
                    raise ValueError(
                        "Gemini non ha restituito una lista di notizie."
                    )
                return notizie
            except Exception as e:
                ultimo_errore = e
                msg = str(e)
                errore_quota = "429" in msg or "RESOURCE_EXHAUSTED" in msg
                errore_temporaneo = (
                    "503" in msg
                    or "UNAVAILABLE" in msg
                    or "overloaded" in msg.lower()
                )

                if errore_quota:
                    quota_giornaliera = bool(
                        re.search(
                            r"GenerateRequestsPerDay|requests? per day|"
                            r"daily quota",
                            msg,
                            flags=re.IGNORECASE,
                        )
                    )
                    if quota_giornaliera:
                        modelli_con_quota_giornaliera_esaurita.add(modello)
                        print(
                            f"{modello}: quota giornaliera esaurita. "
                            "Lo escludo dai prossimi cicli..."
                        )
                        continue

                    attesa_quota = _secondi_attesa_gemini(msg)
                    attesa_ciclo = max(
                        attesa_ciclo or 0,
                        attesa_quota,
                    )
                    print(
                        f"Quota temporanea per {modello}. "
                        "Provo il modello successivo..."
                    )
                    continue

                if errore_temporaneo:
                    attesa_503 = min(
                        ATTESA_503_GEMINI * (2 ** (ciclo - 1)),
                        60,
                    )
                    attesa_ciclo = max(attesa_ciclo or 0, attesa_503)
                    print(
                        f"Modello {modello} temporaneamente non disponibile "
                        "(503). Provo il modello successivo..."
                    )
                    continue

                raise

        if ciclo >= MAX_CICLI_GEMINI or modelli_tentati == 0:
            break
        if attesa_ciclo is None:
            break

        print(
            "Tutti i modelli disponibili sono temporaneamente occupati. "
            f"Attendo {attesa_ciclo}s prima del ciclo successivo..."
        )
        time.sleep(attesa_ciclo)

    if ultimo_errore is None:
        raise RuntimeError("Nessun modello Gemini configurato.")
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
- Formatta gli importi in milioni di euro in modo compatto:
  "10 milioni di euro" -> "10M€"; "100 milioni di euro" -> "100M€";
  "tra 40 e 50 milioni di euro" -> "40-50M€";
  "circa 50 milioni di euro" -> "circa 50M€".
- Conserva sempre parole come "circa", "quasi", "oltre" e "almeno".
  Crea un intervallo soltanto se entrambi gli estremi sono scritti nel PDF:
  non trasformare mai "circa 50 milioni" in un intervallo inventato.
- Usa M€ esclusivamente per importi in euro, non per altri valori espressi
  in milioni.
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
- formatta "10 milioni di euro" come "10M€", un intervallo esplicito come
  "40-50M€" e "circa 50 milioni di euro" come "circa 50M€"; non inventare
  intervalli e non perdere parole come "circa", "quasi", "oltre" o "almeno";
- usa esclusivamente i tag <b>, <t> e <c> previsti e nessun tag fonte nel
  testo;
- restituisci un riscontro breve e fedele e la pagina corretta.

Non aggiungere alcuna informazione per rendere il testo più scorrevole.
In caso di dubbio, ometti.

CANDIDATI DA VERIFICARE:
{candidati_json}
""".strip()


def _tag_bilanciati(testo):
    for tag in ("b", "t", "c"):
        aperture = len(re.findall(fr"<{tag}>", testo, flags=re.IGNORECASE))
        chiusure = len(re.findall(fr"</{tag}>", testo, flags=re.IGNORECASE))
        if aperture != chiusure:
            return False
    return True


def _normalizza_importi_euro(testo):
    """
    Compatta soltanto importi esplicitamente indicati in milioni di euro.

    Non deduce la valuta dal contesto e conserva qualificatori come "circa".
    Gli intervalli vengono creati soltanto quando nel testo sono presenti
    entrambi gli estremi.
    """
    numero = r"\d+(?:[.,]\d+)?"
    valuta = r"(?:milion(?:e|i)|mln)\s*(?:di\s+|d['’]\s*)?(?:euro|€)"

    # "tra/fra i 40 e i 50 milioni di euro" -> "40-50M€"
    testo = re.sub(
        rf"\b(?:tra|fra)\s+(?:i\s+)?({numero})\s+e\s+(?:i\s+)?"
        rf"({numero})\s+{valuta}(?!\w)",
        lambda m: f"{m.group(1)}-{m.group(2)}M€",
        testo,
        flags=re.IGNORECASE,
    )

    # "da/dai 40 a/ai 50 milioni di euro" -> "40-50M€"
    testo = re.sub(
        rf"\bda(?:i)?\s+({numero})\s+a(?:i)?\s+({numero})\s+{valuta}(?!\w)",
        lambda m: f"{m.group(1)}-{m.group(2)}M€",
        testo,
        flags=re.IGNORECASE,
    )

    # "40-50 milioni di euro" -> "40-50M€"
    testo = re.sub(
        rf"\b({numero})\s*[-–—]\s*({numero})\s+{valuta}(?!\w)",
        lambda m: f"{m.group(1)}-{m.group(2)}M€",
        testo,
        flags=re.IGNORECASE,
    )

    # Conserva l'eventuale approssimazione: "circa 50 milioni" non diventa
    # mai un intervallo deciso dal programma.
    testo = re.sub(
        rf"\b((?:circa|quasi|oltre|almeno|meno\s+di|più\s+di)\s+)?"
        rf"({numero})\s+{valuta}(?!\w)",
        lambda m: f"{m.group(1) or ''}{m.group(2)}M€",
        testo,
        flags=re.IGNORECASE,
    )

    # "un milione di euro" -> "1M€"
    testo = re.sub(
        rf"\bun\s+{valuta}(?!\w)",
        "1M€",
        testo,
        flags=re.IGNORECASE,
    )
    return testo


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
    testo = _normalizza_importi_euro(testo)
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


def _valida_notizie(notizie, fonte_attesa):
    """Applica controlli deterministici senza accorciare il testo."""
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
            print(
                f"Notizia {indice}: {lunghezza} caratteri; verrà divisa "
                "in più messaggi Telegram senza riassumerla."
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
    Estrae le notizie in JSON e, solo se richiesto, esegue una seconda verifica
    sullo stesso PDF prima dell'invio.
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


def render_testo(testo):
    """
    Persone e squadre in grassetto; competizioni senza grassetto.
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


def _intervalli_testo(testo, limite):
    """Restituisce intervalli leggibili, preferendo frasi e spazi."""
    if limite < 1:
        raise ValueError("Il limite dei messaggi deve essere positivo.")

    visibile = html.unescape(
        re.sub(r"</?(?:b|t|c)>", "", testo, flags=re.IGNORECASE)
    )
    intervalli = []
    inizio = 0

    while len(visibile) - inizio > limite:
        fine_massima = inizio + limite
        finestra = visibile[inizio : fine_massima + 1]
        fine = None

        frasi = list(re.finditer(r"(?<=[.!?;:])\s+", finestra))
        if frasi:
            candidata = inizio + frasi[-1].start()
            if candidata - inizio >= max(1, limite // 2):
                fine = candidata

        if fine is None:
            spazi = list(re.finditer(r"\s+", finestra))
            if spazi:
                fine = inizio + spazi[-1].start()

        if fine is None or fine <= inizio:
            fine = fine_massima

        intervalli.append((inizio, fine))
        inizio = fine
        while inizio < len(visibile) and visibile[inizio].isspace():
            inizio += 1

    if inizio < len(visibile):
        intervalli.append((inizio, len(visibile)))

    return intervalli


def _estrai_intervallo_markup(testo, inizio, fine):
    """Estrae un intervallo visibile riaprendo e chiudendo i tag interni."""
    token_re = re.compile(
        r"</?(?:b|t|c)>|&(?:#\d+|#x[0-9a-f]+|[a-z][a-z0-9]+);|.",
        flags=re.IGNORECASE | re.DOTALL,
    )
    tag_re = re.compile(r"<(/?)(b|t|c)>", flags=re.IGNORECASE)
    attivi = []
    risultato = []
    posizione = 0
    iniziato = False

    for match in token_re.finditer(testo):
        token = match.group(0)
        tag_match = tag_re.fullmatch(token)
        if tag_match:
            if posizione >= fine:
                break

            chiusura = bool(tag_match.group(1))
            tag = tag_match.group(2).lower()
            if iniziato:
                risultato.append(f"</{tag}>" if chiusura else f"<{tag}>")

            if chiusura:
                if attivi and attivi[-1] == tag:
                    attivi.pop()
            else:
                attivi.append(tag)
            continue

        lunghezza = len(html.unescape(token))
        if posizione + lunghezza <= inizio:
            posizione += lunghezza
            continue
        if posizione >= fine:
            break

        if not iniziato:
            risultato.extend(f"<{tag}>" for tag in attivi)
            iniziato = True
        risultato.append(token)
        posizione += lunghezza

    if iniziato:
        risultato.extend(f"</{tag}>" for tag in reversed(attivi))

    return "".join(risultato).strip()


def _dividi_testo_markup(testo, limite=MAX_CARATTERI_NOTIZIA):
    """Divide senza riassumere, preservando testo e markup consentito."""
    if _lunghezza_visibile(testo) <= limite:
        return [testo]

    parti = [
        _estrai_intervallo_markup(testo, inizio, fine)
        for inizio, fine in _intervalli_testo(testo, limite)
    ]
    return [parte for parte in parti if parte]


def send_to_telegram(news_list):
    """
    Invia ogni notizia con la fonte in alto. I testi oltre soglia vengono
    divisi localmente; True indica che ogni parte è stata consegnata.
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    emoji_mapping = {
        "TUTTO": (
            '<tg-emoji emoji-id="6032834612990841221">📰</tg-emoji>',
            "Tuttosport",
        ),
        "GAZZETTA": (
            '<tg-emoji emoji-id="6032862491623559282">📰</tg-emoji>',
            "Gazzetta dello Sport",
        ),
        "CORRIERE": (
            '<tg-emoji emoji-id="6030691308346019878">📰</tg-emoji>',
            "Corriere dello Sport",
        ),
    }

    def _post(testo, risposta_a=None):
        for attempt in range(5):
            try:
                payload = {
                    "chat_id": CHAT_ID,
                    "text": testo,
                    "parse_mode": "HTML",
                }
                if risposta_a is not None:
                    payload["reply_parameters"] = {
                        "message_id": risposta_a,
                        "allow_sending_without_reply": True,
                    }

                resp = requests.post(
                    url,
                    json=payload,
                    timeout=10,
                )
                if resp.ok:
                    message_id = (
                        resp.json().get("result", {}).get("message_id")
                    )
                    if message_id is None:
                        print(
                            "Telegram ha confermato l'invio senza restituire "
                            "il message_id."
                        )
                        return None
                    return message_id
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
                return None
            except Exception as e:
                print(f"Errore invio Telegram: {e}")
                return None

        print("Telegram: tentativi esauriti, messaggio saltato.")
        return None

    tutto_inviato = True

    for news in news_list:
        clean = news["testo"].strip()
        fonte = _normalizza_fonte(news["fonte"])
        if not clean or fonte not in emoji_mapping:
            print("Notizia saltata: testo vuoto o fonte non valida.")
            tutto_inviato = False
            continue

        emoji_fonte, nome_fonte = emoji_mapping[fonte]
        parti = _dividi_testo_markup(clean)
        risposta_a = None

        for numero, parte in enumerate(parti, start=1):
            corpo = render_testo(parte)
            continuazione = (
                f" ({numero}/{len(parti)})" if len(parti) > 1 else ""
            )
            testo = (
                f"{emoji_fonte} <i>{nome_fonte}</i>{continuazione}"
                f"\n\n{corpo}"
            )

            message_id = _post(testo, risposta_a=risposta_a)
            esito = message_id is not None
            tutto_inviato = esito and tutto_inviato
            time.sleep(1)
            if not esito:
                break
            risposta_a = message_id

    return tutto_inviato


def elabora_documento(documento):
    """Elabora un PDF e lo rimuove da Dropbox solo dopo una lettura riuscita."""
    path = documento["local_path"]
    nome_originale = documento["original_name"]
    lettura_completata = False

    print(f"Elaborazione {nome_originale}...")
    try:
        lista = generate_news_from_pdf(path, nome_originale)
        lettura_completata = True
        if lista:
            print(f"Notizie pronte per l'invio: {len(lista)}")
            if not send_to_telegram(lista):
                print(
                    "Invio Telegram incompleto: il PDF verrà cancellato "
                    "per evitare invii duplicati al prossimo avvio."
                )
        else:
            print("Nessuna notizia Juventus verificata nel PDF.")
    except Exception as e:
        print(f"Errore durante l'elaborazione: {e}")
        print(
            f"{nome_originale} resterà su Dropbox e verrà ritentato "
            "alla prossima esecuzione."
        )
    finally:
        if lettura_completata:
            print(
                f"Lettura completata: cancellazione di {nome_originale} "
                "da Dropbox..."
            )
            delete_files_from_dropbox([documento["dropbox_path"]])
        if os.path.exists(path):
            os.remove(path)

    return lettura_completata


if __name__ == "__main__":
    documenti = get_pdf_from_dropbox()

    if not documenti:
        print("Nessun PDF nuovo. Chiusura.")
    else:
        for i, documento in enumerate(documenti):
            elabora_documento(documento)

            if i < len(documenti) - 1:
                print(
                    "In attesa di 20 secondi prima del prossimo giornale..."
                )
                time.sleep(20)

        print("Operazione completata.")
