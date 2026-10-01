# Independent glyph-localization pilot

This is research evidence, not a production replacement. All files and caches remain in the repository. No live game input was performed by this experiment.

Training uses 32 complete frames from c4 and 244 manually reviewed source-pixel boxes: blue 27, green 48, purple 34, yellow 51, red 39, orange 45. Seven output labels are retained, but **white has no real training/validation examples**. Initial atlas/color/shape labels only propose boxes; all source crops and box outlines were visually reviewed. Two clipped orange proposals were quarantined. Pixel component localization searches an expanded region; an inaccurate projected tile is never directly assigned a label. Projected regions without accepted annotations and target neighborhoods are ignored and blacked out during training. Accepted box pixels are restored. This masking introduces a training-domain limitation.

The complete live02 trajectory and a different-board video are never trained on. Live02 contains 64 accepted independent source boxes across 10 original full ROIs; the video contains 63 accepted boxes across 10 full ROIs. Video proposals use full-image pixel components without pose or atlas. Every video proposal was reviewed; 50 wall-glow, seam, HUD, effect or clipped proposals were rejected. Test images stay unmasked. Annotations are non-exhaustive, so subset recall and localization can be measured, not exhaustive recall or precision.

One YOLO11n 640px/batch8 run used 80 fixed epochs on RTX3060 in 119.6 seconds. Use `runs/glyphs_v1_fixed80/weights/last.pt`, not the validation-selected best checkpoint. No heldout-based retraining, tuning or model selection occurred.

| Heldout | Threshold | Correct / annotated | Wrong class | Missed | Center median / P95 |
|---|---:|---:|---:|---:|---:|
| live02 | .25 | 63/64 | 1 | 0 | 1.63 / 3.74 px |
| live02 | .5 | 61/64 | 0 | 3 | 1.63 / 3.71 px |
| live02 | .7 | 58/64 | 0 | 6 | 1.63 / 3.62 px |
| video | .25 | 63/63 | 0 | 0 | 1.90 / 6.04 px |
| video | .5 | 60/63 | 0 | 3 | 1.77 / 4.51 px |
| video | .7 | 47/63 | 0 | 16 | 1.64 / 5.60 px |

IoU≥.25 assigns predictions to manually accepted boxes; class correctness is scored independently. Center errors compare predicted box midpoints with accepted source-pixel box midpoints, never a projected grid or earlier frame. Centers and classes are useful pose proposals; all existing geometric gates remain necessary. These small datasets cannot establish 99% real-world accuracy.

At .5, all 95 unmatched predictions were additionally visually reviewed: 87 show a true single glyph of the predicted class, 4 incorrectly call an orange three-eye glyph red, 1 merges two adjacent green glyphs, and 3 partial/HUD/glow cases remain quarantined. Annotation incompleteness explains many of the automated validator's false positives; the remaining true classification and localization failures preclude a stability claim.

Recorded pose probes at .5: frame525 matches F6+D3; RANSAC retains F6+D2 and passes original fit gates (median2.10/max4.20px, angle2.37°). Frame630 only D3+L1 and cannot renew an anchor. Frame664 has D3+L6 but its 14.5px translation exceeds the unchanged12px limit. The parent's classical localized-component solution successfully recovers630 and is currently stronger for that stress case.

Warm PyTorch GPU complete ROI preprocessing/inference/postprocessing: median16.44ms, P95 20.60ms. Capture, pose and grid geometry are excluded. Scripts live beside this directory under `model/`: `prepare_glyph_detector.py`, `finalize_glyph_dataset.py`, `prepare_video_glyphs.py`, `finalize_video_glyphs.py`, `train_glyph_detector.py`, `evaluate_glyph_detector.py`, `audit_glyph_extra_predictions.py`, and `finalize_glyph_extra_audit.py`.
