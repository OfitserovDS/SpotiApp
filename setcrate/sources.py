import base64
import csv
import hashlib
import io
import json
import math
import os
import re
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import HTTPException

from .db import connect, decode, encode

ORIGIN = os.environ.get("SETCRATE_ORIGIN", "http://127.0.0.1:8765")
REDIRECT = ORIGIN + "/auth/spotify/callback"
SPOTIFY_ID = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
SC_ID = os.environ.get("SOUNDCLOUD_CLIENT_ID", "").strip()
SC_SECRET = os.environ.get("SOUNDCLOUD_CLIENT_SECRET", "").strip()
MAX_TRACKS = 5000
spotify_lock = threading.Lock()
sc_lock = threading.Lock()
sc_token = None


def api(method, url, **kwargs):
    for attempt in range(3):
        try:
            response = httpx.request(method, url, timeout=12, **kwargs)
        except httpx.HTTPError:
            if attempt == 2:
                raise HTTPException(502, "Сервис временно недоступен.")
            continue
        if response.status_code == 429:
            delay = response.headers.get("retry-after", "")
            if attempt < 2 and delay.isdigit() and 0 <= int(delay) <= 5:
                time.sleep(int(delay))
                continue
            raise HTTPException(429, "Сервис ограничил частоту запросов. Повтори позже.")
        if response.status_code >= 500 and attempt < 2:
            time.sleep(attempt + 1)
            continue
        if response.status_code in (401, 403, 404):
            raise HTTPException(response.status_code, "Сервис отклонил запрос. Проверь вход и доступ к ресурсу.")
        if response.is_error:
            raise HTTPException(502, "Сервис отклонил запрос.")
        try:
            return response.json()
        except ValueError as exc:
            raise HTTPException(502, "Источник вернул ответ в неожиданном формате.") from exc


def spotify_login():
    if not SPOTIFY_ID:
        raise HTTPException(400, "Добавь SPOTIFY_CLIENT_ID в файл .env и перезапусти Docker Compose.")
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    with connect() as db:
        db.execute("DELETE FROM oauth_states WHERE created < ?", (time.time() - 600,))
        db.execute("INSERT INTO oauth_states VALUES (?, ?, ?)", (state, verifier, time.time()))
    query = urlencode({
        "client_id": SPOTIFY_ID, "response_type": "code", "redirect_uri": REDIRECT,
        "scope": "playlist-read-private playlist-read-collaborative", "state": state,
        "code_challenge_method": "S256", "code_challenge": challenge,
    })
    return "https://accounts.spotify.com/authorize?" + query, state


def save_token(db, token, previous=None):
    token["expires"] = time.time() + token.get("expires_in", 3600) - 60
    if previous:
        token.setdefault("refresh_token", previous.get("refresh_token"))
    db.execute("INSERT OR REPLACE INTO spotify_token VALUES (1, ?)", (encode(token),))


def spotify_callback(code, state, cookie):
    if not code or not state or not cookie or not secrets.compare_digest(state, cookie):
        raise HTTPException(400, "Запрос входа просрочен или отменён.")
    with connect() as db:
        record = db.execute(
            "DELETE FROM oauth_states WHERE state=? AND created>? RETURNING verifier",
            (state, time.time() - 600),
        ).fetchone()
    if not record:
        raise HTTPException(400, "Запрос входа просрочен. Подключи Spotify снова.")
    token = api("POST", "https://accounts.spotify.com/api/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": SPOTIFY_ID,
        "redirect_uri": REDIRECT, "code_verifier": record["verifier"],
    })
    with spotify_lock, connect() as db:
        save_token(db, token)


def spotify_access():
    with spotify_lock, connect() as db:
        row = db.execute("SELECT payload FROM spotify_token WHERE id=1").fetchone()
        if not row:
            raise HTTPException(401, "Сначала подключи Spotify.")
        token = decode(row["payload"])
        if token["expires"] > time.time():
            return token["access_token"]
        if not token.get("refresh_token"):
            raise HTTPException(401, "Подключи Spotify повторно.")
        fresh = api("POST", "https://accounts.spotify.com/api/token", data={
            "grant_type": "refresh_token", "client_id": SPOTIFY_ID, "refresh_token": token["refresh_token"],
        })
        save_token(db, fresh, token)
        return fresh["access_token"]


def playlist_id(value):
    if value.startswith("spotify:playlist:"):
        candidate = value.removeprefix("spotify:playlist:")
    else:
        try:
            address = urlsplit(value)
        except ValueError:
            raise HTTPException(400, "Нужна ссылка на плейлист Spotify.")
        match = re.fullmatch(r"/(?:intl-[a-zA-Z-]+/)?playlist/([A-Za-z0-9]{22})/?", address.path)
        candidate = match[1] if address.scheme == "https" and address.netloc == "open.spotify.com" and match else ""
    if not re.fullmatch(r"[A-Za-z0-9]{22}", candidate):
        raise HTTPException(400, "Нужна полная ссылка Spotify вида open.spotify.com/playlist/…")
    return candidate


