# Equipment Presence Refresh

Equipment catalog schema 2 uses `aggregation: presence_by_equipment_id`.
The existing `inventory_equipment.json` and PNG templates remain the source of
equipment identities. Matching uses color TM_CCOEFF_NORMED at the catalog's 0.82
threshold. Items and materials continue to report stack quantities.

An equipment refresh returns detected types in catalog order:

```json
{
  "category": "equipment",
  "recognition_mode": "presence",
  "catalog_schema_version": 2,
  "matched_equipment_count": 1,
  "equipment": [
    {"equipment_id": "thunder_god", "name": "Thunder God", "owned": true}
  ]
}
```

Names in actual results come from the catalog's Chinese display names.
`matched_equipment_count` counts detected equipment types, not physical copies.
Equipment entries no longer contain `count`, and the category no longer contains
`matched_card_count`. Unmatched types are omitted; omission is not proof that an
account lacks an equipment type. Duplicate icons still cannot establish which
game identity is present. For equal-scoring overlapping equipment matches the
first catalog entry is retained, as configured by the user.

The scanner unions IDs across pages. It does not align physical cards across
pages. It continues through pages containing only previously detected equipment.
Once every equipment ID in the current catalog has been detected as owned, the
scan completes immediately before the next drag
(`all_supported_equipment_found`). This uses the union from the current scan,
not previously cached ownership.
Three consecutive scroll attempts with stable grid mean absolute pixel difference
at most 1.5 end the scan (`three_consecutive_unchanged_scrolls`). This is a visual
end-of-scroll heuristic, not a game-provided bottom indicator. A failed drag or
visually identical repeated pages can also satisfy it. Equipment scanning has no
scroll-count limit; the legacy `max_scrolls` argument is accepted but ignored for
equipment. Items and materials retain their existing scroll limits.

Only the template region must be fully visible for equipment. A clipped card
border, level label, or avatar does not require rejecting an otherwise complete
identity region. Single-page cross-template overlap suppression still chooses
the strongest candidate at each location before accumulating equipment IDs.

Consumers should accept legacy positive `count` entries as ownership evidence.
Presence-only results must not be displayed as measured quantities or interpreted
as unlimited copies for simultaneous team slots.

Existing equipment count-oriented tests need to be migrated when testing is
authorized. Regression coverage should include repeated types across moving
pages, stationary bottom detection, scans beyond 30 scrolls, catalog-order duplicate
ties, clipped cards with complete identity regions, GUI ownership display, and
legacy count compatibility.
