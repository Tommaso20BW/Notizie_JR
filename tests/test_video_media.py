import subprocess
import tempfile
import unittest
from pathlib import Path

from video_media import add_silent_audio_track, has_audio_track


class VideoMediaTests(unittest.TestCase):
    def test_silent_mp4_gets_audio_track_without_reencoding_video(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "silent.mp4"
            prepared = Path(directory) / "prepared.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=64x64:d=0.4",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(source),
                ],
                timeout=30,
                check=True,
            )

            self.assertFalse(has_audio_track(source))
            add_silent_audio_track(source, prepared)
            self.assertTrue(has_audio_track(prepared))


if __name__ == "__main__":
    unittest.main()
