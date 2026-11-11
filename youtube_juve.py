"""YouTube -> notizie Juventus sul bot JR.

Si avvia dal workflow "Notizie da YouTube" (a mano o dal Comando Rapido dell'iPhone) con il link di
un video. Gemini guarda il video direttamente dal link (niente sottotitoli da scaricare), tiene solo
quello che riguarda la Juventus o giocatori legati alla Juve e lo riscrive come notizie pronte da
pubblicare: al massimo 240 caratteri ciascuna, divise solo quando serve. Le notizie arrivano sul bot
JR (stessa chat di Notizie quotidiani) già nel formato del canale @Juventus_Reborn, una per
messaggio, pronte da inoltrare: emoji iniziali, nomi in grassetto, foto e "Nome - YouTube" di chi
parla (sul canale di Romano può essere anche Moretto), @Juventus_Reborn.
"""
import html
import os
import re
import sys
import time
import unicodedata

import requests

import bot_giornali as bg

MAX_CARATTERI = int(os.getenv("YOUTUBE_MAX_CARATTERI", "240"))
ATTESA = int(os.getenv("YOUTUBE_ATTESA", "900"))     # quanto aspettare un video non ancora leggibile (s)
PAUSA = int(os.getenv("YOUTUBE_PAUSA", "120"))       # ogni quanto riprovare (s)
YOUTUBE_EMOJI_ID = "5974578526991425004"       # per gli avvisi

# Canali seguiti di più: chi può parlare (sul canale di Romano parla spesso anche Moretto).
CANALI = {
    "@fabrizioromanoitaliano": ["Fabrizio Romano", "Matteo Moretto"],
    "@fabrizioromano": ["Fabrizio Romano", "Matteo Moretto"],
    "@romeoagresti": ["Romeo Agresti"],
    "@mattemoretto": ["Matteo Moretto"],
}

# --- Formato dei post di @Juventus_Reborn (emoji personalizzate prese dai post del canale) ---
FIRMA_JR = ('<tg-emoji emoji-id="5985659276327132147">👉</tg-emoji> '
            '<a href="https://t.me/Juventus_Reborn">@Juventus_Reborn</a>')
FOTO = {                                       # foto di chi parla, prima di "Nome - YouTube"
    "fabrizio romano": "5785366354106261925",
    "matteo moretto": "5785259727248170398",
    "romeo agresti": "5784902446098685755",
    "gianluca di marzio": "5785253271912324677",
    "nicolo schira": "5785305056333012850",
    "mirko di natale": "5810081494302992771",
    "giovanni albanese": "5785204949235279563",
}
FOTO_GENERICA = "5406809207947142040"
TONI = {                                       # prima emoji: che tipo di notizia è
    "nuova": ("🆕", "5361979468887893611"),
    "aggiornamento": ("🆙", "5364105043907716258"),
    "valutazione": ("🤔", "5370724846936267183"),
    "suggestione": ("⁉️", "5467596412663372909"),
    "interesse": ("👀", "5424885441100782420"),
    "approfondimento": ("🧐", "5373153968769735192"),
    "fatto": ("✅", "5427009714745517609"),
    "saltata": ("❌", "5465665476971471368"),
    "ostacolo": ("⚠️", "5447644880824181073"),
    "urgente": ("🚨", "5395695537687123235"),
    "rientro": ("🔙", "5352759161945867747"),
    "in_arrivo": ("🔜", "5355075407743826720"),
    "soldi": ("💰", "5375296873982604963"),
    "accordo": ("🤝", "5357080225463149588"),
    "dichiarazione": ("🗣", "5370765563226236970"),
    "elogio": ("🔝", "5422354988103901774"),
    "silenzio": ("🤐", "5372800046284674872"),
}
FACCE = {                                      # tesserati Juve: la loro emoji va per prima
    "spalletti": ("👨‍🦲", "5785405760431207322"),
    "conceicao": ("🇵🇹", "5789811056847102509"),
    "yildiz": ("🇹🇷", "5789644098583404844"),
    "gonzalez": ("🇦🇷", "5807952577798611024"),
    "alajbegovic": ("🇧🇦", "5789842410108361950"),
    "bremer": ("🇧🇷", "5789828275370992311"),
    "boga": ("🇨🇮", "5789597223310335831"),
    "woltemade": ("🇩🇪", "5816639114795033093"),
    "kalulu": ("🇫🇷", "5789930585786949166"),
    "kolo muani": ("🇫🇷", "5789850871193935307"),
    "vicario": ("🇮🇹", "5789445537950344432"),
    "cambiaso": ("🇮🇹", "5791922170712039419"),
    "ekhator": ("🇮🇹", "5789870589388791439"),
    "gatti": ("🇮🇹", "5789729856195402778"),
    "koopmeiners": ("🇳🇱", "5789588886778814092"),
    "sarr": ("🇸🇳", "5816763346724069174"),
    "mckennie": ("🇺🇸", "5789765362690039962"),
    "zhegrova": ("🇽🇰", "5789535831047806543"),
    "celik": ("🇹🇷", "5789433847049363036"),
    "cabal": ("🇨🇴", "5789717237581491671"),
    "pinsoglio": ("🇮🇹", "5789708535977746989"),
    "milik": ("🇵🇱", "5789440070456974878"),
    "thuram": ("🇫🇷", "5789690608784253534"),
    "douglas luiz": ("🇧🇷", "5789538682906091775"),
}
BANDIERE = {                                   # giocatori non della Juve: bandiera dopo il tono
    "IT": "5449723275628259037", "AR": "5262873863036872166", "BR": "5202074005346983800",
    "FR": "5202132623060640759", "PT": "5382075788369605892", "NL": "5411124743841524806",
    "NO": "5382300771641470186", "DK": "5381854399985366436", "CA": "5382084502858249131",
    "DE": "5409360418520967565", "ES": "5201957744877248121", "BE": "5411564862025244994",
    "SE": "5384542551296455687", "TR": "5226948110873278599", "ME": "5440827745523216914",
    "GR": "5381889099026149370", "HU": "5409065547541260699", "BA": "5382033281078275575",
    "CL": "5222427035023977899", "CO": "5341564321098907092",
}

