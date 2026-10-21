# Black Moon Local Goods Selection

## Scope

`tasks:black_moon_local_select_pc.yaml:black_moon_local_select_pc` starts at the
LOCAL shop, independently of the rest-menu preparation task. Already-enabled
batch mode is confirmed and left unchanged. It starts at the current list position,
scans downward using the bounded page loop described below, and selects requested
goods. It does not require an initial zero total or scroll upward before selecting. A clipped row above
the first complete visible row is outside this scan's starting range. There is no
GUI registration, freight integration, refresh, inventory write, or weekly ledger
write in this stage. The existing generic purchase backend is unchanged.

The input `purchase_table` maps the 140 canonical catalog IDs to booleans.
Unspecified known IDs default to false; unknown IDs are rejected. All encountered
independent stock cards of an enabled product are selected, including discount and pack
variants. No deduplication by product identity occurs.

`submit_purchase` defaults to false. A successful bounded downward scan with
`has_selected_goods=true` permits exactly one purchase-button click. Every
confirmed selected target sets this boolean and adds its catalog ID to
`matched_item_ids`, a unique set of product types. This set is evidence of which
types matched, not a card count, pack quantity or inventory delta. The scanner
does not maintain new/preexisting/card counts or per-card selection totals.
Already-selected targets are not clicked again. A later card of the same type
is still recognized and clicked if unselected: membership in `matched_item_ids`
never suppresses a click. Scrolling above the starting position is omitted.
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

The exit source must be a stable LOCAL batch shop, confirmed in two newly
captured frames along with a stable matched button point. After a verified
purchase it must still have zero total; a no-target visit exits directly without
an initial-total requirement. After each
click, wait for two target-page observations. Unknown or loading states allow no
input. Each of at most three supplemental clicks requires freshly confirmed
source state; a late destination match suppresses that click. Already at the
requested destination succeeds without a click. Failure does not fall back to a
different destination.

With `submit_purchase=false`, purchase settling and exit nodes are skipped, so
the selection-only workflow stays in the shop. A submitted task with a completed
scan and `has_selected_goods=false` skips payment and returns normally. An all-false wishlist is
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
`templates/commerce/shop/black_moon/calibrated/manifest.json`; the old bank is not rewritten.
Use `tools/build_black_moon_local_select_templates.py` with the documented normal
and selected client frames to regenerate the new bank.

Each newly captured page processes complete, recognizable target cards one by
one. Partial or unknown cards are not clicked, and missing or clipped companions
do not require a full two-column row before continuing. Already-selected target
cards permit purchase without another click. Cards that cannot be identified as
requested targets are skipped. Each confirmed target badge sets
`has_selected_goods=true` and adds its product type to `matched_item_ids`.
No selected-card registry is maintained. Repeated sightings or distinct cards
of the same type are handled from their current badge state; an unselected card
remains eligible for clicking.

Before clicking, fresh geometry, unselected state and the target identity are
rechecked locally. After clicking, the recognizer checks the yellow badge in the
upper-right corner at an offset relative to the recognized product/card position.
Fresh feedback confirms selection without rerunning full product identity
matching. If feedback is absent, at most three supplemental clicks are allowed,
each with a fresh source-state check.

### Recognition and scrolling loop

The scanner starts at the current list position. It processes the current page's
complete, recognizable targets before deciding whether to finish or drag again.
It captures a stable baseline frame after the page's clicks and selection
confirmations, immediately before dragging. Each drag moves the pointer upward
by 262 pixels over 0.45 seconds, holds it stationary for 0.5 seconds at the
endpoint (`hold_before_release_sec`), then releases. After release, the scanner waits
for the viewport to stabilize before comparing it with the baseline and
recognizing the new page. The held endpoint is intended to suppress inertia.

The comparison covers the entire product-list viewport image. It does not
compare only the target recognition results: a page without a matching target
can still be recognized as different. The baseline already includes confirmed
selection badges so the current page's own clicks do not count as scroll motion.
No fresh recognition uses coordinates captured before a drag.

The loop finishes when any one of these conditions is met:

- A stable SOLD OUT marker is recognized; the current page's eligible targets
  are processed before finishing (`sold_out_boundary`).
- The list viewport remains unchanged across each of two consecutive drags
  (`unchanged_view`). Any changed viewport resets the unchanged-drag counter to
  zero. Each comparison uses that drag's stable pre-drag baseline and stable
  post-release frame.
- More than ten drags have occurred: the page reached by drag 11 is processed,
  then the loop finishes (`drag_limit`).

A page without a target is not itself an end condition. Full-row pixel
registration, scroll-distance projection, previous-page clipped-pixel proof,
43-pixel bottom probing and mandatory full two-column rows are removed. The
scanner does not scroll back above its starting position.

The user-confirmed LOCAL sort order puts sold-out goods after available stock.
The independent grayscale SOLD OUT plate is observed in stable frames. Product
identity matching below that boundary is unnecessary. An entirely sold-out
screen may establish the boundary without ordinary card corners.

Results distinguish `scan_complete` and `end_reason` (`sold_out_boundary`,
`unchanged_view` or `drag_limit`) from physical `bottom_confirmed`.
`scan_complete` means this bounded page loop reached an allowed stopping
condition; it does not claim every stock card was visited. In particular,
unchanged viewport observations and the drag limit are not physical-bottom
evidence. No confirmed-top field is required. Submission and weekly-ledger
validation use this bounded-loop contract; older records remain readable under
their historical evidence rules.

If `has_selected_goods` is true, the task submits once using the existing
payment journal and settlement checks. If false, it skips payment and returns
normally without claiming a successful purchase. New weekly evidence checks
matched product ID sets and pending/confirmed payment state, rather than card
multiplicity. Historical quantity-bearing evidence remains readable under its
older rules. Payment journaling, confirmation and uncertain-payment handling
remain unchanged.

The new plate is cropped from the user-supplied 2026-10-14 screenshot; only the
anonymous SOLD OUT label enters the bank. The source first row is clipped: its
card top is 125 in client coordinates, not the viewport top 143. The plate origin
is at 719,178. `tools/build_black_moon_sold_out_template.py` regenerates the plate
and provenance; the main icon generator preserves this independent resource.
The 2026-10-18 live run below observed the sold-out marker using this bank.

## Validation Boundary

Historical unit tests cover wishlist validation, local feedback, duplicate cards,
bounded retry, cancellation, the former empty-start requirement and single submission.
Release regression checks cover the current-position/nonempty-start scan,
bounded page/drag loop, selected-target presence, payment/weekly integration and
the category-filter layout regression. Historical overlap/bottom-proof checks do
not validate the replacement loop. Cropped real list/footer fixtures cover target matches, selected/unselected state and paper
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
detectors have offline evidence. At that implementation stage no live exit had
been performed; the newer run below exercises the rest-menu exit.

On 2026-10-18, a user-authorized standalone purchase from the Cape City rest menu
enabled only `digital_thought`. Both observed target selection clicks succeeded
without supplements. Four upward drags ended at a recognized sold-out marker.
The task submitted once, confirmed stable zero-total settlement, persisted the
weekly completion, and returned to the rest menu. Total task time was 19.02
seconds. This is UI settlement evidence, not an independently measured inventory
delta. The temporary runner was closed after all task activity completed.
