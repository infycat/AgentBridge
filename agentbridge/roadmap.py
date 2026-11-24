from __future__ import annotations

import time
import uuid
from collections import defaultdict
from threading import Lock
from typing import TYPE_CHECKING

from .company import HQ, TaskConflict, clean_department, clean_text

if TYPE_CHECKING:
    from .company import CompanyStore

ROADMAP_PRIORITY = 500


class RoadmapStore:
    def __init__(self, company: CompanyStore) -> None:
        self.company = company
        self.bridge = company.bridge
        self._publish_lock = Lock()

    @staticmethod
    def initialize(conn):
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS goals (
                goal_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','completed')),
                created_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_goal ON goals(status) WHERE status='active';
            CREATE TABLE IF NOT EXISTS stages (
                stage_id TEXT PRIMARY KEY,
                goal_id TEXT NOT NULL REFERENCES goals(goal_id),
                stage_order INTEGER NOT NULL CHECK(stage_order>0),
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','in_progress','completed')),
                progress_percent INTEGER NOT NULL DEFAULT 0 CHECK(progress_percent BETWEEN 0 AND 100),
                completed_at REAL,
                UNIQUE(goal_id,stage_order)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_current_stage ON stages(goal_id) WHERE status='in_progress';
            CREATE TABLE IF NOT EXISTS roadmap_notices (
                event_id INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
                goal_id TEXT NOT NULL REFERENCES goals(goal_id),
                related_stage_id TEXT REFERENCES stages(stage_id),
                for_planners INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS roadmap_receipts (
                agent_id TEXT NOT NULL REFERENCES members(agent_id) ON DELETE CASCADE,
                event_id INTEGER NOT NULL REFERENCES roadmap_notices(event_id) ON DELETE CASCADE,
                PRIMARY KEY(agent_id,event_id)
            );
            CREATE TABLE IF NOT EXISTS roadmap_planning (
                goal_id TEXT PRIMARY KEY REFERENCES goals(goal_id),
                status TEXT NOT NULL CHECK(status IN ('running','failed')),
                detail TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
        """)
        if "stage_id" not in {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}:
            conn.execute("ALTER TABLE tasks ADD COLUMN stage_id TEXT REFERENCES stages(stage_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_stage ON tasks(stage_id,status,department)")

    def _goal_data(self, row):
        return {**dict(row), "created_at": self.bridge._iso_time(row["created_at"])} if row else None

    @staticmethod
    def _active(conn):
        return conn.execute("SELECT * FROM goals WHERE status='active'").fetchone()

    @staticmethod
    def _latest(conn):
        return conn.execute("SELECT * FROM goals ORDER BY (status='active') DESC,created_at DESC LIMIT 1").fetchone()

    def _stage_data(self, row):
        completed_at = row["completed_at"]
        return {**dict(row), "completed_at": self.bridge._iso_time(completed_at) if completed_at is not None else None}

    @staticmethod
    def stage_in(conn, stage_id):
        if not isinstance(stage_id, str) or not stage_id:
            raise ValueError("stage_id 必須是階段 ID")
        row = conn.execute("SELECT * FROM stages WHERE stage_id=?", (stage_id,)).fetchone()
        if row is None:
            raise KeyError("找不到指定階段")
        return row

    def state_in(self, conn, *, grouped=True):
        goal = self._latest(conn)
        stages = []
        if goal:
            stages = [self._stage_data(row) for row in conn.execute(
                "SELECT * FROM stages WHERE goal_id=? ORDER BY stage_order", (goal["goal_id"],)
            )]
        if grouped and stages:
            groups = defaultdict(dict)
            for row in conn.execute(
                "SELECT t.stage_id,t.task_id,t.title,t.department,t.status,t.assignee_agent_id,t.assignee_seat_id,"
                "m.agent_name AS assignee_name,w.title AS assignee_seat_title "
                "FROM tasks t JOIN stages s ON s.stage_id=t.stage_id "
                "LEFT JOIN members m ON m.agent_id=t.assignee_agent_id "
                "LEFT JOIN workstations w ON w.seat_id=t.assignee_seat_id "
                "WHERE s.goal_id=? ORDER BY t.department,t.title,t.task_id", (goal["goal_id"],)
            ):
                group = groups[row["stage_id"]].setdefault(row["department"], {
                    "department": row["department"], "total": 0, "done": 0, "tasks": [],
                })
                group["total"] += 1
                group["done"] += row["status"] == "done"
                group["tasks"].append(dict(row))
            for stage in stages:
                stage["departments"] = list(groups[stage["stage_id"]].values())
        goal_data = self._goal_data(goal)
        active = bool(goal and goal["status"] == "active")
        planning = None
        if active and not stages:
            row = conn.execute("SELECT * FROM roadmap_planning WHERE goal_id=?", (goal["goal_id"],)).fetchone()
            planning = {"status": "waiting", "detail": ""} if row is None else {
                "status": row["status"], "detail": row["detail"],
            }
            if row and row["status"] == "running" and time.time() - row["updated_at"] > 150:
                planning = {"status": "failed", "detail": "背景規劃已逾時，可重新派送或由規劃者接手。"}
        return {
            "active_goal": goal_data if active else None,
            "completed_goal": goal_data if goal and not active else None,
            "stages": stages,
            "planning": planning,
        }

    def state(self, *, grouped=True):
        with self.bridge._connection() as conn:
            conn.execute("BEGIN")
            return self.state_in(conn, grouped=grouped)

    def publish(self, events=()):
        with self._publish_lock:
            self.bridge._publish("stages_updated", self.state())
            for event_id in events:
                try:
                    message = self.bridge.get_message(event_id)
                except KeyError:
                    continue
                self.bridge._publish("new_message", message)

    def update_planning(self, goal_id, status, detail=""):
        with self.company.write() as conn:
            changed = conn.execute(
                "INSERT INTO roadmap_planning(goal_id,status,detail,updated_at) "
                "SELECT goal_id,?,?,? FROM goals WHERE goal_id=? AND status='active' "
                "AND NOT EXISTS(SELECT 1 FROM stages WHERE goal_id=goals.goal_id) "
                "ON CONFLICT(goal_id) DO UPDATE SET status=excluded.status, detail=excluded.detail, "
                "updated_at=excluded.updated_at",
                (status, detail, time.time(), goal_id),
            ).rowcount
        if changed:
            self.publish()

    def departments(self):
        with self.bridge._connection() as conn:
            return [row[0] for row in conn.execute("SELECT DISTINCT department FROM workstations ORDER BY department")]

    def _notice(self, conn, goal_id, summary, now, *, stage_id=None, planners=False, content=None):
        event_id = self.bridge._insert_event(
            conn, "owner" if planners else "system", "system", summary, [], now,
            sender_name="老闆" if planners else "系統", sender_role="owner" if planners else "system",
            is_owner=planners, priority=ROADMAP_PRIORITY, channel=HQ, content=content, update_status=False,
        )
        conn.execute("INSERT INTO roadmap_notices VALUES(?,?,?,?)", (event_id, goal_id, stage_id, planners))
        self.bridge._archive_old(conn, now)
        return event_id

    def _create_goal_in(self, conn, title, description, now):
        goal_id = "goal-" + uuid.uuid4().hex[:12]
        conn.execute("INSERT INTO goals VALUES(?,?,?,'active',?)", (goal_id, title, description, now))
        return goal_id

    def create_goal(self, title, description):
        title = clean_text(title, "目標標題", 100)
        description = clean_text(description, "目標說明", 2000, required=False)
        with self.company.write() as conn:
            if self._active(conn):
                raise TaskConflict("已有進行中的總目標，完成後即可發布下一個目標")
            now = time.time()
            goal_id = self._create_goal_in(conn, title, description, now)
            event_id = self._goal_notice_in(conn, goal_id, title, description, now)
            goal = self._goal_data(self._active(conn))
        self.publish([event_id])
        return goal

    def _goal_notice_in(self, conn, goal_id, title, description, now):
        return self._notice(
            conn, goal_id, f"規劃者請拆解總目標 {title}", now, planners=True,
            content=f"規劃指令｜{goal_id}｜{title}\n{description}\n"
                    "請立即以自己的 agent_id 呼叫 create_staged_plan，填入此 goal_id 與各階段及部門工單。",
        )

    def restore_pending_notice_in(self, conn, now):
        goal = self._active(conn)
        if goal and not conn.execute("SELECT 1 FROM stages WHERE goal_id=?", (goal["goal_id"],)).fetchone():
            self._goal_notice_in(conn, goal["goal_id"], goal["title"], goal["description"], now)

    def create_plan(self, identity, goal_title, stages, *, goal_id=None):
        title = clean_text(goal_title, "目標標題", 100)
        if not isinstance(stages, list) or not 1 <= len(stages) <= 20:
            raise ValueError("每個總目標須有 1 至 20 個階段")
        with self.company.write() as conn:
            if identity is not None:
                self.company.require_lead_in(conn, identity)
            now = time.time()
            goal = self._active(conn)
            if goal is None:
                if goal_id:
                    raise TaskConflict("該目標目前無法建立規劃")
                goal_id = self._create_goal_in(conn, title, "", now)
            else:
                if (goal_id and goal["goal_id"] != goal_id) or (not goal_id and goal["title"] != title):
                    raise TaskConflict("已有其他總目標。請使用目前目標的 goal_id")
                goal_id = goal["goal_id"]
                title = goal["title"]
            if conn.execute("SELECT 1 FROM stages WHERE goal_id=?", (goal_id,)).fetchone():
                raise TaskConflict("此目標已有階段規劃，請查看公司快照")
            departments = {row[0] for row in conn.execute("SELECT DISTINCT department FROM workstations")}
            for index, stage in enumerate(stages, 1):
                if not isinstance(stage, dict):
                    raise ValueError("階段必須是物件")
                stage_title = clean_text(stage.get("title"), "階段名稱", 120)
                description = clean_text(stage.get("description"), "階段驗收標準", 1200)
                tasks = stage.get("tasks")
                if not isinstance(tasks, list) or not 1 <= len(tasks) <= 30:
                    raise ValueError(f"階段 {index} 須有 1 至 30 張工單")
                stage_id = "stage-" + uuid.uuid4().hex[:12]
                conn.execute(
                    "INSERT INTO stages VALUES(?,?,?,?,?,?,0,NULL)",
                    (stage_id, goal_id, index, stage_title, description, "in_progress" if index == 1 else "pending"),
                )
                for task in tasks:
                    if not isinstance(task, dict):
                        raise ValueError("工單必須是物件")
                    department = clean_department(task.get("department"))
                    if department not in departments:
                        raise ValueError(f"找不到部門 #{department} 的工位，請先建立工位或選用現有部門")
                    self.company.insert_task_in(conn, task.get("title"), department, task.get("priority"),
                                                now, stage_id=stage_id)
            conn.execute("DELETE FROM roadmap_planning WHERE goal_id=?", (goal_id,))
            event = self._notice(conn, goal_id, f"「{title}」規劃已發布，第 1 階段開始。請查看公司快照及部門工單。", now)
            result = self.state_in(conn)
        self.company.publish_tasks()
        self.publish([event])
        return result

    def recalculate_in(self, conn, stage_id, now):
        if stage_id is None:
            return []
        stage = self.stage_in(conn, stage_id)
        if stage["status"] != "in_progress":
            return []
        total, done, weight = conn.execute(
            "SELECT COUNT(*),COALESCE(SUM(status='done'),0),"
            "COALESCE(SUM(CASE status WHEN 'done' THEN 100 WHEN 'review' THEN 70 "
            "WHEN 'in_progress' THEN 30 ELSE 0 END),0) FROM tasks WHERE stage_id=?", (stage_id,)
        ).fetchone()
        complete = total > 0 and done == total
        percent = 100 if complete else min(99, (2 * weight + total) // (2 * total) if total else 0)
        conn.execute("UPDATE stages SET progress_percent=? WHERE stage_id=?", (percent, stage_id))
        if not complete:
            return []
        conn.execute("UPDATE stages SET status='completed',completed_at=? WHERE stage_id=?", (now, stage_id))
        goal_id = stage["goal_id"]
        following = conn.execute(
            "SELECT * FROM stages WHERE goal_id=? AND status='pending' ORDER BY stage_order LIMIT 1", (goal_id,)
        ).fetchone()
        if following:
            conn.execute("UPDATE stages SET status='in_progress' WHERE stage_id=?", (following["stage_id"],))
            summary = f"「{stage['title']}」已完成。第 {following['stage_order']} 階段「{following['title']}」已解鎖，請認領部門工單。"
            related = following["stage_id"]
        else:
            conn.execute("UPDATE goals SET status='completed' WHERE goal_id=?", (goal_id,))
            summary = f"「{stage['title']}」已完成，總目標的所有階段均已完成。"
            related = None
        return [self._notice(conn, goal_id, summary, now, stage_id=related)]

    def collect_notices_in(self, conn, member, seat, limit):
        if member is None or limit <= 0:
            return []
        goal = self._latest(conn)
        if not goal:
            return []
        is_lead = bool(seat and seat["is_lead"] and not member["muted"])
        rows = conn.execute(
            "SELECT n.event_id,n.for_planners,e.content,e.line FROM roadmap_notices n JOIN events e ON e.id=n.event_id "
            "WHERE n.goal_id=? AND e.removal_kind='' AND NOT EXISTS("
            " SELECT 1 FROM roadmap_receipts r WHERE r.event_id=n.event_id AND r.agent_id=?"
            ") AND ((n.for_planners=1 AND ? AND NOT EXISTS(SELECT 1 FROM stages WHERE goal_id=n.goal_id)) OR"
            " (n.for_planners=0 AND (n.related_stage_id IS NULL OR EXISTS("
            "  SELECT 1 FROM tasks t WHERE t.stage_id=n.related_stage_id AND t.department=?)))) "
            "ORDER BY n.for_planners DESC,n.event_id DESC LIMIT ?",
            (goal["goal_id"], member["agent_id"], is_lead, seat["department"] if seat else None, limit),
        ).fetchall()
        conn.executemany("INSERT INTO roadmap_receipts VALUES(?,?)", ((member["agent_id"], row["event_id"]) for row in rows))
        return [row["content"] if row["for_planners"] else row["line"] for row in rows]

    def planners(self, goal_id):
        with self.bridge._connection() as conn:
            if not self.bridge._enabled_in(conn):
                return []
            goal = self._active(conn)
            if not goal or goal["goal_id"] != goal_id or conn.execute(
                "SELECT 1 FROM stages WHERE goal_id=?", (goal_id,)
            ).fetchone():
                return []
            return [self.bridge._member_data(row) for row in conn.execute(
                "SELECT m.* FROM workstations w JOIN members m ON m.agent_id=w.assigned_agent_id "
                "WHERE w.is_lead=1 AND m.muted=0 ORDER BY w.seat_id"
            )]
