import csv
import hashlib
import io
import os
import re
import shutil
import zipfile

from fastapi import HTTPException

from .audio import probe
from .db import ASSETS, EXPORTS, RENDERS, connect, decode, encode, new_id
from .matching import compare, identity, readable_tag

LIMIT = 180 * 1024 * 1024


def queue(db, track_id, asset_id):
    db.execute("DELETE FROM export_expiry WHERE track_id=?", (track_id,))
    db.execute(
        "UPDATE tracks SET state='queued', asset_id=?, reason=NULL, processed=NULL, claimed=NULL WHERE id=?",
        (asset_id, track_id),
    )


def save_candidate(db, track_id, asset_id, comparison):
    reason, conflict = comparison
    db.execute(
        "INSERT OR REPLACE INTO candidates VALUES (?, ?, ?, ?)",
        (track_id, asset_id, reason, int(conflict)),
    )
    db.execute("UPDATE tracks SET state='review',reason=NULL WHERE id=? AND state='missing'", (track_id,))


def enqueue_automatic(db, playlist_id):
    return db.execute(
        """INSERT OR IGNORE INTO automatic_jobs(track_id)
           SELECT id FROM tracks WHERE playlist_id=? AND state IN ('missing','review','error')""",
        (playlist_id,),
    ).rowcount


def create_playlist(name, url, tracks, automatic=False):
    if not 0 < len(tracks) <= 5000:
        raise HTTPException(422, "Поддерживаются плейлисты от 1 до 5000 позиций.")
    playlist = new_id()
    with connect() as db:
        if automatic and url:
            db.execute("BEGIN IMMEDIATE")
            same_source = db.execute(
                """SELECT p.id FROM playlists p WHERE p.url=? AND NOT EXISTS(
                       SELECT 1 FROM trashed_playlists x WHERE x.playlist_id=p.id)
                   ORDER BY p.created DESC,p.rowid DESC""", (url,),
            ).fetchall()
            for existing in same_source:
                positions = db.execute("SELECT meta FROM tracks WHERE playlist_id=? ORDER BY position",
                                       (existing["id"],)).fetchall()
                if len(positions) == len(tracks) and all(decode(row["meta"]) == track for row, track in zip(positions, tracks)):
                    return existing["id"]
        db.execute("INSERT INTO playlists (id,name,url) VALUES (?,?,?)", (playlist, name, url))
        assets = [] if automatic else [(row["id"], decode(row["meta"])) for row in db.execute("SELECT * FROM assets")]
        known = {} if automatic else dict(db.execute("SELECT track_key, asset_id FROM decisions"))
        for position, track in enumerate(tracks, start=1):
            track_id = new_id()
            blocked = track.get("unsupported")
            state = "unavailable" if blocked else "missing"
            db.execute(
                "INSERT INTO tracks (id,playlist_id,position,meta,state) VALUES (?,?,?,?,?)",
                (track_id, playlist, position, encode(track), state),
            )
            if blocked:
                continue
            prior = known.get(identity(track))
            for asset_id, asset in assets:
                result = compare(track, asset)
                if asset_id == prior and (not result or not result[1]):
                    if not asset["low_quality"]:
                        queue(db, track_id, asset_id)
                        break
                    result = ("Ранее подтверждён. Проверь качество исходника.", False)
                if result:
                    save_candidate(db, track_id, asset_id, result)
        if automatic:
            enqueue_automatic(db, playlist)
    return playlist


