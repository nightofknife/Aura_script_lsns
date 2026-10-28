# Warehouse state regression capture

`warehouse_before_failed_20261024.png` is the unmodified 1280x720 client frame
`0013_prepare.png` saved during the authorized 2026-10-24 live task, CID
`769179538272817152`. The task entered the warehouse but stopped at
`connect_train_product_source_unconfirmed`, before any goods click.

The expected observation is warehouse page, target train/product connector
unselected. This frame is a regression input, not a template-generation source.
It is separate from the user-provided stable screenshot used to crop the new
title/button templates. No successful withdrawal or server persistence is
established by replaying it.

`city_panel_before_20261024.png` is the unchanged 1280x720 city building panel
captured before the combination task's first live attempt. It shows Yunxiu Bridge
Base, not the train/game main screen. The original combination preflight wrongly
required the departure button and stopped without any game input. This image is
a regression input for the existing city badge reader, not a new template source.
