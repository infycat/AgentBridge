"""Top-level PyInstaller entry point for the console PostToolUse helper."""

import os
import sys

if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
    os.environ["TEMP"] = sys._MEIPASS
    os.environ["TMP"] = sys._MEIPASS

from agentbridge.hook import run_hook

if __name__ == "__main__":
    if len(sys.argv) == 1:
        print("AgentBridgeHook is a command-line helper used by Codex/Antigravity hooks.")
        print("Configure it with --agent Codex or --agent Antigravity; it does not have a chat window.")
        if sys.stdin.isatty():
            print()
            input("Press Enter to close.")
        raise SystemExit(0)
    # Hook clients exchange UTF-8 JSON over pipes, even on legacy Windows code pages.
    if not sys.stdin.isatty():
        sys.stdin.reconfigure(encoding="utf-8-sig")
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(run_hook())
