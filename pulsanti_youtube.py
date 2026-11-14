"""Pulsante "Estrai notizie" sotto i video YouTube del bot JR.

Il worker di Notizie Web (sempre acceso, un giro ogni 15 secondi) chiama controlla(): legge i tocchi
(callback "yt:<id del video>"), avvia il workflow "Notizie da YouTube" con il link del video e
risponde al tocco.

Lo stesso bot riceve anche i tocchi di LiveScore (scelta della maglia durante le partite). Telegram
segna come letti gli aggiornamenti solo quando si chiede da un certo punto in poi (offset): qui si
segnano letti soltanto i nostri tocchi e quelli vecchi di altri, mai quelli recenti di LiveScore,
che restano a lui.
"""
import json
import os
import time

import requests

PREFISSO = "yt:"
WORKFLOW = "youtube_juve.yml"
VECCHIO = 6 * 3600          # tocchi di altri più vecchi di così: abbandonati, si possono segnare letti
RIPETIZIONE = 60            # stesso video toccato due volte nel giro di un minuto (doppio tocco): una volta sola.
                            # Dopo si può rifare quando si vuole.

_fatti = {}                 # id del video -> quando è stato avviato (in questo worker)


def _telegram(token, metodo, dati, session):
    r = session.post(f"https://api.telegram.org/bot{token}/{metodo}", data=dati, timeout=15)
    corpo = r.json() if r.content else {}
    if not corpo.get("ok"):
        raise RuntimeError(f"{metodo}: {corpo.get('description') or r.status_code}")
    return corpo.get("result")


def _rispondi(token, callback_id, testo, session):
    try:
        _telegram(token, "answerCallbackQuery", {"callback_query_id": callback_id, "text": testo}, session)
    except Exception as e:            # tocco troppo vecchio per rispondere: pazienza
        print(f"[PULSANTI] risposta al tocco non inviata: {e}")


def avvia(video_id, messaggio_id, gh_token, repo, session):
    r = session.post(
        f"https://api.github.com/repos/{repo}/actions/workflows/{WORKFLOW}/dispatches",
        headers={"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"},
        json={"ref": "main", "inputs": {"url": f"https://www.youtube.com/watch?v={video_id}",
                                        "messaggio": str(messaggio_id or "")}},
        timeout=20,
    )
    if r.status_code != 204:
        raise RuntimeError(f"avvio workflow: {r.status_code} {r.text[:200]}")


def _offset(aggiornamenti, nostri, adesso):
    """Fin dove si possono segnare letti: i nostri e quelli vecchi di altri, fermandosi al primo
    tocco recente di qualcun altro (LiveScore)."""
    offset = None
    for agg in sorted(aggiornamenti, key=lambda a: a["update_id"]):
        cb = agg.get("callback_query") or {}
        data = (cb.get("message") or {}).get("date") or 0
        if agg["update_id"] in nostri or adesso - data > VECCHIO:
            offset = agg["update_id"] + 1
        else:
            break
    return offset


def controlla(token=None, chat_id=None, gh_token=None, repo=None, session=None, adesso=None):
    """Un giro: legge i tocchi, avvia i workflow. Restituisce quanti ne ha avviati."""
    token = token or os.getenv("TELEGRAM_TOKEN")
    chat_id = str(chat_id or os.getenv("CHAT_ID") or "")
    gh_token = gh_token or os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    repo = repo or os.getenv("GITHUB_REPOSITORY") or "Tommaso20BW/Notizie_JR"
    session = session or requests
    adesso = adesso or time.time()
    if not token or not gh_token:
        return 0
    # Senza offset: si legge senza segnare niente come letto.
    aggiornamenti = _telegram(token, "getUpdates", {
        "timeout": "0", "allowed_updates": json.dumps(["callback_query"])}, session) or []
    nostri, avviati = set(), 0
    for agg in aggiornamenti:
        cb = agg.get("callback_query") or {}
        dato = str(cb.get("data") or "")
        if not dato.startswith(PREFISSO):
            continue
        nostri.add(agg["update_id"])
        messaggio = cb.get("message") or {}
        if chat_id and str((messaggio.get("chat") or {}).get("id")) != chat_id:
            _rispondi(token, cb.get("id"), "Messaggio non valido", session)
            continue
        video_id = dato[len(PREFISSO):]
        if adesso - _fatti.get(video_id, 0) < RIPETIZIONE:
            _rispondi(token, cb.get("id"), "Già avviato per questo video", session)
            continue
        try:
            avvia(video_id, messaggio.get("message_id"), gh_token, repo, session)
        except Exception as e:
            print(f"[PULSANTI] {video_id}: {e}")
            _rispondi(token, cb.get("id"), "Non sono riuscito ad avviare l'estrazione, riprova", session)
            continue
        _fatti[video_id] = adesso
        avviati += 1
        print(f"[PULSANTI] estrazione avviata per {video_id}")
        _rispondi(token, cb.get("id"), "Avviato: controllo i sottotitoli e poi estraggo le notizie", session)
    offset = _offset(aggiornamenti, nostri, adesso)
    if offset is not None:
        _telegram(token, "getUpdates", {"offset": str(offset), "timeout": "0", "limit": "1",
                                        "allowed_updates": json.dumps(["callback_query"])}, session)
    return avviati
