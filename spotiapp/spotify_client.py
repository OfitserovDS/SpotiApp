import logging
import re

import spotipy
from spotipy.oauth2 import SpotifyOAuth

from spotiapp.models import TrackInfo


def _clean_track_name(track_name: str) -> str:
    clean_name = re.sub(r"\s*\([^)]*\)", "", track_name)
    clean_name = re.sub(r"\s*\[[^\]]*\]", "", clean_name)
    clean_name = re.sub(r"\s*feat\..*$", "", clean_name, flags=re.IGNORECASE)
    clean_name = re.sub(r"\s*ft\..*$", "", clean_name, flags=re.IGNORECASE)
    return clean_name.strip()


def _sanitize_filename(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", value).strip()


class SpotifyService:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scope: str,
        cache_path: str,
    ):
        auth_manager = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope=scope,
            cache_path=cache_path,
        )
        self._client = spotipy.Spotify(auth_manager=auth_manager)
        self._logger = logging.getLogger(__name__)

    def get_playlist_tracks(self, playlist_url: str) -> list[TrackInfo]:
        playlist_id = self._extract_playlist_id(playlist_url)

        playlist = self._client.playlist(playlist_id)
        self._logger.info(
            "Playlist: %s | owner: %s",
            playlist.get("name", "Unknown"),
            playlist.get("owner", {}).get("display_name", "Unknown"),
        )

        tracks: list[TrackInfo] = []
        results = self._client.playlist_items(playlist_id)

        while True:
            for item in results.get("items", []):
                track_data = item.get("item")
                if not track_data or track_data.get("type") != "track":
                    continue

                track_name = track_data.get("name", "Unknown")
                artists = track_data.get("artists", [])
                artist_name = artists[0].get("name", "Unknown") if artists else "Unknown"
                clean_title = _clean_track_name(track_name)
                duration_sec = track_data.get("duration_ms", 0) / 1000

                tracks.append(
                    TrackInfo(
                        artist=artist_name,
                        title=track_name,
                        clean_title=clean_title,
                        query=f"{artist_name} {clean_title}",
                        filename=_sanitize_filename(f"{artist_name} - {track_name}"),
                        expected_dur=duration_sec,
                    )
                )

            if not results.get("next"):
                break
            results = self._client.next(results)

        return tracks

    @staticmethod
    def _extract_playlist_id(playlist_url: str) -> str:
        if "playlist/" in playlist_url:
            return playlist_url.split("/playlist/")[1].split("?")[0]
        return playlist_url

