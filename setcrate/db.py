import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

DATA = Path(os.environ.get("SETCRATE_DATA", "data")).resolve()
ASSETS = DATA / "originals"
RENDERS = DATA / "renders"
EXPORTS = DATA / "exports"

SCHEMA = """
CREATE TABLE IF NOT EXISTS playlists (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT, created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS trashed_playlists (
    playlist_id TEXT PRIMARY KEY REFERENCES playlists(id),
    trashed TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY, filename TEXT NOT NULL, meta TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracks (
    id TEXT PRIMARY KEY, playlist_id TEXT NOT NULL REFERENCES playlists(id),
    position INTEGER NOT NULL, meta TEXT NOT NULL, state TEXT NOT NULL,
    asset_id TEXT REFERENCES assets(id), reason TEXT, processed TEXT, claimed REAL,
    UNIQUE(playlist_id, position)
);
CREATE INDEX IF NOT EXISTS tracks_state ON tracks(state);
CREATE TABLE IF NOT EXISTS candidates (
    track_id TEXT NOT NULL REFERENCES tracks(id), asset_id TEXT NOT NULL REFERENCES assets(id),
    reason TEXT NOT NULL, conflict INTEGER NOT NULL, PRIMARY KEY(track_id, asset_id)
);
CREATE TABLE IF NOT EXISTS decisions (
    track_key TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(id)
);
CREATE TABLE IF NOT EXISTS spotify_token (
    id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY, verifier TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS export_expiry (
    track_id TEXT PRIMARY KEY REFERENCES tracks(id), expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS automatic_jobs (
    track_id TEXT PRIMARY KEY REFERENCES tracks(id), claimed REAL
);
CREATE TABLE IF NOT EXISTS export_exclusions (
    track_id TEXT PRIMARY KEY REFERENCES tracks(id)
);
"""


def setup():
    for directory in (ASSETS, RENDERS, EXPORTS):
        directory.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)


@contextmanager
def connect():
    db = sqlite3.connect(DATA / "setcrate.sqlite3", timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def new_id():
    return uuid.uuid4().hex


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def decode(value):
    return json.loads(value) if value else None
