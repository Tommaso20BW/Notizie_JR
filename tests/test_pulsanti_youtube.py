import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("GEMINI_API_KEY", "test-key")

import pulsanti_youtube as pu
import telegram_notifier as tn
import youtube_juve as yj

ADESSO = 1_800_000_000


class Risposta:
    def __init__(self, corpo=None, stato=200):
        self.status_code = stato
        self.content = b"x"
        self._corpo = corpo or {"ok": True, "result": True}
        self.text = json.dumps(self._corpo)

    def json(self):
        return self._corpo


def tocco(update_id, dato, chat="42", data=ADESSO - 60):
    return {"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "data": dato,
        "message": {"message_id": 900 + update_id, "date": data, "chat": {"id": int(chat)}}}}


class Sessione:
    def __init__(self, aggiornamenti):
        self.aggiornamenti = aggiornamenti
        self.chiamate = []

    def post(self, url, data=None, json=None, headers=None, timeout=None):
        self.chiamate.append((url.rsplit("/", 1)[-1], data or json))
        if url.endswith("getUpdates"):
            return Risposta({"ok": True, "result": self.aggiornamenti if "offset" not in data else []})
        if url.endswith("dispatches"):
            return Risposta(stato=204)
        return Risposta()

    def metodi(self, nome):
        return [d for m, d in self.chiamate if m == nome]


class PulsanteNelMessaggio(unittest.TestCase):
    def test_solo_sotto_i_video_youtube(self):
        video = SimpleNamespace(source="YouTube - Fabrizio Romano", title="Live", summary="",
                                url="https://www.youtube.com/watch?v=3vDhGYP_rLs")
        html = tn.format_article_rich_html(video)
        self.assertIn('<tg-button type="callback_data" style="primary" data="yt:3vDhGYP_rLs">Estrai notizie</tg-button>', html)
        self.assertIn("Apri video", html)
        juve = SimpleNamespace(source="YouTube - Juventus", title="Highlights", summary="",
                               url="https://www.youtube.com/watch?v=3vDhGYP_rLs")
        self.assertNotIn("callback_data", tn.format_article_rich_html(juve))
        self.assertIn("Apri video", tn.format_article_rich_html(juve))
        articolo = SimpleNamespace(source="Tuttosport", title="T", summary="", url="https://www.tuttosport.com/x")
        self.assertNotIn("callback_data", tn.format_article_rich_html(articolo))


class TocchiTests(unittest.TestCase):
    def setUp(self):
        pu._fatti.clear()

    def controlla(self, sessione):
        return pu.controlla(token="T", chat_id="42", gh_token="G", repo="u/r", session=sessione, adesso=ADESSO)

    def test_tocco_avvia_il_workflow(self):
        s = Sessione([tocco(10, "yt:3vDhGYP_rLs")])
        self.assertEqual(self.controlla(s), 1)
        avvio = s.metodi("dispatches")[0]
        self.assertEqual(avvio["inputs"], {"url": "https://www.youtube.com/watch?v=3vDhGYP_rLs", "messaggio": "910"})
        self.assertIn("Avviato", s.metodi("answerCallbackQuery")[0]["text"])
        self.assertEqual(s.metodi("getUpdates")[-1]["offset"], "11")          # segnato come letto

    def test_secondo_tocco_stesso_video(self):
        self.controlla(Sessione([tocco(10, "yt:3vDhGYP_rLs")]))
        s = Sessione([tocco(11, "yt:3vDhGYP_rLs")])
        self.assertEqual(self.controlla(s), 0)
        self.assertFalse(s.metodi("dispatches"))
        self.assertIn("Già avviato", s.metodi("answerCallbackQuery")[0]["text"])

    def test_tocchi_di_livescore_restano_a_lui(self):
        s = Sessione([tocco(10, "yt:3vDhGYP_rLs"), tocco(11, "kit:401:home"), tocco(12, "yt:dQw4w9WgXcQ")])
        self.assertEqual(self.controlla(s), 2)
        self.assertEqual(s.metodi("getUpdates")[-1]["offset"], "11")          # non oltre la maglia
        self.assertEqual(len(s.metodi("answerCallbackQuery")), 2)             # alla maglia non risponde

    def test_maglie_vecchie_si_possono_segnare(self):
        s = Sessione([tocco(10, "kit:401:home", data=ADESSO - 7 * 3600), tocco(11, "yt:3vDhGYP_rLs")])
        self.controlla(s)
        self.assertEqual(s.metodi("getUpdates")[-1]["offset"], "12")

    def test_altra_chat_ignorata(self):
        s = Sessione([tocco(10, "yt:3vDhGYP_rLs", chat="7")])
        self.assertEqual(self.controlla(s), 0)
        self.assertFalse(s.metodi("dispatches"))


class SottotitoliTests(unittest.TestCase):
    def test_aspetta_finche_ci_sono(self):
        stati = [False, False, True]
        avvisi = []
        with patch.object(yj.bg, "_post_telegram_rich", side_effect=lambda h, **k: avvisi.append(h) or 1):
            ok = yj.aspetta_sottotitoli("u", {}, attesa=1800, pausa=120, dormi=lambda s: None,
                                        controlla=lambda u: stati.pop(0))
        self.assertTrue(ok)
        self.assertEqual(len(avvisi), 2)                  # "non ancora pubblicati" e "pronti"
        self.assertIn("non ancora pubblicati", avvisi[0])
        self.assertIn("pronti", avvisi[1])

    def test_gia_pronti_nessun_avviso(self):
        with patch.object(yj.bg, "_post_telegram_rich") as post:
            self.assertTrue(yj.aspetta_sottotitoli("u", {}, controlla=lambda u: True))
        post.assert_not_called()

    def test_non_verificabili_si_va_avanti(self):
        with patch.object(yj.bg, "_post_telegram_rich") as post:
            self.assertIsNone(yj.aspetta_sottotitoli("u", {}, controlla=lambda u: None))
        post.assert_not_called()

    def test_tempo_scaduto(self):
        avvisi = []
        with patch.object(yj.bg, "_post_telegram_rich", side_effect=lambda h, **k: avvisi.append(h) or 1), \
                patch.object(yj.time, "monotonic", side_effect=lambda: orologio[0]):
            orologio = [0]
            def dormi(secondi):
                orologio[0] += 1700               # dopo il primo controllo il tempo è quasi finito
            ok = yj.aspetta_sottotitoli("u", {}, attesa=1800, pausa=120, dormi=dormi,
                                        controlla=lambda u: False)
        self.assertFalse(ok)
        self.assertIn("ancora assenti", avvisi[-1])


class CancellaAvvisiTests(unittest.TestCase):
    def test_avvisi_cancellati_dopo_5_minuti(self):
        yj._avvisi.clear()
        with patch.object(yj.bg, "_post_telegram_rich", side_effect=[101, 102]), \
                patch.object(yj.time, "monotonic", side_effect=[0, 200]):
            yj.avviso("⏳ attendo")
            yj.avviso("✅ pronti")
        attese, cancellati = [], []
        orologio = iter([250, 500])
        with patch.object(yj.requests, "post", side_effect=lambda url, json, timeout: cancellati.append(json["message_id"]) or SimpleNamespace(ok=True)):
            yj.cancella_avvisi(dormi=attese.append, adesso=lambda: next(orologio))
        self.assertEqual(cancellati, [101, 102])
        self.assertEqual(attese, [50])                      # il primo ha 250s: mancano 50s; il secondo ne ha 300
        self.assertEqual(yj._avvisi, [])


if __name__ == "__main__":
    unittest.main()
