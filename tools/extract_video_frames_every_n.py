from __future__ import annotations

import argparse
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import cv2


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".wmv", ".flv", ".webm", ".m4v"}
_PRINT_LOCK = threading.Lock()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract every Nth frame from each video into per-video folders.")
    parser.add_argument("--input-dir", type=Path, default=Path(r"D:\video"))
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(r"D:\aura_yolo_training\resonance_frames\video_frames_every_10"),
    )
    parser.add_argument("--step", type=int, default=10)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--quality", type=int, default=95)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.step <= 0:
        raise ValueError("--step must be greater than 0.")

    input_dir = args.input_dir.resolve()
    output_root = args.output_root.resolve()
    videos = [
        path
        for path in sorted(input_dir.iterdir())
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    ]
    if not videos:
        raise FileNotFoundError(f"No supported video files found in {input_dir}")

    output_root.mkdir(parents=True, exist_ok=True)
    workers = args.workers if args.workers > 0 else min(4, len(videos), max(os.cpu_count() or 1, 1))
    started_at = time.time()
    log(
        json.dumps(
            {
                "input_dir": str(input_dir),
                "output_root": str(output_root),
                "videos": len(videos),
                "step": int(args.step),
                "workers": int(workers),
                "quality": int(args.quality),
            },
            ensure_ascii=False,
        )
    )

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(
                extract_video,
                video_path=video,
                output_root=output_root,
                step=int(args.step),
                quality=int(args.quality),
            )
            for video in videos
        ]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            log(
                json.dumps(
                    {
                        "done": result["video"],
                        "saved": result["saved_frames"],
                        "read": result["read_frames"],
                        "seconds": round(result["elapsed_sec"], 2),
                        "output_dir": result["output_dir"],
                    },
                    ensure_ascii=False,
                )
            )

    results.sort(key=lambda item: item["video"])
    summary = {
        "ok": True,
        "input_dir": str(input_dir),
        "output_root": str(output_root),
        "step": int(args.step),
        "workers": int(workers),
        "elapsed_sec": round(time.time() - started_at, 3),
        "video_count": len(results),
        "total_saved_frames": sum(int(item["saved_frames"]) for item in results),
        "total_read_frames": sum(int(item["read_frames"]) for item in results),
        "videos": results,
    }
    summary_path = output_root / "manifest.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(json.dumps({"manifest": str(summary_path), "total_saved_frames": summary["total_saved_frames"]}, ensure_ascii=False))
    return 0


def extract_video(*, video_path: Path, output_root: Path, step: int, quality: int) -> dict[str, Any]:
    started_at = time.time()
    output_dir = output_root / video_path.stem
    output_dir.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Failed to open video: {video_path}")

    expected_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    saved = 0
    read = 0

    try:
        while True:
            grabbed = cap.grab()
            if not grabbed:
                break
            if read % step == 0:
                ok, frame = cap.retrieve()
                if not ok or frame is None:
                    break
                out_path = output_dir / f"frame_{read:08d}.jpg"
                write_jpeg(out_path, frame, quality=quality)
                saved += 1
                if saved % 500 == 0:
                    log(
                        json.dumps(
                            {
                                "progress": video_path.name,
                                "saved": saved,
                                "read": read + 1,
                                "expected_total": expected_total,
                            },
                            ensure_ascii=False,
                        )
                    )
            read += 1
    finally:
        cap.release()

    return {
        "video": video_path.name,
        "video_path": str(video_path),
        "output_dir": str(output_dir),
        "fps": fps,
        "width": width,
        "height": height,
        "expected_frame_count": expected_total,
        "read_frames": read,
        "saved_frames": saved,
        "elapsed_sec": round(time.time() - started_at, 3),
    }


def write_jpeg(path: Path, frame: Any, *, quality: int) -> None:
    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimeError(f"Failed to encode frame: {path}")
    encoded.tofile(str(path))


def log(message: str) -> None:
    with _PRINT_LOCK:
        print(message, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
