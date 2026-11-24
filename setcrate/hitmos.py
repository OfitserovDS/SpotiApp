import json
import re
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

from .downloads import download_audio
from .matching import candidate_rank, discovery_compatible, normalized, search_title, title_parts

BASE = "https://rus.hitmos.fm"
PAYLOAD = re.compile(r"self\.__next_f\.push\((.*?)\)</script>", re.DOTALL)
TRACK = re.compile(r'\{"id":\d+,"title":')
DOWNLOAD_HOST = re.compile(r"dwc\d+\.hitmos\.fm")


def tracks_from_page(page):
    decoder = json.JSONDecoder()
    for payload in PAYLOAD.finditer(page):
        try:
            packet = json.loads(payload[1])
        except ValueError:
            continue
        if not isinstance(packet, list) or len(packet) < 2 or not isinstance(packet[1], str):
            continue
        block = packet[1]
        for start in TRACK.finditer(block):
            try:
                item, _ = decoder.raw_decode(block, start.start())
            except ValueError:
                continue
            yield item


def search(track):
    params = {"q": f"{track['artist']} {track['title']}"}
    try:
        with httpx.stream("GET", BASE + "/search", params=params, timeout=8) as response:
            if response.status_code == 429:
                raise HTTPException(429, "Hitmos ограничил частоту запросов.")
            if response.status_code != 200:
                raise HTTPException(502, f"Hitmos не выполнил поиск (HTTP {response.status_code}).")
            page = bytearray()
            for chunk in response.iter_bytes():
                page.extend(chunk)
                if len(page) > 2 * 1024 * 1024:
                    raise HTTPException(502, "Ответ поиска Hitmos слишком большой.")
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Hitmos сейчас не отвечает.") from exc
    return tracks_from_page(page.decode("utf-8", errors="replace"))


def add_matches(track, query, matches):
    for item in search(query):
        url = item.get("download") or ""
        if not isinstance(url, str) or not url.startswith("https://"):
            continue
        try:
            host = urlsplit(url).hostname or ""
        except ValueError:
            continue
        if (
            not DOWNLOAD_HOST.fullmatch(host)
            or not isinstance(item.get("duration"), (int, float))
            or not isinstance(item.get("artist"), str) or not isinstance(item.get("title"), str)
        ):
            continue
        if not discovery_compatible(track, item):
            continue
        score = candidate_rank(track, item)
        if url not in matches or score < matches[url][0]:
            matches[url] = (score, item["id"])


def fetch(track, playlist_id, track_id):
    if not track.get("artist"):
        raise HTTPException(422, "Для поиска в Hitmos нужен артист.")
    matches = {}
    add_matches(track, track, matches)
    if not any(not score[0] for score, _ in matches.values()):
        primary = (track.get("artists") or [track["artist"]])[0]
        base_title = title_parts(search_title(track["title"]))[1]
        seen = {normalized(f"{track['artist']} {track['title']}")}
        for artist, title in ((primary, base_title), ("", base_title)):
            key = normalized(f"{artist} {title}")
            if not key or key in seen:
                continue
            seen.add(key)
            add_matches(track, {**track, "artist": artist, "title": title}, matches)
            if any(not score[1] for score, _ in matches.values()):
                break
    if not matches:
        raise HTTPException(422, "В Hitmos не нашлось подходящих вариантов по артисту и названию.")
    strict = [url for url, (score, _) in matches.items() if not score[0]]
    selected = strict if len(strict) == 1 else sorted(matches, key=lambda url: matches[url][0])[:3]
    asset_ids = []
    failure = None
    review_only = len(strict) != 1
    for url in selected:
        item_id = matches[url][1]
        try:
            result = download_audio(url, playlist_id, track_id, {
                "provider": "hitmos", "page": f"{BASE}/song/{item_id}", "review_only": review_only,
                "manual_only": matches[url][0][0],
            })
        except HTTPException as exc:
            failure = exc
            continue
        asset_ids.append(result["asset_id"])
    if not asset_ids:
        raise failure or HTTPException(422, "Hitmos не отдал ни одного файла.")
    return {"asset_ids": list(dict.fromkeys(asset_ids)), "review_only": review_only,
            "manual_only": not strict}
