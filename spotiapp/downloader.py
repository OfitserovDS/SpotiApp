import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

import yt_dlp

from spotiapp.cache import SearchCache
from spotiapp.models import TrackInfo
from spotiapp.spotify_client import SpotifyService

SEARCH_CANDIDATES = 8
MAX_DURATION_DIFF = 0.18
MIN_DURATION = 30
MAX_DURATION = 900

NON_ORIGINAL_KEYWORDS = [
    "remix", "rmx", "mashup", "bootleg", "flip", "rework", "vip mix",
    "edit", "cover", "acoustic", "instrumental", "extended mix",
    "slowed", "sped up", "nightcore", "8d audio", "reaction",
    "live at", "live from", "live version", "karaoke", "tribute",
]


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
        failed_tracks: list[TrackInfo] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_track = {
                executor.submit(self.download_single_track, track, i, len(new_tracks)): track
                for i, track in enumerate(new_tracks, start=1)
            }
            for future in as_completed(future_to_track):
                if future.result():
                    successful += 1
                else:
                    failed_tracks.append(future_to_track[future])

        self._logger.info(
            "Done. Downloaded %s of %s tracks to %s",
            successful,
            len(new_tracks),
            self.download_path.resolve(),
        )

        if failed_tracks:
            self._logger.warning("Could not download %s track(s):", len(failed_tracks))
            for track in failed_tracks:
                self._safe_print(f"  - {track.artist} - {track.title}")

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
            candidates = self._search_candidates("scsearch", track, options)
        except Exception as error:
            self._safe_print(f"[{index}/{total}] SoundCloud error: {str(error)[:80]}")
            return False

        if not candidates:
            self._safe_print(f"[{index}/{total}] No suitable track on SoundCloud")
            return False

        with yt_dlp.YoutubeDL(options) as ydl:
            for candidate in candidates:
                try:
                    ydl.download([candidate["webpage_url"]])
                    self._safe_print(f"[{index}/{total}] [SOUNDCLOUD] {track.filename}.mp3")
                    return True
                except Exception as error:
                    self._safe_print(
                        f"[{index}/{total}] SoundCloud candidate unavailable "
                        f"({str(error)[:60]}), trying next..."
                    )
                    continue

        return False

    def download_from_youtube(self, track: TrackInfo, index: int, total: int) -> bool:
        options = self._build_ydl_options(default_search="ytsearch", filename=track.filename)

        try:
            candidates = self._search_candidates("ytsearch", track, options)
        except Exception:
            return False

        with yt_dlp.YoutubeDL(options) as ydl:
            for candidate in candidates:
                try:
                    ydl.download([candidate["webpage_url"]])
                    self._safe_print(f"[{index}/{total}] [YOUTUBE] {track.filename}.mp3")
                    return True
                except Exception:
                    continue

        return False

    def _search_candidates(
        self, search_prefix: str, track: TrackInfo, options: dict
    ) -> list[dict]:
        search_options = {**options, "ignoreerrors": True}
        with yt_dlp.YoutubeDL(search_options) as ydl:
            info = ydl.extract_info(
                f"{search_prefix}{SEARCH_CANDIDATES}:{track.query}", download=False
            )
            entries = [entry for entry in (info.get("entries", []) if info else []) if entry]

        return self._rank_candidates(entries, track)

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

    @classmethod
    def _rank_candidates(cls, entries: list[dict], track: TrackInfo) -> list[dict]:
        allowed_keywords = cls._title_keywords(f"{track.title} {track.artist}")
        scored = []

        for entry in entries:
            title = entry.get("title") or ""
            if cls._has_unwanted_keyword(title, allowed_keywords):
                continue

            duration = entry.get("duration")
            if not duration or duration < MIN_DURATION or duration > MAX_DURATION:
                continue

            if track.expected_dur > 0:
                duration_diff = abs(duration - track.expected_dur) / track.expected_dur
                if duration_diff > MAX_DURATION_DIFF:
                    continue
            else:
                duration_diff = 0.0

            score = duration_diff - (0.5 if cls._is_official_source(entry, track) else 0)
            scored.append((score, entry))

        scored.sort(key=lambda pair: pair[0])
        return [entry for _, entry in scored]

    @staticmethod
    def _is_official_source(entry: dict, track: TrackInfo) -> bool:
        uploader = (entry.get("uploader") or "").lower()
        title = (entry.get("title") or "").lower()
        artist = track.artist.lower()

        if uploader.endswith("- topic") or "vevo" in uploader:
            return True
        if artist in uploader:
            return True
        if "official video" in title or "official audio" in title:
            return True
        return False

    @staticmethod
    def _title_keywords(title: str) -> set[str]:
        return {word.lower() for word in re.findall(r"[a-zA-Zа-яА-Я]+", title)}

    @staticmethod
    def _has_unwanted_keyword(title: str, allowed_keywords: set[str]) -> bool:
        lowered = title.lower()
        for keyword in NON_ORIGINAL_KEYWORDS:
            if keyword in lowered and keyword not in allowed_keywords:
                return True
        return False

    def _safe_print(self, message: str) -> None:
        with self._print_lock:
            try:
                print(message)
            except UnicodeEncodeError:
                encoding = sys.stdout.encoding or "utf-8"
                print(message.encode(encoding, errors="replace").decode(encoding))
