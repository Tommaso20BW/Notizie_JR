import os
import re
import unittest
from unittest.mock import patch

os.environ.setdefault("GEMINI_API_KEY", "test-key")

import youtube_juve as yj


_cancellati = []


def setUpModule():
    """Nessuna chiamata vera a Telegram per cancellare gli avvisi."""
    global _patch_cancella
    _patch_cancella = patch.object(yj, "_cancella", side_effect=_cancellati.append)
    _patch_cancella.start()


def tearDownModule():
    _patch_cancella.stop()


class AvvisiSuperatiTests(unittest.TestCase):
    def setUp(self):
        _cancellati.clear()
        yj._avvisi.clear()

    def test_gemini_risponde_e_toglie_subito_il_riprovo(self):
        risposte = [RuntimeError("503 UNAVAILABLE"), []]
        def estrai(url, info):
            r = risposte.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        with patch.object(yj, "estrai", side_effect=estrai),                 patch.object(yj.bg, "_post_telegram_rich", return_value=42):
            yj.estrai_con_attesa("u", {}, attesa=900, pausa=120, dormi=lambda s: None)
        self.assertEqual(_cancellati, [42])
        self.assertEqual(yj._avvisi, [])

    def test_sottotitoli_pronti_toglie_subito_l_attesa(self):
        stati = [False, True]
        inviati = []
        with patch.object(yj.bg, "_post_telegram_rich", side_effect=lambda h, **k: inviati.append(h) or 9):
            yj.aspetta_sottotitoli("u", {}, attesa=900, pausa=120, dormi=lambda s: None,
                                   controlla=lambda u: stati.pop(0))
        self.assertEqual(len(inviati), 1)                # niente "sottotitoli pronti"
        self.assertEqual(_cancellati, [9])


def testo_visibile(post):
    """Solo il corpo della notizia, senza tag."""
    corpo = re.search(r"<p>(.*?)<br>", post).group(1)
    corpo = re.sub(r"<tg-emoji[^>]*>.*?</tg-emoji>", "", corpo)
    return yj.html.unescape(re.sub(r"<[^>]+>", "", corpo)).strip()


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


class PersonaTests(unittest.TestCase):
    def test_canale_di_romano_anche_moretto(self):
        info = {"canale_url": "https://www.youtube.com/@FabrizioRomanoItaliano", "canale": "Fabrizio Romano Italiano"}
        possibili = yj.persone_note(info)
        self.assertEqual(possibili, ["Fabrizio Romano", "Matteo Moretto"])
        self.assertEqual(yj.nome_persona("Moretto", possibili, info), "Matteo Moretto")
        self.assertEqual(yj.nome_persona("fabrizio romano", possibili, info), "Fabrizio Romano")
        self.assertEqual(yj.nome_persona("", possibili, info), "Fabrizio Romano")
        self.assertIn("Matteo Moretto", yj.prompt(info, possibili))

    def test_canale_sconosciuto(self):
        info = {"canale_url": "https://www.youtube.com/@Altro", "canale": "Altro canale"}
        self.assertEqual(yj.persone_note(info), [])
        self.assertEqual(yj.nome_persona("Luca  Rossi", [], info), "Luca Rossi")
        self.assertEqual(yj.nome_persona("", [], info), "Altro canale")


