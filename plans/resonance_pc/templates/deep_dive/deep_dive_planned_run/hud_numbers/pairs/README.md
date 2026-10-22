# HUD numeric pair glyphs

This directory contains alpha glyph templates for `0`–`9`, `/`, `(` and `)`.
No complete `0/1` or `0/2` value is embedded or used as an expected game state.
`sources.json` records the font identity, native/screen size, SHA256 and render phase of each file.

- Action counters: native `HarmonyOS_Sans_Medium`, path ID `-3758921232364580136`, native size 33 at canvas scale 2/3 (screen size 22). The Move/Spin Not/Select/Complete text components share this font and size.
- A neighbouring native size 32 (screen size 21.333) is retained for action-counter rasterization tolerance, using the same font and four subpixel render phases. Matching this variant does not change the confidence or class-margin acceptance gates.
- Progress suffix variants: the embedded HarmonyOS medium font at screen size 18 and `SourceHanSansCN-Medium` at native size 26 (screen size 17.333). Both are native font sources; the reader retains the winning source template for each glyph.

The fonts were extracted from the user's installed game resources. Only the rendered glyph PNGs are runtime assets; no full fonts or runtime game installation are required.

Reading requires a unique glyph class, a minimum overlap score, a margin over the next class, one slash and a geometrically consistent text row. Parentheses are accepted only as a recognized enclosing pair. A missing, clipped, unsupported or ambiguous sequence returns unknown. Template confidence is a heuristic match score rather than a calibrated probability.

Normal controls use light glyphs. Active action controls use dark glyphs only when the current numeric ROI has a dominant cyan background (at least 55% of its inset area). Dark pixels must also lie inside that detected cyan surface, so ordinary dark button/board backgrounds never become a dark-text mask.

The saved 1280×720 board frame from the live run on 2026-09-30 was read as move `0/1`, rotation `0/1`, progress `0/2`, with minimum sequence scores 0.9182, 0.8045 and 0.7672 respectively. These scores cover that recorded frame only.

The recorded active movement-selection frame from that day was also read as `0/1`, `0/1`, `0/2`, with scores 0.9232, 0.8045 and 0.7672. Its movement ROI had 89.65% cyan background coverage; only that counter selected the dark foreground path.

The recorded active rotation-selection frame required the native size 32 variant. Its slash score increased from 0.7514 to 0.9526, while its margin over `7` increased from 0.0501 to 0.2316. All three numeric pairs were then recognized without loosening acceptance thresholds.
