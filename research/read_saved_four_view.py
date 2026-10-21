"""Read one saved four-view frame with the current registered crop reader."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2
import numpy as np

from research.detector_config import add_detector_arguments, detector_config
from research.deep_dive_four_view_node_reader import read_four_view_node
from research.deep_dive_four_view_reading_check import load_reference_inputs
from plans.resonance_pc.src.actions._deep_dive_layout_vision import _crop


def read_saved_view(rgb, aligner, view):
    """Only registered actual pixels enter classification; labels are not used."""
    alignment = aligner.align(rgb, reference_view=view)
    panels = alignment.get("results", [])
    if not panels or any(not panel.get("proposal_accepted") for panel in panels):
        return {"status": "geometry_rejected", "cells": []}
    cells = []
    for panel in panels:
        for cell in panel["projected_reference_cells"]:
            quad = np.asarray(cell["quad"], dtype=np.float32)
            cells.append(dict(face=panel["reference_panel"], row=cell["row"], col=cell["col"],
                              quad=quad.tolist(), result=read_four_view_node(_crop(rgb, quad))))
    return {"status": "read", "cells": cells}


def main():
    from research.deep_dive_four_view_template_alignment import FourViewTemplateAligner
    from plans.resonance_pc.src.services.deep_dive_entity_detector_service import DeepDiveEntityDetectorService
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--view", required=True, type=int, choices=range(1, 5))
    parser.add_argument("--annotations", required=True, type=Path)
    parser.add_argument("--reference-root", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / ".pytest_tmp/saved_four_view/report.json")
    add_detector_arguments(parser)
    args = parser.parse_args()
    labels, references, _ = load_reference_inputs(args.annotations, args.reference_root)
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / ".pytest_tmp"):
        raise ValueError("output must stay under repository .pytest_tmp")
    image = args.image.resolve()
    bgr = cv2.imread(str(image), cv2.IMREAD_COLOR)
    if bgr is None or bgr.shape != (720, 1280, 3):
        raise ValueError(f"Saved frame must be readable 1280x720 RGB/BGR: {image}")
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    result = read_saved_view(rgb, FourViewTemplateAligner(labels, references), args.view)
    detector = DeepDiveEntityDetectorService(detector_config(args))
    try:
        result["entity_packet"] = detector.detect_packet(rgb)
    finally:
        detector.close()
    result.update(offline=True, formal_acceptance=False, targets_ready=False,
                  source_frame=str(image), view=args.view, closed_runtime=detector.status())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
