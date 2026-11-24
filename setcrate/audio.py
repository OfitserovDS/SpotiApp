import json
import math
import re
import subprocess
from pathlib import Path

from mutagen import File as read_tags

SUFFIXES = {".mp3", ".flac", ".wav", ".aiff", ".aif", ".m4a", ".aac", ".ogg", ".opus"}
FORMAT_OPTIONS = [
    "-protocol_whitelist", "file", "-format_whitelist",
    "mp3,flac,wav,aiff,mov,m4a,mp4,aac,ogg",
]
TARGET = -14.0
PEAK = -1.0


def run(args, timeout=180):
    return subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=True,
    )


def probe(path):
    response = run([
        "ffprobe", "-v", "error", *FORMAT_OPTIONS, "-show_streams",
        "-show_format", "-of", "json", str(path),
    ], timeout=20)
    body = json.loads(response.stdout)
    stream = next(item for item in body["streams"] if item["codec_type"] == "audio")
    fmt = body["format"]
    tags = {key.casefold(): str(value) for key, value in fmt.get("tags", {}).items()}
    tags.update({key.casefold(): str(value) for key, value in stream.get("tags", {}).items()})
    try:
        audio = read_tags(path, easy=True)
        if audio and audio.tags:
            for field in ("title", "artist", "album", "isrc"):
                values = audio.get(field)
                if values:
                    tags[field] = ", ".join(values)
    except (OSError, ValueError, KeyError):
        pass
    duration = float(stream.get("duration") or fmt["duration"])
    if not math.isfinite(duration) or not 1 <= duration <= 3600:
        raise ValueError("Длительность файла должна быть от 1 секунды до 1 часа.")
    codec = stream["codec_name"]
    bitrate = int(stream.get("bit_rate") or fmt.get("bit_rate") or 0)
    isrc = re.sub(r"[^A-Z0-9]", "", tags.get("isrc", tags.get("tsrc", "")).upper())
    return {
        "title": tags.get("title", "")[:300], "artist": tags.get("artist", "")[:300],
        "album": tags.get("album", "")[:300], "isrc": isrc if len(isrc) == 12 else "",
        "duration": duration, "codec": codec, "bitrate": bitrate,
        "rate": int(stream["sample_rate"]), "channels": int(stream["channels"]),
        "low_quality": not (codec.startswith("pcm_") or codec in {"flac", "alac"})
        and not (codec in {"mp3", "aac"} and bitrate >= 315_000),
    }


def measure(path):
    output = run([
        "ffmpeg", "-hide_banner", "-nostdin", "-xerror", *FORMAT_OPTIONS, "-i", str(path),
        "-map", "0:a:0", "-ac", "2", "-af",
        "aformat=channel_layouts=stereo,loudnorm=print_format=json", "-f", "null", "-",
    ]).stderr
    blocks = re.findall(r'\{\s*"input_i"[^{}]*\}', output)
    result = json.loads(blocks[-1])
    loudness, peak = float(result["input_i"]), float(result["input_tp"])
    if not all(map(math.isfinite, (loudness, peak))):
        raise ValueError("В файле нет сигнала, пригодного для нормализации.")
    return loudness, peak


def render(source: Path, destination: Path, track, asset):
    loudness, peak = measure(source)
    gain = min(TARGET - loudness, PEAK - peak - 0.3)
    temporary = destination.with_suffix(".part.mp3")
    try:
        for _ in range(3):
            args = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-xerror",
                *FORMAT_OPTIONS, "-i", str(source), "-map", "0:a:0", "-map_metadata", "-1",
                "-af", f"aformat=channel_layouts=stereo,volume={gain:.4f}dB",
                "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "320k",
                "-id3v2_version", "3", "-metadata", f"title={asset['title'] or track['title']}",
                "-metadata", f"artist={asset['artist'] or track['artist']}",
                "-metadata", f"album={asset['album'] or track.get('album', '')}",
            ]
            if track.get("isrc"):
                args += ["-metadata", f"TSRC={track['isrc']}"]
            run([*args, str(temporary)])
            final_loudness, final_peak = measure(temporary)
            if final_peak <= PEAK:
                output = probe(temporary)
                if output["codec"] != "mp3" or abs(output["bitrate"] - 320_000) > 500:
                    raise ValueError("Результат не прошёл проверку MP3 320 кбит/с.")
                temporary.replace(destination)
                return {
                    "input_lufs": loudness, "lufs": final_loudness, "dbtp": final_peak,
                    "gain": round(gain, 2), "target_limited": abs(final_loudness - TARGET) > 1,
                }
            gain -= final_peak - PEAK + 0.2
        raise ValueError("Пики готового MP3 превышают допустимый уровень.")
    finally:
        temporary.unlink(missing_ok=True)
