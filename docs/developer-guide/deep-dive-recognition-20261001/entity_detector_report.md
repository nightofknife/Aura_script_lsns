# Expanded three-entity experiment

No production changes or game inputs. All data and models remain local to this repository.

Manual visual audit reviewed 54 raw PNG candidates from 328 images in Downloads/test and Downloads/test (1). Thirty-five were retained for new training, with 21 player heads, 18 Boss and 81 inspiration boxes; three shortlisted images with ambiguous tiny/hidden border sprites were quarantined, and 16 candidates were outside this bounded audit. Connected-component and legacy boxes were proposals only: every accepted ID was visually checked in raw_review/grid_00..18, missing true sprites were added manually, and final overlays were checked again. Hex glyphs, blue flare effects, yellow rotation UI, seams and HUD remain unlabelled background only after source-image review. The 35 retained images have no decoded-pixel duplicate with d458/video/live02; nearest 64x64 image MAE is 14.19. This is not a claim that correlated captures are independent trials.

Combined training: 61 images, 28 heads, 37 Boss, 107 inspiration. Existing pilot labels and training split were preserved; the new dataset is dataset_extended.yaml. Entire video and live02 remain never trained. d458 is model-selection validation and has no Boss positive, so its mAP cannot establish all-three-class performance.

YOLO11n official initial weights, freeze=0, 120 epochs, 640 input, batch8, workers0, AdamW, rotation/flip/brightness augmentations, GPU RTX3060. Completed in about 258 seconds including startup/final validation (epoch body 231 seconds). Training allocated approximately 2.36GB GPU memory. Best and last produce the same evaluated classifications; both are exported for reproducibility.

## Fixed confidence / same-class IoU >= .5

| Never-trained source | Confidence | Player TP/FP/FN | Boss TP/FP/FN | Inspiration TP/FP/FN | Correct / extra / missing |
|---|---:|---:|---:|---:|---:|
| Video: 19 frames, 41 objects | .25 | 5/0/0 | 15/0/0 | 21/5/0 | 41 / 5 / 0 |
| Video | .60 | 5/0/0 | 15/0/0 | 20/3/1 | 40 / 3 / 1 |
| live02: 12 frames, 23 objects | .25 | 6/0/3 | 3/0/0 | 11/1/0 | 20 / 1 / 3 |
| live02 | .60 | 5/0/4 | 3/0/0 | 10/1/1 | 18 / 1 / 5 |

The .25 recall across these two sources is 61/64 (95.3%), with six extra or wrongly classified objects. Inspiration precision is 32/38 (84.2%). This improves substantially over the first pilot but does not meet 99%, and no cube-cell correctness claim is made. At .85, recall collapses while high-confidence false positives remain. The 18/18 Boss result is a small correlated sample, not proof of 99% recall.

Failures to preserve for future fixes: video frame2826 and 3109/3392/3674/3957 contain inspiration false positives over yellow hex/node glyphs; frame3674 false inspiration confidence is .854. live02 frame384 classifies the pink player head as inspiration at .946, with a nearby real edge-on inspiration. Missing heads in live02 .25 are 176,353,384; overlap/occlusion and classification uncertainty matter. Raising confidence alone cannot repair these errors.

Models: runs/extended_gpu640/weights/{best,last}.pt and {best,last}.onnx. ONNX is static FP32 with [1,3,640,640] input, [1,7,8400] output, no built-in NMS. Class order: 0 player_head, 1 singularity, 2 inspiration. RGB ROI [250,40,1000,650] is aspect-preserving letterboxed to640 and divided by255. extended_onnx_export.json records this contract. Detailed per-frame errors: independent_extended_best_metrics.json and independent_extended_last_metrics.json.

Next: jointly distinguish entity and node glyph shapes, enforce cross-view identity/occupancy consistency, and validate true cube-cell association after geometric fitting. Keep these whole-source holdouts untouched by training; new live03 is another untouched source for future checks. Do not replace remaining ambiguity with top-score truncation.
