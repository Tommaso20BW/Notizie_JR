"""YouTube -> notizie Juventus sul bot JR.

Si avvia dal workflow "Notizie da YouTube" (a mano o dal Comando Rapido dell'iPhone) con il link di
un video. Gemini guarda il video direttamente dal link (niente sottotitoli da scaricare), tiene solo
quello che riguarda la Juventus o giocatori legati alla Juve e lo riscrive come notizie pronte da
pubblicare: al massimo 240 caratteri ciascuna, divise solo quando serve. Le notizie arrivano sul bot
JR (stessa chat di Notizie quotidiani), una per messaggio, con in testa "Nome - YouTube".
"""
import html
import os
import re
import sys
import time

import requests

import bot_giornali as bg

MAX_CARATTERI = int(os.getenv("YOUTUBE_MAX_CARATTERI", "240"))
ATTESA = int(os.getenv("YOUTUBE_ATTESA", "900"))     # quanto aspettare un video non ancora leggibile (s)
PAUSA = int(os.getenv("YOUTUBE_PAUSA", "120"))       # ogni quanto riprovare (s)
YOUTUBE_EMOJI_ID = "5974578526991425004"       # la stessa del bot notizie per "YouTube - ..."
LINK_EMOJI_ID = "5271604874419647061"

# Canali seguiti di più: nome pulito della persona (il nome del canale può essere diverso).
CANALI = {
    "@fabrizioromanoitaliano": "Fabrizio Romano",
    "@fabrizioromano": "Fabrizio Romano",
    "@romeoagresti": "Romeo Agresti",
}

_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})")

