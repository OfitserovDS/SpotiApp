import json
from pathlib import Path
from threading import Lock


class SearchCache:
    def __init__(self, cache_file: str):
        self._cache_file = Path(cache_file)
        self._lock = Lock()
        self._data = self._load()

    def _load(self) -> dict:
        if not self._cache_file.exists():
            return {}

        try:
            with self._cache_file.open("r", encoding="utf-8") as file:
                return json.load(file)
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self) -> None:
        with self._lock:
            with self._cache_file.open("w", encoding="utf-8") as file:
                json.dump(self._data, file, ensure_ascii=False, indent=2)

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def mark(self, key: str) -> None:
        with self._lock:
            self._data[key] = True
        self.save()

