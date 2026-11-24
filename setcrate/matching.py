import re
import unicodedata

VERSIONS = re.compile(
    r"\b(extended(?: mix| edit| version)?|radio(?: mix| edit| version)?|"
    r"remix|rework|bootleg|mashup|dub(?: mix)?|instrumental|acapella|a cappella|"
    r"live|acoustic|cover|karaoke|tribute|sped up|slowed|nightcore|"
    r"clean|explicit|remaster(?:ed)?|original mix|version|edit|mix)\b",
    re.IGNORECASE,
)
QUALIFIERS = re.compile(r"\(([^()]+)\)|\[([^\[\]]+)\]|\s[-–—]\s(.+)$")
CREDITS = re.compile(
    r"\s*(?:\(\s*(?:feat\.?|ft\.?|featuring|with)\s+[^()]+\)"
    r"|\[\s*(?:feat\.?|ft\.?|featuring|with)\s+[^\[\]]+\]"
    r"|[-–—]\s*(?:feat\.?|ft\.?|featuring|with)\s+.+$)",
    re.IGNORECASE,
)
CATALOG_ANNOTATION = re.compile(
    r"\s*\(\s*(?:музыка из|песня из|мелодия из|ост\b|ретро музыка|музыка на|сезон \d|серия \d)[^()]*\)",
    re.IGNORECASE,
)
SOURCE_TAG = re.compile(r"\s*\(\s*zaycev\.net\s*\)\s*$", re.IGNORECASE)
EPISODE_PREFIX = re.compile(r"^s\d{1,2}e\d{1,3}\.\s*", re.IGNORECASE)


