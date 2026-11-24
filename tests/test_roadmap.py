import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import Mock, patch

from agentbridge.company import TaskConflict
from agentbridge.mention_replies import MentionReplies, run_agent
from agentbridge.store import BridgeStore


def plan():
    return [
        {"title": "先驗收輸入", "description": "輸入行為與規格相符", "tasks": [
            {"title": "驗收 A", "department": "引擎研發", "priority": "normal"},
            {"title": "驗收 B", "department": "引擎研發", "priority": "high"},
        ]},
        {"title": "確認整合", "description": "整合測試通過", "tasks": [
            {"title": "測試整合", "department": "QA驗收", "priority": "normal"},
        ]},
    ]


class RoadmapTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.folder = Path(scratch.name)
        self.store = BridgeStore(self.folder / "test.db")
        self.company = self.store.company
        self.roadmap = self.company.roadmap
        people = [self.store.join_group("codex", model, model, new_identity=True)["agent_id"]
                  for model in ("lead", "engine", "qa", "other")]
        self.lead, self.engine, self.qa, self.lobby = people
        for seat_id, agent in zip(("seat-cto", "seat-engine", "seat-qa"), people):
            self.company.assign_workstation(seat_id, agent)
        for agent in people:
            self.store.tool_sync(agent, unread=True)

    def publish_plan(self):
        goal = self.roadmap.create_goal("交付新版本", "交付通過驗收的版本")
        data = self.roadmap.create_plan(self.lead, goal["title"], plan(), goal_id=goal["goal_id"])
        return data["stages"]

    def test_full_lifecycle_and_department_updates(self):
        published = []
        self.store.add_listener(lambda kind, data: published.append((kind, data)))
        goal = self.roadmap.create_goal("交付新版本", "保留舊版欄位")
        self.assertNotIn(goal["goal_id"], " ".join(self.store.tool_sync(self.engine, unread=True)))
        self.assertIn("保留舊版欄位", self.store.tool_sync(self.lead, unread=True)[0])
        self.assertEqual(self.store.tool_sync(self.lead, unread=True), [])
        with self.assertRaises(PermissionError):
            self.roadmap.create_plan(self.qa, goal["title"], plan())
        first, second = self.roadmap.create_plan(self.lead, goal["title"], plan())["stages"]
        self.assertEqual([first["status"], second["status"]], ["in_progress", "pending"])
        self.assertIn("第 1 階段開始", " ".join(self.store.tool_sync(self.lobby)))
        a, b = [item["task_id"] for item in first["departments"][0]["tasks"]]
        next_task = second["departments"][0]["tasks"][0]["task_id"]
        with self.assertRaisesRegex(TaskConflict, "尚未解鎖"):
            self.company.claim_task(self.qa, next_task)
        with self.assertRaises(TaskConflict):
            self.company.update_task(next_task, {"status": "done"})
        with self.assertRaisesRegex(PermissionError, "引擎研發"):
            self.company.claim_task(self.qa, a)
        snapshot = self.company.snapshot(self.engine)
        self.assertEqual(snapshot["active_goal"]["goal_id"], goal["goal_id"])
        self.assertEqual([item["stage_id"] for item in snapshot["tasks"][:2]], [first["stage_id"]] * 2)
        self.company.claim_task(self.engine, a)
        self.assertEqual(self.roadmap.state()["stages"][0]["progress_percent"], 15)
        self.company.update_task(a, {"status": "review"})
        self.assertEqual(self.roadmap.state()["stages"][0]["progress_percent"], 35)
        self.company.complete_task(self.engine, a, "A 已完成")
        self.assertEqual(self.roadmap.state()["stages"][0]["progress_percent"], 50)
        self.company.claim_task(self.engine, b)
        self.assertEqual(self.roadmap.state()["stages"][0]["progress_percent"], 65)
        self.company.complete_task(self.engine, b, "B 已完成")
        first, second = self.roadmap.state()["stages"]
        self.assertEqual((first["status"], first["progress_percent"], second["status"]), ("completed", 100, "in_progress"))
        self.assertTrue(first["completed_at"])
        self.assertIn("已解鎖", " ".join(self.store.tool_sync(self.qa, unread=True)))
        self.assertNotIn("已解鎖", " ".join(self.store.tool_sync(self.engine, unread=True)))
        self.assertTrue(any(kind == "stages_updated" and data["stages"] and data["stages"][0]["progress_percent"] == 65
                            for kind, data in published))
        claimed = self.company.claim_task(self.qa, next_task)
        self.assertEqual(claimed["stage_id"], second["stage_id"])
        self.company.complete_task(self.qa, next_task, "整合通過")
        finished = self.roadmap.state()
        self.assertIsNone(finished["active_goal"])
        self.assertEqual(finished["completed_goal"]["goal_id"], goal["goal_id"])
        self.assertEqual([stage["progress_percent"] for stage in finished["stages"]], [100, 100])
        with self.assertRaises(TaskConflict):
            self.company.update_task(a, {"status": "todo"})
        self.assertNotEqual(self.roadmap.create_goal("下一目標", "")["goal_id"], goal["goal_id"])

    def test_atomically_rejects_incomplete_and_concurrent_plans(self):
        goal = self.roadmap.create_goal("交付", "")
        invalid = plan()
        del invalid[1]["tasks"][0]["priority"]
        with self.assertRaises(ValueError):
            self.roadmap.create_plan(self.lead, goal["title"], invalid)
        self.assertEqual(self.roadmap.state()["stages"], [])
        self.assertEqual(self.company.list_tasks(), [])
        together = Barrier(2)

        def attempt(_):
            together.wait()
            try:
                self.roadmap.create_plan(self.lead, goal["title"], plan(), goal_id=goal["goal_id"])
            except TaskConflict:
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(attempt, range(2))), [False, True])
        self.assertEqual(len(self.roadmap.state()["stages"]), 2)
        self.assertEqual(len(self.company.list_tasks()), 3)
        with self.assertRaises(TaskConflict):
            self.roadmap.create_goal("另一目標", "")

    def test_pending_notice_survives_mute_and_event_archival(self):
        goal = self.roadmap.create_goal("交付", "完整規劃標準")
        self.store.set_channel_enabled(False)
        self.assertEqual(self.store.tool_sync(self.lead), [])
        with self.store._connection() as conn:
            conn.execute("UPDATE events SET archived_at=123 WHERE priority=500")
        self.store.set_channel_enabled(True)
        self.assertIn(goal["goal_id"], self.store.tool_sync(self.lead)[0])
        self.assertEqual(self.store.tool_sync(self.lead), [])

    def test_concurrent_finishes_unlock_the_next_stage_once(self):
        engineer_two = self.store.join_group("codex", "engine-two", "測試員", new_identity=True)["agent_id"]
        self.company.save_workstation({
            "seat_id": "seat-engine-two", "title": "測試員", "department": "引擎研發",
        })
        self.company.assign_workstation("seat-engine-two", engineer_two)
        stages = self.publish_plan()
        events = []
        self.store.add_listener(lambda kind, data: events.append(data) if kind == "new_message" else None)
        active_tasks = self.company.list_tasks("引擎研發")
        pairs = [(agent, task["task_id"]) for agent, task in zip((self.engine, engineer_two), active_tasks)]
        for agent, task_id in pairs:
            self.company.claim_task(agent, task_id)
        together = Barrier(2)

        def finish(pair):
            together.wait()
            return self.company.complete_task(*pair, "驗收通過")

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual([task["status"] for task in pool.map(finish, pairs)], ["done", "done"])
        updates = [event for event in events if "已解鎖" in event["summary"]]
        self.assertEqual(len(updates), 1)
        current = self.roadmap.state()["stages"][1]
        self.assertEqual((current["stage_id"], current["status"]), (stages[1]["stage_id"], "in_progress"))

    def test_old_database_and_unstaged_task_remain_compatible(self):
        path = self.folder / "previous.db"
        with patch("agentbridge.roadmap.RoadmapStore.initialize"):
            old = BridgeStore(path)
            with old._connection() as conn:
                conn.execute(
                    "INSERT INTO tasks(task_id,title,department,status,priority,updated_at) VALUES(?,?,?,?,?,?)",
                    ("old-ticket", "Legacy ticket", "舊部門", "todo", "normal", 1),
                )
        migrated = BridgeStore(path)
        employee = migrated.join_group("codex", "old", "待命")
        ticket = migrated.company.claim_task(employee["agent_id"], "old-ticket")
        self.assertEqual(ticket["title"], "Legacy ticket")
        self.assertIsNone(ticket["stage_id"])
        self.assertEqual(migrated.company.complete_task(employee["agent_id"], ticket["task_id"], "完成")["status"], "done")

    def test_background_planner_uses_goal_and_mcp_without_project_writes(self):
        goal = self.roadmap.create_goal("交付", "背景規劃目標")
        seen = []
        filled = Event()

        def runner(exe, member, text, cancel, **settings):
            process = Mock(returncode=0)
            process.poll.return_value = 0
            process.communicate.side_effect = lambda data, timeout: (seen.append(data.decode()) or b"planned", b"")
            with patch("agentbridge.mention_replies.subprocess.Popen", return_value=process) as spawn, \
                 patch("agentbridge.mention_replies.data_dir", return_value=self.folder):
                run_agent(exe, member, text, cancel, **settings)
                seen.append(spawn.call_args.args[0])
            self.roadmap.create_plan(member["agent_id"], goal["title"], plan(), goal_id=goal["goal_id"])
            filled.set()
            return "planned"

        dispatcher = MentionReplies(self.store, resolve=lambda client: "codex.exe", run=runner,
                                    mcp_url="http://127.0.0.1:8765/mcp")
        self.addCleanup(dispatcher.close)
        self.assertTrue(dispatcher.submit_planning(goal)["queued"])
        self.assertTrue(filled.wait(3))
        self.assertIn("create_staged_plan", seen[0])
        self.assertIn(goal["goal_id"], seen[0])
        self.assertIn(self.lead, seen[0])
        self.assertIn("read-only", seen[1])
        self.assertIn('mcp_servers.AgentBridge.url="http://127.0.0.1:8765/mcp"', seen[1])
        self.assertIn('mcp_servers.AgentBridge.tools.create_staged_plan.approval_mode="approve"', seen[1])
        self.assertIn('mcp_servers.AgentBridge.enabled_tools=["create_staged_plan"]', seen[1])
        self.assertFalse(dispatcher.submit_planning(goal)["queued"])

    def test_antigravity_returned_plan_is_validated_and_failed_jobs_can_retry(self):
        import json

        agy = self.store.join_group("antigravity", "gemini", "規劃員")
        self.company.assign_workstation("seat-cto", agy["agent_id"])
        goal = self.roadmap.create_goal("交付", "背景規劃目標")

        def run(answer):
            dispatcher = MentionReplies(
                self.store, resolve=lambda client: "agy.exe",
                run=lambda *args, **kwargs: answer, mcp_url="http://127.0.0.1:8765/mcp",
            )
            try:
                self.assertTrue(dispatcher.submit_planning(goal)["queued"])
            finally:
                dispatcher.pool.shutdown(wait=True)
                dispatcher.close()

        run("這不是可驗證的規劃")
        self.assertEqual(self.roadmap.state()["planning"]["status"], "failed")
        self.assertEqual(self.company.list_tasks(), [])
        invalid = plan()
        invalid[0]["tasks"][0]["department"] = "打錯的部門"
        run(json.dumps({"stages": invalid}))
        self.assertEqual(self.roadmap.state()["planning"]["status"], "failed")
        self.assertEqual(self.company.list_tasks(), [])
        run(json.dumps({"stages": plan()}))
        self.assertEqual(len(self.roadmap.state()["stages"]), 2)
        self.assertIsNone(self.roadmap.state()["planning"])


if __name__ == "__main__":
    unittest.main()