class FormatoTests(unittest.TestCase):
    def test_post_come_il_canale(self):
        posts = yj.prepara([{
            "argomento": "Sorloth", "minuto": "07:44", "persona": "Matteo Moretto", "tono": "valutazione",
            "protagonista": "Sorloth", "protagonista_juve": False, "nazione": "NO",
            "parti": ["La **Juventus** ha trattato a lungo **Sorloth**, ma mancava l'accordo con l'**Atletico Madrid**."],
        }])
        self.assertEqual(len(posts), 1)
        p = posts[0]
        self.assertTrue(p.startswith('<p><tg-emoji emoji-id="5370724846936267183">🤔</tg-emoji>'
                                     '<tg-emoji emoji-id="5382300771641470186">🇳🇴</tg-emoji> La <b>Juventus</b>'))
        self.assertIn("<b>Atletico Madrid</b>", p)
        self.assertIn('<tg-emoji emoji-id="5785259727248170398">📲</tg-emoji><i> Matteo Moretto - YouTube</i>', p)
        self.assertIn("@Juventus_Reborn</a>", p)
        self.assertNotIn("**", p)

    def test_giocatore_juve_faccia_prima(self):
        iniziali = yj.emoji_iniziali({"tono": "rientro", "protagonista": "Francisco Conceição", "protagonista_juve": True})
        self.assertEqual(iniziali, '<tg-emoji emoji-id="5789811056847102509">🇵🇹</tg-emoji>'
                                   '<tg-emoji emoji-id="5352759161945867747">🔙</tg-emoji>')
        senza = yj.emoji_iniziali({"tono": "boh", "protagonista": "", "nazione": ""})
        self.assertEqual(senza, '<tg-emoji emoji-id="5361979468887893611">🆕</tg-emoji>')
        altra = yj.emoji_iniziali({"tono": "interesse", "protagonista": "Rossi", "nazione": "jp"})
        self.assertTrue(altra.endswith("🇯🇵"))

    @patch.object(yj, "MAX_CARATTERI", 240)
    def test_lunghe_divise_corte_intere(self):
        corta = "La **Juventus** ha chiesto informazioni per il centrocampista."
        lunga = ("La **Juventus** segue da settimane il difensore e ha già avuto un primo contatto con l'agente. "
                 "La richiesta del club è di 30 milioni di euro, cifra che i bianconeri considerano alta. "
                 "Si lavora a una formula con prestito e obbligo di riscatto legato alle presenze.")
        posts = yj.prepara([{"argomento": "a", "parti": [corta], "persona": "X"},
                            {"argomento": "b", "parti": [lunga], "persona": "X"}])
        self.assertEqual(testo_visibile(posts[0]), "La Juventus ha chiesto informazioni per il centrocampista.")
        self.assertGreater(len(posts), 2)
        self.assertTrue(all(len(testo_visibile(p)) <= yj.MAX_CARATTERI for p in posts))
        self.assertIn("30M€", "".join(posts))
        self.assertTrue(all("**" not in p for p in posts))

    def test_stesso_argomento_unito(self):
        posts = yj.prepara([
            {"argomento": "Sorloth", "minuto": "08:45", "parti": ["Accordo verbale con il giocatore."]},
            {"argomento": "Vlahovic", "minuto": "05:00", "parti": ["Rinnovo fermo."]},
            {"argomento": "sorloth", "minuto": "09:35", "parti": ["Manca l'accordo con l'Atletico."]},
        ])
        self.assertEqual([testo_visibile(p) for p in posts],
                         ["Accordo verbale con il giocatore.", "Manca l'accordo con l'Atletico.", "Rinnovo fermo."])


class InvioTests(unittest.TestCase):
    def test_solo_i_post(self):
        inviati = []
        with patch.object(yj.bg, "_post_telegram_rich", side_effect=lambda h, **k: inviati.append(h) or 1), \
                patch.object(yj.time, "sleep"):
            ok = yj.invia(["<p>uno</p>", "<p>due</p>"], "https://www.youtube.com/watch?v=dQw4w9WgXcQ", {"titolo": "Live"})
        self.assertTrue(ok)
        self.assertEqual(inviati, ["<p>uno</p>", "<p>due</p>"])     # niente messaggio col link sopra

    def test_nessuna_notizia(self):
        inviati = []
        with patch.object(yj.bg, "_post_telegram_rich", side_effect=lambda h, **k: inviati.append(h) or 1):
            yj.invia([], "https://www.youtube.com/watch?v=dQw4w9WgXcQ", {"titolo": ""})
        self.assertEqual(len(inviati), 1)
        self.assertIn("Nessuna notizia", inviati[0])


