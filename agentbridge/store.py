from __future__ import annotations

import hashlib
import json
import ntpath
import os
import posixpath
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .company import APPOINTMENT_PRIORITY, HQ, CompanyStore, clean_capabilities
from .config import (
    DEFAULT_LOCK_TTL,
    MAX_ACTIVE_EVENTS,
    MAX_EVENT_CHARS,
    MAX_UNREAD_EVENTS,
)
from .roadmap import ROADMAP_PRIORITY

_ACTION_ALIASES = {
    "write": "edit", "write_file": "edit", "file_write": "edit", "file_edit": "edit",
    "file_modification": "edit", "file_change": "edit", "modify_file": "edit",
    "create": "edit", "delete": "edit", "patch": "edit", "edit_file": "edit",
    "command_run": "command", "run_command": "command", "shell": "command",
    "command_execution": "command", "test_run": "test", "test_execution": "test", "tests": "test",
    "package_install": "install", "package_installation": "install",
    "dependency_install": "install", "install_package": "install",
    "git_operation": "git", "task_handoff": "handoff",
    "lock_files": "lock", "unlock_files": "unlock",
}
_REPORT_ACTIONS = {"edit", "command", "test", "install", "git", "handoff"}
_ALLOWED_ACTIONS = _REPORT_ACTIONS | {"lock", "unlock"}
_DIFF_MARKERS = ("diff --git ", "@@ ", "+++ b/", "--- a/")
_MEMBER_COLORS = (
    "#38BDF8", "#A78BFA", "#34D399", "#FB7185", "#FBBF24", "#2DD4BF",
    "#60A5FA", "#F472B6", "#A3E635", "#FB923C", "#818CF8", "#4ADE80",
)
_SYSTEM_COLOR = "#94A3B8"
_OWNER_COLOR = "#F59E0B"
_HEARTBEAT_TTL = 120
_RECENT_ACTIVITY = 300
_LEADING_MENTION = re.compile(r"^@([a-z0-9_.-]+|全體員工|全體成員|全體|全部)", re.I)
_EVERYONE = {"@all", "all", "@全體員工", "@全體成員", "@全體", "@全部"}
_CLIENT_ALIASES = {
    "codexdesktop": "codex", "codex-desktop": "codex", "codexcli": "codex", "codex-cli": "codex",
    "agy": "antigravity", "antigravitycli": "antigravity", "antigravity-cli": "antigravity",
}
# Older databases contain display paths shortened by the old 120-character summary limit.
_LOCK_MATCH = (
    "(path_key=? OR agentbridge_lock_key(display_path)=? "
    "OR (lock_key_version=0 AND agentbridge_lock_key(display_path)=?))"
)


def _one_line(value: Any, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _safe_summary(value: Any) -> str:
    raw = str(value or "")
    folded = raw.casefold()
    if any(marker in folded for marker in _DIFF_MARKERS):
        return "diff omitted"
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) > 1 and any(line.startswith(("+", "-")) for line in lines):
        return "details omitted"
    return _one_line(raw, 240) or "completed"


def _agent(value: str) -> str:
    return _one_line(value or "unknown", 24) or "unknown"


def _action(value: str) -> str | None:
    action = _one_line(value, 40).lower().replace("-", "_").replace(" ", "_")
    action = _ACTION_ALIASES.get(action, action)
    return action if action in _ALLOWED_ACTIONS else None


