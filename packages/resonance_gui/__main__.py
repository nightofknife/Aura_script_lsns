from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    multiprocessing.freeze_support()

    from packages.resonance_gui.app import launch_resonance_gui, self_check_resonance_gui

    if "--self-check" in sys.argv[1:]:
        return self_check_resonance_gui()
    return launch_resonance_gui()


if __name__ == "__main__":
    raise SystemExit(main())
