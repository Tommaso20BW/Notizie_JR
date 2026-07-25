import unittest
from datetime import date

import requests

import juve_press_bot as bot


class NotFoundResponse:
    status_code = 404

    def raise_for_status(self):
        error = requests.HTTPError("404")
        error.response = self
        raise error


class FakeSession:
    def __init__(self):
        self.urls = []

    def get(self, url, timeout):
        self.urls.append(url)
        return NotFoundResponse()


class SkyTests(unittest.TestCase):
    def test_missing_current_page_is_silent_and_has_no_previous_day_fallback(self):
        session = FakeSession()

        articles = bot.scrape_sky_calciomercato(
            session,
            {date(2026, 7, 26)},
        )

        self.assertEqual(articles, [])
        self.assertEqual(len(session.urls), 1)
        self.assertIn("/2026/07/26/", session.urls[0])


if __name__ == "__main__":
    unittest.main()
