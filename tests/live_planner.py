"""Optional smoke test that consumes one real model turn on a temporary loopback server."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from smoke_mcp_http import call_tool, free_port, http_json

from agentbridge.mention_replies import find_runner
from agentbridge.server import ServerController
from agentbridge.store import BridgeStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client", choices=("codex", "antigravity"))
    args = parser.parse_args()
    executable = find_runner(args.client)
    if not executable:
        parser.error(f"{args.client} CLI was not found")

    with tempfile.TemporaryDirectory(prefix="agentbridge-live-planner-") as scratch:
        scratch = Path(scratch)
        os.environ["AGENTBRIDGE_DATA_DIR"] = str(scratch)
        os.environ.pop("AGENTBRIDGE_WORKSPACE", None)
        if args.client == "codex":
            for name in tuple(os.environ):
                if name.startswith("CODEX_") and name != "CODEX_HOME":
                    os.environ.pop(name)
            os.environ["AGENTBRIDGE_CODEX_EXE"] = executable
        else:
            profile = scratch / "profile"
            profile.mkdir()
            # Use the OS credential store without loading the user's hooks or MCP servers.
            os.environ["USERPROFILE" if os.name == "nt" else "HOME"] = str(profile)
            os.environ["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
            os.environ["AGENTBRIDGE_ANTIGRAVITY_EXE"] = executable

        port = free_port()
        if port == 8765:
            port = free_port()
        origin = f"http://127.0.0.1:{port}"
        service = ServerController(BridgeStore(scratch / "test.db"), port=port, enable_replies=True)
        service.start()
        dispatcher = service._replies
        try:
            member = json.loads(call_tool(origin + "/mcp", 1, "join_group", {
                "client": args.client, "model": "gpt-6" if args.client == "codex" else "gemini",
                "session_key": "isolated-planner", "role_description": "隔離驗收規劃者",
            }))
            http_json(origin + "/api/workstations/seat-cto/assign", "POST", {"agent_id": member["agent_id"]})
            status, published = http_json(origin + "/api/goals", "POST", {
                "title": "示範：驗收設定頁的鍵盤操作",
                "description": "此隔離測試只建立規劃。拆成兩個階段，先交介面設計，再交 QA驗收。"
                               "兩部門各有至少一張工單與具體驗收條件。",
            })
            if status != 201 or not published["planning"]["queued"]:
                raise RuntimeError(f"規劃者無法啟動：{published['planning']}")
            started = time.monotonic()
            while time.monotonic() - started < 130:
                state = http_json(origin + "/api/state")[1]
                stages = state["stages"]
                if stages:
                    break
                if (state.get("planning") or {}).get("status") == "failed":
                    raise RuntimeError(state["planning"]["detail"])
                time.sleep(1)
            else:
                raise TimeoutError(f"{args.client} did not submit a plan")
            assert state["active_goal"]["goal_id"] == published["goal"]["goal_id"]
            assert [stage["status"] for stage in stages] == ["in_progress", "pending"]
            assert {task["department"] for task in state["tasks"]} == {"介面設計", "QA驗收"}
            assert all(task["status"] == "todo" for task in state["tasks"])
            print(f"PASS: {args.client} submitted {len(stages)} stages and {len(state['tasks'])} tickets "
                  f"in {time.monotonic() - started:.1f}s.")
        finally:
            service.stop()
            if dispatcher:
                dispatcher.pool.shutdown(wait=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
