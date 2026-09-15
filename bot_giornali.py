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
MAX_CARATTERI_NOTIZIA = int(os.getenv("MAX_CARATTERI_NOTIZIA", "3800"))
USA_DOPPIA_VERIFICA = os.getenv("USA_DOPPIA_VERIFICA", "false").lower() not in {
    "0",
    "false",
    "no",
}
MAX_CICLI_GEMINI = max(1, int(os.getenv("MAX_CICLI_GEMINI", "3")))
ATTESA_503_GEMINI = max(1, int(os.getenv("ATTESA_503_GEMINI", "20")))

# Rich Messages Telegram. Il workflow lo abilita esplicitamente con "1".
TELEGRAM_RICH_MESSAGES = os.getenv("TELEGRAM_RICH_MESSAGES", "0").strip().lower() not in {
    "",
    "0",
    "false",
    "no",
    "off",
}

client = genai.Client(api_key=GEMINI_API_KEY)

MODELLI = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
]

FONTI_VALIDE = ("TUTTO", "GAZZETTA", "CORRIERE")

FONTI_TELEGRAM = {
    "GAZZETTA": {
        "nome": "La Gazzetta dello Sport",
        "emoji_id": "6032862491623559282",
        "fallback": "📰",
    },
    "CORRIERE": {
        "nome": "Corriere dello Sport",
        "emoji_id": "6030691308346019878",
        "fallback": "📰",
    },
    "TUTTO": {
        "nome": "Tuttosport",
        "emoji_id": "6032834612990841221",
        "fallback": "📰",
    },
}

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
                            "Solo testo semplice: nessun titolo, tag HTML, "
                            "Markdown o tag fonte."
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
    """Scarica i PDF e conserva il nome originale."""
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
        file
        for file in entries
        if isinstance(file, dropbox.files.FileMetadata)
        and file.name.lower().endswith(".pdf")
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
            with open(local_filename, "wb") as local_file:
                local_file.write(response.content)
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
                    print(f"Errore cancellazione {path} dopo 3 tentativi: {e}")
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
                    raise ValueError("Gemini non ha restituito una lista di notizie.")
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
                            r"GenerateRequestsPerDay|requests? per day|daily quota",
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
                    attesa_ciclo = max(attesa_ciclo or 0, attesa_quota)
                    print(
                        f"Quota temporanea per {modello}. "
                        "Provo il modello successivo..."
                    )
                    continue

                if errore_temporaneo:
                    attesa_503 = min(ATTESA_503_GEMINI * (2 ** (ciclo - 1)), 60)
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
- Restituisci esclusivamente testo semplice: non usare tag HTML, Markdown,
  asterischi o altri marcatori di formattazione.
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
- usa esclusivamente testo semplice, senza tag HTML, Markdown, asterischi o
  tag fonte;
- restituisci un riscontro breve e fedele e la pagina corretta.

Non aggiungere alcuna informazione per rendere il testo più scorrevole.
In caso di dubbio, ometti.

