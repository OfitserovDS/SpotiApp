from dataclasses import dataclass


@dataclass(frozen=True)
class TrackInfo:
    artist: str
    title: str
    clean_title: str
    query: str
    filename: str
    expected_dur: float

