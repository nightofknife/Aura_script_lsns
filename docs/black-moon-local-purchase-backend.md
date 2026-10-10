# Black Moon Local Purchase Backend

## Scope

The PC freight executor purchases selected LOCAL-store products at the freight
start city (after any existing reposition) and every confirmed route arrival,
including the endpoint. No detours or reposition-only stops are added. Arrival
extras run first, then shopping, existing planned sparkling-water cups, and the
exchange visit. Shopping and drinking share one rest-area visit. Fatigue refresh
and cup planning happen before entering rest.

GUI settings, priorities, money limits, headquarters purchases, manual refreshes,
and refresh-coupon consumption are out of scope. The feature defaults to off.

## Inputs

The `auto_cycle_trade_pc` task and combined commerce execution accept:

```yaml
auto_black_moon_local_purchase: true
black_moon_local_purchase_items:
  - self_observation_film_roll
  - purchase_order_book
  - advertising_ticket
  - item_11400084
black_moon_purchase_record_scope: default
```

Use canonical `item_id` values from
`plans/resonance_pc/data/meta/black_moon_local_shop_catalog.json`. The catalogue
contains equipment, equipment boxes, items, and materials, with quality and native
source IDs for later GUI grouping. All available discount/pack variants of a
selected product are selected; separate stock cards are never collapsed by name.
Combined commerce forwards these inputs only to execution, not route previews.

## Child Contract

`tasks:black_moon_local_purchase_pc.yaml:black_moon_local_purchase_pc` starts at
an already-open `rest_menu`. Inputs are `city_name`, `selected_item_ids`, and
`record_scope`. Its `purchase` node calls
`resonance_pc.purchase_black_moon_local_from_rest_menu` using the parent engine.
Successful output has `success: true`, `status: completed|skipped`, `reason`,
canonical `city_key`, `week_key`, `completed_this_week`, `purchased_count`,
`purchased_items`, and `page_state: rest_menu`. Failure does not authorize the
exchange/departure to continue. Per-city visits are exposed under
`execution.black_moon_local_purchase` and the top-level field of that name.

## Durable Records

`core/persistent_data` stores `black-moon-shop-purchases.json` under its own
`user-data` root. Keys are explicit account scope, refresh week, and canonical
city. Change the scope when changing accounts; no account UID is inferred.
The reset policy is Monday 05:00 Asia/Shanghai. The native configuration proves
weekly/05:00; weekday corroboration is separately attributed in the catalogue.

Completed records skip shopping before rest navigation. Empty selections never
mark a city completed. An interrupted visit retains its frozen selection.
Completion requires a recognized full scan and no desired available stock.
Unrecognized available cards, ambiguous list boundaries, and failed purchases
leave the week incomplete. A desired product with no available recognized card
has no outstanding purchase, whether absent or already sold out; the backend
does not invent identities for cards obscured by the sold-out sign.

Before the single confirmation click, a pending batch records its stock counts
and selected stock rows. A known pre-submit cancellation closes that intent as
not purchased. An attempted click with no confirmed result remains pending.
Restarts reconcile the full available-product counts and sold-out count change;
they never replay unresolved confirmation. Reward evidence is recorded even if
cancellation arrives, but a full post-purchase sold-out reconciliation is still
required for weekly completion. Return-navigation failure cannot erase confirmed
purchase facts or a completed week. Cooperative file locks serialize sessions and
ledger writes across tasks/processes; malformed ledger data is an error.

## Recognition Assets

Templates and catalogue builders read native Unity/config resources offline.
Runtime reads the bundled JSON/PNG assets only and performs no OCR of prices,
balances, quotas, or pack quantities. Card identity uses native item art, sold-out
state uses the hanging sign, and partial cards are never clicked. Scroll overlap
is positional evidence only, not item identity or purchase proof.

Native font rendering and template thresholds remain uncalibrated. Selected
darkening reuses a native overlay because its runtime tint is not serialized;
asset provenance and limitations are recorded in the template manifest. No
tests, import/compile checks, screenshot matching, GUI, or live game runs were
performed for this implementation, per the current working rules.

## Implementation Plan And Handoff

### Completed Backend Phase

- Native candidate catalogue: seven cities, 140 canonical products, 247 commodity
  variants. Native template bank: 280 normal/selected item variants plus controls,
  frames, anchors, and source provenance.
- Execution-only task inputs, combined-commerce forwarding, and child task/action
  registration. The feature is disabled by default and has no GUI controls.
- Freight-start and confirmed-arrival integration, including the endpoint; one
  shared rest visit preserves the existing planned drinking count.
- Weekly city gates, frozen selections, pending batches, receipt persistence,
  sold-out reconciliation, and per-city execution/progress output.
- Static source review only. Runtime recognition and the full purchase flow are
  not verified; completion of this phase is not a claim of live readiness.

### Repository Integration Phase

Submit the feature code, complete template directory, catalogue, both native
builders, task registrations, and this plan together. Preserve the latest main
investment-entry behavior and unrelated changes in other worktrees. Keep the
feature disabled by default and do not add GUI, version bumps, tags, or a release
solely for this handoff. User authorization currently covers commit, push, and
merge, not additional testing or live game operations.

### Next Implementation Phase

1. Wait for the user's revised GUI requirements before editing any GUI. Reuse the
   catalogue category and quality metadata for grouping, with buy/not-buy
   selections only; do not add priorities or money limits.
2. Bind the GUI to the three existing execution inputs. Persist selections in the
   existing settings mechanism, keep the default disabled, and expose an explicit
   account record scope without guessing the account UID.
3. Reuse the existing progress stage and per-city output. Distinguish a completed
   weekly check from a failed visit; do not clear purchase facts when return
   navigation fails. Midweek settings changes do not invalidate a completed city
   record or replace an interrupted visit's frozen selection.
4. Only after explicit authorization, measure native template scores and perform
   the real-game acceptance work below. Do not start this workflow merely because
   the implementation plan lists it.
5. Resolve recognition limitations before advertising the feature as verified or
   enabling it by default. Package/release only under a separate user request.

### Outstanding Recognition Work

- The native yellow selected-banner sprite reference `5556224950374819097` could
  not be resolved. Current selection evidence uses native-font text with negative
  background space; geometry/item variants reuse a native dark overlay. Its
  selected-state runtime tint and alpha remain unconfirmed.
- Visible card bounds are `327x120`, columns `599/937`, with a safe viewport
  ending at client `y=643`. Serialized bottom padding is 60 native pixels, but the
  runtime final-row fit is not established. Never mark a week complete by treating
  a clipped or unrecognized row as absent.
- Thresholds, Pillow-versus-Unity font rendering, and complete-flow timing remain
  uncalibrated. Record source/threshold changes in the native builder and manifest,
  not only in generated pixels.

Authorized future acceptance should cover headquarters-to-LOCAL switching,
batch on/off, normal/selected/sold-out cards, overlapping scroll pages, duplicated
discount/pack stock, reorder after purchase, reward exit, weekly skips/reset,
pre-submit cancellation, pending restart reconciliation, receipt retention after
return failure, and shared shopping/drinking visits. None of these checks has been
executed for this feature.