SCHEMA = {
    "type": "object",
    "properties": {
        "persona": {
            "type": "string",
            "description": "Nome e cognome di chi parla nel video (il giornalista/autore del canale).",
        },
        "notizie": {
            "type": "array",
            "description": ("Una voce per ARGOMENTO (es. un giocatore o una trattativa), nell'ordine del "
                            "video. Vuota se non si parla della Juventus."),
            "items": {
                "type": "object",
                "properties": {
                    "argomento": {"type": "string", "description": "Di cosa parla, in poche parole (es. 'Sorloth')."},
                    "minuto": {"type": "string", "description": "Minuto del video in cui inizia (mm:ss)."},
                    "parti": {
                        "type": "array",
                        "description": ("La notizia: di norma UNA sola parte. Più parti solo se le informazioni "
                                        "importanti non stanno in 240 caratteri nemmeno condensando."),
                        "items": {"type": "string", "description": "Testo semplice, al massimo 240 caratteri."},
                    },
                },
                "required": ["argomento", "minuto", "parti"],
            },
        },
    },
    "required": ["persona", "notizie"],
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


def persona_nota(info):
    handle = info.get("canale_url", "").rstrip("/").rsplit("/", 1)[-1].lower()
    return CANALI.get(handle)


def prompt(info, persona):
    chi = persona or info.get("canale") or "l'autore del video"
    return f"""
Guarda e ascolta questo video YouTube ("{info.get('titolo', '')}", canale {info.get('canale', '')}).
Parla {chi}. Trasforma in notizie per un canale Telegram sulla Juventus SOLO le parti che riguardano
la Juventus: club, allenatore, dirigenti, squadra, giocatori della Juventus (anche in prestito) e
giocatori accostati alla Juventus o legati a lei (mercato, rinnovi, cessioni). Ignora tutto il resto.

Regole:
- Scrivi in italiano, anche se il video è in un'altra lingua.
- Stile notizia: frasi brevi e chiare, in terza persona, come un post di un canale di notizie.
  Non iniziare con il nome di chi parla: è già nell'intestazione del messaggio.
- Riporta solo quello che viene detto nel video, senza aggiungere conoscenze esterne.
- Mantieni il grado di certezza: "potrebbe", "valuta", "c'è un contatto" non sono fatti certi.
- UNA NOTIZIA PER ARGOMENTO. Un argomento è un giocatore, una trattativa, un tema (es. tutto quello
  che si dice su Sorloth e la Juve è UN argomento, anche se se ne parla in momenti diversi del video).
  Raccogli tutto quello che riguarda lo stesso argomento in una sola voce.
- Ogni parte al massimo {MAX_CARATTERI} caratteri (spazi compresi). Prima condensa: togli ripetizioni,
  giri di parole e dettagli secondari, e fai stare l'argomento in UNA parte. Usa una seconda parte
  solo se restano informazioni importanti che non ci stanno; ogni parte deve reggersi da sola.
- Argomenti diversi vanno in voci diverse: non unirli. Non spezzare un argomento in più voci.
- Non ripetere la stessa informazione: un concetto detto più volte nel video va scritto una volta.
- Importi in milioni di euro in forma compatta: "30 milioni di euro" -> "30M€", "tra 40 e 50" -> "40-50M€".
- Niente emoji, hashtag, titoli, Markdown o HTML.
- Se nel video non si parla della Juventus, restituisci una lista vuota.
- "persona": il nome di chi parla{f' (è {persona})' if persona else ''}.
""".strip()


def estrai(url, info):
    from google.genai import types
    persona = persona_nota(info)
    video = types.Part(file_data=types.FileData(file_uri=url))
    risposta = bg._genera_json(video, prompt(info, persona), schema=SCHEMA, intero=True,
                               config_extra={"media_resolution": "MEDIA_RESOLUTION_LOW"})
    persona = persona or str(risposta.get("persona") or "").strip() or info.get("canale") or "YouTube"
    return persona, risposta.get("notizie") or []


def _chiave(argomento):
    return " ".join(re.sub(r"[^\w ]", " ", str(argomento or "").lower()).split())


def prepara(notizie):
    """Testi puliti per Telegram, un argomento dopo l'altro. Voci con lo stesso argomento si uniscono
    (Gemini a volte lo divide lo stesso); una parte oltre il limite si divide tra una frase e l'altra."""
    argomenti = {}
    for notizia in notizie:
        parti = notizia.get("parti") or [notizia.get("testo", "")]
        chiave = _chiave(notizia.get("argomento")) or f"#{len(argomenti)}"
        voce = argomenti.setdefault(chiave, {"argomento": notizia.get("argomento", ""),
                                            "minuto": notizia.get("minuto", "?"), "parti": []})
        voce["parti"] += [t for t in (bg._sanitizza_markup(p) for p in parti) if t]
    pronte = []
    for voce in argomenti.values():
        if not voce["parti"]:
            continue
        print(f"  [{voce['minuto']}] {voce['argomento']}: {len(voce['parti'])} parte/i")
        for parte in voce["parti"]:
            print(f"      {html.unescape(parte)}")
            pronte += bg._dividi_testo(parte, MAX_CARATTERI)
    return pronte


def intestazione(persona):
    emoji = f'<tg-emoji emoji-id="{YOUTUBE_EMOJI_ID}">🖥</tg-emoji>'
    return f"<h2>{emoji} {html.escape(persona, quote=False)} - YouTube</h2>"


def invia(persona, testi, url, info):
    link = f'<tg-emoji emoji-id="{LINK_EMOJI_ID}">🔗</tg-emoji>'
    titolo = html.escape(info.get("titolo") or "video", quote=False)
    quante = f"{len(testi)} notizi{'a' if len(testi) == 1 else 'e'}" if testi else "nessuna notizia sulla Juventus"
    ok = bg._post_telegram_rich(
        f'{intestazione(persona)}<p>{link} <a href="{html.escape(url)}">{titolo}</a></p><footer>{quante}</footer>'
    ) is not None
    for testo in testi:
        time.sleep(1)
        ok = (bg._post_telegram_rich(f"{intestazione(persona)}<p>{testo}</p>") is not None) and ok
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
        persona, notizie = estrai_con_attesa(url, info)
    except Exception as e:
        print(f"Gemini non è riuscito a leggere il video: {e}")
        avvisa_errore(url, e)
        return 1
    print(f"Persona: {persona}. Notizie trovate: {len(notizie)}")
    testi = prepara(notizie)
    return 0 if invia(persona, testi, url, info) else 1


if __name__ == "__main__":
    sys.exit(main(os.getenv("YOUTUBE_URL") or (sys.argv[1] if len(sys.argv) > 1 else "")))
