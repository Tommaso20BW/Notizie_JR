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
            "description": "Notizie sulla Juventus, nell'ordine in cui escono nel video. Vuota se non se ne parla.",
            "items": {
                "type": "object",
                "properties": {
                    "testo": {"type": "string", "description": "Notizia pronta, testo semplice, al massimo 240 caratteri."},
                    "minuto": {"type": "string", "description": "Minuto del video in cui se ne parla (mm:ss)."},
                },
                "required": ["testo", "minuto"],
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
- Ogni notizia al massimo {MAX_CARATTERI} caratteri (spazi compresi).
- Una notizia per argomento. Se un argomento non sta in {MAX_CARATTERI} caratteri, dividilo in più
  notizie consecutive, ognuna comprensibile da sola. Non dividere se ci sta in una sola, e non unire
  argomenti diversi nella stessa notizia.
- Non ripetere la stessa informazione in più notizie: se nel video un concetto torna più volte,
  mettilo una volta sola, nella notizia a cui appartiene.
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


def prepara(notizie):
    """Testi puliti per Telegram; quelli oltre il limite si dividono tra una frase e l'altra."""
    pronte = []
    for notizia in notizie:
        testo = bg._sanitizza_markup(notizia.get("testo", ""))
        if not testo:
            continue
        print(f"  [{notizia.get('minuto', '?')}] {html.unescape(testo)}")
        pronte += bg._dividi_testo(testo, MAX_CARATTERI)
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
        persona, notizie = estrai(url, info)
    except Exception as e:
        print(f"Gemini non è riuscito a leggere il video: {e}")
        avvisa_errore(url, e)
        return 1
    print(f"Persona: {persona}. Notizie trovate: {len(notizie)}")
    testi = prepara(notizie)
    return 0 if invia(persona, testi, url, info) else 1


if __name__ == "__main__":
    sys.exit(main(os.getenv("YOUTUBE_URL") or (sys.argv[1] if len(sys.argv) > 1 else "")))
