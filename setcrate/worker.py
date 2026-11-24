import logging
import time

from .audio import render
from .db import ASSETS, RENDERS, connect, decode, encode, setup
from .matching import hitmos_needs_listening, identity, readable_tag
from .retention import sweep

LOG = logging.getLogger("setcrate.worker")


def process_one():
    with connect() as db:
        db.execute(
            "UPDATE tracks SET state='queued', claimed=NULL WHERE state='processing' AND claimed < ?",
            (time.time() - 1800,),
        )
        track = db.execute(
            """UPDATE tracks SET state='processing', claimed=?
               WHERE id=(SELECT id FROM tracks WHERE state='queued' ORDER BY rowid LIMIT 1)
               RETURNING id, asset_id, meta""", (time.time(),)
        ).fetchone()
        if not track:
            return False
        asset = decode(db.execute("SELECT meta FROM assets WHERE id=?", (track["asset_id"],)).fetchone()["meta"])
        selected = db.execute("SELECT conflict FROM candidates WHERE track_id=? AND asset_id=?",
                              (track["id"], track["asset_id"])).fetchone()
    for field in ("title", "artist", "album"):
        asset[field] = readable_tag(asset.get(field))
    try:
        track_meta = decode(track["meta"])
        metadata = render(
            ASSETS / (track["asset_id"] + asset["suffix"]), RENDERS / (track["id"] + ".mp3"),
            track_meta, asset,
        )
        if asset.get("provenance"):
            metadata["provenance"] = asset["provenance"]
        if hitmos_needs_listening(track_meta, asset):
            metadata["needs_listening"] = True
        if selected and selected["conflict"]:
            metadata["manual_conflict"] = True
        state, reason = "ready", None
    except Exception as exc:
        LOG.warning("Failed to prepare track %s (%s)", track["id"], type(exc).__name__)
        state, metadata = "missing", None
        reason = str(exc) if type(exc) is ValueError else "Не удалось обработать аудиофайл."
    with connect() as db:
        db.execute(
            "UPDATE tracks SET state=?, processed=?, reason=?, claimed=NULL WHERE id=?",
            (state, encode(metadata) if metadata else None, reason, track["id"]),
        )
        if state == "missing":
            db.execute("UPDATE tracks SET asset_id=NULL WHERE id=?", (track["id"],))
            db.execute("DELETE FROM candidates WHERE track_id=?", (track["id"],))
            db.execute("DELETE FROM decisions WHERE track_key=? AND asset_id=?",
                       (identity(decode(track["meta"])), track["asset_id"]))
    return True


def main():
    logging.basicConfig(level=logging.INFO)
    setup()
    LOG.info("Audio worker started")
    cleanup_at = 0
    while True:
        if time.monotonic() >= cleanup_at:
            cleanup_at = time.monotonic() + 60
            try:
                sweep()
            except Exception:
                LOG.exception("Media cleanup error")
        try:
            if process_one():
                continue
        except Exception:
            LOG.exception("Queue error")
        time.sleep(1)


if __name__ == "__main__":
    main()