_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})")

SCHEMA = {
    "type": "object",
    "properties": {
        "notizie": {
            "type": "array",
            "description": ("Una voce per ARGOMENTO (es. un giocatore o una trattativa), nell'ordine del "
                            "video. Vuota se non si parla della Juventus."),
            "items": {
                "type": "object",
                "properties": {
                    "argomento": {"type": "string", "description": "Di cosa parla, in poche parole (es. 'Sorloth')."},
                    "minuto": {"type": "string", "description": "Minuto del video in cui inizia (mm:ss)."},
                    "persona": {"type": "string", "description": "Nome e cognome di chi dice questa notizia nel video."},
                    "parti": {
                        "type": "array",
                        "description": ("La notizia: di norma UNA sola parte. Più parti solo se le informazioni "
                                        "importanti non stanno in 240 caratteri nemmeno condensando."),
                        "items": {"type": "string", "description": "Testo, al massimo 240 caratteri, nomi tra **."},
                    },
                    "tono": {"type": "string", "enum": list(TONI), "description": "Che tipo di notizia è."},
                    "protagonista": {"type": "string", "description": "Cognome del protagonista (giocatore o allenatore), vuoto se nessuno."},
                    "protagonista_juve": {"type": "boolean", "description": "Il protagonista è tesserato della Juventus."},
                    "nazione": {"type": "string", "description": "Codice ISO a 2 lettere della nazionalità del protagonista, vuoto se non è un giocatore."},
                    "riscontro": {
                        "type": "string",
                        "description": ("Le frasi dette nel video su questo argomento, trascritte parola per "
                                        "parola (nella lingua del video): devono bastare a controllare ogni "
                                        "dettaglio della notizia."),
                    },
                },
                "required": ["argomento", "minuto", "persona", "parti", "tono", "protagonista",
                             "protagonista_juve", "nazione", "riscontro"],
            },
        },
    },
    "required": ["notizie"],
}


def video_url(testo):
    """Link YouTube pulito (https://www.youtube.com/watch?v=ID) da qualsiasi forma di link."""
    trovato = _ID.search(str(testo or ""))
    if not trovato or "youtu" not in str(testo):
        raise ValueError("Link YouTube non valido.")
    return f"https://www.youtube.com/watch?v={trovato.group(1)}"


def info_video(url):
    """Titolo, canale e indirizzo del canale (oEmbed pubblico di YouTube, senza chiavi)."""
    try:
        r = requests.get("https://www.youtube.com/oembed", params={"url": url, "format": "json"}, timeout=15)
        if r.ok:
            dati = r.json()
            return {"titolo": dati.get("title", ""), "canale": dati.get("author_name", ""),
                    "canale_url": dati.get("author_url", "")}
    except (requests.RequestException, ValueError):
        pass
    return {"titolo": "", "canale": "", "canale_url": ""}


