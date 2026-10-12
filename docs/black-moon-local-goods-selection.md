# Black Moon Local Goods Selection

## Scope

`tasks:black_moon_local_select_pc.yaml:black_moon_local_select_pc` starts at the
LOCAL shop, independently of the rest-menu preparation task. Already-enabled
batch mode is confirmed and left unchanged. It resets the retained list position
to the top once, scans to the bottom, and selects requested goods. There is no
GUI registration, freight integration, refresh, inventory write, or weekly ledger
write in this stage. The existing generic purchase backend is unchanged.

The input `purchase_table` maps the 140 canonical catalog IDs to booleans.
Unspecified known IDs default to false; unknown IDs are rejected. All independent
stock cards of an enabled product are selected, including discount and pack
variants. No deduplication by product identity occurs.

`submit_purchase` defaults to false. When true, the initial total must match zero
before scanning, avoiding submission of unrelated preselected goods. A successful
complete scan with newly selected goods permits exactly one purchase-button click.
The returned `purchase_submitted` is not automatic proof of purchase: handling
network loading is followed by an explicit read-only settling step. It requires
two consecutive stable LOCAL batch frames with zero total within 30 seconds.
`purchase_verified` at this stage means this UI settling evidence, not an inventory
or server receipt audit. Failure never retries purchase, clears selection, exits
from an unknown page, or closes the game.

## Exit Destinations

`exit_target` accepts `rest_menu` (default) and `main`. With `submit_purchase=true`,
the task chains `submit -> wait_purchase -> exit_shop`. Return uses the existing
back-button template; main uses the existing train-button template. Both click
buttons use 0.90. Rest completion requires the shop-entry menu template and no
shop-page marker. Main completion reuses the departure-button RGB SQDIFF template
at 0.90. Contradictory page markers are unknown.

The exit source must be a stable LOCAL batch shop with zero total, confirmed in
two newly captured frames along with a stable matched button point. After each
click, wait for two target-page observations. Unknown or loading states allow no
input. Each of at most three supplemental clicks requires freshly confirmed
source state; a late destination match suppresses that click. Already at the
requested destination succeeds without a click. Failure does not fall back to a
different destination.

With `submit_purchase=false`, purchase settling and exit nodes are skipped, so
the selection-only workflow stays in the shop. A submitted task with zero newly
selected cards also skips payment and exit. Results include `purchase_completion`,
`exit`, `page_state`, and aggregate `success`; failed exit cannot report aggregate
business success merely because selection succeeded. There is still no GUI or
freight integration change.

## Recognition

Only enabled normal-state product templates are loaded for routine recognition.
Up to 32 candidates use direct full-resolution SQDIFF matching. Larger sets use
quarter-size candidate ranking followed by full-resolution top-eight evaluation.
Small matches never authorize clicks. Disabled lookalike templates are evaluated
only after a positive target hit, and can reject but never become purchase targets.
The winning target must score strictly above its cataloged lookalikes; ties are
unknown. Paper templates use 0.92; other products retain 0.86. No threshold was
lowered to accommodate calibration.

Native-derived icons are calibrated from 120x119 to the observed 96x96 artwork.
One shared mask excludes discount/type overlays and pack quantities while
retaining lower-left quality background. Cropped screenshot controls provide
generic card corners, yellow selection feedback, the purchase button and the
exact zero-total state. Provenance and the native bank digest are in the new
`templates/black_moon_local_select/manifest.json`; the old bank is not rewritten.
Use `tools/build_black_moon_local_select_templates.py` with the documented normal
and selected client frames to regenerate the new bank.

Each newly captured page locates card geometry and checks selection state first.
Selected cards skip identity matching. Partial or unknown cards are not clicked.
Before clicking, fresh geometry, unselected state and only that target's identity
are rechecked locally. After clicking, two observations of the yellow badge
confirm selection without rerunning product identity matching. An initial click
allows at most three supplements, each requiring a new positive source check.

Scrolling waits for the viewport to stabilize; overlapping selected cards are
recognized from the new image rather than stale coordinates. Boundary checks use
a non-row-sized supplemental drag, with bounded top/down drag loops. No target on
a page is not an end condition. A lower-boundary clipped card prevents completion;
an upper-boundary clipped card does not.

## Validation Boundary

Unit tests cover wishlist validation, local feedback, duplicate cards, bounded
retry, cancellation, empty initial selection and single submission. Cropped real
list/footer fixtures cover target matches, selected/unselected state and paper
lookalike rejection. Native-template synthetic coarse recall covers all 140 IDs
and is not evidence that every product matches the real client.

On 2026-10-11, a user-authorized live run from LOCAL batch shop selected two stock
cards of inferior birch stone and two of purchase-order books, all with one click
and no supplements. Selection took 18.16 seconds and included seven list drags.
Batch mode was already on and its control was not clicked. One purchase submission
was made. A later read-only screenshot confirmed all four stock cards sold out:
seven stones and four books, with currency dropping from 73,038,307 to 72,939,422,
matching the 98,885 total. No further game input or exit followed.

The subsequent exit implementation is verified with offline fake-app transitions
and actual retained navigation/header/footer control crops. Both destination
detectors have offline evidence; exit clicking has not been tested in the live
client. No new live purchase or input was performed for this exit implementation.