CANDIDATI DA VERIFICARE:
{candidati_json}
""".strip()


def _normalizza_importi_euro(testo):
    """Compatta soltanto importi esplicitamente indicati in milioni di euro."""
    numero = r"\d+(?:[.,]\d+)?"
    valuta = r"(?:milion(?:e|i)|mln)\s*(?:di\s+|d['’]\s*)?(?:euro|€)"

    testo = re.sub(
        rf"\b(?:tra|fra)\s+(?:i\s+)?({numero})\s+e\s+(?:i\s+)?"
        rf"({numero})\s+{valuta}(?!\w)",
        lambda match: f"{match.group(1)}-{match.group(2)}M€",
        testo,
        flags=re.IGNORECASE,
    )
    testo = re.sub(
        rf"\bda(?:i)?\s+({numero})\s+a(?:i)?\s+({numero})\s+{valuta}(?!\w)",
        lambda match: f"{match.group(1)}-{match.group(2)}M€",
        testo,
        flags=re.IGNORECASE,
    )
    testo = re.sub(
        rf"\b({numero})\s*[-–—]\s*({numero})\s+{valuta}(?!\w)",
        lambda match: f"{match.group(1)}-{match.group(2)}M€",
        testo,
        flags=re.IGNORECASE,
    )
    testo = re.sub(
        rf"\b((?:circa|quasi|oltre|almeno|meno\s+di|più\s+di)\s+)?"
        rf"({numero})\s+{valuta}(?!\w)",
        lambda match: f"{match.group(1) or ''}{match.group(2)}M€",
        testo,
        flags=re.IGNORECASE,
    )
    testo = re.sub(
        rf"\bun\s+{valuta}(?!\w)",
        "1M€",
        testo,
        flags=re.IGNORECASE,
    )
    return testo


def _sanitizza_markup(testo):
    """Rimuove formattazione dal corpo ed esegue l'escape per Telegram."""
    testo = html.unescape(str(testo))
    testo = testo.replace("*", "").replace("`", "")
    testo = re.sub(
        r"$begin:math:display$NOTIZIA$end:math:display$",
        "",
        testo,
        flags=re.IGNORECASE,
    )
    testo = re.sub(
        r"$begin:math:display$FONTE\_\(\?\:TUTTO\|GAZZETTA\|CORRIERE\)$end:math:display$",
        "",
        testo,
        flags=re.IGNORECASE,
    )
    testo = re.sub(r"<[^<>]*>", "", testo)
    testo = _normalizza_importi_euro(testo)
    testo = " ".join(testo.split()).strip()
    return html.escape(testo, quote=False)


def _lunghezza_visibile(testo):
    return len(html.unescape(testo))


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
            re.sub(
                r"</?(?:b|t|c)>",
                "",
                testo,
                flags=re.IGNORECASE,
            ).lower(),
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
    """Estrae le notizie in JSON e, se richiesto, esegue la seconda verifica."""
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
                _prompt_verifica(nome_originale, fonte_attesa, candidati),
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
    """Restituisce il corpo già sanitizzato senza aggiungere formattazione."""
    return testo.strip()


def _intervalli_testo(testo, limite):
    """Restituisce intervalli leggibili, preferendo frasi e spazi."""
    if limite < 1:
        raise ValueError("Il limite dei messaggi deve essere positivo.")

    visibile = html.unescape(testo)
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


def _estrai_intervallo_testo(testo, inizio, fine):
    """Estrae un intervallo senza spezzare le entità HTML del testo."""
    token_re = re.compile(
        r"&(?:#\d+|#x[0-9a-f]+|[a-z][a-z0-9]+);|.",
        flags=re.IGNORECASE | re.DOTALL,
    )

    risultato = []
    posizione = 0

    for match in token_re.finditer(testo):
        token = match.group(0)
        lunghezza = len(html.unescape(token))

        if posizione + lunghezza <= inizio:
            posizione += lunghezza
            continue
        if posizione >= fine:
            break

        risultato.append(token)
        posizione += lunghezza

    return "".join(risultato).strip()


def _dividi_testo(testo, limite=MAX_CARATTERI_NOTIZIA):
    """Divide senza riassumere e senza perdere caratteri del testo."""
    if _lunghezza_visibile(testo) <= limite:
        return [testo]

    parti = [
        _estrai_intervallo_testo(testo, inizio, fine)
        for inizio, fine in _intervalli_testo(testo, limite)
    ]
    return [parte for parte in parti if parte]


