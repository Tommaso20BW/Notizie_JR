import unittest
from datetime import date

import juve_press_bot as bot


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        return FakeResponse(self.payload)


class GazzettaTests(unittest.TestCase):
    def test_gazzetta_removes_inline_html_without_splitting_words(self):
        payload = {
            "data": [
                {
                    "firstPublicationDate": "2026-09-20T10:15:00+02:00",
                    "url": (
                        "https://www.gazzetta.it/Calcio/Serie-A/Juventus/"
                        "20-09-2026/test.shtml"
                    ),
                    "headline": (
                        'Il punto degli operatori: nerazzurri in prima '
                        'line<span class="rcs-transparent">a, ma dopo il '
                        'big match cambiano le quote</span>'
                    ),
                    "standFirst": (
                        'Dopo la sconfitta i tifosi della '
                        '<a href="https://www.gazzetta.it/" target="_blank">'
                        'Juventus </a>si aspettavano una risposta.'
                    ),
                }
            ]
        }

        articles = bot.scrape_gazzetta(
            FakeSession(payload),
            {date(2026, 9, 20)},
        )

        self.assertEqual(len(articles), 1)
        self.assertEqual(
            articles[0].title,
            (
                "Il punto degli operatori: nerazzurri in prima linea, "
                "ma dopo il big match cambiano le quote"
            ),
        )
        self.assertEqual(
            articles[0].summary,
            "Dopo la sconfitta i tifosi della Juventus si aspettavano una risposta.",
        )
        self.assertNotIn("<", articles[0].title)
        self.assertNotIn("<", articles[0].summary)


if __name__ == "__main__":
    unittest.main()
