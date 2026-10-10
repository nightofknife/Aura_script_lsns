# Trade Goods Investment Task

`tasks:trade_goods_investment_pc.yaml:trade_goods_investment_pc` is the shared
standalone and freight sub-task. It starts at an already-open exchange menu and
returns to that menu. It has no standalone GUI entry.

## Inputs

- `mode`: integer target level from 1 to 20 (default 10). Old string modes,
  booleans, floats, and out-of-range values are rejected without conversion.
- `city_name`: optional label for results and logs; it does not navigate cities.

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
only; city-specific product identity and persistent investment records remain
a separate planned change.
