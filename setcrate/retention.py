import time

from .db import ASSETS, EXPORTS, RENDERS, connect, decode

MEDIA_AGE = 24 * 3600
EXPORT_GRACE = 3600
EXPIRED_REASON = "Срок хранения аудиофайла истёк. Добавь исходник снова."


def discard(path):
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def age(path, now):
    try:
        return now - path.stat().st_mtime
    except FileNotFoundError:
        return float("inf")


def finish_export(archive, delivered):
    discard(archive)
    if not delivered:
        return
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for track_id, asset_id, stamp in delivered:
            row = db.execute("SELECT state,asset_id FROM tracks WHERE id=?", (track_id,)).fetchone()
            path = RENDERS / (track_id + ".mp3")
            if not row or row["state"] != "ready" or row["asset_id"] != asset_id:
                continue
            try:
                if path.stat().st_mtime_ns != stamp:
                    continue
            except FileNotFoundError:
                continue
            db.execute(
                """INSERT INTO export_expiry VALUES (?,?)
                   ON CONFLICT(track_id) DO UPDATE SET expires=excluded.expires""",
                (track_id, time.time() + EXPORT_GRACE),
            )


def remove_asset(db, asset_id, path):
    if not discard(path):
        return
    db.execute("UPDATE tracks SET asset_id=NULL WHERE asset_id=? AND state NOT IN ('ready','queued','processing')",
               (asset_id,))
    db.execute("DELETE FROM decisions WHERE asset_id=?", (asset_id,))
    db.execute("DELETE FROM candidates WHERE asset_id=?", (asset_id,))
    db.execute("DELETE FROM assets WHERE id=?", (asset_id,))


def sweep(now=None):
    now = time.time() if now is None else now
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        track_rows = db.execute(
            """SELECT t.id,t.asset_id,t.state,e.expires FROM tracks t
               LEFT JOIN export_expiry e ON e.track_id=t.id"""
        ).fetchall()
        retained_renders = set()
        expired_assets = set()
        for row in track_rows:
            track_id, asset_id, state, expires = row
            path = RENDERS / (track_id + ".mp3")
            if state in ("queued", "processing"):
                retained_renders.update((path.name, track_id + ".part.mp3"))
            if state != "ready":
                continue
            if (expires is not None and expires <= now) or age(path, now) >= MEDIA_AGE:
                if discard(path):
                    db.execute("DELETE FROM export_expiry WHERE track_id=?", (track_id,))
                    db.execute("DELETE FROM candidates WHERE track_id=? AND asset_id=?", (track_id, asset_id))
                    db.execute("UPDATE tracks SET state='missing',asset_id=NULL,reason=?,processed=NULL WHERE id=?",
                               (EXPIRED_REASON, track_id))
                    expired_assets.add(asset_id)
                    continue
            retained_renders.add(path.name)

        protected = {row[0] for row in db.execute(
            "SELECT DISTINCT asset_id FROM tracks WHERE state IN ('ready','queued','processing')"
        )}
        candidates = {row[0] for row in db.execute("SELECT DISTINCT asset_id FROM candidates")}
        retained_assets = set()
        for row in db.execute("SELECT id,meta FROM assets").fetchall():
            asset_id = row["id"]
            path = ASSETS / (asset_id + decode(row["meta"])["suffix"])
            expired = age(path, now) >= MEDIA_AGE or (asset_id in expired_assets and asset_id not in candidates)
            if asset_id not in protected and expired:
                remove_asset(db, asset_id, path)
            else:
                retained_assets.add(path.name)
        db.execute(
            """UPDATE tracks SET state='missing',reason=? WHERE state='review'
               AND NOT EXISTS(SELECT 1 FROM candidates WHERE candidates.track_id=tracks.id)""", (EXPIRED_REASON,)
        )
        db.execute("DELETE FROM oauth_states WHERE created < ?", (now - 600,))
        for directory, keep in ((ASSETS, retained_assets), (RENDERS, retained_renders), (EXPORTS, set())):
            for file in directory.iterdir():
                if file.name not in keep and file.is_file() and age(file, now) >= MEDIA_AGE:
                    discard(file)