class BridgeStore:
    """SQLite WAL store shared by MCP tools, hooks, and the tray UI."""

    def __init__(self, db_path: str | Path, lock_ttl: int = DEFAULT_LOCK_TTL, *,
                 workspace: str | Path | None = None) -> None:
        self.db_path = str(db_path)
        self.lock_ttl = max(5, int(lock_ttl))
        self._workspace = str(Path(workspace).absolute()) if workspace is not None else None
        self._listeners: list[Callable[[str, Any], None]] = []
        self._listeners_lock = threading.RLock()
        self._heartbeats: dict[str, tuple[float, bool]] = {}
        self._presence_lock = threading.Lock()
        self.company = CompanyStore(self)
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _lock_key(self, path: str) -> str:
        if os.name == "nt" or re.match(r"^(?:[A-Za-z]:|\\\\)", path):
            normalized = path.replace("\\", "/")
            if normalized.casefold().startswith("//?/unc/"):
                normalized = "//" + normalized[8:]
            elif re.match(r"^//\?/[A-Za-z]:/", normalized):
                normalized = normalized[4:]
            if self._workspace and not ntpath.splitdrive(normalized)[0]:
                normalized = ntpath.join(self._workspace, normalized)
            return ntpath.normpath(normalized).replace("\\", "/").casefold()
        normalized = posixpath.join(self._workspace, path) if self._workspace else path
        return posixpath.normpath(normalized)

    def _lock_parameters(self, path: str) -> tuple[str, str, str]:
        key = self._lock_key(path)
        return key, key, self._lock_key(_one_line(path, 120))

    def _files(self, values: list[str] | None) -> list[str]:
        paths: dict[str, str] = {}
        for value in values or []:
            name = str(value) if value is not None else ""
            if name.strip() and "\x00" not in name:
                paths.setdefault(self._lock_key(name), name)
        return list(paths.values())

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.create_function("agentbridge_lock_key", 1, self._lock_key, deterministic=True)
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _begin(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _commit(conn: sqlite3.Connection) -> None:
        conn.execute("COMMIT")

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        if conn.in_transaction:
            conn.execute("ROLLBACK")

    def _initialize(self) -> None:
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    agent_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    files_json TEXT NOT NULL,
                    line TEXT NOT NULL,
                    archived_at REAL,
                    content TEXT NOT NULL DEFAULT '',
                    sender_name TEXT NOT NULL DEFAULT '',
                    sender_role TEXT NOT NULL DEFAULT '',
                    color TEXT NOT NULL DEFAULT '#64748b',
                    is_owner INTEGER NOT NULL DEFAULT 0,
                    mention_to TEXT,
                    priority INTEGER NOT NULL DEFAULT 0,
                    muted INTEGER NOT NULL DEFAULT 0,
                    removal_kind TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_events_active_id
                    ON events(archived_at, id);
                CREATE TABLE IF NOT EXISTS agent_cursors (
                    agent_id TEXT PRIMARY KEY,
                    last_read_event_id INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS file_locks (
                    path_key TEXT PRIMARY KEY,
                    display_path TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    locked_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    progress_summary TEXT NOT NULL DEFAULT '',
                    lock_key_version INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_file_locks_expiry
                    ON file_locks(expires_at);
                CREATE TABLE IF NOT EXISTS current_status (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    status_summary TEXT NOT NULL,
                    next_steps TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    updated_by TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS members (
                    agent_id TEXT PRIMARY KEY,
                    agent_name TEXT NOT NULL UNIQUE,
                    client_name TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    role_description TEXT NOT NULL,
                    color TEXT NOT NULL,
                    muted INTEGER NOT NULL DEFAULT 0,
                    last_seen_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    member_key_hash TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS hook_sessions (
                    session_key TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL REFERENCES members(agent_id) ON DELETE CASCADE,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS owner_inbox (
                    agent_id TEXT NOT NULL REFERENCES members(agent_id) ON DELETE CASCADE,
                    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(agent_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS idx_owner_inbox_event ON owner_inbox(event_id);
                INSERT OR IGNORE INTO settings(key, value) VALUES ('channel_enabled', '1');
                INSERT OR IGNORE INTO settings(key, value) VALUES ('group_mode_enabled', '0');
                INSERT OR IGNORE INTO current_status
                    (id, status_summary, next_steps, updated_at, updated_by)
                    VALUES (1, 'AgentBridge ready', '', 0, 'AgentBridge');
                """
            )
            # Additive migration for databases created by AgentBridge 0.1.x.
            existing = {str(row["name"]) for row in conn.execute("PRAGMA table_info(events)")}
            migrations = {
                "content": "TEXT NOT NULL DEFAULT ''",
                "sender_name": "TEXT NOT NULL DEFAULT ''",
                "sender_role": "TEXT NOT NULL DEFAULT ''",
                "color": "TEXT NOT NULL DEFAULT '#64748b'",
                "is_owner": "INTEGER NOT NULL DEFAULT 0",
                "mention_to": "TEXT",
                "priority": "INTEGER NOT NULL DEFAULT 0",
                "muted": "INTEGER NOT NULL DEFAULT 0",
                "removal_kind": "TEXT NOT NULL DEFAULT ''",
                "channel": f"TEXT NOT NULL DEFAULT '{HQ}'",
                "target_agent_id": "TEXT",
            }
            for column, declaration in migrations.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE events ADD COLUMN {column} {declaration}")
            lock_columns = {row["name"] for row in conn.execute("PRAGMA table_info(file_locks)")}
            if "lock_key_version" not in lock_columns:
                conn.execute("ALTER TABLE file_locks ADD COLUMN lock_key_version INTEGER NOT NULL DEFAULT 0")
            members_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(members)")}
            if "is_admin" not in members_columns:
                conn.execute("ALTER TABLE members ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
            if "member_key_hash" not in members_columns:
                conn.execute("ALTER TABLE members ADD COLUMN member_key_hash TEXT NOT NULL DEFAULT ''")
            if "capabilities_json" not in members_columns:
                conn.execute("ALTER TABLE members ADD COLUMN capabilities_json TEXT NOT NULL DEFAULT '[]'")
            status_columns = {row["name"] for row in conn.execute("PRAGMA table_info(current_status)")}
            if "handoff_event_id" not in status_columns:
                conn.execute("ALTER TABLE current_status ADD COLUMN handoff_event_id INTEGER")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_members_name ON members(agent_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_mention_priority ON events(mention_to,priority,id)")
            self.company.initialize(conn)

    def add_listener(self, listener: Callable[[str, Any], None]) -> None:
        with self._listeners_lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def _publish(self, event_type: str, data: Any) -> None:
        with self._listeners_lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(event_type, data)
            except Exception:
                # A disconnected dashboard must never roll back a committed MCP action.
                continue

    @staticmethod
    def _iso_time(timestamp: float) -> str:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _clean_component(value: str, field: str) -> str:
        cleaned = re.sub(r"\s+", "", str(value or "").strip().lower())
        if not cleaned:
            raise ValueError(f"{field} is required")
        if len(cleaned) > 64:
            raise ValueError(f"{field} must be at most 64 characters")
        if not re.fullmatch(r"[a-z0-9._-]+", cleaned):
            raise ValueError(f"{field} may contain only letters, digits, '.', '_' and '-'")
        return cleaned

    @staticmethod
    def _member_by_identity(conn: sqlite3.Connection, identity: str) -> sqlite3.Row | None:
        normalized = _one_line(identity, 160)
        return conn.execute(
            "SELECT * FROM members WHERE agent_id=? OR agent_name=? COLLATE NOCASE LIMIT 1",
            (normalized, normalized),
        ).fetchone()

    def _resolve_agent(self, conn: sqlite3.Connection, identity: str) -> tuple[str, sqlite3.Row | None]:
        member = self._member_by_identity(conn, identity)
        if member:
            return str(member["agent_id"]), member
        if re.fullmatch(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", identity.strip(), re.I):
            raise PermissionError("員工已離開公司或尚未報到")
        # Keep 0.1.x clients working while new sessions use join_group's stable ID.
        return _agent(identity), None

    @staticmethod
    def _touch_member(conn: sqlite3.Connection, agent_id: str, now: float) -> None:
        conn.execute("UPDATE members SET last_seen_at=? WHERE agent_id=?", (now, agent_id))

    @staticmethod
    def _member_data(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "agent_id": str(row["agent_id"]),
            "agent_name": str(row["agent_name"]),
            "client_name": BridgeStore._client_alias(str(row["client_name"])),
            "model_name": str(row["model_name"]),
            "role_description": str(row["role_description"]),
            "color": str(row["color"]),
            "muted": bool(row["muted"]),
            "is_admin": bool(row["is_admin"]),
            "capabilities": json.loads(row["capabilities_json"]),
            "last_seen_at": BridgeStore._iso_time(float(row["last_seen_at"])),
        }

    @staticmethod
    def _next_color(conn: sqlite3.Connection) -> str:
        used = {str(row[0]).lower() for row in conn.execute("SELECT color FROM members")}
        for color in _MEMBER_COLORS:
            if color.lower() not in used:
                return color
        return _MEMBER_COLORS[conn.execute("SELECT COUNT(*) FROM members").fetchone()[0] % len(_MEMBER_COLORS)]

    def join_group(self, client_name: str, model_name: str, role_description: str,
                   capabilities: list[str] | None = None, *, session_key: str | None = None,
                   agent_id: str | None = None, member_key: str | None = None,
                   new_identity: bool = False) -> dict[str, Any]:
        client = self._client_alias(self._clean_component(client_name, "client_name"))
        model = self._clean_component(model_name, "model_name")
        role = _one_line(role_description, 240)
        caps = json.dumps(clean_capabilities(capabilities if capabilities is not None else []), ensure_ascii=False)
        base_name = f"{client}-{model}"
        now = time.time()
        key = secrets.token_urlsafe(32)
        with self._connection() as conn:
            self._begin(conn)
            try:
                binding = self._session_member(conn, session_key)
                if (agent_id is None) != (member_key is None):
                    raise ValueError("恢復身分需同時提供 agent_id 與 member_key")
                if agent_id is not None:
                    recovered = self._verified_member(conn, agent_id, member_key)
                    if binding and binding["agent_id"] != recovered["agent_id"]:
                        raise PermissionError("此工作階段已綁定另一個員工")
                    binding = recovered
                if binding:
                    if self._client_alias(binding["client_name"]) != client:
                        raise PermissionError("此員工屬於另一個客戶端")
                    agent_id = binding["agent_id"]
                    if session_key:
                        self._bind_session_in(conn, session_key, agent_id, now)
                    self._touch_member(conn, agent_id, now)
                    snapshot = self.company.snapshot_in(conn, agent_id)
                    self._commit(conn)
                    return {**self._member_data(binding), "last_seen_at": self._iso_time(now), "reused": True,
                            "workstation": snapshot["workstation"], "snapshot": snapshot}
                existing = self._client_members(conn, client, model)
                if existing and not new_identity:
                    self._commit(conn)
                    return {
                        "identity_required": True, "existing_names": [row["agent_name"] for row in existing[:8]],
                        "existing_count": len(existing),
                        "message": "請用原工作階段的 session_key 恢復，或提供原員工 agent_id 與 member_key。"
                                   "若記不得原身分，請老闆從員工資訊發放恢復用金鑰。只有老闆要求新增獨立員工時才使用 new_identity=true。",
                    }
                candidate = base_name
                suffix = 2
                while conn.execute(
                    "SELECT 1 FROM members WHERE agent_name=? UNION ALL SELECT 1 FROM workstations WHERE seat_id=?",
                    (candidate, candidate),
                ).fetchone():
                    candidate = f"{base_name}-{suffix}"
                    suffix += 1
                agent_id = str(uuid.uuid4())
                color = self._next_color(conn)
                conn.execute(
                    "INSERT INTO members(agent_id,agent_name,client_name,model_name,role_description,color,last_seen_at,created_at,member_key_hash,capabilities_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (agent_id, candidate, client, model, role, color, now, now, self._key_digest(key), caps),
                )
                if session_key:
                    self._bind_session_in(conn, session_key, agent_id, now)
                conn.execute(
                    "UPDATE settings SET value='1' WHERE key='group_mode_enabled'"
                )
                event_id = self._insert_event(
                    conn,
                    "system",
                    "system",
                    f"{candidate} 已到職",
                    [],
                    now,
                    sender_name="系統",
                    sender_role="system",
                    color=_SYSTEM_COLOR,
                    mention_to="@all",
                    priority=10,
                )
                # A joiner starts after its own announcement but receives earlier group history.
                conn.execute(
                    "INSERT INTO agent_cursors(agent_id,last_read_event_id) VALUES(?,?)",
                    (agent_id, event_id),
                )
                member_row = conn.execute("SELECT * FROM members WHERE agent_id=?", (agent_id,)).fetchone()
                member = self._member_data(member_row)
                snapshot = self.company.snapshot_in(conn, agent_id)
                self._archive_old(conn, now)
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        message = self.get_message(event_id)
        self._publish("new_message", message)
        self._publish("member_updated", self.list_members())
        return {**member, "system_message": message, "member_key": key,
                "workstation": snapshot["workstation"], "snapshot": snapshot}

    def list_members(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM members ORDER BY created_at,agent_name").fetchall()
        return [self._member_data(row) for row in rows]

    @staticmethod
    def _client_alias(client):
        prefix, separator, model = client.partition("-gpt")
        if separator and _CLIENT_ALIASES.get(prefix, prefix) == "codex" and re.match(r"[-.]?\d", model):
            return "codex"
        return _CLIENT_ALIASES.get(client, client)

    def _client_members(self, conn, client, model=None):
        rows = conn.execute(
            "SELECT m.agent_name,m.client_name,m.model_name FROM members m "
            "ORDER BY EXISTS(SELECT 1 FROM workstations w WHERE w.assigned_agent_id=m.agent_id) DESC,"
            "m.is_admin DESC,m.created_at,m.agent_name"
        )
        matches = [row for row in rows if self._client_alias(row["client_name"]) == client]
        return sorted(matches, key=lambda row: row["model_name"] != model) if model else matches

    @staticmethod
    def _session_member(conn, session_key):
        if not session_key:
            return None
        return conn.execute(
            "SELECT m.* FROM hook_sessions h JOIN members m ON m.agent_id=h.agent_id WHERE h.session_key=?",
            (_one_line(session_key, 200),),
        ).fetchone()

    @staticmethod
    def _bind_session_in(conn, session_key, agent_id, now):
        result = conn.execute(
            "INSERT INTO hook_sessions(session_key,agent_id,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(session_key) DO UPDATE SET updated_at=excluded.updated_at "
            "WHERE hook_sessions.agent_id=excluded.agent_id",
            (_one_line(session_key, 200), agent_id, now),
        )
        return bool(result.rowcount)

    def get_session_identity(self, session_key, client):
        with self._connection() as conn:
            member = self._session_member(conn, session_key)
        if member and self._client_alias(member["client_name"]) == self._client_alias(client.casefold()):
            return {"agent_id": member["agent_id"], "agent_name": member["agent_name"]}
        return None

    def has_client_members(self, client: str) -> bool:
        with self._connection() as conn:
            return bool(self._client_members(conn, self._client_alias(client.casefold())))

    @staticmethod
    def _key_digest(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def _verified_member(self, conn: sqlite3.Connection, identity: str, key: str) -> sqlite3.Row:
        member = self._member_by_identity(conn, identity)
        if (member is None or not 0 < len(key) <= 128 or
                not secrets.compare_digest(str(member["member_key_hash"]), self._key_digest(key))):
            raise PermissionError("員工金鑰無效")
        return member

    def issue_member_key(self, identity: str) -> str | None:
        key = secrets.token_urlsafe(32)
        with self._connection() as conn:
            changed = conn.execute(
                "UPDATE members SET member_key_hash=? WHERE agent_id=?",
                (self._key_digest(key), identity),
            ).rowcount
        return key if changed else None

    def set_member_admin(self, identity: str, enabled: bool) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "UPDATE members SET is_admin=? WHERE agent_id=? RETURNING *",
                (int(enabled), identity),
            ).fetchone()
        if row is None:
            return None
        self._publish("member_updated", self.list_members())
        return self._member_data(row)

    def bind_hook_session(self, session_key: str, identity: str, client: str | None = None) -> bool:
        key = _one_line(session_key, 200)
        if not key:
            return False
        now = time.time()
        with self._connection() as conn:
            self._begin(conn)
            try:
                member = self._member_by_identity(conn, identity)
                if member is None or (
                    client and client.casefold() not in {member["agent_id"], member["agent_name"]} and
                    self._client_alias(client.casefold()) != self._client_alias(member["client_name"])
                ):
                    self._commit(conn)
                    return False
                bound = self._bind_session_in(conn, key, member["agent_id"], now)
                if bound:
                    self._touch_member(conn, member["agent_id"], now)
                self._commit(conn)
                return bound
            except Exception:
                self._rollback(conn)
                raise

    def resolve_hook_identity(self, identity: str, session_key: str | None = None) -> str | None:
        alias = _agent(identity)
        with self._connection() as conn:
            client = self._client_alias(alias.casefold())
            group_mode = conn.execute(
                "SELECT value FROM settings WHERE key='group_mode_enabled'"
            ).fetchone()
            if session_key:
                binding = self._session_member(conn, session_key)
                if binding:
                    if client in {"codex", "antigravity"} and self._client_alias(binding["client_name"]) != client:
                        return None
                    return str(binding["agent_id"])
            member = self._member_by_identity(conn, identity)
            if member:
                return str(member["agent_id"])
            if session_key and group_mode and group_mode["value"] == "1":
                # A named but unbound conversation must not inherit another employee's inbox.
                return None
            matches = [row for row in conn.execute("SELECT agent_id,client_name FROM members")
                       if self._client_alias(row["client_name"]) == client]
            if len(matches) == 1:
                return str(matches[0]["agent_id"])
            if len(matches) > 1:
                # Never attribute a multi-session hook to the wrong same-client agent.
                return None
            if group_mode and group_mode["value"] == "1":
                return None
        return alias

    @staticmethod
    def _message_data(row: sqlite3.Row) -> dict[str, Any]:
        try:
            files = json.loads(str(row["files_json"] or "[]"))
        except (TypeError, ValueError):
            files = []
        content = str(row["content"] or row["summary"] or "")
        message = {
            "id": int(row["id"]),
            "sender_id": str(row["agent_id"]),
            "sender_name": "老闆" if row["is_owner"] else str(row["sender_name"] or row["agent_id"]),
            "sender_role": str(row["sender_role"] or ""),
            "color": str(row["color"] or _SYSTEM_COLOR),
            "is_owner": bool(row["is_owner"]),
            "action_type": str(row["action_type"]),
            "summary": content,
            "files_touched": files,
            "mention_to": row["mention_to"],
            "created_at": BridgeStore._iso_time(float(row["created_at"])),
            "channel": row["channel"],
        }
        if row["removal_kind"]:
            message["removal_kind"] = str(row["removal_kind"])
        return message

    def get_message(self, event_id: int) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(f"message {event_id} not found")
        return self._message_data(row)

    def member_messages(self, identity: str, key: str, limit: int = 10) -> list[dict[str, Any]]:
        with self._connection() as conn:
            actor = self._verified_member(conn, identity, key)
            rows = conn.execute(
                "SELECT * FROM events WHERE agent_id=? AND archived_at IS NULL AND removal_kind='' "
                "ORDER BY id DESC LIMIT ?", (actor["agent_id"], max(1, min(limit, 30))),
            ).fetchall()
        return [{"id": row["id"], "action_type": row["action_type"], "summary": str(row["summary"])[:150],
                 "created_at": self._iso_time(row["created_at"])} for row in rows]

    def remove_message(
        self, event_id: int, *, actor: tuple[str, str] | None = None, own_only: bool = False,
    ) -> dict[str, Any]:
        with self._connection() as conn:
            self._begin(conn)
            try:
                member = self._verified_member(conn, *actor) if actor is not None else None
                row = conn.execute("SELECT * FROM events WHERE id=? AND archived_at IS NULL", (event_id,)).fetchone()
                if row is None:
                    raise KeyError("找不到這則訊息")
                owns = row["agent_id"] == (member["agent_id"] if member is not None else "owner")
                if row["action_type"] == "system" or (own_only and not owns):
                    raise PermissionError("只能撤回自己發送的訊息")
                if member is not None and not owns:
                    author = self._member_by_identity(conn, row["agent_id"])
                    if not member["is_admin"] or row["is_owner"] or (author and author["is_admin"]):
                        raise PermissionError("沒有刪除此訊息的權限")
                if row["removal_kind"]:
                    self._commit(conn)
                    return self._message_data(row)

                kind = "withdrawn" if owns else "deleted"
                label = "訊息已撤回" if owns else "訊息已由管理員刪除"
                conn.execute(
                    "UPDATE events SET removal_kind=?, summary=?, content=?, line=?, files_json='[]', "
                    "mention_to=NULL, priority=0 WHERE id=?", (kind, label, label, label, event_id),
                )
                conn.execute(
                    "UPDATE current_status SET status_summary=?,next_steps='' "
                    "WHERE updated_at=? AND updated_by=?", (label, row["created_at"], row["agent_id"]),
                )
                locks_changed = False
                for path in self._message_data(row)["files_touched"]:
                    changed = conn.execute(
                        f"UPDATE file_locks SET progress_summary='' WHERE {_LOCK_MATCH} AND agent_id=? AND progress_summary=?",
                        (*self._lock_parameters(path), row["agent_id"], _safe_summary(row["summary"])),
                    ).rowcount
                    locks_changed = locks_changed or bool(changed)
                removed = self._message_data(conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        self._publish("message_updated", removed)
        if locks_changed:
            self._publish("locks_updated", self.list_locks())
        return removed

    def _normalize_mention(self, conn: sqlite3.Connection, mention_to: str | None) -> str | None:
        if mention_to is None or not str(mention_to).strip():
            return None
        target = str(mention_to).strip().lower()
        if target in _EVERYONE:
            return "@all"
        target_name = target[1:] if target.startswith("@") else target
        member = conn.execute(
            "SELECT agent_name FROM members WHERE agent_name COLLATE NOCASE IN (?,?) "
            "ORDER BY (agent_name=? COLLATE NOCASE) DESC LIMIT 1",
            (target_name, target_name.rstrip("."), target_name),
        ).fetchone()
        if not member:
            target_name = target_name.rstrip(".")
            seat = conn.execute("SELECT seat_id,assigned_agent_id FROM workstations WHERE seat_id=?", (target_name,)).fetchone()
            if seat is None:
                raise ValueError(f"unknown mention target: {mention_to}")
            if not seat["assigned_agent_id"]:
                raise ValueError(f"工位 {target_name} 目前空缺")
            return "@" + seat["seat_id"]
        return "@" + str(member["agent_name"])

    def owner_message(self, content: str, mention_to: str | None = None, channel: str | None = None, *,
                      action_type: str = "message") -> dict[str, Any]:
        text = str(content or "").strip()
        if not text:
            raise ValueError("content is required")
        if len(text) > 4000:
            raise ValueError("content must be at most 4000 characters")
        now = time.time()
        with self._connection() as conn:
            self._begin(conn)
            try:
                leading = _LEADING_MENTION.match(text)
                target = self._normalize_mention(conn, mention_to or ("@" + leading[1] if leading else None))
                event_id = self._insert_event(
                    conn,
                    "owner",
                    action_type,
                    text,
                    [],
                    now,
                    sender_name="老闆",
                    sender_role="owner",
                    color=_OWNER_COLOR,
                    is_owner=True,
                    mention_to=target,
                    priority=100,
                    content=text,
                    channel=self.company.channel_in(conn, "owner", channel),
                )
                self._queue_directed_in(conn, event_id, target)
                self._archive_old(conn, now)
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        message = self.get_message(event_id)
        self._publish("new_message", message)
        return message

    @staticmethod
    def _queue_directed_in(conn, event_id, target, *, exclude=None):
        if target and target != "@all":
            recipient = conn.execute(
                "SELECT COALESCE(target_agent_id,(SELECT agent_id FROM members WHERE agent_name=?)) "
                "FROM events WHERE id=?", (target[1:], event_id),
            ).fetchone()[0]
            if recipient and recipient != exclude:
                conn.execute("INSERT INTO owner_inbox(agent_id,event_id) VALUES(?,?)", (recipient, event_id))
                conn.execute("UPDATE events SET target_agent_id=? WHERE id=?", (recipient, event_id))

    def mention_recipients(self, target: str, *, exclude: str = "", event_id: int | None = None) -> list[dict[str, Any]]:
        with self._connection() as conn:
            if not self._enabled_in(conn):
                return []
            event = conn.execute("SELECT target_agent_id FROM events WHERE id=?", (event_id,)).fetchone()
            target_id = event["target_agent_id"] if event else None
            rows = conn.execute(
                "SELECT * FROM members WHERE muted=0 AND agent_id<>? AND (?='@all' OR agent_name=? OR agent_id=?) "
                "ORDER BY created_at,agent_name",
                (exclude, target, target.removeprefix("@"), target_id),
            ).fetchall()
        return [self._member_data(row) for row in rows]

    def _can_reply_in(self, conn: sqlite3.Connection, origin: dict[str, Any], agent_id: str) -> bool:
        if not self._enabled_in(conn):
            return False
        member = conn.execute("SELECT agent_name,muted FROM members WHERE agent_id=?", (agent_id,)).fetchone()
        event = conn.execute("SELECT * FROM events WHERE id=? AND archived_at IS NULL", (origin["id"],)).fetchone()
        if not event or event["removal_kind"] or self._message_data(event) != origin:
            return False
        if not origin["is_owner"]:
            if origin["action_type"] not in _REPORT_ACTIONS or origin["sender_id"] == agent_id:
                return False
            sender = conn.execute("SELECT muted FROM members WHERE agent_id=?", (origin["sender_id"],)).fetchone()
            if not sender or sender["muted"]:
                return False
        if not member or member["muted"] or not event["mention_to"]:
            return False
        seat = self.company.seat_for(conn, agent_id)
        return self._visible_event(event, agent_id, member["agent_name"], seat["department"] if seat else None)

    def can_reply_to_mention(self, origin: dict[str, Any], agent_id: str) -> bool:
        with self._connection() as conn:
            return self._can_reply_in(conn, origin, agent_id)

    def publish_mention_reply(
        self, origin: dict[str, Any], agent_id: str, content: str, *, system_notice: bool = False,
    ) -> None:
        text = content.strip()[:4000]
        if not text:
            return
        now = time.time()
        with self._connection() as conn:
            self._begin(conn)
            try:
                if not self._can_reply_in(conn, origin, agent_id):
                    self._commit(conn)
                    return
                if not system_notice:
                    self._touch_member(conn, agent_id, now)
                event = self._insert_event(
                    conn, "system" if system_notice else agent_id, "system" if system_notice else "message", text, [], now,
                    sender_name="系統" if system_notice else None, content=text, channel=origin["channel"],
                )
                reply = conn.execute("SELECT * FROM events WHERE id=?", (event,)).fetchone()
                self._archive_old(conn, now)
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        self._publish("new_message", self._message_data(reply))

    def _check_moderator(self, conn: sqlite3.Connection, actor: tuple[str, str], target: sqlite3.Row) -> None:
        member = self._verified_member(conn, *actor)
        if not member["is_admin"]:
            raise PermissionError("需要管理員身分")
        if target["is_admin"]:
            raise PermissionError("只有老闆可以管理管理員")

    def set_member_muted(self, identity: str, muted: bool, *, actor: tuple[str, str] | None = None) -> dict[str, Any] | None:
        now = time.time()
        with self._connection() as conn:
            self._begin(conn)
            try:
                member = self._member_by_identity(conn, identity)
                if member is None:
                    self._commit(conn)
                    return None
                if actor is not None:
                    self._check_moderator(conn, actor, member)
                conn.execute(
                    "UPDATE members SET muted=?,last_seen_at=? WHERE agent_id=?",
                    (1 if muted else 0, now, member["agent_id"]),
                )
                updated_row = conn.execute(
                    "SELECT * FROM members WHERE agent_id=?", (member["agent_id"],)
                ).fetchone()
                result = self._member_data(updated_row)
                members = [self._member_data(row) for row in conn.execute(
                    "SELECT * FROM members ORDER BY created_at,agent_name"
                ).fetchall()]
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        self._publish("member_updated", members)
        return result

    def remove_member(self, identity: str, *, actor: tuple[str, str] | None = None) -> dict[str, Any] | None:
        now = time.time()
        with self._connection() as conn:
            self._begin(conn)
            try:
                member_row = self._member_by_identity(conn, identity)
                if member_row is None:
                    self._commit(conn)
                    return None
                if actor is not None:
                    self._check_moderator(conn, actor, member_row)
                member = self._member_data(member_row)
                agent_id = str(member_row["agent_id"])
                self.company.employee_removed_in(conn, agent_id, now)
                conn.execute("DELETE FROM members WHERE agent_id=?", (agent_id,))
                conn.execute("DELETE FROM agent_cursors WHERE agent_id=?", (agent_id,))
                released = conn.execute("DELETE FROM file_locks WHERE agent_id=?", (agent_id,)).rowcount
                event_id = self._insert_event(
                    conn,
                    "system",
                    "system",
                    f"{member['agent_name']} 已離開公司",
                    [],
                    now,
                    sender_name="系統",
                    sender_role="system",
                    color=_SYSTEM_COLOR,
                    mention_to="@all",
                    priority=10,
                )
                members = [self._member_data(row) for row in conn.execute(
                    "SELECT * FROM members ORDER BY created_at,agent_name"
                ).fetchall()]
                locks_changed = bool(released)
                self._archive_old(conn, now)
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        message = self.get_message(event_id)
        self._publish("new_message", message)
        self._publish("member_updated", members)
        self._publish("workstations_updated", self.company.workstations())
        self.company.publish_tasks()
        self.company.roadmap.publish()
        if locks_changed:
            self._publish("locks_updated", self.list_locks())
        return {"member": member, "system_message": message}

    @property
    def channel_enabled(self) -> bool:
        with self._connection() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='channel_enabled'").fetchone()
        return bool(row and row["value"] == "1")

    def set_channel_enabled(self, enabled: bool) -> bool:
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO settings(key,value) VALUES('channel_enabled',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if enabled else "0",),
            )
        self._publish("channel_status", {"enabled": bool(enabled)})
        return enabled

    def toggle_channel(self) -> bool:
        with self._connection() as conn:
            self._begin(conn)
            try:
                row = conn.execute("SELECT value FROM settings WHERE key='channel_enabled'").fetchone()
                enabled = not (row and row["value"] == "1")
                conn.execute(
                    "INSERT INTO settings(key,value) VALUES('channel_enabled',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("1" if enabled else "0",),
                )
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        self._publish("channel_status", {"enabled": enabled})
        return enabled

    @staticmethod
    def _format_event(agent_id: str, action: str, files: list[str], summary: str, now: float) -> str:
        stamp = datetime.fromtimestamp(now, timezone.utc).astimezone().strftime("%H:%M:%S")
        shown_files = []
        for path in files[:2]:
            display = _one_line(path.replace("\\", "/"), len(path))
            shown_files.append(display if len(display) <= 22 else "…" + display[-21:])
        file_text = ",".join(shown_files)
        if len(files) > 2:
            file_text += f"+{len(files) - 2}"
        prefix = f"[{stamp}] {agent_id[:20]} {action}"
        file_part = f" {file_text}" if file_text else ""
        summary_limit = max(1, MAX_EVENT_CHARS - len(prefix) - len(file_part) - 1)
        compact_summary = summary
        if len(compact_summary) > summary_limit:
            compact_summary = compact_summary[: max(0, summary_limit - 1)].rstrip() + "…"
        return f"{prefix}{file_part} {compact_summary}"

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        action: str,
        summary: str,
        files: list[str],
        now: float,
        *,
        sender_name: str | None = None,
        sender_role: str | None = None,
        color: str | None = None,
        is_owner: bool = False,
        mention_to: str | None = None,
        priority: int = 0,
        muted: bool | None = None,
        content: str | None = None,
        channel: str | None = None,
        target_agent_id: str | None = None,
        update_status: bool = True,
    ) -> int:
        member = self._member_by_identity(conn, agent_id)
        display_name = sender_name or (str(member["agent_name"]) if member else agent_id)
        role = sender_role or (str(member["role_description"]) if member else "")
        event_color = color or (str(member["color"]) if member else _SYSTEM_COLOR)
        event_muted = bool(member["muted"]) if muted is None and member else bool(muted)
        content_text = str(content if content is not None else _safe_summary(summary))
        stored_summary = content_text if content is not None else _safe_summary(summary)
        compact_summary = _safe_summary(summary)
        line = self._format_event(display_name, action, files, compact_summary, now)
        if target_agent_id is None and mention_to and mention_to != "@all":
            seat = conn.execute("SELECT assigned_agent_id FROM workstations WHERE seat_id=?", (mention_to[1:],)).fetchone()
            target_agent_id = seat["assigned_agent_id"] if seat else None
        cursor = conn.execute(
            "INSERT INTO events(created_at,agent_id,action_type,summary,files_json,line,content,sender_name,sender_role,color,is_owner,mention_to,priority,muted,channel,target_agent_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                now, agent_id, action, stored_summary, json.dumps(files, ensure_ascii=False), line,
                content_text, display_name, role, event_color, 1 if is_owner else 0,
                mention_to, int(priority), 1 if event_muted else 0,
                channel or self.company.channel_in(conn, agent_id), target_agent_id,
            ),
        )
        if update_status:
            conn.execute(
                "UPDATE current_status SET status_summary=?, updated_at=?, updated_by=? WHERE id=1",
                (line, now, agent_id),
            )
        return int(cursor.lastrowid)

    @staticmethod
    def _archive_old(conn: sqlite3.Connection, now: float) -> None:
        conn.execute(
            "UPDATE events SET archived_at=? WHERE archived_at IS NULL AND id NOT IN "
            "(SELECT id FROM events WHERE archived_at IS NULL ORDER BY id DESC LIMIT ?)",
            (now, MAX_ACTIVE_EVENTS),
        )

    @staticmethod
    def _clean_expired_locks(conn: sqlite3.Connection, now: float) -> bool:
        return bool(conn.execute("DELETE FROM file_locks WHERE expires_at <= ?", (now,)).rowcount)

    @staticmethod
    def _visible_event(event, agent_id, name, department):
        if event["removal_kind"] or (event["muted"] and not event["is_owner"]):
            return False
        target = (event["mention_to"] or "").lower()
        target_id = event["target_agent_id"]
        directed = bool(target_id == agent_id if target_id is not None else target == "@" + name.lower())
        if (target_id is not None or target not in ("", "@all", "all")) and not directed:
            return False
        return bool(
            event["is_owner"] or event["channel"] == HQ
            or department is not None and (event["channel"] == department or
                                           directed and event["action_type"] == "handoff")
        )

    @staticmethod
    def _collect_directed_inbox(conn, agent_id, limit, department):
        addressed = conn.execute(
            "SELECT e.id,e.line FROM owner_inbox i JOIN events e ON e.id=i.event_id "
            "WHERE i.agent_id=? AND i.delivered=0 AND e.removal_kind='' "
            "AND (e.is_owner=1 OR (e.muted=0 AND (e.channel=? OR ? IS NOT NULL))) "
            "ORDER BY e.is_owner DESC,e.id LIMIT ?", (agent_id, HQ, department, limit),
        ).fetchall()
        conn.executemany("UPDATE owner_inbox SET delivered=1 WHERE agent_id=? AND event_id=?",
                         ((agent_id, event["id"]) for event in addressed))
        return [event["line"] for event in addressed]

    def _collect_unread(self, conn: sqlite3.Connection, agent_id: str) -> list[str]:
        member = self._member_by_identity(conn, agent_id)
        target_name = str(member["agent_name"] if member else agent_id)
        seat = self.company.seat_for(conn, agent_id)
        department = seat["department"] if seat else None
        conn.execute(
            "INSERT OR IGNORE INTO agent_cursors(agent_id,last_read_event_id) VALUES(?,0)",
            (agent_id,),
        )
        cursor = conn.execute(
            "SELECT last_read_event_id FROM agent_cursors WHERE agent_id=?", (agent_id,)
        ).fetchone()
        last_read = int(cursor["last_read_event_id"])
        limit = MAX_UNREAD_EVENTS - bool(conn.execute(
            "SELECT 1 FROM company_appointments WHERE agent_id=?", (agent_id,)
        ).fetchone())
        notices = self._collect_directed_inbox(conn, agent_id, limit, department)
        notices += self.company.roadmap.collect_notices_in(conn, member, seat, limit - len(notices))
        limit -= len(notices)
        if not limit:
            return notices
        rows = []
        for row in conn.execute(
            "SELECT id,line,priority,is_owner,channel,mention_to,target_agent_id,action_type,muted,removal_kind "
            "FROM events WHERE archived_at IS NULL AND removal_kind='' AND id>? AND agent_id<>? AND priority NOT IN (?,?) "
            "AND NOT EXISTS(SELECT 1 FROM owner_inbox i WHERE i.event_id=events.id) "
            "ORDER BY priority DESC,id DESC",
            (last_read, agent_id, APPOINTMENT_PRIORITY, ROADMAP_PRIORITY),
        ):
            if self._visible_event(row, agent_id, target_name, department):
                rows.append(row)
                if len(rows) == limit:
                    break
        if not rows:
            return notices
        rows = sorted(rows, key=lambda row: (-int(row["priority"]), int(row["id"])))
        new_cursor = max(int(row["id"]) for row in rows)
        conn.execute(
            "UPDATE agent_cursors SET last_read_event_id=? WHERE agent_id=?",
            (new_cursor, agent_id),
        )
        return notices + [str(row["line"]) for row in rows]

    def tool_sync(self, identity: str, *, unread: bool = False, via_hook: bool = False,
                  roadmap: bool = True) -> list[str]:
        with self.company.write() as conn:
            if not self._enabled_in(conn):
                return []
            agent, member = self._resolve_agent(conn, identity)
            self._touch_member(conn, agent, time.time())
            updates = self._collect_unread(conn, agent) if unread else []
            orders = self.company.collect_appointment_in(conn, agent, via_hook=via_hook)
            if not unread and roadmap:
                seat = self.company.seat_for(conn, agent)
                orders += self._collect_directed_inbox(
                    conn, agent, MAX_UNREAD_EVENTS - len(orders), seat["department"] if seat else None,
                )
                orders += self.company.roadmap.collect_notices_in(
                    conn, member, seat, MAX_UNREAD_EVENTS - len(orders),
                )
            return orders + updates

    def report_step(
        self,
        agent_id: str,
        action_type: str,
        summary: str,
        files_touched: list[str] | None = None,
        mention_to: str | None = None,
        channel: str | None = None,
        *,
        sync: bool = True,
    ) -> dict[str, Any]:
        action = _action(action_type)
        if action not in _REPORT_ACTIONS:
            return {"recorded": False, "messages": []}
        clean_summary = _safe_summary(summary)
        touched = self._files(files_touched)
        now = time.time()
        locks_changed = False
        expired_locks = False
        with self._connection() as conn:
            self._begin(conn)
            try:
                agent, member = self._resolve_agent(conn, agent_id)
                self._touch_member(conn, agent, now)
                target = self._normalize_mention(conn, mention_to)
                expired_locks = self._clean_expired_locks(conn, now)
                work_content = None
                if action == "handoff" and target and clean_summary not in {"diff omitted", "details omitted"}:
                    work_content = str(summary).strip()[:4000] or clean_summary
                event_id = self._insert_event(
                    conn,
                    agent,
                    action,
                    clean_summary,
                    touched,
                    now,
                    mention_to=target,
                    muted=bool(member["muted"]) if member else False,
                    content=work_content,
                    channel=self.company.channel_in(conn, agent, channel),
                )
                for file in touched:
                    result = conn.execute(
                        "UPDATE file_locks SET progress_summary=?,expires_at=? "
                        f"WHERE {_LOCK_MATCH} AND agent_id=?",
                        (clean_summary, now + self.lock_ttl, *self._lock_parameters(file), agent),
                    )
                    locks_changed = locks_changed or bool(result.rowcount)
                if action == "handoff":
                    if not member or not member["muted"]:
                        self._queue_directed_in(conn, event_id, target, exclude=agent)
                    conn.execute(
                        "UPDATE current_status SET status_summary=?,updated_at=?,updated_by=? WHERE id=1",
                        (clean_summary, now, agent),
                    )
                self._archive_old(conn, now)
                messages = self._collect_unread(conn, agent) if sync and self._enabled_in(conn) else []
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        message = self.get_message(event_id)
        self._publish("new_message", message)
        if locks_changed or expired_locks:
            self._publish("locks_updated", self.list_locks())
        return {"recorded": True, "event_id": event_id, "messages": messages}

    @staticmethod
    def _enabled_in(conn: sqlite3.Connection) -> bool:
        row = conn.execute("SELECT value FROM settings WHERE key='channel_enabled'").fetchone()
        return bool(row and row["value"] == "1")

    def lock_files(self, agent_id: str, files: list[str]) -> dict[str, Any]:
        clean_files = self._files(files)
        now = time.time()
        acquired: list[str] = []
        conflicts: list[dict[str, str]] = []
        event_id: int | None = None
        expired_locks = False
        with self._connection() as conn:
            self._begin(conn)
            try:
                agent, member = self._resolve_agent(conn, agent_id)
                self._touch_member(conn, agent, now)
                expired_locks = self._clean_expired_locks(conn, now)
                for file in clean_files:
                    keys = self._lock_parameters(file)
                    row = conn.execute(
                        "SELECT f.*,m.agent_name FROM file_locks f "
                        f"LEFT JOIN members m ON m.agent_id=f.agent_id WHERE {_LOCK_MATCH} "
                        "ORDER BY (f.agent_id<>?) DESC,f.locked_at LIMIT 1",
                        (*keys, agent),
                    ).fetchone()
                    if row and row["agent_id"] != agent:
                        conflicts.append({
                            "file": str(row["display_path"]),
                            "agent_id": str(row["agent_id"]),
                            "agent_name": str(row["agent_name"] or row["agent_id"]),
                            "progress": str(row["progress_summary"] or "editing"),
                        })
                        continue
                    if row:
                        conn.execute("UPDATE file_locks SET display_path=?,expires_at=? WHERE path_key=?",
                                     (file, now + self.lock_ttl, row["path_key"]))
                    else:
                        conn.execute(
                            "INSERT INTO file_locks(path_key,display_path,agent_id,locked_at,expires_at,progress_summary,lock_key_version) "
                            "VALUES(?,?,?,?,?,?,1)",
                            (keys[0], file, agent, now, now + self.lock_ttl, "editing"),
                        )
                    acquired.append(file)
                if acquired:
                    event_id = self._insert_event(conn, agent, "lock", "editing", acquired, now)
                    self._archive_old(conn, now)
                messages = self._collect_unread(conn, agent) if self._enabled_in(conn) else []
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        if event_id is not None:
            self._publish("new_message", self.get_message(event_id))
        if acquired or expired_locks:
            self._publish("locks_updated", self.list_locks())
        return {"acquired": acquired, "conflicts": conflicts, "messages": messages}

    def unlock_files(self, agent_id: str, files: list[str]) -> dict[str, Any]:
        clean_files = self._files(files)
        now = time.time()
        released: list[str] = []
        event_id: int | None = None
        with self._connection() as conn:
            self._begin(conn)
            try:
                agent, _member = self._resolve_agent(conn, agent_id)
                self._touch_member(conn, agent, now)
                for file in clean_files:
                    cursor = conn.execute(
                        f"DELETE FROM file_locks WHERE {_LOCK_MATCH} AND agent_id=?",
                        (*self._lock_parameters(file), agent),
                    )
                    if cursor.rowcount:
                        released.append(file)
                if released:
                    event_id = self._insert_event(conn, agent, "system", "釋放檔案鎖", released, now)
                    self._archive_old(conn, now)
                messages = self._collect_unread(conn, agent) if self._enabled_in(conn) else []
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        if event_id is not None:
            self._publish("new_message", self.get_message(event_id))
        if released:
            self._publish("locks_updated", self.list_locks())
        return {"released": released, "messages": messages}

    def _status_snapshot(self, conn: sqlite3.Connection) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM current_status WHERE id=1").fetchone()
        return {
            "status_summary": str(row["status_summary"]),
            "next_steps": str(row["next_steps"]),
            "updated_at": float(row["updated_at"]),
            "updated_by": str(row["updated_by"]),
            "channel_enabled": self._enabled_in(conn),
        }

    def get_sync_status(self, agent_id: str) -> dict[str, Any]:
        now = time.time()
        expired = False
        with self._connection() as conn:
            self._begin(conn)
            try:
                agent, _member = self._resolve_agent(conn, agent_id)
                self._touch_member(conn, agent, now)
                expired = bool(conn.execute("DELETE FROM file_locks WHERE expires_at <= ?", (now,)).rowcount)
                messages = self._collect_unread(conn, agent) if self._enabled_in(conn) else []
                snapshot = self._status_snapshot(conn)
                event = conn.execute(
                    "SELECT * FROM events WHERE created_at=? AND agent_id=? ORDER BY id DESC LIMIT 1",
                    (snapshot["updated_at"], snapshot["updated_by"]),
                ).fetchone()
                seat = self.company.seat_for(conn, agent)
                name = _member["agent_name"] if _member else agent
                department = seat["department"] if seat else None
                if event and event["agent_id"] != agent and not self._visible_event(
                    event, agent, name, department
                ):
                    snapshot = {**snapshot, "status_summary": "", "updated_at": 0, "updated_by": ""}
                handoff = conn.execute(
                    "SELECT events.* FROM current_status JOIN events ON events.id=current_status.handoff_event_id WHERE current_status.id=1"
                ).fetchone()
                if handoff and handoff["agent_id"] != agent and not self._visible_event(handoff, agent, name, department):
                    snapshot["next_steps"] = ""
                locks = self._list_locks_in(conn)
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        if expired:
            self._publish("locks_updated", locks)
        return {"status": snapshot, "unread": messages, "locks": locks}

    def update_handoff(self, agent_id: str, status_summary: str, next_steps: str) -> dict[str, Any]:
        status = _safe_summary(status_summary)
        steps = _safe_summary(next_steps) if next_steps else ""
        now = time.time()
        with self._connection() as conn:
            self._begin(conn)
            try:
                agent, _member = self._resolve_agent(conn, agent_id)
                self._touch_member(conn, agent, now)
                event_id = self._insert_event(conn, agent, "handoff", status, [], now, update_status=False)
                conn.execute(
                    "UPDATE current_status SET status_summary=?,next_steps=?,updated_at=?,updated_by=?,handoff_event_id=? WHERE id=1",
                    (status, steps, now, agent, event_id),
                )
                self._archive_old(conn, now)
                messages = self._collect_unread(conn, agent) if self._enabled_in(conn) else []
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        self._publish("new_message", self.get_message(event_id))
        return {"saved": True, "messages": messages}

    @staticmethod
    def _list_locks_in(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        return [
            {
                "display_path": str(row["display_path"]),
                "agent_id": str(row["agent_id"]),
                "agent_name": str(row["agent_name"] or row["agent_id"]),
                "color": str(row["color"] or _SYSTEM_COLOR),
                "locked_at": BridgeStore._iso_time(float(row["locked_at"])),
                "expires_at": float(row["expires_at"]),
                "progress_summary": str(row["progress_summary"] or ""),
            }
            for row in conn.execute(
                "SELECT f.display_path,f.agent_id,f.locked_at,f.expires_at,f.progress_summary,"
                "m.agent_name,m.color FROM file_locks f LEFT JOIN members m ON m.agent_id=f.agent_id "
                "ORDER BY f.display_path"
            ).fetchall()
        ]

    def list_locks(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            expired = bool(conn.execute("DELETE FROM file_locks WHERE expires_at <= ?", (time.time(),)).rowcount)
            locks = self._list_locks_in(conn)
        if expired:
            self._publish("locks_updated", locks)
        return locks

    def heartbeat(self, identity: str, working: bool = False) -> None:
        with self._connection() as conn:
            member = self._member_by_identity(conn, identity)
        if member is not None:
            with self._presence_lock:
                self._heartbeats[str(member["agent_id"])] = (time.time(), working)

    def get_presence(self, running: set[str] | None = None) -> list[dict[str, Any]]:
        now = time.time()
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT agent_id, last_seen_at, EXISTS (SELECT 1 FROM file_locks "
                "WHERE file_locks.agent_id=members.agent_id AND expires_at>?) AS locked FROM members",
                (now,),
            ).fetchall()
        present = []
        with self._presence_lock:
            self._heartbeats = {identity: heartbeat for identity, heartbeat in self._heartbeats.items()
                                if now < heartbeat[0] + _RECENT_ACTIVITY}
            for row in rows:
                identity = str(row["agent_id"])
                at, busy = self._heartbeats.get(identity, (0.0, False))
                last_seen = max(at, float(row["last_seen_at"]))
                online = at > 0 and now < at + _HEARTBEAT_TTL
                if row["locked"] or (running and identity in running) or (online and busy):
                    state = "working"
                elif online:
                    state = "online"
                elif now < last_seen + _RECENT_ACTIVITY:
                    state = "recent"
                else:
                    state = "unknown"
                present.append({"agent_id": identity, "state": state, "last_seen_at": self._iso_time(last_seen)})
        return present

    def member_work_context(self, agent_id: str) -> dict[str, Any]:
        """Return a small snapshot without advancing a member's MCP cursor."""
        with self._connection() as conn:
            events = conn.execute(
                "SELECT line FROM events WHERE agent_id=? AND archived_at IS NULL AND removal_kind='' "
                "AND action_type IN ('edit','command','test','install','git','handoff') ORDER BY id DESC LIMIT 3",
                (agent_id,),
            ).fetchall()
            locks = conn.execute(
                "SELECT display_path, COUNT(*) OVER () AS total FROM file_locks WHERE agent_id=? AND expires_at>? "
                "ORDER BY display_path LIMIT 8",
                (agent_id, time.time()),
            ).fetchall()
            seat = self.company.seat_data(self.company.seat_for(conn, agent_id))
            tasks = conn.execute(
                "SELECT task_id,title,status FROM tasks WHERE (status='todo' AND assignee_seat_id=?) OR "
                "(status='in_progress' AND assignee_agent_id=?) ORDER BY updated_at DESC LIMIT 5",
                (seat["seat_id"] if seat else None, agent_id),
            ).fetchall()
        return {
            "agent_id": agent_id,
            "tasks": [dict(row) for row in tasks],
            "recent_reports": [str(row["line"]) for row in reversed(events)],
            "active_locks": [str(row["display_path"])[:160] for row in locks],
            "active_lock_count": int(locks[0]["total"]) if locks else 0,
        }

    def get_group_state(self) -> dict[str, Any]:
        now = time.time()
        expired = False
        with self._connection() as conn:
            self._begin(conn)
            try:
                expired = bool(conn.execute("DELETE FROM file_locks WHERE expires_at <= ?", (now,)).rowcount)
                members = [self._member_data(row) for row in conn.execute(
                    "SELECT * FROM members ORDER BY created_at,agent_name"
                ).fetchall()]
                locks = self._list_locks_in(conn)
                message_rows = conn.execute(
                    "SELECT * FROM events WHERE archived_at IS NULL ORDER BY id DESC LIMIT ?",
                    (MAX_ACTIVE_EVENTS,),
                ).fetchall()
                messages = [self._message_data(row) for row in reversed(message_rows)]
                company = {"workstations": self.company.workstations_in(conn),
                           "tasks": self.company.tasks_in(conn), "channels": self.company.channels_in(conn),
                           **self.company.roadmap.state_in(conn)}
                enabled = self._enabled_in(conn)
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        if expired:
            self._publish("locks_updated", locks)
        return {
            "channel_enabled": enabled,
            "members": members,
            "locks": locks,
            "messages": messages,
            **company,
        }

    def dashboard_snapshot(self, limit: int = 80) -> dict[str, Any]:
        now = time.time()
        expired = False
        with self._connection() as conn:
            self._begin(conn)
            try:
                expired = self._clean_expired_locks(conn, now)
                status = self._status_snapshot(conn)
                events = [dict(row) for row in conn.execute(
                    "SELECT id,created_at,agent_id,action_type,line,archived_at FROM events "
                    "ORDER BY id DESC LIMIT ?", (max(1, min(limit, 300)),)
                ).fetchall()]
                locks = self._list_locks_in(conn)
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        if expired:
            self._publish("locks_updated", locks)
        return {"status": status, "events": events, "locks": locks}

    def reset_session(self) -> None:
        with self._connection() as conn:
            self._begin(conn)
            try:
                pending = [row[0] for row in conn.execute("SELECT agent_id FROM company_appointments")]
                conn.execute("DELETE FROM events")
                conn.execute("DELETE FROM agent_cursors")
                conn.execute("DELETE FROM file_locks")
                now = time.time()
                conn.execute(
                    "UPDATE current_status SET status_summary='Session reset',next_steps='',updated_at=?,updated_by='AgentBridge',handoff_event_id=NULL WHERE id=1",
                    (now,),
                )
                for agent_id in pending:
                    self.company._appointment(conn, agent_id, now)
                self.company.roadmap.restore_pending_notice_in(conn, now)
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        self._publish("session_reset", {})
        self._publish("locks_updated", [])
