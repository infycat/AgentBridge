"""Top-level PyInstaller entry point for the tray application."""

import os
import sys
import traceback
from pathlib import Path

# In one-file mode, keep Python-side temporary files beside the extracted bundle.
# This also lets the app start when the user's Windows TEMP/TMP is unavailable.
if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
    os.environ["TEMP"] = sys._MEIPASS
    os.environ["TMP"] = sys._MEIPASS
    # PyInstaller's windowed bootloader leaves these as None. Uvicorn and some
    # third-party log handlers expect writable stream objects during startup.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8")

from agentbridge.__main__ import main

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        # Windowed builds have no console; retain a local diagnostic for launch failures.
        try:
            from agentbridge.config import data_dir

            base = data_dir()
            base.mkdir(parents=True, exist_ok=True)
            with (base / "agentbridge.log").open("a", encoding="utf-8") as log:
                log.write(traceback.format_exc() + "\n")
        except OSError:
            pass
        raise
