# Fatigue observation crops

`buy.png` and `sell.png` contain the fatigue HUD from the user-provided buy
and sell screenshots, respectively. Both display `77/872`.

The original 1282 × 752 window images were cropped to the 1280 × 720 game
client at `(1, 31)`, then to client ROI `(938, 14, 95, 24)`. The PNGs retain
the original pixels. The crop procedure was recorded in
`.pytest_tmp/trade-fatigue-images/verify_images.py` during the earlier
trade-page OCR investigation. No screenshot of the full desktop is included.