def load_spotify(value):
    key = playlist_id(value.strip())
    headers = {"Authorization": "Bearer " + spotify_access()}
    endpoint = f"https://api.spotify.com/v1/playlists/{key}"
    params = {"fields": "name,snapshot_id"}
    metadata = api("GET", endpoint, headers=headers, params=params)
    tracks = []
    while True:
        page = api("GET", endpoint + "/items", headers=headers, params={"limit": 50, "offset": len(tracks)})
        items = page.get("items", [])
        for item in items:
            entry = item or {}
            raw = entry.get("item") or entry.get("track") or {}
            artists = [artist["name"] for artist in raw.get("artists", []) if artist.get("name")]
            track_key = raw.get("id") or ""
            tracks.append({
                "title": raw.get("name") or "Недоступная позиция",
                "artist": ", ".join(artists), "artists": artists,
                "album": (raw.get("album") or {}).get("name") or "",
                "duration": raw["duration_ms"] / 1000 if raw.get("duration_ms") else None,
                "isrc": (raw.get("external_ids") or {}).get("isrc") or "",
                "spotify_id": track_key, "url": f"https://open.spotify.com/track/{track_key}" if track_key else "",
                "unsupported": not raw or raw.get("type") != "track",
            })
        if len(tracks) > MAX_TRACKS:
            raise HTTPException(400, "Поддерживаются плейлисты до 5000 позиций.")
        if not page.get("next"):
            break
        if not items:
            raise HTTPException(502, "Spotify вернул неполную страницу. Повтори импорт.")
    ending = api("GET", endpoint, headers=headers, params=params)
    if ending.get("snapshot_id") != metadata.get("snapshot_id"):
        raise HTTPException(409, "Плейлист изменился во время импорта. Повтори импорт.")
    return metadata["name"], f"https://open.spotify.com/playlist/{key}", tracks


def catalog_links(track):
    query = track["artist"] + " " + track["title"]
    return {
        "Beatport": "https://www.beatport.com/search?" + urlencode({"q": query}),
        "Bandcamp": "https://bandcamp.com/search?" + urlencode({"q": query}),
        "SoundCloud": "https://soundcloud.com/search/sounds?" + urlencode({"q": query}),
    }


def soundcloud_search(track):
    global sc_token
    if not SC_ID or not SC_SECRET:
        raise HTTPException(400, "Для поиска внутри сайта добавь ключи приложения SoundCloud в .env.")
    with sc_lock:
        if not sc_token or sc_token["expires"] <= time.time():
            sc_token = api("POST", "https://secure.soundcloud.com/oauth/token", data={
                "grant_type": "client_credentials", "client_id": SC_ID, "client_secret": SC_SECRET,
            })
            sc_token["expires"] = time.time() + sc_token.get("expires_in", 3600) - 60
    result = api("GET", "https://api.soundcloud.com/tracks",
                 headers={"Authorization": "OAuth " + sc_token["access_token"]},
                 params={"q": track["artist"] + " " + track["title"], "limit": 15, "linked_partitioning": "true"})
    candidates = result.get("collection", []) if isinstance(result, dict) else result
    return [{
        "title": item.get("title"), "artist": (item.get("user") or {}).get("username"),
        "url": item["permalink_url"], "duration": (item.get("duration") or 0) / 1000,
        "downloadable": item.get("downloadable") is True,
    } for item in candidates if (item.get("permalink_url") or "").startswith("https://soundcloud.com/")]


def from_uploaded_manifest(content, filename, playlist_name):
    try:
        raw = content.decode("utf-8-sig")
        if filename.lower().endswith(".json"):
            document = json.loads(raw)
            lists = document["playlists"] if isinstance(document, dict) else document
            if not isinstance(lists, list):
                raise ValueError()
            if len(lists) != 1 and not playlist_name:
                raise HTTPException(422, "Укажи имя плейлиста из JSON-выгрузки Spotify.")
            playlist = next(item for item in lists if not playlist_name or item["name"] == playlist_name)
            return playlist["name"][:300], None, [
                account_row(item) for item in playlist["items"]
            ]
        reader = csv.DictReader(io.StringIO(raw, newline=""))
        if not reader.fieldnames or not {"title", "artist"} <= set(reader.fieldnames):
            raise ValueError()
        return playlist_name or "Импорт CSV", None, [csv_row(row) for row in reader]
    except (UnicodeError, ValueError, KeyError, TypeError, AttributeError, StopIteration) as exc:
        raise HTTPException(422, "Файл не распознан. Нужен Spotify Playlist JSON или CSV с title,artist.") from exc


def csv_row(row):
    duration = float(row["duration_ms"]) / 1000 if row.get("duration_ms") else None
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        raise ValueError()
    return {
        "title": (row.get("title") or "Недоступная позиция")[:300],
        "artist": (row.get("artist") or "")[:300],
        "album": (row.get("album") or "")[:300],
        "isrc": re.sub(r"[^A-Z0-9]", "", (row.get("isrc") or "").upper())[:12],
        "duration": duration, "unsupported": not row.get("title"),
    }


def account_row(item):
    track = item.get("track")
    if not track:
        return {"title": "Недоступная позиция", "artist": "", "unsupported": True}
    uri = track.get("trackUri") or ""
    match = re.fullmatch(r"spotify:track:([A-Za-z0-9]{22})", uri)
    return {
        "title": (track.get("trackName") or "Недоступная позиция")[:300],
        "artist": (track.get("artistName") or "")[:300],
        "album": (track.get("albumName") or "")[:300],
        "spotify_id": match[1] if match else "",
        "url": f"https://open.spotify.com/track/{match[1]}" if match else "",
    }
