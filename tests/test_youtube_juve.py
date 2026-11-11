import html
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("GEMINI_API_KEY", "test-key")

import youtube_juve as yj


class LinkTests(unittest.TestCase):
    def test_forme_del_link(self):
        atteso = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        for link in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s",
                     "https://youtu.be/dQw4w9WgXcQ?si=abc",
                     "https://youtube.com/shorts/dQw4w9WgXcQ",
                     "https://www.youtube.com/live/dQw4w9WgXcQ?feature=share",
                     "Guarda qui https://m.youtube.com/watch?v=dQw4w9WgXcQ"):
            self.assertEqual(yj.video_url(link), atteso)
        with self.assertRaises(ValueError):
            yj.video_url("https://www.instagram.com/p/ABC/")

    def test_persona_dei_canali_noti(self):
        self.assertEqual(yj.persona_nota({"canale_url": "https://www.youtube.com/@FabrizioRomanoItaliano"}),
                         "Fabrizio Romano")
        self.assertIsNone(yj.persona_nota({"canale_url": "https://www.youtube.com/@Altro"}))


class TestiTests(unittest.TestCase):
    def test_lunghe_divise_corte_intere(self):
        corta = "La Juventus ha chiesto informazioni per il centrocampista."
        lunga = ("La Juventus segue da settimane il difensore e ha già avuto un primo contatto con l'agente. "
                 "La richiesta del club è di 30 milioni di euro, cifra che i bianconeri considerano alta. "
                 "Si lavora a una formula con prestito e obbligo di riscatto legato alle presenze.")
        testi = yj.prepara([{"testo": corta, "minuto": "01:00"}, {"testo": lunga, "minuto": "02:00"}])
        self.assertEqual(html.unescape(testi[0]), corta)
        self.assertGreater(len(testi), 2)
        self.assertTrue(all(len(html.unescape(t)) <= yj.MAX_CARATTERI for t in testi))
        self.assertIn("30M€", "".join(testi))

    def test_stesso_argomento_unito(self):
        testi = yj.prepara([
            {"argomento": "Sorloth", "minuto": "08:45", "parti": ["Accordo verbale con il giocatore."]},
            {"argomento": "Vlahovic", "minuto": "05:00", "parti": ["Rinnovo fermo."]},
            {"argomento": "sorloth", "minuto": "09:35", "parti": ["Manca l'accordo con l'Atletico."]},
        ])
        self.assertEqual([html.unescape(t) for t in testi],
                         ["Accordo verbale con il giocatore.", "Manca l'accordo con l'Atletico.", "Rinnovo fermo."])

    def test_invio(self):
        inviati = []
        with patch.object(yj.bg, "_post_telegram_rich", side_effect=lambda h: inviati.append(h) or 1), \
                patch.object(yj.time, "sleep"):
            ok = yj.invia("Romeo Agresti", ["Prima notizia.", "Seconda notizia."],
                          "https://www.youtube.com/watch?v=dQw4w9WgXcQ", {"titolo": "Live mercato"})
        self.assertTrue(ok)
        self.assertEqual(len(inviati), 3)                   # link del video + 2 notizie
        self.assertIn("Romeo Agresti - YouTube", inviati[1])
        self.assertIn("2 notizie", inviati[0])

    def test_nessuna_notizia(self):
        inviati = []
        with patch.object(yj.bg, "_post_telegram_rich", side_effect=lambda h: inviati.append(h) or 1):
            yj.invia("Fabrizio Romano", [], "https://www.youtube.com/watch?v=dQw4w9WgXcQ", {"titolo": ""})
        self.assertEqual(len(inviati), 1)
        self.assertIn("nessuna notizia", inviati[0])


class AttesaTests(unittest.TestCase):
    def test_riprova_finche_il_video_e_pronto(self):
        risposte = [RuntimeError("400 video not ready"), RuntimeError("400 video not ready"), ("Romeo Agresti", [])]
        def estrai(url, info):
            r = risposte.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        avvisi = []
        with patch.object(yj, "estrai", side_effect=estrai), \
                patch.object(yj.bg, "_post_telegram_rich", side_effect=lambda h: avvisi.append(h) or 1):
            out = yj.estrai_con_attesa("u", {"canale": "Romeo Agresti"}, attesa=900, pausa=120, dormi=lambda s: None)
        self.assertEqual(out, ("Romeo Agresti", []))
        self.assertEqual(len(avvisi), 1)                 # un solo avviso, non uno per tentativo

    def test_quota_giornaliera_non_aspetta(self):
        with patch.object(yj, "estrai", side_effect=RuntimeError("429 GenerateRequestsPerDay")), \
                patch.object(yj.bg, "_post_telegram_rich") as post:
            with self.assertRaises(RuntimeError):
                yj.estrai_con_attesa("u", {}, attesa=900, pausa=120, dormi=lambda s: None)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