def readable_tag(value):
    value = value or ""
    if len(value) >= 2 and all(ord(char) % 256 == 0 and 32 <= ord(char) // 256 <= 126 for char in value):
        return "".join(chr(ord(char) // 256) for char in value)
    return value


def normalized(value):
    value = unicodedata.normalize("NFKC", readable_tag(value)).casefold()
    value = re.sub(r"(?<=\w)['’ʼ](?=\w)", "", value)
    return " ".join(re.findall(r"[^\W_]+", value))


def artist_key(value):
    value = EPISODE_PREFIX.sub("", readable_tag(value))
    if re.search(r",\s*the\s*$", value, re.IGNORECASE):
        value = re.sub(r",\s*the\s*$", "", value, flags=re.IGNORECASE)
    return normalized(value.replace("&", " and ")).removeprefix("the ")


def title_parts(value):
    value = SOURCE_TAG.sub("", readable_tag(value))
    title = normalized(value)
    qualifiers = [
        normalized(next(part for part in match.groups() if part))
        for match in QUALIFIERS.finditer(value or "")
        if VERSIONS.search(match.group())
    ]
    remainder = QUALIFIERS.sub(
        lambda match: "" if VERSIONS.search(match.group()) else match.group(), value or ""
    )
    return title, normalized(remainder), tuple(sorted(qualifiers))


def search_title(value):
    return CATALOG_ANNOTATION.sub("", CREDITS.sub("", readable_tag(value))).strip()


def review_artist_matches(track, asset):
    def key(value):
        letters = unicodedata.normalize("NFKD", (value or "").replace("$", "s"))
        plain = "".join(char for char in letters if not unicodedata.combining(char))
        return " ".join(word for word in artist_key(plain).split() if word not in ("and", "feat", "featuring", "ft", "with"))

    full_credit = asset["artist"] or ""
    lead = re.split(r"\s+(?:feat\.?|ft\.?|featuring)\s+", full_credit, maxsplit=1, flags=re.IGNORECASE)[0]
    candidates = {key(full_credit), key(lead)} - {""}
    expected = {key(track["artist"]), *(key(name) for name in track.get("artists", []))}
    return bool(candidates & expected)


def preview_compatible(track, asset):
    if not track.get("duration") or not asset.get("duration") or abs(track["duration"] - asset["duration"]) > 5:
        return False
    if not review_artist_matches(track, asset):
        return False
    if track.get("isrc") and asset.get("isrc") and track["isrc"] != asset["isrc"]:
        return False
    _, title, version = title_parts(search_title(track["title"]))
    _, other, other_version = title_parts(search_title(asset["title"]))
    return bool(title and title == other and (not version or not other_version or version == other_version))


def discovery_compatible(track, asset):
    if not review_artist_matches(track, asset):
        return False
    _, title, _ = title_parts(search_title(track["title"]))
    _, other_title, _ = title_parts(search_title(asset["title"]))
    if not title or title != other_title:
        return False
    expected, actual = track.get("duration"), asset.get("duration")
    return not (expected and actual) or 0.55 * expected <= actual <= 1.8 * expected


def candidate_rank(track, asset):
    expected, actual = track.get("duration"), asset.get("duration")
    return (
        not recording_description_matches(track, asset),
        not preview_compatible(track, asset),
        title_parts(search_title(track["title"])) != title_parts(search_title(asset["title"])),
        abs(expected - actual) if expected and actual else float("inf"),
    )


def compare(track, asset, targeted=False):
    """Return (reason, conflict), or None when there is no evidence of a match."""
    provenance = asset.get("provenance") or {}
    if (targeted and provenance.get("provider") == "hitmos"
            and not recording_description_matches(track, asset) and preview_compatible(track, asset)):
        return "Есть близкое совпадение. Прослушай и подтверди версию.", False
    title, base, version = title_parts(track["title"])
    other, other_base, other_version = title_parts(asset["title"])
    artist = artist_key(track["artist"])
    other_artist = artist_key(asset["artist"])
    aliases = {artist_key(name) for name in track.get("artists", [])}
    artist_close = bool(other_artist and (artist == other_artist or other_artist in aliases))
    same_isrc = bool(track.get("isrc") and track["isrc"] == asset.get("isrc"))
    if not same_isrc and not (base and base == other_base and artist_close):
        if not targeted:
            return None
        if other and base != other_base:
            return "Название в тегах отличается от позиции плейлиста", True
        if other_artist and artist and not artist_close:
            return "Артист в тегах отличается от позиции плейлиста", True
    if track.get("isrc") and asset.get("isrc") and not same_isrc:
        return "ISRC различаются", True
    if (other and version != other_version) or (same_isrc and other and base != other_base):
        return "Различаются обозначения версии или название", True
    expected_duration = track.get("duration")
    if expected_duration and abs(expected_duration - asset["duration"]) > 2.5:
        return "Длительность отличается больше чем на 2,5 секунды", True
    if same_isrc and title == other and artist_close and expected_duration:
        return "Совпали ISRC, название, артист и длительность. Проверь источник файла.", False
    if targeted and (not other or not other_artist):
        return "Теги неполные. Прослушай файл и подтверди точную версию.", False
    return "Есть совпадение по части метаданных. Подтверди запись и версию.", False


def identity(track):
    if track.get("spotify_id"):
        return "spotify:" + track["spotify_id"]
    if track.get("isrc") and track.get("duration"):
        return "recording:" + "|".join((
            track["isrc"], normalized(track["title"]), normalized(track["artist"]),
            str(round(track["duration"], 2)),
        ))
    return None


def recording_description_matches(track, asset):
    if not track.get("duration") or not asset.get("duration") or abs(track["duration"] - asset["duration"]) > 2:
        return False
    if not artist_key(track["artist"]) or artist_key(track["artist"]) != artist_key(asset["artist"]):
        return False
    if title_parts(track["title"]) != title_parts(asset["title"]):
        return False
    album = normalized(track.get("album"))
    source_album = normalized(asset.get("album"))
    if any("remaster" in value for value in (album, source_album)) and album != source_album:
        return False
    return True


def verified_recording(track, asset):
    isrc = track.get("isrc") or ""
    return (
        bool(re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}\d{7}", isrc))
        and isrc == asset.get("isrc")
        and recording_description_matches(track, asset)
    )


def hitmos_needs_listening(track, asset):
    provenance = asset.get("provenance") or {}
    return (
        provenance.get("provider") == "hitmos"
        and not provenance.get("review_only")
        and not (track.get("isrc") and asset.get("isrc"))
        and recording_description_matches(track, asset)
    )
