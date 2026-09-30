# Native exhausted-negotiation marker

`plans/resonance_pc/templates/trade_buy_rebargain_button.png` is a 103×34 RGB
template for the stable **再交涉** label and request-book icon on the white replacement negotiation
button. It is composed from the installed game's materials, without screenshot
pixels. The changing `x1` cost is excluded.

## Source materials and geometry

- Prefab: `Patch/Asset/ui/hometrade/hometrade_splited/group_trade.asset`,
  `Group_Trade/Group_Buy/Btn_Renegotiate` (GameObject `2913748993047873171`).
- Button background: `trade_btn_renegotiate`, Sprite `-5993440050832368587`,
  from `Patch/Asset/ui/hometrade.asset`. Its native rectangle is 210×60.
- Request-book icon: `Btn_Renegotiate/Img_` (GameObject `1976339534609225916`),
  `trade_icon_renegotiation`, Sprite `14458624934965497`, from the same
  `Patch/Asset/ui/hometrade.asset` bundle. Its native rectangle is 50×50,
  centered at `(40, 0)` relative to the button. Tight-mesh transparent
  margins are restored before composition. At 1280×720 it is approximately
  33×33 pixels. The crop ends before the cost-text rectangle at native x=167,
  trimming the icon's outermost right edge to exclude changing cost pixels.
- Label: `Btn_Renegotiate/Txt_` (GameObject `-5319291662600138162`),
  localized text ID `80605277`; the Chinese string is read from
  `Patch/BinaryConfig/TextFactory.bin`.
- Font: `SourceHanSansCN-Bold`, Font `-2237384480076949585`, from
  `Patch/Asset/ui/font/originpack.asset`; size 26, regular font style,
  black, middle-left alignment. The label's native rectangle is 160×50,
  centered at `(2.4, 0)` relative to the button.
- Button local position: `(419.9, -153.2)` in `Group_Buy`, whose horizontal
  anchor is 0.7. The output uses the 1920×1080 canvas at a 2/3 scale for
  Aura's 1280×720 reference capture. The stable native crop is
  `(12, 5, 166, 55)` relative to the button's top-left.
- Recognition search rectangle: `(1090, 425, 170, 70)` at 1280×720, matching
  the existing negotiation-button region. The marker should be confirmed
  across successive captures; recognizing it does not authorize clicking it.

## Native visibility condition

`Patch/Script/UIHomeTrade/UITradeController.lua`, `RefreshBargainPanel`,
source lines 300–337 (read from Lua 5.3 bytecode), sets `Btn_Renegotiate`
active at line 336 when both conditions hold:

1. `BargainSuccessRateIndex` (or `RiseSuccessRateIndex` for selling) has not
   exceeded `GetMaxBargainCount(isBuy)`.
2. The city's used count (`CurCityGoodsInfo.b_num` for buying, `r_num` for
   selling) is at least that maximum.

This is an explicit marker for exhausted available attempts. The native
`ConfirmBargain` function, lines 368–380, starts the `refreshBargain` item
prompt in this state. Automation uses the marker to stop negotiating and
continue the purchase; it does not replenish attempts.

## Regeneration

The development-only generator is `tools/generate_trade_rebargain_template.py`.
It requires Pillow, UnityPy, and the separate `Aura_script_lsns_devtools`
source package. Supply the local game data and devtools directories:

```powershell
python tools/generate_trade_rebargain_template.py `
  --data-root path/to/Resonance_Data `
  --devtools-root path/to/Aura_script_lsns_devtools
```

The font is read into memory and is not redistributed with the project.
No tests or template-matching validation were performed for this asset.
