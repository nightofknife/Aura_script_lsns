# Trade Goods Investment Native Templates

Native assets for the 1280x720 investment page. Runtime observations are provided
by `InvestmentVision`, and `resonance_pc.invest_trade_goods_from_shop` executes
ordered investment. The list is not reset to its top.

All template pixels originate from native game Sprite/Font assets. Supplied
screenshots are evaluation fixtures only. `manifest.json` records prefab nodes,
font/sprite PathIDs, source bundle hashes, scale, preprocessing, and thresholds.
No extracted font files or AssetBundle decryption keys are saved here.

- `levels/`: complete LV.0-20 tokens, native-size and client-size raster variants.
- `digits/`: native-font digit templates used to verify the complete-token result.
- UI assets: entry text, page icon/title, lock, selected corner, plus/minus,
  enabled confirmation button, and the success text.
- `cards/`: normal/locked/selected/maximum native frame composites, static-edge masks,
  and hanging-marker templates. Composites include the native backdrop, underlay,
  frame, and state overlays. One-pixel raster-size variants account for the 2/3
  canvas scale across columns; commodity pixels are excluded from the frame mask.
  The maximum badge is matched separately from the frame. A covered selection
  marker is accepted only with the badge and a confirmed right-panel level 20.

The native level font is `BEBAS___`, font PathID `5303591007113889663`, native
size 44. Entry text uses `SourceHanSansCN-Bold`, native size 30, Normal style,
without additional stroke. The success text uses the native `/Tips/Txt_Tips`
font and size. Confirmation combines its native background Sprite and text.
The 1920x1080 UI is scaled by 2/3 to the reference client size.

Level classification compares all 21 complete tokens, then verifies one or two
digit glyphs. The LV prefix cannot dominate the 0/9 or 10/19 decision, and the
whole-token width prevents interpreting LV.10 as LV.1. All preprocessing is
template matching and image segmentation; no OCR engine is used.

Run from the repository root, with TEMP/TMP/TMPDIR set under `.pytest_tmp`:

```powershell
.venv/Scripts/python.exe tools/build_trade_goods_investment_templates.py --game-data <game_Data> --pythonlibs <UnityPy-site-packages> --devtools-src <devtools-src>
.venv/Scripts/python.exe tools/review_trade_goods_investment_templates.py
.venv/Scripts/python.exe tools/review_trade_goods_investment_cards.py
```

The six supplied real screenshots cover ten level readings (0, 1, 2, 10, 11).
They also provide positive/negative UI matches and selected/locked slot labels.
Middle-row lock states obscured by the success banner are excluded, not treated
as unlocked. Results and annotated images are written beneath
`.pytest_tmp/trade_goods_investment_templates/`.

Thresholds are calibrated on this small still-image corpus, not established
production tolerances. Levels 3-9 and 12-20 are generated but not observed in
these screenshots. The separate maximum-layout anchor and yellow level bank
are also native-generated, without a real maximum-page sample. Disabled confirmation,
the full-level layout, scrolling,
button feedback, and toast timing are not live-verified. `page_title.png` is an
auxiliary font rendering; the actual page gate uses `page_anchor.png`.

The card review searches the whole native viewport for hanging-marker matches,
then verifies the masked frame. It does not use `slot_origins` or a pre-set count.
Clipped cards are marked partial and cannot be actionable. Toast-obscured cards
have unknown lock state and cannot be actionable. Independent visually labeled
bounds are used only to evaluate results after detection.

Four investment screenshots yielded 9 complete cards and 1 partial card each;
the exchange-menu negative yielded no cards. Selection, visible locks, unknown
toast-obscured locks, counts, and bounds IoU checks passed 57/57. Annotated real
screenshots and the card report are under `.pytest_tmp/trade_goods_investment_cards/`.
This is calibration on a small, same-layout still corpus, not cross-city or
live-scroll verification. Template correlation scores are not accuracy rates.
