# Inspiration and hex regression evidence

These lossless 180×180 crops come from the real game capture run
`c4a03c0853844bd08046e3f07d45fd5c`, scan `0001`, on 2026-10-01.
`samples.json` records each source frame and rectangle; pixels were not resized
or recolored. Tests place the crop in an otherwise empty 1280×720 board image
and inspect only candidates within 35 pixels of the labeled crop center.

- `u10_*`: true inspiration on U10, viewed above, in front, and below the cube.
- `d22_*`: true inspiration on D22, including side views, the projecting star,
  and views next to the boss effect.
- `hex_red_*`: the yellow D12 cube glyph affected by red light. Frames 0490
  and 0507 were incorrectly promoted to inspiration in the prior live run.

The partial U10 ring in frame 0028 is real but ambiguous in this one frame.
It must remain weak, non-confirmable occlusion evidence. Clear other poses
can establish the occupant. The false D12 crops are also non-confirmable;
repeating their frames cannot establish an inspiration occupant.
