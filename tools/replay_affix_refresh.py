"""Replay a saved affix-window screenshot with production vision and OCR.

This tool never creates a target runtime, captures a desktop, or sends input.
Run from the repository root; temporary outputs must remain inside the repo.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class ScreenshotApp:
    """A file-backed frame source with no access to the actual game."""
    def __init__(self, image):
        self.image = image
        self.capture_count = 0
        self.input_count = 0

    def capture(self):
        self.capture_count += 1
        return SimpleNamespace(success=True, image=self.image, quality_flags=[])

    def click(self, *_args, **_kwargs):
        self.input_count += 1
        raise AssertionError("Offline replay must never send input")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--client-box", default="1,31,1280,720", help="x,y,width,height; crop only, never resize")
    parser.add_argument("--output", type=Path, default=ROOT / ".pytest_tmp/affix_refresh_start")
    parser.add_argument("--expect-film", type=int, help="Optional expected film balance")
    candidate_expectation = parser.add_mutually_exclusive_group()
    candidate_expectation.add_argument("--expect-no-candidate", action="store_true")
    candidate_expectation.add_argument("--expect-candidate-id", type=int)
    parser.add_argument("--expect-candidate-value", help="Expected displayed numeric value; requires candidate ID")
    args = parser.parse_args()
    if args.expect_film is not None and args.expect_film < 0:
        parser.error("Expected film balance cannot be negative")
    if args.expect_candidate_value is not None and args.expect_candidate_id is None:
        parser.error("Expected candidate value requires --expect-candidate-id")
    output = args.output.resolve()
    if not output.is_relative_to(ROOT):
        parser.error("Replay outputs must stay inside the repository")
    output.mkdir(parents=True, exist_ok=True)
    for key in ("TEMP", "TMP", "TMPDIR"):
        os.environ[key] = str(output)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    sys.stdout.reconfigure(encoding="utf-8")

    import numpy as np
    from PIL import Image
    from plans.aura_base.src.services.ocr_service import OcrService
    from plans.aura_base.src.services.vision_service import VisionService
    from plans.resonance_pc.src.actions._affix_refresh_policy import load_catalog
    from plans.resonance_pc.src.actions._affix_refresh_vision import AffixRefreshVision
    from tools._shared import OverlayConfig

    source = Image.open(args.image).convert("RGB")
    box = list(map(int, args.client_box.split(",")))
    if len(box) != 4 or min(box) < 0:
        parser.error("Invalid client-box")
    x, y, width, height = box
    if [width, height] != [1280, 720] or x+width > source.width or y+height > source.height:
        parser.error("Client crop must be a complete 1280x720 region inside the source")
    image = source.crop((x, y, x+width, y+height))
    image.save(output / "client.png")
    rgb = np.array(image)
    plan = ROOT / "plans/resonance_pc"
    catalog = load_catalog(plan)

    async def run():
        loop = asyncio.get_running_loop()
        vision = VisionService()
        vision._loop = loop
        ocr = OcrService(OverlayConfig({"ocr": {"models_root": str(ROOT / "models/ocr"),
                    "execution_provider": "cpu", "session": {"intra_op_num_threads": 4, "inter_op_num_threads": 1}}}))
        ocr._loop = loop
        raw_ocr = []
        phase = "first_observation"

        class RecordingOcr:
            def recognize_all(self, source_image):
                started = time.perf_counter()
                response = ocr.recognize_all(source_image)
                raw_ocr.append({"phase": phase, "seconds": round(time.perf_counter()-started, 4), "rows": [
                    {"text": row.text, "confidence": row.confidence, "rect": row.rect}
                    for row in response.results]})
                return response

        app = ScreenshotApp(rgb)
        observer = AffixRefreshVision(app, vision, catalog, plan, ocr=RecordingOcr())

        def replay():
            nonlocal phase
            started = time.perf_counter()
            first = observer.observe()
            elapsed = time.perf_counter()-started
            phase = "repeat_observation"
            second = observer.observe()
            def result_fields(observation):
                candidate = observation.get("candidate")
                return {"film_remaining": observation.get("film_remaining"),
                        "candidate": None if candidate is None else
                            {"id": candidate.get("id"), "value": candidate.get("value")}}

            actual = result_fields(first)
            repeat_actual = result_fields(second)
            expected = {}
            if args.expect_film is not None:
                expected["film_remaining"] = args.expect_film
            if args.expect_no_candidate:
                expected["candidate"] = None
            elif args.expect_candidate_id is not None:
                expected["candidate"] = {"id": args.expect_candidate_id}
                if args.expect_candidate_value is not None:
                    expected["candidate"]["value"] = args.expect_candidate_value

            def matches_expected(result):
                for key, value in expected.items():
                    if isinstance(value, dict):
                        if not isinstance(result.get(key), dict) or any(
                                result[key].get(field) != item for field, item in value.items()):
                            return False
                    elif result.get(key) != value:
                        return False
                return True

            passed = (first.get("valid") is True and second.get("valid") is True
                      and actual == repeat_actual and matches_expected(actual)
                      and matches_expected(repeat_actual) and app.input_count == 0)
            report = {"source": str(args.image.resolve()), "source_sha256": hashlib.sha256(args.image.read_bytes()).hexdigest(),
                      "source_size": list(source.size), "client_box": box,
                      "client_size": [width, height], "whole_frame_passed": passed,
                      "expectations_supplied": bool(expected), "expected": expected,
                      "actual": actual, "repeat_actual": repeat_actual,
                      "observation": first, "repeat_observation": second,
                      "first_observation_seconds": round(elapsed, 4),
                      "raw_ocr_calls": raw_ocr, "file_frame_reads": app.capture_count,
                      "game_capture_count": 0, "input_count": app.input_count}
            (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["whole_frame_passed"] else 2

        return await asyncio.to_thread(replay)

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
