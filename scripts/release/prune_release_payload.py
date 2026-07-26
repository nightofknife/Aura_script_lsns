from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Callable


Classifier = Callable[[Path], str | None]


def classify_runtime_file(relative: Path) -> str | None:
    parts = tuple(part.lower() for part in relative.parts)
    if not parts:
        return None

    if parts[:3] in {
        ("_internal", "pyside6", "qml"),
        ("_internal", "pyside6", "translations"),
    }:
        return "unused_qt_data"

    if parts[:4] == ("_internal", "pyside6", "plugins", "platforminputcontexts"):
        return "unused_qt_virtual_keyboard"
    if parts == ("_internal", "pyside6", "plugins", "imageformats", "qpdf.dll"):
        return "unused_qt_pdf"

    if parts[:2] == ("_internal", "pyside6") and len(parts) == 3:
        qt_name = parts[-1]
        if qt_name.startswith(("qt6qml", "qt6quick", "qt6virtualkeyboard", "qt6pdf")):
            return "unused_qt_module"
        if qt_name.startswith(("qtqml", "qtquick", "qtvirtualkeyboard", "qtpdf")):
            return "unused_qt_binding"

    if parts[:2] == ("_internal", "numpy") and "tests" in parts[2:]:
        return "dependency_tests"
    if parts[:3] in {
        ("_internal", "onnxruntime", "backend"),
        ("_internal", "onnxruntime", "datasets"),
        ("_internal", "onnxruntime", "quantization"),
        ("_internal", "onnxruntime", "tools"),
        ("_internal", "onnxruntime", "transformers"),
        ("_internal", "av", "datasets"),
    }:
        return "unused_dependency_data"
    if (
        parts[:3] == ("_internal", "numpy", "_core")
        and "_tests." in parts[-1]
        and relative.suffix.lower() in {".pyd", ".lib"}
    ):
        return "dependency_tests"

    if parts[:2] == ("_internal", "pil") and parts[-1].startswith("_avif."):
        return "unused_avif_codec"
    if (
        parts[0] == "_internal"
        and parts[-1].startswith("opencv_videoio_ffmpeg")
        and parts[-1].endswith(".dll")
    ):
        return "unused_opencv_video_codec"
    if parts[0] == "_internal" and (
        relative.suffix.lower()
        in {".a", ".exp", ".h", ".hpp", ".lib", ".pdb", ".pxd", ".pxi", ".pyi", ".pyx"}
        or parts[-1] == "py.typed"
    ):
        return "development_metadata"
    return None


def classify_nvidia_file(relative: Path) -> str | None:
    parts = tuple(part.lower() for part in relative.parts)
    if "include" in parts[:-1]:
        return "development_headers"
    if relative.suffix.lower() in {".h", ".hpp", ".lib", ".a", ".exp", ".pdb"}:
        return "development_file"
    return None


def _matching_files(root: Path, classifier: Classifier) -> list[tuple[Path, str]]:
    return [
        (path, reason)
        for path in root.rglob("*")
        if path.is_file()
        for reason in (classifier(path.relative_to(root)),)
        if reason is not None
    ]


def _remove_empty_directories(root: Path) -> None:
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            continue


def prune_files(root: Path, classifier: Classifier, *, check_only: bool = False) -> dict:
    resolved = root.resolve()
    if not resolved.is_dir():
        raise FileNotFoundError(f"Payload root not found: {resolved}")

    matches = _matching_files(resolved, classifier)
    totals = Counter()
    total_bytes = 0
    for path, reason in matches:
        size = path.stat().st_size
        totals[reason] += 1
        total_bytes += size
        if not check_only:
            path.unlink()

    if check_only and matches:
        sample = ", ".join(str(path.relative_to(resolved)) for path, _ in matches[:5])
        raise ValueError(
            f"Payload contains {len(matches)} excluded files under {resolved}. Sample: {sample}"
        )
    if not check_only:
        _remove_empty_directories(resolved)

    return {
        "root": str(resolved),
        "removed_files": len(matches),
        "removed_bytes": total_bytes,
        "reasons": dict(sorted(totals.items())),
    }


def reset_release_runtime_data(release_root: Path, *, check_only: bool = False) -> dict:
    resolved = release_root.resolve()
    if not resolved.is_dir():
        raise FileNotFoundError(f"Release root not found: {resolved}")

    logs = resolved / "logs"
    logs.mkdir(exist_ok=True)
    files = [path for path in logs.rglob("*") if path.is_file()]
    total_bytes = sum(path.stat().st_size for path in files)
    if check_only and files:
        sample = ", ".join(str(path.relative_to(resolved)) for path in files[:5])
        raise ValueError(
            f"Release contains {len(files)} generated runtime files. Sample: {sample}"
        )
    if not check_only:
        for path in files:
            path.unlink()
        _remove_empty_directories(logs)

    return {
        "root": str(resolved),
        "removed_files": len(files),
        "removed_bytes": total_bytes,
        "reasons": {"generated_runtime_data": len(files)} if files else {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Prune non-runtime files from Aura release payloads.")
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--nvidia-dir", type=Path)
    parser.add_argument("--release-root", type=Path)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if not any((args.runtime_dir, args.nvidia_dir, args.release_root)):
        parser.error("at least one payload root must be provided")

    reports = []
    if args.runtime_dir:
        reports.append(
            prune_files(args.runtime_dir, classify_runtime_file, check_only=args.check_only)
        )
    if args.nvidia_dir:
        reports.append(
            prune_files(args.nvidia_dir, classify_nvidia_file, check_only=args.check_only)
        )
    if args.release_root:
        reports.append(
            reset_release_runtime_data(args.release_root, check_only=args.check_only)
        )
    print(json.dumps({"reports": reports}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
