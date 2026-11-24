import http.client
import ipaddress
import os
import re
import socket
import ssl
import time
from email.message import Message
from pathlib import PurePosixPath
from threading import BoundedSemaphore
from urllib.parse import quote, unquote, urljoin, urlsplit

from fastapi import HTTPException

from .audio import SUFFIXES
from .library import LIMIT, store_upload
from .matching import normalized, title_parts
from .sources import api

_slots = BoundedSemaphore(3)
JAMENDO_ID = os.environ.get("JAMENDO_CLIENT_ID", "").strip()
_blocked_hosts = ("youtube.com", "youtu.be", "googlevideo.com", "ytimg.com",
                  "soundcloud.com", "sndcdn.com", "spotify.com", "scdn.co", "spotifycdn.com")
_audio_types = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/flac": ".flac", "audio/x-flac": ".flac",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/aiff": ".aiff", "audio/x-aiff": ".aiff",
    "audio/mp4": ".m4a", "audio/aac": ".aac", "audio/ogg": ".ogg", "application/ogg": ".ogg",
    "audio/opus": ".opus",
}
_provider_domains = {"promodj": "promodj.com", "archive": "archive.org"}


def public_destination(url):
    try:
        address = urlsplit(url)
        host = (address.hostname or "").rstrip(".").encode("idna").decode().lower()
        if (address.scheme != "https" or not host or address.port not in (None, 443)
                or address.username is not None or address.password is not None
                or any(ord(char) <= 32 or char == "\\" for char in url)):
            raise ValueError()
        if any(host == blocked or host.endswith("." + blocked) for blocked in _blocked_hosts):
            raise HTTPException(422, "Для этой площадки скачай файл в браузере и добавь его в Setcrate.")
        targets = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        ips = [ipaddress.ip_address(item[4][0]) for item in targets]
        if not ips or any(not ip.is_global or ip.is_multicast for ip in ips):
            raise ValueError()
    except (UnicodeError, ValueError, OSError) as exc:
        raise HTTPException(422, "Нужна прямая HTTPS-ссылка на файл с публичного сервера.") from exc
    path = quote(address.path or "/", safe="/%:@!$&'()*+,;=-._~")
    if address.query:
        path += "?" + quote(address.query, safe="/%?:@!$&'()*+,;=-._~")
    return host, targets, path


def pinned_connection(host, targets):
    connection = http.client.HTTPSConnection(host, timeout=10, context=ssl.create_default_context())

    def connect_to_public_ip(_address, timeout, source_address=None):
        # Pin DNS validation to the actual TCP connection while TLS still verifies the original hostname.
        for family, kind, protocol, _, peer in sorted(targets, key=lambda item: item[0] != socket.AF_INET):
            sock = socket.socket(family, kind, protocol)
            try:
                sock.settimeout(timeout)
                sock.connect(peer)
                return sock
            except OSError:
                sock.close()
        raise OSError("No reachable public address")

    connection._create_connection = connect_to_public_ip
    return connection


class DownloadStream:
    def __init__(self, response, deadline):
        self.response = response
        self.deadline = deadline
        raw_length = response.getheader("Content-Length")
        if raw_length and raw_length.isdecimal() and len(raw_length) > 12:
            raise HTTPException(413, "Файл превышает лимит 180 МБ.")
        self.expected = int(raw_length) if raw_length and raw_length.isdecimal() else None
        self.received = 0
        if self.expected is not None and self.expected > LIMIT:
            raise HTTPException(413, "Файл превышает лимит 180 МБ.")

    def read(self, size):
        if time.monotonic() >= self.deadline:
            raise HTTPException(504, "Источник слишком долго отдаёт файл.")
        data = self.response.read1(size)
        self.received += len(data)
        if not data and self.expected is not None and self.received != self.expected:
            raise HTTPException(502, "Источник оборвал загрузку. Попробуй снова.")
        return data


def file_name(response, path):
    content_type = response.getheader("Content-Type", "").split(";", 1)[0].lower().strip()
    if content_type and content_type not in _audio_types and content_type not in (
        "application/octet-stream", "binary/octet-stream", "application/download",
    ):
        raise HTTPException(422, "Ссылка открывает страницу или поток. Нужна прямая ссылка на аудиофайл.")
    content_encoding = response.getheader("Content-Encoding", "identity").strip().lower()
    if content_encoding != "identity":
        raise HTTPException(422, "Источник отдаёт файл в неподдерживаемой упаковке.")
    headers = Message()
    headers["Content-Disposition"] = response.getheader("Content-Disposition", "")
    name = headers.get_filename() or unquote(path.split("?", 1)[0]).rstrip("/").rsplit("/", 1)[-1]
    name = str(name).replace("\\", "/").rsplit("/", 1)[-1][:240]
    suffix = PurePosixPath(name).suffix.lower()
    if suffix not in SUFFIXES:
        suffix = _audio_types.get(content_type)
        if not suffix:
            raise HTTPException(422, "Источник не указал поддерживаемый аудиоформат.")
        name = "Аудиофайл" + suffix
    return name, suffix


