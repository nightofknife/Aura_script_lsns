"""Build masked, colour warehouse commodity crops from native transparent sprites.

Offline asset preparation only. Use the separate devtools source and UnityPy;
neither is required by the game automation runtime. Trade templates provide only
the reviewed product-to-native-sprite identity map, never rendered pixels.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import tempfile

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "plans/resonance_pc"
ICON_SIZE = (84, 84)
# Exclude the top-left type cube, bottom quantity and bottom white card stripe.
CROP = (8, 24, 68, 42)
BUNDLES = {
    "small": "item/goods/small.asset",
    "wulinyuan": "item/goods/wulinyuan.asset",
}


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def repository_path(path: Path, *, label: str) -> Path:
    """Resolve fixture and artifact references against the repository root."""
    resolved = (path if path.is_absolute() else ROOT / path).resolve()
    if not resolved.is_relative_to(ROOT.resolve()):
        raise ValueError(f"{label} must stay inside the repository: {path}")
    return resolved


def evidence_directory(path: Path) -> Path:
    resolved = repository_path(path, label="Evidence directory")
    temporary_root = (ROOT / ".pytest_tmp").resolve()
    if resolved == temporary_root or not resolved.is_relative_to(temporary_root):
        raise ValueError("Evidence directory must be a dedicated scope below repository .pytest_tmp")
    return resolved


def configure_workspace(evidence_dir: Path) -> None:
    """Apply repository-owned temporary paths to this builder process only."""
    if Path.cwd().resolve() != ROOT.resolve():
        raise ValueError(f"Run this builder from the repository root: {ROOT}")
    directory = evidence_directory(evidence_dir)
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("TEMP", "TMP", "TMPDIR"):
        os.environ[name] = str(directory)
    tempfile.tempdir = str(directory)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True


def warehouse_crop(icon: Image.Image) -> tuple[Image.Image, Image.Image]:
    """Resize native RGBA pixels, retaining only opaque foreground in the mask."""
    if icon.size not in {(180, 180), (180, 179)}:
        raise ValueError(f"Unexpected native commodity sprite dimensions: {icon.size}")
    rendered = icon.convert("RGBA").resize(ICON_SIZE, Image.Resampling.BILINEAR)
    x, y, w, h = CROP
    pixels = np.asarray(rendered.crop((x, y, x+w, y+h)))
    mask = np.where(pixels[:, :, 3] >= 250, 255, 0).astype(np.uint8)
    mask = cv2.erode(mask, np.ones((3, 3), np.uint8), iterations=1)
    if int(np.count_nonzero(mask)) < 100:
        raise ValueError("Native icon has too little opaque foreground inside the warehouse crop")
    return Image.fromarray(pixels[:, :, :3], "RGB"), Image.fromarray(mask, "L")


def build(game_data: Path, devtools_src: Path, plan: Path = PLAN,
          product_ids: list[str] | None = None) -> dict:
    sys.path.insert(0, str(devtools_src.resolve()))
    from aura_resonance_devtools.unity_bundle import _logical_sprite_image, load_bundle

    metadata = game_data / "il2cpp_data/Metadata/global-metadata.dat"
    products_file = plan / "data/meta/products.json"
    identities_file = plan / "data/meta/trade_product_templates.json"
    products = json.loads(products_file.read_text(encoding="utf-8"))
    identities = json.loads(identities_file.read_text(encoding="utf-8"))["products"]
    requested = set(product_ids) if product_ids is not None else set(products)
    unknown = requested - products.keys()
    if unknown:
        raise ValueError(f"Unknown configured product ids: {sorted(unknown)}")

    libraries = {}
    source_hashes = {
        "il2cpp_data/Metadata/global-metadata.dat": digest(metadata),
        "plan/products.json": digest(products_file),
        "plan/trade_product_templates.json": digest(identities_file),
    }
    # Read only the two known commodity bundles, not the complete game directory.
    for library, relative in BUNDLES.items():
        bundle = game_data / "Patch/Asset" / relative
        source_hashes[f"Patch/Asset/{relative}"] = digest(bundle)
        environment = load_bundle(bundle, metadata)
        sprites = {}
        for obj in environment.objects:
            if obj.type.name != "Sprite":
                continue
            sprite = obj.read()
            if sprite.m_Name in sprites:
                raise ValueError(f"Duplicate native sprite: {library}/{sprite.m_Name}")
            sprites[sprite.m_Name] = (_logical_sprite_image(sprite, obj.version), obj.path_id)
        libraries[library] = sprites

    destination = plan / "templates/workshop/warehouse/goods"
    destination.mkdir(parents=True, exist_ok=True)
    entries, assets = {}, {}
    for product_id in sorted(requested, key=int):
        name = products[product_id]
        identity = identities.get(product_id, {})
        entry = {"name": name, "supported": False,
                 "screenshot_calibrated": False}
        if identity.get("name") != name:
            entry["reason"] = "identity_catalog_name_mismatch"
        else:
            source = identity.get("source", "")
            entry["source"] = source
            parts = source.split("/")
            native = libraries.get(parts[-2].casefold(), {}).get(parts[-1]) if len(parts) >= 2 else None
            if native is None:
                entry["reason"] = "configured_native_sprite_not_found"
            else:
                icon, path_id = native
                try:
                    template, mask = warehouse_crop(icon)
                except ValueError as exc:
                    entry["reason"] = str(exc)
                    entries[product_id] = entry
                    continue
                image_path, mask_path = destination / f"{product_id}.png", destination / f"{product_id}.mask.png"
                template.save(image_path)
                mask.save(mask_path)
                entry.update(supported=True, image=image_path.relative_to(plan).as_posix(),
                             mask=mask_path.relative_to(plan).as_posix(),
                             native_size=list(icon.size), native_sprite_path_id=path_id)
                for path, image in ((image_path, template), (mask_path, mask)):
                    assets[path.relative_to(plan).as_posix()] = {
                        "sha256": digest(path), "size": list(image.size), "mode": image.mode,
                    }
        entries[product_id] = entry

    catalog = {
        "schema_version": 1,
        "status": "native_generated_pending_screenshot_calibration",
        "reference_client": [1280, 720],
        "icon_size": list(ICON_SIZE), "crop": list(CROP),
        "template_size": [CROP[2], CROP[3]],
        "rendering": {"source": "native_transparent_sprite", "resample": "bilinear",
                      "mask": "native_alpha_ge_250_eroded_3x3", "use_grayscale": False},
        "match": {"method": "TM_CCOEFF_NORMED", "threshold": None,
                  "threshold_status": "requires_screenshot_calibration", "preprocess": "none",
                  "use_grayscale": False, "require_mask": True},
        "source_hashes": source_hashes, "products": entries, "assets": assets,
    }
    output = plan / "data/meta/prison_warehouse_goods_templates_pc.json"
    output.write_text(json.dumps(catalog, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return catalog


def calibrate(catalog: dict, fixtures_file: Path, evidence_dir: Path, plan: Path) -> dict:
    """Compare explicitly annotated screenshot tiles; never infer expected identities."""
    fixtures_file = repository_path(fixtures_file, label="Calibration fixtures")
    evidence_dir = evidence_directory(evidence_dir)
    fixtures = json.loads(fixtures_file.read_text(encoding="utf-8"))
    evidence_dir.mkdir(parents=True, exist_ok=True)
    templates = {}
    for product_id, entry in catalog["products"].items():
        if entry["supported"]:
            templates[product_id] = (
                cv2.imdecode(np.fromfile(plan / entry["image"], np.uint8), cv2.IMREAD_COLOR),
                cv2.imdecode(np.fromfile(plan / entry["mask"], np.uint8), cv2.IMREAD_GRAYSCALE),
            )
    threshold, minimum_margin = .81, .10
    source_records, observations = [], []
    for source in fixtures["sources"]:
        path = repository_path(Path(source["image"]), label="Calibration screenshot")
        source_reference = path.relative_to(ROOT).as_posix()
        image = Image.open(path).convert("RGB")
        crop = source["client_crop_xyxy"]
        frame = np.asarray(image.crop(tuple(crop)))[:, :, ::-1].copy()
        if frame.shape != (720, 1280, 3):
            raise ValueError("Warehouse calibration requires an annotated 1280x720 client crop")
        source_records.append({"file": path.name, "image": source_reference,
                               "source_kind": "repository_file",
                               "durable": not path.is_relative_to((ROOT / ".pytest_tmp").resolve()),
                               "sha256": digest(path),
                               "client_crop_xyxy": crop, "role": source["role"]})
        for sample in source.get("samples", []):
            x, y = sample["origin"]
            tile = frame[y:y+84, x:x+84]
            if tile.shape != (84, 84, 3):
                raise ValueError("Calibration only accepts fully visible 84x84 commodity tiles")
            # Permit two pixels of raster alignment error around the native crop.
            search = tile[22:68, 6:78]
            ranks = []
            for product_id, (template, mask) in templates.items():
                response = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED, mask=mask)
                response = np.nan_to_num(response, nan=-1, posinf=-1, neginf=-1)
                _, score, _, location = cv2.minMaxLoc(response)
                ranks.append({"product_id": product_id, "score": float(score),
                              "crop_offset": [location[0]+6, location[1]+22]})
            ranks.sort(key=lambda item: item["score"], reverse=True)
            best, runner_up = ranks[:2]
            margin = best["score"]-runner_up["score"]
            accepted = best["score"] >= threshold and margin >= minimum_margin
            expected_id = sample.get("product_id")
            correct = (accepted and best["product_id"] == expected_id) if expected_id is not None else not accepted
            observations.append({"source": source_reference, **sample, "accepted": accepted,
                                 "correct": correct, "margin": margin, "ranks": ranks[:3]})
    positive = [item for item in observations if item.get("product_id") is not None]
    negative = [item for item in observations if item.get("product_id") is None]
    observed_ids = {item["product_id"] for item in positive}
    calibrated_ids = sorted({product_id for product_id in observed_ids
                             if all(item["correct"] for item in positive
                                    if item["product_id"] == product_id)}, key=int)
    for product_id in calibrated_ids:
        catalog["products"][product_id]["screenshot_calibrated"] = True
        catalog["products"][product_id]["calibrated_sides"] = sorted({
            item["side"] for item in positive if item["correct"] and item["product_id"] == product_id})
    report = {"scope": "supplied_screenshot_tile_calibration_only", "sources": source_records,
              "positive_samples": len(positive), "negative_samples": len(negative),
              "correct_samples": sum(item["correct"] for item in observations),
              "lowest_positive_score": min(item["ranks"][0]["score"] for item in positive if item["correct"]),
              "highest_competing_score": max(item["ranks"][1]["score"] for item in positive if item["correct"]),
              "lowest_positive_margin": min(item["margin"] for item in positive if item["correct"]),
              "observations": observations}
    report_file = evidence_dir / "calibration_scores.json"
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    catalog["status"] = "native_generated_screenshot_calibrated_subset"
    catalog["match"].update(threshold=threshold, min_margin=minimum_margin,
                            threshold_status="calibrated_on_supplied_screenshots_only",
                            search_crop_xywh=[6, 22, 72, 46])
    catalog["calibration"] = {key: value for key, value in report.items() if key != "observations"}
    catalog["calibration"].update(
        calibrated_product_ids=calibrated_ids,
        warehouse_side_product_ids=sorted({item["product_id"] for item in positive
                                          if item["correct"] and item["side"] in {"raw_warehouse", "product_warehouse"}}, key=int),
        report=report_file.relative_to(ROOT).as_posix(), report_sha256=digest(report_file),
        fixtures=fixtures_file.relative_to(ROOT).as_posix(), fixtures_sha256=digest(fixtures_file),
        live_game_verified=False,
        limitations=["Only product 113 is visible on warehouse sides in supplied screenshots.",
                     "Other calibrated products are train-side observations only.",
                     "Generated products absent from supplied screenshots remain uncalibrated.",
                     "Threshold and margin are offline calibration values, not a live acceptance result."],
    )
    (plan / "data/meta/prison_warehouse_goods_templates_pc.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data", required=True, type=Path)
    parser.add_argument("--devtools-src", type=Path, default=ROOT.parent / "Aura_script_lsns_devtools/src")
    parser.add_argument("--extra-python-dir", type=Path,
                        help="Optional isolated UnityPy dependency directory")
    parser.add_argument("--plan", type=Path, default=PLAN)
    parser.add_argument("--product-id", action="append", dest="product_ids")
    parser.add_argument("--calibration-fixtures", type=Path,
                        help="Optional explicit screenshot tile annotations; no game input")
    parser.add_argument("--evidence-dir", type=Path,
                        default=ROOT / ".pytest_tmp/prison_warehouse_calibration_20261023/goods")
    args = parser.parse_args()
    try:
        configure_workspace(args.evidence_dir)
        plan = repository_path(args.plan, label="Plan output")
        fixtures = repository_path(args.calibration_fixtures, label="Calibration fixtures") if args.calibration_fixtures else None
    except ValueError as exc:
        parser.error(str(exc))
    if args.extra_python_dir:
        sys.path.insert(0, str(args.extra_python_dir.resolve()))
    catalog = build(args.game_data.resolve(), args.devtools_src.resolve(), plan, args.product_ids)
    if fixtures:
        report = calibrate(catalog, fixtures, args.evidence_dir, plan)
        print(json.dumps({"positive_samples": report["positive_samples"],
                          "negative_samples": report["negative_samples"],
                          "correct_samples": report["correct_samples"],
                          "calibrated_products": len(catalog["calibration"]["calibrated_product_ids"])}, indent=2))
    print(json.dumps({"products": len(catalog["products"]), "assets": len(catalog["assets"]),
                      "unsupported": {key: value for key, value in catalog["products"].items()
                                      if not value["supported"]}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
