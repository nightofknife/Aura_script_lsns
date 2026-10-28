"""Rebuild the entry icon from the supplied profile-menu screenshot; no input."""
from pathlib import Path
import hashlib
import json

import cv2
import numpy as np


def main():
    plan = Path(__file__).resolve().parents[1] / "plans/resonance_pc"
    source = plan / "development/template_sources/prison_product_snapshot"
    spec = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    original = source / spec["source"]
    if hashlib.sha256(original.read_bytes()).hexdigest() != spec["sha256"]:
        raise ValueError("Supplied screenshot checksum mismatch")
    image = cv2.imdecode(np.fromfile(original, np.uint8), cv2.IMREAD_COLOR)
    left, top, right, bottom = spec["client_crop_xyxy"]
    client = image[top:bottom, left:right]
    if client.shape != (720, 1280, 3):
        raise ValueError("Expected native 1280x720 client; never rescale")
    x, y, width, height = spec["template_crop_xywh"]
    target = plan / spec["template"]
    cv2.imencode(".png", client[y:y+height, x:x+width])[1].tofile(target)
    print(target.relative_to(plan))


if __name__ == "__main__":
    main()
