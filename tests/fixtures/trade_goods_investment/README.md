# Real Investment Screenshot Fixtures

Six user-provided PNG screenshots of the Farstar Bridge trade exchange and
investment page. Each 1282x752 window screenshot is reduced to its unchanged
1280x720 client pixels with the crop `(1, 31, 1281, 751)`; no scaling is applied.
`samples.json` retains original filenames and SHA-256 hashes with expected
page, actual level, preview level, and selected slot.

These images are evaluation inputs only. The native template builder reads
game Font/Sprite/Prefab resources and never reads these fixtures. No pixel from
a fixture is copied into the production template directory.

The success image contains a visible toast, so the hidden middle-row lock
glyphs are not labeled as unlocked. Test output, annotations, and previews are
stored under `.pytest_tmp/trade_goods_investment_templates/`.