def get_playlist(db, playlist_id):
    row = db.execute(
        """SELECT p.* FROM playlists p WHERE p.id=? AND NOT EXISTS(
               SELECT 1 FROM trashed_playlists x WHERE x.playlist_id=p.id)""", (playlist_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Плейлист не найден. Удалённые плейлисты находятся в корзине.")
    return row


def trash_playlist(playlist_id):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        get_playlist(db, playlist_id)
        db.execute("INSERT INTO trashed_playlists(playlist_id) VALUES (?)", (playlist_id,))
        db.execute(
            "DELETE FROM automatic_jobs WHERE track_id IN (SELECT id FROM tracks WHERE playlist_id=?)",
            (playlist_id,),
        )


def restore_playlist(playlist_id):
    with connect() as db:
        if not db.execute("DELETE FROM trashed_playlists WHERE playlist_id=?", (playlist_id,)).rowcount:
            raise HTTPException(404, "Плейлист не найден в корзине.")


def get_track(db, track_id):
    row = db.execute(
        """SELECT t.* FROM tracks t WHERE t.id=? AND NOT EXISTS(
               SELECT 1 FROM trashed_playlists x WHERE x.playlist_id=t.playlist_id)""", (track_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Позиция не найдена.")
    return row


def store_upload(file, filename, suffix, playlist, target, provenance=None):
    with connect() as db:
        get_playlist(db, playlist)
    temporary = ASSETS / (new_id() + suffix)
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("xb") as out:
            while chunk := file.read(1024 * 1024):
                size += len(chunk)
                if size > LIMIT:
                    raise HTTPException(413, "Файл превышает лимит 180 МБ.")
                digest.update(chunk)
                out.write(chunk)
        try:
            asset = probe(temporary)
        except Exception as exc:
            raise HTTPException(422, "Не удалось прочитать аудио. Проверь формат и целостность файла.") from exc
        asset_id = digest.hexdigest()
        with connect() as db:
            db.execute("BEGIN IMMEDIATE")
            get_playlist(db, playlist)
            if target and get_track(db, target)["playlist_id"] != playlist:
                raise HTTPException(404, "Позиция не относится к этому плейлисту.")
            existing = db.execute("SELECT meta FROM assets WHERE id=?", (asset_id,)).fetchone()
            if existing:
                asset = decode(existing["meta"])
                path = ASSETS / (asset_id + asset["suffix"])
                if path.is_file():
                    os.utime(path)
                else:
                    temporary.replace(path)
                if provenance and not asset.get("provenance"):
                    asset["provenance"] = provenance
                    db.execute("UPDATE assets SET meta=? WHERE id=?", (encode(asset), asset_id))
            else:
                asset["suffix"] = suffix
                if provenance:
                    asset["provenance"] = provenance
                temporary.replace(ASSETS / (asset_id + suffix))
                db.execute("INSERT INTO assets VALUES (?,?,?)", (asset_id, filename, encode(asset)))
            tracks = db.execute(
                """SELECT id,meta FROM tracks WHERE state IN ('missing','review','error')
                   AND playlist_id=? AND (? IS NULL OR id=?)""",
                (playlist, target, target),
            ).fetchall()
            matched = 0
            for row in tracks:
                track = decode(row["meta"])
                result = compare(track, asset, targeted=row["id"] == target)
                if result:
                    save_candidate(db, row["id"], asset_id, result)
                    matched += 1
        return {"matched": matched, "asset_id": asset_id}
    finally:
        temporary.unlink(missing_ok=True)


def require_review(db, track_id):
    track = get_track(db, track_id)
    if (track["state"] != "review"
            or db.execute("SELECT 1 FROM automatic_jobs WHERE track_id=?", (track_id,)).fetchone()):
        raise HTTPException(409, "Выбор доступен, когда трек находится на проверке.")
    return track


def choose(track_id, asset_id, quality_confirmed, remember):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        track = require_review(db, track_id)
        candidate = db.execute(
            """SELECT a.meta FROM candidates c JOIN assets a ON a.id=c.asset_id
               WHERE c.track_id=? AND c.asset_id=?""", (track_id, asset_id)
        ).fetchone()
        asset = decode(candidate["meta"]) if candidate else None
        if not asset or not (ASSETS / (asset_id + asset["suffix"])).is_file():
            raise HTTPException(422, "Этот вариант больше недоступен.")
        if asset["low_quality"] and not quality_confirmed:
            raise HTTPException(422, "Подтверди использование исходника пониженного качества.")
        db.execute("DELETE FROM export_exclusions WHERE track_id=?", (track_id,))
        queue(db, track_id, asset_id)
        key = identity(decode(track["meta"]))
        if remember and key:
            db.execute("INSERT OR REPLACE INTO decisions VALUES (?,?)", (key, asset_id))


def reject_review(track_id):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        require_review(db, track_id)
        db.execute("DELETE FROM candidates WHERE track_id=?", (track_id,))
        db.execute(
            "UPDATE tracks SET state='missing',asset_id=NULL,processed=NULL,reason=? WHERE id=?",
            ("Ни один из вариантов не подошёл.", track_id),
        )


def reset_choice(track_id):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        track = get_track(db, track_id)
        if track["state"] not in ("ready", "error"):
            raise HTTPException(409, "Отменить можно завершённое сопоставление.")
        key = identity(decode(track["meta"]))
        db.execute("DELETE FROM decisions WHERE track_key=? AND asset_id=?", (key, track["asset_id"]))
        db.execute("DELETE FROM export_expiry WHERE track_id=?", (track_id,))
        candidates = db.execute("SELECT 1 FROM candidates WHERE track_id=? LIMIT 1", (track_id,)).fetchone()
        db.execute(
            "UPDATE tracks SET state=?, asset_id=NULL, reason=NULL, processed=NULL WHERE id=?",
            ("review" if candidates else "missing", track_id),
        )


def set_export_selection(track_id, included):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if get_track(db, track_id)["state"] != "ready":
            raise HTTPException(409, "Выбирать для ZIP можно готовые треки.")
        if included:
            db.execute("DELETE FROM export_exclusions WHERE track_id=?", (track_id,))
        else:
            db.execute("INSERT OR IGNORE INTO export_exclusions VALUES (?)", (track_id,))


def display_state(state, searching):
    if searching or state in ("queued", "processing"):
        return "working"
    return state if state in ("ready", "review") else "missing"


def load_playlist(playlist_id):
    with connect() as db:
        db.execute("BEGIN")
        playlist = get_playlist(db, playlist_id)
        raw = db.execute(
            """SELECT t.*, e.track_id IS NULL AS export_included
               FROM tracks t LEFT JOIN export_exclusions e ON e.track_id=t.id
               WHERE t.playlist_id=? ORDER BY t.position""", (playlist_id,),
        ).fetchall()
        automatic = {row[0] for row in db.execute(
            """SELECT j.track_id FROM automatic_jobs j JOIN tracks t ON t.id=j.track_id
               WHERE t.playlist_id=?""", (playlist_id,),
        )}
        candidates = {}
        rows = db.execute(
            """SELECT c.*, a.filename, a.meta FROM candidates c JOIN assets a ON a.id=c.asset_id
               JOIN tracks t ON t.id=c.track_id WHERE t.playlist_id=?
               ORDER BY c.conflict, json_extract(a.meta,'$.low_quality'),
               COALESCE(ABS(json_extract(a.meta,'$.duration')-json_extract(t.meta,'$.duration')), 1e9),
               a.rowid DESC""", (playlist_id,)
        )
        for row in rows:
            item = dict(row)
            item["meta"] = decode(item["meta"])
            for field in ("title", "artist", "album"):
                item["meta"][field] = readable_tag(item["meta"].get(field))
            shortlist = candidates.setdefault(row["track_id"], [])
            if len(shortlist) < 3:
                shortlist.append(item)
        return {
            **dict(playlist), "automatic": sorted(automatic), "tracks": [
                {**dict(row), "state": display_state(row["state"], row["id"] in automatic),
                 "reason": row["reason"] or ("Spotify не предоставил запись." if row["state"] == "unavailable" else None),
                 "meta": decode(row["meta"]), "processed": decode(row["processed"]),
                 "candidates": candidates.get(row["id"], [])}
                for row in raw
            ],
        }


def safe_name(value, length=85):
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(". ")
    return clean[:length].strip(". ") or "Трек"


STATE_LABELS = {
    "ready": "Готово", "missing": "Нет файла", "review": "На проверке", "working": "В работе",
}


def csv_safe(value):
    if isinstance(value, str) and value and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_zip(playlist_id):
    playlist = load_playlist(playlist_id)
    temporary = EXPORTS / (new_id() + ".zip")
    names = ["#EXTM3U"]
    delivered = []
    report = io.StringIO(newline="")
    csv_out = csv.writer(report)
    csv_out.writerow(["Позиция", "Артист", "Трек", "Статус", "Причина", "Файл", "LUFS", "dBTP",
                      "Spotify", "Источник", "Лицензия"])
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            for row in playlist["tracks"]:
                track, state = row["meta"], row["state"]
                relative = ""
                source = RENDERS / (row["id"] + ".mp3")
                reason = row["reason"] or ""
                measurements = row["processed"] or {}
                provenance = measurements.get("provenance") or {}
                if state == "ready" and not row["export_included"]:
                    reason = "Исключён из ZIP. Снята галочка."
                elif state == "ready":
                    try:
                        audio = source.open("rb")
                    except FileNotFoundError:
                        state, reason = "missing", "Готовый файл отсутствует на диске"
                    else:
                        with audio:
                            title = safe_name(f"{track['artist']} - {track['title']}".strip(" -"))
                            relative = f"Tracks/{row['position']:04d} - {title}.mp3"
                            stat = os.fstat(audio.fileno())
                            stamp = stat.st_mtime_ns
                            info = zipfile.ZipInfo(relative)
                            info.file_size = stat.st_size
                            with archive.open(info, "w") as target:
                                shutil.copyfileobj(audio, target, length=1024 * 1024)
                        names.append(relative)
                        delivered.append((row["id"], row["asset_id"], stamp))
                        notes = []
                        if measurements.get("target_limited"):
                            notes.append("Целевая громкость ограничена пиками")
                        if measurements.get("needs_listening"):
                            notes.append("Версия без ISRC, рекомендовано прослушивание")
                        if measurements.get("manual_conflict"):
                            notes.append("Вариант выбран вручную при расхождении метаданных")
                        reason = ". ".join(notes)
                csv_out.writerow(map(csv_safe, (
                    row["position"], track["artist"], track["title"], STATE_LABELS[state], reason, relative,
                    measurements.get("lufs", ""), measurements.get("dbtp", ""), track.get("url", ""),
                    provenance.get("page", ""), provenance.get("license", ""),
                )))
            archive.writestr("playlist.m3u8", "\n".join(names) + "\n")
            archive.writestr("report.csv", report.getvalue().encode("utf-8-sig"))
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary, safe_name(playlist["name"]) + " - Setcrate.zip", delivered