def persone_note(info):
    """Chi può parlare sul canale (lista, anche vuota)."""
    handle = info.get("canale_url", "").rstrip("/").rsplit("/", 1)[-1].lower()
    return CANALI.get(handle, [])


def _semplice(testo):
    testo = unicodedata.normalize("NFKD", str(testo or "")).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^\w ]", " ", testo.lower()).split())


def nome_persona(persona, possibili, info):
    """Nome pulito di chi parla: se è uno dei nomi attesi per il canale si usa quella grafia."""
    trovato = _semplice(persona).split()
    for nome in possibili:
        if _semplice(nome).split()[-1] in trovato:
            return nome
    if str(persona or "").strip():
        return " ".join(str(persona).split())
    return possibili[0] if possibili else (info.get("canale") or "YouTube")


def prompt(info, possibili):
    if len(possibili) == 1:
        chi = f'Parla {possibili[0]}: "persona" è sempre {possibili[0]}.'
    elif possibili:
        chi = (f"Sul canale può parlare {' oppure '.join(possibili)} (o un ospite): riconosci chi parla "
               '(voce, presentazione, titolo) e in "persona" metti chi dice quella notizia.')
    else:
        chi = 'In "persona" metti nome e cognome di chi dice quella notizia.'
    return f"""
Guarda e ascolta questo video YouTube ("{info.get('titolo', '')}", canale {info.get('canale', '')}).
{chi}
Trasforma in notizie per il canale Telegram Juventus Reborn SOLO le parti che riguardano la Juventus:
club, allenatore, dirigenti, squadra, giocatori della Juventus (anche in prestito) e giocatori
accostati alla Juventus o legati a lei (mercato, rinnovi, cessioni). Ignora tutto il resto.

Regole:
- Scrivi in italiano, anche se il video è in un'altra lingua.
- Stile del canale: frasi brevi e chiare, in terza persona, come questi esempi:
  "La **Juventus** valuta l'acquisto di un centravanti. Aveva trattato a lungo **Sorloth**,
  raggiungendo un accordo sui termini personali, ma non con l'**Atletico Madrid**. Al momento non ci
  sono comunicazioni tra le parti."
  "**Juve** e **Conte** non hanno mai parlato: oggi è solo una suggestione."
  Non iniziare con il nome di chi parla: è già nella firma del post.
- Grassetto: racchiudi tra ** i nomi di club, giocatori, allenatori e dirigenti (**Juventus**,
  **Sorloth**, **Atletico Madrid**). Nient'altro in grassetto.
- FEDELTÀ ASSOLUTA: scrivi solo quello che chi parla dice nel video, con le sue parole quando
  possibile. Niente deduzioni, commenti, conclusioni o contesto aggiunto: nessun nome, data, periodo
  ("in estate"), cifra, club o ruolo che non sia detto esplicitamente. Nel dubbio, lascia fuori.
- Non rafforzare e non attenuare: mantieni esattamente il grado di certezza ("potrebbe", "valuta",
  "c'è un contatto", "è vicino" non sono fatti certi) e le attribuzioni ("secondo il club…").
- Condensare vuol dire togliere, non riformulare cambiando il senso.
- "riscontro": trascrivi parola per parola le frasi del video su quell'argomento. Ogni dettaglio
  della notizia deve trovarsi nel riscontro; se non c'è, toglilo dalla notizia.
- UNA NOTIZIA PER ARGOMENTO. Un argomento è un giocatore, una trattativa, un tema (es. tutto quello
  che si dice su Sorloth e la Juve è UN argomento, anche se se ne parla in momenti diversi del video).
  Raccogli tutto quello che riguarda lo stesso argomento in una sola voce.
- Ogni parte al massimo {MAX_CARATTERI} caratteri (spazi compresi, ** esclusi). Prima condensa: togli
  ripetizioni, giri di parole e dettagli secondari, e fai stare l'argomento in UNA parte. Usa una
  seconda parte solo se restano informazioni importanti che non ci stanno; ogni parte deve reggersi da sola.
- Argomenti diversi vanno in voci diverse: non unirli. Non spezzare un argomento in più voci.
- Non ripetere la stessa informazione: un concetto detto più volte nel video va scritto una volta.
- Importi in milioni di euro in forma compatta: "30 milioni di euro" -> "30M€", "tra 40 e 50" -> "40-50M€".
- Niente emoji, hashtag, titoli o HTML.
- "tono": nuova (notizia nuova), aggiornamento (novità su una cosa già nota), valutazione (il club
  valuta, riflette), suggestione (ipotesi, idea, nulla di concreto), interesse (piace, è osservato),
  approfondimento, fatto (deciso, ufficiale), saltata, ostacolo (problema, concorrenza), urgente,
  rientro (da infortunio), in_arrivo, soldi (cifre, bilancio), accordo, dichiarazione (parole
  riportate), elogio, silenzio (nessun contatto, tutto fermo).
- "protagonista": cognome del giocatore o allenatore principale della notizia (vuoto se nessuno);
  "protagonista_juve": se è della Juventus; "nazione": la sua nazionalità (ISO, es. NO, FR).
- Se nel video non si parla della Juventus, restituisci una lista vuota.
""".strip()


