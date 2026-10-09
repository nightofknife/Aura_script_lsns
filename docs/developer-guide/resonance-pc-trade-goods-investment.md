# Automatic Trade Goods Investment

The PC freight flow can invest at every city visit after the initial departure.
Returning to the starting city is eligible, and the endpoint is eligible before
final sale. Investment runs in the existing exchange visit before selling and
buying. It is independent of Cape City Mirage Island investment.

## Configuration

- `auto_trade_goods_investment`: defaults to `false`.
- `trade_goods_investment_mode`: `unlock` (10), `balanced` (14), or `full` (20).

The freight parameter editor shared by the three-column workflow persists these fields.
The preview planner ignores them. Combined commerce forwards them only to the
freight execution phase. The standalone `trade_goods_investment_pc` task begins
at an already-open exchange menu and returns to that menu.

## Execution

Goods are traversed in their displayed unlock order using native card frames,
lock overlays, and selection markers. Names and commodity-specific icon banks
are not required. The list is not reset to its top, and MAX is never clicked.
Current/preview levels are recognized from native full-token and digit banks.
The maximum-level layout uses a separate native anchor and a confirmed level 20.
Maximum cards have their own native frame and badge. If that overlay covers the
selection marker, the badge and confirmed right-panel level 20 are both required.

Each plus click is followed by a stable preview reading. An unchanged preview
gets one bounded retry after rereading, then the current affordable selection is
submitted. Oversized preexisting previews are reduced with minus. Confirm must
match both template shape and native enabled colors. No amount or allowance OCR
is performed.

The submit button is clicked once. The next operation waits until the success
toast is absent and the actual level matches the submitted target. A missed toast
is recoverable only when that actual-level update is stable; an uncertain result
stops the task instead of re-submitting. A product reaches its configured target
before the next product is selected. If the current product cannot advance, or
the next product remains locked, the city investment finishes normally.

Scrolling preserves an overlap. Deduplication matches an ordered suffix of the
old visible complete cards to the prefix of the new cards using internal image
fingerprints, not a global item-image identity. Partial/obscured cards cannot be
clicked. An initial clipped top card without processed-overlap evidence blocks
execution; a visible full-card header with an unconfirmed frame cannot be skipped.
Unknown overlap, stalled scrolling with an unprocessed partial card,
uncertain levels, and unexpected pages stop execution. Polls and clicks check
cooperative cancellation. The exchange menu must be recognized again before
normal commerce resumes.
Native maximum badges hide commodity pixels; matching maximum states in the
same column is equivalent only for completed-card bookkeeping. It cannot mark
an ordinary product complete or authorize an investment.

## Results and Progress

Progress uses the distinct `trade_goods_investment` stage, with city occurrence,
product ordinal, actual level, preview level, target, and transaction count.
Results include per-visit confirmed transactions and aggregate upgraded levels.
Investment spending is not read or subtracted from trade profit. The existing
frozen route estimate may differ after investment changes purchase quantities;
the result carries an explicit warning when the feature is enabled.

## Evidence and Remaining Coverage

Native-generated templates previously passed 138 still-image checks; native
card detection passed 57 checks across four investment images and a menu
negative. Those checks predate runtime integration and do not establish live
scrolling, button feedback, maximum layout, disabled confirmation, or timing.
No new runtime tests were written or executed during integration under the
user's testing policy. The generated maximum-layout and maximum-card assets have no real screenshot
sample in this task. Capture/recognition failures stop the feature explicitly.
