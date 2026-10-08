# Departure-button fixture

`actual-matched-crop.png` is a lossless copy of the retained game screenshot
crop at `output/depart-button-template-20261008/actual-matched-crop.png`.
It is actual captured game pixels, not a synthetic reconstruction. Tests embed
this crop inside a synthetic 160x100 search region; placement and surrounding
pixels are synthetic. No game capture or input is performed by these tests.

- Crop SHA256: `74decc01fb37ea179c2a5a18eea26c1a17f67115648d8b055d9d5ca6c2aa7ffd`
- Original screenshot SHA256 recorded by the extraction report:
  `f64c3c25585d8ca271d33d33cfcc0edff01bcbc34da84d283f727f05e13a1149`
- Extraction report: `output/depart-button-template-20261008/report.json`
- Reconstructed template native source: `ui/mainui.asset:main_btn_launch`
- Comparison configuration: color `TM_SQDIFF_NORMED`, threshold `0.9`, no
  preprocessing and no mask.

This fixture verifies offline matching against one retained appearance, not
all live-client lighting, animation, UI scale, or device configurations.
