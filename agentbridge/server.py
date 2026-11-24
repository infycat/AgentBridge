from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
from functools import wraps
from inspect import signature
from ipaddress import ip_address
from typing import Any, Literal

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect
from typing_extensions import TypedDict

from .company import TaskConflict
from .config import DEFAULT_HOST, DEFAULT_PORT, web_directory
from .mention_replies import MentionReplies, configured_work_project
from .store import BridgeStore

logger = logging.getLogger("agentbridge.server")
_LOCAL_ORIGIN = r"https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?"

class PlannedTask(TypedDict):
    title: str
    department: str
    priority: str


class PlannedStage(TypedDict):
    title: str
    description: str
    tasks: list[PlannedTask]


def _local_management_request(request: Request) -> bool:
    return _local_connection(request.scope)


def _local_connection(scope) -> bool:
    try:
        peer = scope.get("client")
        local_peer = peer is not None and ip_address(peer[0]).is_loopback
    except (TypeError, ValueError):
        local_peer = False
    headers = {name.lower(): value.decode("latin-1") for name, value in scope.get("headers", [])}
    origin = headers.get(b"origin")
    host = headers.get(b"host", "")
    return bool(local_peer and re.fullmatch(_LOCAL_ORIGIN, f"http://{host}", re.I)
                and (origin is None or re.fullmatch(_LOCAL_ORIGIN, origin, re.I)))


class LoopbackOnly:
    """Keep the trusted local dashboard, hooks, and MCP at the same boundary."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not _local_connection(scope):
            await JSONResponse({"error": "AgentBridge 僅接受本機連線"}, status_code=403)(scope, receive, send)
        elif scope["type"] == "websocket" and not _local_connection(scope):
            await send({"type": "websocket.close", "code": 1008})
        else:
            await self.app(scope, receive, send)


def _messages_text(messages: list[str]) -> str:
    return "\n".join(messages)


class BroadcastHub:
    """Fan out committed state changes to local dashboard WebSocket clients."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._state_lock = threading.Lock()
        self._send_lock: asyncio.Lock | None = None

    def bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        with self._state_lock:
            if self._loop is not loop:
                self._clients.clear()
                self._send_lock = None
                self._loop = loop

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    def publish(self, event_type: str, data: Any) -> None:
        with self._state_lock:
            loop = self._loop
        if loop is None or not loop.is_running():
            return

        def schedule() -> None:
            with self._state_lock:
                if self._loop is not loop:
                    return
            asyncio.create_task(self._broadcast({"type": event_type, "data": data}))

        try:
            loop.call_soon_threadsafe(schedule)
        except RuntimeError:
            if not loop.is_closed():
                raise

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        async with self._send_lock:
            disconnected: list[WebSocket] = []
            for client in tuple(self._clients):
                try:
                    await client.send_json(payload)
                except Exception:
                    disconnected.append(client)
            for client in disconnected:
                self._clients.discard(client)


