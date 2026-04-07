# SpotiApp

SpotiApp downloads tracks from Spotify playlists to `.mp3` with `SoundCloud` as the primary source and `YouTube` as fallback.

## Installation

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create a `.env` file based on `.env.example` and fill in your Spotify credentials.

## Run

```bash
python main.py "https://open.spotify.com/playlist/..."
```

or without an argument:

```bash
python main.py
```

The app will ask for a playlist URL in interactive mode.

## Environment Variables

- `SPOTIFY_CLIENT_ID`
- `SPOTIFY_CLIENT_SECRET`
- `SPOTIFY_REDIRECT_URI`
- `SPOTIFY_SCOPE`
- `SPOTIFY_CACHE_PATH`
- `DOWNLOAD_PATH`
- `SEARCH_CACHE_FILE`
- `MAX_WORKERS`
