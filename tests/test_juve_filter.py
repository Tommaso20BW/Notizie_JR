import unittest

import juve_press_bot as bot


class JuventusTitleFilterTests(unittest.TestCase):
    def test_juve_stabia_alone_is_rejected(self):
        cases = (
            "Juve Stabia, vittoria contro il Bari",
            "Mercato Juve Stabia: arriva Rossi",
            "Juve Stabia ufficializza il nuovo allenatore",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertFalse(bot.is_juventus_title(text))

    def test_real_juventus_is_accepted(self):
        cases = (
            "Juventus, nuova operazione di mercato",
            "La Juve prepara la prossima partita",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(bot.is_juventus_title(text))

    def test_juventus_and_juve_stabia_together_are_accepted(self):
        cases = (
            "Juventus-Juve Stabia, amichevole il 10 agosto",
            "Juve e Juve Stabia lavorano allo scambio",
            "Juventus interessata a un giocatore della Juve Stabia",
            "Juve Stabia tratta Rossi con la Juventus",
            "Juve Stabia: contatti con la Juve per Rossi",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(bot.is_juventus_title(text))


if __name__ == "__main__":
    unittest.main()
