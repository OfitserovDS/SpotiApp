import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

import yt_dlp

from spotiapp.cache import SearchCache
from spotiapp.models import TrackInfo
from spotiapp.spotify_client import SpotifyService


class PlaylistDownloader:
    def __init__(
        self,
        spotify_service: SpotifyService,
        cache: SearchCache,
        download_path: str,
        max_workers: int,
    ):
        self.spotify_service = spotify_service
        self.cache = cache
        self.download_path = Path(download_path)
        self.download_path.mkdir(exist_ok=True)
        self.max_workers = max_workers
        self._print_lock = Lock()
        self._logger = logging.getLogger(__name__)

    def download_playlist(self, playlist_url: str) -> None:
        tracks = self.spotify_service.get_playlist_tracks(playlist_url)
        if not tracks:
            self._logger.warning("No tracks found.")
            return

        new_tracks = [
            track
            for track in tracks
            if not (self.download_path / f"{track.filename}.mp3").exists()
        ]

        self._logger.info("Total tracks: %s | new: %s", len(tracks), len(new_tracks))
        if not new_tracks:
            self._logger.info("All tracks are already downloaded.")
            return

        successful = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self.download_single_track, track, i, len(new_tracks))
                for i, track in enumerate(new_tracks, start=1)
            }
            for future in as_completed(futures):
                if future.result():
                    successful += 1

        self._logger.info(
            "Done. Downloaded %s of %s tracks to %s",
            successful,
            len(new_tracks),
            self.download_path.resolve(),
        )

    def download_single_track(self, track: TrackInfo, index: int, total: int) -> bool:
        file_path = self.download_path / f"{track.filename}.mp3"
        if file_path.exists():
            self._safe_print(f"[{index}/{total}] Already exists: {track.filename}")
            return True

        cache_key = f"{track.artist} - {track.title}"
        if self.cache.has(cache_key):
            self._safe_print(f"[{index}/{total}] From cache: {track.filename}")
            return True

        self._safe_print(f"[{index}/{total}] {track.artist} - {track.title}")

        if self.download_from_soundcloud(track, index, total):
            self.cache.mark(cache_key)
            return True

        self._safe_print(f"[{index}/{total}] Not found on SoundCloud, trying YouTube...")
        if self.download_from_youtube(track, index, total):
            self.cache.mark(cache_key)
            return True

        self._safe_print(f"[{index}/{total}] {track.filename} - not found")
        return False

    def download_from_soundcloud(self, track: TrackInfo, index: int, total: int) -> bool:
        options = self._build_ydl_options(default_search="scsearch", filename=track.filename)

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(f"scsearch:{track.query}", download=False)
                entries = info.get("entries", []) if info else []
                best_match = self._pick_best_match(entries, track.expected_dur)

                if not best_match:
                    self._safe_print(f"[{index}/{total}] No suitable track on SoundCloud")
                    return False

                ydl.download([best_match["webpage_url"]])
                self._safe_print(f"[{index}/{total}] [SOUNDCLOUD] {track.filename}.mp3")
                return True
        except Exception as error:
            self._safe_print(f"[{index}/{total}] SoundCloud error: {str(error)[:80]}")
            return False

    def download_from_youtube(self, track: TrackInfo, index: int, total: int) -> bool:
        options = self._build_ydl_options(default_search="ytsearch", filename=track.filename)

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([track.query])
                self._safe_print(f"[{index}/{total}] [YOUTUBE] {track.filename}.mp3")
                return True
        except Exception:
            return False

    def _build_ydl_options(self, default_search: str, filename: str) -> dict:
        output_template = str(self.download_path / f"{filename}.%(ext)s")
        return {
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "320",
                }
            ],
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "default_search": default_search,
            "noplaylist": True,
            "concurrent_fragment_downloads": 10,
            "retries": 3,
            "fragment_retries": 3,
        }

    @staticmethod
    def _pick_best_match(entries: list[dict], expected_duration: float) -> dict | None:
        for entry in entries[:5]:
            duration = entry.get("duration")
            if not duration or duration < 30 or duration > 600:
                continue

            if expected_duration > 0:
                duration_diff = abs(duration - expected_duration) / expected_duration
                if duration_diff < 0.3:
                    return entry
            else:
                return entry

        return None

    def _safe_print(self, message: str) -> None:
        with self._print_lock:
            print(message)

