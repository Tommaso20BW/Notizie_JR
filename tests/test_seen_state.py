import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import juve_press_bot as bot


class SeenStateTests(unittest.TestCase):
    def test_previous_day_is_cleared_automatically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            path.write_text(
                json.dumps({"date": "2026-07-25", "items": ["old:1"]}),
                encoding="utf-8",
            )

            with patch.object(bot, "STATE_FILE", path):
                seen = bot.load_seen(date(2026, 7, 26))

            self.assertEqual(seen, [])
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored, {"date": "2026-07-26", "items": []})

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

    def test_legacy_list_is_migrated_without_resending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            path.write_text('["legacy:1"]', encoding="utf-8")

            with patch.object(bot, "STATE_FILE", path):
                seen = bot.load_seen(date(2026, 7, 26))

            self.assertEqual(seen, ["legacy:1"])
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["date"], "2026-07-26")
            self.assertEqual(stored["items"], ["legacy:1"])


if __name__ == "__main__":
    unittest.main()
