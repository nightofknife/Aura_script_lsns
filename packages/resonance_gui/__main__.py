from __future__ import annotations

import sys

from packages.resonance_gui.app import launch_resonance_gui, self_check_resonance_gui


if __name__ == "__main__":
    if "--self-check" in sys.argv[1:]:
        raise SystemExit(self_check_resonance_gui())
    raise SystemExit(launch_resonance_gui())