def estrai(url, info):
    """Lista di notizie, ognuna con la sua "persona" già sistemata."""
    from google.genai import types
    possibili = persone_note(info)
    video = types.Part(file_data=types.FileData(file_uri=url))
    risposta = bg._genera_json(video, prompt(info, possibili), schema=SCHEMA, intero=True,
                               config_extra={"media_resolution": "MEDIA_RESOLUTION_LOW"})
    notizie = risposta.get("notizie") or []
    for notizia in notizie:
        notizia["persona"] = nome_persona(notizia.get("persona"), possibili, info)
    return notizie


def _chiave(argomento):
    return _semplice(argomento)


def _pulisci(testo):
    """Testo escapato per Telegram, con i ** del grassetto ancora dentro."""
    testo = html.unescape(str(testo or ""))
    testo = re.sub(r"<[^<>]*>", "", testo).replace("`", "")
    testo = re.sub(r"\*{3,}", "**", testo)
    testo = bg._normalizza_importi_euro(testo)
    return html.escape(" ".join(testo.split()).strip(), quote=False)


def visibile(testo):
    return html.unescape(re.sub(r"</?b>", "", testo).replace("**", ""))


def grassetto(testo):
    """**nome** -> <b>nome</b>; se i ** sono spaiati (es. dopo una divisione) si tolgono."""
    if testo.count("**") % 2:
        return testo.replace("*", "")
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", testo).replace("*", "")


def _dividi(testo):
    if len(visibile(testo)) <= MAX_CARATTERI:
        return [testo]
    return bg._dividi_testo(testo, MAX_CARATTERI)    # i ** contano: le parti escono un po' più corte


def emoji(emoji_id, fallback):
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def emoji_iniziali(notizia):
    """Come sul canale: faccia del giocatore Juve + tono, oppure tono + bandiera."""
    fallback, emoji_id = TONI.get(notizia.get("tono"), TONI["nuova"])
    tono = emoji(emoji_id, fallback)
    chi = _semplice(notizia.get("protagonista"))
    if chi:
        for nome, (bandiera, faccia) in FACCE.items():
            if nome in chi or nome.split()[-1] == chi.split()[-1]:
                return emoji(faccia, bandiera) + tono
    nazione = str(notizia.get("nazione") or "").strip().upper()
    if chi and not notizia.get("protagonista_juve") and re.fullmatch(r"[A-Z]{2}", nazione):
        bandiera = "".join(chr(0x1F1E6 + ord(c) - 65) for c in nazione)
        return tono + (emoji(BANDIERE[nazione], bandiera) if nazione in BANDIERE else bandiera)
    return tono


def post(testo, persona, iniziali):
    foto = emoji(FOTO.get(_semplice(persona), FOTO_GENERICA), "📲")
    nome = html.escape(persona, quote=False)
    # un solo paragrafo con righe vuote come sul canale (tra due <p> Telegram non lascia spazio);
    # la foto fuori dal corsivo, altrimenti non si vede
    return (f"<p>{iniziali} {grassetto(testo)}<br><br>"
            f"{foto}<i> {nome} - YouTube</i><br><br>"
            f"{FIRMA_JR}</p>")


