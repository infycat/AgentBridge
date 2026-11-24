"""Run the local two-agent sync verification suite."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_sync

if __name__ == "__main__":
    unittest.main(module=test_sync, verbosity=2)
