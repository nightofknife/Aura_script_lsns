# Black Moon Local Shopping Preparation

## Scope

`tasks:black_moon_local_prepare_pc.yaml:black_moon_local_prepare_pc` is a
standalone first-phase task. The user guarantees the initial rest menu.
It does not detect that initial page, read a city or catalogue, select goods,
scroll, refresh, purchase, or write the weekly ledger. It stops in the local
shop with batch mode enabled. Freight execution is unchanged.

## Task Chain

1. Require the 1280x720 client.
2. `enter_shop`: locate the shop-entry template directly and click it; confirm
   the shop emblem in two stable observations.
3. `select_local`: compare selected/unselected native local-tab templates.
   Click only a confirmed unselected state; a confirmed selected state is skipped.
4. `enable_batch`: compare on/off templates on the confirmed local page.
   Click only a confirmed off state; an on state is skipped.
5. `verify_ready`: confirm the local tab and batch switch together in each
   of two stable observations before returning success.

All transition nodes call `resonance_pc.prepare_black_moon_local_shop_step`.
Only `plans/aura_base/app` is injected; there is no OCR, ledger or inventory
service dependency. The reader reuses the existing matching engine while
loading only the six required controls, not the item/card PNG banks.

The subsequent independent LOCAL-shop phase is documented in
[Black Moon Local Goods Selection](black-moon-local-goods-selection.md).

## Bounded Feedback

Each click is followed by a 0.35-second settling delay and a seven-second
target observation window, polling every 0.2 seconds. A click may be
supplemented at most three times. Before every supplemental click, new
screenshots must still establish the source state and a newly matched point.
If the target has appeared, no further click is allowed. Unknown is never
treated as off/unselected. Source timeout, capture failure, contradictory
state, invalid points or exhausted retries fail the task; cancellation
propagates without success or further input. Failure does not close the game
or navigate back automatically.

The result contains `success`, `page_state: local_shop`, `local_selected: true`,
`batch_enabled: true`, `ready: true`, and the three transition results with
their click, supplemental-click and skipped counts. No purchase is implied.

## Native Controls

The six controls share `templates/commerce/shop/black_moon/native/controls/`, indexed by the native bank manifest one directory above.
Native construction is reproducible through
`tools/build_black_moon_local_shop_templates.py --prepare-only`; full-bank
construction uses the same recipes. Crops search within an eight-pixel local
tolerance with `1-TM_SQDIFF_NORMED`, threshold 0.90. Local/batch states retain
the existing 0.035 comparison margin and masked-color check.

The entry uses an opaque icon/text/background crop: a foreground-only mask
was unsuitable for the dark glyphs on the translucent menu button. The page
marker uses the native emblem instead of re-rendered title text. Local-tab
crops retain the pin and native selected/unselected backgrounds while
excluding dynamic background edges and localized labels. Switch templates
use native fonts and switch sprites with their original alpha masks.

## Evidence Boundary

Preparation and exit share the PC-local bounded transition executor. Their
timing, recognition evidence and cancellation errors remain scene-owned; see
[template click transitions](developer-guide/resonance-pc-template-click-transitions.md).

Four user-provided screenshots are stored as verified control-ROI fixtures
under `tests/fixtures/black_moon_prepare`; unrelated account, currency and NPC
pixels are excluded. Reconstructed surroundings are black, not game pixels.
The tested controls scored 0.971 (entry), 0.948 (emblem), 0.912/0.966 (local
off/on), and 0.990/0.997 (batch off/on). All 24 positive/negative comparisons
passed on those samples. These scores are not an accuracy percentage or
evidence for every city, modal overlay, animation or live transition.

Offline fake-app tests cover sequence, skipped toggles, delayed feedback,
fresh retry positions, unstable sources, four-click bounds, cancellation,
bad captures/points and final same-frame state confirmation. No live clicks,
shopping, freight execution, packaging or release are authorized by this phase.
