from __future__ import annotations

import os
import sys
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_LOCK_TTL = 60
MAX_EVENT_CHARS = 150
MAX_UNREAD_EVENTS = 5
MAX_ACTIVE_EVENTS = 500


def data_dir() -> Path:
    configured = os.environ.get("AGENTBRIDGE_DATA_DIR")
    if configured:
        root = Path(configured).expanduser()
    elif os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "AgentBridge"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "agentbridge"
    try:
        root.mkdir(parents=True, exist_ok=True)
        return root
    except OSError:
        # A damaged or restricted LocalAppData ACL should not prevent the UI
        # from opening. Keep an explicit AGENTBRIDGE_DATA_DIR choice strict.
        if configured or os.name != "nt":
            raise

    profile = Path(os.environ.get("USERPROFILE") or Path.home()).expanduser()
    fallback_candidates = [profile / "AgentBridgeData"]
    if getattr(sys, "frozen", False):
        fallback_candidates.append(Path(sys.executable).resolve().parent / "AgentBridgeData")
    last_error: OSError | None = None
    for fallback in fallback_candidates:
        try:
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback
        except OSError as exc:
            last_error = exc
    raise OSError(
        f"AgentBridge cannot write to {root} or its user-profile fallback"
    ) from last_error


def database_path() -> Path:
    configured = os.environ.get("AGENTBRIDGE_DB")
    if configured:
        path = Path(configured).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return data_dir() / "agentbridge.sqlite3"


def web_directory() -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root) / "web"
    return Path(__file__).resolve().parents[1] / "web"


def app_icon_path() -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root) / "assets" / "agentbridge.ico"
    return Path(__file__).resolve().parents[1] / "assets" / "agentbridge.ico"


def app_logo_path() -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root) / "assets" / "agentbridge.png"
    return Path(__file__).resolve().parents[1] / "assets" / "agentbridge.png"
