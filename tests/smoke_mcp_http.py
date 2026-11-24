"""End-to-end smoke test against the real Streamable HTTP MCP endpoint."""

from __future__ import annotations

import json
import base64
import hashlib
import os
import socket
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentbridge.server import ServerController
from agentbridge.store import BridgeStore


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request_json(url: str, payload: dict | None = None) -> dict:
    if payload is None:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
    else:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            method="POST",
        )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def call_tool(url: str, request_id: int, name: str, arguments: dict) -> str:
    result = request_json(url, {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })["result"]
    if "structuredContent" in result:
        return str(result["structuredContent"].get("result", ""))
    content = result.get("content", [])
    return "\n".join(str(part.get("text", "")) for part in content)


class LocalWebSocket:
    """Tiny RFC 6455 client for exercising the local JSON WebSocket contract."""

    def __init__(self, host: str, port: int, origin: str = "http://localhost:5173") -> None:
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.settimeout(5)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET /ws HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"Origin: {origin}\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            response += self.sock.recv(4096)
        headers = response.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        assert " 101 " in headers, headers
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        header_map = {
            line.partition(":")[0].strip().lower(): line.partition(":")[2].strip()
            for line in headers.split("\r\n")[1:]
            if ":" in line
        }
        assert header_map.get("sec-websocket-accept") == expected, headers
        self.send_text("ready")

    def _read_exact(self, count: int) -> bytes:
        data = bytearray()
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise EOFError("WebSocket closed")
            data.extend(chunk)
        return bytes(data)

    def send_text(self, text: str) -> None:
        data = text.encode("utf-8")
        mask = os.urandom(4)
        length = len(data)
        if length < 126:
            header = bytes((0x81, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytes((0x81, 0x80 | 126)) + struct.pack("!H", length)
        else:
            header = bytes((0x81, 0x80 | 127)) + struct.pack("!Q", length)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
        self.sock.sendall(header + mask + masked)

    def receive_json(self) -> dict:
        while True:
            first, second = self._read_exact(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if second & 0x80 else b""
            payload = self._read_exact(length)
            if mask:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 0x9:
                self._send_control(0xA, payload)
                continue
            if opcode == 0x8:
                raise EOFError("WebSocket closed")
            if opcode == 0x1:
                return json.loads(payload.decode("utf-8"))

    def receive_type(self, event_type: str) -> dict:
        for _ in range(20):
            event = self.receive_json()
            if event.get("type") == event_type:
                return event
        raise AssertionError(f"WebSocket event {event_type!r} not received")

    def _send_control(self, opcode: int, data: bytes = b"") -> None:
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
        self.sock.sendall(bytes((0x80 | opcode, 0x80 | len(data))) + mask + masked)

    def close(self) -> None:
        try:
            self._send_control(0x8)
        finally:
            self.sock.close()


def http_json(url: str, method: str = "GET", payload: dict | None = None, origin: str | None = None,
              headers: dict | None = None) -> tuple[int, dict]:
    headers = {"Accept": "application/json", **(headers or {})}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    if origin:
        headers["Origin"] = origin
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read().decode("utf-8")
        return response.status, (json.loads(raw) if raw else {})


def assert_web_and_api(base: str) -> None:
    with urllib.request.urlopen(base + "/", timeout=5) as response:
        html = response.read().decode("utf-8")
        assert response.status == 200 and "AgentBridge AI 軟體公司" in html
    with urllib.request.urlopen(base + "/assets/app.css", timeout=5) as response:
        assert response.status == 200 and b"--bubble-owner" in response.read()

    preflight = urllib.request.Request(
        base + "/api/state",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "content-type",
        },
        method="OPTIONS",
    )
    with urllib.request.urlopen(preflight, timeout=5) as response:
        assert response.status == 200
        assert response.headers.get("Access-Control-Allow-Origin") == "http://localhost:5173"

    status, state = http_json(base + "/api/state")
    assert status == 200
    assert {"channel_enabled", "members", "locks", "messages", "workstations", "tasks", "channels",
            "active_goal", "completed_goal", "stages"} <= state.keys()
    assert state["channel_enabled"] is True
    for route, method, payload, header in (
        ("/api/state", "GET", None, {"Host": "machine.example"}),
        ("/api/channel/toggle", "POST", {"enabled": False}, {"Origin": "https://external.example"}),
        ("/api/session/reset", "POST", {}, {"Origin": "https://external.example"}),
        ("/hooks/pre-invocation", "POST", {}, {"Origin": "https://external.example"}),
        ("/mcp", "POST", {}, {"Origin": "https://external.example"}),
    ):
        try:
            http_json(base + route, method, payload, headers=header)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403, (route, exc.code)
        else:
            raise AssertionError(f"Non-local request reached {route}")
    assert http_json(base + "/api/state")[1]["channel_enabled"] is True


def assert_group_and_websocket_contract(base: str, port: int) -> None:
    join_ws = LocalWebSocket("127.0.0.1", port)
    try:
        codex = json.loads(call_tool(base + "/mcp", 30, "join_group", {
            "client_name": "Co Dex", "model_name": "GPT 6 Luna", "role_description": "backend",
            "session_key": "smoke-codex-original",
        }))
        joined_message = join_ws.receive_type("new_message")["data"]
        assert joined_message["action_type"] == "system"
        assert "codex-gpt6luna 已到職" in joined_message["summary"]
        assert len(join_ws.receive_type("member_updated")["data"]) == 1

        codex_second = json.loads(call_tool(base + "/mcp", 31, "join_group", {
            "new_identity": True,
            "client_name": "codex", "model_name": "gpt6luna", "role_description": "reviewer",
        }))
        joined_message = join_ws.receive_type("new_message")["data"]
        assert "codex-gpt6luna-2 已到職" in joined_message["summary"]
        assert len(join_ws.receive_type("member_updated")["data"]) == 2

        antigravity = json.loads(call_tool(base + "/mcp", 32, "join_group", {
            "client_name": "Anti Gravity", "model_name": "Opus 5.5", "role_description": "frontend",
        }))
        joined_message = join_ws.receive_type("new_message")["data"]
        assert "antigravity-opus5.5 已到職" in joined_message["summary"]
        assert len(join_ws.receive_type("member_updated")["data"]) == 3
    finally:
        join_ws.close()
    assert codex["agent_name"] == "codex-gpt6luna"
    assert codex_second["agent_name"] == "codex-gpt6luna-2"
    assert antigravity["agent_name"] == "antigravity-opus5.5"
    assert codex["agent_id"] != codex_second["agent_id"]
    restored = http_json(base + "/hooks/session-identity", "POST", {
        "client": "Codex", "session_key": "smoke-codex-original",
    })[1]
    assert restored["member"]["agent_id"] == codex["agent_id"]
    assert restored["recovery_required"] is False
    lost = http_json(base + "/hooks/session-identity", "POST", {
        "client": "Codex", "session_key": "smoke-codex-unbound",
    })[1]
    assert lost == {"member": None, "recovery_required": True}
    duplicate = json.loads(call_tool(base + "/mcp", 39, "join_group", {
        "client_name": "codex-desktop-gpt-6", "model_name": "gpt-6.2-sol", "session_key": "smoke-codex-unbound",
    }))
    assert duplicate["identity_required"] and duplicate["existing_count"] == 2

    _, state = http_json(base + "/api/state")
    assert [member["agent_name"] for member in state["members"]] == [
        "codex-gpt6luna", "codex-gpt6luna-2", "antigravity-opus5.5",
    ]
    assert set(state["members"][0]) == {
        "agent_id", "agent_name", "client_name", "model_name", "role_description",
        "color", "muted", "last_seen_at", "is_admin", "capabilities",
    }
    assert call_tool(base + "/mcp", 40, "heartbeat", {"agent_id": codex_second["agent_id"]}) == ""
    bound = http_json(base + "/hooks/bind-session", "POST", {
        "agent_id": antigravity["agent_id"], "session_key": "smoke-agy-session",
    })[1]
    assert bound["bound"]
    read_only = http_json(base + "/hooks/post-tool-use", "POST", {
        "agent_id": "Antigravity", "session_key": "smoke-agy-session",
    })[1]
    assert read_only == {"recorded": False, "messages": []}
    presence = {member["agent_id"]: member["state"] for member in http_json(base + "/api/presence")[1]["members"]}
    assert presence == {codex["agent_id"]: "online", codex_second["agent_id"]: "online", antigravity["agent_id"]: "online"}
    assert http_json(base + "/api/state")[1]["messages"] == state["messages"]

    ws = LocalWebSocket("127.0.0.1", port)
    try:
        _, admin = http_json(f"{base}/api/members/{codex_second['agent_id']}/admin", "POST", {"is_admin": True})
        assert admin["member"]["is_admin"] and "member_key" not in admin["member"]
        assert any(m["is_admin"] for m in ws.receive_type("member_updated")["data"])
        _, response = http_json(base + "/api/owner/message", "POST", {
            "content": "@codex-gpt6luna Please review the current change", "mention_to": "@antigravity-opus5.5",
        })
        assert response["message"]["sender_id"] == "owner"
        pushed = ws.receive_type("new_message")["data"]
        assert pushed["summary"] == "@codex-gpt6luna Please review the current change"
        assert pushed["mention_to"] == "@antigravity-opus5.5"
        assert set(pushed) == {
            "id", "sender_id", "sender_name", "sender_role", "color", "is_owner",
            "action_type", "summary", "files_touched", "mention_to", "created_at", "channel",
        }
        _, withdrawn = http_json(f"{base}/api/messages/{pushed['id']}", "DELETE")
        assert withdrawn["message"]["removal_kind"] == "withdrawn"
        assert ws.receive_type("message_updated")["data"] == withdrawn["message"]
        assert "Please review the current change" not in json.dumps(http_json(base + "/api/state")[1])

        call_tool(base + "/mcp", 41, "report_step", {"agent_id": codex_second["agent_id"], "action_type": "edit", "summary": "fixture to retract"})
        own = ws.receive_type("new_message")["data"]
        own_id = own["id"]
        listed = json.loads(call_tool(base + "/mcp", 42, "get_my_messages", {
            "agent_id": codex_second["agent_id"], "member_key": codex_second["member_key"],
        }))
        assert listed[0]["id"] == own_id
        call_tool(base + "/mcp", 43, "withdraw_message", {
            "agent_id": codex_second["agent_id"], "member_key": codex_second["member_key"], "message_id": own_id,
        })
        retract = ws.receive_type("message_updated")["data"]
        assert retract["id"] == own_id and retract["removal_kind"] == "withdrawn"

        _, response = http_json(
            f"{base}/api/members/{codex['agent_id']}/mute", "POST", {"muted": True},
        )
        assert response["member"]["muted"] is True
        assert ws.receive_type("member_updated")["data"][0]["muted"] is True

        _, response = http_json(base + "/api/channel/toggle", "POST", {"enabled": False})
        assert response["enabled"] is False
        assert ws.receive_type("channel_status")["data"] == {"enabled": False}
        _, response = http_json(base + "/api/channel/toggle", "POST", {"enabled": True})
        assert response["enabled"] is True
        assert ws.receive_type("channel_status")["data"] == {"enabled": True}

        call_tool(base + "/mcp", 33, "lock_files", {
            "agent_id": codex["agent_id"], "files": ["src/group_shared.py"],
        })
        assert ws.receive_type("new_message")["data"]["action_type"] == "lock"
        locks = ws.receive_type("locks_updated")["data"]
        assert locks[0]["agent_name"] == "codex-gpt6luna"
        assert locks[0]["display_path"] == "src/group_shared.py"

        _, removed = http_json(f"{base}/api/members/{codex['agent_id']}", "DELETE")
        assert removed["member"]["agent_name"] == "codex-gpt6luna"
        assert ws.receive_type("new_message")["data"]["action_type"] == "system"
        assert len(ws.receive_type("member_updated")["data"]) == 2
        remaining_locks = ws.receive_type("locks_updated")["data"]
        assert all(lock["display_path"] != "src/group_shared.py" for lock in remaining_locks)

        _, response = http_json(base + "/api/session/reset", "POST", {})
        assert response["ok"] is True
        assert ws.receive_type("session_reset")["data"] == {}
        assert ws.receive_type("locks_updated")["data"] == []
        _, reset_state = http_json(base + "/api/state")
        assert reset_state["messages"] == [] and reset_state["locks"] == []
        assert len(reset_state["members"]) == 2
    finally:
        ws.close()


def assert_company_contract(base: str, port: int) -> None:
    state = http_json(base + "/api/state")[1]
    assert "總部公告" in state["channels"] and len(state["workstations"]) == 5
    mcp_url = base + "/mcp"
    agent = json.loads(call_tool(mcp_url, 100, "join_group", {
        "client": "codex", "model": "company-smoke", "capabilities": ["Python-Test"], "new_identity": True,
    }))
    assert len(agent["snapshot"]["workstations"]) == 5 and agent["workstation"] is None
    assert agent["capabilities"] == ["Python-Test"]
    agent_id = agent["agent_id"]
    ws = LocalWebSocket("127.0.0.1", port)
    try:
        payload = {"seat_id": "seat-smoke", "title": "流程測試", "department": "測試頻道", "sop_prompt": "先驗證，再提交。",
                   "capabilities": ["Python-Test"], "is_lead": True}
        code, created = http_json(base + "/api/workstations", "POST", payload)
        assert code == 201 and created["workstation"]["seat_id"] == "seat-smoke"
        assert any(seat["seat_id"] == "seat-smoke" for seat in ws.receive_type("workstations_updated")["data"])
        path = base + "/api/workstations/seat-smoke"
        headers = {"Authorization": "Bearer " + agent["member_key"], "X-AgentBridge-Agent-Id": agent_id}
        try:
            http_json(path + "/assign", "POST", {"agent_id": agent_id}, headers=headers)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        else:
            raise AssertionError("ordinary employee assigned a workstation")
        http_json(f"{base}/api/members/{agent_id}/admin", "POST", {"is_admin": True})
        _, assigned = http_json(path + "/assign", "POST", {"agent_id": agent_id}, headers=headers)
        assert assigned["workstation"]["assigned_agent_id"] == agent_id
        assert ws.receive_type("new_message")["data"]["action_type"] == "system"
        _, updated = http_json(path, "PUT", {"sop_prompt": "這是新版 SOP。"})
        assert updated["workstation"]["sop_prompt"] == "這是新版 SOP。"
        for i in range(8):
            http_json(base + "/api/owner/message", "POST", {"content": f"同步動態 {i}"})
        heartbeat = call_tool(mcp_url, 101, "heartbeat", {"agent_id": agent_id})
        assert "這是新版 SOP。" in heartbeat and "工位任職令" in heartbeat
        assert call_tool(mcp_url, 102, "heartbeat", {"agent_id": agent_id}) == ""

        code, response = http_json(base + "/api/tasks", "POST", {
            "title": "REST 驗證", "department": "測試頻道", "assignee_seat_id": "seat-smoke",
        })
        assert code == 201 and response["task"]["status"] == "todo"
        task_id = response["task"]["task_id"]
        while task_id not in {item["task_id"] for item in ws.receive_type("tasks_updated")["data"]}:
            pass
        claimed = json.loads(call_tool(mcp_url, 103, "claim_task", {"agent_id": agent_id, "task_id": task_id}))
        assert claimed["status"] == "in_progress" and claimed["assignee_agent_id"] == agent_id
        _, review = http_json(f"{base}/api/tasks/{task_id}", "PATCH", {"status": "review"})
        assert review["task"]["status"] == "review"
        finished = json.loads(call_tool(mcp_url, 104, "complete_task", {
            "agent_id": agent_id, "task_id": task_id, "result_summary": "驗收完成",
        }))
        assert finished["status"] == "done"
        assert json.loads(call_tool(mcp_url, 105, "list_tasks", {"status": "done"}))["tasks"][0]["task_id"] == task_id
        snapshot = json.loads(call_tool(mcp_url, 106, "get_company_snapshot", {"agent_id": agent_id}))
        assert snapshot["workstation"]["sop_prompt"] == "這是新版 SOP。" and snapshot["tasks"] == []
        member = json.loads(call_tool(mcp_url, 107, "assign_workstation", {
            "agent_id": agent_id, "member_key": agent["member_key"], "seat_id": "seat-smoke", "assignee_agent_id": None,
        }))
        assert member["assigned_agent_id"] is None and "待命區" in member["unread"][0]
        assert http_json(path, "DELETE")[1] == {"ok": True}
    finally:
        ws.close()


def assert_roadmap_contract(base: str, port: int) -> None:
    mcp = base + "/mcp"
    staff = {
        seat: json.loads(call_tool(mcp, 120 + i, "join_group", {
            "client": "antigravity" if seat == "seat-qa" else "codex", "model": model,
            "new_identity": True,
        }))
        for i, (seat, model) in enumerate([("seat-cto", "planner"), ("seat-engine", "engineer"), ("seat-qa", "qa")])
    }
    for seat, member in staff.items():
        http_json(f"{base}/api/workstations/{seat}/assign", "POST", {"agent_id": member["agent_id"]})
    ws = LocalWebSocket("127.0.0.1", port)
    try:
        code, result = http_json(base + "/api/goals", "POST", {"title": "版本驗收", "description": "完成流程驗收"})
        goal = result["goal"]
        assert code == 201 and goal["status"] == "active"
        retry = http_json(f"{base}/api/goals/{goal['goal_id']}/retry", "POST", {})[1]
        assert retry["planning"] == {"queued": False, "reason": "planner_unavailable"}
        assert ws.receive_type("stages_updated")["data"]["active_goal"] == goal
        planner, engineer, qa = (staff[seat]["agent_id"] for seat in ("seat-cto", "seat-engine", "seat-qa"))
        assert goal["goal_id"] in call_tool(mcp, 123, "heartbeat", {"agent_id": planner})
        phases = [
            {"title": title, "description": "通過驗收", "tasks": [{"title": title, "department": dept, "priority": "normal"}]}
            for title, dept in [("確認引擎", "引擎研發"), ("驗收版本", "QA驗收")]
        ]
        proposed = {"agent_id": qa, "goal_id": goal["goal_id"], "goal_title": goal["title"], "stages": phases}
        assert "只有主管工位" in call_tool(mcp, 124, "create_staged_plan", proposed)
        proposed["agent_id"] = planner
        first, second = json.loads(call_tool(mcp, 125, "create_staged_plan", proposed))["stages"]
        assert first["status"] == "in_progress" and second["status"] == "pending"
        try:
            http_json(f"{base}/api/goals/{goal['goal_id']}/retry", "POST", {})
        except urllib.error.HTTPError as exc:
            assert exc.code == 409
        else:
            raise AssertionError("已提交的規劃不可重試")
        assert ws.receive_type("stages_updated")["data"]["stages"][1]["status"] == "pending"
        task_a = first["departments"][0]["tasks"][0]["task_id"]
        task_b = second["departments"][0]["tasks"][0]["task_id"]
        http_json(f"{base}/api/tasks/{task_b}", "PATCH", {"assignee_seat_id": "seat-qa"})
        assert ws.receive_type("stages_updated")["data"]["stages"][1]["status"] == "pending"
        try:
            http_json(f"{base}/api/tasks/{task_b}/dispatch", "POST", {})
        except urllib.error.HTTPError as exc:
            assert exc.code == 409 and "尚未解鎖" in json.loads(exc.read())["error"]
        else:
            raise AssertionError("尚未解鎖的工單不可派工")
        http_json(f"{base}/api/tasks/{task_a}", "PATCH", {"assignee_seat_id": "seat-engine"})
        assert ws.receive_type("stages_updated")["data"]["stages"][0]["progress_percent"] == 0
        code, dispatch = http_json(f"{base}/api/tasks/{task_a}/dispatch", "POST", {"instructions": "驗證引擎設定"})
        assert code == 202 and dispatch["message"]["is_owner"]
        assert dispatch["message"]["action_type"] == "handoff"
        assert dispatch["message"]["mention_to"] == "@" + staff["seat-engine"]["agent_name"]
        assert task_a in dispatch["message"]["summary"]
        # Reserve/dispatch do not claim the ticket. The employee still owns the state transition.
        assert next(task["status"] for task in http_json(base + "/api/state")[1]["tasks"]
                    if task["task_id"] == task_a) == "todo"
        assert "尚未解鎖" in call_tool(mcp, 126, "claim_task", {"agent_id": qa, "task_id": task_b})
        assert json.loads(call_tool(mcp, 127, "claim_task", {"agent_id": engineer, "task_id": task_a}))["status"] == "in_progress"
        started = ws.receive_type("stages_updated")["data"]["stages"][0]
        assert started["progress_percent"] == 30
        assert started["departments"][0]["tasks"][0]["assignee_name"] == staff["seat-engine"]["agent_name"]
        assert json.loads(call_tool(mcp, 128, "complete_task", {
            "agent_id": engineer, "task_id": task_a, "result_summary": "已完成驗收",
        }))["status"] == "done"
        updated = ws.receive_type("stages_updated")["data"]["stages"]
        assert [stage["status"] for stage in updated] == ["completed", "in_progress"]
        assert updated[0]["progress_percent"] == 100
        http_json(base + "/hooks/bind-session", "POST", {"agent_id": qa, "session_key": "roadmap-agy-session"})
        hook = {"agent_id": "Antigravity", "session_key": "roadmap-agy-session"}
        posted = http_json(base + "/hooks/post-tool-use", "POST", {**hook, "action_type": "test", "summary": "測試就緒"})[1]
        assert posted == {"recorded": True, "messages": []}
        assert "已解鎖" in " ".join(http_json(base + "/hooks/pre-invocation", "POST", hook)[1]["messages"])
        assert json.loads(call_tool(mcp, 130, "claim_task", {"agent_id": qa, "task_id": task_b}))["status"] == "in_progress"
    finally:
        ws.close()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="agentbridge-smoke-") as folder:
        store = BridgeStore(Path(folder) / "state.sqlite3")
        port = free_port()
        controller = ServerController(store, port=port)
        with socket.create_server(("127.0.0.1", port)):
            try:
                controller.start()
            except RuntimeError as error:
                assert "連接埠已被使用" in str(error), error
                assert not controller.running
            else:
                controller.stop()
                raise AssertionError("MCP started on a port already in use")
        controller.start()
        base = f"http://127.0.0.1:{port}"
        try:
            health = request_json(base + "/health")
            assert health["ok"] is True
            assert_web_and_api(base)

            init = request_json(base + "/mcp", {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "AgentBridge smoke", "version": "0.1"},
                },
            })
            assert "tools" in init["result"]["capabilities"]

            first = call_tool(base + "/mcp", 2, "report_step", {
                "agent_id": "Codex", "action_type": "edit", "summary": "updated parser",
                "files_touched": ["src/parser.py"],
            })
            assert first == ""
            piggyback = call_tool(base + "/mcp", 3, "report_step", {
                "agent_id": "Antigravity", "action_type": "test", "summary": "tests passed",
            })
            assert "Codex edit src/parser.py updated parser" in piggyback
            empty = call_tool(base + "/mcp", 4, "report_step", {
                "agent_id": "Antigravity", "action_type": "command", "summary": "lint passed",
            })
            assert empty == ""

            call_tool(base + "/mcp", 5, "lock_files", {
                "agent_id": "Codex", "files": ["src/shared.py"],
            })
            call_tool(base + "/mcp", 6, "report_step", {
                "agent_id": "Codex", "action_type": "edit", "summary": "updating shared module",
                "files_touched": ["src/shared.py"],
            })
            conflict = call_tool(base + "/mcp", 7, "lock_files", {
                "agent_id": "Antigravity", "files": ["src/shared.py"],
            })
            assert "LOCK src/shared.py: Codex" in conflict
            assert "updating shared module" in conflict

            call_tool(base + "/mcp", 8, "report_step", {
                "agent_id": "Codex", "action_type": "edit", "summary": "peer update for hook",
                "files_touched": ["src/hook_peer.py"],
            })
            project_root = Path(__file__).resolve().parents[1]
            hook_script = project_root / "hooks" / "post_tool_use.py"
            pre_hook = subprocess.run(
                [sys.executable, str(hook_script), "--agent", "Antigravity", "--event", "pre-invocation", "--port", str(port)],
                input="{}", text=True, capture_output=True, cwd=project_root, timeout=8,
            )
            assert pre_hook.returncode == 0, pre_hook.stderr
            assert "peer update for hook" in json.loads(pre_hook.stdout)["injectSteps"][0]["systemMessage"]["systemMessage"]
            post_hook = subprocess.run(
                [sys.executable, str(hook_script), "--agent", "Antigravity", "--port", str(port)],
                input=json.dumps({
                    "toolCall": {"name": "write_file", "args": {"FilePath": "src/from_hook.py"}},
                    "stepIdx": 12,
                }),
                text=True, capture_output=True, cwd=project_root, timeout=8,
            )
            assert post_hook.returncode == 0, post_hook.stderr
            assert post_hook.stdout == "{}"
            assert any("src/from_hook.py" in item["line"] for item in store.dashboard_snapshot()["events"])

            codex_hook = subprocess.run(
                [sys.executable, str(hook_script), "--agent", "Codex", "--port", str(port)],
                input=json.dumps({
                    "hook_event_name": "PostToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": "pytest -q"},
                    "tool_response": {"exit_code": 0, "stdout": "must not be forwarded"},
                }),
                text=True, capture_output=True, cwd=project_root, timeout=8,
            )
            assert codex_hook.returncode == 0, codex_hook.stderr
            codex_context = json.loads(codex_hook.stdout)["hookSpecificOutput"]["additionalContext"]
            assert "src/from_hook.py" in codex_context
            assert "must not be forwarded" not in codex_context

            store.set_channel_enabled(False)
            muted = call_tool(base + "/mcp", 11, "report_step", {
                "agent_id": "Codex", "action_type": "edit", "summary": "muted edit",
                "files_touched": ["src/muted.py"],
            })
            assert muted == ""
            store.set_channel_enabled(True)
            resumed = call_tool(base + "/mcp", 12, "get_sync_status", {"agent_id": "Antigravity"})
            assert "muted edit" in resumed

            assert_group_and_websocket_contract(base, port)
            assert_company_contract(base, port)
            assert_roadmap_contract(base, port)

            print("PASS: HTTP MCP, REST/static/CORS, WebSocket, legacy tools, staged plans, progress, gate, and hooks")
            return 0
        finally:
            controller.stop()


if __name__ == "__main__":
    raise SystemExit(main())
