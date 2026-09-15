"""Runner Rich Messages per bot_giornali.py.

Non modifica la logica di estrazione, verifica, Dropbox o suddivisione delle
notizie. Sostituisce esclusivamente il trasporto Telegram durante l'esecuzione.
Se sendRichMessage non riesce, la singola parte viene inviata con sendMessage.
"""

from __future__ import annotations

import os
import time

import requests

import bot_giornali as core


_ORIGINAL_SEND_TO_TELEGRAM = core.send_to_telegram


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"", "0", "false", "no", "off"}


def _message_id(response: requests.Response) -> int | None:
    try:
        data = response.json()
    except ValueError:
        return None

    value = (data.get("result") or {}).get("message_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _retry_after(response: requests.Response) -> int:
    try:
        value = response.json().get("parameters", {}).get("retry_after", 30)
        return max(int(value), 1)
    except (ValueError, TypeError):
        return 30


def _post_telegram(
    method: str,
    payload: dict,
    *,
    label: str,
) -> int | None:
    url = f"https://api.telegram.org/bot{core.TELEGRAM_TOKEN}/{method}"

    for attempt in range(5):
        try:
            response = requests.post(url, json=payload, timeout=10)

            if response.ok:
                message_id = _message_id(response)
                if message_id is None:
                    print(
                        f"Telegram {label} ha confermato l'invio senza "
                        "restituire il message_id."
                    )
                return message_id

            if response.status_code == 429:
                retry_after = _retry_after(response)
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


def _post_legacy(text: str, reply_to: int | None = None) -> int | None:
    payload: dict = {
        "chat_id": core.CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_to is not None:
        payload["reply_parameters"] = {
            "message_id": reply_to,
            "allow_sending_without_reply": True,
        }
    return _post_telegram("sendMessage", payload, label="legacy")


def _post_rich(rich_html: str, reply_to: int | None = None) -> int | None:
    payload: dict = {
        "chat_id": core.CHAT_ID,
        "rich_message": {
            "html": rich_html,
        },
    }
    if reply_to is not None:
        payload["reply_parameters"] = {
            "message_id": reply_to,
            "allow_sending_without_reply": True,
        }
    return _post_telegram("sendRichMessage", payload, label="Rich Message")


def send_to_telegram(news_list):
    """Stessa consegna di bot_giornali, con Rich Message e fallback legacy."""
    if not _env_flag("TELEGRAM_RICH_MESSAGES", default=False):
        return _ORIGINAL_SEND_TO_TELEGRAM(news_list)

    all_sent = True

    for news in news_list:
        clean = core._sanitizza_markup(news.get("testo", ""))
        source = core._normalizza_fonte(news.get("fonte", ""))
        config = core.FONTI_TELEGRAM.get(source)

        if not clean or config is None:
            print("Notizia saltata: testo vuoto o fonte non valida.")
            all_sent = False
            continue

        source_name = config["nome"]
        emoji_id = config["emoji_id"]
        fallback_emoji = config["fallback"]
        custom_emoji = (
            f'<tg-emoji emoji-id="{emoji_id}">'
            f"{fallback_emoji}"
            "</tg-emoji>"
        )

        parts = core._dividi_testo(clean)
        reply_to = None

        for number, part in enumerate(parts, start=1):
            body = core.render_testo(part)
            continuation = (
                f" ({number}/{len(parts)})"
                if len(parts) > 1
                else ""
            )

            legacy_text = (
                f"{custom_emoji} <b>{source_name}</b>{continuation}"
                f"\n\n{body}"
            )
            # Stessa gerarchia visiva dei Leak: la fonte è un h2.
            rich_html = (
                f"<h2>{custom_emoji} {source_name}{continuation}</h2>"
                f"<p>{body}</p>"
            )

            message_id = _post_rich(rich_html, reply_to=reply_to)
            if message_id is None:
                print(
                    "[TELEGRAM FALLBACK] sendRichMessage non riuscito; "
                    "uso sendMessage per questa parte."
                )
                message_id = _post_legacy(legacy_text, reply_to=reply_to)

            sent = message_id is not None
            all_sent = sent and all_sent

            if not sent:
                print(
                    f"Invio interrotto per la fonte {source_name}, "
                    f"parte {number}/{len(parts)}."
                )
                break

            reply_to = message_id
            time.sleep(1)

    return all_sent


def main() -> int:
    # Patch solo del trasporto. Tutto il resto rimane nel modulo originale.
    core.send_to_telegram = send_to_telegram

    documents = core.get_pdf_from_dropbox()
    if not documents:
        print("Nessun PDF nuovo. Chiusura.")
        return 0

    for index, document in enumerate(documents):
        core.elabora_documento(document)
        if index < len(documents) - 1:
            print("In attesa di 20 secondi prima del prossimo giornale...")
            time.sleep(20)

    print("Operazione completata.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
