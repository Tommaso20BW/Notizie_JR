"""Preparazione degli MP4 di X per l'invio come veri video Telegram."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import imageio_ffmpeg
import requests

MAX_VIDEO_FILE_BYTES = 49_000_000
DOWNLOAD_CHUNK_BYTES = 64 * 1024


class VideoPreparationError(RuntimeError):
    """Il video non può essere preparato in modo sicuro per Telegram."""


def _ffmpeg_executable() -> str:
    """Restituisce il binario FFmpeg incluso nella dipendenza Python."""
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except RuntimeError as error:
        raise VideoPreparationError(
            f"Binario FFmpeg incluso non disponibile: {error}"
        ) from error


def has_audio_track(video_path: Path) -> bool:
    """Controlla la presenza dell'audio usando il solo binario FFmpeg."""
    try:
        result = subprocess.run(
            [
                _ffmpeg_executable(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(video_path),
                "-map",
                "0:a:0",
                "-c",
                "copy",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise VideoPreparationError(
            f"FFmpeg non può essere eseguito: {error}"
        ) from error

    if result.returncode == 0:
        return True

    error_text = (result.stderr or "").lower()
    no_audio_markers = (
        "matches no streams",
        "does not contain any stream",
        "stream map '0:a:0' matches no streams",
    )
    if any(marker in error_text for marker in no_audio_markers):
        return False

    raise VideoPreparationError(
        f"FFmpeg non riesce a leggere il video: {result.stderr.strip()}"
    )


def add_silent_audio_track(source: Path, destination: Path) -> None:
    """Aggiunge audio AAC silenzioso senza ricodificare la traccia video."""
    try:
        result = subprocess.run(
            [
                _ffmpeg_executable(),
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "32k",
                "-shortest",
                "-movflags",
                "+faststart",
                str(destination),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise VideoPreparationError(
            f"ffmpeg non può essere eseguito: {error}"
        ) from error
    if result.returncode != 0 or not destination.exists():
        raise VideoPreparationError(
            f"ffmpeg non riesce a preparare il video: {result.stderr.strip()}"
        )


def _download_video(
    session: requests.Session,
    video_url: str,
    destination: Path,
) -> None:
    try:
        with session.get(video_url, stream=True, timeout=(15, 60)) as response:
            response.raise_for_status()
            try:
                content_length = int(response.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                content_length = 0
            if content_length > MAX_VIDEO_FILE_BYTES:
                raise VideoPreparationError(
                    "Video oltre il limite di upload Telegram."
                )

            downloaded = 0
            with destination.open("wb") as output:
                for chunk in response.iter_content(DOWNLOAD_CHUNK_BYTES):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > MAX_VIDEO_FILE_BYTES:
                        raise VideoPreparationError(
                            "Video oltre il limite di upload Telegram."
                        )
                    output.write(chunk)
    except (OSError, requests.RequestException) as error:
        raise VideoPreparationError(
            f"Download del video non riuscito: {error}"
        ) from error

    if not destination.exists() or destination.stat().st_size == 0:
        raise VideoPreparationError("Il video scaricato è vuoto.")


@contextmanager
def prepare_telegram_video(
    session: requests.Session,
    video_url: str,
) -> Iterator[Path]:
    """Scarica l'MP4 e garantisce una traccia audio, anche se silenziosa.

    Telegram interpreta gli MP4 senza audio come animazioni/GIF. Il file
    temporaneo prodotto qui viene invece caricato con sendVideo.
    """
    with tempfile.TemporaryDirectory(prefix="notizie-jr-video-") as directory:
        temporary_dir = Path(directory)
        source = temporary_dir / "source.mp4"
        prepared = temporary_dir / "telegram-video.mp4"
        _download_video(session, video_url, source)

        if has_audio_track(source):
            video_path = source
        else:
            add_silent_audio_track(source, prepared)
            video_path = prepared

        if video_path.stat().st_size > MAX_VIDEO_FILE_BYTES:
            raise VideoPreparationError(
                "Video preparato oltre il limite di upload Telegram."
            )
        yield video_path