class AttesaTests(unittest.TestCase):
    def test_riprova_finche_il_video_e_pronto(self):
        risposte = [RuntimeError("400 video not ready"), RuntimeError("400 video not ready"), []]
        def estrai(url, info):
            r = risposte.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        avvisi = []
        with patch.object(yj, "estrai", side_effect=estrai), \
                patch.object(yj.bg, "_post_telegram_rich", side_effect=lambda h, **k: avvisi.append(h) or 1):
            out = yj.estrai_con_attesa("u", {"canale": "Romeo Agresti"}, attesa=900, pausa=120, dormi=lambda s: None)
        self.assertEqual(out, [])
        self.assertEqual(len(avvisi), 1)                 # un solo avviso, non uno per tentativo

    def test_quota_giornaliera_non_aspetta(self):
        with patch.object(yj, "estrai", side_effect=RuntimeError("429 GenerateRequestsPerDay")), \
                patch.object(yj.bg, "_post_telegram_rich") as post:
            with self.assertRaises(RuntimeError):
                yj.estrai_con_attesa("u", {}, attesa=900, pausa=120, dormi=lambda s: None)
        post.assert_not_called()


class OpinioniTests(unittest.TestCase):
    def test_opinioni_scartate(self):
        info = {"canale_url": "https://www.youtube.com/@RomeoAgresti", "canale": "Romeo Agresti"}
        notizie = [{"argomento": "Zhegrova", "tipo": "opinione", "testo": "Io lo venderei.", "persona": ""},
                   {"argomento": "Kolo Muani", "tipo": "notizia", "testo": "Spalletti punta su di lui.", "persona": ""},
                   {"argomento": "Vecchio formato", "testo": "Senza tipo resta.", "persona": ""}]
        tenute = yj.solo_notizie(notizie, [], info)
        self.assertEqual([n["argomento"] for n in tenute], ["Kolo Muani", "Vecchio formato"])

    def test_prompt_con_tesserati(self):
        testo = yj.prompt({"titolo": "t", "canale": "c"}, [])
        self.assertIn("Carlo Pinsoglio", testo)
        self.assertIn("OPINIONE", testo)
        self.assertIn("tipo", yj.SCHEMA["properties"]["notizie"]["items"]["required"])


class ModelliTests(unittest.TestCase):
    def test_prima_i_flash(self):
        provati = []
        def genera(model, contents, config):
            provati.append(model)
            raise RuntimeError("400 prova")
        with patch.object(yj.bg, "modelli_disponibili", return_value=["g-3.5-flash-lite", "g-3.1-flash-lite", "g-3.8-flash"]),                 patch.object(yj.bg.client.models, "generate_content", side_effect=genera),                 patch.object(yj.bg, "MAX_CICLI_GEMINI", 1):
            with self.assertRaises(Exception):
                yj.bg._genera_json("v", "p", prima_flash=True)
        self.assertEqual(provati[0], "g-3.8-flash")

    def test_flash_occupati_si_riprovano_prima_dei_lite(self):
        provati = []
        def genera(model, contents, config):
            provati.append(model)
            raise RuntimeError("503 UNAVAILABLE")
        with patch.object(yj.bg, "modelli_disponibili", return_value=["g-3.5-flash-lite", "g-3.8-flash", "g-3.7-flash"]),                 patch.object(yj.bg.client.models, "generate_content", side_effect=genera),                 patch.object(yj.bg.time, "sleep"):
            with self.assertRaises(Exception):
                yj.bg._genera_json("v", "p", prima_flash=True, cicli=3)
        self.assertEqual(provati, ["g-3.8-flash", "g-3.7-flash"] * 2 + ["g-3.8-flash", "g-3.7-flash", "g-3.5-flash-lite"])


