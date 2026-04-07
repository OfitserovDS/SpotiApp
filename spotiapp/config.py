import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    spotify_client_id: str
    spotify_client_secret: str
    spotify_redirect_uri: str
    spotify_scope: str
    spotify_cache_path: str
    download_path: str
    search_cache_file: str
    max_workers: int

    @staticmethod
    def from_env() -> "Settings":
        load_dotenv()

        client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
        client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()

        if not client_id or not client_secret:
            raise ValueError(
                "SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be set in .env."
            )

        return Settings(
            spotify_client_id=client_id,
            spotify_client_secret=client_secret,
            spotify_redirect_uri=os.getenv(
                "SPOTIFY_REDIRECT_URI",
                "http://localhost:8888/callback",
            ),
            spotify_scope=os.getenv(
                "SPOTIFY_SCOPE",
                "playlist-read-private playlist-read-collaborative",
            ),
            spotify_cache_path=os.getenv("SPOTIFY_CACHE_PATH", ".spotify_cache"),
            download_path=os.getenv("DOWNLOAD_PATH", "downloads"),
            search_cache_file=os.getenv("SEARCH_CACHE_FILE", "search_cache.json"),
            max_workers=int(os.getenv("MAX_WORKERS", "5")),
        )

