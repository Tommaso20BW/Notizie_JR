import unittest

from juve_press_bot import (
    _di_marzio_juventus_is_contextually_relevant,
    _di_marzio_juventus_relevance_reason,
    _is_relevant_di_marzio_juventus_article,
)

ATALANTA_TITLE = (
    "Atalanta, la probabile formazione contro il Venezia: Scamacca torna dal 1'?"
)
ATALANTA_DESCRIPTION = (
    "La probabile formazione dell'Atalanta per la sfida contro il Venezia, "
    "valida per la 6ª giornata di Serie A: le possibili scelte di Sarri"
)
ATALANTA_BODY = (
    "Terminata la maxi sosta per le nazionali, la Serie A si prepara a tornare "
    "in campo con la 6ª giornata di campionato. L'Atalanta ospiterà il Venezia "
    "lunedì 12 ottobre alle 18:30 alla New Balance Arena. I nerazzurri "
    "occupano attualmente l'11º posto e sono reduci da tre sconfitte consecutive. "
    "LA PROBABILE FORMAZIONE - Sarri dovrebbe apportare alcune modifiche "
    "rispetto all'ultima gara contro la Juventus, a partire dal modulo "
    "(4-2-3-1). Tra i pali pronto il solito Carnesecchi, supportato al centro "
    "dalla coppia Scalvini-Kristensen. In attacco Scamacca scalpita per un "
    "posto da titolare, dopo la panchina nella sfida contro i bianconeri. "
    "DOVE VEDERE LA PARTITA - Atalanta-Venezia, gara valida per la 6ª giornata "
    "di Serie A, si giocherà lunedì 12 ottobre alle 18:30."
)


class DiMarzioRelevanceTests(unittest.TestCase):
    def test_golden_boy_list_is_rejected(self):
        self.assertFalse(
            _is_relevant_di_marzio_juventus_article(
                "",
                "Golden Boy 2026, svelati i 25 finalisti: presenti 3 Serie A",
                "Golden Boy 2026, svelati i 25 finalisti: presenti 3 Serie A",
                "",
                "I 25 finalisti sono: ... Alajbegovic (Juventus) e altri giovani.",
                "",
            )
        )

    def test_biographical_juventus_mention_is_rejected(self):
        self.assertFalse(
            _is_relevant_di_marzio_juventus_article(
                "",
                "",
                "I futuri italiani: i nuovi talenti da seguire",
                "",
                "Rossi ha giocato con Sassuolo, Juventus e Marsiglia prima della nuova esperienza.",
                "",
            )
        )

    def test_explicit_juventus_title_is_accepted(self):
        self.assertTrue(
            _is_relevant_di_marzio_juventus_article(
                "",
                "",
                "Il punto sul mercato della Juventus",
                "",
                "",
                "",
            )
        )

    def test_real_juventus_operation_is_accepted(self):
        self.assertTrue(
            _is_relevant_di_marzio_juventus_article(
                "",
                "",
                "Carnevali su Ekhator",
                "",
                "La Juventus sta provando a portare a termine un'operazione per Jeff Ekhator.",
                "",
            )
        )

    def test_juventus_summary_with_market_context_is_accepted(self):
        self.assertTrue(
            _is_relevant_di_marzio_juventus_article(
                "",
                "La Juventus continua a lavorare sul mercato per un nuovo attaccante.",
                "Mercato, tutte le trattative di oggi",
                "La Juventus continua a lavorare sul mercato per un nuovo attaccante.",
                "",
                "",
            )
        )


