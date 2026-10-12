# Black Moon Local Purchase Integration

## Scope And Inputs

PC freight checks local shopping at its start city after reposition and at each
confirmed route arrival, including the endpoint. Existing arrival extras remain
first; shopping and the already-planned sparkling-water cups share one rest visit.
Shopping does not change the planner, passenger trips, fatigue estimates, book
budgets, or cash budgets. Headquarters shopping and manual refresh are excluded.

Execution inputs, shared across all four freight modes and combined commerce:

```yaml
auto_black_moon_local_purchase: false
black_moon_purchase_table:
  purchase_order_book: true
  item_11400084: true
black_moon_purchase_record_scope: default
```

The saved GUI map contains all 140 canonical IDs from
`plans/resonance_pc/data/meta/black_moon_local_shop_catalog.json`; missing known
keys default to false and unknown keys are rejected. The former
`black_moon_local_purchase_items` field is removed, with no list-format fallback.
Task boolean properties use the framework's normal boolean input normalization;
direct action inputs and GUI configuration require genuine boolean values.

`tools/black_moon_purchase_schema.py` generates the repeated input metadata from
the catalogue. Its contract tests prevent child/freight/combined schema drift.
These three settings are execution-only and excluded from route preview.

## Standalone Child And Reuse

`tasks:black_moon_local_purchase_pc.yaml:black_moon_local_purchase_pc` starts at
the guaranteed rest menu, with `city_name`, `purchase_table`, `record_scope`, and
`exit_target` (`rest_menu` default, or `main`). Freight always requests `rest_menu`.
It is independently runnable without adding another GUI small-task entry.

The YAML owns the ordered stages:

1. Validate/freeze the purchase table and obtain the city/week visit.
2. Require client resolution, enter the shop, select LOCAL, and enable batch.
3. Reuse wishlist selection: reset the retained scroll once, scan overlapping
   pages, select only desired identities, verify each yellow selected state.
4. Submit once, journaling pending intent synchronously immediately before the
   validated confirmation click, not before UI validation.
5. Wait read-only for stable LOCAL shop plus zero total; persist evidence and
   complete the wishlist check before navigation.
6. Restore the requested endpoint with fresh state/button checks and at most
   three supplemental exit clicks.

Preparation, selection, settlement and exits reuse the same actions as the
independent preparation/selection tasks. The obsolete monolithic scanner is not
called by the freight child. See the preparation and goods-selection documents
for native template generation and screenshot matching details.

The final `finish_visit` node returns `success`, `status`, `reason`, `city_key`,
`week_key`, `completed_this_week`, `selected_card_count`, `selected_items`,
`purchase_verified`, and the confirmed `page_state`. Card counts are not pack
quantities, book counts, or a verified inventory delta.

An empty wishlist skips shop entry. No matching goods is a completed wishlist
check with zero selected cards, not a fabricated purchase. It still exits the
shop so freight can drink water and continue. A skipped standalone visit requesting
`main` uses a verified rest-menu-to-main transition.

## Durable Evidence And Failure Policy

Continue using `core/persistent_data`'s existing
`black-moon-shop-purchases.json`. Account scope, weekly reset and canonical city
remain the keys. No new user record file is introduced. Default account scope is
`default`; callers must select another scope when switching accounts.

Same completed wishlist skips before rest navigation. A changed true-ID set starts
a new visit and archives prior evidence. Unresolved pending intent in that city/
week, including historical visits, blocks another purchase. It is never replayed
automatically. Corrupt/unreadable records stop freight before shop input.

Two explicit new evidence kinds avoid weakening previous full-stock proof:

- `wishlist_purchase_settled`: recognized UI settlement after the single submit;
  it does not assert actual units received or independently read the server.
- `wishlist_scan`: complete initially-empty list traversal, frozen desired IDs,
  selected card count and per-card selected IDs. Current-visit confirmed batches
  must account for their exact multiplicity. Unrecognized card identities are not
  fabricated as missing/sold-out products.

Payment timeout/cancellation keeps unresolved intent and does not exit or click
again. Successful evidence is persisted before exit, so an exit failure cannot
erase the check or cause another submit on the next run. If confirmation persisted
but completion writing failed, a later visit archives that confirmed attempt and
checks remaining stock; the previous confirmed batch is never replayed.

Task exclusivity owns the UI workflow; cooperative file locks serialize ledger
read/modify/write. No async session lock is left held across YAML nodes. The old
record validator remains strict for existing stock-scan evidence.

## GUI Contract

Freight exposes an off-by-default automatic-purchase toggle, Settings, and saved
selection count. A resizable selector displays 140 canonical items grouped as
equipment (100), equipment boxes (15), items (11), materials (14), with native
icons, name search, quality filters, selected-only filtering and filtered bulk
selection. Discount/pack variants share one choice but remain independent stock
cards during purchase.

The dialog edits a draft. Save updates configuration atomically; cancel/Esc/close
discard it. Disabling shopping retains choices. All four modes and combined
commerce use one saved map and a deep-copied run snapshot. Running tasks allow
viewing but not editing. No quantities, budget or discount-threshold controls.

## Validation Boundary

Targeted unit/contract tests cover schema forwarding, weekly policy, pre-click
journaling, uncertainty, no-stock exits, endpoint navigation, shared rest/water
handoff and GUI drafts/snapshots. Package sync/check/validate and Plan Doctor are
separate from runtime tests. Earlier preparation/selection live evidence does
not certify the new complete freight integration; this integration change has
not initiated another game purchase.
