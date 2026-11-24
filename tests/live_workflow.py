"""Verify a real local agent can finish a staged ticket in a temporary project."""

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
    parser.add_argument("--owner", action="store_true", help="dispatch from the boss API instead of an employee handoff")
    args = parser.parse_args()
    executable = find_runner(args.client)
    if not executable:
        parser.error(f"{args.client} CLI was not found")

    with tempfile.TemporaryDirectory(prefix="agentbridge-live-work-") as folder:
        scratch = Path(folder)
        project = scratch / "project"
        project.mkdir()
        status_file = project / "status.txt"
        status_file.write_text("draft\n", encoding="utf-8")
        os.environ.update(AGENTBRIDGE_DATA_DIR=folder, AGENTBRIDGE_WORKSPACE=str(project))
        if args.client == "codex":
            for name in tuple(os.environ):
                if name.startswith("CODEX_") and name != "CODEX_HOME":
                    os.environ.pop(name)
            os.environ["AGENTBRIDGE_CODEX_EXE"] = executable
        else:
            profile = scratch / "profile"
            profile.mkdir()
            os.environ["USERPROFILE" if os.name == "nt" else "HOME"] = str(profile)
            os.environ.update(AGENTBRIDGE_ANTIGRAVITY_EXE=executable, AGY_CLI_DISABLE_AUTO_UPDATE="true")

        port = free_port()
        if port == 8765:
            port = free_port()
        base = f"http://127.0.0.1:{port}"
        store = BridgeStore(scratch / "state.db")
        worker = store.join_group(args.client, "gpt-6" if args.client == "codex" else "gemini", "隔離驗收員工")
        lead = store.join_group("fixture", "planner", "隔離驗收出題者")
        store.company.assign_workstation("seat-engine", worker["agent_id"])
        store.company.assign_workstation("seat-cto", lead["agent_id"])
        result = store.company.roadmap.create_plan(lead["agent_id"], "示範：核對狀態檔", [
            {"title": "修正狀態", "description": "status.txt 只能有 approved 一行", "tasks": [
                {"title": "將 status.txt 的 draft 改為 approved", "department": "引擎研發", "priority": "normal"},
            ]},
            {"title": "獨立驗收", "description": "覆核狀態檔", "tasks": [
                {"title": "覆核狀態檔", "department": "QA驗收", "priority": "normal"},
            ]},
        ])
        ticket = next(task for task in store.company.list_tasks() if task["department"] == "引擎研發")
        service = ServerController(store, port=port, enable_replies=True)
        service.start()
        dispatcher = service._replies
        try:
            instructions = "僅將工作目錄內 status.txt 的 draft 改成 approved。認領工單、鎖檔、改檔、讀回驗證、解鎖並記錄完成結果。不要觸碰其他檔案。"
            if args.owner:
                path = base + "/api/tasks/" + ticket["task_id"]
                http_json(path, "PATCH", {"assignee_seat_id": "seat-engine"})
                code, dispatch = http_json(path + "/dispatch", "POST", {"instructions": instructions})
                assert code == 202
                handed_off = dispatch["status_message"]
            else:
                handed_off = call_tool(base + "/mcp", 11, "report_step", {
                    "agent_id": lead["agent_id"], "action_type": "handoff", "mention_to": "@seat-engine",
                    "summary": f"工單 {ticket['task_id']}。{instructions}",
                })
            assert "已派工" in handed_off, handed_off
            started = time.monotonic()
            while time.monotonic() - started < 150:
                state = http_json(base + "/api/state")[1]
                current = next(task for task in state["tasks"] if task["task_id"] == ticket["task_id"])
                markers = ("工作回報", "工單未完成", "無法處理交接")
                if any(any(marker in m["summary"] for marker in markers) for m in state["messages"]):
                    break
                time.sleep(0.8)
            else:
                raise TimeoutError(f"{args.client} did not finish the isolated handoff")
            assert state["active_goal"]["goal_id"] == result["active_goal"]["goal_id"]
            reports = [m["summary"] for m in state["messages"] if any(marker in m["summary"] for marker in markers)]
            assert current["status"] == "done", (current["status"], reports[-1])
            assert current["assignee_agent_id"] == worker["agent_id"]
            assert status_file.read_text(encoding="utf-8").splitlines() == ["approved"]
            assert [stage["status"] for stage in state["stages"]] == ["completed", "in_progress"]
            assert not state["locks"]
            print(f"PASS: {args.client} finished the assigned ticket and unlocked stage 2 "
                  f"in {time.monotonic() - started:.1f}s.")
        finally:
            service.stop()
            if dispatcher:
                dispatcher.pool.shutdown(wait=True)
                dispatcher.work_pool.shutdown(wait=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
