import unittest

from juve_press_bot import _is_relevant_di_marzio_juventus_article


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


if __name__ == "__main__":
    unittest.main()
