import io
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
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
    def test_default_pause_is_fifteen_seconds_after_each_cycle(self):
        self.assertEqual(bot.DEFAULT_POLL_INTERVAL_SECONDS, 15)

    def test_heartbeat_file_is_refreshed(self):
        with tempfile.TemporaryDirectory() as directory:
            heartbeat = Path(directory) / "heartbeat"
            with patch.dict(
                os.environ,
                {bot.HEARTBEAT_FILE_ENV: str(heartbeat)},
                clear=False,
            ):
                bot.touch_worker_heartbeat()

            self.assertTrue(heartbeat.exists())

    def test_git_checkpoint_commits_and_pushes_changed_state(self):
        completed = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 1, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]

        with (
            patch.dict(
                os.environ,
                {
                    bot.STATE_CHECKPOINT_ENV: "true",
                    "GITHUB_REF_NAME": "main",
                },
                clear=True,
            ),
            patch.object(bot.subprocess, "run", side_effect=completed) as run,
        ):
            changed = bot.checkpoint_state_to_git()

        self.assertTrue(changed)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0][:3], ("git", "add", "--"))
        self.assertIn(("git", "pull", "--rebase", "origin", "main"), commands)
        self.assertEqual(commands[-1], ("git", "push", "origin", "HEAD:main"))

    def test_checkpoint_is_disabled_outside_github_actions(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(bot.subprocess, "run") as run,
        ):
            changed = bot.checkpoint_state_to_git()

        self.assertFalse(changed)
        run.assert_not_called()

    def test_workflow_has_watchdog_backup_and_failure_handoff(self):
        workflow = (
            bot.SCRIPT_DIR / ".github" / "workflows" / "juve-press-news.yml"
        ).read_text(encoding="utf-8")

        self.assertIn('cron: "*/5 * * * *"', workflow)
        self.assertIn("NOW - LAST_HEARTBEAT > 300", workflow)
        self.assertIn("steps.persist_state.outcome == 'success'", workflow)
        self.assertNotIn("steps.news_worker.outcome == 'success'", workflow)

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

    def test_successful_sources_do_not_flood_the_log(self):
        def scraper(session, requested_dates):
            return [sample_article()]

        output = io.StringIO()
        with (
            bot.requests.Session() as session,
            patch.object(
                bot,
                "_article_scrapers",
                return_value=(("Fonte veloce", scraper),),
            ),
            redirect_stdout(output),
        ):
            bot.collect_articles(
                session,
                {datetime(2026, 8, 12).date()},
            )

        self.assertEqual(output.getvalue(), "")

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

    def test_pause_starts_after_the_cycle_has_finished(self):
        clock = FakeClock()

        def four_second_cycle(**kwargs):
            clock.now += 4
            return 0

        with patch.object(bot, "run", side_effect=four_second_cycle) as run_cycle:
            bot.run_worker(
                duration_seconds=23,
                poll_interval_seconds=15,
                clock=clock,
                sleep=clock.sleep,
            )

        self.assertEqual(run_cycle.call_count, 2)
        self.assertEqual(clock.sleeps, [15])

    def test_worker_log_is_grouped_by_cycle(self):
        clock = FakeClock()
        output = io.StringIO()

        with (
            patch.object(bot, "run", side_effect=(2, 0, 0)),
            redirect_stdout(output),
        ):
            bot.run_worker(
                duration_seconds=31,
                poll_interval_seconds=15,
                clock=clock,
                sleep=clock.sleep,
            )

        log = output.getvalue()
        self.assertIn("[WORKER] attivo", log)
        self.assertIn("[CICLO 1] fine | nuove=2 | ok", log)
        self.assertIn("pausa=15s", log)
        self.assertNotIn("notizie di oggi trovate", log)


if __name__ == "__main__":
    unittest.main()
