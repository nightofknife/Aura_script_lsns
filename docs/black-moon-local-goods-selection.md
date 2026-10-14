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
the selection-only workflow stays in the shop. A submitted task with a completed
zero-selection scan skips payment and returns normally. An all-false wishlist is
explicit no-work and does not authorize navigation. Results include `purchase_completion`,
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
a page is not an end condition. A lower-boundary clipped card prevents completion.

### Sold-out boundary

The user-confirmed LOCAL sort order puts sold-out goods after available stock.
An independent grayscale SOLD OUT plate is observed in two fresh stable frames.
It defines the last row that needs processing, not an immediate return condition:
both columns of all preceding rows and the boundary row are handled before any
successful early return. No product identity matching is performed below it.
An entirely sold-out screen can establish this boundary without ordinary card
corners. Empty card detection without a positive boundary remains an error.

Above-boundary missing geometry/unknown state or changed target feedback cannot
complete a scan. Every earlier page must account for each fully visible two-column
row before scrolling; a target whose fresh identity check fails is not silently
skipped. Counted targets are checked again for selected state before scrolling or
completing. A clipped upper card needs visible-pixel evidence from the corresponding
full card already processed on an overlapping page. One unambiguous full-row
registration establishes scroll displacement before that instance is credited;
an arbitrary matching fragment of a duplicate product is insufficient. Missing or
ambiguous overlap is a conservative failure. A clipped lower companion card
is brought into view before completion. If that evidence cannot be established,
the action fails conservatively rather than silently skipping available stock.
Short lists without a sold-out marker first prove the physical bottom using two
different drag spans before limiting the grid to its observed final row. Only a
trailing empty right slot of that dense last row is permitted; interior gaps,
missing left slots and unknown detected cards remain failures.
The same physical-bottom confirmation is required when the boundary row contains
only a positively recognized left sold-out card and an empty right slot. This
does not exempt any preceding row or skip the boundary's final feedback check.

Results distinguish `scan_complete` and `end_reason` (`list_bottom` or
`sold_out_boundary`) from physical `bottom_confirmed`. Boundary proof includes a
confirmed top, stable marker position/score, and `upper_region_processed=true`.
Submission and weekly-ledger validation share this contract. Old records with
only `bottom_confirmed=true` remain readable; explicit incomplete new results
cannot fall back to that old proof. Payment journaling and confirmation rules
are unchanged. Zero selected goods skip payment but allow normal return navigation
after a completed empty-start scan, without claiming a successful purchase.

The new plate is cropped from the user-supplied 2026-10-14 screenshot; only the
anonymous SOLD OUT label enters the bank. The source first row is clipped: its
card top is 125 in client coordinates, not the viewport top 143. The plate origin
is at 719,178. `tools/build_black_moon_sold_out_template.py` regenerates the plate
and provenance; the main icon generator preserves this independent resource.
This change has not been template-match calibrated or live-run tested.

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