def prepara(notizie):
    """Post pronti per il canale, un argomento dopo l'altro. Voci con lo stesso argomento si uniscono
    (Gemini a volte lo divide lo stesso); una parte oltre il limite si divide tra una frase e l'altra."""
    argomenti = {}
    for notizia in notizie:
        parti = notizia.get("parti") or [notizia.get("testo", "")]
        chiave = _chiave(notizia.get("argomento")) or f"#{len(argomenti)}"
        voce = argomenti.setdefault(chiave, {**notizia, "parti": [], "riscontro": []})
        voce["parti"] += [t for t in (_pulisci(p) for p in parti) if t]
        if notizia.get("riscontro"):
            voce["riscontro"].append(" ".join(str(notizia["riscontro"]).split()))
    pronti = []
    for voce in argomenti.values():
        if not voce["parti"]:
            continue
        persona = voce.get("persona") or "YouTube"
        iniziali = emoji_iniziali(voce)
        print(f"  [{voce.get('minuto', '?')}] {voce.get('argomento', '')} — {persona}, "
              f"tono {voce.get('tono')}, {voce.get('protagonista') or '-'} {voce.get('nazione') or ''}")
        for frase in voce["riscontro"]:
            print(f"      riscontro: «{frase[:600]}»")
        for parte in voce["parti"]:
            print(f"      {visibile(parte)}")
            pronti += [post(p, persona, iniziali) for p in _dividi(parte)]
    return pronti


def intestazione(persona):
    return f"<h2>{emoji(YOUTUBE_EMOJI_ID, '🖥')} {html.escape(persona, quote=False)} - YouTube</h2>"


def invia(posts, url, info):
    """Solo i post, pronti da inoltrare sul canale. Senza notizie un avviso, per sapere che è finito."""
    if not posts:
        titolo = html.escape(info.get("titolo") or url, quote=False)
        return bg._post_telegram_rich(
            f"{intestazione(info.get('canale') or 'YouTube')}<p>Nessuna notizia sulla Juventus in "
            f'<a href="{html.escape(url)}">{titolo}</a>.</p>') is not None
    ok = True
    for i, testo in enumerate(posts):
        if i:
            time.sleep(1)
        ok = (bg._post_telegram_rich(testo) is not None) and ok
    return ok


def avvisa_errore(url, errore):
    bg._post_telegram_rich(
        f"<h2>⚠️ Notizie da YouTube</h2><p>Non sono riuscito a leggere il video: "
        f"{html.escape(str(errore)[:300], quote=False)}</p><p>{html.escape(url)}</p>"
    )


def estrai_con_attesa(url, info, attesa=None, pausa=None, dormi=time.sleep):
    """Un video appena pubblicato può non essere ancora leggibile: si riprova ogni PAUSA secondi
    per ATTESA secondi in tutto (di solito entro 10 minuti è pronto). Avviso sul bot al primo intoppo.
    Quota giornaliera di Gemini finita: inutile aspettare."""
    attesa = ATTESA if attesa is None else attesa
    pausa = PAUSA if pausa is None else pausa
    inizio, avvisato = time.monotonic(), False
    while True:
        try:
            return estrai(url, info)
        except Exception as e:
            passati = time.monotonic() - inizio
            if re.search(r"PerDay|per day|daily", str(e), re.IGNORECASE) or passati + pausa > attesa:
                raise
            print(f"Video non ancora leggibile ({type(e).__name__}): riprovo tra {pausa}s.")
            if not avvisato:
                avvisato = True
                bg._post_telegram_rich(
                    f"{intestazione(info.get('canale') or 'Video')}<p>Il video non è ancora leggibile "
                    f"(appena pubblicato?): riprovo ogni {pausa // 60} minuti per {attesa // 60} minuti.</p>")
            dormi(pausa)


def main(link):
    try:
        url = video_url(link)
    except ValueError as e:
        avvisa_errore(str(link), e)
        print(e)
        return 1
    info = info_video(url)
    print(f"Video: {info['titolo'] or url} ({info['canale'] or 'canale sconosciuto'})")
    try:
        notizie = estrai_con_attesa(url, info)
    except Exception as e:
        print(f"Gemini non è riuscito a leggere il video: {e}")
        avvisa_errore(url, e)
        return 1
    print(f"Notizie trovate: {len(notizie)}")
    return 0 if invia(prepara(notizie), url, info) else 1


if __name__ == "__main__":
    sys.exit(main(os.getenv("YOUTUBE_URL") or (sys.argv[1] if len(sys.argv) > 1 else "")))
