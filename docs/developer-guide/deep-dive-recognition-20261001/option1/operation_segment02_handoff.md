# Paused operation registration diagnosis

User requested pause before any production fix or new regression fixture.
`_deep_dive_operation_frame.py` remains the completed detector-injection
version: optional `target_detector` on wide-reference construction,
operation-frame construction, and rotation-preview verification, with 48
narrow tests passed. There are no half-applied production mask edits.

## Captured failure

Session:
`.pytest_tmp/deep_dive_option1_20261001/planned_segment_02/logs/deep_dive_planned_run/b8124ef1434a44bf838594f7f61a49e4/session.json`

Image: `frames/122708_459422_blocked.png`. State is a real selected move
interface; the player remains slot4 and destination is slot1. The captured
ordinary wide reference is `pending_action.registration_frame`; atlas is
`state.layout`. This analysis issued no capture or game input.

## Evidence and cause

Offline `build_operation_frame` with the actual production model packet and
with legacy `detect_targets` both failed identically:

- One retained seed, initial top readings `[1,2,6,7]`.
- Four readings fail the unchanged five-anchor current-top requirement.
- No geometry fit, transition or Q registration reached.

Slot5's actual crop is independently classified `red_single_eye` at gains
1.65 and1.95, confidence0.813 and0.810. Operation `_colour_mask` still uses
the older red interval `h<=6 or h>=168`, saturation>140/value>140.
Current semantic warm evidence instead uses `h<=23 or h>=162`,
saturation>100/value>130, followed by independent eye-shape classification.
Thus a genuinely classified pink-tinted single-eye glyph has no eligible
centre pixels in operation registration. This is mask/classifier drift,
not an effect of model detector injection.

## Research-only proof

An in-memory `_colour_mask` override uses the current semantic warm colour
support for already independently classified single/triple-eye labels.
No production file was modified. On the same actual image and packet:

- Initial readings become `[1,2,5,6,7]`.
- Original iterative fit yields six current-top readings `[1,2,3,5,6,7]`.
- One Q, six matched atlas glyphs, zero conflicts, consensus1.0.
- Registration status becomes ready.
- Camera transition angle1.2420557 degrees; translation change
  `[-0.0123605,1.6637478,2.0839789]`, passing all existing bounds.
- The audited full call took0.326s on CPU versus original0.208s model /
  0.227s rule calls (diagnostic instrumentation; not a formal benchmark).

No anchors, consensus, HUD visibility, classifier threshold, geometry,
camera-transition or Q margin was lowered. The research increased its
execution budget to20s solely to exclude instrumentation timeout; actual
calls finished well inside the normal3s limit.

Artifacts:

- `operation_segment02_audit.py` and `operation_segment02_audit.json`
- `operation_segment02_crops.py`, `operation_segment02_crops.json`
- `operation_segment02_crops.png`, `operation_segment02_overlay.png`
- `operation_segment02_crop_0.png` through `_8.png`

## Next work after resume

Align operation warm centre extraction with the actual warm classifier's
pixel support after the classifier confirms the shape. Preserve all other
thresholds. Add a saved real-image regression that verifies both the original
failure and the corrected registration using the same actual atlas/reference;
add rejection coverage for warm seams, filled walls, Boss flare and masked
heads. Then rerun existing operation-registration tests and one authorized
real planned-action test. Do not claim general success from this one frame.
