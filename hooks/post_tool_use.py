"""Thin source-checkout entry point; packaged users can call AgentBridge.exe hook."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentbridge.hook import run_hook

if __name__ == "__main__":
    raise SystemExit(run_hook())
