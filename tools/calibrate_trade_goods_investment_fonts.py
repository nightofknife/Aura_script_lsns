"""Inspect rasterization variants of native fonts against supplied screenshots."""

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from _trade_investment_native import configure, load_bundle
from build_trade_goods_investment_templates import bright_mask, crop, render_text, tight_token
from review_trade_goods_investment_templates import match


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-data", type=Path, required=True)
    parser.add_argument("--pythonlibs", type=Path, required=True)
    parser.add_argument("--devtools-src", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    configure(args.pythonlibs, args.devtools_src)
    env = load_bundle(args.game_data / "Patch/Asset/ui/font.asset", args.game_data / "il2cpp_data/Metadata/global-metadata.dat")
    font = next(bytes(obj.read().m_FontData) for obj in env.objects if obj.path_id == 5303591007113889663)
    root = Path(__file__).resolve().parents[1]
    refs = [("initial", "current", 0), ("initial", "preview", 1), ("success", "preview", 2),
            ("double_digit", "current", 10), ("double_digit", "preview", 11)]
    regions = {"current": [899, 217, 81, 48], "preview": [1030, 217, 80, 48]}
    observations = [tight_token(bright_mask(crop(np.array(Image.open(root / "tests/fixtures/trade_goods_investment" / f"{sid}.png")), regions[side])))[0]
                    for sid, side, _ in refs]
    rows = []
    for native_size in (43, 44, 45):
        for variant in ("native", "client"):
            for cutoff in (100, 140, 160, 180, 200, 220, 240):
                bank = [tight_token(np.where(np.asarray(render_text(f"Lv.{lv}", font, native_size, variant)) >= cutoff, 255, 0).astype(np.uint8))[0]
                        for lv in range(21)]
                ranked = [sorted([(match(np.pad(obs, 2), token)["score"], lv) for lv, token in enumerate(bank)], reverse=True)
                          for obs in observations]
                scores = [next(score for score, lv in rank if lv == ref[2]) for rank, ref in zip(ranked, refs)]
                margins = [rank[0][0] - rank[1][0] for rank in ranked]
                rows.append({"size": native_size, "variant": variant, "cutoff": cutoff,
                             "correct": sum(rank[0][1] == ref[2] for rank, ref in zip(ranked, refs)),
                             "min_score": min(scores), "mean_score": float(np.mean(scores)),
                             "min_margin": min(margins), "scores": scores, "margins": margins})
    rows.sort(key=lambda row: (row["correct"], row["min_margin"], row["min_score"]), reverse=True)
    (args.output / "font_calibration.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(json.dumps(rows[:8]))


if __name__ == "__main__":
    main()
