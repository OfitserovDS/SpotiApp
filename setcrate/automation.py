import logging
import time

from fastapi import HTTPException

from . import catalogs, downloads, hitmos, library
from .db import connect, decode
from .matching import compare, hitmos_needs_listening, verified_recording

LOG = logging.getLogger("setcrate.automation")


def promote(track_id, asset_id, allow_probable=True):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = library.get_track(db, track_id)
        if row["state"] not in ("missing", "review", "error"):
            return True
        item = db.execute(
            """SELECT a.meta FROM candidates c JOIN assets a ON c.asset_id=a.id
               WHERE c.track_id=? AND c.asset_id=? AND c.conflict=0""", (track_id, asset_id),
        ).fetchone()
        asset = decode(item["meta"]) if item else None
        track = decode(row["meta"])
        if asset and not (asset.get("provenance") or {}).get("manual_only") and not asset["low_quality"] and (
            verified_recording(track, asset) or (allow_probable and hitmos_needs_listening(track, asset))
        ):
            library.queue(db, track_id, asset_id)
            return True
        return False


def fetch(track, playlist_id, track_id, cooldowns):
    providers = [hitmos.fetch, catalogs.promodj_audio, catalogs.archive_audio, downloads.openverse_audio]
    if downloads.JAMENDO_ID:
        providers.insert(0, downloads.jamendo_audio)
    reason = "В проверенных источниках не нашлось файла по артисту и названию."
    for provider in providers:
        if cooldowns.get(provider, 0) > time.monotonic():
            reason = "Онлайн-каталог ограничил запросы. Повтори автопоиск позже."
            continue
        try:
            result = provider(track, playlist_id, track_id)
            if result.get("manual_only"):
                continue
            for asset_id in result.get("asset_ids") or [result["asset_id"]]:
                if promote(track_id, asset_id, allow_probable=not result.get("review_only")):
                    return None
        except HTTPException as exc:
            if exc.status_code == 429:
                cooldowns[provider] = time.monotonic() + 60
            if exc.status_code == 429 or exc.status_code >= 500:
                reason = exc.detail
    return reason


def reconsider_hitmos(track_id, track):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        candidates = db.execute(
            """SELECT a.id,a.meta FROM candidates c JOIN assets a ON c.asset_id=a.id
               WHERE c.track_id=?""", (track_id,),
        ).fetchall()
        eligible = []
        for row in candidates:
            asset = decode(row["meta"])
            if (asset.get("provenance") or {}).get("provider") != "hitmos":
                continue
            comparison = compare(track, asset, targeted=True)
            library.save_candidate(db, track_id, row["id"], comparison)
            if not comparison[1]:
                eligible.append(row["id"])
    return len(eligible) == 1 and promote(track_id, eligible[0])


def process_one(cooldowns):
    with connect() as db:
        now = time.time()
        job = db.execute(
            """UPDATE automatic_jobs SET claimed=? WHERE track_id=(
                   SELECT track_id FROM automatic_jobs WHERE claimed IS NULL OR claimed<?
                   ORDER BY rowid LIMIT 1)
               RETURNING track_id""", (now, now - 1800),
        ).fetchone()
        if not job:
            return False
        track_id = job["track_id"]
        row = library.get_track(db, track_id)
    reason = None
    try:
        if row["state"] in ("missing", "review", "error"):
            track = decode(row["meta"])
            if not reconsider_hitmos(track_id, track):
                reason = fetch(track, row["playlist_id"], track_id, cooldowns)
    except Exception:
        reason = "Не удалось проверить онлайн-источники. Запусти автопоиск ещё раз."
        LOG.exception("Automatic download failed")
    with connect() as db:
        db.execute("DELETE FROM automatic_jobs WHERE track_id=?", (track_id,))
        if reason:
            db.execute("UPDATE tracks SET reason=? WHERE id=? AND state='missing'", (reason, track_id))
    return True


def run(stop):
    cooldowns = {}
    while not stop.is_set():
        try:
            if process_one(cooldowns):
                continue
        except Exception:
            LOG.exception("Automatic queue error")
        stop.wait(1)
