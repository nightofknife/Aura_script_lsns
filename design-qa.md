# Design QA — City trade timeline rebuild

> Historical verification note: the `.pytest_tmp` screenshots referenced below
> were temporary review artifacts and are not present in the current workspace.
> The pass counts record the verification run performed for this rebuild; they
> are not the current default `tests/smoke` collection and have not been rerun as
> part of later documentation maintenance.

- Source visual truth: `.pytest_tmp/city_progress_design/user-selected-target.png`
- Implementation screenshot: `.pytest_tmp/city_progress_design/implementation-v4.png`
- Conditional-state screenshot: `.pytest_tmp/city_progress_design/implementation-v4-no-investment.png`
- Full comparison: `.pytest_tmp/city_progress_design/comparison-user-target-full-v4.png`
- Focused right-panel comparison: `.pytest_tmp/city_progress_design/comparison-user-target-right-v4.png`
- Viewport: 1440 × 893 native Windows desktop window
- Source pixels: 1592 × 988, normalized to 1440 × 893 with Lanczos resampling
- Implementation pixels: 1440 × 893
- Capture method: native Qt `QWidget.grab()` after the window reached the target state
- State: six-city freight route; city 1 completed, Cape City active, cities 3–6 waiting; logs collapsed

## Findings

No actionable P0, P1, or P2 differences remain in the requested right-hand progress surface.

- Fonts and typography: the implementation preserves the existing Microsoft YaHei UI / Segoe UI stack and matches the source's title, city, phase, and status hierarchy. Long city labels remain fully visible in the target viewport.
- Spacing and layout: the visible progress surface is now an independent city timeline rather than a styled tree. It includes connected nodes, one-line completed/waiting city cards, an expanded current-city card, a two-column phase panel, compact dual progress bars, and a collapsed log row.
- Colors and visual tokens: completed cards and badges use pale sage, the active city and active badge use pale amber, and waiting cards/badges use warm neutral beige-gray. Text labels duplicate every color state.
- Image and icon fidelity: the selected source contains no raster content assets. Timeline rails, nodes, and state marks are native Qt interface primitives rendered at display density.
- Copy and content: phase placement matches the source. Cape City shows arrival, investment, sell, buy, and departure when investment is enabled. With the option disabled, the rendered Cape panel contains arrival, sell, buy, and departure and contains no investment phase.
- Interaction: the current city expands automatically from real progress state; the timeline scrolls for longer routes; logs remain collapsed until requested and open automatically on failures. The hidden task tree remains only as the existing execution model and fallback for non-freight workflows.
- Accessibility: every status has visible text in addition to color. Phase status badges are normal Qt labels, and the scroll surface retains native keyboard/wheel behavior.

## Focused comparison evidence

The focused side-by-side comparison confirms that both source and implementation now share the same core composition: left timeline rail, completed check node, active ring node, full-width city cards, a two-column phase grid inside the active city, collapsed future cities, and a collapsed detailed-log control. The implementation is slightly denser vertically, as requested, but preserves the same hierarchy and contents.

## Comparison history

### Earlier implementation — rejected

- [P1] The visible component remained a `QTreeWidget` with nested rows instead of the selected timeline layout.
- [P1] The active city used a vertical phase list instead of the selected two-column phase panel.
- [P2] Connected timeline nodes and city-card surfaces were missing.
- The earlier `passed` result was incorrect because those structural mismatches were treated as an acceptable native adaptation.

### Rebuild — implementation-v4

Fixes:

- Added a dedicated native Qt city timeline surface.
- Added connected completed/current/waiting nodes.
- Added full-width semantic city cards.
- Added the active city's two-column phase panel and state badges.
- Kept all six cities visible at 1440 × 893.
- Added a separate rendered verification state with Cape investment disabled.

Post-fix evidence:

- `comparison-user-target-full-v4.png`
- `comparison-user-target-right-v4.png`
- `implementation-v4-no-investment.png`

No P0/P1/P2 findings remain.

## Follow-up polish

- [P3] The generated source uses slightly taller pending city cards. The implementation keeps them more compact to satisfy the user's explicit density requirement.

## Verification at the time of the rebuild

- GUI suite: 71 passed.
- Investment execution regressions: 6 passed, 16 deselected.
- Diff whitespace check: passed.

final result: passed
