from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event, Thread
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from . import automation, downloads, library, sources
from .audio import SUFFIXES
from .db import ASSETS, RENDERS, connect, decode, setup
from .retention import finish_export

DIST = Path(__file__).resolve().parents[1] / "dist"
ORIGIN_HOST = urlsplit(sources.ORIGIN).netloc


@asynccontextmanager
async def lifespan(_):
    setup()
    stop = Event()
    worker = Thread(target=automation.run, args=(stop,), daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop.set()


app = FastAPI(title="Setcrate", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def local_boundary(request: Request, call_next):
    host = request.headers.get("host")
    origin = request.headers.get("origin")
    health = request.url.path == "/api/health" and host == "127.0.0.1:8765"
    if not health and host != ORIGIN_HOST:
        return JSONResponse({"detail": "Открой сайт по адресу из инструкции."}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if origin and origin != sources.ORIGIN:
            return JSONResponse({"detail": "Запрос с другого сайта отклонён."}, status_code=403)
        length = request.headers.get("content-length")
        has_bad_length = length is not None and (not length.isdecimal() or int(length) > library.LIMIT + 1024 * 1024)
        if has_bad_length or (length is None and request.method != "DELETE"):
            return JSONResponse({"detail": "Запрос превышает лимит загрузки."}, status_code=413)
    result = await call_next(request)
    result.headers["X-Content-Type-Options"] = "nosniff"
    result.headers["Referrer-Policy"] = "no-referrer"
    result.headers["X-Frame-Options"] = "DENY"
    result.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    result.headers["Cache-Control"] = "no-store"
    return result


class SpotifyImport(BaseModel):
    url: str = Field(max_length=2048)


class Choice(BaseModel):
    asset_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    quality_confirmed: bool = False
    remember: bool = True


class DownloadRequest(BaseModel):
    url: str | None = Field(default=None, min_length=8, max_length=4096)


class ExportChoice(BaseModel):
    included: bool


@app.get("/api/health")
def health():
    with connect() as db:
        db.execute("SELECT 1 FROM playlists LIMIT 1")
    return {"ok": True}


@app.get("/api/settings")
def settings():
    with connect() as db:
        connected = db.execute("SELECT 1 FROM spotify_token").fetchone() is not None
    return {
        "spotify_configured": bool(sources.SPOTIFY_ID), "spotify_connected": connected,
        "soundcloud_configured": bool(sources.SC_ID and sources.SC_SECRET),
        "jamendo_configured": bool(downloads.JAMENDO_ID),
    }


@app.get("/auth/spotify")
def spotify_login():
    url, state = sources.spotify_login()
    response = RedirectResponse(url)
    response.set_cookie("setcrate_oauth", state, max_age=600, httponly=True, samesite="lax",
                        secure=sources.ORIGIN.startswith("https:"), path="/auth/spotify/callback")
    return response


@app.get("/auth/spotify/callback")
def spotify_callback(request: Request, code: str = "", state: str = ""):
    try:
        sources.spotify_callback(code, state, request.cookies.get("setcrate_oauth"))
        url = "/"
    except HTTPException:
        url = "/?spotify=failed"
    response = RedirectResponse(url)
    response.delete_cookie("setcrate_oauth", path="/auth/spotify/callback")
    return response


@app.delete("/api/spotify")
def spotify_disconnect():
    with sources.spotify_lock, connect() as db:
        db.execute("DELETE FROM spotify_token")
        db.execute("DELETE FROM oauth_states")
    return {"ok": True}


@app.get("/api/playlists")
def list_playlists(deleted: bool = False):
    with connect() as db:
        return [dict(row) for row in db.execute(
            """SELECT p.id,p.name,p.created,count(t.id) AS total FROM playlists p
               JOIN tracks t ON p.id=t.playlist_id
               LEFT JOIN trashed_playlists x ON x.playlist_id=p.id
               WHERE (x.playlist_id IS NOT NULL)=?
               GROUP BY p.id ORDER BY x.trashed DESC,p.created DESC,p.rowid DESC""",
            (deleted,),
        )]


@app.post("/api/playlists/spotify", status_code=201)
def import_spotify(payload: SpotifyImport):
    return {"id": library.create_playlist(*sources.load_spotify(payload.url), automatic=True)}


@app.post("/api/playlists/file", status_code=201)
def import_manifest(file: UploadFile = File(...), name: str = Form(default="", max_length=300)):
    filename = file.filename or ""
    limit = 5 * 1024 * 1024
    if Path(filename).suffix.lower() not in (".json", ".csv"):
        raise HTTPException(422, "Загрузи JSON или CSV.")
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, "Список превышает 5 МБ.")
    return {"id": library.create_playlist(*sources.from_uploaded_manifest(data, filename, name.strip()))}


@app.get("/api/playlists/{playlist}")
def load_playlist(playlist: str):
    return library.load_playlist(playlist)


@app.post("/api/playlists/{playlist}/trash")
def trash_playlist(playlist: str):
    library.trash_playlist(playlist)
    return {"ok": True}


@app.post("/api/playlists/{playlist}/restore")
def restore_playlist(playlist: str):
    library.restore_playlist(playlist)
    return {"ok": True}


@app.post("/api/playlists/{playlist}/automatic", status_code=202)
def automatic_search(playlist: str):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        library.get_playlist(db, playlist)
        return {"queued": library.enqueue_automatic(db, playlist)}


@app.post("/api/playlists/{playlist}/audio", status_code=201)
def upload_audio(playlist: str, file: UploadFile = File(...), track_id: str | None = Form(None)):
    filename = Path((file.filename or "audio").replace("\\", "/")).name
    suffix = Path(filename).suffix.lower()
    if suffix not in SUFFIXES:
        raise HTTPException(422, "Этот формат аудио не поддерживается.")
    return library.store_upload(file.file, filename[:255], suffix, playlist, track_id)


@app.post("/api/tracks/{track_id}/choose", status_code=202)
def choose_audio(track_id: str, choice: Choice):
    library.choose(track_id, **choice.model_dump())
    return {"ok": True}


@app.post("/api/tracks/{track_id}/reject-review")
def reject_review(track_id: str):
    library.reject_review(track_id)
    return {"ok": True}


@app.post("/api/tracks/{track_id}/download", status_code=201)
def fetch_audio(track_id: str, request: DownloadRequest):
    with connect() as db:
        row = library.get_track(db, track_id)
    if row["state"] not in ("missing", "review", "error"):
        raise HTTPException(409, "Для этой позиции новый файл сейчас не требуется.")
    try:
        if request.url:
            return downloads.download_audio(request.url.strip(), row["playlist_id"], track_id)
        return downloads.jamendo_audio(decode(row["meta"]), row["playlist_id"], track_id)
    except HTTPException as exc:
        with connect() as db:
            db.execute("UPDATE tracks SET reason=? WHERE id=? AND state='missing'", (exc.detail, track_id))
        raise


@app.post("/api/tracks/{track_id}/reset")
def reset_choice(track_id: str):
    library.reset_choice(track_id)
    return {"ok": True}


@app.put("/api/tracks/{track_id}/export-selection")
def export_selection(track_id: str, payload: ExportChoice):
    library.set_export_selection(track_id, payload.included)
    return {"ok": True}


@app.get("/api/tracks/{track_id}/preview")
def preview_render(track_id: str):
    with connect() as db:
        row = library.get_track(db, track_id)
    path = RENDERS / (row["id"] + ".mp3")
    if row["state"] != "ready" or not path.is_file():
        raise HTTPException(404, "Готовый MP3 пока недоступен.")
    return FileResponse(path, media_type="audio/mpeg")


@app.get("/api/audio/{asset_id}")
def preview_audio(asset_id: str):
    with connect() as db:
        row = db.execute("SELECT meta FROM assets WHERE id=?", (asset_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Файл не найден.")
    suffix = decode(row["meta"])["suffix"]
    path = ASSETS / (asset_id + suffix)
    types = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".wav": "audio/wav", ".m4a": "audio/mp4",
             ".ogg": "audio/ogg", ".opus": "audio/ogg"}
    if suffix not in types or not path.is_file():
        raise HTTPException(404, "Для этого файла предпросмотр недоступен.")
    return FileResponse(path, media_type=types[suffix])


@app.get("/api/tracks/{track_id}/sources")
def find_sources(track_id: str, soundcloud: bool = False):
    with connect() as db:
        track = decode(library.get_track(db, track_id)["meta"])
    if track.get("unsupported"):
        raise HTTPException(422, "Для этой позиции нет данных о музыкальном треке.")
    result = {"links": sources.catalog_links(track), "matches": []}
    if soundcloud:
        result["matches"] = sources.soundcloud_search(track)
    return result


@app.get("/api/playlists/{playlist}/export")
def export_playlist(playlist: str):
    archive, filename, delivered = library.export_zip(playlist)
    return FileResponse(archive, filename=filename, media_type="application/zip",
                        background=BackgroundTask(finish_export, archive, delivered))


@app.get("/")
def home():
    return FileResponse(DIST / "index.html")


if (DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")
