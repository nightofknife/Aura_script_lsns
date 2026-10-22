"""Bounded, versioned pixel snapshots for filesystem templates and masks."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import os
import threading

import numpy as np


def _snapshot(image: np.ndarray) -> np.ndarray:
    # A bytes-backed array cannot be made writable again by a caller.
    return np.frombuffer(image.tobytes(order="C"), dtype=image.dtype).reshape(image.shape)


@dataclass
class _Entry:
    version: tuple[int, ...]
    image: np.ndarray
    prepared: OrderedDict = field(default_factory=OrderedDict)

    @property
    def nbytes(self) -> int:
        return self.image.nbytes + sum(
            image.nbytes for image in self.prepared.values() if image is not self.image
        )


class TemplateImageCache:
    """Keep RGB decoded pixels and preparation profiles, never captures/results.

    Stat every access, including hits. A service owns its cache; LRU eviction is
    bounded by both asset count and pixel bytes. The lock also prevents concurrent
    cold requests decoding the same asset twice. Failed loads are never retained.
    """

    def __init__(self, max_assets: int = 128, max_bytes: int = 64 * 1024 * 1024,
                 max_profiles: int = 8):
        self._max_assets = max_assets
        self._max_bytes = max_bytes
        self._max_profiles = max_profiles
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()

    @staticmethod
    def _version(path: str) -> tuple[int, ...]:
        info = os.stat(path)
        return (info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_dev, info.st_ino)

    def _discard(self, key: str) -> None:
        old = self._entries.pop(key, None)
        if old is not None:
            self._bytes -= old.nbytes

    def _trim(self) -> None:
        while self._entries and (
            len(self._entries) > self._max_assets or self._bytes > self._max_bytes
        ):
            self._discard(next(iter(self._entries)))

    def get(self, image_path: str, *, use_grayscale, preprocess, load, prepare) -> np.ndarray:
        # Lexical normalization is cheap even on Windows. Stat the original path
        # on every lookup: following its current symlink target preserves both
        # retargeting and file replacement invalidation without Path.resolve's
        # repeated filesystem traversal. Different aliases share the same bounded
        # LRU budget rather than accumulating an additional alias index.
        try:
            key = os.path.normcase(os.path.abspath(image_path))
        except OSError:
            return prepare(load())
        with self._lock:
            try:
                version = self._version(image_path)
            except OSError:
                self._discard(key)
                # Let the existing image loader retain its public error contract.
                return prepare(load())
            entry = self._entries.get(key)
            if entry is not None and entry.version != version:
                self._discard(key)
                entry = None
            if entry is None:
                image = _snapshot(load())
                entry = _Entry(version, image)
            else:
                self._entries.move_to_end(key)

            # Decode before profile validation, matching legacy error precedence.
            if preprocess is not None and not isinstance(preprocess, str):
                return prepare(entry.image)
            profile = (bool(use_grayscale), (preprocess or "none").lower())
            result = entry.prepared.get(profile)
            if result is not None:
                entry.prepared.move_to_end(profile)
                return result
            result = prepare(entry.image)
            result = entry.image if result is entry.image else _snapshot(result)

            # Do not publish pixels read while an asset was replaced or removed.
            try:
                unchanged = self._version(image_path) == version
            except OSError:
                unchanged = False
            if not unchanged:
                self._discard(key)
                return result
            old_bytes = entry.nbytes if key in self._entries else 0
            entry.prepared[profile] = result
            while len(entry.prepared) > self._max_profiles:
                entry.prepared.popitem(last=False)
            self._entries[key] = entry
            self._entries.move_to_end(key)
            self._bytes += entry.nbytes - old_bytes
            self._trim()
            return result
