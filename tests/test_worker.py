import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import juve_press_bot as bot


def sample_article():
    return bot.Article(
        source="Fonte veloce",
        title="Notizia immediata",
        url="https://example.com/fast",
        published=datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc),
        state_key="fast:1",
    )


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class WorkerTests(unittest.TestCase):
    def test_completed_source_is_published_while_slow_source_is_running(self):
        delivered = threading.Event()

        def fast_scraper(session, requested_dates):
            return [sample_article()]

        def slow_scraper(session, requested_dates):
            self.assertTrue(delivered.wait(timeout=2))
            return []

        def on_article(article):
            delivered.set()

        with (
            bot.requests.Session() as session,
            patch.object(
                bot,
                "_article_scrapers",
                return_value=(
                    ("Fonte veloce", fast_scraper),
                    ("Fonte lenta", slow_scraper),
                ),
            ),
        ):
            articles, errors = bot.collect_articles(
                session,
                {datetime(2026, 8, 12).date()},
                on_article=on_article,
            )

        self.assertTrue(delivered.is_set())
        self.assertEqual([article.notification_key for article in articles], ["fast:1"])
        self.assertEqual(errors, [])

    def test_worker_runs_multiple_cycles_and_stops_at_deadline(self):
        clock = FakeClock()

        with patch.object(bot, "run", return_value=0) as run_cycle:
            bot.run_worker(
                duration_seconds=25,
                poll_interval_seconds=10,
                clock=clock,
                sleep=clock.sleep,
            )

        self.assertEqual(run_cycle.call_count, 3)
        self.assertEqual(clock.sleeps, [10, 10, 5])


if __name__ == "__main__":
    unittest.main()
