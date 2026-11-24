from __future__ import annotations

import argparse
import logging
import os

from .config import DEFAULT_HOST, DEFAULT_PORT, database_path, data_dir
from .hook import run_hook
from .mention_replies import configured_work_project
from .store import BridgeStore


def _logging() -> None:
    try:
        log_path = data_dir() / "agentbridge.log"
        logging.basicConfig(
            filename=log_path,
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    except OSError:
        logging.basicConfig(level=logging.WARNING)


def _serve(args: argparse.Namespace) -> int:
    from .server import ServerController

    store = BridgeStore(args.db or database_path(), lock_ttl=args.lock_ttl, workspace=configured_work_project())
    controller = ServerController(store, args.host, args.port, enable_replies=True)
    controller.start()
    print(f"AgentBridge listening at http://{args.host}:{args.port}/mcp")
    print("Press Ctrl+C to stop.")
    try:
        while controller.running:
            import time
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        controller.stop()
    return 0


def _tray(args: argparse.Namespace) -> int:
    from .desktop import run_desktop

    store = BridgeStore(args.db or database_path(), lock_ttl=args.lock_ttl, workspace=configured_work_project())
    return run_desktop(store, args.host, args.port)


def main(argv: list[str] | None = None) -> int:
    _logging()
    parser = argparse.ArgumentParser(prog="AgentBridge")
    subparsers = parser.add_subparsers(dest="command")

    for command in ("serve", "tray"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--host", default=DEFAULT_HOST)
        sub.add_argument("--port", type=int, default=DEFAULT_PORT)
        sub.add_argument("--db")
        sub.add_argument("--lock-ttl", type=int, default=60)

    view_parser = subparsers.add_parser("view", help="open the desktop chat for an existing local service")
    view_parser.add_argument("--port", type=int, default=DEFAULT_PORT)

    hook_parser = subparsers.add_parser("hook", help="run a Codex or Antigravity lifecycle hook")
    hook_parser.add_argument("--agent", required=True, choices=("Codex", "Antigravity"))
    hook_parser.add_argument("--event", default="post-tool-use", choices=("post-tool-use", "pre-invocation", "session-start"))
    hook_parser.add_argument("--host", default=DEFAULT_HOST)
    hook_parser.add_argument("--port", type=int, default=DEFAULT_PORT)

    status_parser = subparsers.add_parser("status", help="print the local monitor snapshot")
    status_parser.add_argument("--db")
    status_parser.add_argument("--agent", default="Codex")

    reset_parser = subparsers.add_parser("reset", help="clear events, cursors, and file locks")
    reset_parser.add_argument("--db")

    args = parser.parse_args(argv)
    if args.command == "hook":
        return run_hook([
            "--agent", args.agent, "--event", args.event, "--host", args.host, "--port", str(args.port)
        ])
    if args.command == "serve":
        return _serve(args)
    if args.command == "tray":
        return _tray(args)
    if args.command == "view":
        from .desktop import run_viewer

        return run_viewer(args.port)
    if args.command == "status":
        import json
        store = BridgeStore(args.db or database_path())
        print(json.dumps(store.dashboard_snapshot(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "reset":
        store = BridgeStore(args.db or database_path())
        store.reset_session()
        return 0

    if os.name == "nt":
        return _tray(argparse.Namespace(db=None, lock_ttl=60, host=DEFAULT_HOST, port=DEFAULT_PORT))
    return _serve(argparse.Namespace(db=None, lock_ttl=60, host=DEFAULT_HOST, port=DEFAULT_PORT))


if __name__ == "__main__":
    raise SystemExit(main())