class OccupatoTests(unittest.TestCase):
    def test_solo_flash_mai_lite(self):
        provati = []
        def genera(model, contents, config):
            provati.append(model)
            raise RuntimeError("503 UNAVAILABLE")
        with patch.object(yj.bg, "modelli_disponibili", return_value=["g-3.5-flash-lite", "g-3.8-flash"]),                 patch.object(yj.bg.client.models, "generate_content", side_effect=genera),                 patch.object(yj.bg.time, "sleep"):
            with self.assertRaises(Exception):
                yj.bg._genera_json("v", "p", prima_flash=True, solo_flash=True, cicli=3)
        self.assertEqual(provati, ["g-3.8-flash"] * 3)

    def test_occupato_aspetta_poi_si_arrende(self):
        avvisi = []
        orologio = [0]
        def dormi(secondi):
            orologio[0] += secondi
        with patch.object(yj, "estrai", side_effect=RuntimeError("503 UNAVAILABLE")),                 patch.object(yj.time, "monotonic", side_effect=lambda: orologio[0]),                 patch.object(yj.bg, "_post_telegram_rich", side_effect=lambda h, **k: avvisi.append(h) or 1):
            with self.assertRaises(yj.GeminiOccupato):
                yj.estrai_con_attesa("u", {}, attesa=900, pausa=120, dormi=dormi, attesa_gemini=1800)
        self.assertGreaterEqual(orologio[0], 1600)            # ha aspettato quasi 30 minuti, non 15
        self.assertEqual(len(avvisi), 1)
        self.assertIn("Gemini è occupato", avvisi[0])

    def test_avviso_finale_resta(self):
        yj._avvisi.clear()
        with patch.object(yj.bg, "_post_telegram_rich", return_value=7):
            yj.avviso("riprova più tardi", resta=True)
            yj.avviso("attendo")
        self.assertEqual([m for m, _ in yj._avvisi], [7])     # solo il secondo si cancellerà
        yj._avvisi.clear()


if __name__ == "__main__":
    unittest.main()


def _voce(argomento, tipo="notizia"):
    return {"argomento": argomento, "minuto": "01:00", "persona": "Agresti", "tipo": tipo,
            "testo": f"Notizia su **{argomento}**.", "tono": "nuova", "protagonista": argomento,
            "protagonista_juve": True, "nazione": "IT", "riscontro": "..."}


class SupadataTests(unittest.TestCase):
    def test_trascrizione_con_minuti(self):
        class R:
            status_code = 200
            def json(self):
                return {"content": [{"text": "ciao  a tutti", "offset": 65000}, {"text": "Kalulu", "offset": 125500}]}
        with patch.object(yj, "SUPADATA_API_KEY", "k"):
            out = yj.trascrivi_supadata("u", get=lambda *a, **k: R())
        self.assertEqual(out, "[01:05] ciao a tutti\n[02:05] Kalulu")

    def test_lavoro_lungo_aspetta(self):
        risposte = [(202, {"jobId": "j"}), (200, {"status": "active"}),
                    (200, {"status": "completed", "content": "testo intero"})]
        class R:
            def __init__(self, stato, dati):
                self.status_code, self._d, self.text = stato, dati, ""
            def json(self):
                return self._d
        with patch.object(yj, "SUPADATA_API_KEY", "k"):
            out = yj.trascrivi_supadata("u", dormi=lambda s: None, get=lambda *a, **k: R(*risposte.pop(0)))
        self.assertEqual(out, "testo intero")

class ImportanzaTests(unittest.TestCase):
    def test_inutili_scartate(self):
        inutile = {**_voce("Cabal"), "importanza": "inutile"}
        out = yj.solo_notizie([_voce("Kalulu"), inutile, _voce("Mercato", "opinione")], [], {})
        self.assertEqual([n["argomento"] for n in out], ["Kalulu"])
        self.assertIn("importanza", yj.SCHEMA["properties"]["notizie"]["items"]["required"])


class GeminiTestoTests(unittest.TestCase):
    def test_prima_gemini_flash_sul_testo(self):
        with patch.object(yj, "notizie_gemini_testo", return_value=[_voce("Kalulu"), _voce("X", "opinione")]):
            out = yj.estrai_da_trascrizione("u", {}, trascrizione="[00:10] " + "ciao " * 40)
        self.assertEqual([n["argomento"] for n in out], ["Kalulu"])

    def test_supadata_sospeso_si_usa_il_video(self):
        self.assertFalse(yj.SUPADATA_ATTIVO)
        self.assertFalse(hasattr(yj, "GROQ_API_KEY"))


class TimeoutTests(unittest.TestCase):
    def test_youtube_aspetta_al_massimo_90_secondi(self):
        visti = {}
        def genera(video, richiesta, **k):
            visti.update(k["config_extra"])
            return {"notizie": []}
        with patch.object(yj.bg, "_genera_json", side_effect=genera):
            yj.estrai("https://youtu.be/abcdefghijk", {})
        self.assertEqual(visti["http_options"], {"timeout": 90000})