def _telegram_message_id(response):
    try:
        data = response.json()
    except ValueError:
        return None

    value = (data.get("result") or {}).get("message_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _telegram_retry_after(response):
    try:
        value = response.json().get("parameters", {}).get("retry_after", 30)
        return max(int(value), 1)
    except (ValueError, TypeError):
        return 30


def _post_telegram(method, payload, *, label):
    """Invio Telegram comune a Rich Message e fallback legacy."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"

    for attempt in range(5):
        try:
            response = requests.post(url, json=payload, timeout=10)

            if response.ok:
                message_id = _telegram_message_id(response)
                if message_id is None:
                    print(
                        f"Telegram {label} ha confermato l'invio senza "
                        "restituire il message_id."
                    )
                return message_id

            if response.status_code == 429:
                retry_after = _telegram_retry_after(response)
                print(
                    f"Rate limit Telegram {label}, attendo {retry_after + 1}s "
                    f"(tentativo {attempt + 1}/5)..."
                )
                time.sleep(retry_after + 1)
                continue

            print(
                f"Telegram {label} non disponibile: "
                f"{response.status_code} - {response.text}"
            )
            return None

        except requests.RequestException as exc:
            print(
                f"Errore di rete Telegram {label} "
                f"(tentativo {attempt + 1}/5): {exc}"
            )
            if attempt < 4:
                delay = min(2 ** attempt, 16)
                print(f"Nuovo tentativo tra {delay}s...")
                time.sleep(delay)
                continue
            return None
        except Exception as exc:
            print(f"Errore Telegram {label}: {exc}")
            return None

    print(f"Telegram {label}: tentativi esauriti.")
    return None


def _post_telegram_legacy(text, reply_to=None):
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_to is not None:
        payload["reply_parameters"] = {
            "message_id": reply_to,
            "allow_sending_without_reply": True,
        }
    return _post_telegram("sendMessage", payload, label="legacy")


def _post_telegram_rich(rich_html, reply_to=None):
    payload = {
        "chat_id": CHAT_ID,
        "rich_message": {"html": rich_html},
    }
    if reply_to is not None:
        payload["reply_parameters"] = {
            "message_id": reply_to,
            "allow_sending_without_reply": True,
        }
    return _post_telegram("sendRichMessage", payload, label="Rich Message")


def send_to_telegram(news_list):
    """
    Invia ogni notizia con Rich Messages e fallback automatico al formato legacy.

    La logica di estrazione, suddivisione, reply e retry resta la stessa. La
    fonte usa la custom emoji esistente e, nel formato Rich, un heading grande.
    """
    tutto_inviato = True

    for news in news_list:
        clean = _sanitizza_markup(news.get("testo", ""))
        fonte = _normalizza_fonte(news.get("fonte", ""))
        configurazione = FONTI_TELEGRAM.get(fonte)

        if not clean or configurazione is None:
            print("Notizia saltata: testo vuoto o fonte non valida.")
            tutto_inviato = False
            continue

        nome_fonte = configurazione["nome"]
        emoji_id = configurazione["emoji_id"]
        fallback = configurazione["fallback"]
        emoji_personalizzata = (
            f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
        )

        parti = _dividi_testo(clean)
        risposta_a = None

        for numero, parte in enumerate(parti, start=1):
            corpo = render_testo(parte)
            continuazione = (
                f" ({numero}/{len(parti)})"
                if len(parti) > 1
                else ""
            )

            testo_legacy = (
                f"{emoji_personalizzata} "
                f"<b>{nome_fonte}</b>{continuazione}"
                f"\n\n{corpo}"
            )

            message_id = None

            if TELEGRAM_RICH_MESSAGES:
                rich_parts = [
                    f"<h2>{emoji_personalizzata} {nome_fonte}</h2>",
                ]
                if len(parti) > 1:
                    rich_parts.append(
                        f"<footer>Parte {numero} di {len(parti)}</footer>"
                    )
                rich_parts.append(f"<p>{corpo}</p>")
                rich_html = "".join(rich_parts)

                message_id = _post_telegram_rich(
                    rich_html,
                    reply_to=risposta_a,
                )

                if message_id is None:
                    print(
                        "[TELEGRAM FALLBACK] sendRichMessage non riuscito; "
                        "uso sendMessage per questa parte."
                    )

            if message_id is None:
                message_id = _post_telegram_legacy(
                    testo_legacy,
                    reply_to=risposta_a,
                )

            esito = message_id is not None
            tutto_inviato = esito and tutto_inviato

            if not esito:
                print(
                    f"Invio interrotto per la fonte {nome_fonte}, "
                    f"parte {numero}/{len(parti)}."
                )
                break

            risposta_a = message_id
            time.sleep(1)

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
                print("In attesa di 20 secondi prima del prossimo giornale...")
                time.sleep(20)

        print("Operazione completata.")
