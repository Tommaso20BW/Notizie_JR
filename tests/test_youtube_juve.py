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


if __name__ == "__main__":
    unittest.main()