class DiMarzioOtherTeamArticleTests(unittest.TestCase):
    """Articoli su altre squadre che citano la Juventus solo come avversaria."""

    def relevant(self, body, title="Titolo", description=""):
        return _is_relevant_di_marzio_juventus_article(
            title, "", title, description, body, description
        )

    def test_atalanta_probable_lineup_is_rejected(self):
        self.assertFalse(
            self.relevant(ATALANTA_BODY, ATALANTA_TITLE, ATALANTA_DESCRIPTION)
        )
        self.assertIsNone(
            _di_marzio_juventus_relevance_reason(
                ATALANTA_TITLE,
                "",
                ATALANTA_TITLE,
                ATALANTA_DESCRIPTION,
                ATALANTA_BODY,
                ATALANTA_DESCRIPTION,
            )
        )

    def test_uppercase_heading_glued_to_previous_sentence_is_rejected(self):
        body = ATALANTA_BODY.replace(
            "consecutive. LA PROBABILE", "consecutive.LA PROBABILE"
        )
        self.assertNotEqual(body, ATALANTA_BODY)
        self.assertFalse(self.relevant(body, ATALANTA_TITLE))

    def test_heading_without_dash_variant_is_rejected(self):
        body = ATALANTA_BODY.replace("LA PROBABILE FORMAZIONE -", "LA PROBABILE FORMAZIONE:")
        self.assertFalse(self.relevant(body, ATALANTA_TITLE))

    def test_body_without_heading_is_rejected(self):
        body = ATALANTA_BODY.replace("LA PROBABILE FORMAZIONE - ", "")
        self.assertFalse(self.relevant(body, ATALANTA_TITLE))

    def test_generic_lineup_word_in_same_sentence_is_not_enough(self):
        self.assertFalse(
            self.relevant(
                "Sarri cambia la formazione rispetto alla gara contro la Juventus."
            )
        )

    def test_juventus_as_past_opponent_in_summary_is_rejected(self):
        self.assertFalse(
            _is_relevant_di_marzio_juventus_article(
                "",
                "Il Cagliari cambia la formazione dopo la sconfitta con la Juventus.",
                "Cagliari, le scelte per domenica",
                "Il Cagliari cambia la formazione dopo la sconfitta con la Juventus.",
                "",
                "",
            )
        )

    def test_match_report_with_single_opponent_mention_is_rejected(self):
        self.assertFalse(
            self.relevant(
                "Il Napoli vince 2-0 e allunga in campionato. "
                "La squadra di Allegri aveva perso il derby, non la partita "
                "contro la Juventus di una settimana fa."
            )
        )


class DiMarzioJuventusStoriesStillAcceptedTests(unittest.TestCase):
    """Il filtro più severo non deve perdere le vere notizie sulla Juventus."""

    def relevant(self, body):
        return _di_marzio_juventus_is_contextually_relevant(body)

    def test_juventus_as_subject_is_accepted(self):
        self.assertTrue(
            self.relevant(
                "La Juventus ha contattato l'entourage del giocatore nelle ultime ore."
            )
        )

    def test_juventus_with_parenthetical_aside_is_accepted(self):
        self.assertTrue(
            self.relevant(
                "La Juventus - secondo quanto raccolto - ha avviato i contatti."
            )
        )

    def test_rival_interest_for_a_player_is_accepted(self):
        self.assertTrue(
            self.relevant("Il Milan segue Rossi, obiettivo anche della Juventus.")
        )

    def test_market_word_near_juventus_is_accepted(self):
        self.assertTrue(
            self.relevant("Accordo raggiunto tra Rossi e la Juventus per il rinnovo.")
        )

    def test_uppercase_ufficiale_heading_keeps_the_news(self):
        self.assertTrue(
            self.relevant("UFFICIALE: Rossi firma con la Juventus fino al 2030.")
        )

    def test_uppercase_heading_containing_juve_is_kept_attached(self):
        self.assertTrue(
            self.relevant("MERCATO JUVE - Contatti con l'agente di Rossi per giugno.")
        )

    def test_market_rival_competition_is_accepted(self):
        self.assertTrue(
            self.relevant(
                "Rossi, sfida con la Juventus per l'attaccante: offerta in arrivo."
            )
        )

    def test_repeated_mentions_allow_generic_context(self):
        self.assertTrue(
            self.relevant(
                "Il Cagliari prepara la partita contro la Juventus. "
                "La Juventus è favorita. "
                "Per la Juventus il campionato riparte da qui."
            )
        )

    def test_reason_is_reported(self):
        self.assertEqual(
            _di_marzio_juventus_relevance_reason(
                "", "", "Il punto sulla Juventus", "", "", ""
            ),
            "titolo",
        )
        self.assertEqual(
            _di_marzio_juventus_relevance_reason(
                "", "", "Mercato, le news", "", "La Juventus sta trattando X.", ""
            ),
            "corpo",
        )


if __name__ == "__main__":
    unittest.main()
