import argparse
import logging

import spotipy

from spotiapp.cache import SearchCache
from spotiapp.config import Settings
from spotiapp.downloader import PlaylistDownloader
from spotiapp.spotify_client import SpotifyService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download Spotify playlists to mp3 (SoundCloud -> YouTube fallback)."
    )
    parser.add_argument("playlist_url", nargs="?", help="Spotify playlist URL or ID")
    return parser


def run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args()

    playlist_url = args.playlist_url or input("Your playlist link:\n").strip()
    if not playlist_url:
        print("Playlist URL cannot be empty.")
        return 1

    try:
        settings = Settings.from_env()
        spotify_service = SpotifyService(
            client_id=settings.spotify_client_id,
            client_secret=settings.spotify_client_secret,
            redirect_uri=settings.spotify_redirect_uri,
            scope=settings.spotify_scope,
            cache_path=settings.spotify_cache_path,
        )
        cache = SearchCache(settings.search_cache_file)
        downloader = PlaylistDownloader(
            spotify_service=spotify_service,
            cache=cache,
            download_path=settings.download_path,
            max_workers=settings.max_workers,
        )
        downloader.download_playlist(playlist_url)
        return 0
    except ValueError as error:
        print(f"Configuration error: {error}")
        return 2
    except spotipy.exceptions.SpotifyException as error:
        print(f"Spotify API error: {error}")
        return 3
    except KeyboardInterrupt:
        print("\nOperation interrupted by user.")
        return 130
    except Exception as error:  # pylint: disable=broad-except
        logging.exception("Unexpected error: %s", error)
        return 4


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()

