# Recycling observation fixture

`farstar_bridge_city.png` is the user's unmodified 1280 x 720 screenshot,
originally named `5837d2d8927dcf04d5c4c5be2a93a0ee.png`. It shows the city
panel before entering the recycling station.

The station-title template was cropped from this same screenshot. The tests
therefore verify the complete ROI, native-coordinate and page-classification
integration on a real frame; they do not establish recognition across unseen
backgrounds, animation frames or the wilderness station.

The tests that paste existing control/reward assets onto frames explicitly
exercise asset/threshold/coordinate integration. Those composite frames are
not screenshots of the real recycling/reward pages and do not calibrate their
live-game layout.
