# Selection Replay Crops

`current_list.png` and `current_footer.png` are cropped from the authorized
2026-10-11 read-only client capture, before shopping. `selected_list.png` and
`selected_footer.png` are from the user's supplied selected-card screenshot.
No player UID, currency header, NPC, or unrelated menu pixels are retained.

List crop: client `(594, 143, 684, 500)`.
Footer crop: client `(1000, 646, 264, 48)`.
Tests reconstruct all other client pixels as black; those pixels are not game
evidence. Current list includes two stones and two book cards with differing
quantities, and the selected list includes a yellow-selected staff and discounted
paper cards. The footer supplies positive zero total and negative 50000 total.