def build_mcp(store: BridgeStore, hub: BroadcastHub | None = None, replies: MentionReplies | None = None) -> MCPServer:
    server = MCPServer(
        name="AgentBridge",
        version="0.1.0",
        instructions=(
            "已有 AgentBridge 身分請沿用 agent_id。首次才呼叫 join_group 並帶上 Hook 提供的 session_key。"
            "換工作階段可用原 agent_id 與 member_key 恢復。保存金鑰，勿貼到聊天室。"
            "有任職令時按最新工位 SOP 執行。先 claim_task 再動手，完成後 complete_task。"
            "規劃者收到目標後先呼叫 create_staged_plan。員工只認領當前階段自己部門的工單。"
            "以繁體中文回報實際產出或阻塞，略過唯讀操作、寒暄與重複回報。"
            "用 handoff 與 mention_to 精確指派 @員工或 @seat-id。改檔前 lock_files，完成後 unlock_files。"
            "已知 agent_id 的呼叫可收到任職令。一般同步至多五筆增量摘要，不帶完整差異。"
        ),
    )

    def synced(*, json_result=False, unread=False, roadmap=True):
        def decorate(tool):
            parameters = signature(tool)

            @wraps(tool)
            def run(*args, **kwargs):
                identity = parameters.bind(*args, **kwargs).arguments.get("agent_id")
                try:
                    result = tool(*args, **kwargs)
                except (KeyError, ValueError, PermissionError) as exc:
                    try:
                        orders = store.tool_sync(identity) if identity else []
                    except (ValueError, PermissionError):
                        orders = []
                    message = str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)
                    raise ToolError(_messages_text([message, *orders])) from exc
                updates = store.tool_sync(identity, unread=unread, roadmap=roadmap) if identity else []
                if not updates:
                    return result
                if json_result:
                    data = json.loads(result)
                    if isinstance(data, list):
                        data = {"messages": data}
                    data["unread"] = updates + data.get("unread", [])
                    return _json(data)
                return _messages_text([result, *updates] if result else updates)

            return run
        return decorate

    def _json(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def management_identity(agent_id, member_key):
        if agent_id != "owner":
            return agent_id
        boss_key = os.environ.get("AGENTBRIDGE_BOSS_KEY")
        if not boss_key or member_key is None or not secrets.compare_digest(boss_key.encode(), member_key.encode()):
            raise PermissionError("請使用本機老闆介面或提供老闆金鑰")
        return None

    def dispatch_planning(goal=None):
        if replies is None:
            return {"queued": False, "reason": "planner_unavailable"}
        if goal is None:
            goal = store.company.roadmap.state(grouped=False)["active_goal"]
        return replies.submit_planning(goal) if goal else {"queued": False}

    def work_feedback(dispatch):
        if dispatch.get("replying"):
            return "已派工給 " + "、".join(dispatch["replying"])
        if dispatch.get("busy"):
            detail = "、".join(dispatch["busy"]) + " 正在工作"
        elif dispatch.get("blocked"):
            detail = "、".join(f"{name}（{reason}）" for name, reason in dispatch["blocked"].items())
        else:
            detail = {
                "channel_off": "同步通道已關閉",
                "project_not_configured": "未設定工作目錄",
                "ambiguous_ticket": "每次交接請指定一張工單及一名員工",
            }.get(dispatch.get("reason"), "目前沒有可啟動的員工")
        return "交接已記錄，未自動開工。" + detail

    @server.tool()
    def join_group(
        client_name: str | None = None, model_name: str | None = None, role_description: str = "",
        capabilities: list[str] | None = None, client: str | None = None, model: str | None = None,
        session_key: str | None = None, agent_id: str | None = None, member_key: str | None = None,
        new_identity: bool = False,
    ) -> str:
        """Check in with the Hook session_key. Recover an existing employee with agent_id/member_key even after switching models. Set new_identity only when the Boss requests a separate employee."""
        if not (client_name or client) or not (model_name or model):
            raise ToolError("請提供平台與模型名稱")
        try:
            return _json(store.join_group(
                client_name or client, model_name or model, role_description, capabilities,
                session_key=session_key, agent_id=agent_id, member_key=member_key, new_identity=new_identity,
            ))
        except (ValueError, PermissionError) as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    @synced(roadmap=False)
    def report_step(
        agent_id: str,
        action_type: str,
        summary: str,
        files_touched: list[str] | None = None,
        mention_to: str | None = None,
        channel: str | None = None,
    ) -> str:
        """Record a write-type step, optionally mention a member, and append unread peer updates."""
        result = store.report_step(agent_id, action_type, summary, files_touched, mention_to, channel)
        if not result["recorded"]:
            return _messages_text(store.tool_sync(agent_id))
        messages = list(result["messages"])
        if replies is not None and result["recorded"] and mention_to:
            origin = store.get_message(result["event_id"])
            work = origin["action_type"] == "handoff"
            dispatch = replies.submit(origin, work=work)
            if work:
                messages.insert(0, work_feedback(dispatch))
            elif dispatch.get("replying"):
                messages.insert(0, "已請以下員工回覆 " + "、".join(dispatch["replying"]))
            if dispatch.get("unavailable"):
                messages.insert(1, "未能啟動：" + "、".join(dispatch["unavailable"]))
        return _messages_text(messages)

    @server.tool()
    @synced(roadmap=False)
    def lock_files(agent_id: str, files: list[str]) -> str:
        """Acquire 60-second soft locks. Conflicts include the owner's latest progress."""
        result = store.lock_files(agent_id, files)
        lines: list[str] = []
        for conflict in result["conflicts"]:
            lines.append(
                f"⚠ LOCK {conflict['file']}: {conflict['agent_name']} — {conflict['progress']}"
            )
        lines.extend(result["messages"])
        return "\n".join(lines)

    @server.tool()
    @synced(roadmap=False)
    def unlock_files(agent_id: str, files: list[str]) -> str:
        """Release this agent's locks and append up to five unread peer updates."""
        result = store.unlock_files(agent_id, files)
        return _messages_text(result["messages"])

    @server.tool()
    @synced(json_result=True, roadmap=False)
    def get_sync_status(agent_id: str) -> str:
        """Read the latest project snapshot, active file locks, and unread peer updates."""
        result = store.get_sync_status(agent_id)
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    @server.tool()
    @synced()
    def heartbeat(agent_id: str, working: bool = False) -> str:
        """Confirm presence and receive any pending workstation appointment."""
        store.heartbeat(agent_id, working)
        return ""

    @server.tool()
    @synced(json_result=True)
    def get_my_messages(agent_id: str, member_key: str, limit: int = 10) -> str:
        """List IDs for your recent messages. Keep member_key out of chat and tool reports."""
        return json.dumps(store.member_messages(agent_id, member_key, limit), ensure_ascii=False, separators=(",", ":"))

    @server.tool()
    @synced()
    def withdraw_message(agent_id: str, member_key: str, message_id: int) -> str:
        """Withdraw a message you authored, so the group sees a withdrawal notice."""
        store.remove_message(message_id, actor=(agent_id, member_key), own_only=True)
        return "訊息已撤回"

    @server.tool()
    @synced()
    def delete_group_message(agent_id: str, member_key: str, message_id: int) -> str:
        """As a group admin, delete a message from an ordinary member."""
        message = store.remove_message(message_id, actor=(agent_id, member_key))
        return message["summary"]

    @server.tool()
    @synced()
    def moderate_group_member(agent_id: str, member_key: str, target_agent_id: str,
                              operation: Literal["mute", "unmute", "remove"]) -> str:
        """As a group admin, mute, unmute, or remove an ordinary member."""
        actor = (agent_id, member_key)
        if operation == "remove":
            result = store.remove_member(target_agent_id, actor=actor)
        else:
            result = store.set_member_muted(target_agent_id, operation == "mute", actor=actor)
        if result is None:
            raise KeyError("找不到這位員工")
        return "管理操作已完成"

    @server.tool()
    @synced(roadmap=False)
    def update_handoff(agent_id: str, status_summary: str, next_steps: str) -> str:
        """Overwrite the shared status snapshot and publish a compact handoff event."""
        result = store.update_handoff(agent_id, status_summary, next_steps)
        return _messages_text(result["messages"])

    @server.tool()
    @synced(json_result=True, unread=True)
    def assign_workstation(agent_id: str, member_key: str, seat_id: str, assignee_agent_id: str | None = None) -> str:
        """Assign or vacate a workstation as a delegated company admin. Keep member_key private."""
        seat = store.company.assign_workstation(seat_id, assignee_agent_id, actor=(agent_id, member_key))
        if seat["is_lead"] and seat["assigned_agent_id"]:
            dispatch_planning()
        return _json(seat)

    @server.tool()
    @synced(json_result=True, unread=True)
    def get_company_snapshot(agent_id: str) -> str:
        """Read the goal, ordered stage progress, workstation, prioritized tickets, and file locks."""
        return _json(store.company.snapshot(agent_id))

    @server.tool()
    @synced(json_result=True, unread=True)
    def create_staged_plan(
        agent_id: str, goal_title: str, stages: list[PlannedStage],
        goal_id: str | None = None, member_key: str | None = None,
    ) -> str:
        """As a lead or authenticated boss, publish ordered stages and tickets for a goal in one operation."""
        return _json(store.company.roadmap.create_plan(
            management_identity(agent_id, member_key), goal_title, stages, goal_id=goal_id,
        ))

    @server.tool()
    @synced(json_result=True, unread=True)
    def create_task(
        agent_id: str, title: str, department: str, priority: str = "normal", member_key: str | None = None,
        stage_id: str | None = None,
    ) -> str:
        """Create a ticket as a lead employee. Boss MCP calls require the configured AGENTBRIDGE_BOSS_KEY."""
        return _json(store.company.create_task(
            title, department, priority, identity=management_identity(agent_id, member_key), stage_id=stage_id,
        ))

    @server.tool()
    @synced(json_result=True, unread=True)
    def claim_task(agent_id: str, task_id: str) -> str:
        """Exclusively claim a todo ticket before starting work. An already held ticket returns its current assignee."""
        return _json(store.company.claim_task(agent_id, task_id))

    @server.tool()
    @synced(json_result=True, unread=True)
    def complete_task(agent_id: str, task_id: str, result_summary: str) -> str:
        """Record a concise result and finish a ticket you own."""
        return _json(store.company.complete_task(agent_id, task_id, result_summary))

    @server.tool()
    @synced(json_result=True, unread=True)
    def list_tasks(department: str | None = None, status: str | None = None, agent_id: str | None = None) -> str:
        """List tickets with optional filters. Pass agent_id to receive pending department updates and appointments."""
        return _json({"tasks": store.company.list_tasks(department, status)})

    @server.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "channel_enabled": store.channel_enabled})

    @server.custom_route("/bridge/dashboard", methods=["GET"])
    async def dashboard(_request: Request) -> JSONResponse:
        return JSONResponse(store.dashboard_snapshot())

    @server.custom_route("/api/state", methods=["GET"])
    async def api_state(_request: Request) -> JSONResponse:
        if hub:
            hub.bind_loop()
        return JSONResponse(store.get_group_state())

    @server.custom_route("/api/presence", methods=["GET"])
    async def api_presence(_request: Request) -> JSONResponse:
        active = replies.active_members() if replies is not None else None
        return JSONResponse({"members": store.get_presence(active)}, headers={"Cache-Control": "no-store"})

    @server.custom_route("/api/messages/{message_id:int}", methods=["DELETE"])
    async def api_remove_message(request: Request) -> JSONResponse:
        if not _local_management_request(request):
            return JSONResponse({"error": "老闆管理限本機聊天室"}, status_code=403)
        if hub:
            hub.bind_loop()
        try:
            message = store.remove_message(request.path_params["message_id"])
        except KeyError:
            return JSONResponse({"error": "找不到這則訊息"}, status_code=404)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        return JSONResponse({"message": message})

    @server.custom_route("/api/members/{agent_id}/admin", methods=["POST"])
    async def api_member_admin(request: Request) -> JSONResponse:
        if not _local_management_request(request):
            return JSONResponse({"error": "老闆管理限本機聊天室"}, status_code=403)
        try:
            payload = await request.json()
        except ValueError:
            payload = None
        enabled = payload.get("is_admin") if isinstance(payload, dict) else None
        if not isinstance(enabled, bool):
            return JSONResponse({"error": "請提供布林值 is_admin"}, status_code=400)
        if hub:
            hub.bind_loop()
        member = store.set_member_admin(request.path_params["agent_id"], enabled)
        if member is None:
            return JSONResponse({"error": "找不到這位員工"}, status_code=404)
        return JSONResponse({"member": member})

    @server.custom_route("/api/members/{agent_id}/key", methods=["POST"])
    async def api_member_key(request: Request) -> JSONResponse:
        if not _local_management_request(request):
            return JSONResponse({"error": "老闆管理限本機聊天室"}, status_code=403)
        key = store.issue_member_key(request.path_params["agent_id"])
        if key is None:
            return JSONResponse({"error": "找不到這位員工"}, status_code=404)
        return JSONResponse({"member_key": key}, headers={"Cache-Control": "no-store"})

    @server.custom_route("/api/owner/message", methods=["POST"])
    async def api_owner_message(request: Request) -> JSONResponse:
        if not _local_management_request(request):
            return JSONResponse({"error": "老闆指令限本機聊天室"}, status_code=403)
        if hub:
            hub.bind_loop()
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("JSON object required")
            content = payload.get("content")
            mention_to = payload.get("mention_to")
            if not isinstance(content, str):
                raise ValueError("content must be a string")
            if mention_to is not None and not isinstance(mention_to, str):
                raise ValueError("mention_to must be a string or null")
            message = store.owner_message(content, mention_to, payload.get("channel"))
            dispatch = replies.submit(message) if replies is not None else {}
            return JSONResponse({"message": message, "auto_reply": dispatch}, status_code=201)
        except (ValueError, TypeError, KeyError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def company_mutation(request, operation, *, status=200):
        if not _local_management_request(request):
            return JSONResponse({"error": "老闆管理限本機聊天室"}, status_code=403)
        if hub:
            hub.bind_loop()
        try:
            payload = {} if request.method == "DELETE" else await request.json()
            if not isinstance(payload, dict):
                raise ValueError("請提供 JSON 物件")
            return JSONResponse(operation(payload), status_code=status)
        except KeyError as exc:
            return JSONResponse({"error": exc.args[0]}, status_code=404)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except TaskConflict as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @server.custom_route("/api/goals", methods=["POST"])
    async def api_create_goal(request: Request) -> JSONResponse:
        def create(payload):
            goal = store.company.roadmap.create_goal(payload.get("title"), payload.get("description", ""))
            return {"goal": goal, "planning": dispatch_planning(goal)}

        return await company_mutation(request, create, status=201)

    @server.custom_route("/api/goals/{goal_id}/retry", methods=["POST"])
    async def api_retry_goal(request: Request) -> JSONResponse:
        def retry(_payload):
            state = store.company.roadmap.state(grouped=False)
            goal = state["active_goal"]
            if not goal or goal["goal_id"] != request.path_params["goal_id"]:
                raise KeyError("找不到目前的總目標")
            if state["stages"]:
                raise TaskConflict("此目標已有階段規劃")
            return {"planning": dispatch_planning(goal)}

        return await company_mutation(request, retry)

    @server.custom_route("/api/workstations", methods=["POST"])
    async def api_create_workstation(request: Request) -> JSONResponse:
        return await company_mutation(request, lambda p: {"workstation": store.company.save_workstation(p)}, status=201)

    @server.custom_route("/api/workstations/{seat_id}", methods=["PUT", "DELETE"])
    async def api_workstation(request: Request) -> JSONResponse:
        seat_id = request.path_params["seat_id"]

        def change(payload):
            if request.method == "DELETE":
                store.company.delete_workstation(seat_id)
                return {"ok": True}
            return {"workstation": store.company.save_workstation(payload, seat_id)}

        return await company_mutation(request, change)

    @server.custom_route("/api/workstations/{seat_id}/assign", methods=["POST"])
    async def api_assign_workstation(request: Request) -> JSONResponse:
        def assign(payload):
            if "agent_id" not in payload or not isinstance(payload["agent_id"], (str, type(None))):
                raise ValueError("請提供員工 agent_id 或 null")
            actor_id = request.headers.get("X-AgentBridge-Agent-Id")
            authorization = request.headers.get("Authorization")
            actor = None
            if actor_id is not None or authorization is not None:
                scheme, _, key = (authorization or "").partition(" ")
                if not actor_id or scheme.lower() != "bearer" or not key:
                    raise PermissionError("管理員操作需要員工 ID 與有效金鑰")
                actor = (actor_id, key)
            seat = store.company.assign_workstation(
                request.path_params["seat_id"], payload["agent_id"], actor=actor,
            )
            if seat["is_lead"] and seat["assigned_agent_id"]:
                dispatch_planning()
            return {"workstation": seat}

        return await company_mutation(request, assign)

    @server.custom_route("/api/tasks", methods=["POST"])
    async def api_create_task(request: Request) -> JSONResponse:
        return await company_mutation(request, lambda p: {"task": store.company.create_task(
            p.get("title"), p.get("department"), p.get("priority", "normal"), assignee_seat_id=p.get("assignee_seat_id"),
            stage_id=p.get("stage_id"),
        )}, status=201)

    @server.custom_route("/api/tasks/{task_id}", methods=["PATCH"])
    async def api_update_task(request: Request) -> JSONResponse:
        return await company_mutation(request, lambda p: {"task": store.company.update_task(request.path_params["task_id"], p)})

    @server.custom_route("/api/tasks/{task_id}/dispatch", methods=["POST"])
    async def api_dispatch_task(request: Request) -> JSONResponse:
        def assign_work(payload):
            task, employee = store.company.ticket_recipient(request.path_params["task_id"])
            instructions = payload.get("instructions", "")
            if not isinstance(instructions, str) or len(instructions) > 2000:
                raise ValueError("派工備註上限為 2000 字")
            summary = f"工單 {task['task_id']}｜{task['title']}"
            if instructions.strip():
                summary += "\n" + instructions.strip()
            message = store.owner_message(summary, "@" + employee, task["department"], action_type="handoff")
            dispatch = replies.submit(message, work=True) if replies is not None else {}
            return {"message": message, "dispatch": dispatch, "status_message": work_feedback(dispatch)}

        return await company_mutation(request, assign_work, status=202)

    @server.custom_route("/api/channel/toggle", methods=["POST"])
    async def api_channel_toggle(request: Request) -> JSONResponse:
        if hub:
            hub.bind_loop()
        try:
            payload = await request.json()
            enabled = payload.get("enabled") if isinstance(payload, dict) else None
            if not isinstance(enabled, bool):
                raise ValueError("enabled must be a boolean")
            store.set_channel_enabled(enabled)
            if enabled:
                dispatch_planning()
            return JSONResponse({"enabled": store.channel_enabled})
        except (ValueError, TypeError, KeyError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @server.custom_route("/api/members/{agent_id}/mute", methods=["POST"])
    async def api_member_mute(request: Request) -> JSONResponse:
        if not _local_management_request(request):
            return JSONResponse({"error": "老闆管理限本機聊天室"}, status_code=403)
        if hub:
            hub.bind_loop()
        try:
            payload = await request.json()
            muted = payload.get("muted") if isinstance(payload, dict) else None
            if not isinstance(muted, bool):
                raise ValueError("muted must be a boolean")
            member = store.set_member_muted(request.path_params["agent_id"], muted)
            if member is None:
                return JSONResponse({"error": "member not found"}, status_code=404)
            return JSONResponse({"member": member})
        except (ValueError, TypeError, KeyError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @server.custom_route("/api/members/{agent_id}", methods=["DELETE"])
    async def api_member_remove(request: Request) -> JSONResponse:
        if not _local_management_request(request):
            return JSONResponse({"error": "老闆管理限本機聊天室"}, status_code=403)
        if hub:
            hub.bind_loop()
        result = store.remove_member(request.path_params["agent_id"])
        if result is None:
            return JSONResponse({"error": "member not found"}, status_code=404)
        return JSONResponse(result)

    @server.custom_route("/api/session/reset", methods=["POST"])
    async def api_session_reset(_request: Request) -> JSONResponse:
        if hub:
            hub.bind_loop()
        store.reset_session()
        return JSONResponse({"ok": True})

    @server.custom_route("/hooks/post-tool-use", methods=["POST"])
    async def post_tool_use(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            identity = store.resolve_hook_identity(
                str(payload.get("agent_id", "unknown")), payload.get("session_key")
            )
            if identity is None:
                return JSONResponse({"recorded": False, "messages": [], "ambiguous": True})
            store.heartbeat(identity)
            agy = str(payload.get("agent_id", "")).casefold() == "antigravity"
            result = store.report_step(
                identity,
                str(payload.get("action_type", "")),
                str(payload.get("summary", "completed")),
                payload.get("files_touched") or [],
                payload.get("mention_to"),
                payload.get("channel"),
                sync=not agy,
            )
            orders = [] if agy else store.tool_sync(identity, via_hook=True, roadmap=not result["recorded"])
            return JSONResponse({"recorded": result["recorded"], "messages": orders + result["messages"]})
        except (ValueError, TypeError, KeyError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @server.custom_route("/hooks/bind-session", methods=["POST"])
    async def bind_hook_session(request: Request) -> JSONResponse:
        if not _local_management_request(request):
            return JSONResponse({"error": "工作階段綁定限本機"}, status_code=403)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("請提供 JSON 物件")
            client = payload.get("client")
            if client is not None and not isinstance(client, str):
                raise ValueError("客戶端名稱須為字串")
            bound = store.bind_hook_session(
                str(payload.get("session_key", "")), str(payload.get("agent_id", "")), client,
            )
            if bound:
                store.heartbeat(str(payload["agent_id"]))
            return JSONResponse({"bound": bound})
        except (ValueError, TypeError, KeyError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @server.custom_route("/hooks/session-identity", methods=["POST"])
    async def hook_identity(request: Request) -> JSONResponse:
        if not _local_management_request(request):
            return JSONResponse({"error": "工作階段查詢限本機"}, status_code=403)
        try:
            payload = await request.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or not isinstance(payload.get("client"), str):
            return JSONResponse({"error": "缺少客戶端名稱"}, status_code=400)
        session_key = payload.get("session_key")
        member = store.get_session_identity(session_key if isinstance(session_key, str) else None, payload["client"])
        if member:
            store.heartbeat(member["agent_id"])
        return JSONResponse({"member": member, "recovery_required": bool(
            session_key and not member and store.has_client_members(payload["client"])
        )}, headers={"Cache-Control": "no-store"})

    @server.custom_route("/hooks/pre-invocation", methods=["POST"])
    async def pre_invocation(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            client = str(payload.get("agent_id", "unknown"))
            session_key = payload.get("session_key")
            member = store.get_session_identity(session_key, client)
            identity = member["agent_id"] if member else store.resolve_hook_identity(client, session_key)
            if identity is None:
                return JSONResponse({"messages": [], "member": member,
                                     "recovery_required": bool(session_key and store.has_client_members(client))})
            store.heartbeat(identity, working=True)
            result = store.get_sync_status(identity)
            orders = store.tool_sync(identity, via_hook=True, roadmap=False)
            return JSONResponse({"messages": orders + result["unread"], "member": member})
        except (ValueError, TypeError, KeyError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    return server


def build_asgi_app(store: BridgeStore, hub: BroadcastHub | None = None, replies: MentionReplies | None = None):
    hub = hub or BroadcastHub()
    store.add_listener(hub.publish)
    server = build_mcp(store, hub, replies)
    app = server.streamable_http_app(json_response=True, stateless_http=True)

    async def websocket_endpoint(websocket: WebSocket) -> None:
        hub.bind_loop()
        await hub.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            hub.disconnect(websocket)

    web_dir = web_directory()
    # MCP SDK's Starlette app may not expose add_websocket_route across versions.
    app.router.routes.insert(0, WebSocketRoute("/ws", websocket_endpoint))
    app.mount("/assets", StaticFiles(directory=str(web_dir / "assets"), check_dir=False), name="assets")
    app.mount("/", StaticFiles(directory=str(web_dir), html=True, check_dir=False), name="web")
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=f"^{_LOCAL_ORIGIN}$",
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )
    app.add_middleware(LoopbackOnly)
    app.state.agentbridge_hub = hub
    return app


class ServerController:
    """Starts/stops a loopback-only Streamable HTTP server in a background thread."""

    def __init__(
        self, store: BridgeStore, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *, enable_replies: bool = False,
    ) -> None:
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("AgentBridge 僅支援本機監聽（127.0.0.1、::1 或 localhost）")
        self.store = store
        self.host = "127.0.0.1" if host == "localhost" else host
        self.port = int(port)
        self.hub = BroadcastHub()
        self._thread: threading.Thread | None = None
        self._server: uvicorn.Server | None = None
        self._lock = threading.RLock()
        self._enable_replies = enable_replies
        self._replies: MentionReplies | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._server and self._server.started)

    def start(self, timeout: float = 12.0) -> None:
        with self._lock:
            if self.running:
                return
            project = configured_work_project() if self._enable_replies and self.host in {"127.0.0.1", "::1", "localhost"} else None
            loopback = "[::1]" if self.host == "::1" else "127.0.0.1"
            self._replies = MentionReplies(
                self.store, project=project, mcp_url=f"http://{loopback}:{self.port}/mcp",
            ) if self._enable_replies else None
            app = build_asgi_app(self.store, self.hub, self._replies)
            config = uvicorn.Config(
                app,
                host=self.host,
                port=self.port,
                log_config=None,
                access_log=False,
                proxy_headers=False,
                lifespan="on",
            )
            server = uvicorn.Server(config)
            failures: list[BaseException] = []

            def run() -> None:
                address = (self.host, self.port)
                family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
                try:
                    listener = socket.create_server(address, family=family, backlog=config.backlog)
                except OSError as exc:
                    reason = exc.winerror if getattr(exc, "winerror", None) is not None else exc.errno
                    if reason in (errno.EADDRINUSE, 10048):
                        message = "連接埠已被使用。請結束舊版 AgentBridge 或其他使用此連接埠的程式後再試。"
                    elif reason in (errno.EACCES, 10013):
                        message = "Windows 或目前的環境拒絕使用這個連接埠。請查看錯誤記錄中的系統訊息。"
                    else:
                        message = f"無法開啟此連接埠。{exc}"
                    failure = RuntimeError(f"{self.host}:{self.port} · {message}")
                    failure.__cause__ = exc
                    failures.append(failure)
                    return

                with listener:
                    try:
                        asyncio.run(server.serve(sockets=[listener]))
                    except (Exception, SystemExit) as exc:
                        logger.exception("MCP server exited on %s:%s", *address)
                        failures.append(RuntimeError(f"MCP 服務啟動失敗（{self.host}:{self.port}）。請查看錯誤記錄。"))
                        failures[-1].__cause__ = exc

            thread = threading.Thread(target=run, name="AgentBridge-MCP", daemon=True)
            self._server = server
            self._thread = thread
            thread.start()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not thread.is_alive():
                self.stop()
                if failures:
                    raise failures[0]
                raise RuntimeError(f"MCP server stopped before startup on {self.host}:{self.port}")
            if server.started:
                return
            time.sleep(0.05)
        self.stop()
        raise TimeoutError(f"MCP server did not start on {self.host}:{self.port}")

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            server, thread = self._server, self._thread
            if self._replies is not None:
                self._replies.close()
                self._replies = None
            if server:
                server.should_exit = True
        if thread and thread.is_alive():
            thread.join(timeout)
        with self._lock:
            if self._thread is thread and (not thread or not thread.is_alive()):
                self._server = None
                self._thread = None
