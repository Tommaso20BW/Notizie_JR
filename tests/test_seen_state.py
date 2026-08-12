import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import juve_press_bot as bot


class SeenStateTests(unittest.TestCase):
    def test_previous_day_is_preserved_and_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            path.write_text(
                json.dumps({"date": "2026-07-25", "items": ["old:1"]}),
                encoding="utf-8",
            )

            with patch.object(bot, "STATE_FILE", path):
                seen = bot.load_seen(date(2026, 7, 26))

            self.assertEqual(seen, ["old:1"])
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                stored,
                {
                    "coverage_start": "2026-07-25",
                    "dates": {
                        "2026-07-25": ["old:1"],
                        "2026-07-26": [],
                    }
                },
            )

    def test_days_older_than_yesterday_are_pruned(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            path.write_text(
                json.dumps(
                    {
                        "coverage_start": "2026-07-25",
                        "dates": {
                            "2026-07-24": ["too-old:1"],
                            "2026-07-25": ["yesterday:1"],
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(bot, "STATE_FILE", path):
                seen = bot.load_seen(date(2026, 7, 26))

            self.assertEqual(seen, ["yesterday:1"])
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("2026-07-24", stored["dates"])

    def test_stale_state_restarts_overlap_without_replaying_yesterday(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            path.write_text(
                json.dumps({"date": "2026-07-24", "items": ["stale:1"]}),
                encoding="utf-8",
            )

            with patch.object(bot, "STATE_FILE", path):
                seen, coverage_start = bot.load_seen_state(date(2026, 7, 26))

            self.assertEqual(seen, [])
            self.assertEqual(coverage_start, date(2026, 7, 26))
            self.assertEqual(
                bot.collection_dates(date(2026, 7, 26), coverage_start),
                {date(2026, 7, 26)},
            )

    def test_current_day_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            path.write_text(
                json.dumps({"date": "2026-07-26", "items": ["today:1"]}),
                encoding="utf-8",
            )

            with patch.object(bot, "STATE_FILE", path):
                seen = bot.load_seen(date(2026, 7, 26))

            self.assertEqual(seen, ["today:1"])
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                stored,
                {
                    "coverage_start": "2026-07-26",
                    "dates": {"2026-07-26": ["today:1"]},
                },
            )

    def test_legacy_list_is_migrated_without_resending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            path.write_text('["legacy:1"]', encoding="utf-8")

            with patch.object(bot, "STATE_FILE", path):
                seen = bot.load_seen(date(2026, 7, 26))

            self.assertEqual(seen, ["legacy:1"])
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                stored,
                {
                    "coverage_start": "2026-07-26",
                    "dates": {"2026-07-26": ["legacy:1"]},
                },
            )

    def test_new_item_does_not_move_yesterday_items_into_today(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            path.write_text(
                json.dumps(
                    {
                        "coverage_start": "2026-07-25",
                        "dates": {"2026-07-25": ["yesterday:1"]},
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(bot, "STATE_FILE", path):
                seen = bot.load_seen(date(2026, 7, 26))
                seen.append("today:1")
                bot.save_seen(seen, date(2026, 7, 26))

            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                stored,
                {
                    "coverage_start": "2026-07-25",
                    "dates": {
                        "2026-07-25": ["yesterday:1"],
                        "2026-07-26": ["today:1"],
                    }
                },
            )


if __name__ == "__main__":
    unittest.main()