def download_audio(url, playlist_id, track_id, provenance=None):
    if not _slots.acquire(blocking=False):
        raise HTTPException(429, "Сейчас скачивается три файла. Попробуй чуть позже.")
    deadline = time.monotonic() + 90
    try:
        for _ in range(5):
            host, targets, path = public_destination(url)
            provider = (provenance or {}).get("provider")
            if provider == "hitmos" and not re.fullmatch(r"dwc\d+\.hitmos\.fm", host):
                raise HTTPException(422, "Hitmos перенаправил загрузку на другой источник.")
            domain = _provider_domains.get(provider)
            if domain and host != domain and not host.endswith("." + domain):
                raise HTTPException(422, "Источник перенаправил загрузку на другой домен.")
            connection = pinned_connection(host, targets)
            try:
                connection.request("GET", path, headers={"Accept-Encoding": "identity", "User-Agent": "Setcrate/1"})
                with connection.getresponse() as response:
                    redirect = response.getheader("Location")
                    if response.status in (301, 302, 303, 307, 308) and redirect:
                        url = urljoin(url, redirect)
                        continue
                    if response.status != 200:
                        raise HTTPException(422, f"Источник не отдал файл (HTTP {response.status}).")
                    name, suffix = file_name(response, path)
                    return store_upload(DownloadStream(response, deadline), name, suffix, playlist_id, track_id,
                                        provenance=provenance)
            finally:
                connection.close()
        raise HTTPException(422, "Источник использует слишком много перенаправлений.")
    except (OSError, http.client.HTTPException) as exc:
        raise HTTPException(502, "Не удалось скачать файл с источника.") from exc
    finally:
        _slots.release()


def openverse_audio(track, playlist_id, track_id):
    if not track.get("duration") or not normalized(track.get("artist")):
        raise HTTPException(422, "Для автопоиска нужны артист, название и длительность.")
    result = api("GET", "https://api.openverse.org/v1/audio/", params={
        "title": track["title"], "creator": track["artist"], "license": "cc0,pdm,by,by-sa",
        "excluded_source": "jamendo", "category": "music", "page_size": 20,
    })
    hits = {}
    for entry in result.get("results", []):
        url = entry.get("url") or ""
        license_url = entry.get("license_url") or ""
        duration = entry.get("duration")
        if (
            url.startswith("https://") and entry.get("source") != "jamendo"
            and license_url.startswith(("https://creativecommons.org/", "http://creativecommons.org/"))
            and entry.get("license") in ("cc0", "pdm", "by", "by-sa")
            and title_parts(entry.get("title")) == title_parts(track["title"])
            and normalized(entry.get("creator")) == normalized(track["artist"])
            and isinstance(duration, (float, int)) and abs(duration / 1000 - track["duration"]) <= 2
        ):
            hits[url] = {"provider": "openverse", "page": entry.get("foreign_landing_url") or url, "license": license_url}
    if len(hits) != 1:
        raise HTTPException(422, "Openverse не нашёл единственного совпадения с открытой лицензией.")
    url, provenance = hits.popitem()
    return download_audio(url, playlist_id, track_id, provenance)


def jamendo_audio(track, playlist_id, track_id):
    if not JAMENDO_ID:
        raise HTTPException(400, "Для Jamendo укажи JAMENDO_CLIENT_ID в .env.")
    if not track.get("duration") or not track.get("artist"):
        raise HTTPException(422, "Для поиска нужна длительность и имя артиста.")
    result = api("GET", "https://api.jamendo.com/v3.0/tracks/", params={
        "client_id": JAMENDO_ID, "format": "json", "limit": 30, "namesearch": track["title"],
        "artist_name": track["artist"], "type": "single albumtrack", "audiodlformat": "flac",
    })
    if result.get("headers", {}).get("status") != "success":
        raise HTTPException(502, "Jamendo не выполнил запрос. Проверь настройки ключа.")
    hits = {}
    for entry in result.get("results", []):
        url = entry.get("audiodownload") or ""
        duration = entry.get("duration")
        if (
            entry.get("audiodownload_allowed") is True
            and urlsplit(url).hostname and urlsplit(url).hostname.endswith(".jamendo.com")
            and title_parts(entry.get("name")) == title_parts(track["title"])
            and normalized(entry.get("artist_name")) == normalized(track["artist"])
            and str(duration).isdecimal() and abs(int(duration) - track["duration"]) <= 2.5
        ):
            hits[url] = {"provider": "jamendo", "page": entry.get("shareurl") or entry.get("shorturl") or "",
                         "license": entry.get("license_ccurl") or ""}
    if len(hits) != 1:
        raise HTTPException(422, "Jamendo не дал единственного совпадения с доступным оригиналом.")
    url, provenance = hits.popitem()
    return download_audio(url, playlist_id, track_id, provenance)
