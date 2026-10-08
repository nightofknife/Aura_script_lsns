"""Promote reviewed city badges and the visit-city icon to offline plan assets.

City masks describe edge neighborhoods, not opaque alpha interiors. Runtime
matching preprocesses the RGB black-composited template and screenshot to edges.
"""
from __future__ import annotations

import argparse
import ast
from hashlib import sha256
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"
CITY_ROI = [25, 490, 175, 170]
VISIT_ROI = [1000, 450, 250, 70]
CLIENT_CROP = [25, 56, 1305, 776]


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def city_names(plan: Path) -> dict[str, str]:
    """Read the established canonical key/name table without importing runtime."""
    source = plan / "src/services/city_shop_data_pc_service.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    return next(ast.literal_eval(node.value) for node in module.body
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                and node.target.id == "_CITY_KEY_DISPLAY_NAME")


def contour_template(rgba: Image.Image) -> tuple[Image.Image, Image.Image]:
    if rgba.mode != "RGBA" or rgba.size != (133, 133):
        raise ValueError("Expected a reviewed 133x133 RGBA city badge")
    black = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
    black.alpha_composite(rgba)
    template = black.convert("RGB")
    edge = cv2.Canny(cv2.cvtColor(np.asarray(template), cv2.COLOR_RGB2GRAY), 50, 150)
    mask = cv2.dilate(edge, np.ones((5, 5), np.uint8))
    if not np.count_nonzero(mask):
        raise ValueError("City badge has no usable contour")
    return template, Image.fromarray(mask)


def build(research: Path, plan: Path = PLAN) -> dict:
    folder = plan / "templates/city_identity"
    folder.mkdir(parents=True, exist_ok=True)
    entries, assets, sources = [], {}, {}
    for key, name in city_names(plan).items():
        source = research / f"{key}.png"
        with Image.open(source) as rgba:
            template, mask = contour_template(rgba)
        paths = {}
        for kind, image, suffix in (("template", template, ""), ("mask", mask, "_mask")):
            path = folder / f"{key}{suffix}.png"
            image.save(path)
            relative = path.relative_to(plan).as_posix()
            paths[kind] = relative
            assets[relative] = {"sha256": digest(path), "size": list(image.size), "mode": image.mode}
        entries.append({"city_key": key, "city_name": name, **paths})
        sources[source.name] = digest(source)
    visit_source = research / "visit-city-icon-template.png"
    with Image.open(visit_source) as image:
        if image.size != (35, 29):
            raise ValueError("Expected reviewed 35x29 visit-city icon crop")
        visit = image.convert("RGB")
    visit_target = plan / "templates/visit_city_entry.png"
    visit.save(visit_target)
    visit_relative = visit_target.relative_to(plan).as_posix()
    assets[visit_relative] = {"sha256": digest(visit_target), "size": list(visit.size), "mode": visit.mode}
    sources[visit_source.name] = digest(visit_source)
    catalog = {
        "schema_version": 1,
        "cities": entries,
        "city_match": {"method": "TM_CCORR_NORMED", "threshold": .55,
                       "roi": CITY_ROI, "use_grayscale": True, "preprocess": "edge"},
        "mask_generation": {"source": "black_composited_rgb_contour_neighborhood",
                            "canny": [50, 150], "dilate": [5, 5]},
        "visit_entry": {"template": visit_relative, "method": "TM_SQDIFF_NORMED",
                        "threshold": .9, "roi": VISIT_ROI,
                        "use_grayscale": False, "preprocess": "none"},
        "assets": assets, "source_hashes": sources,
    }
    target = plan / "data/meta/city_identity_templates.json"
    target.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return catalog


def screenshot_fixtures(screenshots: dict[str, Path], destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    entries = {}
    for label, source in screenshots.items():
        with Image.open(source) as image:
            if image.size != (1332, 802):
                raise ValueError(f"Unexpected attached screenshot dimensions: {image.size}")
            client = image.convert("RGB").crop(CLIENT_CROP)
        regions = {}
        for kind, roi in (("city", CITY_ROI), ("visit", VISIT_ROI)):
            x, y, width, height = roi
            path = destination / f"{label}_{kind}_roi.png"
            client.crop((x, y, x + width, y + height)).save(path)
            regions[kind] = {"path": path.name, "roi": roi, "sha256": digest(path)}
        entries[label] = {"attachment": source.name, "source_sha256": digest(source),
                          "client_crop": CLIENT_CROP, "regions": regions}
    provenance = {"kind": "actual_user_screenshot_rois_not_synthetic", "screenshots": entries}
    (destination / "sources.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research", type=Path, required=True)
    parser.add_argument("--plan", type=Path, default=PLAN)
    parser.add_argument("--main-screenshot", type=Path)
    parser.add_argument("--city-screenshot", type=Path)
    parser.add_argument("--bridge-screenshot", type=Path)
    parser.add_argument("--fixtures", type=Path, default=ROOT / "tests/fixtures/city_identity")
    args = parser.parse_args()
    catalog = build(args.research, args.plan)
    supplied = {label: getattr(args, f"{label}_screenshot") for label in ("main", "city", "bridge")}
    supplied = {label: path for label, path in supplied.items() if path is not None}
    if supplied:
        screenshot_fixtures(supplied, args.fixtures)
    print(json.dumps({"cities": len(catalog["cities"]), "assets": len(catalog["assets"]),
                      "screenshot_fixtures": list(supplied)}))


if __name__ == "__main__":
    main()
