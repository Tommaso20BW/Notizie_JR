import html
import os
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


os.environ.setdefault("GEMINI_API_KEY", "test-key")

import bot_giornali


def _visibile(testo):
    return html.unescape(
        re.sub(r"</?(?:b|t|c)>", "", testo, flags=re.IGNORECASE)
    )


class GeminiTests(unittest.TestCase):
    def _response(self):
        return SimpleNamespace(
            parsed={"notizie": [{"testo": "Notizia"}]},
            candidates=[],
        )

    def test_quota_giornaliera_passa_subito_al_modello_successivo(self):
        models = Mock()
        models.generate_content.side_effect = [
            RuntimeError(
                "429 RESOURCE_EXHAUSTED "
                "GenerateRequestsPerDayPerProjectPerModel-FreeTier"
            ),
            self._response(),
        ]
        client = SimpleNamespace(models=models)

        with (
            patch.object(bot_giornali, "client", client),
            patch.object(bot_giornali, "MODELLI", ["primo", "secondo"]),
            patch.object(bot_giornali.time, "sleep") as sleep,
        ):
            risultato = bot_giornali._genera_json(object(), "prompt")

        self.assertEqual(risultato, [{"testo": "Notizia"}])
        self.assertEqual(models.generate_content.call_count, 2)
        self.assertEqual(
            [call.kwargs["model"] for call in models.generate_content.call_args_list],
            ["primo", "secondo"],
        )
        sleep.assert_not_called()

    def test_quota_temporanea_rispetta_retry_delay(self):
        models = Mock()
        models.generate_content.side_effect = [
            RuntimeError(
                "429 RESOURCE_EXHAUSTED. Please retry in 2.4s."
            ),
            self._response(),
        ]
        client = SimpleNamespace(models=models)

        with (
            patch.object(bot_giornali, "client", client),
            patch.object(bot_giornali, "MODELLI", ["unico"]),
            patch.object(bot_giornali.time, "sleep") as sleep,
        ):
            risultato = bot_giornali._genera_json(object(), "prompt")

        self.assertEqual(risultato, [{"testo": "Notizia"}])
        sleep.assert_called_once_with(4)


class DivisioneMessaggiTests(unittest.TestCase):
    def test_divide_senza_perdere_testo_e_bilancia_i_tag(self):
        testo = (
            "<b>Mario &amp; Luigi</b> discutono con <t>Juventus</t>. "
            "La trattativa prosegue senza interruzioni e con nuovi incontri. "
            "La decisione finale arriverà domani."
        )

        parti = bot_giornali._dividi_testo_markup(testo, limite=55)

        self.assertGreater(len(parti), 1)
        self.assertTrue(
            all(bot_giornali._lunghezza_visibile(parte) <= 55 for parte in parti)
        )
        self.assertTrue(all(bot_giornali._tag_bilanciati(parte) for parte in parti))
        self.assertEqual(
            " ".join(_visibile(parte) for parte in parti),
            _visibile(testo),
        )


class InvioTelegramTests(unittest.TestCase):
    def test_parti_successive_rispondono_al_messaggio_precedente(self):
        risposte = []
        for message_id in (101, 102, 103):
            risposta = Mock(ok=True)
            risposta.json.return_value = {
                "ok": True,
                "result": {"message_id": message_id},
            }
            risposte.append(risposta)

        notizie = [{"testo": "testo", "fonte": "TUTTO"}]
        with (
            patch.object(
                bot_giornali,
                "_dividi_testo_markup",
                return_value=["prima", "seconda", "terza"],
            ),
            patch.object(
                bot_giornali.requests,
                "post",
                side_effect=risposte,
            ) as post,
            patch.object(bot_giornali.time, "sleep"),
        ):
            risultato = bot_giornali.send_to_telegram(notizie)

        self.assertTrue(risultato)
        payload = [call.kwargs["json"] for call in post.call_args_list]
        self.assertNotIn("reply_parameters", payload[0])
        self.assertEqual(
            payload[1]["reply_parameters"],
            {"message_id": 101, "allow_sending_without_reply": True},
        )
        self.assertEqual(
            payload[2]["reply_parameters"],
            {"message_id": 102, "allow_sending_without_reply": True},
        )


class GestioneDropboxTests(unittest.TestCase):
    def _documento_temporaneo(self):
        handle = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        handle.write(b"PDF")
        handle.close()
        return {
            "local_path": handle.name,
            "dropbox_path": "/notiziejr/test.pdf",
            "original_name": "test.pdf",
        }

    def test_errore_gemini_conserva_pdf_remoto(self):
        documento = self._documento_temporaneo()

        with (
            patch.object(
                bot_giornali,
                "generate_news_from_pdf",
                side_effect=RuntimeError("429 RESOURCE_EXHAUSTED"),
            ),
            patch.object(bot_giornali, "delete_files_from_dropbox") as delete,
        ):
            risultato = bot_giornali.elabora_documento(documento)

        self.assertFalse(risultato)
        delete.assert_not_called()
        self.assertFalse(os.path.exists(documento["local_path"]))

    def test_lettura_riuscita_cancella_pdf_remoto(self):
        documento = self._documento_temporaneo()

        with (
            patch.object(bot_giornali, "generate_news_from_pdf", return_value=[]),
            patch.object(bot_giornali, "delete_files_from_dropbox") as delete,
        ):
            risultato = bot_giornali.elabora_documento(documento)

        self.assertTrue(risultato)
        delete.assert_called_once_with([documento["dropbox_path"]])
        self.assertFalse(os.path.exists(documento["local_path"]))


if __name__ == "__main__":
    unittest.main()
