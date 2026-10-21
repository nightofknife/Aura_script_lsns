# PC Towing Service

## Standalone Contract

Task: `tasks:use_trailer_service_pc.yaml:use_trailer_service_pc`.
Action: `resonance_pc.use_trailer_service_from_map`.

Start on the intercity map with the desired destination already selected, using
a 1280x720 client. This operation does not select cities, drive normally, wait
for arrival, recover fatigue, purchase extra uses, or upgrade towing.
Freight can call it for preselected long-distance legs. Passenger trips do not
use towing. The standalone operation still owns only one departure submission.

Inputs:

```yaml
daily_first_n: 1
payment: gold  # gold | mileage
```

Read the game's remaining/total fraction below the towing button. Daily usage
is `total - remaining`, including uses made manually or by earlier tasks.
Tow only when remaining is positive and daily usage is below `daily_first_n`.
The limit is a strict positive integer and may exceed the account's total.
There is no local quota ledger or predicted decrement.

## UI Operation

1. Confirm the destination-map button and two consistent numeric quota readings.
2. Recheck quota immediately before opening the towing dialog.
3. Open the dialog and confirm its title, buttons, and unambiguous payment state.
4. Keep the already selected payment, or switch and verify the requested payment.
5. Obtain fresh stable payment/button evidence and submit confirmation once.
6. Report departure only after two consistent travel-horn or station-button
   observations. Report a fatigue panel separately. Dialog disappearance or
   return to the map is not proof of departure.

Opening and switching each allow one initial click plus three supplemental
clicks. Each supplemental click requires freshly recognized source state;
unknown UI cannot authorize input. Observations poll every 0.2 seconds, with
two consistent observations, two-second state windows, and a twelve-second
read-only post-submit window. Every wait and input checks cancellation.

## Results

All business results include `success`, `status`, `reason`, `submitted`,
`departed`, `page_state`, `quota`, click counts, and elapsed time.

- `status: not_used`, `success: true`: quota exhausted or daily limit reached;
  map preserved and no input issued.
- `status: departed`, `success: true`: positive travel/arrival-ready evidence;
  the caller remains responsible for waiting for arrival.
- `status: blocked`, `success: false`: unreadable quota, uncertain UI/payment,
  failed input, fatigue panel, or unconfirmed departure. Do not automatically
  submit again, change currency, or start ordinary driving.

`submitted: true` is conservative: the confirmation click was attempted and may
have reached the game. It does not prove a charge or successful departure.
Unknown rejection dialogs, including insufficient-resource cases without a
validated state detector, stop safely rather than authorizing a fallback.
Framework node success is not business success; callers must inspect output.

## Recognition Assets

Six approved opaque RGB templates reside under `templates/navigation/trailer`.
They use the framework matcher with `TM_SQDIFF_NORMED`, score `1 - difference`,
threshold 0.90, no grayscale, preprocessing, or masks. Currency templates contain
the icon and selected white background but no variable amounts. Only the
80x28 quota crop uses OCR; prices, balances, and speed are not read.

`manifest.json` pins dimensions, regions and SHA256. `native_provenance.json`
records original sprites/font IDs, native-bundle hashes, composition/cropping
recipes and supplied-screenshot calibration. Existing fatigue, horn and station
templates retain their original navigation matching method.

Offline tests cover supplied screenshots and simulated UI transitions. These
are not live-game acceptance or evidence across all game themes/resolutions.

## Freight Integration

Freight execution inputs (also supported under combined commerce's `trade_inputs`):

```yaml
auto_trailer: false
trailer_daily_first_n: 1
trailer_payment: gold
```

These are execution-only options, not solver/preview parameters. Allocation uses
the finished route, including optional reposition legs to the fixed-route start.
Select the longest X legs by actual game kilometers, with earlier occurrences
winning ties. Repeated occurrences of the same city pair remain separate legs.
Selection reads only the local native-distance catalog; it does not open the map,
read quota, or reserve a predicted number of daily uses. The existing fatigue
budget, route, books and negotiation algorithm remain unchanged.

`data/meta/trailer_route_distances.json` covers all 21 canonical trading cities.
Its builder uses the native configured line graph, preserves transit stations,
and reproduces the map's `ceil(shortest_raw_distance * disRatio)` display rule.
The supplied screenshot's Shoggolith-to-Wilderness distance differs from
the current native stationary value (136 versus 140 km); the catalog records this
unresolved cross-check rather than fabricating a scale correction.

On each selected leg, after destination selection, call the standalone towing
operation. A verified `not_used` result with `quota_exhausted` or
`daily_limit_reached`, `submitted: false` and `page_state: map` authorizes ordinary
departure on the same map. It also disables towing for all remaining legs of this
freight run, across reposition and trading, without further quota checks or
replacement candidates. Starting a new freight task creates a new run-local policy.

Confirmed towing skips the ordinary departure button and proceeds to the existing
arrival waiter, without automatic pickup. Normal travel keeps its original pickup
behavior. If a submitted towing attempt reaches the fatigue panel, stop with
`trailer_submission_requires_review` and preserve that panel. There is no native
evidence that the charge was rejected; even an unchanged quota can lag settlement,
so this flow never automatically recovers fatigue and submits a second payment.
All towing/payment/capture uncertainty blocks departure and subsequent city actions.
A no-input quota skip remains sticky during ordinary same-leg medicine retries.

Planning output contains `trailer_plan.selected_legs` (phase/index/distance).
Execution output contains `trailer_service` with the selected legs, attempted
results and the quota stop reason. No daily quota ledger is created.
