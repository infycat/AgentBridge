from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import BridgeStore

HQ = "總部公告"
TASK_STATUSES = ("todo", "in_progress", "review", "done")
APPOINTMENT_PRIORITY = 1000
DEFAULT_SEATS = (
    ("seat-cto", "技術總監", "總部", True, [], "規劃路線、拆分工單，明確指派工位與驗收條件。"),
    ("seat-engine", "引擎修復工程師", "引擎研發", False, ["Python-Test"], "認領後先重現問題，修改前鎖檔，完成後附回歸結果。"),
    ("seat-re", "逆向分析師", "逆向工程", False, ["REA", "CE-MCP"], "認領後依工單範圍分析，交付可查驗的觀察與限制。"),
    ("seat-qa", "QA 測試員", "QA驗收", False, ["Python-Test"], "按驗收條件執行測試，回報重現步驟與通過情況。"),
    ("seat-ui", "介面工程師", "介面設計", False, ["UI"], "認領後鎖定介面檔案，提交可重現的操作驗證。"),
)


class TaskConflict(ValueError):
    pass


def clean_text(value, field, limit, *, required=True):
    if not isinstance(value, str):
        raise ValueError(f"{field} 必須是文字")
    value = value.strip()
    if (required and not value) or len(value) > limit:
        raise ValueError(f"{field} 必須在 {'1' if required else '0'} 至 {limit} 字之內")
    return value


def clean_department(value):
    department = clean_text(value, "部門", 60).lstrip("#").strip()
    if not department or not department.isprintable():
        raise ValueError("部門名稱無效")
    return department


def clean_capabilities(value):
    if not isinstance(value, list) or len(value) > 12:
        raise ValueError("能力標籤必須是最多 12 個項目的陣列")
    return list(dict.fromkeys(clean_text(item, "能力標籤", 32) for item in value))


