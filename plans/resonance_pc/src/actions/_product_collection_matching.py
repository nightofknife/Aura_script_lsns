"""Opt-in matching for collection-owned, immutable filesystem templates.

The shared room matcher retains its writable-array and exception contracts.
Only collection observations use these versioned snapshots and frame scopes.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache, wraps
from inspect import signature
from pathlib import Path
from errno import ELOOP
import math
import os
import re
import stat
import sys
import threading

import cv2
import numpy as np


_PLAN = Path(__file__).resolve().parents[2]
_PATH_TYPE = type(_PLAN)
_PATH_RESOLVE = _PATH_TYPE.resolve
try:
    from nt import _getfinalpathname as _native_final_path
except ImportError:
    _native_final_path = None
_NATIVE_DIRECTORY_RESOLUTION = (os.name == "nt" and sys.implementation.name == "cpython"
                                and sys.version_info[:2] == (3, 12))
_PLAIN_COMPONENT = re.compile(r"[A-Za-z0-9_.-]+\Z")
_OBSERVATION = ContextVar("product_collection_observation", default=None)
_ASSET_LOCK = threading.RLock()
_CURRENT_OBSERVATION = object()


def _snapshot(image):
    return np.frombuffer(image.tobytes(order="C"), dtype=image.dtype).reshape(image.shape)


class _Template:
    def __init__(self, image):
        self.image = _snapshot(image)
        self._gray = None
        self._std = None

    def gray(self):
        with _ASSET_LOCK:
            if self._gray is None:
                self._gray = _snapshot(cv2.cvtColor(self.image, cv2.COLOR_BGR2GRAY))
            return self._gray

    def std(self):
        with _ASSET_LOCK:
            if self._std is None:
                self._std = float(self.gray().std())
            return self._std


def _version(path):
    info = path.stat()
    return _version_info(info)


def _version_info(info):
    return (info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_dev, info.st_ino)


@lru_cache(maxsize=160)
def _load_template(path, version):
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"Template not readable: {path}")
    template = _Template(image)
    if _version(path) != version:
        raise ValueError(f"Template changed while loading: {path}")
    return template


def _location_parts(path):
    name = path.name
    ordinary = (bool(name) and name not in (".", "..") and ":" not in name
                and "\0" not in name and not name.endswith((".", " ")))
    return path, path.parent, name, ordinary


@lru_cache(maxsize=512)
def _logical_location(plan_text, relative_path):
    # Pure path construction only: no file metadata or resolution is cached.
    return _location_parts(Path(plan_text) / relative_path)


@lru_cache(maxsize=512)
def _child_path(parent_text, name):
    # Exact strings keep path spelling (including error messages) intact.
    return Path(parent_text) / name


@lru_cache(maxsize=512)
def _plain_directory_text(text):
    # A conservative subset where removing \\?\ cannot change Win32 meaning.
    # This is syntax only; it neither caches nor proves filesystem state.
    if not (3 < len(text) < 248 and text[0] in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
            and text[1:3] == ":\\"):
        return False
    for component in text[3:].split("\\"):
        if not _PLAIN_COMPONENT.fullmatch(component) or component.endswith("."):
            return False
        stem = component.split(".", 1)[0].upper()
        if stem in {"CON", "PRN", "AUX", "NUL"} or (len(stem) == 4
                and stem[:3] in {"COM", "LPT"} and stem[3] in "0123456789"):
            return False
    return True


def _check_directory_loop(exc):
    if exc.errno == ELOOP or getattr(exc, "winerror", 0) == 1921:
        raise RuntimeError("Symlink loop from %r" % exc.filename)


def _resolve_directory(path):
    # CPython 3.12 realpath queries the final pathname twice to verify prefix
    # removal. For short, plain input AND output that check is redundant.
    # Query the entire logical path each time; never trust a cached ancestor.
    if (_NATIVE_DIRECTORY_RESOLUTION and callable(_native_final_path)
            and type(path) is _PATH_TYPE and type(path).resolve is _PATH_RESOLVE
            and _plain_directory_text(str(path))):
        try:
            os.getcwd()  # realpath does this even for absolute paths; retain errors.
        except OSError as exc:
            _check_directory_loop(exc)
            raise
        try:
            final = _native_final_path(str(path))
        except (OSError, ValueError):
            final = None
        if (type(final) is str and final.startswith("\\\\?\\")
                and _plain_directory_text(final[4:])):
            resolved = Path(final[4:])
            # Preserve Path.resolve's non-strict post-check and loop errors.
            try:
                resolved.stat()
            except OSError as exc:
                _check_directory_loop(exc)
            return resolved
    # Outside the probe's except block: retain original exception chaining.
    return path.resolve()


def _asset(relative_path, *, frame=_CURRENT_OBSERVATION):
    if type(relative_path) is str and type(_PLAN) is _PATH_TYPE:
        logical_path, logical_parent, name, ordinary_child = _logical_location(str(_PLAN), relative_path)
    else:
        logical_path, logical_parent, name, ordinary_child = _location_parts(_PLAN / relative_path)
    context = _OBSERVATION.get()
    if context is not None and frame is not _CURRENT_OBSERVATION and context[0] is not frame:
        context = None
    pinned = context[3] if context is not None else {}
    directories = context[4] if context is not None else {}
    roots = context[5] if context is not None else {}
    key = (_PLAN, logical_path)
    with _ASSET_LOCK:
        if key in pinned:
            return pinned[key]
        # Resolve and check a shared parent once per observation. Checking
        # every sibling's full ancestor chain is costly on Windows Path.
        # Resolution and containment decisions are fresh in every scope;
        # the pure path-object caches never replace filesystem checks.
        directory_key = (_PLAN, logical_parent)
        if directory_key not in directories:
            parent = _resolve_directory(logical_parent)
            if _PLAN not in roots:
                roots[_PLAN] = _resolve_directory(_PLAN)
            directories[directory_key] = (parent, parent.is_relative_to(roots[_PLAN]))
        parent, parent_inside = directories[directory_key]
        path = _child_path(str(parent), name) if type(parent) is _PATH_TYPE else parent / name
        root = roots[_PLAN]
        if name == "..":
            path = path.resolve()
        # An ordinary child of a checked parent stays inside it. Preserve the
        # full check for '..', outside parents (including the root itself),
        # and, below, any redirecting leaf file.
        if (not parent_inside or not ordinary_child) and not path.is_relative_to(root):
            raise ValueError("Template path must remain inside this plan")
        try:
            info = path.lstat()
            is_redirect = stat.S_ISLNK(info.st_mode) or bool(
                getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
            if is_redirect:
                path = path.resolve()
                if not path.is_relative_to(root):
                    raise ValueError("Template path must remain inside this plan")
                info = path.stat()
            version = _version_info(info)
        except OSError:
            # Keep the legacy reader's exception type/message for missing or
            # inaccessible files instead of replacing it with stat() errors.
            image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError(f"Template not readable: {relative_path}")
            template = _Template(image)
            pinned[key] = template
            return template
        try:
            template = _load_template(path, version)
            pinned[key] = template
            return template
        except ValueError as exc:
            if exc.args == (f"Template not readable: {path}",):
                raise ValueError(f"Template not readable: {relative_path}") from exc
            raise


def gray_asset(relative_path, *, frame=_CURRENT_OBSERVATION):
    return _asset(relative_path, frame=frame).gray()


def validate_asset(relative_path):
    _asset(relative_path)


@contextmanager
def observe_frame(image):
    """Pin immutable templates, directory topology, ROIs and scores for one observation.

    Each template is checked on its first use. Replacements, deletion and link
    redirection become visible in the next observation, preserving a consistent
    template snapshot while interpreting one frame. Nothing pins a later frame.
    """
    context = _OBSERVATION.get()
    if context is not None and context[0] is image:
        yield
        return
    token = _OBSERVATION.set((image, {}, {}, {}, {}, {}))
    try:
        yield
    finally:
        _OBSERVATION.reset(token)


def frame_observation(function):
    frame_parameter = next(iter(signature(function).parameters))

    @wraps(function)
    def observe(*args, **kwargs):
        image = args[0] if args else kwargs.get(frame_parameter)
        with observe_frame(image):
            return function(*args, **kwargs)
    return observe


def gray_region(image, region):
    x, y, w, h = map(int, region)
    context = _OBSERVATION.get()
    gray_rois = context[1] if context is not None and context[0] is image else {}
    key = (x, y, w, h)
    if key not in gray_rois:
        gray_rois[key] = cv2.cvtColor(image[y:y+h, x:x+w, :3], cv2.COLOR_BGR2GRAY)
    return gray_rois[key]


def match_asset(image, spec, region=None):
    region = region or spec.get("search_region") or [0, 0, 1280, 720]
    x, y, w, h = map(int, region)
    x, y = max(x, 0), max(y, 0)
    w, h = min(w, 1280-x), min(h, 720-y)
    asset = _asset(spec["image"], frame=image)
    # Preserve the operation order and size short-circuit of the room matcher.
    source = image[y:y+h, x:x+w, :3]
    target = asset.gray()
    context = _OBSERVATION.get()
    key = (x, y, w, h)
    gray_rois = context[1] if context is not None and context[0] is image else {}
    if key not in gray_rois:
        gray_rois[key] = cv2.cvtColor(source, cv2.COLOR_BGR2GRAY)
    source = gray_rois[key]
    th, tw = target.shape[:2]
    if h < th or w < tw or asset.std() < 1:
        return None
    key = (asset, x, y, w, h)
    matches = context[2] if context is not None and context[0] is image else {}
    if key not in matches:
        scores = cv2.matchTemplate(source, target, cv2.TM_CCOEFF_NORMED)
        scores[~np.isfinite(scores)] = -1
        _, score, _, location = cv2.minMaxLoc(scores)
        matches[key] = (score, location)
    score, location = matches[key]
    if not math.isfinite(score) or score < float(spec.get("threshold", .92)):
        return None
    return {"score": float(score), "rect": [x+location[0], y+location[1], tw, th],
            "center": [x+location[0]+tw//2, y+location[1]+th//2]}
