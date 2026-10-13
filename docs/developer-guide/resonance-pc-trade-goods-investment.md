# Trade Goods Investment Task

`tasks:trade_goods_investment_pc.yaml:trade_goods_investment_pc` is the shared
standalone and freight sub-task. It starts at an already-open exchange menu and
returns to that menu. It has no standalone GUI entry.

## Inputs

- `mode`: integer target level from 1 to 20 (default 10). Old string modes,
  booleans, floats, and out-of-range values are rejected without conversion.
- `city_name`: optional current-city name, alias, or catalog ID; it does not
  navigate cities. When provided, it bounds scans and processing by that city's
  full investable product count. Unknown non-empty names fail before input.

The task checks the 1280x720 client resolution and calls the registered
`resonance_pc.invest_trade_goods_from_shop` action. Commodity names and money
are not read with OCR; native templates recognize controls, cards and levels.

## Investment Entry Availability

Before clicking the exchange investment entry, two consecutive frames must
agree on its availability. Both native RGB templates contain the investment
icon, entry label and white or grey background in the same 169x67 crop. They use
the same ROI and shared opaque-pixel mask, preserving background colors. Scores
are `1 - TM_SQDIFF_NORMED`: a higher white score means available; a higher or
equal grey score means unavailable. These are the only two availability states.
The white template's matched center is reused for entry clicks, without another
label-only match. Locks and variable condition text are not used.

A confirmed closed entry returns `success=true`, `status=skipped`,
`triggered=false`, and `reason=investment_not_available` on the exchange menu,
with no clicks or navigation. Freight resumes selling and buying. Failure to
locate either entry template uses the existing bounded entry-location retry and
timeout; it is a missing-entry error, not a third availability state.
Cancellation and screenshot failures remain failures, not closed observations.
Logs record both entry scores and the chosen state.

The entry assets are generated from the native `HomeTrade` prefab, button/icon
Sprites and font using `--entry-only`. The mask excludes transparent contour
pixels in either version; labels and icons remain included. The user approved
the native-generated visual pair. No white/grey score comparison or live-flow
validation was run during this implementation.

## Investment Speed and City Bounds

The investment page lists only the current city's products. The native catalog
`data/meta/trade_goods_investment_cities.json` joins station sell lists to goods
quotations and goods, including initial products rather than only prestige
unlocks. Expensive card frame/state checks are limited to the city's product
count, and visiting that many products ends processing without another scan or
end-of-list drag. Screen order remains authoritative; native sell-list order is
not interpreted as investment order. Omitting the city retains unbounded visual
discovery. Regenerate the catalog with
`tools/build_trade_goods_investment_cities.py` from an installed game package.

Page readiness and card stability share one observation loop. Each frame's
success-banner result is reused by card detection. Upgrade preparation checks
the stable actual/preview levels and enabled confirmation button once, followed
directly by one submit click using that confirmed point. No input occurs between
those checks and submission. The submission observation window is unchanged.

Level adjustments use batches sized by `abs(target - preview)`, not by
`target - current`: the default preview already includes the first pending
upgrade. Each batch checks the actual level and adjustment button once, clicks
at the confirmed point with a short interval and cancellation checks, then reads
stable feedback. There is no per-click recognition or fixed post-batch pause
when feedback progresses. The final stable batch frame also supplies the
pre-submit button check, without another level-read loop. Partial progress recalculates the remaining count;
overshoot is corrected with minus. Unchanged feedback gets the delayed stable
reread before the existing plus-limit stopping rule, preserving the earlier
no-progress observation window (delayed reread at least 0.9 seconds after the
last click, followed by stable sampling). Late feedback observed
before a new batch is returned without clicking so its count can be recalculated.

Selecting a product does not move the list. After the initial full-list scan,
selection checks only the selected-corner marker and maximum badge at that
product's known rectangle, without relocating it or falling back to a full-list
scan. Selection and right-panel levels must stabilize together in the same
observation loop. A maximum badge still requires the confirmed right-panel
maximum layout. Unconfirmed selection times out without another click.

## Freight Integration

`auto_trade_goods_investment` defaults to false. The existing freight option
sets integer `trade_goods_investment_mode` (1-20, default 10); preview planning
excludes both inputs. The GUI uses a numeric target-level control.
Combined commerce forwards them to freight, not passenger execution.

The initial city is excluded. Each later visit, including a return to the
starting city and the final sale city, enters the exchange once and invokes
the same task using `ActionInjector.execute("aura.run_task", ...)` in the
parent's execution context. The parent reads `framework_data.nodes.invest.output`,
checks its success, mode, target and exchange-menu return, then resumes the
existing sell/buy worker without entering the exchange again. No runner or
scheduler is constructed by the business action.

Per-product progress is forwarded through a scoped callback to the parent
freight progress reporter. The callback is reset on completion, failure and
cancellation; standalone execution requires no freight context. An uncertain
child result blocks subsequent commerce. Cancellation uses the framework's
child-task and synchronous-action cleanup path.

## Stopping

An unchanged plus click gets a delayed stable reread, not another plus click.
The pending preview is still submitted once, even if plus never increased it:
the available resources may fund exactly one level. If the actual level rises,
the upgrade is recorded. If the actual and preview
levels remain unchanged through the bounded observation window, with no success
toast, the submission is treated as ineffective. No-effect submissions are never repeated.
Unreadable or contradictory feedback remains an explicit failure, not a guessed
resource limit. Both confirmed stopping cases return to the exchange menu.

Reaching the requested target normally advances to the next product. Locked
products end the visit; uncertain card, level, scroll or submission states fail
explicitly. An unconfirmed submission is never repeated automatically.

The next product is never attempted while the current product is below level
10, even if a requested target of 1-9 was reached. Already-unlocked products
above a low target can be skipped normally. If a product is at least level 10
but cannot reach the requested target, the next unfinished, unlocked product
gets one upgrade preparation and at most one confirmation before the visit
ends. Reading already-complete products does not spend that final attempt.

Investment spending is not deducted from trade profit. The route is not
replanned after investment changes purchase quantities; enabled results carry
a warning and per-visit transaction/level totals.

## Validation Boundary

The October 9 trials first exposed the old stopping-condition failure after a
0-to-6 upgrade, then verified a 0-to-9 upgrade and normal exchange-menu return
under the intermediate stop rule. The subsequent single-submit/no-effect rule
and freight sub-task integration are covered by offline tests, not live runs.
Numeric targets and the unlock-level/final-attempt rules have offline coverage
only. City count bounds, local selection, merged observation loops, batch
feedback, and the single pre-submit check have offline coverage only.
Per-product identity and persistent investment records remain a separate
planned change.

## Return to the Exchange After Investment

Return uses a fresh screenshot to distinguish the investment page from the
exchange menu. Two consecutive investment observations authorize a back click;
page identity and the button are refreshed immediately before each click.
There are at most three clicks, at least three seconds apart, within a
20-second deadline. Unknown, ambiguous, success-overlay, or invalid-template
observations do not authorize a click. Once the exchange menu is seen, retries
are disabled; two consecutive exchange observations confirm completion.

A return timeout retains all confirmed transactions in the error and progress
report. It does not submit another investment. White/grey entry availability
checks remain in force. These return changes have offline fake-input coverage;
no game execution was performed for this integration.
