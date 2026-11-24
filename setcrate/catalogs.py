import re
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit

from fastapi import HTTPException

from .audio import SUFFIXES
from .downloads import download_audio
from .matching import candidate_rank, discovery_compatible, search_title, title_parts
from .sources import api

PROMO = "https://promodj.com"
ARCHIVE = "https://archive.org"
OPEN_LICENSE = re.compile(
    r"https?://creativecommons\.org/(?:(?:licenses/(?:by|by-sa)|publicdomain/(?:zero|mark))"
    r"/[0-9.]+(?:/[a-z]{2})?|licenses/publicdomain)/?"
)
LOSSLESS = {".flac", ".wav", ".aiff", ".aif"}


class PromoResult(HTMLParser):
    def __init__(self, item_id):
        super().__init__()
        self.item_id = item_id
        self.fields = {"title": "", "duration": ""}
        self.capture = None
        self.page = ""
        self.download = ""
        self.lossless = ""

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        classes = (attrs.get("class") or "").split()
        if tag == "span" and "player_standard_tool__time" in classes:
            self.capture = "duration"
        if tag != "a":
            return
        url = attrs.get("href") or ""
        if not url.startswith(PROMO + "/"):
            return
        path = urlsplit(url).path
        if attrs.get("amba") == "file:" + self.item_id:
            self.page, self.capture = url, "title"
        if "player_standard_tool__downloads" in classes and path.startswith(f"/download/{self.item_id}/"):
            self.download = url
        if "player_standard_tool__bitrate_lossless" in classes and path.startswith(f"/source/{self.item_id}/"):
            self.lossless = url

    def handle_data(self, data):
        if self.capture:
            self.fields[self.capture] += data

    def handle_endtag(self, tag):
        if tag in ("a", "span"):
            self.capture = None

    def offer(self):
        if not self.download or not self.page:
            return None
        name = re.split(r"\s+[-–—]\s+", self.fields["title"].strip(), maxsplit=1)
        if len(name) != 2 or not all(name):
            return None
        parts = self.fields["duration"].strip().split(":")
        duration = sum(int(part) * 60 ** i for i, part in enumerate(reversed(parts))) if all(
            part.isdecimal() for part in parts
        ) else None
        return {
            "artist": name[0], "title": name[1], "duration": duration,
            "url": self.lossless or self.download, "page": self.page,
        }


def promodj_offers(track):
    artist = (track.get("artists") or [track["artist"]])[0]
    base = title_parts(search_title(track["title"]))[1]
    result = api("GET", PROMO + "/search", params={
        "searchfor": f"{artist} {base}", "mode": "audio", "results": 1,
    })
    for item in result.get("results", [])[:15]:
        item_id = str(item.get("kindID") or "")
        if item.get("kind") != "file" or not item_id.isdecimal():
            continue
        parsed = PromoResult(item_id)
        parsed.feed(item.get("html") or "")
        offer = parsed.offer()
        if offer and discovery_compatible(track, offer):
            yield offer


def quoted_search(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def first_text(value):
    if isinstance(value, list):
        value = value[0] if value else ""
    return value if isinstance(value, str) else ""


def archive_offers(track):
    if not track["artist"]:
        return
    base = title_parts(search_title(track["title"]))[1]
    artist = (track.get("artists") or [track["artist"]])[0]
    if not base:
        return
    result = api("GET", ARCHIVE + "/advancedsearch.php", params={
        "q": f"mediatype:audio AND licenseurl:* AND title:{quoted_search(base)} AND creator:{quoted_search(artist)}",
        "fl[]": "identifier", "rows": 5, "page": 1, "output": "json",
    })
    for row in result.get("response", {}).get("docs", [])[:5]:
        item_id = first_text(row.get("identifier"))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", item_id):
            continue
        item = api("GET", f"{ARCHIVE}/metadata/{item_id}")
        meta = item.get("metadata") or {}
        license_url = first_text(meta.get("licenseurl"))
        offer = {"artist": first_text(meta.get("creator")), "title": first_text(meta.get("title")), "duration": None}
        if (item.get("is_dark") or meta.get("access-restricted-item") in (True, "true")
                or not OPEN_LICENSE.fullmatch(license_url) or not discovery_compatible(track, offer)):
            continue
        audio = []
        for entry in item.get("files", []):
            name = first_text(entry.get("name"))
            if (entry.get("source") == "original" and not entry.get("private")
                    and PurePosixPath(name).suffix.lower() in SUFFIXES):
                audio.append(name)
        if audio and len({PurePosixPath(name).with_suffix("").as_posix().casefold() for name in audio}) == 1:
            name = min(audio, key=lambda path: PurePosixPath(path).suffix.lower() not in LOSSLESS)
            yield {**offer, "url": f"{ARCHIVE}/download/{item_id}/{quote(name, safe='')}",
                   "page": f"{ARCHIVE}/details/{item_id}", "license": license_url}


def download_best(track, playlist_id, track_id, provider, offers):
    candidate = min(offers, key=lambda item: candidate_rank(track, item), default=None)
    if not candidate:
        raise HTTPException(422, "В источнике нет подходящего файла для прослушивания.")
    result = download_audio(candidate["url"], playlist_id, track_id, {
        "provider": provider, "page": candidate["page"], "license": candidate.get("license", ""),
        "manual_only": True,
    })
    return {**result, "manual_only": True}


def promodj_audio(track, playlist_id, track_id):
    return download_best(track, playlist_id, track_id, "promodj", promodj_offers(track))


def archive_audio(track, playlist_id, track_id):
    return download_best(track, playlist_id, track_id, "archive", archive_offers(track))
