import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import Mock, patch

from agentbridge.company import HQ, TaskConflict
from agentbridge.mention_replies import MentionReplies, run_agent
from agentbridge.store import BridgeStore


class CompanyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.store = BridgeStore(self.folder / "company.db")
        self.company = self.store.company
        self.engineer = self.store.join_group("codex", "gpt-6", "初始職責", ["Python-Test"])
        self.qa = self.store.join_group("codex", "gpt-6", "初始職責", ["Python-Test", "UI"], new_identity=True)
        self.lobby = self.store.join_group("antigravity", "gemini", "待命")

    def assign(self, seat, member):
        return self.company.assign_workstation(seat, member["agent_id"])

    def test_workstation_move_and_admin_authority(self):
        engineer, qa = self.engineer["agent_id"], self.qa["agent_id"]
        seat = self.company.save_workstation({
            "seat_id": "seat-ops", "title": "部署驗證", "department": "營運",
            "capabilities": ["Local-Test"], "sop_prompt": "驗證本機測試",
        })
        self.assertEqual(seat["assigned_agent_id"], None)
        self.assign("seat-engine", self.engineer)
        with self.assertRaises(PermissionError):
            self.company.assign_workstation("seat-ops", engineer, actor=(qa, self.qa["member_key"]))
        self.store.set_member_admin(qa, True)
        with self.assertRaises(PermissionError):
            self.company.assign_workstation("seat-ops", engineer, actor=(qa, "invalid"))
        moved = self.company.assign_workstation("seat-ops", engineer, actor=(qa, self.qa["member_key"]))
        self.assertEqual(moved["assigned_agent_id"], engineer)
        self.assertIsNone(next(s for s in self.company.workstations() if s["seat_id"] == "seat-engine")["assigned_agent_id"])
        self.assertIn(self.qa["agent_name"], self.store.get_group_state()["messages"][-1]["summary"])
        self.company.save_workstation({"sop_prompt": "僅執行測試"}, "seat-ops")
        self.assertIn("僅執行測試", self.store.tool_sync(engineer)[0])
        self.company.delete_workstation("seat-ops")
        self.assertIn("待命區", self.store.tool_sync(engineer)[0])

    def test_appointment_survives_window_mute_and_invocation_hooks(self):
        agent = self.engineer["agent_id"]
        sop = "依工單驗證。" + "測" * 280
        self.company.save_workstation({"sop_prompt": sop}, "seat-engine")
        self.assign("seat-engine", self.engineer)
        self.store.set_channel_enabled(False)
        self.assertEqual(self.store.tool_sync(agent), [])
        self.store.set_channel_enabled(True)
        for i in range(9):
            self.store.report_step(self.qa["agent_id"], "test", f"最近進度{i}")
        with self.store._connection() as conn:
            self.store._archive_old(conn, 1e10)
            conn.execute("UPDATE events SET archived_at=1e10 WHERE priority=1000")
        hook = self.store.tool_sync(agent, unread=True, via_hook=True)
        self.assertIn(sop, hook[0])
        self.assertEqual(len(hook), 5)
        self.assertFalse(self.store.tool_sync(agent, via_hook=True))
        self.assertIn(sop, self.store.tool_sync(agent)[0])
        self.assertEqual(self.store.tool_sync(agent), [])
        self.assertEqual(self.store.tool_sync(self.qa["agent_id"]), [])

    def test_department_isolation_and_seat_handoff(self):
        engineer, qa, lobby = (member["agent_id"] for member in (self.engineer, self.qa, self.lobby))
        self.assign("seat-engine", self.engineer)
        self.assign("seat-qa", self.qa)
        for identity in (engineer, qa, lobby):
            self.store.get_sync_status(identity)
        self.store.report_step(engineer, "test", "引擎專用報告")
        self.store.report_step(qa, "edit", "測試筆記")
        self.store.report_step(engineer, "test", "公司公告", channel=HQ)
        event = self.store.report_step(
            engineer, "handoff", "跨部門交接", mention_to="@seat-qa",
        )
        self.store.report_step(engineer, "edit", "無關工作", mention_to="@seat-qa")
        self.store.report_step(engineer, "handoff", "不可進待命區", mention_to="@" + self.lobby["agent_name"])
        self.store.owner_message("老闆跨部門指令", "@" + self.qa["agent_name"], "引擎研發")
        qa_updates = " ".join(self.store.get_sync_status(qa)["unread"])
        lobby_updates = " ".join(self.store.get_sync_status(lobby)["unread"])
        self.assertIn("老闆跨部門指令", qa_updates)
        self.assertIn("公司公告", qa_updates)
        self.assertIn("跨部門交接", qa_updates)
        self.assertNotIn("引擎專用報告", qa_updates)
        self.assertNotIn("無關工作", qa_updates)
        self.assertIn("公司公告", lobby_updates)
        self.assertNotIn("不可進待命區", lobby_updates)
        self.assertNotIn("老闆跨部門指令", lobby_updates)
        origin = self.store.get_message(event["event_id"])
        self.assertTrue(self.store.can_reply_to_mention(origin, qa))
        self.company.assign_workstation("seat-qa", lobby)
        self.assertFalse(self.store.can_reply_to_mention(origin, lobby))

    def test_ticket_claim_is_atomic_and_assignee_can_complete(self):
        first, second = self.engineer["agent_id"], self.qa["agent_id"]
        with self.assertRaises(PermissionError):
            self.company.create_task("工單", "總部", identity=first)
        self.assign("seat-cto", self.engineer)
        task_id = self.company.create_task("工單", "總部", identity=first)["task_id"]
        together = Barrier(2)

        def attempt(identity):
            together.wait()
            try:
                return self.company.claim_task(identity, task_id)
            except TaskConflict as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [first, second]))
        winner = next(value for value in results if isinstance(value, dict))
        conflict = next(value for value in results if isinstance(value, str))
        agent_id = winner["assignee_agent_id"]
        employee = self.engineer if agent_id == first else self.qa
        self.assertIn(employee["agent_name"], conflict)
        with self.assertRaises(PermissionError):
            self.company.complete_task(second if agent_id == first else first, task_id, "錯誤員工")
        self.company.update_task(task_id, {"status": "review"})
        self.assertEqual(self.company.complete_task(agent_id, task_id, "完成\n驗證")["result_summary"], "完成 驗證")

    def test_snapshot_and_unique_names_show_live_state(self):
        self.assertEqual(self.qa["agent_name"], "codex-gpt-6-2")
        first, second = self.engineer["agent_id"], self.qa["agent_id"]
        self.assign("seat-engine", self.engineer)
        self.assign("seat-qa", self.qa)
        self.store.lock_files(first, ["shared.py"])
        self.company.create_task("待認領工單", "引擎研發")
        active = self.company.create_task("認領工單", "QA驗收")
        self.company.claim_task(second, active["task_id"])
        done = self.company.create_task("歷史", "引擎研發")
        self.company.update_task(done["task_id"], {"status": "done"})
        join = self.store.join_group("codex", "gpt-6", "新人", new_identity=True)
        snapshot = join["snapshot"]
        self.assertEqual(join["agent_name"], "codex-gpt-6-3")
        self.assertIsNone(snapshot["workstation"])
        self.assertEqual(snapshot["locks"][0]["display_path"], "shared.py")
        self.assertEqual({task["status"] for task in snapshot["tasks"]}, {"todo", "in_progress"})
        qa = next(seat for seat in snapshot["workstations"] if seat["seat_id"] == "seat-qa")
        self.assertEqual(qa["agent_capabilities"], ["Python-Test", "UI"])
        self.assertNotIn("sop_prompt", qa)
        self.assertEqual(self.company.snapshot(second)["workstation"]["seat_id"], "seat-qa")
        self.store.remove_member(second)
        self.assertEqual(self.company.list_tasks("QA驗收")[0]["status"], "todo")
        self.assertIsNone(self.company.employee_workstation(second))

    def test_current_sop_reaches_toolless_chat_after_order_consumed(self):
        self.assign("seat-engine", self.engineer)
        self.store.tool_sync(self.engineer["agent_id"])
        sop = "先核對工程回歸紀錄，再依輸入矩陣驗證。"
        self.company.save_workstation({"sop_prompt": sop}, "seat-engine")
        self.store.tool_sync(self.engineer["agent_id"])
        seen = []

        def runner(exe, member, text, cancel):
            process = Mock(returncode=0)
            process.poll.return_value = 0
            process.communicate.side_effect = lambda data, timeout: (seen.append(data.decode()) or b"verified", b"")
            with patch("agentbridge.mention_replies.subprocess.Popen", return_value=process), \
                 patch("agentbridge.mention_replies.data_dir", return_value=self.folder):
                return run_agent(exe, member, text, Event())

        dispatcher = MentionReplies(self.store, resolve=lambda client: "codex.exe", run=runner)
        self.addCleanup(dispatcher.close)
        origin = self.store.owner_message("目前的工作崗位是什麼？", "@seat-engine")
        self.assertEqual(dispatcher.submit(origin)["replying"], [self.engineer["agent_name"]])
        dispatcher.pool.shutdown(wait=True)
        self.assertEqual(len(seen), 1)
        for field in ("引擎修復工程師", "seat-engine", "引擎研發", "Python-Test", sop):
            self.assertIn(field, seen[0])
        self.assertEqual(self.store.get_group_state()["messages"][-1]["summary"], "verified")


if __name__ == "__main__":
    unittest.main()