class CompanyStore:
    def __init__(self, bridge: BridgeStore) -> None:
        from .roadmap import RoadmapStore

        self.bridge = bridge
        self.roadmap = RoadmapStore(self)

    def initialize(self, conn):
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS workstations (
                seat_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                department TEXT NOT NULL,
                is_lead INTEGER NOT NULL DEFAULT 0 CHECK(is_lead IN (0,1)),
                capabilities TEXT NOT NULL DEFAULT '[]',
                sop_prompt TEXT NOT NULL CHECK(length(sop_prompt)<=300),
                assigned_agent_id TEXT UNIQUE REFERENCES members(agent_id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                department TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('todo','in_progress','review','done')),
                assignee_seat_id TEXT REFERENCES workstations(seat_id) ON DELETE SET NULL,
                assignee_agent_id TEXT REFERENCES members(agent_id) ON DELETE SET NULL,
                priority TEXT NOT NULL,
                result_summary TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status_department ON tasks(status,department);
            CREATE TABLE IF NOT EXISTS company_appointments (
                agent_id TEXT PRIMARY KEY REFERENCES members(agent_id) ON DELETE CASCADE,
                event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                hook_delivered INTEGER NOT NULL DEFAULT 0
            );
        """)
        self.roadmap.initialize(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(company_appointments)")}
            if "hook_delivered" not in columns:
                conn.execute("ALTER TABLE company_appointments ADD COLUMN hook_delivered INTEGER NOT NULL DEFAULT 0")
            seeded = conn.execute("INSERT OR IGNORE INTO settings VALUES ('company_seats_seeded','1')").rowcount
            if seeded:
                conn.executemany(
                    "INSERT OR IGNORE INTO workstations(seat_id,title,department,is_lead,capabilities,sop_prompt) "
                    "VALUES(?,?,?,?,?,?)",
                    [(sid, title, dept, lead, json.dumps(caps), sop) for sid, title, dept, lead, caps, sop in DEFAULT_SEATS],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @contextmanager
    def write(self):
        with self.bridge._connection() as conn:
            self.bridge._begin(conn)
            try:
                yield conn
                self.bridge._commit(conn)
            except Exception:
                self.bridge._rollback(conn)
                raise

    @staticmethod
    def seat_data(row):
        if row is None:
            return None
        return {**dict(row), "is_lead": bool(row["is_lead"]), "capabilities": json.loads(row["capabilities"])}

    @staticmethod
    def seat_for(conn, agent_id):
        return conn.execute("SELECT * FROM workstations WHERE assigned_agent_id=?", (agent_id,)).fetchone()

    def _seat(self, conn, seat_id):
        row = conn.execute("SELECT * FROM workstations WHERE seat_id=?", (seat_id,)).fetchone()
        if row is None:
            raise KeyError("找不到指定工位")
        return row

    def _employee(self, conn, agent_id):
        member = self.bridge._member_by_identity(conn, agent_id)
        if member is None:
            raise KeyError("找不到指定員工，請先報到")
        return member

    def workstations_in(self, conn):
        return [self.seat_data(row) for row in conn.execute(
            "SELECT * FROM workstations ORDER BY is_lead DESC, department, seat_id"
        )]

    def workstations(self):
        with self.bridge._connection() as conn:
            return self.workstations_in(conn)

    def employee_workstation(self, agent_id):
        with self.bridge._connection() as conn:
            return self.seat_data(self.seat_for(conn, agent_id))

    def channels_in(self, conn):
        rows = conn.execute(
            "SELECT department FROM workstations UNION SELECT department FROM tasks "
            "UNION SELECT channel FROM events WHERE archived_at IS NULL ORDER BY 1"
        )
        return [HQ, *(row[0] for row in rows if row[0] != HQ)]

    def channel_in(self, conn, agent_id, value=None):
        if value is None:
            seat = self.seat_for(conn, agent_id)
            return seat["department"] if seat else HQ
        name = clean_department(value)
        if name not in self.channels_in(conn):
            raise ValueError("找不到指定部門頻道")
        return name

    def snapshot_in(self, conn, agent_id):
        occupants = {row["agent_id"]: row for row in conn.execute(
            "SELECT agent_id,agent_name,capabilities_json FROM members"
        )}
        seats = self.workstations_in(conn)
        own_seat = next((seat for seat in seats if seat["assigned_agent_id"] == agent_id), None)
        for seat in seats:
            occupant = occupants.get(seat["assigned_agent_id"])
            seat["agent_name"] = occupant["agent_name"] if occupant else None
            seat["agent_capabilities"] = json.loads(occupant["capabilities_json"]) if occupant else []
        tasks = self.tasks_in(conn, active_only=True)
        roadmap = self.roadmap.state_in(conn, grouped=False)
        current = next((stage for stage in roadmap["stages"] if stage["status"] == "in_progress"), None)
        if own_seat and current:
            tasks.sort(key=lambda task: not (
                task["stage_id"] == current["stage_id"] and task["department"] == own_seat["department"]
                and task["status"] == "todo"
            ))
        return {
            **roadmap,
            "workstation": own_seat,
            "workstations": [{key: value for key, value in seat.items() if key != "sop_prompt"} for seat in seats],
            "tasks": tasks,
            "locks": [lock for lock in self.bridge._list_locks_in(conn) if lock["expires_at"] > time.time()],
        }

    def snapshot(self, agent_id):
        with self.bridge._connection() as conn:
            conn.execute("BEGIN")
            resolved, _ = self.bridge._resolve_agent(conn, agent_id)
            return self.snapshot_in(conn, resolved)

    def _appointment(self, conn, agent_id, now, actor=None):
        member = self._employee(conn, agent_id)
        target = "@" + member["agent_name"]
        seat = self.seat_data(self.seat_for(conn, agent_id))
        title = "工位任職令｜待命區"
        if seat:
            title = f"工位任職令｜{seat['title']}（{seat['seat_id']}）"
            content = f"{title}｜部門 {seat['department']}｜能力 {'、'.join(seat['capabilities']) or '無'}｜SOP {seat['sop_prompt'] or '未設定'}"
        else:
            content = title + "｜目前無指派工位。查看公司快照與待認領工單。"
        if actor:
            content += f"｜由管理員 {actor} 代派"
        event_id = self.bridge._insert_event(
            conn, "owner", "system", title, [], now, sender_name="老闆", sender_role="owner",
            is_owner=True, mention_to=target, target_agent_id=agent_id, priority=APPOINTMENT_PRIORITY,
            channel=HQ, content=content, update_status=False,
        )
        conn.execute(
            "INSERT INTO company_appointments(agent_id,event_id) VALUES (?,?) "
            "ON CONFLICT(agent_id) DO UPDATE SET event_id=excluded.event_id,hook_delivered=0",
            (agent_id, event_id),
        )
        return event_id

    def collect_appointment_in(self, conn, agent_id, *, via_hook=False):
        row = conn.execute(
            "SELECT e.content,a.hook_delivered FROM company_appointments a JOIN events e ON e.id=a.event_id WHERE a.agent_id=?",
            (agent_id,),
        ).fetchone()
        if row is None or (via_hook and row["hook_delivered"]):
            return []
        if via_hook:
            conn.execute("UPDATE company_appointments SET hook_delivered=1 WHERE agent_id=?", (agent_id,))
        else:
            conn.execute("DELETE FROM company_appointments WHERE agent_id=?", (agent_id,))
        return [row["content"]]

    def _publish_seats(self, events=(), tasks_changed=False):
        self.bridge._publish("workstations_updated", self.workstations())
        if tasks_changed:
            self.publish_tasks()
        if self.roadmap.state(grouped=False)["stages"]:
            self.roadmap.publish()
        for event_id in events:
            self.bridge._publish("new_message", self.bridge.get_message(event_id))

    def save_workstation(self, payload, seat_id=None):
        creating = seat_id is None
        if creating:
            seat_id = clean_text(payload.get("seat_id") or f"seat-{uuid.uuid4().hex[:8]}", "工位 ID", 60).lower()
            if not re.fullmatch(r"seat-[a-z0-9][a-z0-9-]*", seat_id):
                raise ValueError("工位 ID 必須以 seat- 開頭，並使用英數字與連字號")
        now = time.time()
        events = []
        with self.write() as conn:
            previous = None if creating else self.seat_data(self._seat(conn, seat_id))
            fields = {**(previous or {"is_lead": False, "capabilities": [], "sop_prompt": ""}), **payload}
            title = clean_text(fields.get("title"), "職稱", 100)
            department = clean_department(fields.get("department"))
            lead = fields["is_lead"]
            if not isinstance(lead, bool):
                raise ValueError("is_lead 必須是布林值")
            capabilities = clean_capabilities(fields["capabilities"])
            sop = clean_text(fields["sop_prompt"], "工位 SOP", 300, required=False)
            values = (title, department, lead, json.dumps(capabilities, ensure_ascii=False), sop)
            if creating:
                if conn.execute("SELECT 1 FROM workstations WHERE seat_id=?", (seat_id,)).fetchone():
                    raise TaskConflict("工位 ID 已存在")
                if self.bridge._member_by_identity(conn, seat_id):
                    raise TaskConflict("工位 ID 與員工名稱重複")
                conn.execute(
                    "INSERT INTO workstations(title,department,is_lead,capabilities,sop_prompt,seat_id) VALUES (?,?,?,?,?,?)",
                    (*values, seat_id),
                )
            else:
                conn.execute(
                    "UPDATE workstations SET title=?,department=?,is_lead=?,capabilities=?,sop_prompt=? WHERE seat_id=?",
                    (*values, seat_id),
                )
            saved = self.seat_data(self._seat(conn, seat_id))
            if saved != previous and saved["assigned_agent_id"]:
                events.append(self._appointment(conn, saved["assigned_agent_id"], now))
                self.bridge._archive_old(conn, now)
        self._publish_seats(events)
        return saved

    def assign_workstation(self, seat_id, identity, *, actor=None):
        now = time.time()
        with self.write() as conn:
            admin_name = None
            if actor is not None:
                admin = self.bridge._verified_member(conn, *actor)
                if not admin["is_admin"] or admin["muted"]:
                    raise PermissionError("只有老闆或未被靜音的管理員可以指派工位")
                admin_name = admin["agent_name"]
            seat = self._seat(conn, seat_id)
            new_agent = self._employee(conn, identity)["agent_id"] if identity is not None else None
            old_agent = seat["assigned_agent_id"]
            if new_agent == old_agent:
                return self.seat_data(seat)
            if new_agent is not None:
                conn.execute("UPDATE workstations SET assigned_agent_id=NULL WHERE assigned_agent_id=?", (new_agent,))
            conn.execute("UPDATE workstations SET assigned_agent_id=? WHERE seat_id=?", (new_agent, seat_id))
            affected = [(agent, assigned_seat) for agent, assigned_seat in ((old_agent, None), (new_agent, seat_id)) if agent]
            for agent, assigned_seat in affected:
                conn.execute(
                    "UPDATE tasks SET assignee_seat_id=?,updated_at=? WHERE assignee_agent_id=? AND status IN ('in_progress','review')",
                    (assigned_seat, now, agent),
                )
            events = [self._appointment(conn, agent, now, admin_name) for agent, _ in affected]
            self.bridge._archive_old(conn, now)
            saved = self.seat_data(self._seat(conn, seat_id))
        self._publish_seats(events, tasks_changed=True)
        return saved

    def delete_workstation(self, seat_id):
        now = time.time()
        with self.write() as conn:
            seat = self._seat(conn, seat_id)
            conn.execute("UPDATE tasks SET assignee_seat_id=NULL,updated_at=? WHERE assignee_seat_id=?", (now, seat_id))
            conn.execute("DELETE FROM workstations WHERE seat_id=?", (seat_id,))
            agent = seat["assigned_agent_id"]
            events = [self._appointment(conn, agent, now)] if agent else []
            self.bridge._archive_old(conn, now)
        self._publish_seats(events, tasks_changed=True)

    def employee_removed_in(self, conn, agent_id, now):
        stages = [row[0] for row in conn.execute(
            "SELECT DISTINCT stage_id FROM tasks WHERE assignee_agent_id=? AND status IN ('in_progress','review') "
            "AND stage_id IS NOT NULL", (agent_id,)
        )]
        conn.execute(
            "UPDATE tasks SET status=CASE WHEN status IN ('in_progress','review') THEN 'todo' ELSE status END,"
            "assignee_agent_id=NULL,updated_at=? WHERE assignee_agent_id=?",
            (now, agent_id),
        )
        for stage_id in stages:
            self.roadmap.recalculate_in(conn, stage_id, now)

    def _task_data(self, row):
        return {**dict(row), "updated_at": self.bridge._iso_time(row["updated_at"])}

    def tasks_in(self, conn, department=None, status=None, *, active_only=False):
        if department is not None:
            department = clean_department(department)
        if status is not None:
            self._validate_status(status)
        rows = conn.execute(
            "SELECT * FROM tasks WHERE (? IS NULL OR department=?) AND (? IS NULL OR status=?) "
            "AND (?=0 OR status IN ('todo','in_progress')) ORDER BY updated_at DESC,task_id",
            (department, department, status, status, active_only),
        )
        return [self._task_data(row) for row in rows]

    def list_tasks(self, department=None, status=None):
        with self.bridge._connection() as conn:
            return self.tasks_in(conn, department, status)

    def publish_tasks(self):
        self.bridge._publish("tasks_updated", self.list_tasks())

    def _publish_task_change(self, task, notices):
        self.publish_tasks()
        if task["stage_id"]:
            self.roadmap.publish(notices)

    @staticmethod
    def _validate_status(status):
        if status not in TASK_STATUSES:
            raise ValueError("工單狀態必須是 todo、in_progress、review 或 done")
        return status

    @staticmethod
    def _task(conn, task_id):
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError("找不到指定工單")
        return row

    def get_task(self, task_id):
        with self.bridge._connection() as conn:
            return self._task_data(self._task(conn, task_id))

    def require_lead_in(self, conn, identity):
        member = self._employee(conn, identity)
        seat = self.seat_for(conn, member["agent_id"])
        if not seat or not seat["is_lead"]:
            raise PermissionError("只有主管工位或老闆可以規劃階段及建立工單")

    @staticmethod
    def insert_task_in(conn, title, department, priority, now, *, assignee_seat_id=None, stage_id=None):
        task_id = "task-" + uuid.uuid4().hex[:12]
        conn.execute(
            "INSERT INTO tasks(task_id,title,department,status,assignee_seat_id,priority,updated_at,stage_id) "
            "VALUES (?,?,?,'todo',?,?,?,?)",
            (task_id, clean_text(title, "工單標題", 160), clean_department(department),
             assignee_seat_id, clean_text(priority, "優先級", 24), now, stage_id),
        )
        return task_id

    def create_task(self, title, department, priority="normal", *, identity=None, assignee_seat_id=None, stage_id=None):
        now = time.time()
        department = clean_department(department)
        with self.write() as conn:
            if identity is not None:
                self.require_lead_in(conn, identity)
            if assignee_seat_id is not None:
                seat = self._seat(conn, assignee_seat_id)
                if stage_id and seat["department"] != department:
                    raise TaskConflict(f"請為階段工單選擇 #{department} 的工位")
            if stage_id is not None and self.roadmap.stage_in(conn, stage_id)["status"] == "completed":
                raise TaskConflict("已完成的階段不能新增工單")
            task_id = self.insert_task_in(conn, title, department, priority, now,
                                          assignee_seat_id=assignee_seat_id, stage_id=stage_id)
            task = self._task_data(self._task(conn, task_id))
            notices = self.roadmap.recalculate_in(conn, stage_id, now)
        self._publish_task_change(task, notices)
        return task

    def _claimable_task_in(self, conn, agent, task_id):
        task = self._task(conn, task_id)
        seat = self.seat_for(conn, agent)
        if task["stage_id"]:
            stage = self.roadmap.stage_in(conn, task["stage_id"])
            if stage["status"] != "in_progress":
                raise TaskConflict(f"階段「{stage['title']}」{'尚未解鎖' if stage['status'] == 'pending' else '已完成'}")
            if not seat or seat["department"] != task["department"]:
                raise PermissionError(f"此工單須由 #{task['department']} 的員工認領")
        old_agent = task["assignee_agent_id"]
        seat_id = seat["seat_id"] if seat else None
        if task["status"] != "todo":
            if old_agent == agent and task["status"] == "in_progress":
                return task, seat_id
            owner = self._employee(conn, old_agent)["agent_name"] if old_agent else "未指定"
            raise TaskConflict(f"工單目前為 {task['status']}，負責人 {owner}，無法認領")
        if task["assignee_seat_id"] and task["assignee_seat_id"] != seat_id:
            reserved = self._seat(conn, task["assignee_seat_id"])
            holder = reserved["assigned_agent_id"]
            owner = self._employee(conn, holder)["agent_name"] if holder else "空缺"
            raise TaskConflict(f"工單已指派給工位 {reserved['title']}（{owner}）")
        return task, seat_id

    def handoff_ticket(self, identity, task_id):
        with self.bridge._connection() as conn:
            agent = self._employee(conn, identity)["agent_id"]
            task, _ = self._claimable_task_in(conn, agent, task_id)
            stage = self.roadmap.stage_in(conn, task["stage_id"]) if task["stage_id"] else None
            return {
                "task_id": task["task_id"], "title": task["title"], "department": task["department"],
                "status": task["status"], "驗收標準": stage["description"] if stage else "",
            }

    def ticket_recipient(self, task_id):
        with self.bridge._connection() as conn:
            ticket = self._task(conn, task_id)
            if not ticket["assignee_seat_id"]:
                raise TaskConflict("請先為工單指派工位")
            seat = self._seat(conn, ticket["assignee_seat_id"])
            if not seat["assigned_agent_id"]:
                raise TaskConflict(f"工位 {seat['title']} 目前空缺")
            employee = self._employee(conn, seat["assigned_agent_id"])
            if employee["muted"]:
                raise TaskConflict(f"{employee['agent_name']} 已被靜音")
            self._claimable_task_in(conn, employee["agent_id"], task_id)
            return self._task_data(ticket), employee["agent_name"]

    def claim_task(self, identity, task_id):
        with self.write() as conn:
            agent = self._employee(conn, identity)["agent_id"]
            task, seat_id = self._claimable_task_in(conn, agent, task_id)
            if task["status"] == "in_progress":
                return self._task_data(task)
            now = time.time()
            conn.execute(
                "UPDATE tasks SET status='in_progress', assignee_agent_id=?,assignee_seat_id=?,updated_at=? WHERE task_id=?",
                (agent, seat_id, now, task_id),
            )
            saved = self._task_data(self._task(conn, task_id))
            notices = self.roadmap.recalculate_in(conn, task["stage_id"], now)
        self._publish_task_change(saved, notices)
        return saved

    def complete_task(self, identity, task_id, result_summary):
        with self.write() as conn:
            agent = self._employee(conn, identity)["agent_id"]
            task = self._task(conn, task_id)
            if task["assignee_agent_id"] != agent:
                raise PermissionError("只有認領工單的員工可以回報完成")
            if task["status"] == "done":
                return self._task_data(task)
            if task["status"] not in ("in_progress", "review"):
                raise TaskConflict("工單必須先認領才可完成")
            result = " ".join(clean_text(result_summary, "完成結果", 300).split())
            now = time.time()
            conn.execute("UPDATE tasks SET status='done',result_summary=?,updated_at=? WHERE task_id=?",
                         (result, now, task_id))
            saved = self._task_data(self._task(conn, task_id))
            notices = self.roadmap.recalculate_in(conn, task["stage_id"], now)
        self._publish_task_change(saved, notices)
        return saved

    def update_task(self, task_id, payload):
        with self.write() as conn:
            old = self._task(conn, task_id)
            data = {**dict(old), **payload}
            status = self._validate_status(data["status"])
            if data["stage_id"] != old["stage_id"]:
                raise TaskConflict("工單的階段需在建立時指定")
            if old["stage_id"] and status != old["status"]:
                stage = self.roadmap.stage_in(conn, old["stage_id"])
                if stage["status"] != "in_progress":
                    raise TaskConflict("只有進行中的階段可以變更工單狀態")
            seat_id = data["assignee_seat_id"]
            seat = self._seat(conn, seat_id) if seat_id is not None else None
            department = clean_department(data["department"])
            if old["stage_id"] and seat and seat["department"] != department:
                raise TaskConflict(f"請為階段工單選擇 #{department} 的工位")
            agent = seat["assigned_agent_id"] if seat_id != old["assignee_seat_id"] else old["assignee_agent_id"]
            if status == "todo":
                agent = None
            if status in ("in_progress", "review") and agent is None:
                agent = seat["assigned_agent_id"] if seat else None
                if agent is None:
                    raise TaskConflict("請先指派一個已有員工入座的工位，或讓員工認領工單")
            result = clean_text(data["result_summary"], "完成結果", 300, required=False) if status != "todo" else ""
            now = time.time()
            conn.execute(
                "UPDATE tasks SET title=?,department=?,status=?,assignee_seat_id=?,assignee_agent_id=?,"
                "priority=?,result_summary=?,updated_at=? WHERE task_id=?",
                (clean_text(data["title"], "工單標題", 160), department, status,
                 seat_id, agent, clean_text(data["priority"], "優先級", 24), result, now, task_id),
            )
            saved = self._task_data(self._task(conn, task_id))
            notices = self.roadmap.recalculate_in(conn, old["stage_id"], now)
        self._publish_task_change(saved, notices)
        return saved
